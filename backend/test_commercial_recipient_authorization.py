from __future__ import annotations

import tempfile
from pathlib import Path

from app.main import app
from app.routes import automation as automation_routes
from fastapi.testclient import TestClient
from modules.automation_engine.commercial_queue.repository import CommercialQueueRepository
from modules.automation_engine.commercial_queue.service import CommercialQueueService


class OrchestratorStub:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, **payload: object) -> dict:
        self.calls.append(dict(payload))
        return {
            "success": True,
            "forward_verified": True,
            "diagnostics_consistent": True,
            "verified_forwarded_recipient_count": 1,
            "confirm_click_count": 1,
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
            "deliveries_per_account_round": 10,
            "default_daily_limit_per_account": 20,
            "delay_between_deliveries_seconds": 0,
            "round_cooldown_seconds": 0,
            "session_reuse_enabled": False,
        }
    )
    service.update_account_settings("bale_a", {"daily_limit_override": 20, "deliveries_per_round_override": 10, "source_channel_uid_override": "5613544284"})
    return service


def _campaign(service: CommercialQueueService) -> dict:
    return service.create_campaign({"name": "Authorization Campaign", "platform": "bale", "status": "running", "source_channel_uid": "5613544284", "capacity_reservation": 10})


def _add_recipient(service: CommercialQueueService, campaign: dict, phone: str, name: str | None = None) -> tuple[dict, dict]:
    return service.repository.create_recipient_and_job(campaign, phone, phone, name, "manual")


def _authorize(service: CommercialQueueService, recipient_id: str) -> None:
    recipient = service.repository.get_recipient(recipient_id)
    manifest = service.repository.create_recipient_input_manifest(
        campaign_id=recipient["campaign_id"],
        phones=[recipient["phone_normalized"]],
        batch_id=None,
        submitted_by="test",
        source_type="test_manifest",
        confirmation_status="confirmed",
        confirmed_by="test",
    )
    service.repository.update_recipient_authorization(
        recipient_id,
        {
            "recipient_origin": "user_provided",
            "synthetic_test_data": False,
            "live_execution_authorized": True,
            "live_authorized_at": "2026-07-12T00:00:00+00:00",
            "live_authorized_by": "test",
            "authorization_source": "explicit_user_confirmation",
            "authorization_note": "test authorization",
            "authorization_status": "authorized",
            "should_not_retry": False,
            "input_manifest_id": manifest["manifest_id"],
            "input_manifest_hash": manifest["manifest_hash"],
            "input_sequence": 1,
            "input_provenance_status": "confirmed_manifest",
        },
    )
    service.repository.update_jobs_authorization_by_recipient(
        recipient_id,
        {
            "input_manifest_id": manifest["manifest_id"],
            "input_manifest_hash": manifest["manifest_hash"],
            "input_sequence": 1,
            "input_provenance_status": "confirmed_manifest",
        },
    )


def _mark_synthetic(service: CommercialQueueService, recipient_id: str) -> None:
    service.repository.update_recipient_authorization(
        recipient_id,
        {
            "recipient_origin": "synthetic_test",
            "synthetic_test_data": True,
            "live_execution_authorized": False,
            "authorization_status": "dry_run_only",
            "should_not_retry": True,
        },
    )


def test_authorized_real_recipient_passes_normal_mode_validation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "auth.db")
        campaign = _campaign(service)
        recipient, job = _add_recipient(service, campaign, "989304073331", "Bale-000001")
        _authorize(service, recipient["id"])
        details = service.repository.get_job_with_recipient(job["id"])
        assert service.validate_live_recipient_authorization(job, details, execution_mode="real_send")["ok"] is True


def test_synthetic_and_unauthorized_rejected_in_normal_mode_but_allowed_in_dry_run() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "auth.db")
        campaign = _campaign(service)
        synthetic_recipient, synthetic_job = _add_recipient(service, campaign, "989304073332", "Bale-000002")
        unauthorized_recipient, unauthorized_job = _add_recipient(service, campaign, "989304073333", "Bale-000003")
        _mark_synthetic(service, synthetic_recipient["id"])
        for job in [synthetic_job, unauthorized_job]:
            details = service.repository.get_job_with_recipient(job["id"])
            normal = service.validate_live_recipient_authorization(job, details, execution_mode="real_send")
            dry = service.validate_live_recipient_authorization(job, details, execution_mode="simulation")
            assert normal["ok"] is False
            assert normal["error_code"] in {"recipient_input_manifest_required", "live_recipient_authorization_required"}
            assert dry["ok"] is True
            assert dry["simulation_only"] is True


def test_production_worker_rejects_simulation_override_and_does_not_authorize() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "auth.db")
        campaign = _campaign(service)
        recipient, job = _add_recipient(service, campaign, "989304073334", "Bale-000004")
        try:
            service.run_account_round("bale_a", campaign["id"], max_jobs=1, execution_mode="simulation")
        except TypeError:
            pass
        else:
            raise AssertionError("Production worker must reject execution_mode overrides")
        rerun_campaign = _campaign(service)
        recipient2, job2 = _add_recipient(service, rerun_campaign, "989304073334", "Bale-000004")
        details = service.repository.get_job_with_recipient(job2["id"])
        check = service.validate_live_recipient_authorization(job2, details, execution_mode="real_send")
        assert check["ok"] is False
        assert check["authorization"]["live_execution_authorized"] is False


def test_import_preview_and_default_confirm_do_not_authorize_explicit_confirm_does() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "auth.db")
        campaign = _campaign(service)
        preview = service.preview_paste_import(campaign["id"], "09304073331")
        assert preview["created_recipient_count"] == 0
        confirmed = service.confirm_import_batch(preview["batch_id"])
        recipient = confirmed["created_recipients"][0]
        auth = service.get_recipient_authorization(recipient["id"])
        assert auth["live_execution_authorized"] is False
        assert auth["authorization_status"] == "authorization_required"

        campaign2 = _campaign(service)
        preview2 = service.preview_paste_import(campaign2["id"], "09304073332")
        confirmed2 = service.confirm_import_batch(preview2["batch_id"], authorize_for_live_execution=True, authorized_by="tester", authorization_note="explicit")
        auth2 = service.get_recipient_authorization(confirmed2["created_recipients"][0]["id"])
        assert auth2["live_execution_authorized"] is True
        assert auth2["authorization_status"] == "authorized"


def test_synthetic_import_cannot_be_authorized_directly() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "auth.db")
        campaign = _campaign(service)
        preview = service.preview_paste_import(campaign["id"], "09304073332", import_source="synthetic_test")
        try:
            service.confirm_import_batch(preview["batch_id"], authorize_for_live_execution=True, authorization_note="no")
        except ValueError as exc:
            assert str(exc) == "synthetic_import_cannot_be_authorized"
        else:
            raise AssertionError("synthetic import authorization should fail")


def test_authorization_rejection_before_adapter_execution_and_worker_continues() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        orchestrator = OrchestratorStub()
        service = _service(Path(tmp) / "auth.db", orchestrator)
        campaign = _campaign(service)
        bad_recipient, bad_job = _add_recipient(service, campaign, "989304073332", "Bale-000002")
        good_recipient, good_job = _add_recipient(service, campaign, "989304073331", "Bale-000001")
        _mark_synthetic(service, bad_recipient["id"])
        _authorize(service, good_recipient["id"])
        result = service.run_account_round("bale_a", campaign["id"], max_jobs=2)
        jobs = {job["id"]: job for job in service.list_jobs(campaign_id=campaign["id"], limit=10)["items"]}
        assert result["processed_count"] == 1
        assert len(orchestrator.calls) == 1
        assert jobs[bad_job["id"]]["status"] == "skipped"
        assert jobs[bad_job["id"]]["last_error_code"] in {"recipient_input_manifest_required", "recipient_provenance_unknown", "live_recipient_authorization_required"}
        assert jobs[bad_job["id"]]["attempt_count"] == 0
        assert jobs[bad_job["id"]]["should_not_retry"] == 1
        assert jobs[good_job["id"]]["status"] == "succeeded"
        assert service.get_account_health("bale_a")["health_status"] == "healthy"
        assert result["round_diagnostics"]["browser_start_count"] == 0


def test_campaign_validation_reports_and_blocks_unsafe_queued_jobs() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "auth.db")
        campaign = _campaign(service)
        synthetic_recipient, _ = _add_recipient(service, campaign, "989304073332", "Bale-000002")
        _mark_synthetic(service, synthetic_recipient["id"])
        validation = service.validate_campaign_start(campaign["id"])
        dry = service.validate_campaign_recipient_authorization(campaign["id"], execution_mode="simulation")
        assert validation["unauthorized_job_count"] == 0
        assert validation["synthetic_test_job_count"] == 0
        assert "campaign_has_no_deliverable_jobs" in validation["blocking_reasons"]
        assert dry["simulation_eligible_job_count"] == 0
        assert dry["blocking_jobs"] == []


def test_revoke_prevents_future_normal_execution() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "auth.db")
        campaign = _campaign(service)
        recipient, job = _add_recipient(service, campaign, "989304073331", "Bale-000001")
        _authorize(service, recipient["id"])
        service.revoke_recipient_live(recipient["id"], "operator revoked")
        details = service.repository.get_job_with_recipient(job["id"])
        assert service.validate_live_recipient_authorization(job, details, execution_mode="real_send")["ok"] is False


def test_migration_dry_run_apply_idempotent_and_preserves_historical_jobs() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "auth.db")
        campaign = service.create_campaign({"id": "campaign_7b944764c789", "name": "Controlled Live Forward Verification", "platform": "bale", "status": "completed", "source_channel_uid": "5613544284"})
        r1, j1 = _add_recipient(service, campaign, "989304073331", "Bale-000001")
        r2, j2 = _add_recipient(service, campaign, "989304073332", "Bale-000002")
        service.repository.complete_job(j1["id"], "succeeded", {"result_success": True, "verified_forwarded_recipient_count": 1, "forward_verified": True, "diagnostics_consistent": True})
        service.repository.complete_job(j2["id"], "failed", {"result_success": False, "verified_forwarded_recipient_count": 0, "forward_verified": False, "diagnostics_consistent": True, "last_error_code": "recipient_not_found", "last_error_message": "No exact recipient match was found"})
        before_success = service.repository.get_job(j1["id"])
        dry = service.migrate_phase5d_recipient_authorizations(dry_run=True)
        assert dry["changed"] is True
        assert service.repository.get_recipient(r1["id"])["authorization_status"] == "authorization_required"
        applied = service.migrate_phase5d_recipient_authorizations(dry_run=False)
        assert applied["changed"] is True
        assert service.migrate_phase5d_recipient_authorizations(dry_run=True)["changed"] is False
        after_success = service.repository.get_job(j1["id"])
        failed = service.repository.get_job(j2["id"])
        assert after_success["status"] == before_success["status"] == "succeeded"
        assert after_success["attempt_count"] == before_success["attempt_count"]
        assert failed["status"] == "failed"
        assert failed["should_not_retry"] == 1


def test_authorization_api_exposes_safe_fields_only() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "auth.db")
        campaign = _campaign(service)
        recipient, _ = _add_recipient(service, campaign, "989304073331", "Bale-000001")
        previous = automation_routes.commercial_queue_service
        automation_routes.commercial_queue_service = service
        try:
            client = TestClient(app)
            payload = client.get(f"/automation/recipients/{recipient['id']}/authorization").json()
            assert "cookies" not in payload
            assert "localStorage" not in payload
            assert payload["recipient_id"] == recipient["id"]
            authorized = client.post(f"/automation/recipients/{recipient['id']}/authorize-live", json={"authorization_note": "explicit"}).json()
            assert authorized["live_execution_authorized"] is True
        finally:
            automation_routes.commercial_queue_service = previous


if __name__ == "__main__":
    test_authorized_real_recipient_passes_normal_mode_validation()
    test_synthetic_and_unauthorized_rejected_in_normal_mode_but_allowed_in_dry_run()
    test_contact_store_display_name_and_previous_dry_run_do_not_authorize()
    test_import_preview_and_default_confirm_do_not_authorize_explicit_confirm_does()
    test_synthetic_import_cannot_be_authorized_directly()
    test_authorization_rejection_before_adapter_execution_and_worker_continues()
    test_campaign_validation_reports_and_blocks_unsafe_queued_jobs()
    test_revoke_prevents_future_normal_execution()
    test_migration_dry_run_apply_idempotent_and_preserves_historical_jobs()
    test_authorization_api_exposes_safe_fields_only()
    print("Commercial recipient authorization tests passed")
