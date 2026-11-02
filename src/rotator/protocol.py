"""The write-ahead rotation protocol as a LangGraph state machine.

Code owns every safety step. The LLM appears only inside `change`. Graph state holds IDs, status and reports,
never passwords: those are read from the vault at the moment they are needed, so checkpoints stay secret-free.

Any node may run twice (a crash before its checkpoint makes LangGraph re-run it), so every node is idempotent.
"""

import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from rotator.audit import AuditLog
from rotator.browser import CURRENT, NEW, AttemptAborted, Budget, BrowserSession, BrowserWorker
from rotator.changers import PasswordChanger, Target
from rotator.config import Settings
from rotator.device import DeviceProbe, DeviceUnreachable
from rotator.vault import Vault, VaultError
from simclock import Clock

CRASH_POINTS = ("after_lock", "after_precheck", "before_escrow_write", "after_escrow_write", "mid_change",
                "after_change", "after_verify", "after_commit_write", "after_unlock")


class SimulatedCrash(Exception):
    pass


class CrashInjector:
    """Lets tests kill a run at a named point, the way a power cut or OOM kill would."""

    def __init__(self):
        self.armed: str | None = None

    def arm(self, point: str) -> None:
        assert point in CRASH_POINTS, point
        self.armed = point

    def point(self, name: str) -> None:
        if self.armed == name:
            self.armed = None
            raise SimulatedCrash(name)


class RunState(TypedDict, total=False):
    run_id: str
    serial: str
    event_id: str
    status: str
    outcome: str
    reason: str
    change_attempts: int
    change_error: str
    report: dict


@dataclass
class Deps:
    vault: Vault
    probe: DeviceProbe
    changer: PasswordChanger
    worker: BrowserWorker
    settings: Settings
    clock: Clock
    crash: CrashInjector = field(default_factory=CrashInjector)
    # Lets a new run take over a lock whose owner is known to be dead. Default: never assume that.
    is_run_alive: Callable[[str], bool] = lambda run_id: True

    def audit(self, run_id: str) -> AuditLog:
        return AuditLog(self.settings.data_dir / "runs" / run_id)

    @contextmanager
    def session(self, target: Target, audit: AuditLog) -> Iterator[BrowserSession]:
        def secrets() -> dict[str, str]:
            record = self.vault.get_device(target.serial)
            return {NEW: record.pending, CURRENT: record.active}

        settings = self.settings
        page = self.worker.new_page()
        try:
            session = BrowserSession(
                page, settings.dashboard_url, target.serial, secrets, self.vault.admin_credential,
                Budget(settings.max_tool_calls, settings.max_tokens, settings.max_seconds), audit, worker=self.worker,
            )
            session.login()
            yield session
        finally:
            self.worker.close_page(page)


def build_graph(deps: Deps, checkpointer):
    vault, probe, crash = deps.vault, deps.probe, deps.crash

    def log(state: RunState, step: str, **fields) -> None:
        deps.audit(state["run_id"]).event(step, serial=state["serial"], **fields)

    def escalate_to(state: RunState, reason: str) -> Command:
        log(state, "escalate", reason=reason)
        return Command(update={"status": "escalated", "reason": reason}, goto="escalate")

    def finish(state: RunState, outcome: str) -> Command:
        return Command(update={"outcome": outcome}, goto="unlock")

    def lock(state: RunState) -> Command:
        serial, run_id = state["serial"], state["run_id"]
        if not vault.acquire_lock(serial, run_id):
            holder = vault.lock_holder(serial)
            if holder and not deps.is_run_alive(holder):
                log(state, "lock_takeover", previous_holder=holder)
                vault.release_lock(serial, holder)
            if not vault.acquire_lock(serial, run_id):
                log(state, "skipped_locked", holder=vault.lock_holder(serial))
                return Command(update={"status": "done", "outcome": "skipped_locked"}, goto=END)
        log(state, "lock")
        crash.point("after_lock")
        return Command(update={"status": "running"}, goto="inspect")

    def inspect(state: RunState) -> Command:
        record = vault.get_device(state["serial"])
        log(state, "inspect", vault_state=record.state)
        return Command(goto="recover" if record.state == "pending" else "precheck")

    def precheck(state: RunState) -> Command:
        record = vault.get_device(state["serial"])
        try:
            ok = probe.check(record.serial, record.active)
        except DeviceUnreachable:
            return escalate_to(state, "device_unreachable")
        log(state, "precheck", active_works=ok)
        if not ok:
            return escalate_to(state, "precheck_failed: the stored active password does not work")
        crash.point("after_precheck")
        return Command(goto="escrow")

    def escrow(state: RunState) -> Command:
        record = vault.get_device(state["serial"])
        if record.state == "pending" and record.pending_run_id == state["run_id"]:
            log(state, "escrow_reused")
        else:
            try:
                new_password = vault.generate_password(record.model)
                crash.point("before_escrow_write")
                record.pending, record.pending_run_id, record.state = new_password, state["run_id"], "pending"
                vault.put_device(record)
            except VaultError as error:
                return escalate_to(state, f"vault_unavailable at escrow: {error}")
            log(state, "escrow", vault_version=record.version)
            crash.point("after_escrow_write")
        return Command(goto="approve" if deps.settings.supervised else "change")

    def approve(state: RunState) -> Command:
        decision = interrupt({"kind": "approval", "serial": state["serial"], "options": ["approve", "abort"]})
        log(state, "approval", decision=decision)
        if decision == "approve":
            return Command(update={"status": "running"}, goto="change")
        return finish(state, "aborted")

    def change(state: RunState) -> Command:
        record = vault.get_device(state["serial"])
        try:
            already = probe.check(record.serial, record.pending)
        except DeviceUnreachable:
            return escalate_to(state, "device_unreachable")
        if already:
            # A crash after the device changed but before this node finished: don't change it twice.
            log(state, "change_skipped_already_applied")
            return Command(goto="verify")
        attempts = state.get("change_attempts", 0) + 1
        target = Target(serial=record.serial, name=record.name, model=record.model, run_id=state["run_id"])
        audit = deps.audit(state["run_id"])
        report, error = {}, ""
        try:
            with deps.session(target, audit) as session:
                report = deps.changer.change(session, target).model_dump()
                log(state, "change", attempt=attempts, tool_calls=session.budget.tool_calls,
                    tokens=session.budget.tokens, seconds=round(session.budget.seconds, 1),
                    gate_rejections=session.gate_rejections, report=report)
        except AttemptAborted as aborted:
            error = aborted.reason
        except Exception as failure:  # noqa: BLE001 - any agent failure goes to verify, which decides
            error = f"{type(failure).__name__}: {str(failure)[:200]}"
        if error:
            log(state, "change_failed", attempt=attempts, error=error)
        crash.point("mid_change")
        return Command(update={"change_attempts": attempts, "change_error": error, "report": report}, goto="verify")

    def verify(state: RunState) -> Command:
        crash.point("after_change")
        record = vault.get_device(state["serial"])
        try:
            new_works = probe.check(record.serial, record.pending)
            old_works = probe.check(record.serial, record.active)
        except DeviceUnreachable:
            return escalate_to(state, "device_unreachable")
        log(state, "verify", new_works=new_works, old_works=old_works)
        crash.point("after_verify")
        if new_works and not old_works:
            return Command(goto="commit")
        if new_works and old_works:
            return escalate_to(state, "both passwords work; the device did not change as expected")
        if old_works:
            if state.get("change_attempts", 0) <= deps.settings.verify_retries:
                return Command(goto="change")
            detail = state.get("change_error") or "the dashboard did not apply the change"
            return escalate_to(state, f"change_not_applied after {state.get('change_attempts', 0)} attempts: {detail}")
        return escalate_to(state, "incident: no stored password works on the device")

    def commit(state: RunState) -> Command:
        record = vault.get_device(state["serial"])
        if record.state == "pending":
            record.previous, record.active = record.active, record.pending
            record.pending, record.pending_run_id, record.state = "", "", "active"
            record.active_since = deps.clock.now().isoformat()
            try:
                vault.put_device(record)
            except VaultError as error:
                return escalate_to(state, f"vault_unavailable at commit: {error}")
            log(state, "commit", vault_version=record.version)
            crash.point("after_commit_write")
        else:
            log(state, "commit_already_done")
        return finish(state, "succeeded")

    def recover(state: RunState) -> Command:
        record = vault.get_device(state["serial"])
        try:
            if record.pending and probe.check(record.serial, record.pending):
                log(state, "recover", found="pending")
                return Command(goto="commit")
            if probe.check(record.serial, record.active):
                record.pending, record.pending_run_id, record.state = "", "", "active"
                vault.put_device(record)
                log(state, "recover", found="active")
                return Command(goto="precheck")
        except DeviceUnreachable:
            return escalate_to(state, "device_unreachable")
        except VaultError as error:
            return escalate_to(state, f"vault_unavailable at recover: {error}")
        return escalate_to(state, "incident: no stored password works on the device")

    def escalate(state: RunState) -> Command:
        decision = interrupt({"kind": "escalation", "serial": state["serial"], "reason": state.get("reason", ""),
                              "options": ["retry", "resolved", "abort"]})
        log(state, "human_decision", decision=decision)
        if decision == "retry":
            return Command(update={"status": "running", "reason": "", "change_attempts": 0, "change_error": ""},
                           goto="inspect")
        return finish(state, "resolved_by_human" if decision == "resolved" else "aborted")

    def unlock(state: RunState) -> dict:
        vault.release_lock(state["serial"], state["run_id"])
        log(state, "unlock", outcome=state.get("outcome"))
        crash.point("after_unlock")
        return {"status": "done"}

    graph = StateGraph(RunState)
    for name, node in [("lock", lock), ("inspect", inspect), ("precheck", precheck), ("escrow", escrow),
                       ("approve", approve), ("change", change), ("verify", verify), ("commit", commit),
                       ("recover", recover), ("escalate", escalate), ("unlock", unlock)]:
        graph.add_node(name, node)
    graph.add_edge(START, "lock")
    graph.add_edge("unlock", END)
    return graph.compile(checkpointer=checkpointer)


def _config(run_id: str) -> dict:
    return {"configurable": {"thread_id": run_id}, "run_name": f"rotation {run_id}"}


def start_run(graph, serial: str, event_id: str = "", run_id: str | None = None) -> dict:
    run_id = run_id or uuid.uuid4().hex[:12]
    state = {"run_id": run_id, "serial": serial, "event_id": event_id, "status": "running", "change_attempts": 0}
    # durability="sync" writes each checkpoint before the next step starts, so a crash loses at most one step.
    graph.invoke(state, _config(run_id), durability="sync")
    return run_view(graph, run_id)


def resume_run(graph, run_id: str, decision: str | None = None) -> dict:
    """decision=None continues a crashed run; a string answers its pending interrupt."""
    graph.invoke(None if decision is None else Command(resume=decision), _config(run_id), durability="sync")
    return run_view(graph, run_id)


def run_view(graph, run_id: str) -> dict[str, Any]:
    snapshot = graph.get_state(_config(run_id))
    values = dict(snapshot.values)
    values["waiting_for"] = snapshot.interrupts[0].value if snapshot.interrupts else None
    values["next"] = list(snapshot.next)
    return values
