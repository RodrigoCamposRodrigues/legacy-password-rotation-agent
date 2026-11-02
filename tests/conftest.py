import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
import uvicorn

from dashboard.app import create_app
from dashboard.db import connect
from dashboard.faults import Faults
from demo_seed import seed_all
from rotator.audit import AuditLog
from rotator.browser import Budget, BrowserSession, BrowserWorker, _browser_env
from rotator.vault import InMemoryVault
from simclock import FakeClock

ADMIN = ("admin", "admin-secret-pw")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class LiveDashboard:
    url: str
    app: object
    db_path: Path
    clock: FakeClock
    vault: InMemoryVault = None
    passwords: dict = field(default_factory=dict)

    def reset(self):
        self.app.state.faults = Faults()
        self.vault = InMemoryVault(admin=ADMIN)
        self.passwords = seed_all(self.db_path, self.vault, self.clock, ADMIN)

    def faults(self, **values):
        self.app.state.faults = Faults(**values)

    def truth(self, serial: str) -> str:
        return httpx.get(f"{self.url}/_test/device/{serial}").json()["password"]

    def id_of(self, serial: str) -> int:
        with connect(self.db_path) as conn:
            return conn.execute("SELECT id FROM devices WHERE serial = ?", (serial,)).fetchone()["id"]


@pytest.fixture(scope="session")
def _dashboard_server(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("dash") / "dashboard.db"
    clock = FakeClock()
    app = create_app(db_path, clock)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    yield LiveDashboard(url=f"http://127.0.0.1:{port}", app=app, db_path=db_path, clock=clock)
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def dashboard(_dashboard_server):
    _dashboard_server.reset()
    return _dashboard_server


@pytest.fixture(scope="session")
def browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(env=_browser_env())
        yield b
        b.close()


@pytest.fixture
def page(browser):
    context = browser.new_context()
    yield context.new_page()
    context.close()


@pytest.fixture(scope="session")
def worker():
    w = BrowserWorker()
    yield w
    w.close()


@pytest.fixture
def worker_page(worker):
    page = worker.new_page()
    yield page
    worker.close_page(page)


@pytest.fixture
def make_session(dashboard, page, worker, worker_page, tmp_path):
    """threaded=True puts the page on the browser worker, as the protocol and the LLM agent need."""
    def factory(serial: str, new_password: str | None = None, threaded: bool = False, **budget):
        record = dashboard.vault.get_device(serial)
        new = new_password or dashboard.vault.generate_password(record.model)
        secrets = {"{{NEW_PASSWORD}}": new, "{{CURRENT_PASSWORD}}": record.active}
        session = BrowserSession(
            worker_page if threaded else page, dashboard.url, serial, lambda: secrets, dashboard.vault.admin_credential,
            Budget(budget.get("max_tool_calls", 40), 60_000, budget.get("max_seconds", 60)),
            AuditLog(tmp_path / "audit"), worker=worker if threaded else None,
        )
        session.login()
        session.new_password = new
        return session
    return factory
