from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

os.environ["CLINICOS_DISABLE_SCHEDULER_RUNTIME"] = "1"
os.environ["CLINICOS_DISABLE_AUTH_MAINTENANCE_RUNTIME"] = "1"

from modules.automation_engine.account_registry.bale_onboarding import BaleOnboardingService
from modules.automation_engine.commercial_queue.repository import CommercialQueueRepository
from modules.automation_engine.commercial_queue.scheduler_runtime import CommercialSchedulerRuntime
from modules.automation_engine.commercial_queue.service import CommercialQueueService


class EmptyAccountStore:
    def list_accounts(self):
        return []

    def get_account(self, _account_id):
        return None


PERSISTED_TABLES = (
    "commercial_campaigns",
    "commercial_campaign_capacity_reservations",
    "commercial_delivery_jobs",
    "commercial_global_settings",
    "commercial_account_settings",
    "bale_operational_accounts",
)


def snapshot(path: Path) -> dict[str, list[dict]]:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return {
            table: [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")]
            for table in PERSISTED_TABLES
        }
    finally:
        connection.close()


def fixture_database(path: Path, profile_root: Path) -> None:
    onboarding = BaleOnboardingService(path, EmptyAccountStore(), profile_root, process_inspector=lambda _record: [])
    onboarding._ensure_schema()
    repository = CommercialQueueRepository(path)
    campaign = repository.create_campaign({
        "id": "campaign_persisted", "name": "persisted", "platform": "bale",
        "status": "paused", "lifecycle_stage": "paused",
        "policy_overrides": {"accounts_per_round": 9, "operation_delay_seconds": 4},
    })
    repository.upsert_campaign_capacity_reservation(campaign["id"], 9, 9)
    CommercialQueueService(repository=repository).update_global_settings({
        "operator_defined_max_concurrent_accounts": 9,
        "browser_concurrency": 9,
        "worker_concurrency": 9,
    })
    repository.upsert_account_settings("bale_persisted", {"enabled": True, "worker_status": "idle"})
    now = "2026-08-03T00:00:00+00:00"
    profile = profile_root / "bale_persisted"
    profile.mkdir(parents=True)
    with repository.connection() as connection:
        connection.execute(
            """INSERT INTO bale_operational_accounts(
            account_id,normalized_identifier,masked_identifier,lifecycle_status,scheduling_enabled,
            authentication_status,session_persistence_status,health_status,canonical_profile_path,
            normalized_profile_path,onboarding_blocked,created_at,updated_at,audit_metadata_json,
            identity_bound_phone,identity_verification_status,identity_verified_at,session_status,
            profile_generation_id,identity_profile_generation_id,profile_bound_identity,
            onboarding_completed,profile_persistence_verified)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "bale_persisted", "09120000000", "091******00", "ready", 1,
                "authenticated", "verified", "healthy", str(profile), str(profile).lower(),
                0, now, now, "{}", "09120000000", "verified", now, "authenticated",
                "profile_generation_1", "profile_generation_1", "09120000000", 1, 1,
            ),
        )
        connection.commit()


def test_ten_backend_restarts_preserve_all_persisted_state(tmp_path: Path):
    path = tmp_path / "production-equivalent.db"
    profiles = tmp_path / "profiles"
    fixture_database(path, profiles)
    before = snapshot(path)
    for _ in range(10):
        onboarding = BaleOnboardingService(path, EmptyAccountStore(), profiles, process_inspector=lambda _record: [])
        assert onboarding.cleanup_proven_stale_locks()["released_count"] == 0
        service = CommercialQueueService(database_path=path)
        runtime = CommercialSchedulerRuntime(service, loop_interval_seconds=60)

        async def cycle():
            await runtime.start()
            await runtime.stop()

        asyncio.run(cycle())
        assert snapshot(path) == before
    after = snapshot(path)
    assert after == before
    account = after["bale_operational_accounts"][0]
    assert account["onboarding_completed"] == 1
    assert account["identity_verification_status"] == "verified"
    assert account["profile_generation_id"] == "profile_generation_1"
    assert after["commercial_campaign_capacity_reservations"][0]["allocated_account_count"] == 9


def test_ten_real_fastapi_lifespans_are_read_only(tmp_path: Path):
    path = tmp_path / "production-equivalent-fastapi.db"
    profiles = tmp_path / "profiles-fastapi"
    fixture_database(path, profiles)
    before = snapshot(path)
    environment = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).parent),
        "CLINICOS_AUTOMATION_DATABASE_PATH": str(path),
        "CLINICOS_BALE_PROFILE_ROOT": str(profiles),
        "CLINICOS_DISABLE_SCHEDULER_RUNTIME": "1",
        "CLINICOS_DISABLE_AUTH_MAINTENANCE_RUNTIME": "1",
    }
    command = [
        sys.executable,
        "-c",
        "from fastapi.testclient import TestClient; from app.main import app; "
        "c=TestClient(app); c.__enter__(); assert c.get('/health/live').status_code==200; c.__exit__(None,None,None)",
    ]
    for _ in range(10):
        completed = subprocess.run(command, cwd=Path(__file__).parent, env=environment, capture_output=True, text=True, timeout=30)
        assert completed.returncode == 0, completed.stderr
        assert snapshot(path) == before


def test_read_helpers_do_not_create_missing_settings(tmp_path: Path):
    path = tmp_path / "read-only-settings.db"
    profiles = tmp_path / "profiles"
    BaleOnboardingService(path, EmptyAccountStore(), profiles, process_inspector=lambda _record: [])._ensure_schema()
    service = CommercialQueueService(database_path=path)
    before = snapshot(path)
    assert service.get_global_settings()["persisted"] is False
    assert service.get_account_settings("missing")["persisted"] is False
    assert snapshot(path) == before


def test_campaign_mutations_return_operator_before_after_audit(tmp_path: Path):
    path = tmp_path / "audit.db"
    BaleOnboardingService(path, EmptyAccountStore(), tmp_path / "profiles", process_inspector=lambda _record: [])._ensure_schema()
    service = CommercialQueueService(database_path=path)
    created = service.create_campaign({"name": "explicit", "platform": "bale"})
    audit = created["mutation_audit"]
    assert audit["actor"] == "operator"
    assert audit["action"] == "create_campaign"
    assert audit["before"] is None
    assert audit["after"]["id"] == created["id"]
