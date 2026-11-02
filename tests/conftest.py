import pytest

from rotator.audit import AuditLog
from rotator.browser import Budget, BrowserSession, BrowserWorker, _browser_env
from testbed import start_dashboard


@pytest.fixture(scope="session")
def _dashboard_server(tmp_path_factory):
    live = start_dashboard(tmp_path_factory.mktemp("dash") / "dashboard.db")
    yield live
    live.stop()


@pytest.fixture
def dashboard(_dashboard_server):
    _dashboard_server.reset()
    return _dashboard_server


@pytest.fixture(scope="session")
def browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(env=_browser_env())
        yield b
        b.close()


@pytest.fixture
def page(browser):
    context = browser.new_context()
    yield context.new_page()
    context.close()


@pytest.fixture(scope="session")
def worker():
    w = BrowserWorker()
    yield w
    w.close()


@pytest.fixture
def worker_page(worker):
    page = worker.new_page()
    yield page
    worker.close_page(page)


@pytest.fixture
def make_session(dashboard, page, worker, worker_page, tmp_path):
    """threaded=True puts the page on the browser worker, as the protocol and the LLM agent need."""
    def factory(serial: str, new_password: str | None = None, threaded: bool = False, **budget):
        record = dashboard.vault.get_device(serial)
        new = new_password or dashboard.vault.generate_password(record.model)
        secrets = {"{{NEW_PASSWORD}}": new, "{{CURRENT_PASSWORD}}": record.active}
        session = BrowserSession(
            worker_page if threaded else page, dashboard.url, serial, lambda: secrets, dashboard.vault.admin_credential,
            Budget(budget.get("max_tool_calls", 40), 60_000, budget.get("max_seconds", 60)),
            AuditLog(tmp_path / "audit"), worker=worker if threaded else None,
            known_serials=dashboard.vault.list_devices(),
        )
        session.login()
        session.new_password = new
        return session
    return factory
