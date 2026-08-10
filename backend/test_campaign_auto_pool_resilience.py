from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from modules.automation_engine.commercial_queue.repository import CommercialQueueRepository
from modules.automation_engine.commercial_queue.scheduler_runtime import CommercialSchedulerRuntime
from modules.automation_engine.commercial_queue.service import CampaignLifecycleError, CommercialQueueService


def _service(path: Path, *, total: int, healthy: int) -> CommercialQueueService:
    service = CommercialQueueService(
        repository=CommercialQueueRepository(path),
        account_auth_checker=lambda _account_id: True,
        sleeper=lambda _seconds: None,
    )
    service.update_global_settings({
        "concurrency_mode": "unrestricted",
        "operator_defined_max_concurrent_accounts": 1,
        "browser_concurrency": 1,
        "worker_concurrency": 1,
        "default_source_channel_uid": "source-auto-pool",
        "round_cooldown_seconds": 0,
        "delay_between_deliveries_seconds": 0,
        "live_campaign_execution_enabled": True,
    })
    readiness = []
    for index in range(total):
        account_id = f"bale_pool_{index:03d}"
        service.update_account_settings(account_id, {
            "enabled": True,
            "worker_status": "idle",
            "source_channel_uid_override": "source-auto-pool",
            "daily_limit_override": 1000,
        })
        is_healthy = index < healthy
        readiness.append({
            "account_id": account_id,
            "worker_eligible": is_healthy,
            "enabled": True,
            "commercial_enabled": True,
            "authentication_status": "authenticated" if is_healthy else "login_required",
            "worker_status": "idle",
            "eligibility_reasons": [] if is_healthy else ["session_revalidation_required"],
        })
        if not is_healthy:
            service.account_health.set_status(account_id, "session_error", "isolated_unhealthy_fixture")
    service.account_readiness_matrix = lambda: readiness
    return service


def _campaign(service: CommercialQueueService, *, jobs: int) -> dict:
    campaign = service.create_campaign({
        "name": "AUTO pool fixture",
        "platform": "bale",
        "status": "draft",
        "source_channel_uid": "source-auto-pool",
        # Legacy lists must remain AUTO unless a future pinned mode is explicit.
        "policy_overrides": {"eligible_account_ids": ["bale_pool_999"]},
    })
    service.import_recipients(campaign["id"], [f"0930407{index:04d}" for index in range(jobs)])
    for recipient in service.list_recipients(campaign["id"], limit=10_000)["items"]:
        authorization = {
            "recipient_origin": "user_provided",
            "synthetic_test_data": False,
            "live_execution_authorized": True,
            "live_authorized_at": "2026-08-09T00:00:00+00:00",
            "live_authorized_by": "isolated_test",
            "authorization_source": "isolated_test",
            "authorization_status": "authorized",
            "should_not_retry": False,
        }
        service.repository.update_recipient_authorization(recipient["id"], authorization)
        service.repository.update_jobs_authorization_by_recipient(recipient["id"], authorization)
    return campaign


def _start_with_demand(service: CommercialQueueService, campaign: dict, demand: int) -> dict:
    service.allocate_campaign_capacity(campaign["id"], {"requested_account_count": demand})
    service.repository.update_campaign(campaign["id"], {"status": "queued", "lifecycle_stage": "queued"})
    service.scheduler_start()
    return service.start_campaign(campaign["id"])


def _no_delivery_worker(calls: list[tuple[str, str | None]]):
    def run(account_id: str, *, preassigned_job_id: str | None = None, **_kwargs: object) -> dict:
        calls.append((account_id, preassigned_job_id))
        return {"assigned_count": 1, "processed_count": 0, "results": []}
    return run


def test_auto_pool_ignores_two_unhealthy_legacy_ids_and_claims_exact_seven(tmp_path: Path) -> None:
    service = _service(tmp_path / "nine-seven-two.db", total=9, healthy=7)
    campaign = _campaign(service, jobs=7)
    calls: list[tuple[str, str | None]] = []
    service.run_account_round = _no_delivery_worker(calls)  # type: ignore[method-assign]

    validation = service.validate_campaign_start(campaign["id"])
    assert validation["eligible_account_count"] == 7
    assert validation["account_selection_mode"] == "auto"
    assert validation["ok"] is True
    _start_with_demand(service, campaign, 7)
    tick = service.scheduler_run_once(campaign["id"])

    assert tick["initial_exact_reservation"] is True
    assert len(tick["started_accounts"]) == 7
    assert all(account.endswith(tuple(f"{index:03d}" for index in range(7))) for account in tick["started_accounts"])
    assert all(job_id for _account_id, job_id in calls)
    assert service.repository.campaign_job_counts(campaign["id"]).get("assigned", 0) == 7


def test_insufficient_six_of_seven_is_atomic_and_reports_requested_and_eligible(tmp_path: Path) -> None:
    service = _service(tmp_path / "nine-six-two.db", total=9, healthy=6)
    campaign = _campaign(service, jobs=7)
    with pytest.raises(CampaignLifecycleError) as exc_info:
        service.allocate_campaign_capacity(campaign["id"], {"requested_account_count": 7})
    error = exc_info.value
    assert error.error_code == "requested_accounts_exceed_eligible"
    assert error.summary["requested_account_count"] == 7
    assert error.summary["eligible_account_count"] == 6
    assert service.repository.get_campaign_capacity_reservation(campaign["id"]) is None
    assert service.repository.campaign_job_counts(campaign["id"]).get("assigned", 0) == 0


def test_large_auto_pool_claims_twenty_without_considering_ten_unhealthy(tmp_path: Path) -> None:
    # The pool projection is deliberately 500 rows; only the 20 claim targets
    # need durable settings for this isolated scheduler test.
    service = _service(tmp_path / "five-hundred.db", total=20, healthy=20)
    pool_rows = [
        {
            "account_id": f"bale_large_{index:03d}",
            "worker_eligible": index < 490,
            "enabled": True,
            "commercial_enabled": True,
            "authentication_status": "authenticated" if index < 490 else "login_required",
            "worker_status": "idle",
            "eligibility_reasons": [] if index < 490 else ["session_revalidation_required"],
        }
        for index in range(500)
    ]
    service.account_readiness_matrix = lambda: pool_rows
    eligible = [{"account_id": f"bale_large_{index:03d}"} for index in range(490)]
    service._ordered_eligible_accounts = lambda _campaign_id=None: (
        eligible,
        {"disabled": [], "cooling_down": [], "daily_limited": [], "locked": [], "ineligible": [f"bale_large_{index:03d}:session_error" for index in range(490, 500)]},
    )
    service.worker_status = lambda _account_id: {"worker_status": "idle", "cooldown_until": None}  # type: ignore[method-assign]
    campaign = _campaign(service, jobs=20)
    calls: list[tuple[str, str | None]] = []
    service.run_account_round = _no_delivery_worker(calls)  # type: ignore[method-assign]

    _start_with_demand(service, campaign, 20)
    tick = service.scheduler_run_once(campaign["id"])

    assert len(tick["started_accounts"]) == 20
    assert tick["desired_concurrency"] == 20
    assert tick["replacement_needed"] == 0
    assert all(int(account.rsplit("_", 1)[1]) < 490 for account in tick["started_accounts"])


def test_runtime_failure_requeues_and_late_binds_spare_without_changing_demand(tmp_path: Path) -> None:
    service = _service(tmp_path / "replacement.db", total=8, healthy=8)
    campaign = _campaign(service, jobs=9)
    first_calls: list[tuple[str, str | None]] = []

    def first_round(account_id: str, *, preassigned_job_id: str | None = None, **_kwargs: object) -> dict:
        first_calls.append((account_id, preassigned_job_id))
        if account_id == "bale_pool_000":
            service.account_health.set_status(account_id, "session_error", "isolated_pre_send_failure")
            service.repository.requeue_assigned_jobs_for_account(account_id, reason="isolated_pre_send_failure")
            return {"assigned_count": 1, "processed_count": 0, "results": [{"status": "failed"}], "reason": "pre_send_failure"}
        return {"assigned_count": 1, "processed_count": 0, "results": []}

    service.run_account_round = first_round  # type: ignore[method-assign]
    _start_with_demand(service, campaign, 7)
    first = service.scheduler_run_once(campaign["id"])
    assert len(first["started_accounts"]) == 7
    assert service.repository.campaign_job_counts(campaign["id"]).get("queued", 0) == 3

    second_calls: list[tuple[str, str | None]] = []
    service.run_account_round = _no_delivery_worker(second_calls)  # type: ignore[method-assign]
    service.update_global_settings({"account_assignment_strategy": "round_robin"})
    service.repository.update_scheduler_state({"round_robin_cursor": "bale_pool_006"})
    service.resource_provider.decide = lambda *_args, **_kwargs: SimpleNamespace(  # type: ignore[method-assign]
        allow_new_worker=True, available_worker_slots=7, available_browser_start_slots=7,
        reason_codes=[], retry_after_seconds=0,
    )
    second = service.scheduler_run_once(campaign["id"])
    assert "bale_pool_000" not in second["started_accounts"]
    assert "bale_pool_007" in second["started_accounts"]
    assert second["desired_concurrency"] == 7


def test_runtime_failure_without_spare_is_degraded_not_campaign_wide(tmp_path: Path) -> None:
    service = _service(tmp_path / "no-spare.db", total=7, healthy=7)
    campaign = _campaign(service, jobs=8)
    service.run_account_round = _no_delivery_worker([])  # type: ignore[method-assign]
    _start_with_demand(service, campaign, 7)
    service.scheduler_run_once(campaign["id"])
    service.account_health.set_status("bale_pool_000", "session_error", "isolated_session_loss")
    service.resource_provider.decide = lambda *_args, **_kwargs: SimpleNamespace(  # type: ignore[method-assign]
        allow_new_worker=True, available_worker_slots=6, available_browser_start_slots=6,
        reason_codes=[], retry_after_seconds=0,
    )
    tick = service.scheduler_run_once(campaign["id"])

    assert tick["desired_concurrency"] == 7
    assert tick["active_concurrency"] == 6
    assert tick["replacement_needed"] == 1
    assert service.repository.get_campaign(campaign["id"])["status"] == "running"


def test_runtime_restart_marks_historical_snapshot_and_emits_current_heartbeat(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = _service(tmp_path / "runtime-history.db", total=1, healthy=1)
        service.scheduler_start()
        service.repository.update_scheduler_state({
            "runtime_owner_id": "scheduler_dead",
            "last_tick_at": "2026-08-01T00:00:00+00:00",
            "loop_heartbeat_at": "2026-08-01T00:00:00+00:00",
        })
        runtime = CommercialSchedulerRuntime(service, loop_interval_seconds=1)
        await runtime.start()
        try:
            await asyncio.sleep(1.1)
            status = service.scheduler_status()
            assert status["current_runtime_owner"] == runtime.owner_id
            assert status["current_runtime_owner_matches_persisted"] is True
            assert status["current_heartbeat"] is not None
            assert status["first_current_runtime_tick_at"] is not None
            assert status["historical_runtime_owner"] == "scheduler_dead"
            assert status["historical_last_tick_at"] == "2026-08-01T00:00:00+00:00"
            assert service.repository.list_campaigns("running", 10, 0) == []
        finally:
            await runtime.stop()

    asyncio.run(scenario())
