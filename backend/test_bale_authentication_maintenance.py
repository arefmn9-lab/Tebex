from __future__ import annotations

import asyncio
import os
from pathlib import Path
import threading
import time
from typing import Any

from app.main import app
from app.routes import automation as automation_routes
from fastapi.testclient import TestClient
from modules.automation_engine.commercial_queue.bale_authentication import BaleAuthenticationMaintenanceService, FakeBaleAuthenticationMaintenanceService
from modules.automation_engine.commercial_queue.repository import CommercialQueueRepository
from modules.automation_engine.commercial_queue.service import CommercialQueueService
from modules.automation_engine.runtime_sessions.manager import AccountRuntimeSessionManager
from modules.automation_engine.platforms.bale_adapter import BaleDeliveryAdapter


ACCOUNT_ID = "bale_auth_test_09211690533"
PROFILE_PATH = Path("runtime/browser_profiles") / ACCOUNT_ID


def test_fake_authentication_controls_are_deterministic_and_suffix_scoped(monkeypatch) -> None:
    monkeypatch.setenv("CLINICOS_FAKE_BALE_AUTH_MODE", "authenticated")
    monkeypatch.setenv("CLINICOS_FAKE_BALE_AUTH_FAILURE_SUFFIXES", "6441")
    service = FakeBaleAuthenticationMaintenanceService()

    success_ids = [f"bale_093926090{index:02d}" for index in range(10, 19)]
    successful_sessions = [service.open(account_id) for account_id in success_ids]
    assert all(item["auth"]["auth_state"] == "authenticated" for item in successful_sessions)
    assert all(item["auth"]["chat_list_ready"] is True for item in successful_sessions)
    assert all(service.verify(item["maintenance_session_id"])["verified"] is True for item in successful_sessions)

    failed = service.open("bale_09214036441")
    assert failed["auth"]["auth_state"] == "login_required"
    assert failed["auth"]["error_code"] == "fake_session_recheck_failure"
    assert service.verify(failed["maintenance_session_id"])["verified"] is False


def test_fake_authentication_defaults_to_the_existing_login_code_state(monkeypatch) -> None:
    monkeypatch.delenv("CLINICOS_FAKE_BALE_AUTH_MODE", raising=False)
    monkeypatch.delenv("CLINICOS_FAKE_BALE_AUTH_FAILURE_SUFFIXES", raising=False)
    opened = FakeBaleAuthenticationMaintenanceService().open("bale_09392609017")
    assert opened["auth"] == {
        "auth_state": "verification_code_required",
        "authenticated": False,
        "chat_shell_visible": False,
        "chat_list_ready": False,
    }


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
            "chat_list_ready": state == "authenticated",
            "chat_readiness_state": "bale_authenticated_and_chats_loaded" if state == "authenticated" else "temporarily_inconclusive",
            "chat_row_count": 1 if state == "authenticated" else 0,
            "login_check": {"login_form_visible": state in {"login_required", "qr_login_required"}},
            "legacy_auth_state": state,
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
    db_path = Path(os.environ["CLINICOS_DB_PATH"]).resolve().parent / "test_bale_authentication_maintenance.db"
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
    profile_path = Path(os.environ["CLINICOS_BALE_PROFILE_ROOT"]).resolve() / ACCOUNT_ID
    profile_path.mkdir(parents=True, exist_ok=True)
    service.update_browser_identity(ACCOUNT_ID, {"profile_path": str(profile_path), "enabled": True})
    return service, adapter, plugin


def _open(service: CommercialQueueService) -> dict[str, Any]:
    return service.open_bale_authentication(ACCOUNT_ID)


def test_runtime_adapter_uses_authoritative_onboarding_registration() -> None:
    """A SQLite/onboarding account must not be looked up again in global JSON."""
    captured: dict[str, Any] = {}

    class RecordingPlugin:
        def create_reusable_runtime_session(self, account_id, provider_mode=None, profile_path=None, account_record=None):
            captured.update(
                account_id=account_id,
                provider_mode=provider_mode,
                profile_path=profile_path,
                account_record=account_record,
            )
            return {"profile_path": profile_path, "page": object(), "context": object()}

    authoritative = {"account_id": ACCOUNT_ID, "phone": "09211690533", "browser_provider": "native_chrome"}
    result = BaleDeliveryAdapter(plugin=RecordingPlugin()).create_runtime_session_for_account(
        ACCOUNT_ID,
        "owner",
        "round",
        {"profile_path": str(PROFILE_PATH), "account_record": authoritative},
    )

    assert captured["account_record"] is authoritative
    assert captured["account_id"] == ACCOUNT_ID
    assert result["profile_path"] == str(PROFILE_PATH)


def test_exact_browser_identity_profile_is_used() -> None:
    service, adapter, _ = _service(FakePage("login_required"))
    result = _open(service)
    expected = (Path(os.environ["CLINICOS_BALE_PROFILE_ROOT"]) / ACCOUNT_ID).resolve()
    assert Path(result["profile_path"]).resolve() == expected
    assert Path(adapter.create_calls[0]["profile_path"]).resolve() == expected
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
    service, adapter, _ = _service()
    first = _open(service)
    second = _open(service)
    assert second["maintenance_session_id"] == first["maintenance_session_id"]
    assert second["runtime_session_id"] == first["runtime_session_id"]
    assert second["reused"] is True
    assert len(adapter.create_calls) == 1
    service.close_bale_authentication(first["maintenance_session_id"])


def test_stale_maintenance_session_is_recovered_before_reopen() -> None:
    page = FakePage()
    service, adapter, _ = _service(page)
    first = _open(service)
    page.closed = True

    prepared = service.prepare_bale_authentication_open(ACCOUNT_ID)

    assert prepared["action"] == "recovered"
    assert prepared["maintenance_session_id"] == first["maintenance_session_id"]
    assert service.runtime_session_manager.get_session(ACCOUNT_ID) is None
    assert adapter.close_calls == 1

    adapter.page = FakePage()
    reopened = _open(service)
    assert reopened["maintenance_session_id"] != first["maintenance_session_id"]
    assert len(adapter.create_calls) == 2
    service.close_bale_authentication(reopened["maintenance_session_id"])


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


def test_authentication_verification_does_not_change_paused_campaign_or_jobs() -> None:
    service, adapter, plugin = _service(FakePage("authenticated"))
    campaign = service.create_campaign({"name": "auth-isolation", "platform": "bale", "status": "paused", "source_channel_uid": "safe-source"})
    before_campaign = service.repository.get_campaign(campaign["id"])
    before_counts = service.repository.campaign_job_counts(campaign["id"])
    opened = _open(service)
    service.verify_bale_authentication(opened["maintenance_session_id"])
    service.close_bale_authentication(opened["maintenance_session_id"])
    after_campaign = service.repository.get_campaign(campaign["id"])
    assert after_campaign["status"] == before_campaign["status"] == "paused"
    assert service.repository.campaign_job_counts(campaign["id"]) == before_counts
    assert adapter.deliver_calls == 0
    assert plugin.source_channel_calls == 0
    assert plugin.forward_picker_opened_count == 0


def test_manual_credentials_are_never_accepted_and_status_exposes_no_secrets() -> None:
    automation_routes._bale_action_operations.clear()
    service, _, _ = _service(FakePage("login_required"))
    previous = automation_routes.commercial_queue_service
    previous_onboarding = automation_routes.bale_onboarding_service
    class FakeOnboarding:
        def connection(self): return previous_onboarding.connection()
        def acquire_profile_launch_lock(self, account_id): return {"owner_id": "fake"}
        def record_authentication_open(self, account_id, result, purpose="login"): return {}
        def release_profile_launch_lock(self, account_id, owner_id): return True
    automation_routes.bale_onboarding_service = FakeOnboarding()
    automation_routes.commercial_queue_service = service
    try:
        response = TestClient(app).post("/automation/platforms/bale/authentication/open", json={"account_id": ACCOUNT_ID, "password": "secret", "otp": "123456"})
    finally:
        automation_routes.bale_account_executor_registry.shutdown_all()
        automation_routes.commercial_queue_service = previous
        automation_routes.bale_onboarding_service = previous_onboarding
    body = response.json()
    assert response.status_code == 202
    assert "password" not in str(body)
    assert "'otp'" not in str(body)
    assert "123456" not in str(body)
    assert body["operation_id"].startswith("baleop_")


def test_authentication_lifecycle_routes_run_sync_operations_outside_asyncio_loop() -> None:
    automation_routes._bale_action_operations.clear()
    previous = automation_routes.commercial_queue_service
    previous_onboarding = automation_routes.bale_onboarding_service
    calls: list[tuple[str, int, bool]] = []

    def record(name: str) -> None:
        try:
            asyncio.get_running_loop()
            loop_running = True
        except RuntimeError:
            loop_running = False
        calls.append((name, threading.get_ident(), loop_running))

    class LifecycleService:
        def open_bale_authentication(self, account_id):
            record("open")
            return {"maintenance_session_id": "session-thread-boundary", "account_id": account_id, "verified": True, "maintenance_released": True}
        def canonical_bale_account_states(self, include_soft_deleted=False):
            return [{"account_id": ACCOUNT_ID, "worker_eligible": True}]
        def get_bale_authentication_status(self, session_id):
            record("status")
            return {"maintenance_session_id": session_id, "account_id": ACCOUNT_ID}
        def verify_bale_authentication(self, session_id):
            record("verify")
            return {"maintenance_session_id": session_id, "account_id": ACCOUNT_ID, "verified": True}
        def close_bale_authentication(self, session_id):
            record("close")
            return {"maintenance_session_id": session_id, "account_id": ACCOUNT_ID, "closed": True}
        def refresh_authenticated_bale_worker_eligibility(self):
            record("worker_ready")
            return {"enabled_account_ids": [ACCOUNT_ID], "enabled_count": 1}

    class LifecycleOnboarding:
        def connection(self): return previous_onboarding.connection()
        def acquire_profile_launch_lock(self, account_id): return {"owner_id": "thread-boundary"}
        def release_profile_launch_lock(self, account_id, owner_id): return True
        def record_authentication_open(self, account_id, result, purpose="login"): return {}
        def record_authentication_status(self, session_id, result): return {}
        def record_authentication_verified(self, session_id, result): return {"account_id": ACCOUNT_ID}
        def record_authentication_closed(self, session_id, result): return {"account_id": ACCOUNT_ID}

    automation_routes.commercial_queue_service = LifecycleService()
    automation_routes.bale_onboarding_service = LifecycleOnboarding()
    try:
        with TestClient(app) as client:
            opened = client.post("/automation/platforms/bale/authentication/open", json={"account_id": ACCOUNT_ID, "purpose": "login"})
            operation_id = opened.json()["operation_id"]
            for _ in range(50):
                status = client.get(f"/automation/platforms/bale/account-operations/{operation_id}")
                if status.json()["status"] == "succeeded": break
                time.sleep(0.01)
    finally:
        automation_routes.bale_account_executor_registry.shutdown_all()
        automation_routes.commercial_queue_service = previous
        automation_routes.bale_onboarding_service = previous_onboarding

    assert [opened.status_code, status.status_code] == [202, 200]
    assert status.json()["status"] == "succeeded"
    assert [name for name, _, _ in calls] == ["open"]
    assert all(not loop_running for _, _, loop_running in calls)
    assert all(thread_id != threading.get_ident() for _, thread_id, _ in calls)
    assert len({thread_id for _, thread_id, _ in calls}) == 1


def test_multi_account_authentication_uses_independent_stable_executors() -> None:
    automation_routes._bale_action_operations.clear()
    previous = automation_routes.commercial_queue_service
    previous_onboarding = automation_routes.bale_onboarding_service
    account_a = "bale_09211690533"
    account_b = "bale_09392609017"
    calls: list[tuple[str, str, int, bool]] = []
    sessions: dict[str, str] = {}

    def record(account_id: str, operation: str) -> None:
        try:
            asyncio.get_running_loop()
            loop_running = True
        except RuntimeError:
            loop_running = False
        calls.append((account_id, operation, threading.get_ident(), loop_running))

    class MultiAccountService:
        def open_bale_authentication(self, account_id):
            record(account_id, "open")
            session_id = f"session-{account_id}"
            sessions[session_id] = account_id
            return {"maintenance_session_id": session_id, "account_id": account_id, "verified": True, "maintenance_released": True}

        def canonical_bale_account_states(self, include_soft_deleted=False):
            return [{"account_id": account_a, "worker_eligible": True}, {"account_id": account_b, "worker_eligible": True}]

        def get_bale_authentication_status(self, session_id):
            account_id = sessions[session_id]
            record(account_id, "status")
            return {"maintenance_session_id": session_id, "account_id": account_id, "closed": False}

        def close_bale_authentication(self, session_id):
            account_id = sessions[session_id]
            record(account_id, "close")
            return {"maintenance_session_id": session_id, "account_id": account_id, "closed": True}

    class MultiAccountOnboarding:
        def connection(self): return previous_onboarding.connection()
        def acquire_profile_launch_lock(self, account_id): return {"owner_id": f"owner-{account_id}"}
        def release_profile_launch_lock(self, account_id, owner_id): return True
        def record_authentication_open(self, account_id, result, purpose="login"): return {}
        def record_authentication_status(self, session_id, result): return {}
        def record_authentication_closed(self, session_id, result): return {"account_id": result["account_id"]}

    automation_routes.commercial_queue_service = MultiAccountService()
    automation_routes.bale_onboarding_service = MultiAccountOnboarding()
    try:
        with TestClient(app) as client:
            opened_a = client.post("/automation/platforms/bale/authentication/open", json={"account_id": account_a})
            opened_b = client.post("/automation/platforms/bale/authentication/open", json={"account_id": account_b})
            statuses = []
            for opened in (opened_a, opened_b):
                operation_id = opened.json()["operation_id"]
                for _ in range(50):
                    status = client.get(f"/automation/platforms/bale/account-operations/{operation_id}")
                    if status.json()["status"] == "succeeded": break
                    time.sleep(0.01)
                statuses.append(status)
    finally:
        automation_routes.bale_account_executor_registry.shutdown_all()
        automation_routes.commercial_queue_service = previous
        automation_routes.bale_onboarding_service = previous_onboarding

    assert [opened_a.status_code, opened_b.status_code] == [202, 202]
    assert all(item.json()["status"] == "succeeded" for item in statuses)
    calls_a = [item for item in calls if item[0] == account_a]
    calls_b = [item for item in calls if item[0] == account_b]
    assert {item[2] for item in calls_a} and len({item[2] for item in calls_a}) == 1
    assert {item[2] for item in calls_b} and len({item[2] for item in calls_b}) == 1
    assert calls_a[0][2] != calls_b[0][2]
    assert all(not item[3] for item in calls)
    assert [item[1] for item in calls_b] == ["open"]


def test_verify_updates_account_health_to_healthy() -> None:
    page = FakePage("authenticated")
    service, _, _ = _service(page)
    opened = _open(service)
    verified = service.verify_bale_authentication(opened["maintenance_session_id"])
    health = service.get_account_health(ACCOUNT_ID)
    assert verified["verified"] is True
    assert verified["identity_check"]["verified_match"] is True
    assert health["health_status"] == "healthy"
    assert health["last_authentication_verified_at"]
    service.close_bale_authentication(opened["maintenance_session_id"])


def test_authenticated_shell_with_stuck_chats_is_not_verified() -> None:
    service, _adapter, plugin = _service(FakePage("authenticated"))
    original = plugin.classify_authentication_state

    def stuck(page, timeout_ms=3000):
        return {
            **original(page, timeout_ms),
            "auth_state": "auth_unverified",
            "authenticated": False,
            "chat_shell_visible": True,
            "chat_list_ready": False,
            "chat_readiness_state": "bale_authenticated_but_chats_stuck_loading",
            "loading_visible": True,
            "error_code": "bale_authenticated_but_chats_stuck_loading",
        }

    plugin.classify_authentication_state = stuck
    opened = _open(service)
    verified = service.verify_bale_authentication(opened["maintenance_session_id"])

    assert verified["verified"] is False
    assert verified["auth"]["chat_readiness_verified"] is False
    assert verified["verification_evidence"]["chat_readiness_state"] == "bale_authenticated_but_chats_stuck_loading"
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
    assert status["closed"] is False
    assert status["state"] == "temporarily_inconclusive"
    assert status["retryable"] is True
    assert len(service.runtime_session_manager.list_active_sessions()) == 1
    service.close_bale_authentication(opened["maintenance_session_id"])
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
