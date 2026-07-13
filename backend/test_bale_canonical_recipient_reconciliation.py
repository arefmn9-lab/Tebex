from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from modules.automation_engine.commercial_queue.repository import utc_now
from modules.automation_engine.commercial_queue.service import (
    CONTROLLED_SINGLE_RECIPIENT_NAME,
    CONTROLLED_SINGLE_RECIPIENT_PHONE,
    CONTROLLED_SINGLE_RECIPIENT_SOURCE_UID,
    CONTROLLED_SINGLE_RECIPIENT_SOURCE_URL,
)
from test_bale_controlled_single_recipient_live_preflight import (
    _approved_preflight,
    _codes,
    _config,
    _insert_recipient,
    _ready_campaign,
    _service,
)


def _approve_config(service: Any, campaign_id: str, config: dict[str, Any]) -> None:
    revision = service.create_or_update_campaign_configuration_draft(campaign_id, config)
    service.validate_campaign_configuration(campaign_id, revision["revision_id"])
    service.approve_campaign_configuration_revision(campaign_id, revision["revision_id"])
    service.create_execution_configuration_snapshot(campaign_id, revision_id=revision["revision_id"])


def _insert_legacy_contact_identity(service: Any, campaign_id: str) -> dict[str, Any]:
    now = utc_now()
    record = {
        "id": "recipient_legacy_contact",
        "campaign_id": campaign_id,
        "phone_raw": "09050454491",
        "phone_normalized": CONTROLLED_SINGLE_RECIPIENT_PHONE,
        "display_name": CONTROLLED_SINGLE_RECIPIENT_NAME,
        "import_source": "contact_maintenance",
        "validation_status": "valid",
        "duplicate_of_recipient_id": None,
        "created_at": now,
        "updated_at": now,
        "recipient_origin": "user_provided",
        "synthetic_test_data": 0,
        "live_execution_authorized": 1,
        "authorization_status": "authorized",
        "should_not_retry": 0,
        "stable_display_name": CONTROLLED_SINGLE_RECIPIENT_NAME,
        "bale_contact_verified": 1,
        "bale_verification_status": "verified",
        "contact_preparation_allowed": 0,
        "live_execution_blocked": 0,
        "input_provenance_status": "unknown",
    }
    with service.repository.connection() as connection:
        connection.execute(
            """
            INSERT INTO commercial_recipients (
                id, campaign_id, phone_raw, phone_normalized, display_name, import_source,
                validation_status, duplicate_of_recipient_id, created_at, updated_at,
                recipient_origin, synthetic_test_data, live_execution_authorized,
                authorization_status, should_not_retry, stable_display_name,
                bale_contact_verified, bale_verification_status, contact_preparation_allowed,
                live_execution_blocked, input_provenance_status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(record[key] for key in [
                "id", "campaign_id", "phone_raw", "phone_normalized", "display_name", "import_source",
                "validation_status", "duplicate_of_recipient_id", "created_at", "updated_at",
                "recipient_origin", "synthetic_test_data", "live_execution_authorized",
                "authorization_status", "should_not_retry", "stable_display_name",
                "bale_contact_verified", "bale_verification_status", "contact_preparation_allowed",
                "live_execution_blocked", "input_provenance_status",
            ]),
        )
        connection.commit()
    return record


def _force_queued(service: Any, job_id: str) -> None:
    with service.repository.connection() as connection:
        connection.execute(
            "UPDATE commercial_delivery_jobs SET status='queued', should_not_retry=0, safe_to_requeue=NULL, live_execution_blocked=0, manual_review_required=0 WHERE id=?",
            (job_id,),
        )
        connection.commit()


def test_duplicate_recipients_same_phone_do_not_define_execution_identity() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, adapter = _service(Path(tmp) / "canonical.db")
        campaign = service.create_campaign({"name": "Legacy Contact", "platform": "bale", "status": "draft", "source_channel_uid": CONTROLLED_SINGLE_RECIPIENT_SOURCE_UID})
        _insert_legacy_contact_identity(service, campaign["id"])
        _approve_config(service, campaign["id"], _config())
        preflight = service.live_preflight(campaign["id"])
        assert "recipient_manifest_not_confirmed" in _codes(preflight)
        assert "recipient_provenance_unknown" in _codes(preflight)
        assert adapter.created == 0 and adapter.executed == 0


def test_manifest_null_authorized_true_cannot_preflight_queue_or_claim() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, adapter = _service(Path(tmp) / "canonical.db")
        campaign = service.create_campaign({"name": "Null Manifest", "platform": "bale", "status": "draft", "source_channel_uid": CONTROLLED_SINGLE_RECIPIENT_SOURCE_UID})
        _insert_legacy_contact_identity(service, campaign["id"])
        _approve_config(service, campaign["id"], _config())
        preflight = service.live_preflight(campaign["id"])
        assert preflight["ready"] is False
        assert "recipient_manifest_not_confirmed" in _codes(preflight)

        recipient, job = service.repository.create_recipient_and_job(campaign, "09050454491", CONTROLLED_SINGLE_RECIPIENT_PHONE, CONTROLLED_SINGLE_RECIPIENT_NAME, "legacy_authorized")
        service.repository.update_recipient_authorization(recipient["id"], {
            "recipient_origin": "user_provided",
            "synthetic_test_data": False,
            "live_execution_authorized": True,
            "authorization_status": "authorized",
            "should_not_retry": False,
            "bale_contact_verified": True,
        })
        service.repository.update_job_authorization_metadata(job["id"], {
            "recipient_origin": "user_provided",
            "synthetic_test_data": False,
            "live_execution_authorized": True,
            "authorization_status": "authorized",
            "should_not_retry": False,
        })
        service.repository.update_campaign(campaign["id"], {"status": "running"})
        _force_queued(service, job["id"])
        assigned = service.assign_jobs("bale_09211690533", campaign["id"], limit=1)
        stored = service.repository.get_job(job["id"])
        assert assigned["assigned_count"] == 0
        assert stored["status"] == "skipped"
        assert stored["safe_to_requeue"] == 0
        assert adapter.created == 0 and adapter.executed == 0


def test_verified_contact_without_manifest_is_not_execution_authorized() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, _ = _service(Path(tmp) / "canonical.db")
        campaign = service.create_campaign({"name": "Verified Contact", "platform": "bale", "status": "draft", "source_channel_uid": CONTROLLED_SINGLE_RECIPIENT_SOURCE_UID})
        recipient, job = service.repository.create_recipient_and_job(campaign, "09050454491", CONTROLLED_SINGLE_RECIPIENT_PHONE, CONTROLLED_SINGLE_RECIPIENT_NAME, "contact_maintenance")
        service.repository.update_recipient_authorization(recipient["id"], {
            "recipient_origin": "user_provided",
            "synthetic_test_data": False,
            "live_execution_authorized": True,
            "authorization_status": "authorized",
            "bale_contact_verified": True,
        })
        details = service.repository.get_job_with_recipient(job["id"])
        check = service.validate_live_recipient_authorization(job, details, dry_run=False)
        assert check["ok"] is False
        assert check["error_code"] == "recipient_input_manifest_required"


def test_historical_recipient_and_job_cannot_be_reused() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, adapter = _service(Path(tmp) / "canonical.db")
        campaign, _, _ = _ready_campaign(service)
        recipient, job = service.repository.create_recipient_and_job(campaign, "09050454491", CONTROLLED_SINGLE_RECIPIENT_PHONE, CONTROLLED_SINGLE_RECIPIENT_NAME, "historical_5613544284")
        service.repository.update_recipient_authorization(recipient["id"], {"should_not_retry": True})
        service.repository.update_job_authorization_metadata(job["id"], {"should_not_retry": True, "safe_to_requeue": False})
        preflight = service.live_preflight(campaign["id"])
        assert "historical_job_reuse_forbidden" in _codes(preflight)
        service.repository.update_campaign(campaign["id"], {"status": "running"})
        _force_queued(service, job["id"])
        assigned = service.assign_jobs("bale_09211690533", campaign["id"], limit=1)
        assert assigned["assigned_count"] == 0
        assert adapter.created == 0 and adapter.executed == 0


def test_new_confirmed_manifest_creates_independent_execution_scope() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, adapter = _service(Path(tmp) / "canonical.db")
        legacy_campaign = service.create_campaign({"name": "Legacy", "platform": "bale", "status": "draft", "source_channel_uid": "5613544284"})
        _insert_legacy_contact_identity(service, legacy_campaign["id"])
        campaign, manifest, recipient = _ready_campaign(service)
        preflight = _approved_preflight(service, campaign["id"])
        assert preflight["ready"] is True
        assert preflight["manifest_summary"]["manifest_id"] == manifest["manifest_id"]
        assert recipient["id"] != "recipient_legacy_contact"
        assert adapter.created == 0 and adapter.executed == 0


def test_stable_name_phone_mismatch_rejected() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, _ = _service(Path(tmp) / "canonical.db")
        campaign, _, _ = _ready_campaign(service, phone=CONTROLLED_SINGLE_RECIPIENT_PHONE, name="Bale-000099")
        preflight = service.live_preflight(campaign["id"])
        assert "approval_scope_invalid" in _codes(preflight)


def test_source_configuration_remains_campaign_specific() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, _ = _service(Path(tmp) / "canonical.db")
        controlled, _, _ = _ready_campaign(service)
        other = service.create_campaign({"name": "Other Source", "platform": "bale", "status": "draft", "source_channel_uid": "5613544284"})
        _approve_config(service, other["id"], _config(uid="5613544284", url="https://web.bale.ai/chat?uid=5613544284"))
        controlled_config = service.resolve_campaign_configuration(controlled["id"])["resolved_configuration"]
        other_config = service.resolve_campaign_configuration(other["id"])["resolved_configuration"]
        assert controlled_config["source"]["source_channel_uid"] == CONTROLLED_SINGLE_RECIPIENT_SOURCE_UID
        assert other_config["source"]["source_channel_uid"] == "5613544284"


def test_no_operational_side_effects() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, adapter = _service(Path(tmp) / "canonical.db")
        campaign, _, _ = _ready_campaign(service)
        before = {
            "jobs": len(service.repository.list_campaign_jobs_all(campaign["id"])),
            "recipients": len(service.repository.list_recipients(campaign["id"], None, 100, 0)),
            "approvals": len(service.repository.list_live_execution_approvals(campaign["id"])),
        }
        service.live_preflight(campaign["id"])
        after = {
            "jobs": len(service.repository.list_campaign_jobs_all(campaign["id"])),
            "recipients": len(service.repository.list_recipients(campaign["id"], None, 100, 0)),
            "approvals": len(service.repository.list_live_execution_approvals(campaign["id"])),
        }
        assert before == after
        assert adapter.created == 0 and adapter.executed == 0


if __name__ == "__main__":
    test_duplicate_recipients_same_phone_do_not_define_execution_identity()
    test_manifest_null_authorized_true_cannot_preflight_queue_or_claim()
    test_verified_contact_without_manifest_is_not_execution_authorized()
    test_historical_recipient_and_job_cannot_be_reused()
    test_new_confirmed_manifest_creates_independent_execution_scope()
    test_stable_name_phone_mismatch_rejected()
    test_source_configuration_remains_campaign_specific()
    test_no_operational_side_effects()
    print("Bale canonical recipient reconciliation tests passed")
