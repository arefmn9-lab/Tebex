from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import modules.automation_engine.commercial_queue.service as commercial_service
from modules.automation_engine.commercial_queue.repository import CommercialQueueRepository
from modules.automation_engine.commercial_queue.service import CampaignLifecycleError, CommercialQueueService
from modules.automation_engine.plugins.bale.contact_store import BaleContactStore


AUTHORIZED_PHONES = {"989304073331", "989050454491", "989377686492"}


class ForbiddenAdapter:
    def __init__(self) -> None:
        self.created = 0
        self.executed = 0

    def create_runtime_session_for_account(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.created += 1
        raise AssertionError("dry-run must not launch Chrome")

    def execute_plan(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.executed += 1
        raise AssertionError("dry-run must not execute adapter")


def _service(path: Path) -> CommercialQueueService:
    adapter = ForbiddenAdapter()
    service = CommercialQueueService(
        repository=CommercialQueueRepository(path),
        orchestrator=lambda **payload: (_ for _ in ()).throw(AssertionError("dry-run must not call plugin/orchestrator")),
        account_auth_checker=lambda account_id: True,
        sleeper=lambda seconds: None,
    )
    service.platform_adapters = {"bale": adapter}
    service.update_global_settings(
        {
            "default_source_channel_uid": "5613544284",
            "deliveries_per_account_round": 2,
            "default_daily_limit_per_account": 10,
            "delay_between_deliveries_seconds": 0,
            "round_cooldown_seconds": 0,
            "session_reuse_enabled": True,
        }
    )
    return service


def _campaign(service: CommercialQueueService) -> dict[str, Any]:
    return service.create_campaign({"name": "Provenance Campaign", "platform": "bale", "status": "running", "source_channel_uid": "5613544284"})


def _authorize(service: CommercialQueueService, recipient_id: str) -> None:
    service.repository.update_recipient_authorization(
        recipient_id,
        {
            "recipient_origin": "user_provided",
            "synthetic_test_data": False,
            "live_execution_authorized": True,
            "live_authorized_at": "2026-07-13T00:00:00+00:00",
            "live_authorized_by": "test",
            "authorization_source": "explicit_user_confirmation",
            "authorization_note": "test authorization",
            "authorization_status": "authorized",
            "should_not_retry": False,
        },
    )


def _provenance_service(tmp: Path, monkeypatch: Any) -> CommercialQueueService:
    contact_store = BaleContactStore(tmp / "contacts.json")
    monkeypatch.setattr(commercial_service, "bale_contact_store", contact_store)
    service = _service(tmp / "provenance.db")
    service.contact_store = contact_store
    account_id = "bale_09211690533"
    contact_store.bulk_add_bale_contacts(account_id, [f"093040731{value:02d}" for value in range(3, 14)])
    campaign = _campaign(service)
    for phone in ["989304073112", "989304073113"]:
        contact_store.update_contact_metadata(account_id, phone, {
            "recipient_origin": "unauthorized_generated",
            "authorization_status": "blocked",
            "synthetic_test_data": True,
            "live_execution_authorized": False,
            "should_not_retry": True,
            "contact_preparation_allowed": False,
            "live_execution_blocked": True,
            "block_reason": "missing_explicit_user_input",
        })
        recipient, job = service.repository.create_recipient_and_job(campaign, phone, phone, f"Trace-{phone[-3:]}", "run-dry-round")
        service.repository.create_job_event({
            "job_id": job["id"],
            "campaign_id": campaign["id"],
            "recipient_id": recipient["id"],
            "event_type": "dry_run_probe",
            "status": "completed",
            "diagnostics": {
                "contact_save_status": "saved",
                "contact_created": True,
                "confirm_click_count": 0,
                "verified_forwarded_recipient_count": 0,
            },
        })
    return service


def test_bale_000010_and_000011_creation_trace_is_found(monkeypatch: Any) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _provenance_service(Path(tmp), monkeypatch)
        trace = service.audit_bale_contact_provenance(["Bale-000010", "Bale-000011"])
        by_name = {item["display_name"]: item for item in trace["items"]}
        assert by_name["Bale-000010"]["normalized_phone"] == "989304073112"
        assert by_name["Bale-000011"]["normalized_phone"] == "989304073113"
        assert by_name["Bale-000010"]["api_endpoint"] == "/automation/campaigns/{campaign_id}/run-dry-round"
        assert by_name["Bale-000011"]["api_endpoint"] == "/automation/campaigns/{campaign_id}/run-dry-round"
        assert by_name["Bale-000010"]["bale_contact_created"] is True
        assert by_name["Bale-000011"]["bale_contact_created"] is True
        assert by_name["Bale-000010"]["confirm_click_count"] == 0
        assert by_name["Bale-000011"]["send_or_forward_action_occurred"] is False


def test_no_user_manifest_means_incident_contacts_are_blocked(monkeypatch: Any) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _provenance_service(Path(tmp), monkeypatch)
        trace = service.audit_bale_contact_provenance(["Bale-000010", "Bale-000011"])
        for item in trace["items"]:
            record = item["contact_store_record"]
            assert record["recipient_origin"] == "unauthorized_generated"
            assert record["authorization_status"] == "blocked"
            assert record["synthetic_test_data"] is True
            assert record["live_execution_authorized"] is False
            assert record["should_not_retry"] is True
            assert record["contact_preparation_allowed"] is False
            assert record["live_execution_blocked"] is True
            assert record["block_reason"] == "missing_explicit_user_input"


def test_only_three_user_authorized_numbers_remain_authorized_in_workspace_db() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "authorized_scope.db")
        campaign = _campaign(service)
        for index, phone in enumerate(sorted(AUTHORIZED_PHONES), start=1):
            recipient, _ = service.repository.create_recipient_and_job(
                campaign,
                phone,
                phone,
                f"Authorized-{index:03d}",
                "test",
            )
            _authorize(service, recipient["id"])
        with service.repository.connection() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT phone_normalized
                FROM commercial_recipients
                WHERE live_execution_authorized = 1
                  AND authorization_status = 'authorized'
                  AND COALESCE(synthetic_test_data, 0) = 0
                """
            ).fetchall()
    assert {str(row["phone_normalized"]) for row in rows} == AUTHORIZED_PHONES


def test_dry_run_is_read_only_and_creates_only_audit_record() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "dry.db")
        campaign = _campaign(service)
        before_recipients = len(service.repository.list_recipients(campaign["id"], None, 100, 0))
        before_jobs = len(service.repository.list_campaign_jobs_all(campaign["id"]))
        result = service.check_campaign_without_sending(campaign["id"])
        after_recipients = len(service.repository.list_recipients(campaign["id"], None, 100, 0))
        after_jobs = len(service.repository.list_campaign_jobs_all(campaign["id"]))
        assert result["dry_run"] is True
        assert result["diagnostics"]["read_only"] is True
        assert result["diagnostics"]["forbidden_actions"]["recipient_created"] is False
        assert result["diagnostics"]["forbidden_actions"]["stable_name_allocated"] is False
        assert result["diagnostics"]["forbidden_actions"]["contact_store_mutated"] is False
        assert result["diagnostics"]["forbidden_actions"]["delivery_job_created"] is False
        assert result["diagnostics"]["forbidden_actions"]["browser_launched"] is False
        assert result["diagnostics"]["forbidden_actions"]["adapter_called"] is False
        assert result["diagnostics"]["forbidden_actions"]["message_sent"] is False
        assert before_recipients == after_recipients
        assert before_jobs == after_jobs


def test_recipient_preview_is_in_memory_and_confirmation_creates_manifest() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "manifest.db")
        campaign = _campaign(service)
        preview = service.preview_campaign_recipients(campaign["id"], ["09304073331", "09304073331", "bad"])
        assert preview["read_only"] is True
        assert preview["valid_count"] == 1
        assert preview["duplicate_count"] == 1
        assert preview["invalid_count"] == 1
        assert len(service.repository.list_recipients(campaign["id"], None, 100, 0)) == 0

        result = service.confirm_campaign_recipients(campaign["id"], ["09304073331"], confirmation_checked=True)
        manifest = result["manifest"]
        assert manifest["confirmation_status"] == "confirmed"
        assert json.loads(manifest["normalized_phones_json"]) == ["989304073331"]
        recipients = service.repository.list_recipients(campaign["id"], None, 100, 0)
        assert len(recipients) == 1
        assert recipients[0]["input_manifest_id"] == manifest["manifest_id"]
        assert recipients[0]["input_provenance_status"] == "confirmed_manifest"


def test_confirmation_checkbox_is_required_and_actions_do_not_chain() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "checkbox.db")
        campaign = _campaign(service)
        try:
            service.confirm_campaign_recipients(campaign["id"], ["09304073331"], confirmation_checked=False)
            raise AssertionError("confirmation must be required")
        except CampaignLifecycleError as exc:
            assert exc.error_code == "recipient_input_manifest_required"
        assert service.prepare_campaign_contacts_explicit(campaign["id"])["error_code"] == "unauthorized_contact_preparation"
        assert service.final_review(campaign["id"])["live_execution_enabled"] is False
        try:
            service.request_send_approval(campaign["id"])
            raise AssertionError("approval request must require a final review hash")
        except CampaignLifecycleError as exc:
            assert exc.error_code == "final_review_required"


def test_contact_preparation_and_live_validation_require_confirmed_manifest() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "gate.db")
        campaign = _campaign(service)
        recipient, job = service.repository.create_recipient_and_job(campaign, "09304073331", "989304073331", "Bale-000001", "manual")
        _authorize(service, recipient["id"])
        details = service.repository.get_job_with_recipient(job["id"])
        assert service.validate_live_recipient_authorization(job, details, execution_mode="real_send")["reason"] == "recipient_input_manifest_required"
        assert service.validate_recipient_provenance_for_contact_preparation(recipient)["error_code"] == "recipient_input_manifest_required"

        confirmed = service.confirm_campaign_recipients(campaign["id"], ["09305000000"], confirmation_checked=True)
        other_recipient = confirmed["import_result"]["created_recipients"][0]
        other_job = confirmed["import_result"]["created_jobs"][0]
        _authorize(service, other_recipient["id"])
        other_details = service.repository.get_job_with_recipient(other_job["id"])
        assert service.validate_live_recipient_authorization(other_job, other_details, execution_mode="real_send")["ok"] is True
        assert service.validate_recipient_provenance_for_contact_preparation(other_recipient)["error_code"] == "unauthorized_contact_preparation"


if __name__ == "__main__":
    test_bale_000010_and_000011_creation_trace_is_found()
    test_no_user_manifest_means_incident_contacts_are_blocked()
    test_only_three_user_authorized_numbers_remain_authorized_in_workspace_db()
    test_dry_run_is_read_only_and_creates_only_audit_record()
    test_recipient_preview_is_in_memory_and_confirmation_creates_manifest()
    test_confirmation_checkbox_is_required_and_actions_do_not_chain()
    test_contact_preparation_and_live_validation_require_confirmed_manifest()
    print("Commercial recipient provenance and dry-run tests passed")
