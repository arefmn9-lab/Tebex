from pathlib import Path
import time

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routes import automation
from modules.automation_engine.account_registry.bale_onboarding import BaleOnboardingService
from modules.automation_engine.commercial_queue.repository import CommercialQueueRepository
from modules.automation_engine.commercial_queue.service import CampaignLifecycleError, CommercialQueueService
from modules.automation_engine.plugins.bale.account_store import BaleAccountStore
import modules.automation_engine.commercial_queue.service as service_module


ROOT = Path(__file__).resolve().parents[1]
PAGE = (ROOT / "frontend/src/pages/BaleAccounts.jsx").read_text(encoding="utf-8")
CAPACITY_PAGE = (ROOT / "frontend/src/pages/CommercialCampaigns.jsx").read_text(encoding="utf-8")
API = (ROOT / "frontend/src/api/baleOnboarding.js").read_text(encoding="utf-8")


def _environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    database = tmp_path / "isolated.db"
    store = BaleAccountStore(tmp_path / "registry")
    store._write_json(store.accounts_path, [])
    onboarding = BaleOnboardingService(database, store, tmp_path / "profiles", lambda _record: [])
    monkeypatch.setattr(service_module, "bale_onboarding_service", onboarding)
    service = CommercialQueueService(repository=CommercialQueueRepository(database), account_auth_checker=lambda _id: True, sleeper=lambda _n: None)
    created = onboarding.provision({"identifier": "09120000000", "idempotency_key": "create-1", "created_by": "test"})["account"]
    return service, onboarding, created


def test_non_ready_state_does_not_disable_management_actions():
    assert "worker_eligible" not in PAGE[PAGE.index("function actionAvailability"):PAGE.index("function recordActionClick")]
    assert 'action === "delete_account"' in PAGE
    assert 'const anyPending = authenticationPending || destructivePending;' in PAGE
    assert 'reason: anyPending ? "عملیات قبلی این اکانت هنوز تمام نشده است." : null' in PAGE
    assert "Boolean(busy) || hasActiveSession" not in PAGE
    assert 'type="button"' in PAGE
    assert 'className="row-actions account-actions account-primary-actions"' in PAGE


def test_actions_are_keyed_and_delete_has_one_boolean_confirmation():
    assert "pendingActions[account.account_id]" in PAGE
    assert "setActionPending(account.account_id, action, true)" in PAGE
    assert "window.confirm" in PAGE
    assert "window.prompt" not in PAGE
    assert "deleteBaleAccount(account.account_id)" in PAGE
    assert "delete_profile=true" in API
    assert "/authentication/open" not in API[API.index("export const deleteBaleAccount"):API.index("export const preflightBaleAccount")]


def test_full_delete_removes_only_selected_profile_and_allows_clean_reregistration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service, onboarding, account = _environment(tmp_path, monkeypatch)
    account_id = account["account_id"]
    profile = Path(account["canonical_profile_path"])
    marker = profile / "marker.txt"
    marker.write_text("old", encoding="utf-8")
    old_generation = onboarding.get_account(account_id)["profile_generation_id"]
    campaign = service.repository.create_campaign({"name": "reservation-does-not-bind-account", "platform": "bale", "status": "paused"})
    service.repository.upsert_campaign_capacity_reservation(campaign["id"], 1, 1)
    result = service.delete_bale_account_and_profile(account_id, "delete-test")
    assert result["success"] and result["profile_deleted"] and not result["profile_preserved"]
    assert not profile.exists()
    assert onboarding.get_account(account_id) is None
    assert onboarding.account_store.get_account(account_id) is None

    preflight = onboarding.preflight("09120000000")
    assert preflight["provisioning_allowed"] is True
    recreated = onboarding.provision({"identifier": "09120000000", "idempotency_key": "create-2", "created_by": "test"})["account"]
    assert recreated["profile_generation_id"] != old_generation
    assert Path(recreated["canonical_profile_path"]).exists()
    assert not (Path(recreated["canonical_profile_path"]) / "marker.txt").exists()
    assert recreated["session_status"] == "login_required"
    assert recreated["durable_identity_verified"] is False


def test_assigned_job_blocks_before_profile_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service, onboarding, account = _environment(tmp_path, monkeypatch)
    profile = Path(account["canonical_profile_path"])
    original = service.canonical_bale_account_states
    service.canonical_bale_account_states = lambda **_kwargs: [{**original(include_soft_deleted=True)[0], "active_job": {"status": "assigned"}}]
    with pytest.raises(CampaignLifecycleError, match="cannot be removed") as error:
        service.delete_bale_account_and_profile(account["account_id"], "blocked-test")
    assert "assigned_job" in error.value.summary["blockers"]
    assert profile.exists()
    assert onboarding.get_account(account["account_id"]) is not None


def test_capacity_input_is_an_independent_editable_string():
    assert 'const [capacityInput, setCapacityInput] = useState("")' in CAPACITY_PAGE
    assert "value={capacityInput}" in CAPACITY_PAGE
    assert "setCapacityInput(value)" in CAPACITY_PAGE
    assert "Math.max(0, Number(value || 0))" not in CAPACITY_PAGE
    assert "capacityInputDirty.current = true" in CAPACITY_PAGE
    assert "!capacityInputDirty.current && capacityInitializedCampaign.current !== expandedCampaignId" in CAPACITY_PAGE
    assert "free_account_count" not in CAPACITY_PAGE[CAPACITY_PAGE.index("<NumberInput", CAPACITY_PAGE.index("capacityInput")):CAPACITY_PAGE.index("</FormField>", CAPACITY_PAGE.index("<NumberInput", CAPACITY_PAGE.index("capacityInput")))]


def test_chat_readiness_is_required_by_authentication_classifier():
    source = (ROOT / "backend/modules/automation_engine/plugins/bale/plugin.py").read_text(encoding="utf-8")
    assert "chat_list_ready" in source
    assert "chat_row_count > 0" in source
    assert "bale_authenticated_but_chats_stuck_loading" in source
    assert "bale_authenticated_but_chat_list_empty" in source
    assert "bale_shell_loaded_but_storage_failed" in source


def test_production_shaped_historical_accounts_keep_last_known_good_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    database = tmp_path / "nine-shaped.db"
    store = BaleAccountStore(tmp_path / "registry-nine")
    store._write_json(store.accounts_path, [])
    onboarding = BaleOnboardingService(database, store, tmp_path / "profiles-nine", lambda _record: [])
    repository = CommercialQueueRepository(database)
    monkeypatch.setattr(service_module, "bale_onboarding_service", onboarding)
    service = CommercialQueueService(repository=repository, account_auth_checker=lambda _id: True, sleeper=lambda _n: None)

    for index in range(9):
        phone = f"09214035{index:03d}"
        account = onboarding.provision({"identifier": phone, "idempotency_key": f"shape-{index}", "created_by": "test"})["account"]
        account_id = account["account_id"]
        repository.upsert_account_settings(account_id, {"enabled": index < 7, "worker_status": "idle"})
        if index < 7:
            onboarding.reconcile_profile_identity_probe(account_id, identity_status="match")
            # Copy-shaped legacy rows can retain a durable identity and positive
            # evidence while a newer transient probe stores an unknown state.
            with onboarding.connection() as connection:
                connection.execute(
                    "UPDATE bale_operational_accounts SET session_status='unknown_auth_state', authentication_status='authenticated' WHERE account_id=?",
                    (account_id,),
                )
                connection.commit()

    states = service.canonical_bale_account_states()
    assert len(states) == 9
    historical = states[:7]
    assert all(row["durable_identity_verified"] for row in historical)
    assert all(row["session_state"] == "authenticated" for row in historical)
    assert all(row["backend_auth_state"] == "authenticated" for row in historical)
    assert all(row["canonical_compatibility_source"] == "historical_last_known_good" for row in historical)
    assert all(row["worker_eligible"] for row in historical), [(row["account_id"], row["blockers"]) for row in historical]
    assert all(not row["durable_identity_verified"] for row in states[7:])


def test_authentication_reads_operational_account_when_registry_mirror_is_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    database = tmp_path / "split-registry.db"
    store = BaleAccountStore(tmp_path / "split-registry-json")
    store._write_json(store.accounts_path, [])
    onboarding = BaleOnboardingService(database, store, tmp_path / "split-profiles", lambda _record: [])
    account = onboarding.provision({"identifier": "09214036441", "idempotency_key": "split", "created_by": "test"})["account"]
    account_id = account["account_id"]
    # Reproduce the production-shaped defect: durable operational data and
    # canonical profile remain, while the old JSON registry mirror is absent.
    store.delete_account(account_id)
    monkeypatch.setattr(service_module, "bale_onboarding_service", onboarding)

    service = CommercialQueueService(
        repository=CommercialQueueRepository(database),
        account_auth_checker=lambda _id: True,
        sleeper=lambda _seconds: None,
    )
    resolved = service.bale_authentication.account_store.get_account(account_id)

    assert resolved is not None
    assert resolved["account_id"] == account_id
    assert resolved["normalized_identifier"] == account["normalized_identifier"]
    assert resolved["canonical_profile_path"] == account["canonical_profile_path"]


def test_full_delete_handles_missing_legacy_registry_mirror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service, onboarding, account = _environment(tmp_path, monkeypatch)
    account_id = account["account_id"]
    profile = Path(account["canonical_profile_path"])
    onboarding.account_store.delete_account(account_id)

    result = service.delete_bale_account_and_profile(account_id, "delete-split-registry")

    assert result["success"] is True
    assert result["profile_deleted"] is True
    assert onboarding.get_account(account_id) is None
    assert not profile.exists()


def test_bale_delete_route_removes_durable_state_before_same_phone_reregistration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service, onboarding, account = _environment(tmp_path, monkeypatch)
    account_id = account["account_id"]
    profile = Path(account["canonical_profile_path"])
    old_generation = account["profile_generation_id"]
    monkeypatch.setattr(automation, "bale_onboarding_service", onboarding)
    monkeypatch.setattr(automation, "commercial_queue_service", service)
    automation._bale_action_operations.clear()

    with TestClient(app) as client:
        accepted = client.delete(f"/automation/platforms/bale/accounts/{account_id}?delete_profile=true")
        assert accepted.status_code == 202
        operation_id = accepted.json()["operation_id"]
        terminal = {}
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            terminal = client.get(f"/automation/platforms/bale/account-operations/{operation_id}").json()
            if terminal["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.01)

    assert terminal["status"] == "completed", terminal
    assert onboarding.get_account(account_id) is None
    assert onboarding.account_store.get_account(account_id) is None
    assert not profile.exists()
    assert onboarding.preflight("09120000000")["provisioning_allowed"] is True
    recreated = onboarding.provision({"identifier": "09120000000", "idempotency_key": "route-reregister", "created_by": "test"})["account"]
    assert recreated["profile_generation_id"] != old_generation
