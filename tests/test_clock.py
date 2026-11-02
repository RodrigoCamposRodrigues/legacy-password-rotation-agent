from datetime import datetime, timedelta, timezone

from simclock import FakeClock, SystemClock


def test_fake_clock_advances_by_days():
    clock = FakeClock()
    start = clock.now()
    clock.advance(180)
    assert clock.now() - start == timedelta(days=180)


def test_system_clock_offset():
    ahead = SystemClock(5).now() - datetime.now(timezone.utc)
    assert timedelta(days=4.99) < ahead < timedelta(days=5.01)
