from __future__ import annotations

import os
from pathlib import Path

import pytest

from modules.automation_engine.commercial_queue.repository import CommercialQueueRepository
from modules.automation_engine.commercial_queue.service import CommercialQueueService


def _service(database: Path, account_count: int = 9) -> tuple[CommercialQueueService, list[str]]:
    assert database.name != "clinicos.db"
    os.environ["CLINICOS_DISABLE_SCHEDULER_RUNTIME"] = "1"
    service = CommercialQueueService(
        repository=CommercialQueueRepository(database),
        account_auth_checker=lambda _account_id: True,
        sleeper=lambda _seconds: None,
    )
    account_ids = [f"bale_exact_{index}" for index in range(account_count)]
    rows = [{"account_id": value, "worker_eligible": True, "worker_status": "idle"} for value in account_ids]
    service.account_readiness_matrix = lambda: rows
    service.update_global_settings({
        "concurrency_mode": "operator_defined",
        "max_concurrent_accounts": account_count,
        "operator_defined_max_concurrent_accounts": account_count,
        "browser_concurrency": account_count,
        "worker_concurrency": account_count,
        "accounts_per_round": account_count,
        "default_source_channel_uid": "source-test",
        "live_campaign_execution_enabled": True,
    })
    for account_id in account_ids:
        service.repository.upsert_account_settings(account_id, {
            "enabled": True, "worker_status": "idle", "source_channel_uid_override": "source-test",
            "daily_limit_override": 100, "commercial_enabled": True,
        })
    return service, account_ids


def _campaign_with_jobs(service: CommercialQueueService, count: int) -> dict:
    campaign = service.create_campaign({
        "name": "Exact generic N", "platform": "bale", "status": "running", "lifecycle_stage": "initializing_capacity", "source_channel_uid": "source-test",
        "policy_overrides": {"accounts_per_round": count, "max_concurrent_accounts": count, "operator_defined_max_concurrent_accounts": count, "browser_concurrency": count, "worker_concurrency": count},
    })
    service.repository.upsert_campaign_capacity_reservation(campaign["id"], count, count)
    service.import_recipients(campaign["id"], [f"0930408{index:04d}" for index in range(count)])
    for recipient in service.list_recipients(campaign["id"], limit=100)["items"]:
        service.repository.update_recipient_authorization(recipient["id"], {
            "recipient_origin": "user_provided", "synthetic_test_data": False,
            "live_execution_authorized": True, "live_authorized_at": "2026-08-03T00:00:00+00:00",
            "live_authorized_by": "isolated_test", "authorization_source": "isolated_test",
            "authorization_status": "authorized", "should_not_retry": False,
        })
    return campaign


def test_atomic_nine_claims_distinct_jobs_before_worker_launch(tmp_path: Path) -> None:
    service, account_ids = _service(tmp_path / "exact-nine.db")
    campaign = _campaign_with_jobs(service, 9)
    eligible = [{"account_id": value} for value in account_ids]
    groups = {"disabled": [], "cooling_down": [], "daily_limited": [], "locked": [], "ineligible": []}
    service._ordered_eligible_accounts = lambda _campaign_id=None: (eligible, groups)
    launches: list[tuple[str, str]] = []
    service.run_account_round = lambda account_id, campaign_id=None, preassigned_job_id=None, **_kwargs: launches.append((account_id, preassigned_job_id)) or {"assigned_count": 1, "processed_count": 0, "results": []}
    service.scheduler_start()
    result = service.scheduler_run_once(campaign["id"])
    assert len(result["started_accounts"]) == 9
    assert len({account for account, _job in launches}) == 9
    assert len({job for _account, job in launches}) == 9
    assert all(job for _account, job in launches)


def test_partial_exact_claim_rolls_back_and_launches_none(tmp_path: Path) -> None:
    service, account_ids = _service(tmp_path / "exact-rollback.db")
    campaign = _campaign_with_jobs(service, 9)
    with service.repository.connection() as connection:
        connection.execute("UPDATE commercial_delivery_jobs SET should_not_retry=1 WHERE id=(SELECT id FROM commercial_delivery_jobs WHERE campaign_id=? LIMIT 1)", (campaign["id"],))
        connection.commit()
    eligible = [{"account_id": value} for value in account_ids]
    groups = {"disabled": [], "cooling_down": [], "daily_limited": [], "locked": [], "ineligible": []}
    service._ordered_eligible_accounts = lambda _campaign_id=None: (eligible, groups)
    launches: list[str] = []
    service.run_account_round = lambda account_id, **_kwargs: launches.append(account_id)
    service.scheduler_start()
    result = service.scheduler_run_once(campaign["id"])
    assert result["reason"] == "exact_round_insufficient_claimable_jobs"
    assert launches == []
    assert service.repository.campaign_job_counts(campaign["id"]).get("assigned", 0) == 0


def test_exact_readiness_reports_two_unavailable_accounts(tmp_path: Path) -> None:
    service, account_ids = _service(tmp_path / "exact-readiness.db", account_count=9)
    unavailable = account_ids[-2:]
    rows = [
        {"account_id": value, "eligible": value not in unavailable, "blockers": [] if value not in unavailable else ["identity_verification_required"]}
        for value in account_ids
    ]
    readiness = service.exact_execution_requirements(9, rows, 139, {"accounts_per_round": 9, "max_concurrent_accounts": 9, "browser_concurrency": 9, "worker_concurrency": 9})
    assert readiness["eligible_account_count"] == 7
    assert [row["account_id"] for row in readiness["unavailable_accounts"]] == unavailable
    assert readiness["blocking_reasons"] == ["requested_accounts_exceed_eligible"]
    assert readiness["ready"] is False


def test_production_code_has_no_private_playwright_access() -> None:
    backend = Path(__file__).resolve().parent / "modules"
    offenders = [path for path in backend.rglob("*.py") if "._playwright" in path.read_text(encoding="utf-8")]
    assert offenders == []
