from __future__ import annotations

import asyncio
import time

from fastapi.testclient import TestClient

from app import main as main_module
from app.main import app
from app.routes import automation
from modules.automation_engine.account_registry.bale_onboarding import BaleOnboardingService
from modules.automation_engine.plugins.bale.account_store import BaleAccountStore


class _Registry:
    def __init__(self, opened: dict):
        self.opened = opened
        self.closed = False

    async def execute_open(self, account_id, callback, *args):
        return dict(self.opened)

    async def execute_session_operation(self, session_id, operation, callback, *args):
        if operation == "close":
            self.closed = True
            return {"closed": True}
        return dict(self.opened)


class _CanonicalService:
    def __init__(self, account_id: str, fail_after: bool = False):
        self.account_id = account_id
        self.calls = 0
        self.fail_after = fail_after

    def canonical_bale_account_states(self, include_soft_deleted=False):
        self.calls += 1
        if self.fail_after and self.calls > 1:
            raise RuntimeError("canonical refresh unavailable")
        return [{"account_id": self.account_id, "worker_eligible": self.calls > 1}]


def _onboarding(tmp_path, phone="09392609017"):
    store = BaleAccountStore(tmp_path / "registry")
    store._write_json(store.accounts_path, [])
    service = BaleOnboardingService(tmp_path / "operations.db", store, tmp_path / "profiles", lambda _record: [])
    account = service.provision({"identifier": phone, "idempotency_key": f"test-{phone}", "created_by": "test"})["account"]
    return service, account["account_id"]


def test_session_recheck_account_error_is_persisted_without_campaign_exception(tmp_path, monkeypatch):
    onboarding, account_id = _onboarding(tmp_path)
    registry = _Registry({
        "maintenance_session_id": "fake-session",
        "terminal": True,
        "verified": False,
        "auth": {"auth_state": "login_required"},
    })
    monkeypatch.setattr(automation, "bale_onboarding_service", onboarding)
    monkeypatch.setattr(automation, "commercial_queue_service", _CanonicalService(account_id))
    monkeypatch.setattr(automation, "bale_account_executor_registry", registry)

    operation, _ = automation._new_bale_action_operation(account_id, "session_recheck", "/test", "POST")
    asyncio.run(automation._run_auth_action(operation["operation_id"], account_id, "session_recheck"))
    automation._bale_action_operations.pop(operation["operation_id"], None)
    persisted = automation._load_persisted_bale_action_operation(operation["operation_id"])

    assert persisted["status"] == "failed"
    assert persisted["error_code"] == "authentication_required"
    assert persisted["failed_step"] == "wait_for_authenticated_identity"
    assert persisted["nested_error"]["exception_type"] == "BaleOnboardingError"
    assert persisted["result"]["last_authentication_result"]["auth"]["auth_state"] == "login_required"
    assert registry.closed is True
    assert "CampaignLifecycleError" not in persisted["error_message"]


def test_terminal_failure_persists_when_canonical_after_refresh_fails(tmp_path, monkeypatch):
    onboarding, account_id = _onboarding(tmp_path, "09214036441")
    registry = _Registry({
        "maintenance_session_id": "fake-session",
        "terminal": True,
        "verified": True,
        "auth": {"auth_state": "authenticated"},
    })
    monkeypatch.setattr(automation, "bale_onboarding_service", onboarding)
    monkeypatch.setattr(automation, "commercial_queue_service", _CanonicalService(account_id, fail_after=True))
    monkeypatch.setattr(automation, "bale_account_executor_registry", registry)

    operation, _ = automation._new_bale_action_operation(account_id, "session_recheck", "/test", "POST")
    asyncio.run(automation._run_auth_action(operation["operation_id"], account_id, "session_recheck"))
    automation._bale_action_operations.pop(operation["operation_id"], None)
    persisted = automation._load_persisted_bale_action_operation(operation["operation_id"])

    assert persisted["status"] == "failed"
    assert persisted["error_code"] == "RuntimeError"
    assert persisted["failed_step"] == "load_after_persisted_account_state"
    assert persisted["result"]["last_authentication_result"]["verified"] is True
    assert persisted["result"]["before_persisted_account_state"]["account_id"] == account_id
    assert persisted["nested_error"]["traceback"]


def test_different_actions_for_one_account_coalesce_to_one_active_operation(tmp_path, monkeypatch):
    """An Open/Login and a Session Recheck must never race for one profile."""
    onboarding, account_id = _onboarding(tmp_path, "09392609019")
    monkeypatch.setattr(automation, "bale_onboarding_service", onboarding)
    try:
        opening, opening_created = automation._new_bale_action_operation(account_id, "open_login", "/open", "POST")
        recheck, recheck_created = automation._new_bale_action_operation(account_id, "session_recheck", "/session-recheck", "POST")

        assert opening_created is True
        assert recheck_created is False
        assert recheck["operation_id"] == opening["operation_id"]
        assert recheck["action"] == "open_login"
        assert recheck["last_coalesced_request"] == {
            "action": "session_recheck",
            "endpoint": "/session-recheck",
            "http_method": "POST",
            "requested_at": recheck["last_coalesced_request"]["requested_at"],
        }
        assert recheck["coalesced_requests"][-1]["action"] == "session_recheck"
    finally:
        automation._bale_action_operations.pop(opening["operation_id"], None)


def test_fastapi_coalesces_open_recheck_and_delete_for_one_account(tmp_path, monkeypatch):
    """All account endpoints must share the same account-scoped operation guard."""
    onboarding, account_id = _onboarding(tmp_path, "09392609020")
    monkeypatch.setattr(automation, "bale_onboarding_service", onboarding)

    def discard_scheduled_action(coroutine):
        coroutine.close()

    monkeypatch.setattr(automation, "_schedule_bale_action", discard_scheduled_action)
    with TestClient(app) as client:
        opened = client.post(
            "/automation/platforms/bale/authentication/open",
            json={"account_id": account_id, "purpose": "login"},
        )
        rechecked = client.post(
            "/automation/platforms/bale/authentication/session-recheck",
            json={"account_id": account_id, "purpose": "session_recheck"},
        )
        deleted = client.delete(f"/automation/platforms/bale/accounts/{account_id}")

    try:
        assert opened.status_code == rechecked.status_code == deleted.status_code == 202
        assert rechecked.json()["operation_id"] == opened.json()["operation_id"]
        assert deleted.json()["operation_id"] == opened.json()["operation_id"]
        assert deleted.json()["action"] == "open_login"
        assert [request["action"] for request in deleted.json()["coalesced_requests"]] == [
            "session_recheck",
            "delete_account",
        ]
    finally:
        automation._bale_action_operations.pop(opened.json()["operation_id"], None)


def test_backend_restart_cancels_orphaned_account_operation_for_terminal_polling(tmp_path, monkeypatch):
    """A new process must not leave a persisted browser action permanently queued."""
    onboarding, account_id = _onboarding(tmp_path, "09392609021")
    monkeypatch.setattr(automation, "bale_onboarding_service", onboarding)
    monkeypatch.setattr(main_module, "bale_onboarding_service", onboarding)
    operation, _ = automation._new_bale_action_operation(account_id, "open_login", "/open", "POST")
    operation_id = operation["operation_id"]
    automation._bale_action_operations.pop(operation_id, None)

    with TestClient(app) as client:
        terminal = client.get(f"/automation/platforms/bale/account-operations/{operation_id}")
        reconciliation = app.state.bale_account_action_reconciliation

    assert terminal.status_code == 200
    payload = terminal.json()
    assert payload["status"] == "cancelled"
    assert payload["stage"] == "cancelled"
    assert payload["error_code"] == "account_operation_interrupted_by_restart"
    assert payload["restart_reconciliation"]["reason"] == "in_memory_operation_owner_missing"
    assert payload["lock_release_result"]["release_attempted"] is False
    assert reconciliation["cancelled_operation_ids"] == [operation_id]


def test_fastapi_session_recheck_reaches_persisted_terminal_account_error(tmp_path, monkeypatch):
    onboarding, account_id = _onboarding(tmp_path, "09392609018")
    registry = _Registry({
        "maintenance_session_id": "fake-route-session",
        "terminal": True,
        "verified": False,
        "auth": {"auth_state": "login_required"},
    })
    monkeypatch.setattr(automation, "bale_onboarding_service", onboarding)
    monkeypatch.setattr(automation, "commercial_queue_service", _CanonicalService(account_id))
    monkeypatch.setattr(automation, "bale_account_executor_registry", registry)

    with TestClient(app) as client:
        accepted = client.post(
            "/automation/platforms/bale/authentication/session-recheck",
            json={"account_id": account_id, "purpose": "session_recheck"},
        )
        assert accepted.status_code == 202
        operation_id = accepted.json()["operation_id"]
        for _ in range(100):
            terminal = client.get(f"/automation/platforms/bale/account-operations/{operation_id}")
            if terminal.json()["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.01)

        assert terminal.status_code == 200
        assert terminal.json()["status"] == "failed"
        assert terminal.json()["error_code"] == "authentication_required"
        assert terminal.json()["nested_error"]["exception_type"] == "BaleOnboardingError"

        automation._bale_action_operations.pop(operation_id, None)
        after_registry_restart = client.get(f"/automation/platforms/bale/account-operations/{operation_id}")
        assert after_registry_restart.status_code == 200
        assert after_registry_restart.json()["status"] == "failed"
        assert after_registry_restart.json()["failed_step"] == "wait_for_authenticated_identity"
