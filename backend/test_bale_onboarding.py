from __future__ import annotations

import json
from pathlib import Path

import pytest

from modules.automation_engine.account_registry.bale_onboarding import BaleOnboardingError, BaleOnboardingService
from modules.automation_engine.plugins.bale.account_store import BaleAccountStore


def service(tmp_path: Path) -> BaleOnboardingService:
    store = BaleAccountStore(tmp_path / "registry")
    store._write_json(store.accounts_path, [])
    return BaleOnboardingService(
        database_path=tmp_path / "state.db",
        account_store=store,
        profile_root=tmp_path / "profiles",
        process_inspector=lambda _record: [],
        auth_ttl_seconds=3600,
    )


def provision(svc: BaleOnboardingService, phone: str, key: str, batch_id: str | None = None):
    return svc.provision({"identifier": phone, "idempotency_key": key, "onboarding_batch_id": batch_id, "created_by": "test"})


def test_preflight_is_side_effect_free_and_normalizes_phone(tmp_path: Path) -> None:
    svc = service(tmp_path)
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    result = svc.preflight("+98 912-345-6789")
    after = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    assert result["normalized_identifier"] == "09123456789"
    assert result["provisioning_allowed"] is True
    assert before == after
    assert not (tmp_path / "profiles").exists()


def test_provision_is_idempotent_and_scheduling_disabled(tmp_path: Path) -> None:
    svc = service(tmp_path)
    first = provision(svc, "09123456789", "same-key")
    replay = provision(svc, "+989123456789", "same-key")
    assert first["account"]["account_id"] == "bale_09123456789"
    assert replay["idempotent_replay"] is True
    assert replay["account"]["scheduling_enabled"] is False
    assert replay["account"]["lifecycle_status"] == "login_required"
    assert (tmp_path / "profiles" / "bale_09123456789").is_dir()
    assert len(svc.list_accounts()["items"]) == 1


def test_duplicate_and_conflicting_retry_rejected(tmp_path: Path) -> None:
    svc = service(tmp_path)
    provision(svc, "09123456789", "key-a")
    duplicate = svc.preflight("00989123456789")
    assert duplicate["provisioning_allowed"] is False
    assert "duplicate_bale_identifier" in duplicate["blocking_error_codes"]
    with pytest.raises(BaleOnboardingError) as exc:
        provision(svc, "09120000000", "key-a")
    assert exc.value.error_code == "idempotency_conflict"


def test_dynamic_batches_and_more_than_eight_accounts(tmp_path: Path) -> None:
    svc = service(tmp_path)
    ids = []
    for index in range(40):
        phone = f"0912{index:07d}"
        ids.append(provision(svc, phone, f"key-{index}")["account"]["account_id"])
    batch = svc.create_batch({"name": "subset", "target_count": 7, "account_ids": ids[:3], "created_by": "test"})
    assert len(svc.list_accounts()["items"]) == 40
    assert batch["current_batch_size"] == 3
    assert batch["target_count"] == 7
    extra = provision(svc, "09350000000", "account-41")
    assert extra["account"]["account_id"] not in batch["account_ids"]
    assert len(svc.list_accounts()["items"]) == 41


def test_existing_account_reconciliation_does_not_mutate(tmp_path: Path) -> None:
    svc = service(tmp_path)
    svc.account_store.create_account({"account_id": "legacy_account", "phone": "09121111111", "status": "disabled"})
    registry_before = svc.account_store.accounts_path.read_bytes()
    result = svc.reconcile("legacy_account")
    assert result["registry_present"] is True
    assert result["safe_to_preserve"] is True
    assert svc.account_store.accounts_path.read_bytes() == registry_before
    assert not (tmp_path / "profiles" / "legacy_account").exists()


def test_retirement_preserves_profile_and_other_accounts(tmp_path: Path) -> None:
    svc = service(tmp_path)
    first = provision(svc, "09121111111", "one")["account"]
    second = provision(svc, "09122222222", "two")["account"]
    profile = Path(first["canonical_profile_path"])
    retired = svc.transition(first["account_id"], "retired", reason="test")
    assert retired["retired"] is True
    assert retired["scheduling_enabled"] is False
    assert profile.exists()
    assert svc.get_account(second["account_id"])["lifecycle_status"] == "login_required"


def test_scheduler_authentication_requires_persistence_and_ready(tmp_path: Path) -> None:
    svc = service(tmp_path)
    account = provision(svc, "09123333333", "auth")["account"]
    assert svc.scheduler_authentication_available(account["account_id"]) is False
    with svc.connection() as connection:
        connection.execute(
            """UPDATE bale_operational_accounts SET lifecycle_status='ready',
            authentication_status='authenticated', authentication_verified_at=?,
            verification_expires_at=?, session_persistence_status='verified',
            health_status='healthy' WHERE account_id=?""",
            ("2099-01-01T00:00:00+00:00", "2099-01-02T00:00:00+00:00", account["account_id"]),
        )
        connection.commit()
    assert svc.scheduler_authentication_available(account["account_id"]) is True


def test_no_fixed_account_slots_in_serialized_state(tmp_path: Path) -> None:
    svc = service(tmp_path)
    provision(svc, "09124444444", "dynamic")
    encoded = json.dumps(svc.list_accounts())
    assert "account_1" not in encoded
    assert "account_8" not in encoded


def test_login_concurrency_is_configured_not_account_count(tmp_path: Path) -> None:
    svc = service(tmp_path)
    first = provision(svc, "09125555555", "lock-one")["account"]
    second = provision(svc, "09126666666", "lock-two")["account"]
    lock = svc.acquire_profile_launch_lock(first["account_id"])
    with pytest.raises(BaleOnboardingError) as exc:
        svc.acquire_profile_launch_lock(second["account_id"])
    assert exc.value.error_code == "login_concurrency_reached"
    assert svc.release_profile_launch_lock(first["account_id"], lock["owner_id"]) is True
    assert svc.acquire_profile_launch_lock(second["account_id"])["account_id"] == second["account_id"]
