from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path

from modules.automation_engine.account_registry.bale_onboarding import BaleOnboardingService
from modules.automation_engine.commercial_queue.bale_identity import BaleOwnIdentityClassifier, deterministic_mask_match
from modules.automation_engine.plugins.bale.account_store import BaleAccountStore

EXPECTED = "09211690533"

def service(tmp_path: Path) -> tuple[BaleOnboardingService, str]:
    store = BaleAccountStore(tmp_path / "registry")
    store._write_json(store.accounts_path, [])
    svc = BaleOnboardingService(database_path=tmp_path / "state.db", account_store=store, profile_root=tmp_path / "profiles", process_inspector=lambda _: [], auth_ttl_seconds=86400)
    account = svc.provision({"identifier": EXPECTED, "idempotency_key": "identity", "created_by": "test"})["account"]
    return svc, account["account_id"]

def test_authenticated_shell_without_identity_evidence_remains_ineligible(tmp_path: Path) -> None:
    svc, account_id = service(tmp_path)
    result = BaleOwnIdentityClassifier().classify_visible_values([], EXPECTED, [EXPECTED])
    account = svc.reconcile_profile_identity_probe(account_id, identity_status="inconclusive")
    assert result["status"] == "missing"
    assert account["authentication_verified_at"] is None
    assert svc.scheduler_authentication_available(account_id) is False

def test_exact_visible_phone_identity_refreshes_verification(tmp_path: Path) -> None:
    svc, account_id = service(tmp_path)
    result = BaleOwnIdentityClassifier().classify_visible_values(["+۹۸ ۹۲۱ ۱۶۹ ۰۵۳۳"], EXPECTED, [EXPECTED])
    observed = datetime.now(timezone.utc).isoformat()
    account = svc.reconcile_profile_identity_probe(account_id, identity_status=result["status"], observed_at=observed)
    assert result["match_method"] == "exact_visible_phone"
    assert account["profile_probe_result"] == "authenticated_identity_verified"
    assert account["verification_source"] == "visible_profile_identity_probe"
    assert account["authentication_verified_at"] == observed

def test_mismatched_identity_does_not_refresh_verification(tmp_path: Path) -> None:
    svc, account_id = service(tmp_path)
    result = BaleOwnIdentityClassifier().classify_visible_values(["09120000000"], EXPECTED, [EXPECTED, "09120000000"])
    account = svc.reconcile_profile_identity_probe(account_id, identity_status=result["status"], safe_observed_identity=result["observed_masked"][0])
    assert result["status"] == "mismatch"
    assert account["profile_probe_result"] == "identity_mismatch"
    assert account["authentication_verified_at"] is None

def test_masked_identity_requires_unique_deterministic_match() -> None:
    assert deterministic_mask_match("0921******33", EXPECTED, [EXPECTED, "09390000017"]) is True
    assert deterministic_mask_match("0921******33", EXPECTED, [EXPECTED, "09219999933"]) is False
    assert deterministic_mask_match("09********3", EXPECTED, [EXPECTED]) is False

def test_profile_folder_ownership_alone_is_insufficient(tmp_path: Path) -> None:
    _svc, _account_id = service(tmp_path)
    (tmp_path / "profiles" / "bale_09211690533").mkdir(parents=True, exist_ok=True)
    result = BaleOwnIdentityClassifier().classify_visible_values([], EXPECTED, [EXPECTED])
    assert result["verified_match"] is False
    assert result["error_code"] == "own_identity_unverifiable"
