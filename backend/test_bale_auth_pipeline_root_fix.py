from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.routes import automation
import modules.automation_engine.commercial_queue.service as commercial_queue_module
from modules.automation_engine.account_registry.bale_onboarding import BaleOnboardingService
from modules.automation_engine.commercial_queue.repository import CommercialQueueRepository
from modules.automation_engine.commercial_queue.service import CommercialQueueService
from modules.automation_engine.commercial_queue.bale_authentication import (
    BALE_BOOTSTRAP_NAVIGATION_TIMEOUT_MS,
    BaleAuthenticationMaintenanceService,
)
from modules.automation_engine.plugins.bale.plugin import BalePlugin
from modules.automation_engine.plugins.bale.account_store import BaleAccountStore
from modules.automation_engine.runtime_sessions.manager import AccountRuntimeSessionManager
from modules.automation_engine.runtime_sessions.models import RuntimeSession


class _VerifiedRegistry:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.closed = 0

    async def execute_open(self, _account_id, _callback, *_args):
        return dict(self.payload)

    async def execute_session_operation(self, _session_id, operation, _callback, *_args):
        if operation == "close":
            self.closed += 1
            return {"closed": True}
        return dict(self.payload)


class _CanonicalProjection:
    def __init__(self, account_id: str) -> None:
        self.account_id = account_id

    def canonical_bale_account_states(self, include_soft_deleted: bool = False):
        return [{"account_id": self.account_id, "lifecycle_ready": True, "worker_eligible": True}]


def _service(tmp_path: Path, count: int = 1, process_inspector=None) -> BaleOnboardingService:
    store = BaleAccountStore(tmp_path / "registry")
    store._write_json(store.accounts_path, [])
    inspector = process_inspector if process_inspector is not None else (lambda _record: [])
    service = BaleOnboardingService(tmp_path / "state.db", store, tmp_path / "profiles", inspector)
    for index in range(count):
        service.provision({
            "identifier": f"0912{index:07d}",
            "idempotency_key": f"provision-{index}",
            "created_by": "isolated-auth-pipeline-test",
        })
    return service


def _account_id(index: int = 0) -> str:
    return f"bale_0912{index:07d}"


def _verify(service: BaleOnboardingService, account_id: str) -> dict:
    return service.reconcile_profile_identity_probe(account_id, identity_status="match")


def test_a_b_c_fresh_existing_session_and_stale_blocker_reconciliation(tmp_path: Path) -> None:
    """A/B/C: positive matching evidence atomically produces canonical readiness."""
    service = _service(tmp_path)
    account_id = _account_id()

    fresh = _verify(service, account_id)
    assert fresh["lifecycle_ready"] is True
    assert fresh["eligible"] is True
    assert fresh["readiness"]["blockers"] == []

    # An already authenticated profile that had old blockers follows precisely
    # the same matching-identity transition; no phone-specific behavior exists.
    with service.connection() as connection:
        connection.execute(
            """UPDATE bale_operational_accounts SET lifecycle_status='login_in_progress', scheduling_enabled=0,
            authentication_status='unverified', authentication_state='auth_probe_inconclusive',
            session_persistence_status='unknown', session_status='login_required', health_status='unknown',
            identity_bound_phone=NULL, identity_verification_status='unverified', identity_verified_at=NULL,
            identity_profile_generation_id=NULL, profile_bound_identity=NULL, onboarding_completed=0,
            profile_persistence_verified=0 WHERE account_id=?""",
            (account_id,),
        )
        connection.execute("UPDATE commercial_account_settings SET enabled=0 WHERE account_id=?", (account_id,))
        connection.commit()

    reconciled = _verify(service, account_id)
    assert reconciled["authentication_status"] == "authenticated"
    assert reconciled["durable_identity_verified"] is True
    assert reconciled["session_persistence_status"] == "verified"
    assert reconciled["lifecycle_ready"] is True
    assert reconciled["eligible"] is True
    assert not set(reconciled["eligibility_reasons"]) & {
        "lifecycle_not_ready", "identity_verification_required", "session_revalidation_required",
        "login_required", "session_persistence_not_verified",
    }


def test_d_operation_terminal_states_and_restart_recovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path)
    account_id = _account_id()
    registry = _VerifiedRegistry({
        "maintenance_session_id": "success-session", "verified": True,
        "maintenance_released": True,
        "auth": {"auth_state": "authenticated", "authenticated": True},
    })
    monkeypatch.setattr(automation, "bale_onboarding_service", service)
    monkeypatch.setattr(automation, "commercial_queue_service", _CanonicalProjection(account_id))
    monkeypatch.setattr(automation, "bale_account_executor_registry", registry)

    succeeded, _ = automation._new_bale_action_operation(account_id, "open_login", "/open", "POST")
    asyncio.run(automation._run_auth_action(succeeded["operation_id"], account_id, "login"))
    assert automation._load_persisted_bale_action_operation(succeeded["operation_id"])["status"] == "succeeded"

    failed_payload = {
        "maintenance_session_id": "failure-session", "verified": False, "terminal": True,
        "auth": {"auth_state": "login_required", "authenticated": False},
    }
    registry.payload = failed_payload
    failed, _ = automation._new_bale_action_operation(account_id, "session_recheck", "/recheck", "POST")
    asyncio.run(automation._run_auth_action(failed["operation_id"], account_id, "session_recheck"))
    assert automation._load_persisted_bale_action_operation(failed["operation_id"])["status"] == "failed"

    orphan, _ = automation._new_bale_action_operation(account_id, "open_login", "/open", "POST")
    automation._bale_action_operations.pop(orphan["operation_id"], None)
    recovery = automation.reconcile_orphaned_bale_account_action_operations()
    restarted = automation._load_persisted_bale_action_operation(orphan["operation_id"])
    assert orphan["operation_id"] in recovery["cancelled_operation_ids"]
    assert restarted["status"] == "cancelled"
    assert restarted["status"] not in {"queued", "running"}
    for operation_id in (succeeded["operation_id"], failed["operation_id"]):
        automation._bale_action_operations.pop(operation_id, None)


def test_e_stale_profile_lease_is_reclaimed_only_when_owner_is_dead(tmp_path: Path) -> None:
    service = _service(tmp_path)
    account_id = _account_id()
    first = service.acquire_profile_launch_lock(account_id, operation_id="old-operation")
    assert service.renew_profile_launch_lock(account_id, "wrong-owner", operation_id="old-operation") is False
    assert service.renew_profile_launch_lock(account_id, first["owner_id"], operation_id="old-operation") is True
    with service.connection() as connection:
        connection.execute(
            "UPDATE bale_profile_launch_locks SET owner_pid=?, expires_at=? WHERE account_id=?",
            (99999999, "2999-01-01T00:00:00+00:00", account_id),
        )
        connection.commit()
    reclaimed = service.acquire_profile_launch_lock(account_id, operation_id="new-operation")
    assert reclaimed["operation_id"] == "new-operation"
    assert reclaimed["owner_id"] != first["owner_id"]
    assert service.release_profile_launch_lock(account_id, reclaimed["owner_id"]) is True


def test_operation_watchdog_terminalizes_stuck_browser_call_without_waiting_for_thread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A blocked Playwright/executor call cannot keep the API operation busy."""
    service = _service(tmp_path)
    account_id = _account_id()

    class HangingRegistry:
        async def execute_open(self, *_args):
            await asyncio.sleep(30)
            return {}

        async def execute_session_operation(self, *_args):
            return {"closed": True}

    monkeypatch.setattr(automation, "bale_onboarding_service", service)
    monkeypatch.setattr(automation, "commercial_queue_service", _CanonicalProjection(account_id))
    monkeypatch.setattr(automation, "bale_account_executor_registry", HangingRegistry())
    operation, _ = automation._new_bale_action_operation(account_id, "open_login", "/open", "POST")
    short_deadline = (datetime.now(timezone.utc)).isoformat()
    automation._update_bale_action_operation(operation["operation_id"], deadline_at=short_deadline)

    import time as _time
    begin = _time.perf_counter()
    asyncio.run(automation._run_auth_action(operation["operation_id"], account_id, "login"))
    elapsed = _time.perf_counter() - begin
    persisted = automation._load_persisted_bale_action_operation(operation["operation_id"])
    assert elapsed < 1.0
    assert persisted["status"] == "timed_out"
    assert persisted["terminal_reason"] == "bale_bootstrap_unresolved"
    automation._bale_action_operations.pop(operation["operation_id"], None)


def test_bale_bootstrap_navigation_has_bounded_domcontentloaded_timeout() -> None:
    calls: list[dict] = []

    class Page:
        def goto(self, url, **kwargs):
            calls.append({"url": url, **kwargs})

        def is_closed(self):
            return False

        url = "https://web.bale.ai"

    maintenance = object.__new__(BaleAuthenticationMaintenanceService)
    maintenance.plugin = type("Plugin", (), {"web_url": "https://web.bale.ai"})()
    result = maintenance._navigate_to_bale_shell(Page())
    assert result["navigated"] is True
    assert calls == [{
        "url": "https://web.bale.ai",
        "wait_until": "domcontentloaded",
        "timeout": BALE_BOOTSTRAP_NAVIGATION_TIMEOUT_MS,
    }]


def test_startup_recovery_times_out_expired_operation_and_releases_only_its_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path)
    account_id = _account_id()
    monkeypatch.setattr(automation, "bale_onboarding_service", service)
    operation, _ = automation._new_bale_action_operation(account_id, "open_login", "/open", "POST")
    service.acquire_profile_launch_lock(account_id, operation_id=operation["operation_id"])
    automation._update_bale_action_operation(
        operation["operation_id"],
        deadline_at=(datetime.now(timezone.utc)).isoformat(),
    )
    automation._bale_action_operations.pop(operation["operation_id"], None)

    result = automation.reconcile_orphaned_bale_account_action_operations()
    persisted = automation._load_persisted_bale_action_operation(operation["operation_id"])
    assert operation["operation_id"] in result["timed_out_operation_ids"]
    assert persisted["status"] == "timed_out"
    assert persisted["terminal_reason"] == "bale_bootstrap_unresolved"
    with service.connection() as connection:
        assert connection.execute("SELECT 1 FROM bale_profile_launch_locks WHERE operation_id=?", (operation["operation_id"],)).fetchone() is None


def test_e_late_close_cannot_release_a_newer_operation_lease(tmp_path: Path) -> None:
    service = _service(tmp_path)
    account_id = _account_id()
    old = service.acquire_profile_launch_lock(account_id, operation_id="old-operation")
    service.record_authentication_open(account_id, {
        "maintenance_session_id": "old-maintenance-session",
        "auth": {"auth_state": "login_required", "authenticated": False},
        "operation_id": "old-operation",
        "profile_generation_id": old["profile_generation_id"],
        "owner_pid": old["owner_pid"],
    })
    assert service.release_profile_launch_lock(account_id, old["owner_id"]) is True
    newer = service.acquire_profile_launch_lock(account_id, operation_id="new-operation")
    service.record_authentication_closed("old-maintenance-session", {"closed": True})
    with service.connection() as connection:
        retained = connection.execute(
            "SELECT owner_id, operation_id FROM bale_profile_launch_locks WHERE account_id=?",
            (account_id,),
        ).fetchone()
    assert retained is not None
    assert retained["owner_id"] == newer["owner_id"]
    assert retained["operation_id"] == "new-operation"


def test_f_duplicate_open_coalesces_one_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path)
    account_id = _account_id()
    monkeypatch.setattr(automation, "bale_onboarding_service", service)
    first, created = automation._new_bale_action_operation(account_id, "open_login", "/open", "POST")
    second, duplicated = automation._new_bale_action_operation(account_id, "open_login", "/open", "POST")
    assert created is True and duplicated is False
    assert second["operation_id"] == first["operation_id"]
    assert len(second["coalesced_requests"]) == 1
    automation._bale_action_operations.pop(first["operation_id"], None)


def test_g_h_i_durable_restart_transient_probe_and_real_logout(tmp_path: Path) -> None:
    service = _service(tmp_path)
    account_id = _account_id()
    ready = _verify(service, account_id)
    assert ready["lifecycle_ready"] is True

    # H: inconclusive browser/network evidence cannot erase a proven session.
    transient = service.record_session_health_probe(account_id, result="temporarily_inconclusive")
    assert transient["durable_identity_verified"] is True
    assert transient["session_health_acceptable"] is True
    assert transient["lifecycle_ready"] is True

    reloaded = BaleOnboardingService(service.database_path, service.account_store, service.profile_root, lambda _record: [])
    after_restart = reloaded.get_account(account_id)
    assert after_restart and after_restart["lifecycle_ready"] is True
    assert after_restart["durable_identity_verified"] is True
    assert "identity_verification_required" not in after_restart["eligibility_reasons"]

    # I: only strong Bale login/OTP/logout evidence demotes session health.
    logged_out = reloaded.record_session_health_probe(account_id, result="login_required")
    assert logged_out["session_health_acceptable"] is False
    assert logged_out["lifecycle_ready"] is False
    assert "login_required" in logged_out["eligibility_reasons"]


def test_bale_runtime_recovers_a_replaced_initial_page() -> None:
    """A closed startup tab is not treated as a lost profile/session."""
    class ClosedPage:
        url = "https://web.bale.ai/chat"

        @staticmethod
        def is_closed() -> bool:
            return True

    class ReplacementPage:
        url = "about:blank"

        @staticmethod
        def is_closed() -> bool:
            return False

    class RecoverableContext:
        def __init__(self) -> None:
            self.pages: list[ReplacementPage] = []
            self.replacement = ReplacementPage()

        @staticmethod
        def is_closed() -> bool:
            return False

        def new_page(self) -> ReplacementPage:
            self.pages.append(self.replacement)
            return self.replacement

    context = RecoverableContext()
    session = RuntimeSession.create(
        account_id="bale_runtime_recovery",
        platform="bale",
        owner_token="test",
        worker_round_id="test",
        profile_path="profile",
        context=context,
        page=ClosedPage(),
    )
    resolved = AccountRuntimeSessionManager().resolve_live_page(session)
    assert resolved is context.replacement
    assert session.page is context.replacement
    assert session.metadata["page_replacement_count"] == 1
    assert session.metadata["page_recovery_required_navigation"] is True


def test_authenticated_bale_shell_wins_over_stale_qr_text() -> None:
    """A QR word in a chat must not demote a visibly authenticated profile."""
    class Locator:
        @staticmethod
        def count() -> int:
            return 15

        @staticmethod
        def inner_text(timeout: int | None = None) -> str:
            return "prior chat mentions QR code"

    class Context:
        @staticmethod
        def cookies() -> list[dict]:
            return [{}]

    class Page:
        url = "https://web.bale.ai/chat"
        context = Context()

        @staticmethod
        def locator(_selector: str) -> Locator:
            return Locator()

        @staticmethod
        def evaluate(_script: str) -> dict:
            return {
                "local_storage_accessible": True,
                "indexeddb_accessible": True,
                "service_worker_state": "activated",
            }

        @staticmethod
        def is_closed() -> bool:
            return False

    plugin = BalePlugin()
    plugin._detect_login_state = lambda _page, timeout_ms=3000: {
        "install_prompt_detected": False,
        "chat_list_visible": True,
        "message_input_detected": False,
        "search_input_detected": True,
        "search_icon_visible": True,
        "login_page_detected": False,
        "login_form_visible": False,
    }
    plugin._contacts_ui_visible = lambda _page: False
    plugin._first_visible_selector = lambda *_args, **_kwargs: ""
    state = plugin.classify_authentication_state(Page())
    assert state["auth_state"] == "authenticated"
    assert state["authenticated"] is True
    assert "stale_login_text_ignored_in_authenticated_shell" in state["detection_evidence"]


def test_j_1000_account_readiness_is_persisted_and_browser_free(tmp_path: Path) -> None:
    calls = 0

    def inspector(_record):
        nonlocal calls
        calls += 1
        return []

    store = BaleAccountStore(tmp_path / "registry-scale")
    accounts = [
        {"account_id": _account_id(index), "phone": f"0912{index:07d}", "username_or_number": f"0912{index:07d}", "browser_provider": "native_chrome"}
        for index in range(1000)
    ]
    store._write_json(store.accounts_path, accounts)
    service = BaleOnboardingService(tmp_path / "scale.db", store, tmp_path / "profiles", inspector)
    service._ensure_schema()
    now = datetime.now(timezone.utc).isoformat()
    with service.connection() as connection:
        for index, account in enumerate(accounts):
            account_id = account["account_id"]
            profile = Path(service.profile_root) / account_id
            profile.mkdir(parents=True, exist_ok=True)
            ready = index < 990
            connection.execute(
                """INSERT INTO bale_operational_accounts
                (account_id,normalized_identifier,masked_identifier,lifecycle_status,scheduling_enabled,
                 authentication_status,session_persistence_status,health_status,canonical_profile_path,
                 normalized_profile_path,onboarding_blocked,created_at,updated_at,audit_metadata_json,
                 profile_generation_id,profile_health,identity_bound_phone,identity_verification_status,
                 identity_verified_at,identity_profile_generation_id,profile_bound_identity,session_status,
                 last_authenticated_shell_at,profile_persistence_verified,onboarding_completed)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    account_id, account["phone"], "masked", "ready" if ready else "login_required", int(ready),
                    "authenticated" if ready else "unverified", "verified" if ready else "unknown", "healthy" if ready else "unknown",
                    str(profile), str(profile).casefold(), 0, now, now, "{}", f"profile-{index}", "healthy",
                    account["phone"] if ready else None, "verified" if ready else "unverified", now if ready else None,
                    f"profile-{index}" if ready else None, account["phone"] if ready else None,
                    "authenticated" if ready else "login_required", now if ready else None, int(ready), int(ready),
                ),
            )
        connection.commit()
    payload = service.list_accounts()
    ready = [item for item in payload["items"] if item["lifecycle_ready"]]
    unhealthy = [item for item in payload["items"] if not item["lifecycle_ready"]]
    assert len(payload["items"]) == 1000
    assert len(ready) == 990
    assert len(unhealthy) == 10
    assert calls == 0
    assert all(not item["operation_busy"] for item in payload["items"])


def test_campaign_capacity_consumes_canonical_auth_readiness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Capacity naturally follows readiness; no campaign-side auth workaround."""
    onboarding = _service(tmp_path, count=9)
    for index in range(7):
        _verify(onboarding, _account_id(index))
    commercial = CommercialQueueService(
        repository=CommercialQueueRepository(onboarding.database_path),
        sleeper=lambda _seconds: None,
    )
    monkeypatch.setattr(commercial_queue_module, "bale_onboarding_service", onboarding)
    first = commercial.campaign_capacity_pool()
    assert first["eligible_account_count"] == 7
    assert len(first["eligible_account_ids"]) == 7

    for index in range(7, 9):
        _verify(onboarding, _account_id(index))
    second = commercial.campaign_capacity_pool()
    assert second["eligible_account_count"] == 9
    assert len(second["eligible_account_ids"]) == 9
