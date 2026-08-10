from __future__ import annotations

from pathlib import Path

from test_commercial_campaign_lifecycle import _campaign, _service


def test_capacity_projection_matches_validate_start_runtime_ceiling(tmp_path: Path) -> None:
    service = _service(tmp_path / "capacity-consistency.db")
    service.update_account_settings(
        "bale_b",
        {"enabled": True, "source_channel_uid_override": "5613544284", "deliveries_per_round_override": 3},
    )
    campaign = _campaign(service, count=1)
    service.configure_campaign_platform_settings(
        campaign["id"],
        ["bale"],
        {
            "bale": {
                "source_uid": "5613544284",
                "source_url": "https://web.bale.ai/chat?uid=5613544284",
                "source_label": "Capacity consistency source",
                "sender_account_ids": ["bale_a", "bale_b"],
            }
        },
        created_by="test",
    )
    service.update_global_settings({"max_concurrent_accounts": 1})
    service.repository.upsert_campaign_capacity_reservation(campaign["id"], 2, 2)
    service.account_readiness_matrix = lambda: [
        {"account_id": "bale_a", "worker_eligible": True, "enabled": True, "commercial_enabled": True, "worker_status": "idle", "authentication_status": "authenticated", "session_health_acceptable": True},
        {"account_id": "bale_b", "worker_eligible": True, "enabled": True, "commercial_enabled": True, "worker_status": "idle", "authentication_status": "authenticated", "session_health_acceptable": True},
    ]

    capacity = service.campaign_capacity_state(campaign["id"])
    validation = service.validate_campaign_start(campaign["id"])

    assert capacity["eligible_account_count"] == 2
    assert capacity["ready_for_exact_account_execution"] is False
    assert capacity["exact_concurrency"]["max_concurrent_accounts"] == 1
    assert capacity["concurrency_floor_shortfalls"] == {
        "max_concurrent_accounts": 1,
        "browser_concurrency": 1,
        "worker_concurrency": 1,
    }
    assert "requested_accounts_exceed_runtime_capacity" in capacity["exact_blockers"]
    assert validation["can_start"] is False
    assert "requested_accounts_exceed_runtime_capacity" in validation["blocking_reasons"]
