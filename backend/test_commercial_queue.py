from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from app.main import app
from app.routes import automation as automation_routes
from fastapi.testclient import TestClient
from modules.automation_engine.commercial_queue.repository import CommercialQueueRepository
from modules.automation_engine.commercial_queue.service import CommercialQueueService


def _service(path: Path) -> CommercialQueueService:
    return CommercialQueueService(repository=CommercialQueueRepository(path))


def test_global_settings_create_read_update() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "commercial.db")
        defaults = service.get_global_settings()
        updated = service.update_global_settings({"default_daily_limit_per_account": 77, "default_source_channel_uid": "5613544284"})
        reloaded = _service(Path(tmp_dir) / "commercial.db").get_global_settings()

    assert defaults["id"] == "global"
    assert updated["default_daily_limit_per_account"] == 77
    assert reloaded["default_source_channel_uid"] == "5613544284"


def test_account_override_resolution_and_apply_global_preserves_overrides() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "commercial.db")
        service.update_global_settings(
            {
                "default_daily_limit_per_account": 50,
                "deliveries_per_account_round": 8,
                "delay_between_deliveries_seconds": 30,
                "round_cooldown_seconds": 600,
                "default_source_channel_uid": "global_uid",
            }
        )
        service.update_account_settings(
            "bale_a",
            {
                "daily_limit_override": 12,
                "source_channel_uid_override": "account_uid",
                "priority": 500,
            },
        )
        service.apply_global_defaults()
        effective = service.resolve_account_settings("bale_a")

    assert effective["daily_limit"] == 12
    assert effective["source_channel_uid"] == "account_uid"
    assert effective["deliveries_per_round"] == 8
    assert effective["priority"] == 500


def test_campaign_creation_and_recipient_import_boundaries() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "commercial.db")
        campaign = service.create_campaign({"name": "Manual Campaign", "platform": "bale", "source_channel_uid": "5613544284"})
        result = service.import_recipients(
            campaign["id"],
            ["09304073331", "+989304073331", "invalid", "989351111111"],
            import_source="manual",
        )
        campaign_after = service.get_campaign(campaign["id"])
        recipients = service.list_recipients(campaign["id"])
        jobs = service.list_jobs(campaign_id=campaign["id"])

    assert campaign["status"] == "draft"
    assert result["submitted_count"] == 4
    assert result["valid_count"] == 2
    assert result["invalid_count"] == 1
    assert result["duplicate_count"] == 1
    assert result["created_recipient_count"] == 2
    assert result["created_job_count"] == 2
    assert result["invalid_items"][0]["error_code"] == "invalid_phone"
    assert result["duplicate_items"][0]["duplicate_scope"] == "request"
    assert campaign_after is not None
    assert campaign_after["total_recipients"] == 2
    assert campaign_after["queued_count"] == 0
    assert campaign_after["skipped_count"] == 2
    assert len(recipients["items"]) == 2
    assert len(jobs["items"]) == 2
    assert all(job["account_id"] is None for job in jobs["items"])


def test_duplicate_recipient_detection_against_existing_campaign() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "commercial.db")
        campaign = service.create_campaign({"name": "Duplicates", "platform": "bale"})
        first = service.import_recipients(campaign["id"], ["09304073331"])
        second = service.import_recipients(campaign["id"], ["989304073331"])

    assert first["created_job_count"] == 1
    assert second["created_job_count"] == 0
    assert second["duplicate_count"] == 1
    assert second["duplicate_items"][0]["duplicate_scope"] == "campaign"


def test_delivery_job_idempotency_key_is_unique() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "commercial.db")
        campaign = service.create_campaign({"name": "Idempotency", "platform": "bale"})
        service.repository.create_recipient_and_job(campaign, "09304073331", "989304073331", None, "manual")
        try:
            service.repository.create_recipient_and_job(campaign, "989304073331", "989304073331", None, "manual")
            raised = False
        except sqlite3.IntegrityError:
            raised = True

    assert raised is True


def test_queue_and_settings_persist_after_service_reload() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "commercial.db"
        service = _service(db_path)
        service.update_global_settings({"default_daily_limit_per_account": 88})
        campaign = service.create_campaign({"name": "Persistence", "platform": "bale"})
        service.import_recipients(campaign["id"], ["09304073331"])
        reloaded = _service(db_path)
        assert reloaded.get_global_settings()["default_daily_limit_per_account"] == 88
        assert reloaded.list_jobs(campaign_id=campaign["id"])["items"][0]["status"] == "skipped"


def test_job_event_creation_listing_pagination_and_filters() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "commercial.db")
        campaign = service.create_campaign({"name": "Events", "platform": "bale"})
        import_result = service.import_recipients(campaign["id"], ["09304073331", "09351111111"])
        job = import_result["created_jobs"][0]
        recipient = import_result["created_recipients"][0]
        event = service.create_job_event(
            {
                "job_id": job["id"],
                "campaign_id": campaign["id"],
                "recipient_id": recipient["id"],
                "account_id": "bale_a",
                "event_type": "job_started",
                "step_name": "open_source_channel",
                "status": "running",
                "message": "started",
                "diagnostics": {"ok": True},
            }
        )
        job_events = service.list_events(job_id=job["id"], limit=10)
        account_events = service.list_events(account_id="bale_a", limit=1)
        filtered_jobs = service.list_jobs(status="skipped", campaign_id=campaign["id"], limit=1)

    assert event["event_type"] == "job_started"
    assert len(job_events["items"]) >= 2
    assert account_events["items"][0]["account_id"] == "bale_a"
    assert filtered_jobs["limit"] == 1
    assert filtered_jobs["items"][0]["campaign_id"] == campaign["id"]


def test_commercial_queue_api_contracts() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        previous_service = automation_routes.commercial_queue_service
        service = _service(Path(tmp_dir) / "commercial.db")
        automation_routes.commercial_queue_service = service
        try:
            client = TestClient(app)
            settings_response = client.put("/automation/settings/global", json={"default_daily_limit_per_account": 66})
            account_response = client.put("/automation/accounts/bale_a/settings", json={"daily_limit_override": 11})
            campaign_response = client.post("/automation/campaigns", json={"name": "API Campaign", "platform": "bale"})
            campaign = campaign_response.json()
            import_response = client.post(
                f"/automation/campaigns/{campaign['id']}/recipients",
                json={"phones": ["09304073331", "bad"], "import_source": "manual"},
            )
            jobs_response = client.get(f"/automation/jobs?campaign_id={campaign['id']}&status=skipped&limit=10")
            job = jobs_response.json()["items"][0]
            events_response = client.get(f"/automation/jobs/{job['id']}/events")
            campaigns_response = client.get("/automation/campaigns?status=draft&limit=1")
        finally:
            automation_routes.commercial_queue_service = previous_service

    assert settings_response.status_code == 200
    assert settings_response.json()["default_daily_limit_per_account"] == 66
    assert account_response.json()["effective"]["daily_limit"] == 11
    assert campaign_response.status_code == 200
    assert import_response.json()["created_job_count"] == 1
    assert import_response.json()["invalid_count"] == 1
    assert jobs_response.json()["items"][0]["status"] == "skipped"
    assert events_response.json()["items"][0]["event_type"] == "job_created"
    assert campaigns_response.json()["limit"] == 1


if __name__ == "__main__":
    test_global_settings_create_read_update()
    test_account_override_resolution_and_apply_global_preserves_overrides()
    test_campaign_creation_and_recipient_import_boundaries()
    test_duplicate_recipient_detection_against_existing_campaign()
    test_delivery_job_idempotency_key_is_unique()
    test_queue_and_settings_persist_after_service_reload()
    test_job_event_creation_listing_pagination_and_filters()
    test_commercial_queue_api_contracts()
    print("Commercial queue tests passed")
