import sqlite3
from dataclasses import replace

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from rotator.changers import ScriptedChanger
from rotator.config import Settings
from rotator.device import DeviceProbe
from rotator.protocol import CRASH_POINTS, CrashInjector, Deps, SimulatedCrash, build_graph, resume_run, start_run


@pytest.fixture
def make_deps(dashboard, worker, tmp_path):
    def factory(**overrides):
        settings = Settings(dashboard_url=dashboard.url, data_dir=tmp_path / "data", max_seconds=60)
        settings = replace(settings, **overrides.pop("settings", {}))
        values = dict(vault=dashboard.vault, probe=DeviceProbe(dashboard.url), changer=ScriptedChanger(),
                      worker=worker, settings=settings, clock=dashboard.clock, crash=CrashInjector())
        values.update(overrides)
        return Deps(**values)
    return factory


@pytest.fixture
def checkpoint_path(tmp_path):
    return tmp_path / "checkpoints.db"


def graph_for(deps, path):
    return build_graph(deps, SqliteSaver(sqlite3.connect(path, check_same_thread=False)))


def assert_invariant(dashboard, serial):
    record = dashboard.vault.get_device(serial)
    assert dashboard.truth(serial) in {record.active, record.pending} - {""}, "no stored password works"


def assert_rotated(dashboard, serial):
    record = dashboard.vault.get_device(serial)
    assert record.state == "active" and record.pending == ""
    assert dashboard.truth(serial) == record.active != dashboard.passwords[serial]
    assert record.previous and dashboard.vault.lock_holder(serial) is None


@pytest.mark.parametrize("serial", ["CT200-0012", "FL9-0012"])
def test_happy_path(dashboard, make_deps, checkpoint_path, serial):
    result = start_run(graph_for(make_deps(), checkpoint_path), serial)
    assert result["outcome"] == "succeeded" and result["status"] == "done"
    assert_rotated(dashboard, serial)
    assert dashboard.vault.get_device(serial).previous == dashboard.passwords[serial]


@pytest.mark.parametrize("point", CRASH_POINTS)
def test_crash_then_resume_same_run(dashboard, make_deps, checkpoint_path, point):
    deps = make_deps()
    deps.crash.arm(point)
    with pytest.raises(SimulatedCrash):
        start_run(graph_for(deps, checkpoint_path), "CT200-0012", run_id="r1")
    assert_invariant(dashboard, "CT200-0012")
    pending_at_crash = dashboard.vault.get_device("CT200-0012").pending

    # A fresh process: new graph object, same checkpoint file.
    result = resume_run(graph_for(make_deps(), checkpoint_path), "r1")
    assert result["outcome"] == "succeeded"
    assert_rotated(dashboard, "CT200-0012")
    if pending_at_crash:
        assert dashboard.vault.get_device("CT200-0012").active == pending_at_crash, "escrow must not regenerate"


@pytest.mark.parametrize("point", CRASH_POINTS)
def test_crash_then_new_run_with_checkpoint_lost(dashboard, make_deps, tmp_path, point):
    deps = make_deps()
    deps.crash.arm(point)
    with pytest.raises(SimulatedCrash):
        start_run(graph_for(deps, tmp_path / "lost.db"), "CT200-0012", run_id="dead")
    assert_invariant(dashboard, "CT200-0012")

    deps = make_deps(is_run_alive=lambda run_id: run_id != "dead")
    result = start_run(graph_for(deps, tmp_path / "new.db"), "CT200-0012", run_id="fresh")
    assert result["outcome"] == "succeeded"
    record = dashboard.vault.get_device("CT200-0012")
    assert dashboard.truth("CT200-0012") == record.active and record.state == "active"
    assert dashboard.vault.lock_holder("CT200-0012") is None


def test_mid_change_crash_does_not_change_twice(dashboard, make_deps, checkpoint_path):
    deps = make_deps()
    deps.crash.arm("mid_change")
    with pytest.raises(SimulatedCrash):
        start_run(graph_for(deps, checkpoint_path), "FL9-0012", run_id="r1")
    resume_run(graph_for(make_deps(), checkpoint_path), "r1")
    steps = [e["step"] for e in deps.audit("r1").read()]
    assert "change_skipped_already_applied" in steps and steps.count("change") == 1


def test_vault_down_at_escrow_leaves_device_untouched(dashboard, make_deps, checkpoint_path):
    dashboard.vault.fail_writes = 1
    result = start_run(graph_for(make_deps(), checkpoint_path), "CT200-0012")
    assert result["status"] == "escalated" and "vault_unavailable" in result["reason"]
    assert dashboard.truth("CT200-0012") == dashboard.passwords["CT200-0012"]


def test_wrong_stored_password_escalates_at_precheck(dashboard, make_deps, checkpoint_path):
    record = dashboard.vault.get_device("CT200-0012")
    record.active = "WrongPassword123"
    dashboard.vault.put_device(record)
    result = start_run(graph_for(make_deps(), checkpoint_path), "CT200-0012")
    assert result["reason"].startswith("precheck_failed")
    assert result["waiting_for"]["kind"] == "escalation"


def test_unreachable_device_is_not_a_wrong_password(dashboard, make_deps, checkpoint_path):
    deps = make_deps(probe=DeviceProbe("http://127.0.0.1:9"))
    result = start_run(graph_for(deps, checkpoint_path), "CT200-0012")
    assert result["reason"] == "device_unreachable"
    assert dashboard.vault.get_device("CT200-0012").state == "active"


def test_silent_failure_is_caught_by_verify_and_retried(dashboard, make_deps, checkpoint_path):
    dashboard.faults(silent_failure="once")
    result = start_run(graph_for(make_deps(), checkpoint_path), "CT200-0012")
    assert result["outcome"] == "succeeded" and result["change_attempts"] == 2
    assert_rotated(dashboard, "CT200-0012")


def test_save_always_failing_escalates_then_abort(dashboard, make_deps, checkpoint_path):
    dashboard.faults(save_error="always")
    graph = graph_for(make_deps(), checkpoint_path)
    result = start_run(graph, "CT200-0012", run_id="r1")
    assert result["status"] == "escalated" and result["reason"].startswith("change_not_applied after 3 attempts")
    assert_invariant(dashboard, "CT200-0012")
    result = resume_run(graph, "r1", "abort")
    assert result["outcome"] == "aborted" and dashboard.vault.lock_holder("CT200-0012") is None
    # The unused pending stays in the vault; a later run recovers from it.
    assert dashboard.vault.get_device("CT200-0012").state == "pending"


def test_retry_after_human_fixes_the_cause(dashboard, make_deps, checkpoint_path):
    dashboard.faults(save_error="always")
    graph = graph_for(make_deps(), checkpoint_path)
    start_run(graph, "CT200-0012", run_id="r1")
    dashboard.faults()
    result = resume_run(graph, "r1", "retry")
    assert result["outcome"] == "succeeded"
    assert_rotated(dashboard, "CT200-0012")


def test_resolved_by_human(dashboard, make_deps, checkpoint_path):
    deps = make_deps(probe=DeviceProbe("http://127.0.0.1:9"))
    graph = graph_for(deps, checkpoint_path)
    start_run(graph, "CT200-0012", run_id="r1")
    assert resume_run(graph, "r1", "resolved")["outcome"] == "resolved_by_human"


def test_supervised_mode_waits_for_approval(dashboard, make_deps, checkpoint_path):
    graph = graph_for(make_deps(settings={"supervised": True}), checkpoint_path)
    result = start_run(graph, "CT200-0012", run_id="r1")
    assert result["waiting_for"]["kind"] == "approval"
    assert dashboard.truth("CT200-0012") == dashboard.passwords["CT200-0012"]
    assert resume_run(graph, "r1", "approve")["outcome"] == "succeeded"
    assert_rotated(dashboard, "CT200-0012")


def test_second_run_on_locked_device_is_skipped(dashboard, make_deps, checkpoint_path):
    dashboard.vault.acquire_lock("CT200-0012", "someone-else")
    result = start_run(graph_for(make_deps(), checkpoint_path), "CT200-0012")
    assert result["outcome"] == "skipped_locked"
    assert dashboard.truth("CT200-0012") == dashboard.passwords["CT200-0012"]


def test_no_password_leaks_into_checkpoints_or_audit(dashboard, make_deps, checkpoint_path, tmp_path):
    deps = make_deps()
    dashboard.faults(silent_failure="once")
    for serial in ("CT200-0012", "FL9-0012"):
        start_run(graph_for(deps, checkpoint_path), serial)
    secrets = set(dashboard.passwords.values())
    for serial in ("CT200-0012", "FL9-0012"):
        record = dashboard.vault.get_device(serial)
        secrets |= {record.active, record.previous}
    files = [checkpoint_path, *(tmp_path / "data").rglob("*")]
    blob = b"".join(f.read_bytes() for f in files if f.is_file())
    assert blob and not [s for s in secrets if s.encode() in blob]
