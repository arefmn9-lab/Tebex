from __future__ import annotations

from pathlib import Path

import pytest

from modules.automation_engine.commercial_queue.repository import CommercialQueueRepository
from modules.automation_engine.commercial_queue.service import CommercialQueueService


def _service(path: Path, eligible: int = 3) -> CommercialQueueService:
    service = CommercialQueueService(
        repository=CommercialQueueRepository(path),
        account_auth_checker=lambda _account_id: True,
        sleeper=lambda _seconds: None,
    )
    service.account_readiness_matrix = lambda: [
        {
            "account_id": f"bale_{index}", "worker_eligible": True,
            "enabled": True, "commercial_enabled": True,
            "authentication_status": "authenticated", "worker_status": "idle",
            "daily_limit": 10_000 + index, "current_daily_sent_count": 9_999,
        }
        for index in range(eligible)
    ]
    return service


def _campaign(service: CommercialQueueService, name: str, status: str = "draft") -> dict:
    return service.repository.create_campaign({"name": name, "platform": "bale", "status": status})


def test_capacity_total_is_worker_eligible_accounts_not_daily_limits(tmp_path: Path) -> None:
    service = _service(tmp_path / "capacity.db", eligible=3)
    pool = service.campaign_capacity_pool()
    assert pool["eligible_account_count"] == 3
    assert "total_available_sending_capacity" not in pool
    assert "remaining_free_capacity" not in pool


def test_recipient_count_does_not_affect_account_capacity(tmp_path: Path) -> None:
    service = _service(tmp_path / "capacity.db", eligible=3)
    campaign = _campaign(service, "recipients")
    service.repository.update_campaign(campaign["id"], {"total_recipients": 50_000})
    assert service.campaign_capacity_pool()["eligible_account_count"] == 3


def test_one_reservation_reduces_free_and_pause_retains_it(tmp_path: Path) -> None:
    service = _service(tmp_path / "capacity.db", eligible=3)
    campaign = _campaign(service, "paused", status="paused")
    reservation = service.repository.upsert_campaign_capacity_reservation(campaign["id"], 2, 3)
    pool = service.campaign_capacity_pool()
    assert reservation["requested_account_count"] == 2
    assert reservation["allocated_account_count"] == 2
    assert pool["reserved_account_count"] == 2
    assert pool["free_account_count"] == 1


def test_completed_and_cancelled_campaigns_free_capacity(tmp_path: Path) -> None:
    service = _service(tmp_path / "capacity.db", eligible=3)
    completed = _campaign(service, "completed", status="completed")
    cancelled = _campaign(service, "cancelled", status="cancelled")
    service.repository.upsert_campaign_capacity_reservation(completed["id"], 1, 3)
    service.repository.upsert_campaign_capacity_reservation(cancelled["id"], 1, 3)
    assert service.campaign_capacity_pool()["reserved_account_count"] == 0


def test_overallocation_is_transactional_and_exact(tmp_path: Path) -> None:
    service = _service(tmp_path / "capacity.db", eligible=3)
    first = _campaign(service, "first")
    second = _campaign(service, "second")
    service.repository.upsert_campaign_capacity_reservation(first["id"], 2, 3)
    with pytest.raises(ValueError, match="campaign_capacity_pool_insufficient"):
        service.repository.upsert_campaign_capacity_reservation(second["id"], 2, 3)
    assert service.repository.get_campaign_capacity_reservation(second["id"]) is None


def test_one_current_reservation_persists_after_restart(tmp_path: Path) -> None:
    database = tmp_path / "capacity.db"
    service = _service(database, eligible=3)
    campaign = _campaign(service, "persistent")
    service.allocate_campaign_capacity(campaign["id"], {"requested_account_count": 2})
    service.allocate_campaign_capacity(campaign["id"], {"requested_account_count": 2})
    reloaded = _service(database, eligible=3)
    reservation = reloaded.repository.get_campaign_capacity_reservation(campaign["id"])
    with reloaded.repository.connection() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM commercial_campaign_capacity_reservations WHERE campaign_id=?",
            (campaign["id"],),
        ).fetchone()[0]
    assert count == 1
    assert reservation["allocated_account_count"] == 2


def test_stable_name_allocator_reuses_phone_and_skips_conflicting_preference(tmp_path: Path) -> None:
    repository = CommercialQueueRepository(tmp_path / "contacts.db")
    first = repository.get_or_create_stable_contact_mapping("989120000001", "Bale-000050")
    second = repository.get_or_create_stable_contact_mapping("989120000002", "Bale-000050")
    repeated = repository.get_or_create_stable_contact_mapping("989120000001", "Bale-999999")
    assert first["stable_display_name"] == "Bale-000050"
    assert second["stable_display_name"] != "Bale-000050"
    assert second["sequence_number"] != first["sequence_number"]
    assert repeated["stable_display_name"] == first["stable_display_name"]
