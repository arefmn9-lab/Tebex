from __future__ import annotations

import tempfile
from pathlib import Path

from modules.automation_engine.commercial_queue.repository import CommercialQueueRepository
from modules.automation_engine.commercial_queue.service import CampaignLifecycleError, CommercialQueueService


class OrchestratorStub:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, **payload: object) -> dict:
        self.calls.append(dict(payload))
        return {
            "success": True,
            "forward_verified": False if payload.get("dry_run") else True,
            "diagnostics_consistent": True,
            "verified_forwarded_recipient_count": 0 if payload.get("dry_run") else 1,
            "confirm_click_count": 0 if payload.get("dry_run") else 1,
            "dry_run": bool(payload.get("dry_run")),
        }


def _service(path: Path, orchestrator: OrchestratorStub | None = None, auth_ok: bool = True) -> CommercialQueueService:
    service = CommercialQueueService(
        repository=CommercialQueueRepository(path),
        orchestrator=orchestrator or OrchestratorStub(),
        account_auth_checker=lambda account_id: auth_ok,
        sleeper=lambda seconds: None,
    )
    service.update_global_settings(
        {
            "max_concurrent_accounts": 2,
            "deliveries_per_account_round": 3,
            "default_daily_limit_per_account": 10,
            "default_source_channel_uid": "5613544284",
            "delay_between_deliveries_seconds": 0,
            "round_cooldown_seconds": 0,
        }
    )
    service.update_account_settings("bale_a", {"enabled": True, "source_channel_uid_override": "5613544284", "deliveries_per_round_override": 3})
    return service


def _campaign(service: CommercialQueueService, count: int = 3, status: str = "draft") -> dict:
    campaign = service.create_campaign({"name": "Lifecycle", "platform": "bale", "status": status, "source_channel_uid": "5613544284"})
    if count:
        service.import_recipients(campaign["id"], [f"093040735{index:02d}" for index in range(count)], "test")
        for recipient in service.list_recipients(campaign["id"], limit=100)["items"]:
            service.repository.update_recipient_authorization(
                recipient["id"],
                {
                    "recipient_origin": "user_provided",
                    "synthetic_test_data": False,
                    "live_execution_authorized": True,
                    "live_authorized_at": "2026-07-12T00:00:00+00:00",
                    "live_authorized_by": "test",
                    "authorization_source": "test_fixture",
                    "authorization_note": "mock recipient",
                    "authorization_status": "authorized",
                    "should_not_retry": False,
                },
            )
    return campaign


def _running_campaign(service: CommercialQueueService, count: int = 3) -> dict:
    campaign = _campaign(service, count=count)
    service.queue_campaign(campaign["id"])
    return service.start_campaign(campaign["id"])["campaign"]


def _error_code(func) -> str:
    try:
        func()
    except CampaignLifecycleError as exc:
        return exc.error_code
    raise AssertionError("expected lifecycle error")


def test_valid_lifecycle_transitions() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "life.db")
        campaign = _campaign(service, 1)
        queued = service.queue_campaign(campaign["id"])["campaign"]
        running = service.start_campaign(campaign["id"])["campaign"]
        paused = service.pause_campaign(campaign["id"])["campaign"]
        resumed = service.resume_campaign(campaign["id"])["campaign"]
        cancelled = service.cancel_campaign(campaign["id"])["campaign"]
    assert queued["status"] == "queued"
    assert running["status"] == "running"
    assert paused["status"] == "paused"
    assert resumed["status"] == "running"
    assert cancelled["status"] == "cancelled"


def test_invalid_transitions_and_start_validation() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "life.db")
        empty = _campaign(service, 0)
        assert _error_code(lambda: service.start_campaign(empty["id"])) == "invalid_campaign_transition"
        assert _error_code(lambda: service.queue_campaign(empty["id"])) == "campaign_has_no_deliverable_jobs"
        campaign = _campaign(service, 1)
        assert _error_code(lambda: service.pause_campaign(campaign["id"])) == "invalid_campaign_transition"
        service.queue_campaign(campaign["id"])
        service.cancel_campaign(campaign["id"])
        assert _error_code(lambda: service.start_campaign(campaign["id"])) == "campaign_cancelled"


def test_campaign_with_no_eligible_account_cannot_start() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "life.db", auth_ok=False)
        campaign = _campaign(service, 1)
        service.queue_campaign(campaign["id"])
        code = _error_code(lambda: service.start_campaign(campaign["id"]))
        summary = service.validate_campaign_start(campaign["id"])
    assert code == "no_eligible_account"
    assert summary["eligible_account_count"] == 0


def test_scheduler_only_assigns_running_campaigns_and_filter_is_strict() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        orchestrator = OrchestratorStub()
        service = _service(Path(tmp_dir) / "life.db", orchestrator)
        draft = _campaign(service, 1)
        paused = _running_campaign(service, 1)
        service.pause_campaign(paused["id"])
        cancelled = _campaign(service, 1)
        service.queue_campaign(cancelled["id"])
        service.cancel_campaign(cancelled["id"])
        running = _running_campaign(service, 1)
        service.scheduler_start()
        draft_result = service.scheduler_run_once(campaign_id=draft["id"], dry_run=True)
        paused_result = service.scheduler_run_once(campaign_id=paused["id"], dry_run=True)
        cancelled_result = service.scheduler_run_once(campaign_id=cancelled["id"], dry_run=True)
        running_result = service.scheduler_run_once(campaign_id=running["id"], dry_run=True)
        other_jobs = service.list_jobs(campaign_id=draft["id"], limit=10)["items"]
    assert draft_result["started_accounts"] == []
    assert paused_result["started_accounts"] == []
    assert cancelled_result["started_accounts"] == []
    assert running_result["started_accounts"] == ["bale_a"]
    assert all(job["account_id"] is None for job in other_jobs)


def test_pause_requeues_unstarted_assigned_but_not_running_uncertain_job() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "life.db")
        campaign = _running_campaign(service, 2)
        service.assign_jobs("bale_a", campaign["id"], limit=2)
        jobs = service.list_jobs(campaign_id=campaign["id"], limit=10)["items"]
        with service.repository.connection() as connection:
            connection.execute("UPDATE commercial_delivery_jobs SET status = 'running', started_at = '2026-01-01T00:00:00+00:00' WHERE id = ?", (jobs[0]["id"],))
            connection.commit()
        result = service.pause_campaign(campaign["id"])
        after = {job["id"]: job for job in service.list_jobs(campaign_id=campaign["id"], limit=10)["items"]}
    assert result["requeued_assigned_count"] == 1
    assert after[jobs[0]["id"]]["status"] == "running"
    assert after[jobs[1]["id"]]["status"] == "queued"


def test_resume_preserves_succeeded_and_cancel_preserves_history() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "life.db")
        campaign = _running_campaign(service, 3)
        jobs = service.list_jobs(campaign_id=campaign["id"], limit=10)["items"]
        service.repository.complete_job(jobs[0]["id"], "succeeded", {"result_success": True, "verified_forwarded_recipient_count": 1, "forward_verified": True, "diagnostics_consistent": True})
        service.repository.complete_job(jobs[1]["id"], "failed", {"result_success": False, "verified_forwarded_recipient_count": 0, "forward_verified": False, "diagnostics_consistent": True, "last_error_code": "test"})
        service.pause_campaign(campaign["id"])
        service.resume_campaign(campaign["id"])
        service.cancel_campaign(campaign["id"])
        after = {job["id"]: job for job in service.list_jobs(campaign_id=campaign["id"], limit=10)["items"]}
    assert after[jobs[0]["id"]]["status"] == "succeeded"
    assert after[jobs[1]["id"]]["status"] == "failed"
    assert sum(1 for job in after.values() if job["status"] == "cancelled") == 1


def test_completion_detection_and_paused_campaign_not_completed() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "life.db")
        campaign = _running_campaign(service, 1)
        job = service.list_jobs(campaign_id=campaign["id"], limit=1)["items"][0]
        service.repository.complete_job(job["id"], "succeeded", {"result_success": True, "verified_forwarded_recipient_count": 1, "forward_verified": True, "diagnostics_consistent": True})
        completed = service.complete_campaign_if_finished(campaign["id"])

        paused = _running_campaign(service, 1)
        service.pause_campaign(paused["id"])
        paused_job = service.list_jobs(campaign_id=paused["id"], limit=1)["items"][0]
        service.repository.complete_job(paused_job["id"], "skipped", {"result_success": False, "verified_forwarded_recipient_count": 0, "forward_verified": False, "diagnostics_consistent": True})
        paused_after = service.complete_campaign_if_finished(paused["id"])
    assert completed["status"] == "completed"
    assert completed["completed_at"]
    assert paused_after["status"] == "paused"


def test_dry_round_forces_dry_run_and_does_not_increment_counters() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        orchestrator = OrchestratorStub()
        service = _service(Path(tmp_dir) / "life.db", orchestrator)
        campaign = _running_campaign(service, 3)
        service.scheduler_start()
        before_jobs = service.list_jobs(campaign_id=campaign["id"], limit=10)["items"]
        result = service.run_campaign_dry_round(campaign["id"])
        settings = service.resolve_account_settings("bale_a")
        jobs = service.list_jobs(campaign_id=campaign["id"], limit=10)["items"]
    assert result["dry_run"] is True
    assert result["diagnostics"]["read_only"] is True
    assert result["audit"]["status"] == "completed"
    assert orchestrator.calls == []
    assert settings["current_daily_sent_count"] == 0
    assert {job["id"]: job["status"] for job in jobs} == {job["id"]: job["status"] for job in before_jobs}


if __name__ == "__main__":
    test_valid_lifecycle_transitions()
    test_invalid_transitions_and_start_validation()
    test_campaign_with_no_eligible_account_cannot_start()
    test_scheduler_only_assigns_running_campaigns_and_filter_is_strict()
    test_pause_requeues_unstarted_assigned_but_not_running_uncertain_job()
    test_resume_preserves_succeeded_and_cancel_preserves_history()
    test_completion_detection_and_paused_campaign_not_completed()
    test_dry_round_forces_dry_run_and_does_not_increment_counters()
    print("Commercial campaign lifecycle tests passed")
