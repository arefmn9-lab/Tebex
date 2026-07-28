from __future__ import annotations

import tempfile
from pathlib import Path

from app.main import app
from app.routes import automation as automation_routes
from fastapi.testclient import TestClient
from modules.automation_engine.commercial_queue.repository import CommercialQueueRepository, commercial_send_completed, utc_now
from modules.automation_engine.commercial_queue.service import CommercialQueueService


class OrchestratorStub:
    def __init__(self, results: list[dict] | None = None) -> None:
        self.results = list(results or [])
        self.calls: list[dict] = []

    def __call__(self, **payload: object) -> dict:
        self.calls.append(dict(payload))
        if self.results:
            result = dict(self.results.pop(0))
        else:
            result = {"success": True, "forward_verified": True, "diagnostics_consistent": True}
        return {
            "action": "forward_latest_channel_message",
            "verified_forwarded_recipient_count": 1 if result.get("success") and not payload.get("dry_run") else 0,
            "confirm_click_count": 0 if payload.get("dry_run") else 1,
            "error_code": None,
            "error_message": None,
            **result,
        }


def _service(path: Path, orchestrator: OrchestratorStub | None = None) -> CommercialQueueService:
    service = CommercialQueueService(
        repository=CommercialQueueRepository(path),
        orchestrator=orchestrator or OrchestratorStub(),
        account_auth_checker=lambda account_id: True,
        sleeper=lambda seconds: None,
    )
    service.update_global_settings(
        {
            "default_source_channel_uid": "5613544284",
            "deliveries_per_account_round": 2,
            "default_daily_limit_per_account": 10,
            "delay_between_deliveries_seconds": 0,
            "round_cooldown_seconds": 0,
            "max_job_duration_seconds": 30,
            "job_timeout_seconds": 30,
        }
    )
    return service


def _campaign_with_jobs(service: CommercialQueueService, count: int = 3) -> dict:
    campaign = service.create_campaign({"name": "Worker Campaign", "platform": "bale", "status": "running", "source_channel_uid": "5613544284"})
    phones = [f"0930407333{index}" for index in range(1, count + 1)]
    service.import_recipients(campaign["id"], phones)
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


def test_disabled_cooldown_and_daily_limit_accounts_cannot_assign() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "worker.db")
        campaign = _campaign_with_jobs(service)
        service.update_account_settings("bale_a", {"enabled": False})
        disabled = service.assign_jobs("bale_a", campaign["id"])
        service.update_account_settings("bale_b", {"cooldown_until": "2999-01-01T00:00:00+00:00"})
        cooling = service.assign_jobs("bale_b", campaign["id"])
        service.update_account_settings("bale_c", {"daily_limit_override": 1, "current_daily_sent_count": 1})
        limited = service.assign_jobs("bale_c", campaign["id"])

    assert disabled["reason"] == "account_disabled"
    assert cooling["reason"] == "account_cooling_down"
    assert limited["reason"] == "daily_limit_reached"


def test_assignment_respects_round_limit_remaining_capacity_and_ordering() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "worker.db")
        campaign = _campaign_with_jobs(service, count=3)
        jobs = service.list_jobs(campaign_id=campaign["id"], limit=10)["items"]
        with service.repository.connection() as connection:
            connection.execute("UPDATE commercial_delivery_jobs SET priority = 5, scheduled_at = NULL WHERE id = ?", (jobs[0]["id"],))
            connection.execute("UPDATE commercial_delivery_jobs SET priority = 10, scheduled_at = '2026-01-02T00:00:00+00:00' WHERE id = ?", (jobs[1]["id"],))
            connection.execute("UPDATE commercial_delivery_jobs SET priority = 10, scheduled_at = NULL WHERE id = ?", (jobs[2]["id"],))
            connection.commit()
        service.update_account_settings("bale_a", {"daily_limit_override": 1, "current_daily_sent_count": 0, "deliveries_per_round_override": 2})
        assigned = service.assign_jobs("bale_a", campaign["id"], limit=10)

    assert assigned["assigned_count"] == 1
    assert assigned["assigned_job_ids"] == [jobs[2]["id"]]
    assert assigned["remaining_daily_capacity"] == 0


def test_concurrent_assignment_cannot_claim_same_job() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "worker.db")
        campaign = _campaign_with_jobs(service, count=2)
        first = service.assign_jobs("bale_a", campaign["id"], limit=2)
        second = service.assign_jobs("bale_b", campaign["id"], limit=2)

    assert set(first["assigned_job_ids"]).isdisjoint(set(second["assigned_job_ids"]))
    assert first["assigned_count"] == 2
    assert second["assigned_count"] == 0


def test_one_worker_lock_per_account_and_stale_release() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        repo = CommercialQueueRepository(Path(tmp_dir) / "worker.db")
        first = repo.acquire_worker_lock("bale_a", "owner-1", ttl_seconds=30)
        second = repo.acquire_worker_lock("bale_a", "owner-2", ttl_seconds=30)
        released_active = repo.release_worker_lock("bale_a", stale_only=True)
        with repo.connection() as connection:
            connection.execute("UPDATE commercial_account_worker_locks SET expires_at = '2000-01-01T00:00:00+00:00' WHERE account_id = 'bale_a'")
            connection.commit()
        released_stale = repo.release_worker_lock("bale_a", stale_only=True)

    assert first is not None
    assert second is None
    assert released_active is False
    assert released_stale is True


def test_stale_job_recovery_requeues_assigned_and_pauses_uncertain_running() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "worker.db")
        campaign = _campaign_with_jobs(service, count=2)
        service.assign_jobs("bale_a", campaign["id"], limit=2)
        jobs = service.list_jobs(campaign_id=campaign["id"], limit=10)["items"]
        with service.repository.connection() as connection:
            connection.execute("UPDATE commercial_delivery_jobs SET status = 'running' WHERE id = ?", (jobs[0]["id"],))
            connection.commit()
        recovered = service.recover_stale_jobs()
        after = {job["id"]: job for job in service.list_jobs(campaign_id=campaign["id"], limit=10)["items"]}

    assert recovered["requeued_assigned_count"] == 1
    assert recovered["manual_review_required_count"] == 1
    assert after[jobs[1]["id"]]["status"] == "queued"
    assert after[jobs[0]["id"]]["status"] == "paused"
    assert after[jobs[0]["id"]]["last_error_code"] == "manual_review_required"


def test_worker_runs_assigned_jobs_sequentially_and_updates_success_counters() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        orchestrator = OrchestratorStub()
        service = _service(Path(tmp_dir) / "worker.db", orchestrator)
        campaign = _campaign_with_jobs(service, count=2)
        result = service.run_account_round("bale_a", campaign["id"], max_jobs=2, dry_run=False)
        settings = service.resolve_account_settings("bale_a")
        jobs = service.list_jobs(campaign_id=campaign["id"], status="succeeded", limit=10)["items"]

    assert result["processed_count"] == 2
    assert [call["job_id"] for call in orchestrator.calls] == [item["job_id"] for item in result["results"]]
    assert len(jobs) == 2
    assert settings["current_daily_sent_count"] == 2
    assert settings["current_round_sent_count"] == 2


def test_commercial_send_completed_prefers_modern_submitted_contract() -> None:
    cases = [
        (
            {
                "success": True,
                "delivery_status": "submitted",
                "send_action_verified": True,
                "delivery_verified": False,
                "forward_verified": False,
                "diagnostics_consistent": False,
                "error_code": None,
            },
            True,
        ),
        (
            {
                "success": True,
                "delivery_status": "delivered",
                "send_action_verified": True,
                "delivery_verified": True,
                "error_code": None,
            },
            True,
        ),
        (
            {
                "success": False,
                "delivery_status": "send_unverified",
                "send_action_verified": False,
                "forward_verified": True,
                "diagnostics_consistent": True,
                "error_code": "send_result_unverified",
            },
            False,
        ),
        (
            {"success": True, "forward_verified": True, "diagnostics_consistent": True, "error_code": None},
            True,
        ),
        (
            {"success": True, "forward_verified": False, "diagnostics_consistent": True, "error_code": None},
            False,
        ),
    ]

    for payload, expected in cases:
        assert commercial_send_completed(payload) is expected


def test_submitted_worker_result_is_terminal_success_without_delivery_verification_or_retry() -> None:
    submitted = {
        "success": True,
        "delivery_status": "submitted",
        "send_action_verified": True,
        "delivery_verified": False,
        "send_success_verified": False,
        "forward_verified": False,
        "diagnostics_consistent": False,
        "verified_forwarded_recipient_count": 0,
        "error_code": None,
    }
    with tempfile.TemporaryDirectory() as tmp_dir:
        orchestrator = OrchestratorStub([submitted])
        service = _service(Path(tmp_dir) / "worker.db", orchestrator)
        campaign = _campaign_with_jobs(service, count=1)
        result = service.run_account_round("bale_a", campaign["id"], max_jobs=1, dry_run=False)
        resumed = service.run_account_round("bale_a", campaign["id"], max_jobs=1, dry_run=False)
        jobs = service.list_jobs(campaign_id=campaign["id"], limit=10)["items"]
        campaign_after = service.get_campaign(campaign["id"])

    assert result["processed_count"] == 1
    assert result["requeued_unstarted_count"] == 0
    assert resumed["assigned_count"] == 0
    assert jobs[0]["status"] == "succeeded"
    assert jobs[0]["result_success"] == 1
    assert jobs[0]["forward_verified"] == 0
    assert jobs[0]["retryable"] is None
    assert campaign_after["succeeded_count"] == 1
    assert campaign_after["failed_count"] == 0


def test_failed_job_does_not_increment_and_ordinary_failure_continues() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        orchestrator = OrchestratorStub(
            [
                {"success": False, "forward_verified": False, "diagnostics_consistent": True, "error_code": "recipient_not_found", "error_message": "missing"},
                {"success": True, "forward_verified": True, "diagnostics_consistent": True},
            ]
        )
        service = _service(Path(tmp_dir) / "worker.db", orchestrator)
        campaign = _campaign_with_jobs(service, count=2)
        result = service.run_account_round("bale_a", campaign["id"], max_jobs=2, dry_run=False)
        settings = service.resolve_account_settings("bale_a")
        failed = service.list_jobs(campaign_id=campaign["id"], status="failed", limit=10)["items"]
        succeeded = service.list_jobs(campaign_id=campaign["id"], status="succeeded", limit=10)["items"]

    assert result["processed_count"] == 2
    assert len(failed) == 1
    assert len(succeeded) == 1
    assert settings["current_daily_sent_count"] == 1


def test_unsafe_account_level_failure_stops_round_and_requeues_unstarted() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        orchestrator = OrchestratorStub(
            [
                {"success": False, "forward_verified": False, "diagnostics_consistent": False, "error_code": "multiple_recipients_selected"},
                {"success": True, "forward_verified": True, "diagnostics_consistent": True},
            ]
        )
        service = _service(Path(tmp_dir) / "worker.db", orchestrator)
        campaign = _campaign_with_jobs(service, count=2)
        result = service.run_account_round("bale_a", campaign["id"], max_jobs=2, dry_run=False)
        queued = service.list_jobs(campaign_id=campaign["id"], status="queued", limit=10)["items"]

    assert result["stopped_early"] is True
    assert result["processed_count"] == 1
    assert result["requeued_unstarted_count"] == 1
    assert len(queued) == 1


def test_dry_run_sends_nothing_and_does_not_increment_daily_count() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        orchestrator = OrchestratorStub([{"success": True, "forward_verified": False, "diagnostics_consistent": True}])
        service = _service(Path(tmp_dir) / "worker.db", orchestrator)
        campaign = _campaign_with_jobs(service, count=1)
        result = service.run_account_round("bale_a", campaign["id"], max_jobs=1, dry_run=True)
        settings = service.resolve_account_settings("bale_a")
        skipped = service.list_jobs(campaign_id=campaign["id"], status="skipped", limit=10)["items"]

    assert result["processed_count"] == 1
    assert orchestrator.calls[0]["dry_run"] is True
    assert settings["current_daily_sent_count"] == 0
    assert skipped[0]["last_error_code"] == "dry_run_no_delivery"
    assert skipped[0]["result_success"] == 0
    assert skipped[0]["verified_forwarded_recipient_count"] == 0


def test_campaign_counters_and_queue_persist_after_reload() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "worker.db"
        service = _service(db_path)
        campaign = _campaign_with_jobs(service, count=2)
        service.run_account_round("bale_a", campaign["id"], max_jobs=1, dry_run=False)
        reloaded = _service(db_path)
        campaign_after = reloaded.get_campaign(campaign["id"])
        queued = reloaded.list_jobs(campaign_id=campaign["id"], status="queued", limit=10)["items"]

        assert campaign_after is not None
        assert campaign_after["succeeded_count"] == 1
        assert campaign_after["queued_count"] == 1
        assert len(queued) == 1


def test_worker_api_contracts() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        previous_service = automation_routes.commercial_queue_service
        service = _service(Path(tmp_dir) / "worker.db")
        automation_routes.commercial_queue_service = service
        try:
            client = TestClient(app)
            campaign = client.post("/automation/campaigns", json={"name": "Worker API", "platform": "bale", "status": "running", "source_channel_uid": "5613544284"}).json()
            client.post(f"/automation/campaigns/{campaign['id']}/recipients", json={"phones": ["09304073331"], "import_source": "manual"})
            recipient = service.list_recipients(campaign["id"], limit=1)["items"][0]
            service.repository.update_recipient_authorization(
                recipient["id"],
                {
                    "recipient_origin": "user_provided",
                    "synthetic_test_data": False,
                    "live_execution_authorized": True,
                    "authorization_status": "authorized",
                    "should_not_retry": False,
                },
            )
            assign = client.post("/automation/workers/bale_a/assign", json={"campaign_id": campaign["id"], "limit": 1})
            status = client.get("/automation/workers/bale_a/status")
            recover = client.post("/automation/jobs/recover-stale")
            release = client.post("/automation/workers/bale_a/release-stale-lock")
        finally:
            automation_routes.commercial_queue_service = previous_service

    assert assign.status_code == 200
    assert assign.json()["assigned_count"] == 1
    assert status.json()["account_id"] == "bale_a"
    assert recover.status_code == 200
    assert release.status_code == 200


if __name__ == "__main__":
    test_disabled_cooldown_and_daily_limit_accounts_cannot_assign()
    test_assignment_respects_round_limit_remaining_capacity_and_ordering()
    test_concurrent_assignment_cannot_claim_same_job()
    test_one_worker_lock_per_account_and_stale_release()
    test_stale_job_recovery_requeues_assigned_and_pauses_uncertain_running()
    test_worker_runs_assigned_jobs_sequentially_and_updates_success_counters()
    test_commercial_send_completed_prefers_modern_submitted_contract()
    test_submitted_worker_result_is_terminal_success_without_delivery_verification_or_retry()
    test_failed_job_does_not_increment_and_ordinary_failure_continues()
    test_unsafe_account_level_failure_stops_round_and_requeues_unstarted()
    test_dry_run_sends_nothing_and_does_not_increment_daily_count()
    test_campaign_counters_and_queue_persist_after_reload()
    test_worker_api_contracts()
    print("Commercial worker tests passed")
