from __future__ import annotations

import tempfile
from pathlib import Path
from uuid import uuid4

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
        phones = [f"093040735{index:02d}" for index in range(count)]
        service.confirm_campaign_recipients(campaign["id"], phones, submitted_by="test", source_type="test", confirmation_checked=True)
        service.materialize_campaign_recipients(
            campaign["id"],
            ["bale"],
            authorize_for_live_execution=True,
            authorized_by="test",
            authorization_note="mock recipient",
        )
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
            service.repository.update_jobs_authorization_by_recipient(
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
        service.configure_campaign_platform_settings(
            campaign["id"],
            ["bale"],
            {
                "bale": {
                    "source_uid": "5613544284",
                    "source_url": "https://web.bale.ai/chat?uid=5613544284",
                    "source_label": "Lifecycle source",
                    "sender_account_ids": ["bale_a"],
                }
            },
            created_by="test",
        )
    return campaign


def _queue_payload(service: CommercialQueueService, campaign_id: str, *, idempotency_key: str | None = None) -> dict:
    validation = service.validate_campaign_start(campaign_id)
    dry = service.check_campaign_without_sending(campaign_id)
    review = service.final_review(campaign_id)
    manifest = service.repository.get_confirmed_manifest_for_campaign(campaign_id)
    return {
        "validation_hash": service.validation_hash(validation),
        "dry_run_id": dry["audit"]["dry_run_id"],
        "final_review_hash": review["final_review_hash"],
        "manifest_hash": manifest["manifest_hash"] if manifest else None,
        "idempotency_key": idempotency_key or f"queue-{uuid4().hex}",
        "explicit_operator_confirmation": True,
        "expected_campaign_status": "draft",
    }


def _queue(service: CommercialQueueService, campaign_id: str, **overrides: object) -> dict:
    payload = {**_queue_payload(service, campaign_id), **overrides}
    return service.queue_campaign(campaign_id, payload)


def _running_campaign(service: CommercialQueueService, count: int = 3) -> dict:
    campaign = _campaign(service, count=count)
    _queue(service, campaign["id"])
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
        queued = _queue(service, campaign["id"])["campaign"]
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
        assert _error_code(lambda: service.queue_campaign(empty["id"], {"expected_campaign_status": "draft", "explicit_operator_confirmation": True, "idempotency_key": "empty"})) == "manifest_missing"
        campaign = _campaign(service, 1)
        assert _error_code(lambda: service.pause_campaign(campaign["id"])) == "invalid_campaign_transition"
        _queue(service, campaign["id"])
        service.cancel_campaign(campaign["id"])
        assert _error_code(lambda: service.start_campaign(campaign["id"])) == "campaign_cancelled"


def test_campaign_with_no_eligible_account_cannot_start() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "life.db", auth_ok=False)
        campaign = _campaign(service, 1)
        assert _error_code(lambda: _queue(service, campaign["id"])) == "account_context_missing"
        with service.repository.connection() as connection:
            connection.execute("UPDATE commercial_campaigns SET status = 'queued' WHERE id = ?", (campaign["id"],))
            connection.commit()
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
        _queue(service, cancelled["id"])
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


def test_queue_requires_hardened_evidence_and_confirmation() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "life.db")
        campaign = _campaign(service, 1)
        payload = _queue_payload(service, campaign["id"])
        assert _error_code(lambda: service.queue_campaign(campaign["id"], {**payload, "explicit_operator_confirmation": False})) == "operator_confirmation_required"
        assert _error_code(lambda: service.queue_campaign(campaign["id"], {**payload, "idempotency_key": ""})) == "idempotency_key_required"
        assert _error_code(lambda: service.queue_campaign(campaign["id"], {**payload, "dry_run_id": "missing"})) == "dry_run_evidence_missing"
        assert _error_code(lambda: service.queue_campaign(campaign["id"], {**payload, "final_review_hash": "stale"})) == "final_review_stale"
        assert _error_code(lambda: service.queue_campaign(campaign["id"], {**payload, "manifest_hash": "stale"})) == "manifest_stale"


def test_valid_queue_response_is_non_executing_and_duplicate_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        orchestrator = OrchestratorStub()
        service = _service(Path(tmp_dir) / "life.db", orchestrator)
        campaign = _campaign(service, 1)
        result = _queue(service, campaign["id"], idempotency_key="queue-once")
        duplicate = _error_code(lambda: service.queue_campaign(campaign["id"], _queue_payload(service, campaign["id"], idempotency_key="queue-once")))
    assert result["queued"] is True
    assert result["execution_started"] is False
    assert result["campaign"]["status"] == "queued"
    assert result["authorized_recipient_count"] == 1
    assert result["blocked_recipient_count"] == 0
    assert result["idempotency"]["status"] == "created"
    assert duplicate == "duplicate_queue_request"
    assert orchestrator.calls == []


def test_queue_rejects_limit_and_account_context_failures() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        limited = _service(Path(tmp_dir) / "limited.db")
        limited.update_global_settings({"deliveries_per_account_round": 1})
        too_many = _campaign(limited, 2)
        assert _error_code(lambda: _queue(limited, too_many["id"])) == "limit_exceeded"

    with tempfile.TemporaryDirectory() as tmp_dir:
        no_account = _service(Path(tmp_dir) / "no_account.db", auth_ok=False)
        campaign = _campaign(no_account, 1)
        assert _error_code(lambda: _queue(no_account, campaign["id"])) == "account_context_missing"


def test_queue_rejects_missing_manifest_and_stale_dry_run_evidence() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "missing_manifest.db")
        campaign = service.create_campaign({"name": "No manifest", "platform": "bale", "status": "draft", "source_channel_uid": "5613544284"})
        recipient, _ = service.repository.create_recipient_and_job(campaign, "09304073500", "09304073500", "No manifest", "test")
        service.repository.update_recipient_authorization(recipient["id"], {"recipient_origin": "user_provided", "live_execution_authorized": True, "authorization_status": "authorized", "synthetic_test_data": False})
        service.repository.update_jobs_authorization_by_recipient(recipient["id"], {"recipient_origin": "user_provided", "live_execution_authorized": True, "authorization_status": "authorized", "synthetic_test_data": False})
        for recipient in service.list_recipients(campaign["id"], limit=100)["items"]:
            service.repository.update_recipient_authorization(recipient["id"], {"recipient_origin": "user_provided", "live_execution_authorized": True, "authorization_status": "authorized", "synthetic_test_data": False})
            service.repository.update_jobs_authorization_by_recipient(recipient["id"], {"recipient_origin": "user_provided", "live_execution_authorized": True, "authorization_status": "authorized", "synthetic_test_data": False})
        validation = service.validate_campaign_start(campaign["id"])
        assert _error_code(lambda: service.queue_campaign(campaign["id"], {
            "validation_hash": service.validation_hash(validation),
            "dry_run_id": "dryrun_missing",
            "final_review_hash": "review_missing",
            "manifest_hash": "manifest_missing",
            "idempotency_key": "missing-manifest",
            "explicit_operator_confirmation": True,
            "expected_campaign_status": "draft",
        })) == "manifest_missing"

    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "stale_dry.db")
        campaign = _campaign(service, 1)
        payload = _queue_payload(service, campaign["id"])
        with service.repository.connection() as connection:
            connection.execute("UPDATE commercial_dry_run_audit_records SET diagnostics_json = ? WHERE dry_run_id = ?", ('{"validation":{},"confirmed_manifest_id":"stale"}', payload["dry_run_id"]))
            connection.commit()
        assert _error_code(lambda: service.queue_campaign(campaign["id"], payload)) == "dry_run_evidence_stale"


def test_queue_rejects_stale_final_review_and_authorization_incomplete() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "auth.db")
        campaign = _campaign(service, 1)
        payload = _queue_payload(service, campaign["id"])
        recipient = service.list_recipients(campaign["id"], limit=1)["items"][0]
        service.repository.update_recipient_authorization(recipient["id"], {"live_execution_authorized": False, "authorization_status": "authorization_required"})
        service.repository.update_jobs_authorization_by_recipient(recipient["id"], {"live_execution_authorized": False, "authorization_status": "authorization_required"})
        fresh_validation = service.validate_campaign_start(campaign["id"])
        assert _error_code(lambda: service.queue_campaign(campaign["id"], {**payload, "validation_hash": service.validation_hash(fresh_validation)})) == "authorization_incomplete"

    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "stale_review.db")
        campaign = _campaign(service, 1)
        payload = _queue_payload(service, campaign["id"])
        assert _error_code(lambda: service.queue_campaign(campaign["id"], {**payload, "final_review_hash": "stale"})) == "final_review_stale"


if __name__ == "__main__":
    test_valid_lifecycle_transitions()
    test_invalid_transitions_and_start_validation()
    test_campaign_with_no_eligible_account_cannot_start()
    test_scheduler_only_assigns_running_campaigns_and_filter_is_strict()
    test_pause_requeues_unstarted_assigned_but_not_running_uncertain_job()
    test_resume_preserves_succeeded_and_cancel_preserves_history()
    test_completion_detection_and_paused_campaign_not_completed()
    test_dry_round_forces_dry_run_and_does_not_increment_counters()
    test_queue_requires_hardened_evidence_and_confirmation()
    test_valid_queue_response_is_non_executing_and_duplicate_is_rejected()
    test_queue_rejects_limit_and_account_context_failures()
    test_queue_rejects_missing_manifest_and_stale_dry_run_evidence()
    test_queue_rejects_stale_final_review_and_authorization_incomplete()
    print("Commercial campaign lifecycle tests passed")
