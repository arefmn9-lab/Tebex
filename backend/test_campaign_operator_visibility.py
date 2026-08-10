from __future__ import annotations

from pathlib import Path

from modules.automation_engine.commercial_queue.repository import CommercialQueueRepository
from modules.automation_engine.commercial_queue.service import CommercialQueueService, campaign_presentation


def _service(database: Path) -> CommercialQueueService:
    return CommercialQueueService(repository=CommercialQueueRepository(database))


def test_operator_campaign_list_hides_known_historical_artifacts_without_deleting_them(tmp_path: Path) -> None:
    service = _service(tmp_path / "campaigns.db")
    cases = [
        ("Bale Controlled Batch 50", "draft", "verification_campaign"),
        ("Future Advertising Campaign Template", "draft", "template"),
        ("Controlled Live Forward Verification 2", "completed", "verification_campaign"),
        ("Phase 5E No-Send Readiness Verification", "running", "verification_campaign"),
        ("Worker Dry Run Verification", "draft", "automated_test_fixture"),
    ]
    internal_ids = []
    for name, status, expected_classification in cases:
        campaign = service.repository.create_campaign({"name": name, "platform": "bale", "status": status})
        internal_ids.append(campaign["id"])

    runtime = service.repository.create_campaign({
        "id": "campaign_phase5f1_contact_maintenance",
        "name": "Phase 5F.1 Bale Contact Maintenance",
        "platform": "bale",
        "status": "paused",
    })
    internal_ids.append(runtime["id"])
    operator = service.create_campaign({
        "name": "پیگیری بیماران شهریور",
        "platform": "bale",
        "policy_overrides": {"campaign_origin": "operator_ui", "operator_visible": True},
    })
    historical = service.repository.create_campaign({"name": "پیگیری مرداد", "platform": "bale", "status": "completed"})
    soft_deleted = service.repository.create_campaign({"name": "حذف شده", "platform": "bale"})
    service.repository.delete_campaign(soft_deleted["id"])

    normal = service.list_campaigns(limit=100)
    internal = service.list_campaigns(limit=100, include_internal=True)
    normal_ids = {item["id"] for item in normal["items"]}
    internal_by_id = {item["id"]: item for item in internal["items"]}

    assert operator["id"] in normal_ids
    assert historical["id"] in normal_ids
    assert soft_deleted["id"] not in normal_ids
    assert all(campaign_id not in normal_ids for campaign_id in internal_ids)
    assert service.repository.get_campaign(internal_ids[0]) is not None
    assert internal_by_id[runtime["id"]]["campaign_classification"] == "runtime_generated_artifact"
    deleted_record = service.repository.get_campaign(soft_deleted["id"])
    assert deleted_record is not None
    assert campaign_presentation(deleted_record)["campaign_classification"] == "soft_deleted_campaign"
    assert internal_by_id[soft_deleted["id"]]["campaign_classification"] == "soft_deleted_campaign"
    for campaign_id, (_, _, classification) in zip(internal_ids, cases + [("", "", "runtime_generated_artifact")], strict=True):
        assert internal_by_id[campaign_id]["campaign_classification"] == classification
