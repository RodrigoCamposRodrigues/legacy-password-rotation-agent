import re

import httpx
import pytest
from fastapi.testclient import TestClient

from dashboard.app import create_app
from dashboard.db import DEFAULT_DEVICES, connect, seed
from dashboard.notifier import Notifier
from simclock import FakeClock

ADMIN = ("admin", "admin-pass-123")
PASSWORDS = {serial: ("12345678" if serial.startswith("FL9") else "Old0ld0ld0ld0ld!") for _, _, serial, *_ in DEFAULT_DEVICES}


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def db_path(tmp_path, clock):
    path = tmp_path / "dashboard.db"
    seed(path, PASSWORDS, ADMIN, clock.now())
    return path


@pytest.fixture
def client(db_path, clock):
    app = create_app(db_path, clock)
    with TestClient(app) as c:
        c.post("/login", data={"username": ADMIN[0], "password": ADMIN[1]})
        yield c


def device_id(db_path, serial):
    with connect(db_path) as conn:
        return conn.execute("SELECT id FROM devices WHERE serial = ?", (serial,)).fetchone()["id"]


def device_password(client, serial):
    return client.get(f"/_test/device/{serial}").json()["password"]


def change_ct200(client, db_path, serial, current, new):
    did = device_id(db_path, serial)
    page = client.post(f"/devices/{did}/password",
                       data={"current_password": current, "new_password": new, "confirm_password": new})
    token = re.search(r'name="token" value="([^"]+)"', page.text)
    return page, (client.post(f"/devices/{did}/password/confirm", data={"token": token.group(1)}) if token else None)


def test_login_required(db_path, clock):
    with TestClient(create_app(db_path, clock)) as c:
        response = c.get("/devices", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/login")


def test_device_list_pages(client):
    assert "Page 1 of 3" in client.get("/devices").text
    page2 = client.get("/devices?page=2").text
    assert "Fridge 12</a>" in page2 and "Fridge 12B</a>" in page2


def test_ct200_change_needs_current_password_and_confirmation(client, db_path):
    page, _ = change_ct200(client, db_path, "CT200-0012", "wrong", "NewPassw0rd!!!!!")
    assert "Current password is incorrect" in page.text
    page, done = change_ct200(client, db_path, "CT200-0012", PASSWORDS["CT200-0012"], "NewPassw0rd!!!!!")
    assert "Are you sure?" in page.text and "Saved!" in done.text
    assert device_password(client, "CT200-0012") == "NewPassw0rd!!!!!"


def test_fl9_rejects_bad_pin(client, db_path):
    did = device_id(db_path, "FL9-0012")
    assert "exactly 8 digits" in client.post(f"/devices/{did}/pin", data={"service_pin": "12ab", "repeat_pin": "12ab"}).text
    assert "Saved!" in client.post(f"/devices/{did}/pin", data={"service_pin": "87654321", "repeat_pin": "87654321"}).text
    assert device_password(client, "FL9-0012") == "87654321"


def test_save_error_once_then_works(client, db_path):
    client.post("/_test/faults", json={"save_error": "once"})
    _, done = change_ct200(client, db_path, "CT200-0001", PASSWORDS["CT200-0001"], "NewPassw0rd!!!!!")
    assert done.status_code == 500
    _, done = change_ct200(client, db_path, "CT200-0001", PASSWORDS["CT200-0001"], "NewPassw0rd!!!!!")
    assert "Saved!" in done.text


def test_silent_failure_says_saved_but_keeps_password(client, db_path):
    client.post("/_test/faults", json={"silent_failure": "once"})
    _, done = change_ct200(client, db_path, "CT200-0001", PASSWORDS["CT200-0001"], "NewPassw0rd!!!!!")
    assert "Saved!" in done.text
    assert device_password(client, "CT200-0001") == PASSWORDS["CT200-0001"]


def test_session_expiry(client):
    client.post("/_test/faults", json={"session_expiry_requests": 2})
    client.get("/devices")
    client.get("/devices")
    assert "session has expired" in client.get("/devices").text


def test_device_login(client):
    assert client.post("/device/CT200-0012/login", json={"password": PASSWORDS["CT200-0012"]}).status_code == 200
    assert client.post("/device/CT200-0012/login", json={"password": "nope"}).status_code == 401


def test_notifier_sends_each_due_device_once_with_stable_ids(db_path, clock):
    sent, fail = [], {"on": True}

    def handler(request):
        if fail["on"]:
            return httpx.Response(503)
        sent.append(request.content)
        return httpx.Response(202)

    notifier = Notifier(db_path, clock, "http://rotator/webhook", http=httpx.Client(transport=httpx.MockTransport(handler)))
    due = {e["device_serial"] for e in notifier.due_events()}
    assert due == {serial for _, _, serial, _, age, _ in DEFAULT_DEVICES if age >= 180}
    assert notifier.run_once() == []
    fail["on"] = False
    first = notifier.run_once()
    assert {e["device_serial"] for e in first} == due
    assert notifier.run_once() == []
    assert [e["event_id"] for e in notifier.due_events()] == [e["event_id"] for e in first]
