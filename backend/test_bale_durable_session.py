from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import pytest

from modules.automation_engine.account_registry.bale_onboarding import BaleOnboardingService
from modules.automation_engine.account_registry.bale_onboarding import BaleOnboardingError
from modules.automation_engine.account_registry.bale_session_maintenance import BaleSessionMaintenanceScheduler
from modules.automation_engine.plugins.bale.account_store import BaleAccountStore


def make_service(tmp_path: Path, count: int = 1) -> BaleOnboardingService:
    store = BaleAccountStore(tmp_path / "registry")
    store._write_json(store.accounts_path, [])
    svc = BaleOnboardingService(tmp_path / "state.db", store, tmp_path / "profiles", lambda _record: [], auth_ttl_seconds=60)
    for index in range(count):
        svc.provision({"identifier": f"0912{index:07d}", "idempotency_key": f"key-{index}", "created_by": "test"})
    return svc


def verify(svc: BaleOnboardingService, account_id: str, observed_at: str | None = None) -> dict:
    return svc.reconcile_profile_identity_probe(account_id, identity_status="match", observed_at=observed_at)


def set_test_configuration(svc: BaleOnboardingService, **changes: object) -> None:
    """Avoid host tzdata dependencies while testing persisted policy."""
    current = svc.configuration()
    payload = {key: value for key, value in current.items() if key not in {"revision", "updated_at"}}
    payload.update(changes)
    with svc.connection() as connection:
        connection.execute(
            "INSERT INTO bale_operational_configuration(id,configuration_json,revision,updated_by,updated_at) VALUES('global',?,1,'test','2000-01-01T00:00:00+00:00') ON CONFLICT(id) DO UPDATE SET configuration_json=excluded.configuration_json",
            (json.dumps(payload),),
        )
        connection.commit()


def test_identity_is_durable_beyond_legacy_ttl(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    account_id = "bale_09120000000"
    set_test_configuration(svc, session_revalidation_interval_seconds=864000)
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    account = verify(svc, account_id, old)
    assert account["legacy_verification_expired"] is True
    assert account["durable_identity_verified"] is True
    assert account["session_health_acceptable"] is True
    assert "verification_expired" not in account["eligibility_reasons"]


def test_confirmed_logout_and_identity_mismatch_block(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    account_id = "bale_09120000000"
    verify(svc, account_id)
    logged_out = svc.reconcile_profile_probe(account_id, authenticated=False)
    assert logged_out["session_status"] == "login_required"
    assert logged_out["session_health_acceptable"] is False
    verify(svc, account_id)
    mismatch = svc.reconcile_profile_identity_probe(account_id, identity_status="mismatch", safe_observed_identity="091***999")
    assert mismatch["durable_identity_verified"] is False
    assert mismatch["session_status"] == "identity_mismatch"


def test_temporary_failure_uses_grace_without_erasing_identity(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    account_id = "bale_09120000000"
    verify(svc, account_id)
    inconclusive = svc.reconcile_profile_identity_probe(account_id, identity_status="inconclusive")
    assert inconclusive["durable_identity_verified"] is True
    assert inconclusive["session_status"] == "temporarily_inconclusive"
    assert inconclusive["session_in_grace"] is True
    assert inconclusive["session_health_acceptable"] is True


def test_healthy_session_refresh_does_not_rewrite_durable_identity(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    account_id = "bale_09120000000"
    original = verify(svc, account_id)
    refreshed = svc.record_session_health_probe(account_id, result="authenticated")
    assert refreshed["identity_verified_at"] == original["identity_verified_at"]
    assert refreshed["identity_verification_source"] == "visible_profile_identity_probe"
    assert refreshed["session_health_acceptable"] is True


def test_profile_paths_are_stable_unique_and_survive_restart(tmp_path: Path) -> None:
    svc = make_service(tmp_path, 45)
    before = {row["account_id"]: (row["canonical_profile_path"], row["profile_generation_id"]) for row in svc.list_accounts()["items"]}
    reloaded = BaleOnboardingService(svc.database_path, svc.account_store, svc.profile_root, lambda _record: [], auth_ttl_seconds=60)
    reloaded.reconcile_startup_state()
    after = {row["account_id"]: (row["canonical_profile_path"], row["profile_generation_id"]) for row in reloaded.list_accounts()["items"]}
    assert before == after
    assert len({value[0] for value in after.values()}) == 45


def test_cleanup_preserves_profile_storage(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    account_id = "bale_09120000000"
    profile = Path(svc.get_account(account_id)["canonical_profile_path"])
    storage = profile / "Default" / "IndexedDB" / "bale.fixture"
    storage.parent.mkdir(parents=True, exist_ok=True)
    storage.write_text("persistent", encoding="utf-8")
    svc.record_authentication_open(account_id, {"maintenance_session_id": "maintenance-1", "auth": {"auth_state": "authenticated", "authenticated": True}})
    svc.record_authentication_closed("maintenance-1", {"closed": True})
    assert storage.read_text(encoding="utf-8") == "persistent"


def test_profile_reset_requires_exact_destructive_confirmation(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    account_id = "bale_09120000000"
    profile = Path(svc.get_account(account_id)["canonical_profile_path"])
    marker = profile / "Default" / "Local Storage" / "marker"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("session", encoding="utf-8")
    with pytest.raises(BaleOnboardingError, match="confirmation"):
        svc.reset_profile(account_id, "wrong")
    assert marker.exists()
    reset = svc.reset_profile(account_id, account_id)
    assert not marker.exists()
    assert reset["session_status"] == "login_required"
    assert reset["durable_identity_verified"] is False


def test_maintenance_scheduler_is_bounded_and_lock_aware(tmp_path: Path) -> None:
    svc = make_service(tmp_path, 5)
    set_test_configuration(svc, maintenance_browser_concurrency=2, bale_session_maintenance_mode="scheduled")
    for account in svc.list_accounts()["items"]:
        Path(account["canonical_profile_path"]).mkdir(parents=True, exist_ok=True)
    first = svc.list_accounts()["items"][0]["account_id"]
    with svc.connection() as connection:
        connection.execute("UPDATE bale_operational_accounts SET session_probe_due_at='2000-01-01T00:00:00+00:00', last_authenticated_shell_at='2000-01-01T00:00:00+00:00', last_session_probe_at='2000-01-01T00:00:00+00:00'")
        connection.execute("UPDATE bale_operational_accounts SET current_browser_owner='delivery-worker' WHERE account_id=?", (first,))
        connection.commit()
    seen: list[str] = []
    async def probe(account_id: str, _full: bool) -> dict:
        seen.append(account_id)
        return {"account_id": account_id}
    result = asyncio.run(BaleSessionMaintenanceScheduler(svc, probe).run_once())
    assert result["executed"] == 2, result
    assert first not in seen
    assert len(set(seen)) == 2


def test_manual_only_scheduler_never_calls_probe_for_due_or_unknown_accounts(tmp_path: Path) -> None:
    svc = make_service(tmp_path, 2)
    seen: list[str] = []
    scheduler = BaleSessionMaintenanceScheduler(svc, lambda account_id, _full: seen.append(account_id))
    result = asyncio.run(scheduler.run_once())
    assert svc.configuration()["bale_session_maintenance_mode"] == "manual_only"
    assert result == {"mode": "manual_only", "automatic_profile_launch_enabled": False, "queued": 0, "selected": [], "executed": 0}
    assert seen == []


def test_old_last_known_authenticated_session_remains_eligible_after_restart(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    account_id = "bale_09120000000"
    old = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
    verified = verify(svc, account_id, old)
    assert verified["onboarding_completed"] is True
    reloaded = BaleOnboardingService(svc.database_path, svc.account_store, svc.profile_root, lambda _record: [])
    account = reloaded.get_account(account_id)
    assert account["session_last_known_state"] == "authenticated"
    assert account["eligibility_based_on_persisted_state"] is True
    assert account["eligible_if_enabled"] is True
    assert "session_revalidation_required" not in account["eligibility_reasons"]
