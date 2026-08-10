from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from modules.automation_engine.commercial_queue.repository import CommercialQueueRepository
from modules.automation_engine.commercial_queue.service import (
    DETERMINISTIC_UI_FAILURES,
    CommercialQueueService,
    classify_ui_failure_step,
    deepest_execution_evidence,
)
from modules.automation_engine.db.database import DATABASE_PATH


def _service(tmp_path: Path) -> CommercialQueueService:
    database = tmp_path / "campaign-observability.db"
    assert database.resolve() != Path(DATABASE_PATH).resolve()
    return CommercialQueueService(
        repository=CommercialQueueRepository(database),
        orchestrator=lambda **_payload: pytest.fail("test must not invoke a delivery adapter"),
        account_auth_checker=lambda _account_id: True,
        sleeper=lambda _seconds: None,
    )


def _queued_campaign(service: CommercialQueueService, count: int) -> str:
    service.update_account_settings("bale_capacity_fixture", {
        "enabled": True, "commercial_enabled": True, "daily_limit_override": count + 10,
        "current_daily_sent_count": 0, "worker_status": "idle",
    })
    campaign = service.create_campaign({
        "name": "isolated assignment test", "platform": "bale", "status": "running",
        "source_channel_uid": "test-source", "capacity_reservation": count,
    })
    service.import_recipients(campaign["id"], [f"0935000{index:04d}" for index in range(count)])
    for recipient in service.list_recipients(campaign["id"], limit=count + 10)["items"]:
        service.repository.update_recipient_authorization(recipient["id"], {
            "recipient_origin": "user_provided", "synthetic_test_data": False,
            "live_execution_authorized": True, "live_authorized_at": "2026-08-02T00:00:00+00:00",
            "live_authorized_by": "isolated_test", "authorization_source": "test_fixture",
            "authorization_status": "authorized", "should_not_retry": False,
        })
    return str(campaign["id"])


@pytest.mark.parametrize("account_count", [1, 5, 40])
def test_each_account_can_claim_exactly_one_job(tmp_path: Path, account_count: int) -> None:
    service = _service(tmp_path)
    campaign_id = _queued_campaign(service, account_count + 3)
    claimed = []
    for index in range(account_count):
        account_id = f"bale_test_{index:03d}"
        first = service.repository.assign_queued_jobs_atomic(account_id, campaign_id, 3, "test-source")
        second = service.repository.assign_queued_jobs_atomic(account_id, campaign_id, 3, "test-source")
        assert len(first) == 1
        assert second == []
        claimed.extend(first)
    assert len(claimed) == account_count
    assert len({row["account_id"] for row in claimed}) == account_count
    assert all(value == 1 for value in service.repository.active_job_counts_by_account().values())


def test_partial_unique_index_blocks_overlapping_tick_assignment(tmp_path: Path) -> None:
    service = _service(tmp_path)
    campaign_id = _queued_campaign(service, 3)
    jobs = service.repository.list_jobs(None, None, campaign_id, 10, 0)
    now = "2026-08-02T00:00:00+00:00"
    with service.repository.connection() as connection:
        connection.execute("UPDATE commercial_delivery_jobs SET account_id='bale_one', status='assigned', claimed_at=? WHERE id=?", (now, jobs[0]["id"]))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE commercial_delivery_jobs SET account_id='bale_one', status='running', claimed_at=? WHERE id=?", (now, jobs[1]["id"]))


def test_deepest_plugin_failure_reaches_public_fields() -> None:
    result = {
        "success": False, "error_code": "element_not_found",
        "scenario_result": {
            "error_code": "recipient_result_selector_missing",
            "error_message": "Recipient result row did not appear",
            "failed_step": "wait_recipient_results",
            "records": {"source_url": "https://web.bale.ai/chat?uid=safe"},
            "steps": [
                {"step_id": "type_recipient_name", "status": "success", "details": {}},
                {"step_id": "wait_recipient_results", "status": "failed", "error_code": "recipient_result_selector_missing", "message": "Recipient result row did not appear", "details": {"selector": ".result-row"}},
            ],
        },
    }
    evidence = deepest_execution_evidence(result)
    assert evidence == evidence | {
        "nested_error_code": "recipient_result_selector_missing",
        "nested_error_message": "Recipient result row did not appear",
        "failed_step": "wait_recipient_results",
        "last_successful_step": "type_recipient_name",
        "selector": ".result-row",
        "page_url": "https://web.bale.ai/chat?uid=safe",
        "failure_class": "recipient_selection_failure",
    }


def test_deterministic_failure_classification_is_account_scoped() -> None:
    failing = classify_ui_failure_step("wait_recipient_results")
    unrelated = classify_ui_failure_step("unknown_transport_timeout")
    assert failing == "recipient_selection_failure"
    assert failing in DETERMINISTIC_UI_FAILURES
    assert unrelated is None


def test_scheduler_diagnostics_use_persisted_worker_and_browser_evidence(tmp_path: Path) -> None:
    service = _service(tmp_path)
    campaign_id = _queued_campaign(service, 1)
    job = service.repository.list_jobs(None, None, campaign_id, 1, 0)[0]
    common = {"job_id": job["id"], "campaign_id": campaign_id, "account_id": "bale_test",
              "recipient_id": job["recipient_id"], "worker_round_id": "round_test", "status": "running"}
    service.repository.create_job_event({**common, "event_type": "job_started", "step_name": "job_started"})
    service.repository.create_job_event({
        **common, "event_type": "execution_step", "step_name": "browser_started", "status": "succeeded",
        "diagnostics": {"browser_pid": 4242, "screenshot_path": "runtime/safe-failure.png"},
    })
    status = service.scheduler_status()
    assert status["latest_campaign_worker_started"] is True
    assert status["latest_campaign_browser_started"] is True
    assert status["latest_browser_pids"] == [4242]
    assert status["latest_screenshots"] == ["runtime/safe-failure.png"]


def test_queue_arithmetic_is_exact_and_no_adapter_runs(tmp_path: Path) -> None:
    service = _service(tmp_path)
    campaign_id = _queued_campaign(service, 6)
    before = service.repository.count_jobs_by_status()
    assigned = service.repository.assign_queued_jobs_atomic("bale_test", campaign_id, 3, "test-source")
    after = service.repository.count_jobs_by_status()
    assert len(assigned) == 1
    assert before["queued"] - after["queued"] == 1
    assert after["assigned"] == 1
    assert after.get("running", 0) == 0
