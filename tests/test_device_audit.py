import httpx
import pytest

from rotator.audit import AuditLog
from rotator.device import DeviceProbe, DeviceUnreachable


def probe_with(handler):
    return DeviceProbe("http://dashboard", http=httpx.Client(base_url="http://dashboard", transport=httpx.MockTransport(handler)))


def test_probe_maps_status_codes():
    assert probe_with(lambda r: httpx.Response(200, json={"ok": True})).check("X", "p") is True
    assert probe_with(lambda r: httpx.Response(401)).check("X", "p") is False
    with pytest.raises(DeviceUnreachable):
        probe_with(lambda r: httpx.Response(503)).check("X", "p")


def test_probe_connection_error_is_unreachable():
    with pytest.raises(DeviceUnreachable):
        DeviceProbe("http://127.0.0.1:9").check("X", "p")


def test_audit_round_trip(tmp_path):
    log = AuditLog(tmp_path / "run-1")
    log.event("precheck", ok=True)
    log.event("verify", new_works=True, old_works=False)
    assert [e["step"] for e in log.read()] == ["precheck", "verify"]
    assert log.read()[1]["new_works"] is True
