import os
import sqlite3
import threading
from urllib.parse import urlparse

import uvicorn
from langgraph.checkpoint.sqlite import SqliteSaver

from rotator.browser import BrowserWorker
from rotator.changers import LLMChanger, ScriptedChanger
from rotator.config import Settings
from rotator.device import DeviceProbe
from rotator.protocol import Deps
from rotator.service import create_service
from rotator.vault import OpenBaoVault
from simclock import SystemClock

if __name__ == "__main__":
    settings = Settings.from_env()
    if not settings.bao_token:
        raise SystemExit("No OpenBao token. Start OpenBao and run scripts/bao_init.py first.")
    # ROTATOR_CHANGER=scripted runs the whole system without an LLM, e.g. before an API key is set.
    changer = ScriptedChanger() if os.environ.get("ROTATOR_CHANGER") == "scripted" else LLMChanger(settings.model, reasoning_effort=settings.reasoning_effort)
    deps = Deps(
        vault=OpenBaoVault(settings.bao_addr, settings.bao_token),
        probe=DeviceProbe(settings.dashboard_url),
        changer=changer,
        # ROTATOR_HEADED=true shows the browser window, slowed down so you can follow each action.
        worker=BrowserWorker(headed=os.environ.get("ROTATOR_HEADED") == "true",
                             slow_mo_ms=float(os.environ.get("ROTATOR_SLOW_MO_MS", "400"))),
        settings=settings,
        clock=SystemClock(settings.clock_offset_days),
    )
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    checkpointer = SqliteSaver(sqlite3.connect(settings.data_dir / "checkpoints.db", check_same_thread=False))
    app = create_service(deps, settings.data_dir / "service.db", checkpointer)
    interval = float(os.environ.get("SWEEP_INTERVAL_SECONDS", "86400"))

    def sweep_loop():
        while True:
            threading.Event().wait(interval)
            app.state.sweep()

    threading.Thread(target=sweep_loop, daemon=True, name="sweep").start()
    url = urlparse(os.environ.get("ROTATOR_URL", "http://127.0.0.1:8100"))
    uvicorn.run(app, host=url.hostname, port=url.port or 8100)
