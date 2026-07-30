from __future__ import annotations

from pathlib import Path
from typing import Any

from app.main import app
from app.routes import automation as automation_routes
from fastapi.testclient import TestClient
from modules.automation_engine.commercial_queue.bale_authentication import BaleAuthenticationMaintenanceService
from modules.automation_engine.commercial_queue.repository import CommercialQueueRepository
from modules.automation_engine.commercial_queue.service import CommercialQueueService
from modules.automation_engine.runtime_sessions.manager import AccountRuntimeSessionManager


ACCOUNT_ID = "bale_auth_test_09211690533"
PROFILE_PATH = Path("runtime/browser_profiles") / ACCOUNT_ID


class FakePage:
    def __init__(self, auth_state: str = "login_required") -> None:
        self.auth_state = auth_state
        self.url = ""
        self.closed = False
        self.goto_calls: list[str] = []
        self.contacts_visible = False

    def goto(self, url: str, wait_until: str = "load", timeout: int | None = None) -> None:
        self.url = url
        self.goto_calls.append(url)
        if "/contacts" in url and self.auth_state == "authenticated":
            self.contacts_visible = True

    def title(self) -> str:
        if self.closed:
            raise RuntimeError("closed")
        return "Bale"

    def is_closed(self) -> bool:
        return self.closed

    def evaluate(self, script: str) -> str:
        if self.closed:
            raise RuntimeError("closed")
        return "complete"


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.closed = False

    def close(self) -> None:
        self.closed = True
        self.page.closed = True


class FakeAdapter:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.create_calls: list[dict[str, Any]] = []
        self.close_calls = 0
        self.deliver_calls = 0

    def create_runtime_session_for_account(self, account_id: str, owner_token: str, worker_round_id: str, policy: dict[str, Any]) -> dict[str, Any]:
        self.create_calls.append({"account_id": account_id, "profile_path": policy.get("profile_path")})
        return {"page": self.page, "context": FakeContext(self.page), "profile_path": str(policy["profile_path"]), "browser_path": "fake-chrome"}

    def close_runtime_session(self, session: Any) -> dict[str, Any]:
        self.close_calls += 1
        if session.context:
            session.context.close()
        return {"ok": True, "closed": True}

    def execute_plan(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.deliver_calls += 1
        return {"success": False}


class FakePlugin:
    web_url = "https://web.bale.ai"

    def __init__(self) -> None:
        self.contact_prepare_calls = 0
        self.source_channel_calls = 0
        self.forward_picker_opened_count = 0
        self.confirm_click_count = 0
        self.messages_sent_count = 0

    def classify_authentication_state(self, page: FakePage, timeout_ms: int = 3000) -> dict[str, Any]:
        state = page.auth_state
        return {
            "auth_state": state,
            "authenticated": state == "authenticated",
            "login_ui_visible": state in {"login_required", "verification_code_required", "qr_login_required"},
            "chat_shell_visible": state == "authenticated",
            "contacts_ui_available": bool(page.contacts_visible),
            "page_url": page.url,
            "error_code": "authentication_required" if state in {"login_required", "verification_code_required", "qr_login_required"} else (state if state == "unknown_auth_state" else None),
            "detection_evidence": [state],
            "diagnostics_consistent": state != "unknown_auth_state",
        }

    def _first_visible_selector(self, page: FakePage, selectors: list[str], timeout_ms: int = 750) -> str:
        return "text=متوجه شدم" if page.auth_state == "install_help_prompt" else ""

    def _click_if_possible(self, page: FakePage, selector: str) -> bool:
        if selector and page.auth_state == "install_help_prompt":
            page.auth_state = "login_required"
            return True
        return False

    def _contacts_ui_visible(self, page: FakePage) -> bool:
        return bool(page.contacts_visible)

    def save_bale_contact(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.contact_prepare_calls += 1
        return {"success": False}

    def open_bale_source_channel(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.source_channel_calls += 1
        return {"success": False}

    def forward_latest_channel_message(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.forward_picker_opened_count += 1
        return {"success": False}


class FakeMatchingIdentityClassifier:
    def classify(self, page: Any, registered_identifier: str) -> dict[str, Any]:
        return {"status": "match", "verified_match": True, "blocking": False, "secrets_accessed": False}


class FakeAccountStore:
    def get_account(self, account_id: str) -> dict[str, Any]:
        return {"account_id": account_id, "phone": "09211690533"}


def _service(page: FakePage | None = None) -> tuple[CommercialQueueService, FakeAdapter, FakePlugin]:
    db_path = Path("runtime/test_bale_authentication_maintenance.db")
    if db_path.exists():
        db_path.unlink()
    service = CommercialQueueService(repository=CommercialQueueRepository(db_path), sleeper=lambda seconds: None)
    page = page or FakePage()
    adapter = FakeAdapter(page)
    plugin = FakePlugin()
    service.platform_adapters["bale"] = adapter
    service.runtime_session_manager = AccountRuntimeSessionManager({"bale": adapter})
    service.runtime_session_manager.identity_resolver = service.browser_identity_resolver
    service.bale_authentication = BaleAuthenticationMaintenanceService(
        runtime_session_manager=service.runtime_session_manager,
        browser_identity_resolver=service.browser_identity_resolver,
        account_health=service.account_health,
        plugin=plugin,
        identity_classifier=FakeMatchingIdentityClassifier(),
        account_store=FakeAccountStore(),
    )
    profile_path = Path(__file__).resolve().parent / PROFILE_PATH
    profile_path.mkdir(parents=True, exist_ok=True)
    service.update_browser_identity(ACCOUNT_ID, {"profile_path": str(profile_path), "enabled": True})
    return service, adapter, plugin


def _open(service: CommercialQueueService) -> dict[str, Any]:
    return service.open_bale_authentication(ACCOUNT_ID)


def test_exact_browser_identity_profile_is_used() -> None:
    service, adapter, _ = _service(FakePage("login_required"))
    result = _open(service)
    assert result["profile_path"].endswith(str(PROFILE_PATH))
    assert adapter.create_calls[0]["profile_path"].endswith(str(PROFILE_PATH))
    service.close_bale_authentication(result["maintenance_session_id"])


def test_legacy_mismatched_path_is_rejected() -> None:
    service, _, _ = _service()
    service.bale_authentication.plugin.web_url = "https://web.bale.ai"
    service.bale_authentication.audit_profile_paths = lambda account_id: {"all_paths_match_expected": False}
    try:
        _open(service)
    except Exception as exc:
        assert getattr(exc, "error_code", "") == "profile_path_mismatch"
    else:
        raise AssertionError("profile mismatch should be rejected")


def test_only_one_maintenance_session_per_account() -> None:
    service, _, _ = _service()
    first = _open(service)
    try:
        _open(service)
    except Exception as exc:
        assert getattr(exc, "error_code", "") == "maintenance_session_already_active"
    else:
        raise AssertionError("second maintenance session should fail")
    service.close_bale_authentication(first["maintenance_session_id"])


def test_same_profile_cannot_be_used_by_worker_and_maintenance() -> None:
    service, _, _ = _service()
    service.runtime_session_manager.acquire_or_create_session(ACCOUNT_ID, "worker", "round", {"platform": "bale", "session_reuse_enabled": True})
    try:
        _open(service)
    except Exception as exc:
        assert getattr(exc, "error_code", "") == "profile_session_already_active"
    else:
        raise AssertionError("maintenance should not open over worker session")


def test_authenticated_state_detected_correctly() -> None:
    service, _, _ = _service(FakePage("authenticated"))
    opened = _open(service)
    assert opened["auth"]["authenticated"] is True
    assert opened["auth"]["chat_shell_visible"] is True
    service.close_bale_authentication(opened["maintenance_session_id"])


def test_login_required_state_detected_and_health_updated() -> None:
    service, _, _ = _service(FakePage("login_required"))
    opened = _open(service)
    health = service.get_account_health(ACCOUNT_ID)
    assert opened["auth"]["auth_state"] == "login_required"
    assert opened["auth"]["error_code"] == "authentication_required"
    assert health["health_status"] == "auth_required"
    service.close_bale_authentication(opened["maintenance_session_id"])


def test_install_help_prompt_handled_separately() -> None:
    page = FakePage("install_help_prompt")
    service, _, _ = _service(page)
    opened = _open(service)
    assert opened["auth"]["auth_state"] == "login_required"
    assert page.auth_state == "login_required"
    service.close_bale_authentication(opened["maintenance_session_id"])


def test_unknown_state_does_not_pass_authentication() -> None:
    service, _, _ = _service(FakePage("unknown_auth_state"))
    opened = _open(service)
    assert opened["auth"]["authenticated"] is False
    service.close_bale_authentication(opened["maintenance_session_id"])


def test_maintenance_open_launches_no_worker_job_contact_source_or_forwarding() -> None:
    service, adapter, plugin = _service(FakePage("login_required"))
    opened = _open(service)
    assert adapter.deliver_calls == 0
    assert plugin.contact_prepare_calls == 0
    assert plugin.source_channel_calls == 0
    assert plugin.forward_picker_opened_count == 0
    assert plugin.confirm_click_count == 0
    assert plugin.messages_sent_count == 0
    service.close_bale_authentication(opened["maintenance_session_id"])


def test_manual_credentials_are_never_accepted_and_status_exposes_no_secrets() -> None:
    service, _, _ = _service(FakePage("login_required"))
    previous = automation_routes.commercial_queue_service
    previous_onboarding = automation_routes.bale_onboarding_service
    class FakeOnboarding:
        def acquire_profile_launch_lock(self, account_id): return {"owner_id": "fake"}
        def record_authentication_open(self, account_id, result, purpose="login"): return {}
        def release_profile_launch_lock(self, account_id, owner_id): return True
    automation_routes.bale_onboarding_service = FakeOnboarding()
    automation_routes.commercial_queue_service = service
    try:
        response = TestClient(app).post("/automation/platforms/bale/authentication/open", json={"account_id": ACCOUNT_ID, "password": "secret", "otp": "123456"})
    finally:
        automation_routes.commercial_queue_service = previous
        automation_routes.bale_onboarding_service = previous_onboarding
    body = response.json()
    assert response.status_code == 200
    assert "password" not in str(body)
    assert "'otp'" not in str(body)
    assert "123456" not in str(body)
    service.close_bale_authentication(body["maintenance_session_id"])


def test_verify_updates_account_health_to_healthy() -> None:
    page = FakePage("authenticated")
    service, _, _ = _service(page)
    opened = _open(service)
    verified = service.verify_bale_authentication(opened["maintenance_session_id"])
    health = service.get_account_health(ACCOUNT_ID)
    assert verified["verified"] is True
    assert verified["auth"]["contacts_ui_available"] is True
    assert health["health_status"] == "healthy"
    assert health["last_authentication_verified_at"]
    service.close_bale_authentication(opened["maintenance_session_id"])


def test_close_releases_lock_and_repeated_close_is_idempotent() -> None:
    service, adapter, _ = _service()
    opened = _open(service)
    first = service.close_bale_authentication(opened["maintenance_session_id"])
    second = service.close_bale_authentication(opened["maintenance_session_id"])
    assert first["closed"] is True
    assert second["already_closed"] is True
    assert adapter.close_calls == 1
    assert service.runtime_session_manager.list_active_sessions() == []


def test_manually_closed_browser_is_cleaned_safely() -> None:
    page = FakePage("authenticated")
    service, _, _ = _service(page)
    opened = _open(service)
    page.closed = True
    status = service.get_bale_authentication_status(opened["maintenance_session_id"])
    assert status["closed"] is True
    assert service.runtime_session_manager.list_active_sessions() == []


if __name__ == "__main__":
    test_exact_browser_identity_profile_is_used()
    test_legacy_mismatched_path_is_rejected()
    test_only_one_maintenance_session_per_account()
    test_same_profile_cannot_be_used_by_worker_and_maintenance()
    test_authenticated_state_detected_correctly()
    test_login_required_state_detected_and_health_updated()
    test_install_help_prompt_handled_separately()
    test_unknown_state_does_not_pass_authentication()
    test_maintenance_open_launches_no_worker_job_contact_source_or_forwarding()
    test_manual_credentials_are_never_accepted_and_status_exposes_no_secrets()
    test_verify_updates_account_health_to_healthy()
    test_close_releases_lock_and_repeated_close_is_idempotent()
    test_manually_closed_browser_is_cleaned_safely()
    print("Bale authentication maintenance tests passed")
