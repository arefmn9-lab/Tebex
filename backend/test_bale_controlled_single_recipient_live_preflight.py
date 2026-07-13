from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from modules.automation_engine.commercial_queue.repository import CommercialQueueRepository, new_id, utc_now
from modules.automation_engine.commercial_queue.service import (
    CONTROLLED_SINGLE_RECIPIENT_ACCOUNT_ID,
    CONTROLLED_SINGLE_RECIPIENT_APPROVAL_SCOPE,
    CONTROLLED_SINGLE_RECIPIENT_NAME,
    CONTROLLED_SINGLE_RECIPIENT_PHONE,
    CONTROLLED_SINGLE_RECIPIENT_SOURCE_UID,
    CONTROLLED_SINGLE_RECIPIENT_SOURCE_URL,
    CampaignLifecycleError,
    CommercialQueueService,
)


class ForbiddenAdapter:
    def __init__(self) -> None:
        self.created = 0
        self.executed = 0

    def create_runtime_session_for_account(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.created += 1
        raise AssertionError("preflight must not launch Chrome")

    def execute_plan(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.executed += 1
        raise AssertionError("preflight must not execute adapter")


def _config(uid: str = CONTROLLED_SINGLE_RECIPIENT_SOURCE_UID, url: str | None = CONTROLLED_SINGLE_RECIPIENT_SOURCE_URL, accounts: list[str] | None = None, max_jobs: int = 1) -> dict[str, Any]:
    return {
        "source": {"platform": "bale", "source_channel_uid": uid, "source_channel_url": url if url is not None else f"https://web.bale.ai/chat?uid={uid}", "source_channel_label": "controlled_single", "source_origin": "explicit_user_selection"},
        "accounts": {"allowed_account_ids": accounts or [CONTROLLED_SINGLE_RECIPIENT_ACCOUNT_ID], "account_selection_strategy": "priority_round_robin", "max_concurrent_accounts": 1},
        "delivery": {"daily_delivery_limit": 1, "deliveries_per_round": max_jobs, "max_jobs_per_execution": max_jobs, "stop_on_first_non_success": True, "automatic_retry": False, "retry_policy": {"max_attempts": 1}},
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
        orchestrator=lambda **payload: (_ for _ in ()).throw(AssertionError("preflight must not call plugin")),
        account_auth_checker=lambda account_id: True,
        sleeper=lambda seconds: None,
    )
    service.platform_adapters["bale"] = adapter
    service.update_global_settings({"default_source_channel_uid": "5613544284", "session_reuse_enabled": True})
    service.update_account_settings(CONTROLLED_SINGLE_RECIPIENT_ACCOUNT_ID, {"enabled": True, "daily_limit_override": 1, "deliveries_per_round_override": 1})
    return service, adapter


def _insert_recipient(service: CommercialQueueService, campaign_id: str, manifest: dict[str, Any], phone: str = CONTROLLED_SINGLE_RECIPIENT_PHONE, name: str = CONTROLLED_SINGLE_RECIPIENT_NAME, **overrides: Any) -> dict[str, Any]:
    now = utc_now()
    record = {
        "id": new_id("recipient"),
        "campaign_id": campaign_id,
        "phone_raw": "09050454491" if phone == CONTROLLED_SINGLE_RECIPIENT_PHONE else phone,
        "phone_normalized": phone,
        "display_name": name,
        "import_source": "controlled_single_manifest",
        "validation_status": "valid",
        "duplicate_of_recipient_id": None,
        "created_at": now,
        "updated_at": now,
        "recipient_origin": "user_provided",
        "synthetic_test_data": 0,
        "live_execution_authorized": 1,
        "authorization_source": "explicit_user_confirmation",
        "authorization_status": "authorized",
        "should_not_retry": 0,
        "stable_display_name": name,
        "bale_contact_verified": 1,
        "bale_verification_status": "verified",
        "input_manifest_id": manifest["manifest_id"],
        "input_manifest_hash": manifest["manifest_hash"],
        "input_sequence": 1,
        "input_provenance_status": "confirmed_manifest",
        "contact_preparation_allowed": 0,
        "live_execution_blocked": 0,
        "block_reason": None,
    }
    record.update(overrides)
    with service.repository.connection() as connection:
        connection.execute(
            """
            INSERT INTO commercial_recipients (
                id, campaign_id, phone_raw, phone_normalized, display_name, import_source,
                validation_status, duplicate_of_recipient_id, created_at, updated_at,
                recipient_origin, synthetic_test_data, live_execution_authorized,
                authorization_source, authorization_status, should_not_retry,
                stable_display_name, bale_contact_verified, bale_verification_status,
                input_manifest_id, input_manifest_hash, input_sequence,
                input_provenance_status, contact_preparation_allowed,
                live_execution_blocked, block_reason
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(record[key] for key in [
                "id", "campaign_id", "phone_raw", "phone_normalized", "display_name", "import_source",
                "validation_status", "duplicate_of_recipient_id", "created_at", "updated_at",
                "recipient_origin", "synthetic_test_data", "live_execution_authorized",
                "authorization_source", "authorization_status", "should_not_retry",
                "stable_display_name", "bale_contact_verified", "bale_verification_status",
                "input_manifest_id", "input_manifest_hash", "input_sequence",
                "input_provenance_status", "contact_preparation_allowed",
                "live_execution_blocked", "block_reason",
            ]),
        )
        connection.commit()
    return record


def _ready_campaign(service: CommercialQueueService, phone: str = CONTROLLED_SINGLE_RECIPIENT_PHONE, name: str = CONTROLLED_SINGLE_RECIPIENT_NAME, uid: str = CONTROLLED_SINGLE_RECIPIENT_SOURCE_UID, url: str = CONTROLLED_SINGLE_RECIPIENT_SOURCE_URL, recipient_overrides: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    campaign = service.create_campaign({"name": "Controlled Single Recipient", "platform": "bale", "status": "draft", "source_channel_uid": uid})
    manifest = service.repository.create_recipient_input_manifest(campaign["id"], [phone], None, "tester", "controlled_single", confirmation_status="confirmed", confirmed_by="tester")
    recipient = _insert_recipient(service, campaign["id"], manifest, phone=phone, name=name, **(recipient_overrides or {}))
    revision = service.create_or_update_campaign_configuration_draft(campaign["id"], _config(uid, url))
    service.validate_campaign_configuration(campaign["id"], revision["revision_id"])
    service.approve_campaign_configuration_revision(campaign["id"], revision["revision_id"])
    service.create_execution_configuration_snapshot(campaign["id"], revision_id=revision["revision_id"])
    return campaign, manifest, recipient


def _approved_preflight(service: CommercialQueueService, campaign_id: str) -> dict[str, Any]:
    review = service.final_review(campaign_id)
    approval = service.request_send_approval(
        campaign_id,
        review["final_review_hash"],
        approval_scope=CONTROLLED_SINGLE_RECIPIENT_APPROVAL_SCOPE,
        explicit_confirmation=True,
    )
    return service.live_preflight(campaign_id, approval_id=approval["approval"]["approval_id"])


def _codes(preflight: dict[str, Any]) -> set[str]:
    return {str(error.get("error_code")) for error in preflight.get("blocking_errors", [])}


def _counts(service: CommercialQueueService, campaign_id: str) -> dict[str, int]:
    jobs = service.repository.list_campaign_jobs_all(campaign_id)
    return {
        "recipients": len(service.repository.list_recipients(campaign_id, None, 100, 0)),
        "jobs": len(jobs),
        "queued": sum(1 for job in jobs if job.get("status") == "queued"),
        "assigned": sum(1 for job in jobs if job.get("status") == "assigned"),
        "approvals": len(service.repository.list_live_execution_approvals(campaign_id)),
    }


def test_bale_000008_correct_source_preflight_passes_without_execution() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, adapter = _service(Path(tmp) / "single.db")
        campaign, _, _ = _ready_campaign(service)
        before = _counts(service, campaign["id"])
        preflight = _approved_preflight(service, campaign["id"])
        after = _counts(service, campaign["id"])
        assert preflight["ready"] is True
        assert preflight["execute_allowed"] is False
        assert preflight["execution_feature_status"] == "disabled"
        assert before["jobs"] == after["jobs"] == 0
        assert before["queued"] == after["queued"] == 0
        assert adapter.created == 0 and adapter.executed == 0


def test_old_source_uid_is_blocked_before_browser() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, _ = _service(Path(tmp) / "single.db")
        campaign, _, _ = _ready_campaign(service, uid="5613544284", url="https://web.bale.ai/chat?uid=5613544284")
        preflight = service.live_preflight(campaign["id"])
        assert "historical_source_fallback_forbidden" in _codes(preflight)


def test_correct_uid_wrong_url_blocked() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, _ = _service(Path(tmp) / "single.db")
        campaign, _, _ = _ready_campaign(service)
        revision = service.repository.get_latest_configuration_revision(campaign["id"], {"approved", "active"})
        config = json.loads(revision["resolved_configuration_json"])
        config["source"]["source_channel_url"] = "https://web.bale.ai/chat?uid=wrong"
        with service.repository.connection() as connection:
            connection.execute(
                "UPDATE commercial_campaign_configuration_revisions SET resolved_configuration_json=?, configuration_json=? WHERE revision_id=?",
                (json.dumps(config, ensure_ascii=False), json.dumps(config, ensure_ascii=False), revision["revision_id"]),
            )
            connection.commit()
        preflight = service.live_preflight(campaign["id"])
        assert "source_url_mismatch" in _codes(preflight)


def test_manifest_with_more_than_one_recipient_blocked() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, _ = _service(Path(tmp) / "single.db")
        campaign, manifest, _ = _ready_campaign(service)
        _insert_recipient(service, campaign["id"], manifest, phone="989377686492", name="Bale-000009", input_sequence=2)
        preflight = service.live_preflight(campaign["id"])
        assert "single_recipient_scope_required" in _codes(preflight)


def test_bale_000009_and_incident_recipients_blocked() -> None:
    cases = [
        ("989377686492", "Bale-000009", {"should_not_retry": 1}, "recipient_should_not_retry"),
        ("989304073112", "Bale-000010", {"recipient_origin": "unauthorized_generated", "synthetic_test_data": 1, "live_execution_authorized": 0, "authorization_status": "blocked", "live_execution_blocked": 1}, "approval_scope_invalid"),
        ("989304073113", "Bale-000011", {"recipient_origin": "unauthorized_generated", "synthetic_test_data": 1, "live_execution_authorized": 0, "authorization_status": "blocked", "live_execution_blocked": 1}, "approval_scope_invalid"),
    ]
    for phone, name, overrides, expected in cases:
        with tempfile.TemporaryDirectory() as tmp:
            service, _ = _service(Path(tmp) / "single.db")
            campaign, _, _ = _ready_campaign(service, phone=phone, name=name, recipient_overrides=overrides)
            preflight = service.live_preflight(campaign["id"])
            assert expected in _codes(preflight)


def test_historical_job_bale_000008_cannot_be_reused() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, _ = _service(Path(tmp) / "single.db")
        campaign, _, recipient = _ready_campaign(service)
        service.repository.create_recipient_and_job(campaign, "09050454491", CONTROLLED_SINGLE_RECIPIENT_PHONE, CONTROLLED_SINGLE_RECIPIENT_NAME, "historical")
        preflight = service.live_preflight(campaign["id"])
        assert "historical_job_reuse_forbidden" in _codes(preflight)


def test_requested_invalidated_expired_and_snapshot_drift_block_execution() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, _ = _service(Path(tmp) / "single.db")
        campaign, _, _ = _ready_campaign(service)
        review = service.final_review(campaign["id"])
        requested = service.request_send_approval(campaign["id"], review["final_review_hash"], approval_scope=CONTROLLED_SINGLE_RECIPIENT_APPROVAL_SCOPE)
        preflight = service.live_preflight(campaign["id"], approval_id=requested["approval"]["approval_id"])
        assert "approval_not_approved" in _codes(preflight)
        service.repository.update_live_execution_approval(requested["approval"]["approval_id"], {"approval_status": "invalidated", "invalidated_at": utc_now(), "invalidation_reason": "test"})
        preflight = service.live_preflight(campaign["id"], approval_id=requested["approval"]["approval_id"])
        assert "approval_already_invalidated" in _codes(preflight)

    with tempfile.TemporaryDirectory() as tmp:
        service, _ = _service(Path(tmp) / "single.db")
        campaign, _, _ = _ready_campaign(service)
        review = service.final_review(campaign["id"])
        approved = service.request_send_approval(campaign["id"], review["final_review_hash"], approval_scope=CONTROLLED_SINGLE_RECIPIENT_APPROVAL_SCOPE, explicit_confirmation=True)
        service.repository.update_live_execution_approval(approved["approval"]["approval_id"], {"expires_at": "2000-01-01T00:00:00+00:00"})
        preflight = service.live_preflight(campaign["id"], approval_id=approved["approval"]["approval_id"])
        assert "approval_expired" in _codes(preflight)

    with tempfile.TemporaryDirectory() as tmp:
        service, _ = _service(Path(tmp) / "single.db")
        campaign, _, _ = _ready_campaign(service)
        review = service.final_review(campaign["id"])
        approved = service.request_send_approval(campaign["id"], review["final_review_hash"], approval_scope=CONTROLLED_SINGLE_RECIPIENT_APPROVAL_SCOPE, explicit_confirmation=True)
        snapshot = service.repository.get_latest_configuration_snapshot(campaign["id"])
        with service.repository.connection() as connection:
            connection.execute("UPDATE commercial_execution_configuration_snapshots SET configuration_hash=? WHERE snapshot_id=?", ("changed", snapshot["snapshot_id"]))
            connection.commit()
        preflight = service.live_preflight(campaign["id"], approval_id=approved["approval"]["approval_id"])
        assert "final_review_hash_mismatch" in _codes(preflight) or "configuration_snapshot_hash_mismatch" in _codes(preflight)


def test_new_mock_job_validation_level_only_and_duplicate_scope_blocked() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, _ = _service(Path(tmp) / "single.db")
        campaign, _, _ = _ready_campaign(service)
        preflight = _approved_preflight(service, campaign["id"])
        assert preflight["ready"] is True
        assert len(service.repository.list_campaign_jobs_all(campaign["id"])) == 0

    with tempfile.TemporaryDirectory() as tmp:
        service, _ = _service(Path(tmp) / "single.db")
        campaign, _, _ = _ready_campaign(service)
        _, job = service.repository.create_recipient_and_job(campaign, "09050454491", CONTROLLED_SINGLE_RECIPIENT_PHONE, CONTROLLED_SINGLE_RECIPIENT_NAME, "duplicate")
        service.repository.complete_job(job["id"], "succeeded", {"result_success": True, "verified_forwarded_recipient_count": 1, "forward_verified": True, "diagnostics_consistent": True})
        preflight = service.live_preflight(campaign["id"])
        assert "duplicate_execution_scope" in _codes(preflight)


def test_single_use_token_replay_and_preflight_side_effects() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, adapter = _service(Path(tmp) / "single.db")
        campaign, _, _ = _ready_campaign(service)
        review = service.final_review(campaign["id"])
        approved = service.request_send_approval(campaign["id"], review["final_review_hash"], approval_scope=CONTROLLED_SINGLE_RECIPIENT_APPROVAL_SCOPE, explicit_confirmation=True)
        before = _counts(service, campaign["id"])
        token = service.issue_execution_authorization_for_approved_preflight(campaign["id"], approved["approval"]["approval_id"])
        service.consume_execution_authorization(token["execution_authorization_id"])
        try:
            service.consume_execution_authorization(token["execution_authorization_id"])
            raise AssertionError("token replay must be rejected")
        except CampaignLifecycleError as exc:
            assert exc.error_code == "approval_execution_forbidden"
        after = _counts(service, campaign["id"])
        assert before["jobs"] == after["jobs"] == 0
        assert before["queued"] == after["queued"] == 0
        assert adapter.created == 0 and adapter.executed == 0


if __name__ == "__main__":
    test_bale_000008_correct_source_preflight_passes_without_execution()
    test_old_source_uid_is_blocked_before_browser()
    test_correct_uid_wrong_url_blocked()
    test_manifest_with_more_than_one_recipient_blocked()
    test_bale_000009_and_incident_recipients_blocked()
    test_historical_job_bale_000008_cannot_be_reused()
    test_requested_invalidated_expired_and_snapshot_drift_block_execution()
    test_new_mock_job_validation_level_only_and_duplicate_scope_blocked()
    test_single_use_token_replay_and_preflight_side_effects()
    print("Bale controlled single-recipient live preflight tests passed")
