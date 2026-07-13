from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from modules.automation_engine.commercial_queue.repository import CommercialQueueRepository
from modules.automation_engine.commercial_queue.service import CampaignLifecycleError, CommercialQueueService


ACCOUNT_ID = "bale_approval"


class ForbiddenAdapter:
    def __init__(self) -> None:
        self.created = 0
        self.executed = 0

    def create_runtime_session_for_account(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.created += 1
        raise AssertionError("approval lifecycle must not launch Chrome")

    def execute_plan(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.executed += 1
        raise AssertionError("approval lifecycle must not execute adapter")


def _config(uid: str = "5613544284") -> dict[str, Any]:
    return {
        "source": {"platform": "bale", "source_channel_uid": uid, "source_channel_url": f"https://web.bale.ai/chat?uid={uid}", "source_channel_label": "approval_source", "source_origin": "explicit_user_selection"},
        "accounts": {"allowed_account_ids": [ACCOUNT_ID], "account_selection_strategy": "priority_round_robin", "max_concurrent_accounts": 1},
        "delivery": {"daily_delivery_limit": 10, "deliveries_per_round": 1, "max_jobs_per_execution": 1, "stop_on_first_non_success": True, "automatic_retry": False, "retry_policy": {"max_attempts": 1}},
        "timing": {"timezone": "Asia/Tehran", "active_window_start": "00:00", "active_window_end": "23:59", "weekdays": [0, 1, 2, 3, 4, 5, 6], "min_interval_seconds": 0, "max_interval_seconds": 0, "cooldown_seconds": 0, "catch_up_policy": "skip"},
        "recipients": {"require_live_authorization": True, "require_verified_contact": True, "allow_synthetic": False, "deduplication_policy": "campaign_phone_unique"},
        "safety": {"require_live_readiness": True, "require_approval": True, "uncertain_delivery_policy": "stop_manual_review", "duplicate_delivery_policy": "block"},
        "platform": {"adapter_name": "bale", "adapter_configuration": {"source_uid": uid}, "session_reuse_enabled": True},
        "ai": {"ai_access_mode": "assist_only", "allowed_agent_ids": [], "ai_may_create_draft": True, "ai_may_request_approval": False, "ai_may_execute": False, "human_approval_required": True},
        "feature_flags": {"required_feature_flags": []},
    }


def _service(path: Path) -> tuple[CommercialQueueService, ForbiddenAdapter]:
    adapter = ForbiddenAdapter()
    service = CommercialQueueService(
        repository=CommercialQueueRepository(path),
        orchestrator=lambda **payload: (_ for _ in ()).throw(AssertionError("approval must not call plugin/orchestrator")),
        account_auth_checker=lambda account_id: True,
        sleeper=lambda seconds: None,
    )
    service.platform_adapters["bale"] = adapter
    service.update_global_settings({"default_source_channel_uid": "global", "session_reuse_enabled": True})
    service.update_account_settings(ACCOUNT_ID, {"enabled": True, "daily_limit_override": 10, "deliveries_per_round_override": 1})
    return service, adapter


def _ready_campaign(service: CommercialQueueService, uid: str = "5613544284") -> dict[str, Any]:
    campaign = service.create_campaign({"name": "Approval Lifecycle", "platform": "bale", "status": "draft", "source_channel_uid": uid})
    confirmed = service.confirm_campaign_recipients(campaign["id"], ["09050454491"], confirmation_checked=True)
    recipient = confirmed["import_result"]["created_recipients"][0]
    job = confirmed["import_result"]["created_jobs"][0]
    auth = {
        "recipient_origin": "user_provided",
        "synthetic_test_data": False,
        "live_execution_authorized": True,
        "authorization_status": "authorized",
        "should_not_retry": False,
        "bale_contact_verified": True,
        "bale_verification_status": "verified",
    }
    service.repository.update_recipient_authorization(recipient["id"], auth)
    service.repository.update_job_authorization_metadata(job["id"], auth)
    revision = service.create_or_update_campaign_configuration_draft(campaign["id"], _config(uid))
    service.validate_campaign_configuration(campaign["id"], revision["revision_id"])
    service.approve_campaign_configuration_revision(campaign["id"], revision["revision_id"])
    service.create_execution_configuration_snapshot(campaign["id"], revision_id=revision["revision_id"])
    return campaign


def _counts(service: CommercialQueueService, campaign_id: str) -> dict[str, int]:
    return {
        "jobs": len(service.repository.list_campaign_jobs_all(campaign_id)),
        "approvals": len(service.repository.list_live_execution_approvals(campaign_id)),
        "queued": sum(1 for job in service.repository.list_campaign_jobs_all(campaign_id) if job.get("status") == "queued"),
    }


def test_final_review_read_only_and_hash_generated() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, adapter = _service(Path(tmp) / "approval.db")
        campaign = _ready_campaign(service)
        before = _counts(service, campaign["id"])
        review = service.final_review(campaign["id"])
        after = _counts(service, campaign["id"])
        assert review["read_only"] is True
        assert review["final_review_hash"]
        assert review["validation"]["ok"] is True
        assert before == after
        assert adapter.created == 0 and adapter.executed == 0


def test_request_approval_records_only_no_execution_side_effects() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, adapter = _service(Path(tmp) / "approval.db")
        campaign = _ready_campaign(service)
        review = service.final_review(campaign["id"])
        before = _counts(service, campaign["id"])
        result = service.request_send_approval(campaign["id"], review["final_review_hash"], requested_by="tester")
        after = _counts(service, campaign["id"])
        assert result["ok"] is True
        assert result["approval"]["approval_status"] == "requested"
        assert after["approvals"] == before["approvals"] + 1
        assert after["jobs"] == before["jobs"]
        assert after["queued"] == before["queued"]
        assert result["execution_started"] is False
        assert adapter.created == 0 and adapter.executed == 0


def test_request_approval_requires_final_review_and_matching_hash() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, _ = _service(Path(tmp) / "approval.db")
        campaign = _ready_campaign(service)
        try:
            service.request_send_approval(campaign["id"])
            raise AssertionError("final review hash must be required")
        except CampaignLifecycleError as exc:
            assert exc.error_code == "final_review_required"
        try:
            service.request_send_approval(campaign["id"], "wrong")
            raise AssertionError("hash mismatch must be rejected")
        except CampaignLifecycleError as exc:
            assert exc.error_code == "final_review_hash_mismatch"
        assert len(service.repository.list_live_execution_approvals(campaign["id"])) == 0


def test_manifest_source_and_configuration_changes_invalidate_or_reject() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, _ = _service(Path(tmp) / "approval.db")
        campaign = _ready_campaign(service)
        review = service.final_review(campaign["id"])
        first = service.request_send_approval(campaign["id"], review["final_review_hash"])
        service.confirm_campaign_recipients(campaign["id"], ["09377686492"], confirmation_checked=True)
        try:
            service.request_send_approval(campaign["id"], review["final_review_hash"])
            raise AssertionError("changed manifest must reject old review")
        except CampaignLifecycleError as exc:
            assert exc.error_code == "final_review_hash_mismatch"
        old = service.repository.get_live_execution_approval(first["approval"]["approval_id"])
        assert old["approval_status"] == "invalidated"

    with tempfile.TemporaryDirectory() as tmp:
        service, _ = _service(Path(tmp) / "approval.db")
        campaign = _ready_campaign(service, "5613544284")
        review = service.final_review(campaign["id"])
        service.request_send_approval(campaign["id"], review["final_review_hash"])
        revision = service.create_or_update_campaign_configuration_draft(campaign["id"], _config("6407382527"))
        service.validate_campaign_configuration(campaign["id"], revision["revision_id"])
        service.approve_campaign_configuration_revision(campaign["id"], revision["revision_id"])
        service.create_execution_configuration_snapshot(campaign["id"], revision_id=revision["revision_id"])
        try:
            service.request_send_approval(campaign["id"], review["final_review_hash"])
            raise AssertionError("source/config change must reject old review")
        except CampaignLifecycleError as exc:
            assert exc.error_code == "final_review_hash_mismatch"


def test_duplicate_request_is_idempotent_and_incident_recipients_rejected() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, _ = _service(Path(tmp) / "approval.db")
        campaign = _ready_campaign(service)
        review = service.final_review(campaign["id"])
        first = service.request_send_approval(campaign["id"], review["final_review_hash"])
        second = service.request_send_approval(campaign["id"], review["final_review_hash"])
        assert second["idempotent"] is True
        assert second["approval"]["approval_id"] == first["approval"]["approval_id"]
        assert len(service.repository.list_live_execution_approvals(campaign["id"])) == 1

    with tempfile.TemporaryDirectory() as tmp:
        service, _ = _service(Path(tmp) / "approval.db")
        campaign = service.create_campaign({"name": "Incident", "platform": "bale", "status": "draft", "source_channel_uid": "5613544284"})
        service.confirm_campaign_recipients(campaign["id"], ["09304073112", "09304073113"], confirmation_checked=True)
        for recipient in service.repository.list_recipients(campaign["id"], None, 100, 0):
            service.repository.update_recipient_authorization(recipient["id"], {
                "recipient_origin": "unauthorized_generated",
                "synthetic_test_data": True,
                "live_execution_authorized": False,
                "authorization_status": "blocked",
                "should_not_retry": True,
                "live_execution_blocked": True,
                "block_reason": "missing_explicit_user_input",
            })
        revision = service.create_or_update_campaign_configuration_draft(campaign["id"], _config("5613544284"))
        service.validate_campaign_configuration(campaign["id"], revision["revision_id"])
        service.approve_campaign_configuration_revision(campaign["id"], revision["revision_id"])
        service.create_execution_configuration_snapshot(campaign["id"], revision_id=revision["revision_id"])
        review = service.final_review(campaign["id"])
        assert review["validation"]["ok"] is False
        try:
            service.request_send_approval(campaign["id"], review["final_review_hash"])
            raise AssertionError("incident recipients must not be approvable")
        except CampaignLifecycleError as exc:
            assert exc.error_code == "approval_scope_invalid"


if __name__ == "__main__":
    test_final_review_read_only_and_hash_generated()
    test_request_approval_records_only_no_execution_side_effects()
    test_request_approval_requires_final_review_and_matching_hash()
    test_manifest_source_and_configuration_changes_invalidate_or_reject()
    test_duplicate_request_is_idempotent_and_incident_recipients_rejected()
    print("Commercial approval lifecycle tests passed")
