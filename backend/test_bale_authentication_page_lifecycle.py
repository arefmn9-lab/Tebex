from __future__ import annotations

from test_bale_authentication_maintenance import ACCOUNT_ID, FakePage, _open, _service
from modules.automation_engine.runtime_sessions.manager import RuntimeSessionError


def test_open_request_does_not_close_runtime_page() -> None:
    page = FakePage("authenticated")
    service, adapter, _plugin = _service(page)
    opened = _open(service)
    runtime = service.runtime_session_manager.get_session(ACCOUNT_ID)
    assert opened["terminal"] is False
    assert runtime is not None and runtime.page is page and not page.closed
    assert adapter.close_calls == 0
    service.close_bale_authentication(opened["maintenance_session_id"])


def test_status_polling_never_closes_page() -> None:
    page = FakePage("login_required")
    service, adapter, _plugin = _service(page)
    opened = _open(service)
    status = service.get_bale_authentication_status(opened["maintenance_session_id"])
    assert status["page_open"] is True
    assert adapter.close_calls == 0
    assert not page.closed
    service.close_bale_authentication(opened["maintenance_session_id"])


def test_closed_cached_page_is_replaced_by_newest_live_bale_page() -> None:
    original = FakePage("authenticated")
    original.url = "https://web.bale.ai/chat"
    service, adapter, _plugin = _service(original)
    opened = _open(service)
    runtime = service.runtime_session_manager.get_session(ACCOUNT_ID)
    replacement = FakePage("authenticated")
    replacement.url = "https://web.bale.ai/chat"
    runtime.context.pages = [original, replacement]
    original.closed = True

    status = service.get_bale_authentication_status(opened["maintenance_session_id"])

    assert status["page_open"] is True
    assert status["current_page_id"] != status["original_page_id"]
    assert status["page_replacement_count"] == 1
    assert runtime.page is replacement
    assert adapter.close_calls == 0
    service.close_bale_authentication(opened["maintenance_session_id"])


def test_no_live_page_is_retryable_and_status_does_not_cleanup() -> None:
    page = FakePage("authenticated")
    service, adapter, _plugin = _service(page)
    opened = _open(service)
    runtime = service.runtime_session_manager.get_session(ACCOUNT_ID)
    runtime.context.pages = [page]
    page.closed = True

    status = service.get_bale_authentication_status(opened["maintenance_session_id"])

    assert status["state"] == "temporarily_inconclusive"
    assert status["retryable"] is True
    assert status["terminal"] is False
    assert status["error_code"] == "session_page_closed"
    assert status["another_live_page_existed"] is False
    assert adapter.close_calls == 0


def test_health_error_contains_page_lifecycle_evidence() -> None:
    page = FakePage("authenticated")
    service, _adapter, _plugin = _service(page)
    opened = _open(service)
    runtime = service.runtime_session_manager.get_session(ACCOUNT_ID)
    runtime.context.pages = [page]
    page.closed = True
    try:
        service.runtime_session_manager.health_check(runtime)
    except RuntimeSessionError as exc:
        assert exc.error_code == "session_page_closed"
        assert exc.details["remaining_context_page_count"] == 0
        assert exc.details["another_live_page_existed"] is False
        assert "last_page_url" in exc.details
    else:
        raise AssertionError("closed page should report lifecycle evidence")
