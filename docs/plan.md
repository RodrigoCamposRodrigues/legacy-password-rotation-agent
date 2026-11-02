# Legacy Password Rotation Agent Implementation Plan

**Goal:** Rotate device passwords through a legacy web dashboard with an LLM browser agent, without ever losing a password.

**Architecture:** A fake legacy dashboard (FastAPI + SQLite) plays the devices and the notifier. A rotation service runs a code-owned LangGraph state machine (write-ahead protocol, SQLite checkpointer, `interrupt()` escalations). One node calls a password changer: an LLM agent (`create_agent`) or a scripted agent, both acting only through gated Playwright tools. OpenBao stores and generates passwords.

**Tech Stack:** Python 3.10, FastAPI, LangGraph 1.2, LangChain 1.4 (`create_agent`, `init_chat_model`), langchain-openai, Playwright 1.64 (sync), OpenBao 2.7 (HTTP API via httpx), pytest, LangSmith.

**Spec:** `docs/spec.md`

## Global Constraints

- Passwords never appear in graph state, checkpoints, prompts, tool results, audit logs, traces or exceptions.
- Default model `gpt-6-luna`, set by `ROTATOR_MODEL`.
- Rotation age 180 days. Device list page size 10.
- Budgets per change attempt: 40 tool calls, 60,000 tokens, 180 seconds. Verification retries of **change**: 2.
- CT-200 policy: 16 chars, `a-zA-Z0-9!@#%`. FL-9 policy: exactly 8 digits.
- OpenBao never in `-dev` mode. Unseal key and tokens only in ignored `.bao/`.
- Commits: Conventional Commits + gitmoji, sentence case, trailing period, single line.

## Review Focus

1. **Device unreachable vs wrong password.** A network error on device login must not count as "password wrong"; it escalates as `device_unreachable` and never triggers recovery logic. Test in Task 7.
2. **Crash after the device changed but before the checkpoint.** Re-running **change** must detect that `pending` already works and skip. Test in Task 7 (`mid_change` crash point).
3. **Escrow re-run.** Re-running **escrow** for the same run must reuse the stored `pending`, not generate a second one. Test in Task 7.
4. **Session expiry mid-flow.** The agent lands on `/login`; `login_again()` must restore the session without the LLM seeing the credential. Test in Task 5.
5. **Placeholder in a non-secret field** (e.g. the search box) must be refused, so the secret can't leak into the page or URL. Test in Task 5.

---

## File Structure

```
pyproject.toml, .env.example, .gitignore, docker-compose.yml, openbao/config.hcl
README.md
scripts/bao_init.py        init/unseal/configure OpenBao, write .bao/state.json
scripts/seed.py            seed dashboard DB + vault with matching initial passwords
src/simclock.py            Clock, SystemClock, FakeClock
src/dashboard/             fake legacy app: app.py, db.py, faults.py, notifier.py, templates/
src/rotator/config.py      Settings from env
src/rotator/vault.py       DeviceRecord, Vault protocol, InMemoryVault, OpenBaoVault
src/rotator/device.py      DeviceProbe, DeviceUnreachable
src/rotator/audit.py       AuditLog (jsonl + screenshots)
src/rotator/browser.py     Element, snapshot JS, Budget, BrowserSession (tools + gate)
src/rotator/changers.py    Target, AgentReport, ScriptedChanger, LLMChanger
src/rotator/protocol.py    RunState, Deps, CrashInjector, build_graph
src/rotator/service.py     FastAPI service: webhook, worker, sweep, runs page
tests/                     conftest (live dashboard fixture), one file per task
evals/                     scenarios.py, run_evals.py
```

## Tasks

### Task 1: Scaffold, clock, settings
**Files:** `pyproject.toml`, `.gitignore`, `.env.example`, `src/simclock.py`, `src/rotator/config.py`, `tests/test_clock.py`
**Produces:** `Clock.now() -> datetime` (UTC), `SystemClock(offset_days: float = 0)`, `FakeClock(start: datetime).advance(days: float)`; `Settings.from_env()` with fields `dashboard_url, bao_addr, bao_token, model, data_dir, supervised, max_tool_calls=40, max_tokens=60000, max_seconds=180, verify_retries=2, rotation_days=180`.
- [ ] Test: `FakeClock.advance(180)` moves `now()` by 180 days; `SystemClock(5).now()` is ~5 days ahead.
- [ ] Implement, run `pytest tests/test_clock.py`, commit.

### Task 2: Fake legacy dashboard and notifier
**Files:** `src/dashboard/{app.py,db.py,faults.py,notifier.py}`, `src/dashboard/templates/*.html`, `tests/test_dashboard.py`
**Produces:** `create_app(db_path, clock, webhook_url=None) -> FastAPI`; `Faults` (`slow_ms`, `session_expiry_requests`, `save_error: off|once|always`, `silent_failure: off|once|always`); routes `/login`, `/devices?page=&q=`, `/devices/{id}`, `/devices/{id}?tab=`, `POST /devices/{id}/password`, `POST /devices/{id}/password/confirm`, `POST /device/{serial}/login`, `POST /_test/faults`, `POST /_test/clock`, `POST /_test/notify`, `GET /_test/device/{serial}`; `seed_devices(db, passwords: dict[serial,str])`; `Notifier.run_once() -> list[dict]` (event_id = uuid5 of serial + due date).
- [ ] Tests (FastAPI TestClient): login required; ~20 devices, page 2 exists; CT-200 change needs correct current password and confirm page; FL-9 rejects non-8-digit PIN; `save_error=once` returns 500 then works; `silent_failure=once` shows "Saved!" without changing; device login 200/401; notifier emits once per due device with stable `event_id`, nothing for fresh devices, again after retry if webhook failed.
- [ ] Implement, run, commit.

### Task 3: Vault
**Files:** `src/rotator/vault.py`, `scripts/bao_init.py`, `openbao/config.hcl`, `docker-compose.yml`, `tests/test_vault.py`
**Produces:** `DeviceRecord(serial, model, name, active, pending, pending_run_id, previous, state, active_since, version)`; `Vault` protocol: `get_device(serial) -> DeviceRecord`, `put_device(rec) -> None` (CAS on `rec.version`, raises `VaultConflict`), `list_devices() -> list[str]`, `acquire_lock(serial, run_id) -> bool`, `lock_holder(serial) -> str | None`, `release_lock(serial, run_id)`, `generate_password(model) -> str`, `admin_credential() -> tuple[str, str]`. `InMemoryVault(fail_writes: int = 0)` and `OpenBaoVault(addr, token)`.
- [ ] Contract tests run against both implementations (OpenBao ones skip when `BAO_ADDR` unreachable): CAS conflict raises; lock is exclusive and re-entrant for the same run; generated passwords match each model's policy.
- [ ] `bao_init.py`: init (1 key share), unseal, enable KV v2 at `secret/`, write policies `ct200`/`fl9`, create `rotator` ACL policy + token. Idempotent; unseals only if already initialised.
- [ ] Implement, run against the local `bao` binary, commit.

### Task 4: Device probe and audit log
**Files:** `src/rotator/device.py`, `src/rotator/audit.py`, `tests/test_device_audit.py`
**Produces:** `DeviceProbe(base_url).check(serial, password) -> bool` (raises `DeviceUnreachable` on connection error or 5xx); `AuditLog(run_dir).event(step: str, **fields)`, `.screenshot(page, name)` (masks password inputs), `.read() -> list[dict]`.
- [ ] Tests: 200 → True, 401 → False, closed port → `DeviceUnreachable`; audit events round-trip.
- [ ] Implement, run, commit.

### Task 5: Browser session (tools and gate)
**Files:** `src/rotator/browser.py`, `tests/conftest.py` (live dashboard on a free port via uvicorn thread; Playwright browser fixture), `tests/test_browser.py`
**Consumes:** `Vault`, `AuditLog`.
**Produces:** `Budget(max_tool_calls, max_tokens, max_seconds, usage=UsageMetadataCallbackHandler|None)`; `AttemptAborted(reason)`; `BrowserSession(page, dashboard_url, target_serial, secrets: Callable[[], dict[str, str]], admin_credential: Callable[[], tuple[str,str]], budget, audit)` with `login()`, `snapshot() -> list[Element]`, `read_page() -> str`, `click(id) -> str`, `fill(id, value) -> str`, `select(id, option) -> str`, `request_submit(id) -> str`, `login_again() -> str`, and `gate_rejections: int`. Secrets dict keys: `{{NEW_PASSWORD}}`, `{{CURRENT_PASSWORD}}`.
- [ ] Tests: `read_page` never contains the real password after filling; `fill` of raw text into a secret field refused; placeholder into search box refused; `click` on a POST submit refused; gate refuses submit on Fridge 12B page when target is Fridge 12; gate refuses when confirm field holds a different value; gate passes on correct CT-200 form and confirm page; FL-9 `confirm()` dialog accepted; expired session → `login_again()` works; budget exhaustion raises `AttemptAborted`; second gate rejection raises `AttemptAborted`.
- [ ] Implement, run, commit.

### Task 6: Password changers
**Files:** `src/rotator/changers.py`, `tests/test_changers.py`
**Produces:** `Target(serial, name, model, run_id)`; `AgentReport(device_serial_seen: str, submitted: bool, notes: str)` (Pydantic); `PasswordChanger` protocol `change(session: BrowserSession, target: Target) -> AgentReport`; `ScriptedChanger()` (searches the list by name, opens the device, finds fields by label in `snapshot()`); `LLMChanger(model_name)` (`create_agent` with the session tools, `response_format=AgentReport`, system prompt in `changers.py`).
- [ ] Test: `ScriptedChanger` changes both models end to end against the live dashboard. `LLMChanger` covered by evals (Task 9), plus a unit test with a fake chat model that only checks the tool wiring.
- [ ] Implement, run, commit.

### Task 7: Rotation protocol (Layer 1)
**Files:** `src/rotator/protocol.py`, `tests/test_protocol.py`
**Consumes:** Tasks 3-6.
**Produces:** `RunState` (TypedDict: `run_id, serial, event_id, status, outcome, reason, change_attempts, report`); `Deps(vault, probe, changer, open_session: Callable[[Target], ContextManager[BrowserSession]], clock, settings, crash: CrashInjector, audit_for: Callable[[str], AuditLog])`; `CrashInjector.arm(point: str)` / `.point(name)` raising `SimulatedCrash`; `build_graph(deps, checkpointer) -> CompiledStateGraph`; outcomes `succeeded | skipped_locked | resolved_by_human | aborted` and `status` `escalated` while interrupted.
- Crash points: `after_lock, after_precheck, before_escrow_write, after_escrow_write, mid_change, after_change, after_verify, after_commit_write, after_unlock`.
- [ ] Tests:
  - happy path for both models: vault `active` works on the device, old fails, `previous` holds the old one, lock released;
  - for every crash point, (a) resume the same thread with a new graph on the same SQLite file and (b) start a new run with the checkpoint lost: rotation completes or stays consistent; invariant holds after each;
  - escrow re-run reuses the stored `pending` (Review Focus 3); `mid_change` re-run skips the change (Review Focus 2);
  - vault write failure at escrow → escalated, device untouched;
  - wrong `active` → escalated `precheck_failed`; dashboard down → escalated `device_unreachable` (Review Focus 1);
  - `silent_failure=once` → verify retries and succeeds; `save_error=always` → escalated after 2 retries;
  - resume with `retry`, `resolved`, `abort`; supervised mode pauses before change;
  - second run on a locked device → `skipped_locked`;
  - leak scan: real passwords absent from checkpoint DB bytes and audit files.
- [ ] Implement, run, commit.

### Task 8: Rotation service
**Files:** `src/rotator/service.py`, `src/rotator/templates/*.html`, `tests/test_service.py`
**Produces:** `create_service(deps, db_path) -> FastAPI` with `POST /webhooks/rotation-due`, `POST /sweep`, `GET /runs`, `GET /runs/{id}`, `GET /runs/{id}/shots/{name}`, `POST /runs/{id}/resume`; `Worker` (one thread, queue); `sweep(deps) -> list[str]`; resume of unfinished runs at startup.
- [ ] Tests: duplicate `event_id` → `duplicate`, one run; webhook → run succeeds; device rotated in vault but webhook lost → sweep finds it after clock jump; stale `pending` without live run → sweep starts recovery; paused run resumes from the Runs page form.
- [ ] Implement, run, commit.

### Task 9: LLM agent, evals, docs
**Files:** `evals/scenarios.py`, `evals/run_evals.py`, `README.md`, `docs/future-work.md`
- [ ] Scenarios per spec, 5 repetitions, LangSmith experiment when `LANGSMITH_API_KEY` is set, local JSON + Markdown table in `evals/results/`.
- [ ] Metrics: lost passwords, wrong-device changes, leaks, success, correct escalations, gate rejections, tool calls, tokens, seconds.
- [ ] Run with `gpt-6-luna` once keys exist; then `gpt-6`.
- [ ] README: architecture, protocol, how to run, results. Future work list from the spec.
