import os
from pathlib import Path
from urllib.parse import urlparse

import uvicorn
from dotenv import load_dotenv

from dashboard.app import create_app
from simclock import SystemClock

REPO_ROOT = Path(__file__).resolve().parents[2]

if __name__ == "__main__":
    load_dotenv(REPO_ROOT / ".env")
    url = urlparse(os.environ.get("DASHBOARD_URL", "http://127.0.0.1:8000"))
    rotator = os.environ.get("ROTATOR_URL", "http://127.0.0.1:8100")
    app = create_app(
        Path(os.environ.get("DASHBOARD_DB", REPO_ROOT / "data" / "dashboard.db")),
        SystemClock(float(os.environ.get("CLOCK_OFFSET_DAYS", "0"))),
        webhook_url=f"{rotator}/webhooks/rotation-due",
    )
    app.state.notifier.start_loop(float(os.environ.get("NOTIFIER_INTERVAL_SECONDS", "86400")))
    uvicorn.run(app, host=url.hostname, port=url.port or 8000)
