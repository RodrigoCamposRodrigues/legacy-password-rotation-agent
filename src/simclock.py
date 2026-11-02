"""Clocks shared by the dashboard and the rotator, so tests can jump months ahead."""

from datetime import datetime, timedelta, timezone


class Clock:
    def now(self) -> datetime:
        raise NotImplementedError


class SystemClock(Clock):
    def __init__(self, offset_days: float = 0):
        self.offset_days = offset_days

    def now(self) -> datetime:
        return datetime.now(timezone.utc) + timedelta(days=self.offset_days)


class FakeClock(Clock):
    def __init__(self, start: datetime | None = None):
        self._now = start or datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance(self, days: float) -> None:
        self._now += timedelta(days=days)
