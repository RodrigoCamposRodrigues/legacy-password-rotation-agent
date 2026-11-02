"""Receives rotation webhooks, runs the protocol one run at a time, sweeps for missed work, and shows runs."""

import queue
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from rotator.protocol import Deps, build_graph, resume_run, run_view, start_run

TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
LIVE = ("queued", "running", "escalated", "crashed")
SHOT_NAME = re.compile(r"^\d{3}-[\w-]+\.png$")


class RotationDue(BaseModel):
    event_id: str
    device_serial: str
    due_at: str


class ClockChange(BaseModel):
    advance_days: float


class RunStore:
    """Service bookkeeping: which events were seen and which runs exist. Holds no secrets."""

    def __init__(self, path: Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock, self.conn:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, serial TEXT, due_at TEXT, run_id TEXT, received_at TEXT);
                CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, serial TEXT, event_id TEXT, trigger TEXT,
                    status TEXT, outcome TEXT, reason TEXT, created_at TEXT, updated_at TEXT);
            """)

    def record_event(self, event: RotationDue, run_id: str, now: str) -> str | None:
        """Returns the existing run id if this event was already received."""
        with self.lock, self.conn:
            row = self.conn.execute("SELECT run_id FROM events WHERE event_id = ?", (event.event_id,)).fetchone()
            if row:
                return row["run_id"]
            self.conn.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?)",
                              (event.event_id, event.device_serial, event.due_at, run_id, now))
        return None

    def add_run(self, run_id: str, serial: str, event_id: str, trigger: str, now: str) -> None:
        with self.lock, self.conn:
            self.conn.execute("INSERT INTO runs VALUES (?, ?, ?, ?, 'queued', '', '', ?, ?)",
                              (run_id, serial, event_id, trigger, now, now))

    def update(self, run_id: str, now: str, **fields) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.lock, self.conn:
            self.conn.execute(f"UPDATE runs SET {sets}, updated_at = ? WHERE run_id = ?", (*fields.values(), now, run_id))

    def get(self, run_id: str):
        with self.lock:
            return self.conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()

    def all(self):
        with self.lock:
            return self.conn.execute("SELECT * FROM runs ORDER BY created_at DESC").fetchall()

    def with_status(self, *statuses: str):
        with self.lock:
            marks = ",".join("?" * len(statuses))
            return self.conn.execute(f"SELECT * FROM runs WHERE status IN ({marks})", statuses).fetchall()


class Worker:
    """One thread, one run at a time: devices share one dashboard session limit and one browser."""

    def __init__(self, graph, store: RunStore, clock):
        self.graph, self.store, self.clock = graph, store, clock
        self.jobs: queue.Queue = queue.Queue()
        self.thread = threading.Thread(target=self._loop, daemon=True, name="rotation-worker")
        self.thread.start()

    def submit(self, kind: str, run_id: str, **args) -> None:
        self.jobs.put((kind, run_id, args))

    def wait_idle(self) -> None:
        self.jobs.join()

    def _loop(self) -> None:
        while True:
            kind, run_id, args = self.jobs.get()
            now = self.clock.now().isoformat()
            try:
                self.store.update(run_id, now, status="running")
                if kind == "start":
                    view = start_run(self.graph, args["serial"], args.get("event_id", ""), run_id=run_id)
                else:
                    view = resume_run(self.graph, run_id, args.get("decision"))
                self.store.update(run_id, self.clock.now().isoformat(), status=view.get("status", ""),
                                  outcome=view.get("outcome", ""), reason=view.get("reason", ""))
            except Exception as error:  # noqa: BLE001 - the checkpoint lets a human or restart resume it
                self.store.update(run_id, self.clock.now().isoformat(), status="crashed",
                                  reason=f"{type(error).__name__}: {str(error)[:200]}")
            finally:
                self.jobs.task_done()


def create_service(deps: Deps, store_path: Path, checkpointer, test_routes: bool = True) -> FastAPI:
    store = RunStore(store_path)
    deps.is_run_alive = lambda run_id: (row := store.get(run_id)) is not None and row["status"] in LIVE
    graph = build_graph(deps, checkpointer)
    worker = Worker(graph, store, deps.clock)
    app = FastAPI(title="Rotation service")
    app.state.store, app.state.worker, app.state.graph, app.state.deps = store, worker, graph, deps

    def now() -> str:
        return deps.clock.now().isoformat()

    def queue_run(serial: str, trigger: str, event_id: str = "", run_id: str | None = None) -> str:
        run_id = run_id or uuid.uuid4().hex[:12]
        store.add_run(run_id, serial, event_id, trigger, now())
        worker.submit("start", run_id, serial=serial, event_id=event_id)
        return run_id

    def sweep() -> list[dict]:
        """Catches what webhooks missed: overdue devices and rotations left half-done."""
        busy = {row["serial"] for row in store.with_status("queued", "running", "escalated", "crashed")}
        started = []
        for serial in deps.vault.list_devices():
            if serial in busy:
                continue
            holder = deps.vault.lock_holder(serial)
            if holder and deps.is_run_alive(holder):
                continue
            record = deps.vault.get_device(serial)
            due = datetime.fromisoformat(record.active_since) + timedelta(days=deps.settings.rotation_days)
            if record.state == "pending":
                reason = "recover_pending"
            elif due <= deps.clock.now():
                reason = "overdue"
            else:
                continue
            started.append({"serial": serial, "reason": reason, "run_id": queue_run(serial, f"sweep:{reason}")})
        return started

    app.state.sweep = sweep

    for row in store.with_status("running", "queued"):
        # Left over from a process that died: continue from the last checkpoint, or start if it never ran.
        if graph.get_state({"configurable": {"thread_id": row["run_id"]}}).values:
            worker.submit("resume", row["run_id"])
        else:
            worker.submit("start", row["run_id"], serial=row["serial"], event_id=row["event_id"])

    @app.post("/webhooks/rotation-due", status_code=202)
    def rotation_due(event: RotationDue):
        try:
            deps.vault.get_device(event.device_serial)
        except KeyError:
            raise HTTPException(404, "unknown device") from None
        run_id = uuid.uuid4().hex[:12]
        existing = store.record_event(event, run_id, now())
        if existing:
            return {"status": "duplicate", "run_id": existing}
        queue_run(event.device_serial, "webhook", event.event_id, run_id=run_id)
        return {"status": "accepted", "run_id": run_id}

    @app.post("/sweep")
    def sweep_route():
        return {"started": sweep()}

    @app.post("/runs/sweep")
    def sweep_from_page():
        sweep()
        return RedirectResponse("/runs", status_code=303)

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/api/runs/{run_id}")
    def run_json(run_id: str):
        row = store.get(run_id)
        if not row:
            raise HTTPException(404)
        return {**dict(row), "state": run_view(graph, run_id)}

    @app.get("/", response_class=HTMLResponse)
    @app.get("/runs", response_class=HTMLResponse)
    def runs_page(request: Request):
        return TEMPLATES.TemplateResponse(request, "runs.html", {"runs": store.all()})

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_page(request: Request, run_id: str):
        row = store.get(run_id)
        if not row:
            raise HTTPException(404)
        events = deps.audit(run_id).read()
        return TEMPLATES.TemplateResponse(request, "run.html", {"run": row, "view": run_view(graph, run_id), "events": events})

    @app.get("/runs/{run_id}/shots/{name}")
    def screenshot(run_id: str, name: str):
        path = deps.audit(run_id).run_dir / name
        if not SHOT_NAME.match(name) or not store.get(run_id) or not path.exists():
            raise HTTPException(404)
        return FileResponse(path)

    @app.post("/runs/{run_id}/resume")
    def resume(run_id: str, decision: str = Form("")):
        row = store.get(run_id)
        if not row:
            raise HTTPException(404)
        view = run_view(graph, run_id)
        options = (view.get("waiting_for") or {}).get("options", [])
        if row["status"] == "crashed" and not decision:
            worker.submit("resume", run_id)
        elif decision in options:
            worker.submit("resume", run_id, decision=decision)
        else:
            raise HTTPException(400, "this run is not waiting for that decision")
        store.update(run_id, now(), status="queued")
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    if test_routes:
        @app.post("/_test/clock")
        def advance_clock(change: ClockChange):
            clock = deps.clock
            if hasattr(clock, "advance"):
                clock.advance(change.advance_days)
            else:
                clock.offset_days += change.advance_days
            return {"now": clock.now().isoformat()}

    return app
