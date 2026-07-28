from __future__ import annotations

import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from app.routes import automation as automation_routes
from fastapi import HTTPException, Response
from modules.automation_engine.commercial_queue.repository import CommercialQueueRepository
from modules.automation_engine.commercial_queue.service import CommercialQueueService


def _service(path: Path) -> CommercialQueueService:
    return CommercialQueueService(repository=CommercialQueueRepository(path))


def _scenario(service: CommercialQueueService, platforms: list[str] | None = None) -> dict[str, Any]:
    campaign = service.create_campaign({"name": "Scenario", "platform": "multi"})
    imported = service.import_recipients(campaign["id"], ["09304073331"])
    recipient = imported["created_recipients"][0]
    service.authorize_recipient_live(recipient["id"], "temp test authorization", authorized_by="test")
    return service.start_recipient_scenario(
        campaign["id"],
        recipient["id"],
        platforms or ["bale", "telegram", "whatsapp"],
        global_contact_id=f"global:{recipient['phone_normalized']}",
    )


def _platforms(scenario: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item["platform"]): item for item in scenario["platform_runs"]}


def test_sent_not_found_sent_completes_unified_scenario() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "commercial.db")
        scenario = _scenario(service)
        runs = _platforms(scenario)

        service.update_platform_run_outcome(runs["bale"]["id"], "sent", {"stable_display_name": "Bale-000123"})
        service.update_platform_run_outcome(runs["telegram"]["id"], "account_not_found")
        updated = service.update_platform_run_outcome(runs["whatsapp"]["id"], "sent", {"stable_display_name": "WhatsApp-000052"})["scenario"]

    assert updated["scenario_status"] == "completed"
    assert updated["selected_platform_count"] == 3
    assert updated["checked_platform_count"] == 3
    assert updated["sent_platform_count"] == 2
    assert updated["account_not_found_count"] == 1
    assert updated["failed_platform_count"] == 0


def test_account_not_found_is_checked_terminal_not_sent_and_not_retried_by_default() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "commercial.db")
        scenario = _scenario(service, ["telegram"])
        telegram = _platforms(scenario)["telegram"]

        updated = service.update_platform_run_outcome(telegram["id"], "account_not_found")["scenario"]
        retry = service.retry_recipient_scenario(scenario["id"])

    assert updated["scenario_status"] == "completed"
    assert updated["checked_platform_count"] == 1
    assert updated["sent_platform_count"] == 0
    assert updated["account_not_found_count"] == 1
    assert retry["requeued_platform_run_count"] == 0


def test_failed_retryable_produces_retry_pending_and_only_that_platform_requeues() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "commercial.db")
        scenario = _scenario(service)
        runs = _platforms(scenario)

        service.update_platform_run_outcome(runs["bale"]["id"], "sent", {"stable_display_name": "Bale-000123"})
        service.update_platform_run_outcome(runs["telegram"]["id"], "failed_retryable", {"last_error_code": "timeout"})
        retry_pending = service.update_platform_run_outcome(runs["whatsapp"]["id"], "account_not_found")["scenario"]
        retry = service.retry_recipient_scenario(scenario["id"])
        retried = _platforms(retry["scenario"])

    assert retry_pending["scenario_status"] == "retry_pending"
    assert retry_pending["retryable_platform_count"] == 1
    assert retry["requeued_platform_run_count"] == 1
    assert retry["requeued_platforms"] == ["telegram"]
    assert retried["bale"]["outcome"] == "sent"
    assert retried["telegram"]["outcome"] == "pending"
    assert retried["whatsapp"]["outcome"] == "account_not_found"


def test_linked_delivery_job_completion_updates_platform_run_and_retry_recreates_only_failed_job() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "commercial.db")
        campaign = service.create_campaign({"name": "Linked Jobs", "platform": "multi"})
        imported = service.import_recipients(campaign["id"], ["09304073331"])
        recipient = imported["created_recipients"][0]
        service.authorize_recipient_live(recipient["id"], "temp test authorization", authorized_by="test")
        scenario = service.start_recipient_scenario(
            campaign["id"],
            recipient["id"],
            ["bale", "telegram", "whatsapp"],
            create_delivery_jobs=True,
        )
        runs = _platforms(scenario)
        original_telegram_job_id = runs["telegram"]["delivery_job_id"]

        service.repository.complete_job(runs["bale"]["delivery_job_id"], "succeeded", {"result_success": True, "verified_forwarded_recipient_count": 1, "forward_verified": True, "diagnostics_consistent": True})
        service.repository.complete_job(runs["telegram"]["delivery_job_id"], "failed", {"retryable": True, "last_error_code": "timeout"})
        service.repository.complete_job(runs["whatsapp"]["delivery_job_id"], "succeeded", {"result_success": True, "verified_forwarded_recipient_count": 1, "forward_verified": True, "diagnostics_consistent": True})
        retry_pending = service.get_recipient_scenario(scenario["id"])
        retry = service.retry_recipient_scenario(scenario["id"])
        retried = _platforms(retry["scenario"])

    assert retry_pending["scenario_status"] == "retry_pending"
    assert retry["requeued_platforms"] == ["telegram"]
    assert retried["bale"]["outcome"] == "sent"
    assert retried["telegram"]["outcome"] == "queued"
    assert retried["telegram"]["delivery_job_id"] != original_telegram_job_id
    assert retried["whatsapp"]["outcome"] == "sent"


def test_linked_submitted_delivery_job_is_terminal_sent_without_delivery_verification() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "commercial.db")
        campaign = service.create_campaign({"name": "Linked Submitted", "platform": "multi"})
        imported = service.import_recipients(campaign["id"], ["09304073331"])
        recipient = imported["created_recipients"][0]
        service.authorize_recipient_live(recipient["id"], "temp test authorization", authorized_by="test")
        scenario = service.start_recipient_scenario(campaign["id"], recipient["id"], ["bale"], create_delivery_jobs=True)
        bale = _platforms(scenario)["bale"]

        service.repository.complete_job(
            bale["delivery_job_id"],
            "succeeded",
            {
                "result_success": True,
                "delivery_status": "submitted",
                "send_action_verified": True,
                "delivery_verified": False,
                "verified_forwarded_recipient_count": 0,
                "forward_verified": False,
                "diagnostics_consistent": True,
            },
        )
        updated = service.get_recipient_scenario(scenario["id"])
        retry = service.retry_recipient_scenario(scenario["id"])

    assert _platforms(updated)["bale"]["outcome"] == "sent"
    assert updated["scenario_status"] == "completed"
    assert updated["sent_platform_count"] == 1
    assert updated["failed_platform_count"] == 0
    assert retry["requeued_platform_run_count"] == 0


def test_repeated_scenario_creation_is_idempotent_for_campaign_phone() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "commercial.db")
        campaign = service.create_campaign({"name": "Idempotent", "platform": "multi"})
        imported = service.import_recipients(campaign["id"], ["09304073331"])
        recipient = imported["created_recipients"][0]
        service.authorize_recipient_live(recipient["id"], "temp test authorization", authorized_by="test")

        first = service.start_recipient_scenario(campaign["id"], recipient["id"], ["bale", "telegram"])
        second = service.start_recipient_scenario(campaign["id"], recipient["id"], ["bale", "telegram"])
        report = service.list_recipient_scenario_report(campaign["id"])["items"]

    assert second["id"] == first["id"]
    assert len(report) == 1
    assert len(second["platform_runs"]) == 2


def test_concurrent_scenario_creation_creates_one_recipient_run() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "commercial.db")
        campaign = service.create_campaign({"name": "Concurrent", "platform": "multi"})
        imported = service.import_recipients(campaign["id"], ["09304073331"])
        recipient = imported["created_recipients"][0]
        service.authorize_recipient_live(recipient["id"], "temp test authorization", authorized_by="test")

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [
                executor.submit(service.start_recipient_scenario, campaign["id"], recipient["id"], ["bale", "telegram", "whatsapp"])
                for _ in range(4)
            ]
            results = [future.result() for future in futures]
        report = service.list_recipient_scenario_report(campaign["id"])["items"]

    assert {result["id"] for result in results} == {results[0]["id"]}
    assert len(report) == 1
    assert report[0]["selected_platforms"] == ["bale", "telegram", "whatsapp"]


def test_terminal_sent_outcome_cannot_be_silently_downgraded() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "commercial.db")
        scenario = _scenario(service, ["bale"])
        bale = _platforms(scenario)["bale"]
        service.update_platform_run_outcome(bale["id"], "sent", {"stable_display_name": "Bale-000123"})
        service.update_platform_run_outcome(bale["id"], "sent", {"stable_display_name": "Bale-000123"})
        try:
            service.update_platform_run_outcome(bale["id"], "failed_terminal", {"last_error_code": "late_failure"})
            raised = False
        except ValueError as exc:
            raised = str(exc) == "terminal_platform_outcome_immutable"

    assert raised is True


def test_public_api_cannot_forge_platform_outcome() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        previous_service = automation_routes.commercial_queue_service
        service = _service(Path(tmp_dir) / "commercial.db")
        automation_routes.commercial_queue_service = service
        try:
            campaign = service.create_campaign({"name": "API Safety", "platform": "multi"})
            imported = service.import_recipients(campaign["id"], ["09304073331"])
            recipient = imported["created_recipients"][0]
            service.authorize_recipient_live(recipient["id"], "temp test authorization", authorized_by="test")
            scenario = service.start_recipient_scenario(campaign["id"], recipient["id"], ["bale"])
            bale = _platforms(scenario)["bale"]
            try:
                automation_routes.update_platform_run_outcome(
                    bale["id"],
                    automation_routes.PlatformRunOutcomeRequest(outcome="sent"),
                    Response(),
                )
                status_code = 200
            except HTTPException as exc:
                status_code = exc.status_code
        finally:
            automation_routes.commercial_queue_service = previous_service

    assert status_code == 403


def test_unauthorized_recipient_cannot_start_scenario() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "commercial.db")
        campaign = service.create_campaign({"name": "Unauthorized", "platform": "multi"})
        imported = service.import_recipients(campaign["id"], ["09304073331"])
        recipient = imported["created_recipients"][0]
        try:
            service.start_recipient_scenario(campaign["id"], recipient["id"], ["bale"])
            raised = False
        except Exception as exc:
            raised = getattr(exc, "error_code", "") == "recipient_live_execution_not_authorized"

    assert raised is True


def test_platform_runs_share_one_campaign_recipient_run_identity() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "commercial.db")
        scenario = _scenario(service)

    run_ids = {item["campaign_recipient_run_id"] for item in scenario["platform_runs"]}
    campaign_ids = {item["campaign_id"] for item in scenario["platform_runs"]}
    contact_ids = {item["global_contact_id"] for item in scenario["platform_runs"]}
    correlation_ids = {item["correlation_id"] for item in scenario["platform_runs"]}

    assert run_ids == {scenario["id"]}
    assert campaign_ids == {scenario["campaign_id"]}
    assert contact_ids == {scenario["global_contact_id"]}
    assert correlation_ids == {scenario["correlation_id"]}


def test_concurrent_platform_completion_keeps_unified_report_aggregate() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "commercial.db")
        scenario = _scenario(service)
        runs = _platforms(scenario)

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [
                executor.submit(service.update_platform_run_outcome, runs["bale"]["id"], "sent", {"stable_display_name": "Bale-000123"}),
                executor.submit(service.update_platform_run_outcome, runs["telegram"]["id"], "account_not_found", {}),
                executor.submit(service.update_platform_run_outcome, runs["whatsapp"]["id"], "sent", {"stable_display_name": "WhatsApp-000052"}),
            ]
            for future in futures:
                future.result()

        report = service.list_recipient_scenario_report(scenario["campaign_id"])["items"][0]

    assert report["phone"] == scenario["phone_normalized"]
    assert report["scenario_status"] == "completed"
    assert report["bale_outcome"] == "sent"
    assert report["telegram_outcome"] == "account_not_found"
    assert report["whatsapp_outcome"] == "sent"
    assert report["sent_count"] == 2
    assert report["not_found_count"] == 1
    assert report["failed_count"] == 0
    assert report["retry_pending_count"] == 0
    assert report["active_pending_count"] == 0


def test_account_not_found_does_not_allocate_stable_name_without_preparation_authorization() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "commercial.db")
        scenario = _scenario(service, ["telegram"])
        telegram = _platforms(scenario)["telegram"]

        service.update_platform_run_outcome(telegram["id"], "account_not_found")
        inspected = service.get_recipient_scenario(scenario["id"])

    platform_run = inspected["platform_runs"][0]
    assert platform_run["outcome"] == "account_not_found"
    assert platform_run["stable_display_name"] is None


if __name__ == "__main__":
    test_sent_not_found_sent_completes_unified_scenario()
    test_account_not_found_is_checked_terminal_not_sent_and_not_retried_by_default()
    test_failed_retryable_produces_retry_pending_and_only_that_platform_requeues()
    test_linked_delivery_job_completion_updates_platform_run_and_retry_recreates_only_failed_job()
    test_linked_submitted_delivery_job_is_terminal_sent_without_delivery_verification()
    test_repeated_scenario_creation_is_idempotent_for_campaign_phone()
    test_concurrent_scenario_creation_creates_one_recipient_run()
    test_terminal_sent_outcome_cannot_be_silently_downgraded()
    test_public_api_cannot_forge_platform_outcome()
    test_unauthorized_recipient_cannot_start_scenario()
    test_platform_runs_share_one_campaign_recipient_run_identity()
    test_concurrent_platform_completion_keeps_unified_report_aggregate()
    test_account_not_found_does_not_allocate_stable_name_without_preparation_authorization()
    print("Commercial recipient scenario tests passed")
