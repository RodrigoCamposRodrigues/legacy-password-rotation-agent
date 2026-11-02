import sqlite3
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.sqlite import SqliteSaver

from rotator.config import Settings
from rotator.changers import ScriptedChanger
from rotator.device import DeviceProbe
from rotator.protocol import CrashInjector, Deps
from rotator.service import create_service


@pytest.fixture
def service(dashboard, worker, tmp_path):
    def factory(**settings):
        deps = Deps(vault=dashboard.vault, probe=DeviceProbe(dashboard.url), changer=ScriptedChanger(), worker=worker,
                    settings=Settings(dashboard_url=dashboard.url, data_dir=tmp_path / "data", **settings),
                    clock=dashboard.clock, crash=CrashInjector())
        checkpointer = SqliteSaver(sqlite3.connect(tmp_path / "checkpoints.db", check_same_thread=False))
        app = create_service(deps, tmp_path / "service.db", checkpointer)
        return TestClient(app), app
    return factory


def event(serial="CT200-0012", event_id="evt-1"):
    return {"event_id": event_id, "device_serial": serial, "due_at": "2026-11-01T00:00:00+00:00"}


def wait(app):
    app.state.worker.wait_idle()


def test_webhook_rotates_and_duplicates_are_ignored(dashboard, service):
    client, app = service()
    first = client.post("/webhooks/rotation-due", json=event())
    second = client.post("/webhooks/rotation-due", json=event())
    assert first.json()["status"] == "accepted"
    assert second.json() == {"status": "duplicate", "run_id": first.json()["run_id"]}
    wait(app)
    runs = app.state.store.all()
    assert len(runs) == 1 and runs[0]["outcome"] == "succeeded"
    assert dashboard.truth("CT200-0012") == dashboard.vault.get_device("CT200-0012").active


def test_unknown_device_rejected(service):
    client, _ = service()
    assert client.post("/webhooks/rotation-due", json=event(serial="NOPE")).status_code == 404


def test_sweep_catches_lost_webhook_after_six_months(dashboard, service):
    client, app = service()
    started = {s["serial"] for s in client.post("/sweep").json()["started"]}
    overdue = {s for s in dashboard.vault.list_devices()
               if datetime.fromisoformat(dashboard.vault.get_device(s).active_since) + timedelta(days=180) <= dashboard.clock.now()}
    assert started == overdue and "CT200-0001" not in started
    wait(app)
    assert all(r["outcome"] == "succeeded" for r in app.state.store.all())

    dashboard.clock.advance(181)
    assert "CT200-0001" in {s["serial"] for s in client.post("/sweep").json()["started"]}
    wait(app)


def test_sweep_recovers_stale_pending(dashboard, service):
    record = dashboard.vault.get_device("CT200-0003")
    record.pending, record.pending_run_id, record.state = "NeverUsed123456!", "ghost", "pending"
    dashboard.vault.put_device(record)
    dashboard.vault.acquire_lock("CT200-0003", "ghost")
    client, app = service()
    started = client.post("/sweep").json()["started"]
    assert {"serial": "CT200-0003", "reason": "recover_pending"} in [{k: s[k] for k in ("serial", "reason")} for s in started]
    wait(app)
    record = dashboard.vault.get_device("CT200-0003")
    assert record.state == "active" and dashboard.truth("CT200-0003") == record.active


def test_paused_run_resumes_from_runs_page(dashboard, service):
    client, app = service(supervised=True)
    run_id = client.post("/webhooks/rotation-due", json=event()).json()["run_id"]
    wait(app)
    page = client.get(f"/runs/{run_id}").text
    assert "Approval needed" in page and 'value="approve"' in page
    assert client.post(f"/runs/{run_id}/resume", data={"decision": "bogus"}).status_code == 400
    client.post(f"/runs/{run_id}/resume", data={"decision": "approve"})
    wait(app)
    assert app.state.store.get(run_id)["outcome"] == "succeeded"
    assert "Steps" in client.get(f"/runs/{run_id}").text and "/shots/" in client.get(f"/runs/{run_id}").text
