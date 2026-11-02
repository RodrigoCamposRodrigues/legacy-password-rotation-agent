"""An in-process dashboard with a fake clock and in-memory vault, for tests and evals."""

import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import uvicorn

from dashboard.app import create_app
from dashboard.db import connect
from dashboard.faults import Faults
from demo_seed import seed_all
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
    server: uvicorn.Server
    thread: threading.Thread
    vault: InMemoryVault = None
    passwords: dict = field(default_factory=dict)

    def reset(self) -> None:
        self.app.state.faults = Faults()
        self.vault = InMemoryVault(admin=ADMIN)
        self.passwords = seed_all(self.db_path, self.vault, self.clock, ADMIN)

    def faults(self, **values) -> None:
        self.app.state.faults = Faults(**values)

    def truth(self, serial: str) -> str:
        return httpx.get(f"{self.url}/_test/device/{serial}").json()["password"]

    def id_of(self, serial: str) -> int:
        with connect(self.db_path) as conn:
            return conn.execute("SELECT id FROM devices WHERE serial = ?", (serial,)).fetchone()["id"]

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)


def start_dashboard(db_path: Path) -> LiveDashboard:
    clock = FakeClock()
    app = create_app(db_path, clock)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    return LiveDashboard(url=f"http://127.0.0.1:{port}", app=app, db_path=db_path, clock=clock, server=server, thread=thread)
