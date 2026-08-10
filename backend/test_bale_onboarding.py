from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from modules.automation_engine.account_registry.bale_onboarding import BaleOnboardingError, BaleOnboardingService
from modules.automation_engine.plugins.bale.account_store import BaleAccountStore

def observed_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    svc.reconcile_profile_identity_probe(account["account_id"], identity_status="match")
    assert svc.scheduler_authentication_available(account["account_id"]) is True


def test_successful_login_verification_syncs_operational_worker_state(tmp_path: Path) -> None:
    svc = service(tmp_path)
    account = provision(svc, "09123333334", "verified-login")["account"]
    svc.record_authentication_open(
        account["account_id"],
        {"maintenance_session_id": "verified-login-session", "runtime_session_id": "runtime-login"},
        purpose="login",
    )

    updated = svc.record_authentication_verified("verified-login-session", {"verified": True})

    assert updated["authentication_status"] == "authenticated"
    assert updated["session_persistence_status"] == "verified"
    assert updated["lifecycle_status"] == "ready"
    assert svc.scheduler_authentication_available(account["account_id"]) is True


def test_failed_login_verification_remains_blocked_from_worker(tmp_path: Path) -> None:
    svc = service(tmp_path)
    account = provision(svc, "09123333335", "failed-login")["account"]
    svc.record_authentication_open(
        account["account_id"],
        {"maintenance_session_id": "failed-login-session", "runtime_session_id": "runtime-login"},
        purpose="login",
    )

    updated = svc.record_authentication_verified(
        "failed-login-session",
        {"verified": False, "auth": {"error_code": "authentication_required"}},
    )

    assert updated["authentication_status"] == "unverified"
    assert updated["lifecycle_status"] == "login_in_progress"
    assert updated["session_status"] == "temporarily_inconclusive"
    assert updated["durable_identity_verified"] is False
    assert updated["profile_probe_result"] == "auth_probe_inconclusive"
    assert svc.scheduler_authentication_available(account["account_id"]) is False


@pytest.mark.parametrize(
    ("auth_state", "expected_status", "expected_probe"),
    [
        ("login_required", "login_required", "logged_out"),
        ("verification_code_required", "otp_required", "otp_required"),
        ("unknown_auth_state", "unverified", "auth_probe_inconclusive"),
    ],
)
def test_unsuccessful_visible_probe_never_updates_verification_timestamp(tmp_path: Path, auth_state: str, expected_status: str, expected_probe: str) -> None:
    svc = service(tmp_path)
    account = provision(svc, "09123333336", f"probe-{auth_state}")["account"]
    session_id = f"session-{auth_state}"
    svc.record_authentication_open(account["account_id"], {"maintenance_session_id": session_id, "runtime_session_id": "runtime"})
    updated = svc.record_authentication_verified(session_id, {"verified": False, "auth": {"auth_state": auth_state}})
    assert updated["authentication_status"] == expected_status
    assert updated["profile_probe_result"] == expected_probe
    assert updated["authentication_verified_at"] is None
    assert updated["verification_expires_at"] is None
    assert svc.scheduler_authentication_available(account["account_id"]) is False


def test_fresh_visible_probe_persists_source_and_ttl(tmp_path: Path) -> None:
    svc = service(tmp_path)
    account = provision(svc, "09123333337", "fresh-visible-probe")["account"]
    observed_at = observed_now()
    updated = svc.reconcile_profile_probe(account["account_id"], authenticated=True, observed_at=observed_at)
    assert updated["authentication_verified_at"] == observed_at
    assert updated["verification_source"] == "visible_profile_probe"
    assert datetime.fromisoformat(updated["verification_expires_at"]) > datetime.fromisoformat(observed_at)
    assert svc.scheduler_authentication_available(account["account_id"]) is True


def test_no_fixed_account_slots_in_serialized_state(tmp_path: Path) -> None:
    svc = service(tmp_path)
    provision(svc, "09124444444", "dynamic")
    encoded = json.dumps(svc.list_accounts())
    assert "account_1" not in encoded
    assert "account_8" not in encoded


def test_multiple_accounts_can_acquire_profile_locks_simultaneously(tmp_path: Path) -> None:
    svc = service(tmp_path)
    first = provision(svc, "09125555555", "lock-one")["account"]
    second = provision(svc, "09126666666", "lock-two")["account"]
    third = provision(svc, "09127777777", "lock-three")["account"]

    locks = [
        svc.acquire_profile_launch_lock(first["account_id"]),
        svc.acquire_profile_launch_lock(second["account_id"]),
        svc.acquire_profile_launch_lock(third["account_id"]),
    ]

    assert [lock["account_id"] for lock in locks] == [
        first["account_id"],
        second["account_id"],
        third["account_id"],
    ]
    assert len({lock["normalized_profile_path"] for lock in locks}) == 3


def test_duplicate_account_profile_lock_is_rejected(tmp_path: Path) -> None:
    svc = service(tmp_path)
    account = provision(svc, "09128888888", "lock-duplicate")["account"]
    svc.acquire_profile_launch_lock(account["account_id"])

    with pytest.raises(BaleOnboardingError) as exc:
        svc.acquire_profile_launch_lock(account["account_id"])

    assert exc.value.error_code == "profile_launch_locked"


def test_releasing_one_account_lock_does_not_affect_another(tmp_path: Path) -> None:
    svc = service(tmp_path)
    first = provision(svc, "09129999999", "lock-release-one")["account"]
    second = provision(svc, "09120000000", "lock-release-two")["account"]
    first_lock = svc.acquire_profile_launch_lock(first["account_id"])
    svc.acquire_profile_launch_lock(second["account_id"])

    assert svc.release_profile_launch_lock(first["account_id"], first_lock["owner_id"]) is True
    assert svc.acquire_profile_launch_lock(first["account_id"])["account_id"] == first["account_id"]
    with pytest.raises(BaleOnboardingError) as exc:
        svc.acquire_profile_launch_lock(second["account_id"])
    assert exc.value.error_code == "profile_launch_locked"


def test_stale_authentication_open_releases_session_and_profile_lock(tmp_path: Path) -> None:
    svc = service(tmp_path)
    account = provision(svc, "09124444444", "stale-auth")["account"]
    account_id = account["account_id"]
    svc.acquire_profile_launch_lock(account_id)
    svc.record_authentication_open(
        account_id,
        {
            "maintenance_session_id": "stale-maintenance-session",
            "runtime_session_id": "stale-runtime-session",
            "auth": {"auth_state": "unknown_auth_state"},
        },
    )

    recovered = svc.recover_stale_authentication_open(account_id, force=True)

    assert recovered["recovered"] is True
    assert recovered["profile_launch_lock_released"] is True
    assert svc.acquire_profile_launch_lock(account_id)["account_id"] == account_id
    with svc.connection() as connection:
        row = connection.execute(
            "SELECT status FROM bale_maintenance_sessions WHERE maintenance_session_id='stale-maintenance-session'"
        ).fetchone()
    assert row["status"] == "stale_released"


def test_existing_authenticated_profile_reconciles_to_ready(tmp_path: Path) -> None:
    svc = service(tmp_path)
    account_id = provision(svc, "09211690533", "profile-ready")["account"]["account_id"]
    account = svc.reconcile_profile_probe(account_id, authenticated=True, observed_at=observed_now())
    account = svc.set_scheduling(account_id, True)
    assert account["authentication_status"] == "authenticated"
    assert account["session_persistence_status"] == "verified"
    assert account["lifecycle_status"] == "ready"
    assert account["health_status"] == "healthy"
    assert account["profile_probe_result"] == "authenticated"
    assert account["scheduling_enabled"] is True


def test_completed_auth_session_uses_persisted_state_after_runtime_disappears(tmp_path: Path) -> None:
    svc = service(tmp_path)
    account_id = provision(svc, "09211690533", "completed-session")["account"]["account_id"]
    session_id = "completed-auth-session"
    svc.record_authentication_open(account_id, {
        "maintenance_session_id": session_id,
        "runtime_session_id": "runtime-finished",
        "last_checked_at": observed_now(),
        "auth": {"auth_state": "authenticated", "authenticated": True},
    })
    svc.record_authentication_verified(session_id, {"verified": True, "last_checked_at": observed_now(), "auth": {"auth_state": "authenticated", "authenticated": True}})
    status = svc.persisted_authentication_status(session_id)
    assert status is not None
    assert status["closed"] is True
    assert status["completed"] is True
    assert status["auth"] == {"auth_state": "authenticated", "authenticated": True}


def test_stale_unknown_poll_cannot_overwrite_newer_verified_profile_probe(tmp_path: Path) -> None:
    svc = service(tmp_path)
    account_id = provision(svc, "09211690533", "stale-poll")["account"]["account_id"]
    session_id = "stale-auth-session"
    svc.record_authentication_open(account_id, {
        "maintenance_session_id": session_id,
        "runtime_session_id": "runtime-finished",
        "last_checked_at": observed_now(),
        "auth": {"auth_state": "authenticated", "authenticated": True},
    })
    svc.record_authentication_verified(session_id, {"verified": True, "last_checked_at": observed_now(), "auth": {"auth_state": "authenticated", "authenticated": True}})
    returned = svc.record_authentication_status(session_id, {
        "maintenance_session_id": session_id,
        "last_checked_at": "2020-07-31T07:29:00+00:00",
        "closed": True,
        "auth": {"auth_state": "unknown_auth_state", "authenticated": False},
    })
    account = svc.get_account(account_id)
    assert returned["auth"]["auth_state"] == "authenticated"
    assert account is not None
    assert account["authentication_status"] == "authenticated"
    assert account["lifecycle_status"] == "ready"


def test_service_reload_preserves_profile_readiness_and_real_logout_revokes_it(tmp_path: Path) -> None:
    svc = service(tmp_path)
    account_id = provision(svc, "09211690533", "reload-ready")["account"]["account_id"]
    svc.reconcile_profile_probe(account_id, authenticated=True, observed_at=observed_now())
    svc.set_scheduling(account_id, True)
    reloaded = BaleOnboardingService(
        database_path=svc.database_path,
        account_store=svc.account_store,
        profile_root=svc.profile_root,
        process_inspector=lambda _record: [],
        auth_ttl_seconds=3600,
    )
    ready = reloaded.get_account(account_id)
    assert ready is not None
    assert ready["authentication_status"] == "authenticated"
    assert ready["session_persistence_status"] == "verified"
    assert ready["lifecycle_status"] == "ready"
    logged_out = reloaded.reconcile_profile_probe(account_id, authenticated=False, observed_at=observed_now())
    assert logged_out["authentication_status"] == "unverified"
    assert logged_out["lifecycle_status"] == "login_required"
    assert logged_out["health_status"] == "auth_required"
    assert logged_out["profile_probe_result"] == "logged_out"
