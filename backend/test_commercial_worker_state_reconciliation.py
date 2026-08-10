from __future__ import annotations

import os
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from modules.automation_engine.commercial_queue.repository import CommercialQueueRepository
from modules.automation_engine.commercial_queue.repository import classify_failed_delivery_recovery_evidence
from modules.automation_engine.commercial_queue.service import CommercialQueueService


def _service(path: Path) -> CommercialQueueService:
    return CommercialQueueService(
        repository=CommercialQueueRepository(path),
        orchestrator=lambda **payload: {"success": True},
        account_auth_checker=lambda account_id: True,
        sleeper=lambda seconds: None,
    )


def test_expired_cooldown_without_lock_reconciles_worker_not_idle_to_idle() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "queue.db")
        service.update_account_settings("bale_a", {
            "enabled": True,
            "worker_status": "cooling_down",
            "cooldown_until": "2020-01-01T00:00:00+00:00",
            "daily_limit_override": 10,
        })

        before = service.repository.get_account_settings("bale_a")
        result = service.reconcile_worker_state("bale_a")
        after = service.repository.get_account_settings("bale_a")

        assert before["worker_status"] == "cooling_down"
        assert result["canonical_state"] == "idle"
        assert after["worker_status"] == "idle"
        assert after["cooldown_until"] is None
        assert service.repository.list_active_worker_locks() == []


def test_restart_clears_lock_owned_by_dead_runtime_and_is_idempotent() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "queue.db")
        service.update_account_settings("bale_a", {"enabled": True, "worker_status": "running"})
        service.repository.acquire_worker_lock(
            "bale_a", "old_worker", 300,
            runtime_owner_id="scheduler_old", worker_round_id="round_old", process_id=99999999,
        )

        first = service.reconcile_worker_state("bale_a")
        second = service.reconcile_worker_state("bale_a")

        assert "stale_lock_released" in first["actions"]
        assert service.repository.get_worker_lock("bale_a") is None
        assert service.repository.get_account_settings("bale_a")["worker_status"] == "idle"
        assert second["changed"] is False


def test_live_round_lock_is_not_cleared_and_active_count_agrees() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "queue.db")
        runtime = SimpleNamespace(owner_id="scheduler_current")
        service.scheduler_runtime = runtime
        service.update_account_settings("bale_a", {"enabled": True, "worker_status": "running"})
        lock = service.repository.acquire_worker_lock(
            "bale_a", "worker", 300,
            runtime_owner_id=runtime.owner_id, worker_round_id="round_live", process_id=os.getpid(),
        )
        with service._active_worker_rounds_lock:
            service._active_worker_rounds["bale_a"] = {
                "worker_round_id": "round_live",
                "runtime_owner_id": runtime.owner_id,
                "lock_token": lock["lock_token"],
            }

        result = service.reconcile_worker_state("bale_a")

        assert result["canonical_state"] == "running"
        assert result["changed"] is False
        assert service.repository.get_worker_lock("bale_a") is not None
        assert len(service.repository.list_active_worker_locks()) == 1


def test_pre_browser_exception_always_releases_lock_and_returns_idle() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "queue.db")
        service.update_account_settings("bale_a", {
            "enabled": True,
            "worker_status": "idle",
            "daily_limit_override": 10,
            "source_channel_uid_override": "5613544284",
        })
        service.account_readiness_matrix = lambda: [{
            "account_id": "bale_a", "worker_eligible": True,
            "enabled": True, "commercial_enabled": True,
            "authentication_status": "authenticated", "worker_status": "idle",
        }]
        campaign = service.create_campaign({
            "name": "pre-browser-failure", "platform": "bale", "status": "running",
            "source_channel_uid": "5613544284", "capacity_reservation": 1,
        })
        service.import_recipients(campaign["id"], ["09351234567"])
        recipient = service.list_recipients(campaign["id"], limit=10)["items"][0]
        service.repository.update_recipient_authorization(recipient["id"], {
            "recipient_origin": "user_import", "live_execution_authorized": True,
            "live_authorized_at": "2026-07-31T00:00:00+00:00", "live_authorized_by": "test",
            "authorization_source": "test", "authorization_status": "authorized",
        })
        service._ensure_pre_browser_runtime_context = lambda job, account_id: (_ for _ in ()).throw(RuntimeError("forced_pre_browser_failure"))

        result = service.run_account_round("bale_a", campaign["id"], max_jobs=1)

        assert result["reason"] == "forced_pre_browser_failure"
        assert service.repository.get_worker_lock("bale_a") is None
        assert service.repository.get_account_settings("bale_a")["worker_status"] == "idle"
        assert service._active_worker_rounds == {}


def test_safe_test_boundary_requeues_before_contact_or_browser(monkeypatch) -> None:
    """The UI acceptance boundary must be safe even if the contact store would fail."""
    class ContactStoreMustNotRun:
        def get_or_create_bale_contact(self, *args, **kwargs):  # pragma: no cover - failure path only
            raise AssertionError("safe worker boundary tried to create a Bale contact")

    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "queue.db")
        service.contact_store = ContactStoreMustNotRun()
        service.update_account_settings("bale_a", {
            "enabled": True,
            "worker_status": "idle",
            "daily_limit_override": 10,
            "source_channel_uid_override": "5613544284",
        })
        service.account_readiness_matrix = lambda: [{
            "account_id": "bale_a", "worker_eligible": True,
            "enabled": True, "commercial_enabled": True,
            "authentication_status": "authenticated", "worker_status": "idle",
        }]
        campaign = service.create_campaign({
            "name": "safe-boundary", "platform": "bale", "status": "running",
            "source_channel_uid": "5613544284", "capacity_reservation": 1,
        })
        service.import_recipients(campaign["id"], ["09351234567"])
        recipient = service.list_recipients(campaign["id"], limit=10)["items"][0]
        authorization = {
            "recipient_origin": "user_import", "synthetic_test_data": False,
            "live_execution_authorized": True,
            "live_authorized_at": "2026-08-09T00:00:00+00:00",
            "live_authorized_by": "isolated_test",
            "authorization_source": "isolated_test", "authorization_status": "authorized",
            "should_not_retry": False, "live_execution_blocked": False,
        }
        service.repository.update_recipient_authorization(recipient["id"], authorization)
        service.repository.update_jobs_authorization_by_recipient(recipient["id"], authorization)
        monkeypatch.setenv("CLINICOS_TEST_MODE", "1")
        monkeypatch.setenv("CLINICOS_SAFE_TEST_WORKER_BOUNDARY", "1")

        result = service.run_account_round("bale_a", campaign["id"], max_jobs=1)

        jobs = service.list_jobs(campaign_id=campaign["id"], limit=10)["items"]
        events = service.repository.list_events(None, campaign["id"], "bale_a", limit=10, offset=0)
        assert result["success"] is True
        assert result.get("stop_reason") == "test_delivery_boundary_reached", result
        assert result["requeued_unstarted_count"] == 1
        assert jobs[0]["status"] == "queued"
        assert jobs[0]["account_id"] is None
        assert jobs[0]["attempt_count"] == 0
        assert service.get_campaign(campaign["id"])["status"] == "running"
        assert any(event["event_type"] == "test_safe_worker_boundary_reached" for event in events)
        assert service.repository.get_worker_lock("bale_a") is None


def _recovery_evidence(**overrides: object) -> tuple[dict[str, object], dict[str, object]]:
    job = {"id": "job_safe", "status": "failed", "result_success": 0, "forward_verified": 0, "verified_forwarded_recipient_count": 0}
    result = {
        "forward_picker_opened": True,
        "recipient_selected": False,
        "confirm_click_count": 0,
        "send_confirmation_click_count": 0,
        "forward_verified": False,
        "final_send_invoked": False,
        "delivery_status": "not_sent",
        "success_toast_text": "",
        "scenario_result": {"final_send_invoked": False, "records": {"recipient_picker_visible": True}},
    }
    result.update(overrides)
    return job, {"orchestrator_result": result}


def test_picker_opened_without_selection_or_send_is_safe_to_requeue() -> None:
    job, diagnostics = _recovery_evidence()
    result = classify_failed_delivery_recovery_evidence(job, diagnostics, platform_outcome="failed_retryable", attempt_statuses=["failed"])
    assert result["forward_picker_opened"] is True
    assert result["safe_to_requeue"] is True


def test_recipient_selection_is_not_automatically_safe() -> None:
    job, diagnostics = _recovery_evidence(recipient_selected=True)
    result = classify_failed_delivery_recovery_evidence(job, diagnostics)
    assert result["classification"] == "manual_review"
    assert "recipient_selected" in result["reasons"]


def test_confirmation_requires_manual_review_without_verified_no_send_evidence() -> None:
    job, diagnostics = _recovery_evidence(confirm_click_count=1)
    result = classify_failed_delivery_recovery_evidence(job, diagnostics)
    assert result["classification"] == "manual_review"
    assert "confirm_clicked" in result["reasons"]


def test_failed_no_send_recovery_is_idempotent_and_creates_no_duplicate_job() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repository = CommercialQueueRepository(Path(tmp) / "queue.db")
        now = "2026-08-01T00:00:00+00:00"
        job, diagnostics = _recovery_evidence()
        with repository.connection() as connection:
            connection.execute("INSERT INTO commercial_campaigns (id,name,platform,status,lifecycle_stage,total_recipients,queued_count,running_count,succeeded_count,failed_count,skipped_count,cancelled_count,created_at,updated_at) VALUES ('campaign_safe','safe','bale','paused','paused',1,0,0,0,1,0,0,?,?)", (now, now))
            connection.execute("INSERT INTO commercial_recipients (id,campaign_id,phone_raw,phone_normalized,import_source,validation_status,created_at,updated_at) VALUES ('recipient_safe','campaign_safe','09121234567','989121234567','test','valid',?,?)", (now, now))
            connection.execute("INSERT INTO commercial_delivery_jobs (id,campaign_id,recipient_id,phone_normalized,idempotency_key,status,priority,attempt_count,max_attempts,last_error_code,failed_step,created_at,updated_at) VALUES ('job_safe','campaign_safe','recipient_safe','989121234567','campaign_safe:989121234567','failed',0,1,1,'element_not_found','wait_recipient_results',?,?)", (now, now))
            connection.execute("INSERT INTO commercial_job_events (id,job_id,campaign_id,recipient_id,event_type,status,diagnostics_json,created_at) VALUES ('event_safe','job_safe','campaign_safe','recipient_safe','forward_failed','failed',?,?)", (json.dumps(diagnostics), now))
            connection.commit()

        first = repository.recover_failed_jobs_without_send("campaign_safe", ["job_safe"])
        second = repository.recover_failed_jobs_without_send("campaign_safe", ["job_safe"])

        assert first["requeued_job_ids"] == ["job_safe"]
        assert second["requeued_count"] == 0
        assert second["classifications"][0]["classification"] == "already_requeued"
        with repository.connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM commercial_delivery_jobs WHERE idempotency_key='campaign_safe:989121234567'").fetchone()[0] == 1
            # A proof that no recipient/send boundary was reached must not
            # consume the only delivery attempt available to its replacement.
            assert connection.execute("SELECT attempt_count FROM commercial_delivery_jobs WHERE id='job_safe'").fetchone()[0] == 0


def test_verified_reconciliation_closes_confirm_uncertain_without_requeue_or_duplicate() -> None:
    """A late provider proof may close ambiguity, but may never create a retry."""
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "queue.db")
        campaign = service.create_campaign({
            "name": "uncertain-reconciliation", "platform": "bale", "status": "running",
            # Reconciliation is a persisted-job proof test; it does not claim
            # an operator-facing account-demand reservation.
            "source_channel_uid": "5613544284", "capacity_reservation": 0,
        })
        service.import_recipients(campaign["id"], ["09351234567"])
        job = service.list_jobs(campaign_id=campaign["id"], limit=10)["items"][0]
        service.repository.complete_job(job["id"], "failed", {
            "result_success": False,
            "verified_forwarded_recipient_count": 0,
            "forward_verified": False,
            "diagnostics_consistent": False,
            "last_error_code": "confirm_uncertain",
            "last_error_message": "send outcome was interrupted before confirmation",
            "manual_review_required": True,
            "safe_to_requeue": False,
        })

        proof = {
            "outcome": "sent",
            "verified_forwarded_recipient_count": 1,
            "trusted_result_key": "provider-message-verified-1",
            "remote_message_id": "provider-message-verified-1",
            "reconciliation_source": "test_provider_reconciliation",
        }
        first = service.reconcile_uncertain_delivery_result(job["id"], proof)
        duplicate = service.reconcile_uncertain_delivery_result(job["id"], proof)
        jobs = service.list_jobs(campaign_id=campaign["id"], limit=10)["items"]

    assert first["applied"] is True
    assert first["reconciled_uncertain_delivery"] is True
    assert duplicate["applied"] is False
    assert duplicate["idempotent"] is True
    assert len(jobs) == 1
    assert jobs[0]["status"] == "succeeded"
    assert not bool(jobs[0]["manual_review_required"])
    assert not bool(jobs[0]["safe_to_requeue"])
    assert jobs[0]["attempt_count"] == 0
