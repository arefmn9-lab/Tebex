from __future__ import annotations

import tempfile
from pathlib import Path

from modules.automation_engine.commercial_queue.repository import CommercialQueueRepository
from modules.automation_engine.commercial_queue.service import CommercialQueueService
from modules.automation_engine.plugins.bale.contact_store import BaleContactStore


class MockExecutor:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, **payload: object) -> dict:
        self.calls.append(dict(payload))
        return {
            "success": True,
            "verified_forwarded_recipient_count": 1,
            "confirm_click_count": 1,
            "forward_verified": True,
            "diagnostics_consistent": True,
        }


def _service(root: Path, executor: MockExecutor | None = None) -> CommercialQueueService:
    return CommercialQueueService(
        repository=CommercialQueueRepository(root / "queue.db"),
        orchestrator=executor or (lambda **payload: {"success": True}),
        account_auth_checker=lambda account_id: True,
        sleeper=lambda seconds: None,
        contact_store=BaleContactStore(root / "contacts.json"),
    )


def _job(service: CommercialQueueService, phone: str, *, account_id: str, campaign_name: str) -> dict:
    service.update_account_settings(account_id, {
        "enabled": True,
        "source_channel_uid_override": "5613544284",
        "daily_limit_override": 10,
        "deliveries_per_round_override": 1,
    })
    campaign = service.create_campaign({
        "name": campaign_name,
        "platform": "bale",
        "status": "running",
        "source_channel_uid": "5613544284",
        "capacity_reservation": 1,
    })
    service.import_recipients(campaign["id"], [phone])
    recipient = service.list_recipients(campaign["id"], limit=10)["items"][0]
    service.repository.update_recipient_authorization(recipient["id"], {
        "recipient_origin": "user_import",
        "synthetic_test_data": False,
        "live_execution_authorized": True,
        "live_authorized_at": "2026-07-31T00:00:00+00:00",
        "live_authorized_by": "test",
        "authorization_source": "test",
        "authorization_status": "authorized",
        "should_not_retry": False,
    })
    assigned = service.assign_jobs(account_id, campaign["id"], limit=1)
    return service.repository.get_job(assigned["assigned_job_ids"][0])


def test_phone_only_recipient_gets_persisted_context_before_executor() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        service = _service(root)
        job = _job(service, "09351234567", account_id="bale_a", campaign_name="phone-only")
        assert job["display_name"] is None

        context = service._ensure_pre_browser_runtime_context(job, "bale_a")
        refreshed = service.repository.get_job_with_recipient(job["id"])

        assert context["recipient_display_name"] == "Bale-000001"
        assert refreshed["display_name"] == "Bale-000001"
        assert refreshed["recipient_display_name"] == "Bale-000001"


def test_same_phone_reuses_name_across_campaign_account_and_service_reload() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        first_service = _service(root)
        first_job = _job(first_service, "09351234567", account_id="bale_a", campaign_name="first")
        first = first_service._ensure_pre_browser_runtime_context(first_job, "bale_a")

        reloaded_service = _service(root)
        second_job = _job(reloaded_service, "+989351234567", account_id="bale_b", campaign_name="second")
        second = reloaded_service._ensure_pre_browser_runtime_context(second_job, "bale_b")

        assert first["recipient_display_name"] == "Bale-000001"
        assert second["recipient_display_name"] == first["recipient_display_name"]
        identities = reloaded_service.contact_store.list_platform_contact_identities("bale")
        assert len(identities) == 1
        assert {binding["account_id"] for binding in identities[0]["account_bindings"]} == {"bale_a", "bale_b"}


def test_mock_executor_receives_constructed_display_name() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        executor = MockExecutor()
        service = _service(root, executor)
        job = _job(service, "09351234567", account_id="bale_a", campaign_name="mock-execution")
        service.repository.requeue_assigned_jobs_for_account("bale_a", reason="test_setup")

        result = service.run_account_round("bale_a", job["campaign_id"], max_jobs=1)

        assert result["processed_count"] == 1
        assert len(executor.calls) == 1
        assert executor.calls[0]["display_name"] == "Bale-000001"
        assert executor.calls[0]["runtime_session"] is None
