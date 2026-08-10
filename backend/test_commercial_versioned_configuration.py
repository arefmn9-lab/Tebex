from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from modules.automation_engine.commercial_queue.repository import CommercialQueueRepository
from modules.automation_engine.commercial_queue.service import CommercialQueueService


ACCOUNT_ID = "bale_09211690533"


class NoBrowserAdapter:
    def __init__(self) -> None:
        self.created = 0
        self.executed = 0

    def create_runtime_session_for_account(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.created += 1
        raise AssertionError("browser must not launch")

    def execute_plan(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.executed += 1
        raise AssertionError("adapter must not execute")


def _config(uid: str, account_id: str = ACCOUNT_ID, max_jobs: int = 2) -> dict[str, Any]:
    return {
        "source": {
            "platform": "bale",
            "source_channel_uid": uid,
            "source_channel_url": f"https://web.bale.ai/chat?uid={uid}",
            "source_channel_label": f"source_{uid}",
            "source_origin": "explicit_user_selection",
        },
        "accounts": {
            "allowed_account_ids": [account_id],
            "account_selection_strategy": "priority_round_robin",
            "max_concurrent_accounts": 1,
        },
        "delivery": {
            "daily_delivery_limit": 10,
            "deliveries_per_round": max_jobs,
            "max_jobs_per_execution": max_jobs,
            "stop_on_first_non_success": True,
            "automatic_retry": False,
            "retry_policy": {"max_attempts": 1},
        },
        "timing": {
            "timezone": "Asia/Tehran",
            "active_window_start": "00:00",
            "active_window_end": "23:59",
            "weekdays": [0, 1, 2, 3, 4, 5, 6],
            "min_interval_seconds": 0,
            "max_interval_seconds": 0,
            "cooldown_seconds": 0,
            "catch_up_policy": "skip",
        },
        "recipients": {
            "require_live_authorization": True,
            "require_verified_contact": True,
            "allow_synthetic": False,
            "deduplication_policy": "campaign_phone_unique",
        },
        "safety": {
            "require_live_readiness": True,
            "require_approval": True,
            "uncertain_delivery_policy": "stop_manual_review",
            "duplicate_delivery_policy": "block",
        },
        "platform": {
            "adapter_name": "bale",
            "adapter_configuration": {"source_uid": uid},
            "session_reuse_enabled": True,
        },
        "ai": {
            "ai_access_mode": "assist_only",
            "allowed_agent_ids": [],
            "ai_may_create_draft": True,
            "ai_may_request_approval": False,
            "ai_may_execute": False,
            "human_approval_required": True,
        },
        "feature_flags": {"required_feature_flags": []},
    }


def _service(path: Path) -> CommercialQueueService:
    adapter = NoBrowserAdapter()
    service = CommercialQueueService(repository=CommercialQueueRepository(path), orchestrator=lambda **payload: {"success": True}, account_auth_checker=lambda account_id: True, sleeper=lambda seconds: None)
    service.platform_adapters["bale"] = adapter
    service.update_global_settings({
        "default_source_channel_uid": "global_source",
        "deliveries_per_account_round": 9,
        "default_daily_limit_per_account": 20,
        "session_reuse_enabled": True,
        "resource_guard_enabled": True,
    })
    service.update_account_settings(ACCOUNT_ID, {"enabled": True, "source_channel_uid_override": "account_source", "deliveries_per_round_override": 4, "daily_limit_override": 12})
    return service


def _campaign(service: CommercialQueueService, name: str, uid: str) -> dict[str, Any]:
    return service.create_campaign({"name": name, "platform": "bale", "status": "draft", "source_channel_uid": uid, "capacity_reservation": 1})


def _approve_snapshot(service: CommercialQueueService, campaign_id: str, uid: str) -> tuple[dict[str, Any], dict[str, Any]]:
    revision = service.create_or_update_campaign_configuration_draft(campaign_id, _config(uid), change_summary=f"uid {uid}")
    validation = service.validate_campaign_configuration(campaign_id, revision["revision_id"])
    assert validation["validation"]["ok"], validation
    approved = service.approve_campaign_configuration_revision(campaign_id, revision["revision_id"])
    snapshot = service.create_execution_configuration_snapshot(campaign_id, revision_id=revision["revision_id"])
    return approved["revision"], snapshot["snapshot"]


def test_different_campaigns_use_different_sources_and_global_does_not_override() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "config.db")
        campaign_a = _campaign(service, "A", "5613544284")
        campaign_b = _campaign(service, "B", "6407382527")
        service.create_or_update_campaign_configuration_draft(campaign_a["id"], _config("5613544284"))
        service.create_or_update_campaign_configuration_draft(campaign_b["id"], _config("6407382527"))
        effective_a = service.resolve_campaign_configuration(campaign_a["id"])
        effective_b = service.resolve_campaign_configuration(campaign_b["id"])
    assert effective_a["resolved_configuration"]["source"]["source_channel_uid"] == "5613544284"
    assert effective_b["resolved_configuration"]["source"]["source_channel_uid"] == "6407382527"
    assert effective_a["origin_trace"]["fields"]["source.source_channel_uid"]["source_layer"] in {"campaign_draft", "execution_override"}


def test_source_editable_in_draft_and_change_after_approval_creates_new_revision() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "config.db")
        campaign = _campaign(service, "editable", "111")
        draft = service.create_or_update_campaign_configuration_draft(campaign["id"], _config("111"))
        edited = service.create_or_update_campaign_configuration_draft(campaign["id"], _config("222"))
        assert draft["revision_id"] == edited["revision_id"]
        service.validate_campaign_configuration(campaign["id"], edited["revision_id"])
        service.approve_campaign_configuration_revision(campaign["id"], edited["revision_id"])
        new_draft = service.create_or_update_campaign_configuration_draft(campaign["id"], _config("333"))
    assert new_draft["revision_id"] != edited["revision_id"]
    assert new_draft["revision_number"] == edited["revision_number"] + 1


def test_account_origin_visible_and_critical_fallback_blocked() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "config.db")
        campaign = _campaign(service, "fallback", "")
        effective = service.resolve_campaign_configuration(campaign["id"], account_id=ACCOUNT_ID)
        fields = effective["origin_trace"]["fields"]
    assert fields["delivery.daily_delivery_limit"]["source_layer"] == "account_override"
    assert effective["validation"]["ok"] is False
    assert any(error["error_code"] == "implicit_critical_fallback_forbidden" for error in effective["validation"]["errors"])


def test_approval_locks_revision_snapshot_immutable_and_jobs_receive_snapshot() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "config.db")
        campaign = _campaign(service, "snapshot", "5613544284")
        recipient, job = service.repository.create_recipient_and_job(campaign, "0905", "989050454491", "Bale-000008", "test")
        manifest = service.repository.create_recipient_input_manifest(
            campaign_id=campaign["id"],
            phones=["989050454491"],
            batch_id=None,
            submitted_by="test",
            source_type="test_manifest",
            confirmation_status="confirmed",
            confirmed_by="test",
        )
        updates = {
            "recipient_origin": "user_provided",
            "synthetic_test_data": False,
            "live_execution_authorized": True,
            "authorization_status": "authorized",
            "should_not_retry": False,
            "input_manifest_id": manifest["manifest_id"],
            "input_manifest_hash": manifest["manifest_hash"],
            "input_sequence": 1,
            "input_provenance_status": "confirmed_manifest",
        }
        service.repository.update_recipient_authorization(recipient["id"], updates)
        service.repository.update_job_authorization_metadata(job["id"], updates)
        revision, snapshot = _approve_snapshot(service, campaign["id"], "5613544284")
        stored_job = service.repository.get_job(job["id"])
        snapshot_again = service.repository.get_configuration_snapshot(snapshot["snapshot_id"])
    assert revision["status"] == "approved" or revision["status"] == "active"
    assert stored_job["execution_snapshot_id"] == snapshot["snapshot_id"]
    assert stored_job["configuration_revision_id"] == revision["revision_id"]
    assert stored_job["configuration_snapshot_hash"] == snapshot["configuration_hash"]
    assert snapshot_again["configuration_json"] == snapshot["configuration_json"]


def test_drift_detected_but_global_change_does_not_mutate_snapshot() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "config.db")
        campaign = _campaign(service, "drift", "5613544284")
        _, snapshot = _approve_snapshot(service, campaign["id"], "5613544284")
        original_json = snapshot["configuration_json"]
        service.update_global_settings({"default_source_channel_uid": "changed_global"})
        drift = service.check_campaign_configuration_drift(campaign["id"], snapshot["snapshot_id"])
        snapshot_after = service.repository.get_configuration_snapshot(snapshot["snapshot_id"])
    assert drift["configuration_drift_detected"] is False
    assert snapshot_after["configuration_json"] == original_json


def test_account_override_change_detected_when_revision_depends_on_account() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "config.db")
        campaign = _campaign(service, "account-drift", "")
        service.create_or_update_campaign_configuration_draft(campaign["id"], _config("5613544284", max_jobs=2))
        revision, snapshot = _approve_snapshot(service, campaign["id"], "5613544284")
        service.repository.update_configuration_revision(revision["revision_id"], {"configuration_json": json.dumps({"source": _config("5613544284")["source"]}), "configuration_hash": "manual"})
        service.update_account_settings(ACCOUNT_ID, {"deliveries_per_round_override": 6})
        drift = service.check_campaign_configuration_drift(campaign["id"], snapshot["snapshot_id"])
    assert drift["configuration_drift_detected"] is True


def test_active_campaign_direct_edit_rejected_paused_allows_revision() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "config.db")
        campaign = _campaign(service, "active", "5613544284")
        service.update_campaign(campaign["id"], {"status": "running"})
        try:
            service.create_or_update_campaign_configuration_draft(campaign["id"], _config("6407382527"))
        except Exception as exc:
            blocked = getattr(exc, "error_code", "") == "configuration_changed_after_approval"
        else:
            blocked = False
        service.update_campaign(campaign["id"], {"status": "paused"})
        draft = service.create_or_update_campaign_configuration_draft(campaign["id"], _config("6407382527"))
    assert blocked is True
    assert draft["status"] == "draft"


def test_worker_rejects_snapshot_mismatch_before_browser_or_adapter() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "config.db")
        adapter = service.platform_adapters["bale"]
        campaign = _campaign(service, "mismatch", "5613544284")
        recipient, job = service.repository.create_recipient_and_job(campaign, "0905", "989050454491", "Bale-000008", "test")
        _approve_snapshot(service, campaign["id"], "5613544284")
        service.update_campaign(campaign["id"], {"status": "running"})
        manifest = service.repository.create_recipient_input_manifest(
            campaign_id=campaign["id"],
            phones=["989050454491"],
            batch_id=None,
            submitted_by="test",
            source_type="test_manifest",
            confirmation_status="confirmed",
            confirmed_by="test",
        )
        auth = {
            "recipient_origin": "user_provided",
            "synthetic_test_data": False,
            "live_execution_authorized": True,
            "authorization_status": "authorized",
            "should_not_retry": False,
            "input_manifest_id": manifest["manifest_id"],
            "input_manifest_hash": manifest["manifest_hash"],
            "input_sequence": 1,
            "input_provenance_status": "confirmed_manifest",
        }
        service.repository.update_recipient_authorization(recipient["id"], auth)
        service.repository.update_job_authorization_metadata(job["id"], {
            **auth,
            "execution_snapshot_id": "wrong",
            "configuration_snapshot_hash": "wrong",
        })
        result = service.run_account_round(ACCOUNT_ID, campaign["id"], max_jobs=1)
        stored = service.repository.get_job(job["id"])
    assert result["processed_count"] == 1
    assert stored["last_error_code"] == "execution_snapshot_missing"
    assert adapter.created == 0
    assert adapter.executed == 0


def test_historical_incident_can_be_frozen_with_actual_source_and_future_template_separate() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "config.db")
        incident = service.create_campaign({"id": "campaign_9dd7fa0520d5", "name": "Controlled Live Forward Verification 2", "platform": "bale", "status": "completed", "source_channel_uid": "5613544284"})
        _, job_a = service.repository.create_recipient_and_job(incident, "0905", "989050454491", "Bale-000008", "incident")
        _, job_b = service.repository.create_recipient_and_job(incident, "0937", "989377686492", "Bale-000009", "incident")
        revision, snapshot = _approve_snapshot(service, incident["id"], "5613544284")
        service.repository.update_job_authorization_metadata(job_a["id"], {"should_not_retry": True})
        service.repository.update_job_authorization_metadata(job_b["id"], {"should_not_retry": True})
        template = service.create_campaign({"name": "Future Advertising Campaign Template", "platform": "bale", "status": "draft", "source_channel_uid": "6407382527"})
        draft = service.create_or_update_campaign_configuration_draft(template["id"], _config("6407382527"))
        job_a_after = service.repository.get_job(job_a["id"])
        job_b_after = service.repository.get_job(job_b["id"])
        template_jobs = service.repository.list_campaign_jobs_all(template["id"])
        assert json.loads(snapshot["configuration_json"])["source"]["source_channel_uid"] == "5613544284"
        assert revision["campaign_id"] == "campaign_9dd7fa0520d5"
        assert bool(job_a_after["should_not_retry"]) is True
        assert bool(job_b_after["should_not_retry"]) is True
        assert json.loads(draft["configuration_json"])["source"]["source_channel_uid"] == "6407382527"
        assert template_jobs == []


if __name__ == "__main__":
    test_different_campaigns_use_different_sources_and_global_does_not_override()
    test_source_editable_in_draft_and_change_after_approval_creates_new_revision()
    test_account_origin_visible_and_critical_fallback_blocked()
    test_approval_locks_revision_snapshot_immutable_and_jobs_receive_snapshot()
    test_drift_detected_but_global_change_does_not_mutate_snapshot()
    test_account_override_change_detected_when_revision_depends_on_account()
    test_active_campaign_direct_edit_rejected_paused_allows_revision()
    test_worker_rejects_snapshot_mismatch_before_browser_or_adapter()
    test_historical_incident_can_be_frozen_with_actual_source_and_future_template_separate()
    print("Commercial versioned configuration tests passed")
