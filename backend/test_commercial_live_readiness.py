from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app.main import app
from app.routes import automation as automation_routes
from fastapi.testclient import TestClient
from modules.automation_engine.commercial_queue.repository import CommercialQueueRepository
from modules.automation_engine.commercial_queue.service import CommercialQueueService


class AdapterProbe:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, **payload: object) -> dict:
        self.calls.append(dict(payload))
        return {"success": True, "forward_verified": True, "diagnostics_consistent": True}


def _service(path: Path, probe: AdapterProbe | None = None, live_enabled: bool = True) -> CommercialQueueService:
    service = CommercialQueueService(
        repository=CommercialQueueRepository(path),
        orchestrator=probe or AdapterProbe(),
        account_auth_checker=lambda account_id: True,
        sleeper=lambda seconds: None,
    )
    service.update_global_settings(
        {
            "default_source_channel_uid": "5613544284",
            "deliveries_per_account_round": 5,
            "default_daily_limit_per_account": 10,
            "delay_between_deliveries_seconds": 0,
            "round_cooldown_seconds": 0,
            "max_concurrent_accounts": 2,
            "live_campaign_execution_enabled": live_enabled,
        }
    )
    service.update_account_settings(
        "bale_09211690533",
        {
            "enabled": True,
            "daily_limit_override": 10,
            "deliveries_per_round_override": 5,
            "source_channel_uid_override": "5613544284",
            "round_cooldown_override": 0,
        },
    )
    return service


def _campaign(service: CommercialQueueService, **overrides: object) -> dict:
    payload = {
        "name": "Live Readiness",
        "platform": "bale",
        "status": "running",
        "source_channel_uid": "5613544284",
        "capacity_reservation": 10,
    }
    payload.update(overrides)
    return service.create_campaign(payload)


def _recipient_job(service: CommercialQueueService, campaign: dict, phone: str, name: str) -> tuple[dict, dict]:
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
            "live_authorized_by": "tester",
            "authorization_source": "explicit_user_confirmation",
            "authorization_note": "explicit test approval",
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


def _synthetic(service: CommercialQueueService, recipient_id: str) -> None:
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


def _set_job_field(service: CommercialQueueService, job_id: str, **fields: object) -> None:
    assignments = ", ".join(f"{key} = ?" for key in fields)
    with service.repository.connection() as connection:
        connection.execute(f"UPDATE commercial_delivery_jobs SET {assignments} WHERE id = ?", (*fields.values(), job_id))
        connection.commit()


def test_feature_flag_unauthorized_synthetic_revoked_and_source_blocks_readiness() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "live.db", live_enabled=False)
        campaign = _campaign(service, source_channel_uid=None)
        real, _ = _recipient_job(service, campaign, "989304073331", "Bale-000001")
        synthetic, _ = _recipient_job(service, campaign, "989304073332", "Bale-000002")
        revoked, _ = _recipient_job(service, campaign, "989304073333", "Bale-000003")
        _authorize(service, real["id"])
        _synthetic(service, synthetic["id"])
        service.revoke_recipient_live(revoked["id"], "revoked before launch")
        readiness = service.validate_live_execution_readiness(campaign["id"])
        assert "live_execution_feature_disabled" in readiness["blocking_reasons"]
        assert readiness["live_authorized_job_count"] == 1
        assert readiness["synthetic_job_count"] == 0
        assert readiness["revoked_job_count"] == 0


def test_duplicate_idempotency_uncertain_and_should_not_retry_block_readiness() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "live.db")
        campaign = _campaign(service)
        old_recipient, old_job = _recipient_job(service, campaign, "989304073331", "Bale-000001")
        _authorize(service, old_recipient["id"])
        service.repository.complete_job(old_job["id"], "succeeded", {"result_success": True, "verified_forwarded_recipient_count": 1, "forward_verified": True, "diagnostics_consistent": True})
        _set_job_field(service, old_job["id"], idempotency_key="legacy-success-key")
        duplicate_recipient, duplicate_job = _recipient_job(service, campaign, "989304073331", "Bale-000001")
        key_recipient, key_job = _recipient_job(service, campaign, "989304073334", "Bale-000004")
        uncertain_recipient, uncertain_job = _recipient_job(service, campaign, "989304073335", "Bale-000005")
        for recipient in [duplicate_recipient, key_recipient, uncertain_recipient]:
            _authorize(service, recipient["id"])
        _set_job_field(service, key_job["id"], idempotency_key="same-key")
        _set_job_field(service, uncertain_job["id"], manual_review_required=1, last_error_code="confirm_uncertain", should_not_retry=1)
        readiness = service.validate_live_execution_readiness(campaign["id"])
        reasons = set(readiness["blocking_reasons"])
        assert "recipient_already_delivered" in reasons
        assert "duplicate_live_delivery_blocked" in reasons
        assert "uncertain_delivery_requires_review" in reasons
        assert readiness["duplicate_delivery_risk_count"] >= 2


def test_account_health_daily_limit_and_eligible_account_rules() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "live.db")
        campaign = _campaign(service)
        recipient, _ = _recipient_job(service, campaign, "989304073331", "Bale-000001")
        _authorize(service, recipient["id"])
        assert service.validate_live_execution_readiness(campaign["id"])["ready"] is True
        service.account_health.set_status("bale_09211690533", "auth_required", "test")
        unhealthy = service.validate_live_execution_readiness(campaign["id"])
        assert "bale_09211690533" in unhealthy["unhealthy_account_ids"]
        assert "no_eligible_account" in unhealthy["blocking_reasons"]
        service.account_health.set_status("bale_09211690533", "healthy", "reset")
        service.update_account_settings("bale_09211690533", {"current_daily_sent_count": 10})
        limited = service.validate_live_execution_readiness(campaign["id"])
        assert "daily_capacity_insufficient" in limited["blocking_reasons"]


def test_authorized_queued_job_passes_validation_and_max_jobs_is_validated() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "live.db")
        campaign = _campaign(service)
        recipient, _ = _recipient_job(service, campaign, "989304073331", "Bale-000001")
        _authorize(service, recipient["id"])
        good = service.validate_live_execution_readiness(campaign["id"], requested_max_jobs=1)
        bad = service.validate_live_execution_readiness(campaign["id"], requested_max_jobs=0)
        assert good["ready"] is True
        assert good["live_authorized_job_count"] == 1
        assert good["estimated_jobs_this_run"] == 1
        assert "invalid_requested_max_jobs" in bad["blocking_reasons"]


def test_approval_lifecycle_expiration_revocation_consumption_and_scope_checks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "live.db")
        campaign = _campaign(service)
        recipient, _ = _recipient_job(service, campaign, "989304073331", "Bale-000001")
        _authorize(service, recipient["id"])
        try:
            service.create_live_execution_approval(campaign["id"], "", "note")
        except ValueError as exc:
            assert str(exc) == "requested_by_required"
        else:
            raise AssertionError("requester is required")
        approval = service.create_live_execution_approval(campaign["id"], "tester", "readiness reviewed", ["bale_09211690533"], 1)
        assert approval["approval_status"] == "pending"
        approved = service.approve_live_execution_approval(approval["approval_id"], "reviewer")
        assert approved["approval_status"] == "approved"
        wrong_scope = service.validate_live_approval_for_execution(approval["approval_id"], account_ids=[], max_jobs=1)
        wrong_jobs = service.validate_live_approval_for_execution(approval["approval_id"], account_ids=["bale_09211690533"], max_jobs=2)
        assert "approval_account_scope_mismatch" in wrong_scope["blocking_reasons"]
        assert "approval_max_jobs_scope_mismatch" in wrong_jobs["blocking_reasons"]
        consumed = service.consume_live_execution_approval(approval["approval_id"])
        assert consumed["consumed"] is True
        second = service.consume_live_execution_approval(approval["approval_id"])
        assert second["consumed"] is False
        revoked = service.create_live_execution_approval(campaign["id"], "tester", "revoke me")
        service.revoke_live_execution_approval(revoked["approval_id"], "not needed")
        assert service.get_live_execution_approval(revoked["approval_id"])["approval_status"] == "revoked"
        expiring = service.create_live_execution_approval(campaign["id"], "tester", "expire me")
        service.repository.update_live_execution_approval(expiring["approval_id"], {"expires_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()})
        assert service.get_live_execution_approval(expiring["approval_id"])["approval_status"] == "expired"


def test_apis_launch_zero_browsers_adapters_or_messages_and_expose_no_secrets() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        probe = AdapterProbe()
        service = _service(Path(tmp) / "live.db", probe, live_enabled=False)
        campaign = _campaign(service)
        recipient, job = _recipient_job(service, campaign, "989304073331", "Bale-000001")
        _authorize(service, recipient["id"])
        service.repository.complete_job(job["id"], "succeeded", {"result_success": True, "verified_forwarded_recipient_count": 1, "forward_verified": True, "diagnostics_consistent": True})
        previous = automation_routes.commercial_queue_service
        automation_routes.commercial_queue_service = service
        try:
            client = TestClient(app)
            readiness = client.post(f"/automation/campaigns/{campaign['id']}/validate-live-readiness", json={"account_ids": None, "max_jobs": None}).json()
            approval = client.post(
                f"/automation/campaigns/{campaign['id']}/live-approvals",
                json={"requested_by": "tester", "approval_note": "control-plane only"},
            ).json()
            listed = client.get("/automation/live-approvals").json()
            execute = client.post(f"/automation/live-approvals/{approval['approval_id']}/execute")
            assert "live_execution_feature_disabled" in readiness["blocking_reasons"]
            assert approval["approval_status"] == "pending"
            assert listed["items"]
            assert execute.status_code == 409
            body = str(approval) + str(listed)
            assert "cookies" not in body
            assert "localStorage" not in body
            assert "authorization" not in body.lower()
            assert probe.calls == []
            assert service.runtime_session_manager.list_active_sessions() == []
        finally:
            automation_routes.commercial_queue_service = previous


def test_approved_execution_sets_scoped_final_send_authorization() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        probe = AdapterProbe()
        service = _service(Path(tmp) / "live.db", probe)
        campaign = _campaign(service, capacity_reservation=1)
        recipient, _job = _recipient_job(service, campaign, "989304073336", "Bale-000006")
        _authorize(service, recipient["id"])
        approval = service.create_live_execution_approval(
            campaign["id"],
            "tester",
            "one controlled recipient",
            ["bale_09211690533"],
            1,
        )
        service.approve_live_execution_approval(approval["approval_id"], "reviewer")

        result = service.execute_approved_live_campaign(approval["approval_id"])

        assert result["executed"] is True
        assert len(probe.calls) == 1
        plan = probe.calls[0]["execution_plan"]
        assert "allow_final_send" not in plan
        assert plan["live_approval_id"] == approval["approval_id"]
        assert result["campaign_capacity_reservation"]["remaining_capacity"] == 0


def test_approved_live_execution_endpoint_runs_bounded_campaign() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        probe = AdapterProbe()
        service = _service(Path(tmp) / "live.db", probe)
        campaign = _campaign(service, capacity_reservation=1)
        recipient, _job = _recipient_job(service, campaign, "989304073338", "Bale-000008")
        _authorize(service, recipient["id"])
        approval = service.create_live_execution_approval(
            campaign["id"],
            "tester",
            "endpoint controlled recipient",
            ["bale_09211690533"],
            1,
        )
        service.approve_live_execution_approval(approval["approval_id"], "reviewer")
        previous = automation_routes.commercial_queue_service
        automation_routes.commercial_queue_service = service
        try:
            response = TestClient(app).post(f"/automation/live-approvals/{approval['approval_id']}/execute")
        finally:
            automation_routes.commercial_queue_service = previous

        assert response.status_code == 200
        assert response.json()["executed"] is True
        assert "allow_final_send" not in probe.calls[0]["execution_plan"]


def test_production_worker_round_uses_explicit_real_send_mode() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        probe = AdapterProbe()
        service = _service(Path(tmp) / "live.db", probe)
        campaign = _campaign(service, capacity_reservation=1)
        recipient, _job = _recipient_job(service, campaign, "989304073337", "Bale-000007")
        _authorize(service, recipient["id"])

        service.run_account_round("bale_09211690533", campaign["id"], max_jobs=1)

        assert len(probe.calls) == 1
        assert "dry_run" not in probe.calls[0]["execution_plan"]
        assert probe.calls[0]["execution_mode"] == "real_send"
        assert "allow_final_send" not in probe.calls[0]["execution_plan"]


def test_multiple_authenticated_healthy_accounts_enter_worker_pool() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = CommercialQueueService(
            repository=CommercialQueueRepository(Path(tmp) / "live.db"),
            account_auth_checker=lambda _account_id: False,
            sleeper=lambda _seconds: None,
        )
        canonical_accounts = [
            {"account_id": "bale_worker_a", "daily_limit": 7},
            {"account_id": "bale_worker_b", "daily_limit": 9},
        ]
        operational = {
            "authentication_status": "authenticated",
            "session_persistence_status": "verified",
            "verification_expired": False,
            "retired": False,
        }
        for account in canonical_accounts:
            service.update_account_settings(
                account["account_id"],
                {"enabled": False, "daily_limit_override": account["daily_limit"]},
            )
        with (
            patch(
                "modules.automation_engine.commercial_queue.service.bale_account_store.list_accounts",
                return_value=canonical_accounts,
            ),
            patch(
                "modules.automation_engine.commercial_queue.service.bale_onboarding_service.get_account",
                side_effect=lambda account_id: {"account_id": account_id, **operational},
            ),
        ):
            result = service.refresh_authenticated_bale_worker_eligibility()

        assert set(result["enabled_account_ids"]) == {"bale_worker_a", "bale_worker_b"}
        assert service.resolve_account_settings("bale_worker_a")["enabled"] is True
        assert service.resolve_account_settings("bale_worker_b")["enabled"] is True


def test_bale_000001_historical_success_remains_unchanged() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "live.db")
        campaign = _campaign(service)
        recipient, job = _recipient_job(service, campaign, "989304073331", "Bale-000001")
        _authorize(service, recipient["id"])
        service.repository.complete_job(job["id"], "succeeded", {"result_success": True, "verified_forwarded_recipient_count": 1, "forward_verified": True, "diagnostics_consistent": True})
        before = service.repository.get_job(job["id"])
        readiness = service.validate_live_execution_readiness(campaign["id"])
        after = service.repository.get_job(job["id"])
        assert readiness["total_queued_jobs"] == 0
        assert before["status"] == after["status"] == "succeeded"
        assert before["attempt_count"] == after["attempt_count"]
        assert before["verified_forwarded_recipient_count"] == after["verified_forwarded_recipient_count"]


if __name__ == "__main__":
    test_feature_flag_unauthorized_synthetic_revoked_and_source_blocks_readiness()
    test_duplicate_idempotency_uncertain_and_should_not_retry_block_readiness()
    test_account_health_daily_limit_and_eligible_account_rules()
    test_authorized_queued_job_passes_validation_and_max_jobs_is_validated()
    test_approval_lifecycle_expiration_revocation_consumption_and_scope_checks()
    test_apis_launch_zero_browsers_adapters_or_messages_and_expose_no_secrets()
    test_bale_000001_historical_success_remains_unchanged()
    print("Commercial live readiness tests passed")
