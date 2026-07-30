from __future__ import annotations

import os
from pathlib import Path

TEST_ROOT = Path(__file__).resolve().parent.parent / ".fastapi_isolated"
os.environ["CLINICOS_AUTOMATION_DATABASE_PATH"] = str(TEST_ROOT / "state.db")
os.environ["CLINICOS_BALE_PROFILE_ROOT"] = str(TEST_ROOT / "profiles")
os.environ["CLINICOS_BALE_RUNTIME_DIR"] = str(TEST_ROOT / "registry")

from fastapi.testclient import TestClient

from app.main import app
from app.routes import automation
from modules.automation_engine.account_registry.bale_onboarding import BaleOnboardingService
from modules.automation_engine.plugins.bale.account_store import BaleAccountStore


class FakeAuthentication:
    def __init__(self) -> None:
        self.sessions = {}

    def audit_bale_authentication_profile(self, account_id):
        return {"account_id": account_id, "all_paths_match_expected": True, "secrets_exposed": False}

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
        return {"maintenance_session_id": session_id, "closed": True, "ok": True}


def test_actual_http_onboarding_flow_is_dependency_isolated(tmp_path, monkeypatch):
    store = BaleAccountStore(tmp_path / "registry")
    store._write_json(store.accounts_path, [])
    service = BaleOnboardingService(tmp_path / "state.db", store, tmp_path / "profiles", lambda _record: [])
    fake = FakeAuthentication()
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
    opened = client.post("/automation/platforms/bale/authentication/open", json={"account_id": account_id, "purpose": "login"}).json()
    session_id = opened["maintenance_session_id"]
    assert client.get(f"/automation/platforms/bale/authentication/status/{session_id}").status_code == 200
    verified = client.post(f"/automation/platforms/bale/authentication/verify/{session_id}").json()
    assert verified["account"]["lifecycle_status"] == "persistence_check_required"
    assert client.post(f"/automation/platforms/bale/authentication/close/{session_id}").status_code == 200

    persistence = client.post("/automation/platforms/bale/authentication/open", json={"account_id": account_id, "purpose": "persistence"}).json()
    verified = client.post(f"/automation/platforms/bale/authentication/verify/{persistence['maintenance_session_id']}").json()
    assert verified["account"]["lifecycle_status"] == "ready"
    client.post(f"/automation/platforms/bale/authentication/close/{persistence['maintenance_session_id']}")
    enabled_response = client.put(f"/automation/platforms/bale/onboarding/accounts/{account_id}/scheduling", json={"enabled": True})
    assert enabled_response.status_code == 200, enabled_response.json()
    enabled = enabled_response.json()
    assert enabled["scheduling_enabled"]
    settings = client.put("/automation/platforms/bale/onboarding/configuration", json={"configuration": {"max_login_concurrency": 2}}).json()
    assert settings["max_login_concurrency"] == 2
    assert client.get("/automation/platforms/bale/onboarding/configuration").json()["max_login_concurrency"] == 2
    assert client.post("/automation/platforms/bale/onboarding/recover-stale-sessions").status_code == 200
    retired = client.post(f"/automation/platforms/bale/onboarding/accounts/{account_id}/retire", json={"reason": "test"}).json()
    assert retired["retired"]
    assert (tmp_path / "profiles" / account_id).exists()


def test_legacy_login_routes_are_contained(monkeypatch):
    client = TestClient(app)
    assert client.post("/automation/platforms/bale/accounts/anything/open-login").status_code == 410
    assert client.post("/automation/platforms/bale/accounts/anything/check-login").status_code == 410
