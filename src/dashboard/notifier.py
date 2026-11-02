import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from dashboard.db import connect
from simclock import Clock


class Notifier:
    """The legacy system's own "password expires soon" check, sent as a webhook."""

    def __init__(self, db_path: Path, clock: Clock, webhook_url: str | None, rotation_days: int = 180,
                 http: httpx.Client | None = None):
        self.db_path = db_path
        self.clock = clock
        self.webhook_url = webhook_url
        self.rotation_days = rotation_days
        self.http = http or httpx.Client(timeout=5)

    def due_events(self) -> list[dict]:
        now = self.clock.now()
        events = []
        with connect(self.db_path) as conn:
            for row in conn.execute("SELECT serial, password_changed_at FROM devices ORDER BY serial"):
                due_at = datetime.fromisoformat(row["password_changed_at"]) + timedelta(days=self.rotation_days)
                if due_at > now:
                    continue
                # Same device and due date give the same id, so a retried webhook is recognisable.
                event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{row['serial']}:{due_at.isoformat()}"))
                events.append({"event_id": event_id, "device_serial": row["serial"], "due_at": due_at.isoformat()})
        return events

    def run_once(self) -> list[dict]:
        sent = []
        if not self.webhook_url:
            return sent
        with connect(self.db_path) as conn:
            for event in self.due_events():
                row = conn.execute("SELECT sent_at FROM notifications WHERE event_id = ?", (event["event_id"],)).fetchone()
                if row and row["sent_at"]:
                    continue
                conn.execute(
                    "INSERT INTO notifications (event_id, serial, due_at) VALUES (?, ?, ?)"
                    " ON CONFLICT(event_id) DO UPDATE SET attempts = attempts + 1",
                    (event["event_id"], event["device_serial"], event["due_at"]),
                )
                try:
                    self.http.post(self.webhook_url, json=event).raise_for_status()
                except httpx.HTTPError:
                    continue
                conn.execute("UPDATE notifications SET sent_at = ? WHERE event_id = ?",
                             (self.clock.now().isoformat(), event["event_id"]))
                sent.append(event)
        return sent

    def start_loop(self, interval_seconds: float) -> threading.Thread:
        def loop():
            while True:
                self.run_once()
                threading.Event().wait(interval_seconds)

        thread = threading.Thread(target=loop, daemon=True, name="notifier")
        thread.start()
        return thread
