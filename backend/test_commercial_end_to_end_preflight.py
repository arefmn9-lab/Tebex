from __future__ import annotations

import tempfile
import time
from pathlib import Path
from statistics import median
from typing import Any

from modules.automation_engine.commercial_queue.repository import CommercialQueueRepository
from modules.automation_engine.commercial_queue.service import CommercialQueueService
from modules.automation_engine.plugins.bale.contact_store import BaleContactStore


def _service(path: Path, contact_path: Path | None = None) -> CommercialQueueService:
    service = CommercialQueueService(repository=CommercialQueueRepository(path))
    service.contact_store = BaleContactStore(contact_path or (path.parent / "contacts.json"))
    return service


def _counts(service: CommercialQueueService) -> dict[str, int]:
    repo = service.repository
    campaign_ids = [row["id"] for row in repo.list_campaigns(None, 100000, 0)]
    jobs = sum(len(repo.list_campaign_jobs_all(campaign_id)) for campaign_id in campaign_ids)
    scenarios = sum(len(repo.list_campaign_recipient_report_rows(campaign_id, 100000, 0)) for campaign_id in campaign_ids)
    approvals = sum(len(repo.list_live_execution_approvals(campaign_id, 100000, 0)) for campaign_id in campaign_ids)
    return {
        "jobs": jobs,
        "queued_jobs": sum(1 for campaign_id in campaign_ids for job in repo.list_campaign_jobs_all(campaign_id) if job["status"] == "queued"),
        "scenarios": scenarios,
        "approvals": approvals,
        "global_contacts": repo.count_global_contacts(),
    }


def _configure(service: CommercialQueueService, campaign_id: str, platforms: list[str], with_accounts: bool = True) -> dict[str, Any]:
    settings = {
        platform: {
            "source_uid": f"{platform}-source-001",
            "source_url": f"https://source.example/{platform}/platform-{platform}-source-001",
            "source_label": f"{platform} source",
            "sender_account_ids": [f"{platform}_sender_1"],
        }
        for platform in platforms
    }
    for platform in platforms:
        service.repository.upsert_account_settings(f"{platform}_sender_1", {"enabled": bool(with_accounts), "priority": 1, "worker_status": "idle"})
        if with_accounts:
            service.set_account_health(f"{platform}_sender_1", "healthy")
        else:
            service.set_account_health(f"{platform}_sender_1", "disabled")
    return service.configure_campaign_platform_settings(campaign_id, platforms, settings, created_by="test")


def _canonical_flow(service: CommercialQueueService, phones: list[str], platforms: list[str], with_accounts: bool = True) -> dict[str, Any]:
    campaign = service.create_campaign({"name": "E2E", "platform": "multi"})
    preview = service.preview_campaign_recipients(campaign["id"], phones)
    confirm = service.confirm_recipient_manifest(campaign["id"], phones, confirmation_checked=True, submitted_by="test")
    materialized = service.materialize_campaign_recipients(
        campaign["id"],
        platforms,
        authorize_for_live_execution=True,
        authorized_by="test",
        authorization_note="Temp-DB end-to-end preflight test only.",
    )
    configured = _configure(service, campaign["id"], platforms, with_accounts=with_accounts)
    review = service.final_review(campaign["id"])
    approval = service.request_send_approval(campaign["id"], review["final_review_hash"], explicit_confirmation=True, requested_by="test")
    preflight = service.live_preflight(campaign["id"], approval_id=approval["approval"]["approval_id"])
    return {
        "campaign": campaign,
        "preview": preview,
        "confirm": confirm,
        "materialized": materialized,
        "configured": configured,
        "review": review,
        "approval": approval,
        "preflight": preflight,
    }


def _codes(preflight: dict[str, Any]) -> set[str]:
    return {str(item.get("error_code")) for item in preflight.get("blocking_errors") or []}


def test_one_phone_one_platform_import_to_unified_preflight() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "e2e.db")
        result = _canonical_flow(service, ["09304073331"], ["bale"])

    assert result["preview"]["read_only"] is True
    assert result["confirm"]["created_job_count"] == 0
    assert result["materialized"]["scenario_count"] == 1
    assert result["materialized"]["platform_run_count"] == 1
    assert result["review"]["recipient_scenario_summary"]["scenario_count"] == 1
    assert result["preflight"]["execute_allowed"] is False
    assert result["preflight"]["ready"] is True


def test_one_phone_three_platforms_creates_one_scenario_three_platform_runs() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "e2e.db")
        result = _canonical_flow(service, ["09304073331"], ["bale", "telegram", "whatsapp"])
        scenario = result["materialized"]["items"][0]

    assert result["materialized"]["scenario_count"] == 1
    assert result["materialized"]["platform_run_count"] == 3
    assert sorted(run["platform"] for run in scenario["platform_runs"]) == ["bale", "telegram", "whatsapp"]


def test_multiple_phones_create_one_scenario_per_normalized_phone() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "e2e.db")
        result = _canonical_flow(service, ["09304073331", "09304073332", "09304073333"], ["bale", "telegram"])

    assert result["materialized"]["scenario_count"] == 3
    assert result["materialized"]["platform_run_count"] == 6


def test_duplicate_inputs_do_not_duplicate_global_contact_or_scenario() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "e2e.db")
        result = _canonical_flow(service, ["09304073331", "989304073331", "09304073331"], ["bale"])
        global_contact_count = service.repository.count_global_contacts()

    assert result["preview"]["duplicate_count"] == 2
    assert global_contact_count == 1
    assert result["materialized"]["scenario_count"] == 1


def test_preview_is_side_effect_free() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "e2e.db")
        campaign = service.create_campaign({"name": "Preview", "platform": "multi"})
        before = _counts(service)
        preview = service.preview_campaign_recipients(campaign["id"], ["09304073331", "bad"])
        after = _counts(service)

    assert preview["valid_count"] == 1
    assert before == after


def test_confirm_manifest_creates_no_jobs_or_queue_side_effects() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "e2e.db")
        campaign = service.create_campaign({"name": "Confirm", "platform": "multi"})
        result = service.confirm_recipient_manifest(campaign["id"], ["09304073331"], confirmation_checked=True)
        counts = _counts(service)

    assert result["manifest"]["confirmation_status"] == "confirmed"
    assert counts["jobs"] == 0
    assert counts["queued_jobs"] == 0
    assert counts["scenarios"] == 0


def test_materialize_creates_planning_rows_only() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "e2e.db")
        campaign = service.create_campaign({"name": "Materialize", "platform": "multi"})
        service.confirm_recipient_manifest(campaign["id"], ["09304073331"], confirmation_checked=True)
        result = service.materialize_campaign_recipients(campaign["id"], ["bale", "telegram"], authorize_for_live_execution=True)
        counts = _counts(service)

    assert result["created_job_count"] == 0
    assert counts["jobs"] == 0
    assert counts["scenarios"] == 1
    assert result["platform_run_count"] == 2


def test_source_independence_and_source_change_invalidates_prior_approval() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "e2e.db")
        result = _canonical_flow(service, ["09304073331"], ["bale", "telegram", "whatsapp"])
        campaign_id = result["campaign"]["id"]
        sources = result["review"]["platform_sources"]
        old_hash = result["review"]["final_review_hash"]

        assert sources["bale"]["source_uid"] != sources["telegram"]["source_uid"]
        assert sources["telegram"]["source_uid"] != sources["whatsapp"]["source_uid"]

        changed_settings = {
            "bale": {"source_uid": "bale-source-002", "source_url": "https://source.example/bale/platform-bale-source-002", "sender_account_ids": ["bale_sender_1"]},
            "telegram": {"source_uid": "telegram-source-001", "source_url": "https://source.example/telegram/platform-telegram-source-001", "sender_account_ids": ["telegram_sender_1"]},
            "whatsapp": {"source_uid": "whatsapp-source-001", "source_url": "https://source.example/whatsapp/platform-whatsapp-source-001", "sender_account_ids": ["whatsapp_sender_1"]},
        }
        service.configure_campaign_platform_settings(campaign_id, ["bale", "telegram", "whatsapp"], changed_settings)
        new_review = service.final_review(campaign_id)

    assert new_review["final_review_hash"] != old_hash
    assert new_review["platform_sources"]["bale"]["source_uid"] == "bale-source-002"


def test_approval_and_preflight_create_no_jobs_or_workers() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "e2e.db")
        campaign = service.create_campaign({"name": "No jobs", "platform": "multi"})
        service.confirm_recipient_manifest(campaign["id"], ["09304073331"], confirmation_checked=True)
        service.materialize_campaign_recipients(campaign["id"], ["bale"], authorize_for_live_execution=True)
        _configure(service, campaign["id"], ["bale"])
        before_review = _counts(service)
        review = service.final_review(campaign["id"])
        after_review = _counts(service)
        approval = service.request_send_approval(campaign["id"], review["final_review_hash"], explicit_confirmation=True)
        after_approval = _counts(service)
        preflight = service.live_preflight(campaign["id"], approval["approval"]["approval_id"])
        after_preflight = _counts(service)

    assert before_review == after_review
    assert after_approval["approvals"] == after_review["approvals"] + 1
    assert after_approval["jobs"] == after_review["jobs"] == 0
    assert after_preflight == after_approval
    assert preflight["execute_allowed"] is False


def test_missing_sender_account_is_not_recipient_account_not_found() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "e2e.db")
        result = _canonical_flow(service, ["09304073331"], ["telegram"], with_accounts=False)

    assert "sender_account_unavailable" in _codes(result["preflight"])
    assert "account_not_found" not in _codes(result["preflight"])
    assert "recipient_account_not_found" not in _codes(result["preflight"])


def test_historical_job_never_reused_by_preflight() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "e2e.db")
        result = _canonical_flow(service, ["09304073331"], ["bale"])
        campaign = result["campaign"]
        service.repository.create_recipient_and_job(campaign, "09304073332", "989304073332", None, "historical")
        preflight = service.live_preflight(campaign["id"], result["approval"]["approval"]["approval_id"])

    assert "duplicate_execution_scope" not in _codes(preflight)
    assert preflight["execute_allowed"] is False


def test_same_phone_another_campaign_reuses_global_contact_and_creates_new_scenario() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "e2e.db")
        first = _canonical_flow(service, ["09304073331"], ["bale"])
        second = _canonical_flow(service, ["989304073331"], ["bale"])
        global_contact_count = service.repository.count_global_contacts()

    assert global_contact_count == 1
    assert first["materialized"]["items"][0]["id"] != second["materialized"]["items"][0]["id"]


def _run_6000_row_import_sample() -> float:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp) / "e2e.db", Path(tmp) / "contacts.json")
        phones = [f"09304{i:06d}" for i in range(6000)]
        started = time.perf_counter()
        campaign = service.create_campaign({"name": "Large", "platform": "multi"})
        preview = service.preview_campaign_recipients(campaign["id"], phones)
        service.confirm_recipient_manifest(campaign["id"], phones, confirmation_checked=True)
        materialized = service.materialize_campaign_recipients(campaign["id"], ["bale"], authorize_for_live_execution=True)
        elapsed = time.perf_counter() - started
        platform_identities = service.contact_store.list_platform_contact_identities("bale")

    assert preview["valid_count"] == 6000
    assert materialized["scenario_count"] == 6000
    assert materialized["created_job_count"] == 0
    assert platform_identities == []
    return elapsed


def test_6000_row_import_completes_without_live_execution_or_stable_names() -> None:
    elapsed_samples = [_run_6000_row_import_sample() for _ in range(3)]
    sample_median = median(elapsed_samples)
    print(
        "6000_ROW_IMPORT_TIMINGS "
        f"samples={[round(value, 3) for value in elapsed_samples]} "
        f"median={sample_median:.3f}s max={max(elapsed_samples):.3f}s"
    )
    assert sample_median < 30
    assert max(elapsed_samples) < 90


if __name__ == "__main__":
    test_one_phone_one_platform_import_to_unified_preflight()
    test_one_phone_three_platforms_creates_one_scenario_three_platform_runs()
    test_multiple_phones_create_one_scenario_per_normalized_phone()
    test_duplicate_inputs_do_not_duplicate_global_contact_or_scenario()
    test_preview_is_side_effect_free()
    test_confirm_manifest_creates_no_jobs_or_queue_side_effects()
    test_materialize_creates_planning_rows_only()
    test_source_independence_and_source_change_invalidates_prior_approval()
    test_approval_and_preflight_create_no_jobs_or_workers()
    test_missing_sender_account_is_not_recipient_account_not_found()
    test_historical_job_never_reused_by_preflight()
    test_same_phone_another_campaign_reuses_global_contact_and_creates_new_scenario()
    test_6000_row_import_completes_without_live_execution_or_stable_names()
    print("Commercial end-to-end preflight tests passed")
