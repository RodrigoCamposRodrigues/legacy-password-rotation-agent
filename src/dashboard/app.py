"""A deliberately old-fashioned device dashboard with no API: the system the agent has to operate."""

import asyncio
import math
import secrets
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from dashboard.db import CT200, FL9, connect, init_db, password_valid
from dashboard.faults import Faults
from dashboard.notifier import Notifier
from simclock import Clock, FakeClock, SystemClock

PAGE_SIZE = 10
TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")


class DeviceLogin(BaseModel):
    password: str


class ClockChange(BaseModel):
    advance_days: float


def create_app(db_path: Path, clock: Clock | None = None, webhook_url: str | None = None,
               test_routes: bool = True) -> FastAPI:
    db_path = Path(db_path)
    init_db(db_path)
    app = FastAPI(title="ColdChain Manager", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.faults = Faults()
    app.state.clock = clock or SystemClock()
    app.state.notifier = Notifier(db_path, app.state.clock, webhook_url)

    def render(request: Request, name: str, status_code: int = 200, **context):
        return TEMPLATES.TemplateResponse(request, name, context, status_code=status_code)

    def session_ok(request: Request) -> bool:
        token = request.cookies.get("sid")
        if not token:
            return False
        with connect(db_path) as conn:
            row = conn.execute("SELECT requests FROM sessions WHERE token = ?", (token,)).fetchone()
            if not row:
                return False
            limit = app.state.faults.session_expiry_requests
            if limit and row["requests"] >= limit:
                # One expiry per fault setting, like a real timeout in the middle of a task.
                app.state.faults.session_expiry_requests = 0
                conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
                return False
            conn.execute("UPDATE sessions SET requests = requests + 1 WHERE token = ?", (token,))
        return True

    def to_login(request: Request) -> RedirectResponse:
        next_path = request.url.path if request.method == "GET" else "/devices"
        return RedirectResponse(f"/login?expired=1&next={next_path}", status_code=303)

    def load_device(device_id: int):
        with connect(db_path) as conn:
            return conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()

    def apply_password(device_id: int, new_password: str) -> HTMLResponse | None:
        """Final save step, where the save faults live. Returns an error response if a fault fires."""
        if app.state.faults.take("save_error"):
            return HTMLResponse("<h1>500 Internal Server Error</h1><p>Unexpected error. Try again later.</p>",
                                status_code=500)
        if app.state.faults.take("silent_failure"):
            return None
        with connect(db_path) as conn:
            conn.execute("UPDATE devices SET password = ?, password_changed_at = ? WHERE id = ?",
                         (new_password, app.state.clock.now().isoformat(), device_id))
        return None

    @app.middleware("http")
    async def slow_pages(request: Request, call_next):
        if app.state.faults.slow_ms and not request.url.path.startswith(("/_test", "/device/")):
            await asyncio.sleep(app.state.faults.slow_ms / 1000)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    def home():
        return RedirectResponse("/devices", status_code=303)

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, next: str = "/devices", expired: int = 0):
        return render(request, "login.html", next=next, expired=expired, error="")

    @app.post("/login")
    def login(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form("/devices")):
        with connect(db_path) as conn:
            row = conn.execute("SELECT password FROM admin WHERE username = ?", (username,)).fetchone()
            if not row or not secrets.compare_digest(row["password"], password):
                return render(request, "login.html", status_code=401, next=next, expired=0,
                              error="Invalid user name or password.")
            token = secrets.token_urlsafe(24)
            conn.execute("INSERT INTO sessions (token) VALUES (?)", (token,))
        response = RedirectResponse(next if next.startswith("/") else "/devices", status_code=303)
        response.set_cookie("sid", token, httponly=True)
        return response

    @app.get("/logout")
    def logout():
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie("sid")
        return response

    @app.get("/devices", response_class=HTMLResponse)
    def devices(request: Request, page: int = 1, q: str = ""):
        if not session_ok(request):
            return to_login(request)
        with connect(db_path) as conn:
            rows = conn.execute("SELECT * FROM devices WHERE name LIKE ? ORDER BY name",
                                (f"%{q}%",)).fetchall()
        pages = max(1, math.ceil(len(rows) / PAGE_SIZE))
        page = min(max(page, 1), pages)
        shown = rows[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
        return render(request, "devices.html", devices=shown, page=page, pages=pages, q=q, total=len(rows))

    @app.get("/devices/{device_id}", response_class=HTMLResponse)
    def device(request: Request, device_id: int, tab: str = "overview"):
        if not session_ok(request):
            return to_login(request)
        row = load_device(device_id)
        if not row:
            return HTMLResponse("<h1>404</h1><p>Device not found.</p>", status_code=404)
        if row["model"] == FL9:
            return render(request, "device_fl9.html", d=row, tab=tab, error="")
        return render(request, "device_ct200.html", d=row, error="")

    @app.post("/devices/{device_id}/password", response_class=HTMLResponse)
    def ct200_password(request: Request, device_id: int, current_password: str = Form(""),
                       new_password: str = Form(""), confirm_password: str = Form("")):
        if not session_ok(request):
            return to_login(request)
        row = load_device(device_id)
        if not row or row["model"] != CT200:
            return HTMLResponse("<h1>400</h1><p>Unsupported device.</p>", status_code=400)
        error = ""
        if not secrets.compare_digest(current_password, row["password"]):
            error = "Current password is incorrect."
        elif new_password != confirm_password:
            error = "New password and confirmation do not match."
        elif not password_valid(CT200, new_password):
            error = "Password must be exactly 16 characters: letters, digits and !@#% only."
        if error:
            return render(request, "device_ct200.html", status_code=400, d=row, error=error)
        token = secrets.token_urlsafe(16)
        with connect(db_path) as conn:
            conn.execute("INSERT INTO pending_changes VALUES (?, ?, ?)", (token, device_id, new_password))
        return render(request, "confirm.html", d=row, token=token)

    @app.post("/devices/{device_id}/password/confirm", response_class=HTMLResponse)
    def ct200_confirm(request: Request, device_id: int, token: str = Form(...)):
        if not session_ok(request):
            return to_login(request)
        with connect(db_path) as conn:
            change = conn.execute("SELECT * FROM pending_changes WHERE token = ? AND device_id = ?",
                                  (token, device_id)).fetchone()
            conn.execute("DELETE FROM pending_changes WHERE token = ?", (token,))
        if not change:
            return HTMLResponse("<h1>400</h1><p>This confirmation has expired. Start again.</p>", status_code=400)
        if failure := apply_password(device_id, change["new_password"]):
            return failure
        return render(request, "message.html", d=load_device(device_id), message="Saved! The admin password was changed.")

    @app.post("/devices/{device_id}/pin", response_class=HTMLResponse)
    def fl9_pin(request: Request, device_id: int, service_pin: str = Form(""), repeat_pin: str = Form("")):
        if not session_ok(request):
            return to_login(request)
        row = load_device(device_id)
        if not row or row["model"] != FL9:
            return HTMLResponse("<h1>400</h1><p>Unsupported device.</p>", status_code=400)
        error = ""
        if service_pin != repeat_pin:
            error = "PIN entries differ."
        elif not password_valid(FL9, service_pin):
            error = "Service PIN must be exactly 8 digits."
        if error:
            return render(request, "device_fl9.html", status_code=400, d=row, tab="security", error=error)
        if failure := apply_password(device_id, service_pin):
            return failure
        return render(request, "message.html", d=load_device(device_id), message="Saved! Service PIN applied.")

    @app.post("/device/{serial}/login")
    def device_login(serial: str, body: DeviceLogin):
        """Stands in for logging in to the physical device itself, not part of the dashboard UI."""
        with connect(db_path) as conn:
            row = conn.execute("SELECT password FROM devices WHERE serial = ?", (serial,)).fetchone()
        if not row:
            return JSONResponse({"ok": False, "error": "unknown device"}, status_code=404)
        if not secrets.compare_digest(row["password"], body.password):
            return JSONResponse({"ok": False}, status_code=401)
        return {"ok": True}

    if test_routes:
        @app.get("/_test/faults")
        def get_faults():
            return app.state.faults

        @app.post("/_test/faults")
        def set_faults(faults: Faults):
            app.state.faults = faults
            return faults

        @app.post("/_test/clock")
        def advance_clock(change: ClockChange):
            clock = app.state.clock
            if isinstance(clock, FakeClock):
                clock.advance(change.advance_days)
            else:
                clock.offset_days += change.advance_days
            return {"now": clock.now().isoformat()}

        @app.post("/_test/notify")
        def notify():
            return {"sent": app.state.notifier.run_once()}

        @app.get("/_test/device/{serial}")
        def device_truth(serial: str):
            with connect(db_path) as conn:
                row = conn.execute("SELECT password, password_changed_at FROM devices WHERE serial = ?",
                                   (serial,)).fetchone()
            return {"password": row["password"], "changed_at": row["password_changed_at"]}

    return app
