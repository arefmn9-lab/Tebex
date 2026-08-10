from __future__ import annotations

from pathlib import Path

import pytest

from modules.automation_engine.commercial_queue.service import CampaignLifecycleError
from modules.automation_engine.commercial_queue.repository import CommercialQueueRepository
from modules.automation_engine.commercial_queue.service import CommercialQueueService
from test_campaign_auto_pool_resilience import (
    _campaign,
    _no_delivery_worker,
    _service as pool_service,
    _start_with_demand,
)


def _operator_service(path: Path, *, total: int, healthy: int, concurrency: int):
    service = pool_service(path, total=total, healthy=healthy)
    service.update_global_settings({
        "concurrency_mode": "operator_defined",
        "max_concurrent_accounts": concurrency,
        "live_campaign_execution_enabled": True,
    })
    return service


def test_operator_values_round_trip_without_silent_clamp(tmp_path: Path) -> None:
    service = _operator_service(tmp_path / "settings.db", total=1, healthy=1, concurrency=1)
    for value in (1, 2, 5, 9, 20, 100):
        updated = service.update_global_settings({"max_concurrent_accounts": value})
        current = service.get_global_settings()
        assert updated["max_concurrent_accounts"] == value
        assert current["max_concurrent_accounts"] == value
        assert current["effective_browser_concurrency"] == value
        assert current["effective_worker_concurrency"] == value


def test_nine_requested_uses_exact_nine_claims_and_shared_capacity(tmp_path: Path) -> None:
    service = _operator_service(tmp_path / "exact-nine.db", total=9, healthy=9, concurrency=9)
    campaign = _campaign(service, jobs=9)
    before_start = service.campaign_capacity_state(campaign["id"])
    assert before_start["requested_account_count"] == 0

    calls: list[tuple[str, str | None]] = []
    service.run_account_round = _no_delivery_worker(calls)  # type: ignore[method-assign]
    _start_with_demand(service, campaign, 9)
    state = service.campaign_capacity_state(campaign["id"])
    assert state["requested_account_count"] == 9
    assert state["configured_runtime_concurrency"] == 9
    assert state["effective_runtime_capacity"] == 9
    assert state["available_runtime_slots"] == 9
    assert state["ready_for_exact_account_execution"] is True

    tick = service.scheduler_run_once(campaign["id"])
    assert len(tick["started_accounts"]) == 9
    assert service.repository.campaign_job_counts(campaign["id"]).get("assigned", 0) == 9
    assert service.repository.active_runtime_slots() == 9


def test_runtime_five_atomically_blocks_requested_nine_without_partial_claim(tmp_path: Path) -> None:
    service = _operator_service(tmp_path / "capacity-five.db", total=9, healthy=9, concurrency=5)
    campaign = _campaign(service, jobs=9)
    service.allocate_campaign_capacity(campaign["id"], {"requested_account_count": 9})
    service.repository.update_campaign(campaign["id"], {"status": "queued", "lifecycle_stage": "queued"})
    service.scheduler_start()

    validation = service.validate_campaign_start(campaign["id"])
    assert validation["ok"] is False
    assert "requested_accounts_exceed_runtime_capacity" in validation["blocking_reasons"]
    assert validation["requested_account_count"] == 9
    assert validation["effective_runtime_capacity"] == 5
    assert service.repository.campaign_job_counts(campaign["id"]).get("assigned", 0) == 0
    with pytest.raises(CampaignLifecycleError) as error:
        service.start_campaign(campaign["id"])
    assert error.value.error_code == "requested_accounts_exceed_runtime_capacity"
    assert service.repository.get_campaign(campaign["id"])["status"] == "queued"


def test_runtime_change_decrease_does_not_cancel_inflight_work_and_increase_preserves_demand(tmp_path: Path) -> None:
    service = _operator_service(tmp_path / "dynamic.db", total=9, healthy=9, concurrency=9)
    campaign = _campaign(service, jobs=9)
    calls: list[tuple[str, str | None]] = []
    service.run_account_round = _no_delivery_worker(calls)  # type: ignore[method-assign]
    _start_with_demand(service, campaign, 5)
    first = service.scheduler_run_once(campaign["id"])
    assert len(first["started_accounts"]) == 5
    assert service.repository.active_runtime_slots() == 5

    service.update_global_settings({"max_concurrent_accounts": 3})
    reduced = service.scheduler_run_once(campaign["id"])
    assert reduced["started_accounts"] == []
    assert service.repository.active_runtime_slots() == 5
    assert service.repository.campaign_job_counts(campaign["id"]).get("assigned", 0) == 5

    service.update_global_settings({"max_concurrent_accounts": 9})
    state = service.campaign_capacity_state(campaign["id"])
    reservation = service.repository.get_campaign_capacity_reservation(campaign["id"])
    assert state["requested_account_count"] == 5
    assert reservation["requested_account_count"] == 5
    assert state["effective_runtime_capacity"] == 9


def test_two_campaign_reservations_share_one_global_capacity_and_same_account_is_exclusive(tmp_path: Path) -> None:
    service = _operator_service(tmp_path / "multi-campaign.db", total=9, healthy=9, concurrency=9)
    first = _campaign(service, jobs=5)
    second = _campaign(service, jobs=5)
    service.allocate_campaign_capacity(first["id"], {"requested_account_count": 5})
    service.allocate_campaign_capacity(second["id"], {"requested_account_count": 4})
    service.repository.update_campaign(first["id"], {"status": "running", "lifecycle_stage": "running"})
    service.repository.update_campaign(second["id"], {"status": "running", "lifecycle_stage": "running"})
    service.repository.reserve_campaign_start_capacity(first["id"], 5, 9)
    service.repository.reserve_campaign_start_capacity(second["id"], 4, 9)

    first_claim = service.repository.claim_exact_campaign_round_atomic(
        first["id"], [f"bale_pool_{index:03d}" for index in range(5)], "source-auto-pool", max_active_account_slots=9,
    )
    second_claim = service.repository.claim_exact_campaign_round_atomic(
        second["id"], [f"bale_pool_{index:03d}" for index in range(5, 9)], "source-auto-pool", max_active_account_slots=9,
    )
    assert len(first_claim) == 5
    assert len(second_claim) == 4
    assert service.repository.active_runtime_slots() == 9
    with pytest.raises(ValueError, match="exact_round_account_already_active"):
        service.repository.claim_exact_campaign_round_atomic(
            second["id"], ["bale_pool_000"], "source-auto-pool", max_active_account_slots=9,
        )


def test_thousand_account_projection_accepts_one_hundred_without_runtime_session_probes(tmp_path: Path) -> None:
    service = CommercialQueueService(
        repository=CommercialQueueRepository(tmp_path / "thousand.db"),
        account_auth_checker=lambda _account_id: True,
        sleeper=lambda _seconds: None,
    )
    service.update_global_settings({
        "concurrency_mode": "operator_defined",
        "max_concurrent_accounts": 100,
        "default_source_channel_uid": "source-auto-pool",
    })
    readiness = [
        {
            "account_id": f"bale_scale_{index:04d}",
            "worker_eligible": index < 990,
            "enabled": True,
            "commercial_enabled": True,
            "authentication_status": "authenticated" if index < 990 else "login_required",
            "worker_status": "idle",
            "eligibility_reasons": [] if index < 990 else ["session_revalidation_required"],
        }
        for index in range(1000)
    ]
    service.account_readiness_matrix = lambda: readiness
    campaign = _campaign(service, jobs=100)
    service.runtime_session_manager.get_session = lambda _account_id: (_ for _ in ()).throw(AssertionError("idle capacity must not probe runtime sessions"))  # type: ignore[method-assign]
    state = service.allocate_campaign_capacity(campaign["id"], {"requested_account_count": 100})
    assert state["registered_account_count"] == 1000
    assert state["eligible_account_count"] == 990
    assert state["requested_account_count"] == 100
    assert state["configured_runtime_concurrency"] == 100
    assert state["effective_runtime_capacity"] == 100
    assert state["available_runtime_slots"] == 100
    assert state["ready_for_exact_account_execution"] is True


def test_staged_draft_jobs_cannot_be_claimed_before_campaign_start(tmp_path: Path) -> None:
    service = _operator_service(tmp_path / "draft-gate.db", total=1, healthy=1, concurrency=1)
    campaign = _campaign(service, jobs=1)
    job = service.repository.list_campaign_jobs_all(campaign["id"])[0]
    assigned = service.repository.assign_queued_jobs_atomic(
        "bale_pool_000", campaign["id"], 1, "source-auto-pool", max_active_account_slots=1,
    )
    assert assigned == []
    assert service.repository.get_job(job["id"])["status"] == "queued"
