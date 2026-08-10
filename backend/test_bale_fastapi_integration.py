from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.routes import automation
from modules.automation_engine.account_registry.bale_onboarding import BaleOnboardingService
from modules.automation_engine.plugins.bale.account_store import BaleAccountStore


class FakeAuthentication:
    def __init__(self) -> None:
        self.sessions = {}
        self.onboarding = None

    def canonical_bale_account_states(self):
        return self.onboarding.list_accounts()["items"] if self.onboarding is not None else []

    def audit_bale_authentication_profile(self, account_id):
        return {"account_id": account_id, "all_paths_match_expected": True, "secrets_exposed": False}

    def prepare_bale_authentication_open(self, account_id):
        session = self.sessions.get(f"fake-{account_id}")
        if session and not session.get("closed"):
            return {"action": "reuse", "account_id": account_id, "session": {**session, "reused": True}}
        return {"action": "new", "account_id": account_id}

    def open_bale_authentication(self, account_id):
        session_id = f"fake-{account_id}"
        result = {"maintenance_session_id": session_id, "runtime_session_id": "fake-runtime", "account_id": account_id, "closed": False, "auth": {"auth_state": "verification_code_required", "authenticated": False}}
        self.sessions[session_id] = result
        return result

    def get_bale_authentication_status(self, session_id):
        return self.sessions[session_id]

    def verify_bale_authentication(self, session_id):
        result = {**self.sessions[session_id], "verified": True, "auth": {"auth_state": "authenticated", "authenticated": True, "chat_shell_visible": True, "contacts_ui_available": True}, "identity_check": {"status": "match", "verified_match": True}}
        self.sessions[session_id] = result
        return result

    def close_bale_authentication(self, session_id):
        if session_id in self.sessions:
            self.sessions[session_id]["closed"] = True
        return {"maintenance_session_id": session_id, "closed": True, "ok": True}


def test_actual_http_onboarding_flow_is_dependency_isolated(tmp_path, monkeypatch):
    store = BaleAccountStore(tmp_path / "registry")
    store._write_json(store.accounts_path, [])
    service = BaleOnboardingService(tmp_path / "state.db", store, tmp_path / "profiles", lambda _record: [])
    fake = FakeAuthentication()
    fake.onboarding = service
    monkeypatch.setattr(automation, "bale_onboarding_service", service)
    monkeypatch.setattr(automation, "commercial_queue_service", fake)
    client = TestClient(app)

    assert client.get("/automation/platforms/bale/onboarding/accounts").json()["items"] == []
    preflight = client.post("/automation/platforms/bale/onboarding/preflight", json={"identifier": "+989123456789"}).json()
    assert preflight["provisioning_allowed"]
    provisioned = client.post("/automation/platforms/bale/onboarding/provision", json={"identifier": "09123456789", "idempotency_key": "http-one"}).json()
    account_id = provisioned["account"]["account_id"]
    assert client.get(f"/automation/platforms/bale/onboarding/accounts/{account_id}/reconcile").json()["safe_to_preserve"]
    batch = client.post("/automation/platforms/bale/onboarding/batches", json={"name": "test", "target_count": 7, "account_ids": [account_id]}).json()
    assert batch["current_batch_size"] == 1
    opened_response = client.post("/automation/platforms/bale/authentication/open", json={"account_id": account_id, "purpose": "login"})
    assert opened_response.status_code == 202
    opened = opened_response.json()
    assert opened["operation_id"].startswith("baleop_")
    assert client.get(f"/automation/platforms/bale/account-operations/{opened['operation_id']}").status_code == 200
    settings = client.put("/automation/platforms/bale/onboarding/configuration", json={"configuration": {"max_login_concurrency": 2}}).json()
    assert settings["max_login_concurrency"] == 2
    assert client.get("/automation/platforms/bale/onboarding/configuration").json()["max_login_concurrency"] == 2
    assert client.post("/automation/platforms/bale/onboarding/recover-stale-sessions").status_code == 200
    assert (tmp_path / "profiles" / account_id).exists()


def test_legacy_login_routes_are_contained(monkeypatch):
    client = TestClient(app)
    assert client.post("/automation/platforms/bale/accounts/anything/open-login").status_code == 410
    assert client.post("/automation/platforms/bale/accounts/anything/check-login").status_code == 410


def test_account_operation_result_survives_in_memory_registry_restart(tmp_path, monkeypatch):
    store = BaleAccountStore(tmp_path / "registry-operation")
    store._write_json(store.accounts_path, [])
    onboarding = BaleOnboardingService(tmp_path / "operations.db", store, tmp_path / "profiles-operation", lambda _record: [])
    account = onboarding.provision({"identifier": "09211690534", "idempotency_key": "operation", "created_by": "test"})["account"]
    monkeypatch.setattr(automation, "bale_onboarding_service", onboarding)
    operation, _ = automation._new_bale_action_operation(account["account_id"], "session_recheck", "/test", "POST")
    automation._update_bale_action_operation(operation["operation_id"], status="completed", stage="completed", success=True, result={"persisted": True})
    automation._bale_action_operations.pop(operation["operation_id"], None)

    response = TestClient(app).get(f"/automation/platforms/bale/account-operations/{operation['operation_id']}")
    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert response.json()["result"] == {"persisted": True}


def test_existing_bale_account_authentication_open_is_mounted_and_cors_safe(tmp_path, monkeypatch):
    account_id = "bale_09211690533"
    fake = FakeAuthentication()
    store = BaleAccountStore(tmp_path / "registry")
    store._write_json(store.accounts_path, [])
    onboarding = BaleOnboardingService(tmp_path / "state.db", store, tmp_path / "profiles", lambda _record: [])
    onboarding.provision({"identifier": "09211690533", "idempotency_key": "existing", "created_by": "test"})
    fake.onboarding = onboarding
    monkeypatch.setattr(automation, "bale_onboarding_service", onboarding)
    monkeypatch.setattr(automation, "commercial_queue_service", fake)
    client = TestClient(app)

    response = client.post(
        "/automation/platforms/bale/authentication/open",
        headers={"Origin": "http://127.0.0.1:5173"},
        json={"account_id": account_id, "purpose": "login"},
    )

    assert response.status_code == 202
    assert response.json()["account_id"] == account_id
    assert response.json()["operation_id"].startswith("baleop_")
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"

    repeated = client.post(
        "/automation/platforms/bale/authentication/open",
        headers={"Origin": "http://127.0.0.1:5173"},
        json={"account_id": account_id, "purpose": "login"},
    )
    assert repeated.status_code == 202
    first_status = client.get(f"/automation/platforms/bale/account-operations/{response.json()['operation_id']}").json()["status"]
    if first_status in {"queued", "running"}:
        assert repeated.json()["operation_id"] == response.json()["operation_id"]
    else:
        assert repeated.json()["operation_id"].startswith("baleop_")

    automation.bale_account_executor_registry.shutdown_all()
