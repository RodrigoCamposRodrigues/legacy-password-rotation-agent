"""Defence switches for the security harness's ablation. Defaults must equal today's behaviour."""

from dataclasses import asdict
from urllib.parse import unquote

import pytest

from rotator.audit import AuditLog
from rotator.browser import CURRENT, NEW
from rotator.changers import ScriptedChanger, Target
from rotator.config import DEFENCE_NAMES, Defences, Settings
from rotator.device import DeviceProbe
from rotator.protocol import Deps

from test_browser import el, fill_ct200, open_device

RAW = "Abcd1234Efgh5678"


def test_defaults_keep_every_defence_on():
    assert Settings().defences == Defences()
    assert all(asdict(Defences()).values())
    assert DEFENCE_NAMES == ("placeholders", "submit_gate", "untrusted_marking", "budgets", "verify")


def test_with_disabled():
    d = Defences().with_disabled(["verify"])
    assert d.verify is False and d.placeholders and d.submit_gate and d.budgets and d.untrusted_marking
    assert not any(asdict(Defences().with_disabled(DEFENCE_NAMES)).values())
    with pytest.raises(ValueError, match="nope"):
        Defences().with_disabled(["nope"])


def test_placeholders_off_accepts_raw_value_in_secret_field(dashboard, make_session):
    session = make_session("CT200-0012", defences=Defences(placeholders=False))
    open_device(session, dashboard, "CT200-0012")
    assert not session.fill(el(session, "New password"), RAW).startswith("Refused")
    assert session.page.locator("#new_password").input_value() == RAW


def test_placeholders_off_still_substitutes_placeholders(dashboard, make_session):
    session = make_session("CT200-0012", defences=Defences(placeholders=False))
    open_device(session, dashboard, "CT200-0012")
    session.fill(el(session, "New password"), NEW)
    assert session.page.locator("#new_password").input_value() == session.new_password


def test_placeholders_off_allows_secret_in_search_box(dashboard, make_session):
    session = make_session("CT200-0012", defences=Defences(placeholders=False))
    session.page.goto(f"{dashboard.url}/devices")
    session.snapshot()
    assert not session.fill(el(session, "Find device by name"), session.new_password).startswith("Refused")
    assert session.request_submit(el(session, "Search")).startswith("Submitted")
    assert session.new_password in unquote(session.page.url)


def test_placeholders_off_never_logs_raw_secret(dashboard, make_session):
    session = make_session("CT200-0012", defences=Defences(placeholders=False))
    open_device(session, dashboard, "CT200-0012")
    session.fill(el(session, "New password"), RAW)
    audit = session.audit.path.read_text()
    assert RAW not in audit and '"raw": true' in audit


def test_submit_gate_off_saves_on_wrong_device(dashboard, make_session):
    # FL9 forms have no current-password field, so placeholders alone can't stop a wrong-device save.
    session = make_session("FL9-0012", defences=Defences(submit_gate=False))
    open_device(session, dashboard, "FL9-0007", "?tab=security")
    session.fill(el(session, "Service PIN"), NEW)
    session.fill(el(session, "Repeat PIN"), NEW)
    assert session.request_submit(el(session, "Apply")).startswith("Submitted")
    assert dashboard.truth("FL9-0007") == session.new_password


def test_submit_gate_on_blocks_same_wrong_device(dashboard, make_session):
    session = make_session("FL9-0012")
    open_device(session, dashboard, "FL9-0007", "?tab=security")
    session.fill(el(session, "Service PIN"), NEW)
    session.fill(el(session, "Repeat PIN"), NEW)
    assert "wrong device" in session.request_submit(el(session, "Apply"))
    assert dashboard.truth("FL9-0007") == dashboard.passwords["FL9-0007"]


def test_submit_gate_off_saves_on_wrong_device_with_raw_values(dashboard, make_session):
    session = make_session("CT200-0012", defences=Defences(submit_gate=False, placeholders=False))
    open_device(session, dashboard, "CT200-0112")
    fill_ct200(session, new=RAW, confirm=RAW, current=dashboard.passwords["CT200-0112"])
    assert session.request_submit(el(session, "Save")).startswith("Submitted")
    assert session.request_submit(el(session, "Yes, change password")).startswith("Submitted")
    assert dashboard.truth("CT200-0112") == RAW


def test_submit_gate_off_never_aborts_on_rejections(dashboard, make_session):
    # The two-rejection limit belongs to the gate; the placeholders GET-form rule may still reject.
    session = make_session("CT200-0012", defences=Defences(submit_gate=False))
    session.page.goto(f"{dashboard.url}/devices")
    session.snapshot()
    session.page.fill("input[name=q]", session.new_password)
    for _ in range(3):
        assert "GET form contains a secret" in session.request_submit(el(session, "Search"))


def test_submit_gate_off_click_presses_submit(dashboard, make_session):
    session = make_session("FL9-0012", defences=Defences(submit_gate=False))
    open_device(session, dashboard, "FL9-0012", "?tab=security")
    session.fill(el(session, "Service PIN"), NEW)
    session.fill(el(session, "Repeat PIN"), NEW)
    assert not session.click(el(session, "Apply")).startswith("Refused")
    assert dashboard.truth("FL9-0012") == session.new_password


def test_submit_gate_on_click_still_refuses_submit(dashboard, make_session):
    session = make_session("FL9-0012")
    open_device(session, dashboard, "FL9-0012", "?tab=security")
    assert "request_submit" in session.click(el(session, "Apply"))


def test_untrusted_marking_off_drops_marker_lines(dashboard, make_session):
    on = make_session("CT200-0012")
    open_device(on, dashboard, "CT200-0112")
    assert "untrusted data" in on.read_page()
    off = make_session("CT200-0012", defences=Defences(untrusted_marking=False))
    open_device(off, dashboard, "CT200-0112")
    text = off.read_page()
    assert "untrusted" not in text and "end of page text" not in text
    assert "SYSTEM NOTICE" in text


def test_secret_values_exposes_both_passwords(dashboard, make_session):
    session = make_session("CT200-0012")
    values = session.secret_values()
    assert values[NEW] == session.new_password
    assert values[CURRENT] == dashboard.passwords["CT200-0012"]


@pytest.mark.parametrize("budgets, expected", [(True, (40, 60_000, 180)), (False, (100, 150_000, 300))])
def test_budgets_off_uses_safety_cap(dashboard, worker, tmp_path, budgets, expected):
    settings = Settings(dashboard_url=dashboard.url, data_dir=tmp_path, defences=Defences(budgets=budgets))
    deps = Deps(vault=dashboard.vault, probe=DeviceProbe(dashboard.url), changer=ScriptedChanger(), worker=worker,
                settings=settings, clock=dashboard.clock)
    target = Target(serial="CT200-0012", name="Fridge 12", model="CoolTech CT-200", run_id="r1")
    with deps.session(target, AuditLog(tmp_path / "audit")) as session:
        budget = session.budget
        assert (budget.max_tool_calls, budget.max_tokens, budget.max_seconds) == expected
        assert session.defences == settings.defences
