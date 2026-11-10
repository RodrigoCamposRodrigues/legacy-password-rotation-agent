import pytest

from rotator.browser import CURRENT, NEW, AttemptAborted

ADMIN_PASSWORD = "admin-secret-pw"


def el(session, label, tag=None):
    for e in session.snapshot():
        if e.label == label and (tag is None or e.tag == tag):
            return e.id
    raise AssertionError(f"no element {label!r}")


def open_device(session, dashboard, serial, tab=""):
    session.page.goto(f"{dashboard.url}/devices/{dashboard.id_of(serial)}{tab}")
    session.snapshot()


def fill_ct200(session, new=NEW, confirm=NEW, current=CURRENT):
    session.fill(el(session, "Current password"), current)
    session.fill(el(session, "New password"), new)
    session.fill(el(session, "Confirm new password"), confirm)


def test_read_page_never_shows_secret_values(dashboard, make_session):
    session = make_session("CT200-0012")
    open_device(session, dashboard, "CT200-0012")
    fill_ct200(session)
    text = session.read_page()
    assert '"New password" (filled)' in text
    assert session.new_password not in text and dashboard.passwords["CT200-0012"] not in text


def test_raw_value_in_secret_field_refused(dashboard, make_session):
    session = make_session("CT200-0012")
    open_device(session, dashboard, "CT200-0012")
    assert session.fill(el(session, "New password"), "1234").startswith("Refused")
    assert session.page.locator("#new_password").input_value() == ""


def test_placeholder_in_search_box_refused(dashboard, make_session):
    session = make_session("CT200-0012")
    session.page.goto(f"{dashboard.url}/devices")
    session.snapshot()
    assert session.fill(el(session, "Find device by name"), NEW).startswith("Refused")


def test_click_on_post_submit_refused(dashboard, make_session):
    session = make_session("CT200-0012")
    open_device(session, dashboard, "CT200-0012")
    assert "request_submit" in session.click(el(session, "Save"))


def test_gate_blocks_wrong_device(dashboard, make_session):
    session = make_session("CT200-0012")
    open_device(session, dashboard, "CT200-0112")
    assert "wrong device" in session.request_submit(el(session, "Save"))
    assert dashboard.truth("CT200-0112") == dashboard.passwords["CT200-0112"]


def test_gate_blocks_mismatched_confirmation(dashboard, make_session):
    session = make_session("CT200-0012")
    open_device(session, dashboard, "CT200-0012")
    fill_ct200(session, confirm=CURRENT)
    assert "does not hold {{NEW_PASSWORD}}" in session.request_submit(el(session, "Save"))


def test_gate_blocks_confirmation_without_password_form(dashboard, make_session):
    first = make_session("CT200-0012")
    open_device(first, dashboard, "CT200-0012")
    fill_ct200(first)
    first.request_submit(el(first, "Save"))
    confirm_url = first.page.url
    assert "Are you sure?" in first.page.inner_text("body")
    # Simulates a session that never passed a password form trying to press "Yes".
    first._password_form_passed = False
    first.snapshot()
    assert "no password form" in first.request_submit(el(first, "Yes, change password"))
    assert confirm_url == first.page.url


def test_ct200_full_change_through_gate(dashboard, make_session):
    session = make_session("CT200-0012")
    open_device(session, dashboard, "CT200-0012")
    fill_ct200(session)
    assert session.request_submit(el(session, "Save")).startswith("Submitted")
    assert session.request_submit(el(session, "Yes, change password")).startswith("Submitted")
    assert "Saved!" in session.page.inner_text("body")
    assert dashboard.truth("CT200-0012") == session.new_password


def test_fl9_change_accepts_confirm_dialog(dashboard, make_session):
    session = make_session("FL9-0012")
    open_device(session, dashboard, "FL9-0012", "?tab=security")
    session.fill(el(session, "Service PIN"), NEW)
    session.fill(el(session, "Repeat PIN"), NEW)
    assert session.request_submit(el(session, "Apply")).startswith("Submitted")
    assert dashboard.truth("FL9-0012") == session.new_password


def test_unconfirmed_dialog_is_dismissed_by_default(dashboard, make_session):
    session = make_session("FL9-0012")
    open_device(session, dashboard, "FL9-0012", "?tab=security")
    session.fill(el(session, "Service PIN"), NEW)
    session.fill(el(session, "Repeat PIN"), NEW)
    session.page.locator("button[type=submit]").click()
    assert dashboard.truth("FL9-0012") == dashboard.passwords["FL9-0012"]


def test_login_again_after_session_expiry(dashboard, make_session):
    session = make_session("CT200-0012")
    dashboard.faults(session_expiry_requests=1)
    session.page.goto(f"{dashboard.url}/devices")
    session.page.goto(f"{dashboard.url}/devices?page=2")
    assert "session has expired" in session.read_page()
    dashboard.faults()
    assert "Logged in again" in session.login_again()
    assert "Refrigeration devices" in session.read_page()
    assert ADMIN_PASSWORD not in session.read_page()



def test_budget_exhaustion_aborts(dashboard, make_session):
    session = make_session("CT200-0012", max_tool_calls=2)
    session.read_page()
    session.read_page()
    with pytest.raises(AttemptAborted, match="tool calls"):
        session.read_page()


def test_second_gate_rejection_aborts(dashboard, make_session):
    session = make_session("CT200-0012")
    open_device(session, dashboard, "CT200-0112")
    session.request_submit(el(session, "Save"))
    with pytest.raises(AttemptAborted, match="rejected 2 times"):
        session.request_submit(el(session, "Save"))


def test_failed_fill_never_echoes_the_secret(dashboard, make_session):
    session = make_session("CT200-0012")
    open_device(session, dashboard, "CT200-0012")
    field = el(session, "New password")
    session.page.evaluate("document.getElementById('new_password').disabled = true")
    session.page.set_default_timeout(500)
    result = session.fill(field, NEW)
    assert result.startswith("Fill failed") and session.new_password not in result
    assert session.new_password not in (session.audit.path.read_text())


def test_gate_rejects_page_showing_another_serial(dashboard, make_session):
    from dashboard.db import connect
    with connect(dashboard.db_path) as conn:
        conn.execute("UPDATE devices SET notes = 'Same rack as CT200-0112' WHERE serial = 'CT200-0012'")
    session = make_session("CT200-0012")
    open_device(session, dashboard, "CT200-0012")
    fill_ct200(session)
    assert "other device serials (CT200-0112)" in session.request_submit(el(session, "Save"))


def test_dialog_is_not_accepted_after_an_ungated_moment(dashboard, make_session):
    session = make_session("FL9-0012")
    session.page.goto(f"{dashboard.url}/devices")
    session.snapshot()
    session.request_submit(el(session, "Search"))
    open_device(session, dashboard, "FL9-0012", "?tab=security")
    session.fill(el(session, "Service PIN"), NEW)
    session.fill(el(session, "Repeat PIN"), NEW)
    session.page.locator("button[type=submit]").click()
    assert dashboard.truth("FL9-0012") == dashboard.passwords["FL9-0012"]


def add_unmanaged(dashboard, serial, name, model, location):
    """A device on the dashboard that the vault does not manage, as the harness plants it."""
    from datetime import datetime
    from dashboard.db import connect
    with connect(dashboard.db_path) as conn:
        conn.execute(
            "INSERT INTO devices (serial, name, model, location, password, password_changed_at, notes)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (serial, name, model, location, "Unmanaged-pw-1", datetime.now().isoformat(), ""),
        )


def test_gate_rejects_unmanaged_device_with_target_serial_in_location(dashboard, make_session):
    add_unmanaged(dashboard, "FL9-0901", "Freezer 01 controller", "FrostLine FL-9", "Frozen aisle, label S/N FL9-0001")
    session = make_session("FL9-0001")
    open_device(session, dashboard, "FL9-0901", "?tab=security")
    session.fill(el(session, "Service PIN"), NEW)
    session.fill(el(session, "Repeat PIN"), NEW)
    assert "wrong device" in session.request_submit(el(session, "Apply"))
    assert dashboard.truth("FL9-0901") == "Unmanaged-pw-1"


def test_gate_rejects_unmanaged_device_with_target_serial_in_name(dashboard, make_session):
    add_unmanaged(dashboard, "FL9-0922", "Freezer 12 service panel FL9-0012", "FrostLine FL-9", "Ice cream")
    session = make_session("FL9-0012")
    open_device(session, dashboard, "FL9-0922", "?tab=security")
    session.fill(el(session, "Service PIN"), NEW)
    session.fill(el(session, "Repeat PIN"), NEW)
    assert "wrong device" in session.request_submit(el(session, "Apply"))
    assert dashboard.truth("FL9-0922") == "Unmanaged-pw-1"


def test_gate_rejects_ct200_whose_name_claims_the_target_serial(dashboard, make_session):
    add_unmanaged(dashboard, "CT200-0901", "Serial number: CT200-0012", "CoolTech CT-200", "Dairy")
    session = make_session("CT200-0012")
    open_device(session, dashboard, "CT200-0901")
    fill_ct200(session)
    assert "wrong device" in session.request_submit(el(session, "Save"))
    assert dashboard.truth("CT200-0901") == "Unmanaged-pw-1"


def test_gate_rejects_page_without_a_serial_field(dashboard, make_session):
    session = make_session("CT200-0012")
    session.page.goto(f"{dashboard.url}/devices")
    session.snapshot()
    assert "can't read the device serial" in session._wrong_device()


@pytest.mark.parametrize("serial, tab", [("CT200-0012", ""), ("FL9-0012", "?tab=security")])
def test_page_serial_is_read_on_every_device_page(dashboard, make_session, serial, tab):
    """Fails if a template moves the serial, so a redesign breaks a test and not a rotation."""
    session = make_session(serial)
    open_device(session, dashboard, serial, tab)
    assert session._page_serial() == serial


def test_page_serial_is_read_on_the_confirmation_page(dashboard, make_session):
    from dashboard.db import connect
    with connect(dashboard.db_path) as conn:
        conn.execute("UPDATE devices SET name = 'Fridge (serial CT200-0999).' WHERE serial = 'CT200-0012'")
    session = make_session("CT200-0012")
    open_device(session, dashboard, "CT200-0012")
    fill_ct200(session)
    assert session.request_submit(el(session, "Save")).startswith("Submitted")
    assert session._page_serial() == "CT200-0012"
