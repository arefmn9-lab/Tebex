from __future__ import annotations

import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from modules.automation_engine.commercial_queue.service import (
    AUTHORIZED_PHASE5D_NAME,
    CONTROLLED_LIVE_NO_SEND_ENV,
    CONTROLLED_LIVE_NO_SEND_MODE,
    CONTROLLED_SINGLE_RECIPIENT_ACCOUNT_ID,
    CONTROLLED_SINGLE_RECIPIENT_SOURCE_UID,
    CONTROLLED_SINGLE_RECIPIENT_SOURCE_URL,
    CampaignLifecycleError,
)
from test_commercial_end_to_end_preflight import _configure, _service


def _approved_flow(phones: list[str], platforms: list[str]):
    tmp = tempfile.TemporaryDirectory()
    service = _service(Path(tmp.name) / "controlled.db", Path(tmp.name) / "contacts.json")
    service.account_auth_checker = lambda account_id: True
    campaign = service.create_campaign({"name": "Controlled", "platform": "multi"})
    service.confirm_recipient_manifest(campaign["id"], phones, confirmation_checked=True, submitted_by="test")
    service.materialize_campaign_recipients(
        campaign["id"],
        platforms,
        authorize_for_live_execution=True,
        authorized_by="test",
        authorization_note="Synthetic controlled execution test only.",
    )
    _configure(service, campaign["id"], platforms, with_accounts=True)
    for platform in platforms:
        service.repository.upsert_account_settings(f"{platform}_sender_1", {"source_channel_uid_override": f"{platform}-source-001"})
        service.account_health.repository.upsert({
            "account_id": f"{platform}_sender_1",
            "health_status": "healthy",
            "last_authentication_verified_at": "2026-07-17T00:00:00+00:00",
        })
    review = service.final_review(campaign["id"])
    approval = service.request_send_approval(campaign["id"], review["final_review_hash"], requested_by="test", explicit_confirmation=True)["approval"]
    return tmp, service, campaign, review, approval


def _execute(service, campaign, review, approval, key: str, platforms: list[str] | None = None):
    return service.request_controlled_execution(
        campaign_id=campaign["id"],
        approval_id=approval["approval_id"],
        final_review_hash=review["final_review_hash"],
        execution_snapshot_id=review["execution_snapshot"]["snapshot_id"],
        idempotency_key=key,
        requested_by="test",
        mode="mock_only",
        selected_platforms=platforms,
    )


def _retry(service, campaign, scenario_id: str, review, approval, key: str, platforms: list[str] | None = None):
    return service.retry_controlled_recipient_scenario(
        campaign_id=campaign["id"],
        campaign_recipient_run_id=scenario_id,
        approval_id=approval["approval_id"],
        final_review_hash=review["final_review_hash"],
        execution_snapshot_id=review["execution_snapshot"]["snapshot_id"],
        idempotency_key=key,
        requested_by="test",
        mode="mock_only",
        selected_platforms=platforms,
    )


def _authorized_no_send_flow(phones: list[str] | None = None):
    tmp = tempfile.TemporaryDirectory()
    service = _service(Path(tmp.name) / "no-send.db", Path(tmp.name) / "contacts.json")
    service.account_auth_checker = lambda account_id: True
    campaign = service.create_campaign({"name": "Controlled No Send", "platform": "bale"})
    selected_phones = phones or ["09304073331"]
    service.confirm_recipient_manifest(campaign["id"], selected_phones, confirmation_checked=True, submitted_by="test")
    service.materialize_campaign_recipients(
        campaign["id"],
        ["bale"],
        authorize_for_live_execution=True,
        authorized_by="test",
        authorization_note="Explicitly authorized controlled no-send test recipient.",
    )
    service.configure_campaign_platform_settings(
        campaign["id"],
        ["bale"],
        {
            "bale": {
                "source_uid": CONTROLLED_SINGLE_RECIPIENT_SOURCE_UID,
                "source_url": CONTROLLED_SINGLE_RECIPIENT_SOURCE_URL,
                "source_label": "controlled no-send source",
                "sender_account_ids": [CONTROLLED_SINGLE_RECIPIENT_ACCOUNT_ID],
            }
        },
        created_by="test",
    )
    service.repository.upsert_account_settings(CONTROLLED_SINGLE_RECIPIENT_ACCOUNT_ID, {"enabled": True, "priority": 1, "worker_status": "idle"})
    service.account_health.repository.upsert({
        "account_id": CONTROLLED_SINGLE_RECIPIENT_ACCOUNT_ID,
        "health_status": "healthy",
        "last_authentication_verified_at": "2026-07-17T00:00:00+00:00",
    })
    with service.repository.connection() as connection:
        connection.execute(
            "UPDATE commercial_recipients SET display_name = ?, stable_display_name = ?, bale_contact_verified = 1, bale_verification_status = 'verified' WHERE campaign_id = ? AND phone_normalized = ?",
            (AUTHORIZED_PHASE5D_NAME, AUTHORIZED_PHASE5D_NAME, campaign["id"], "989304073331"),
        )
        connection.commit()
    review = service.final_review(campaign["id"])
    approval = service.request_send_approval(campaign["id"], review["final_review_hash"], requested_by="test", explicit_confirmation=True)["approval"]
    return tmp, service, campaign, review, approval


def _request_no_send(service, campaign, review, approval, key: str = "no-send"):
    return service.request_controlled_execution(
        campaign_id=campaign["id"],
        approval_id=approval["approval_id"],
        final_review_hash=review["final_review_hash"],
        execution_snapshot_id=review["execution_snapshot"]["snapshot_id"],
        idempotency_key=key,
        requested_by="test",
        mode=CONTROLLED_LIVE_NO_SEND_MODE,
        selected_platforms=["bale"],
    )


class NoSendAdapter:
    def __init__(self) -> None:
        self.no_send_plans = []
        self.execute_plan_called = 0

    def execute_plan(self, *args, **kwargs):
        self.execute_plan_called += 1
        raise AssertionError("controlled_live_no_send must not call normal execute_plan")

    def controlled_live_no_send(self, plan, runtime_session=None):
        self.no_send_plans.append(plan)
        return {
            "success": True,
            "stopped_before_send": True,
            "source_resolved": True,
            "recipient_resolved": True,
            "composer_visible": True,
            "final_send_control_visible": True,
            "authentication_state": "authenticated",
            "sender_account_state": "available",
            "recipient_account_state": "resolved",
            "remote_message_id": None,
        }


def test_valid_approved_preflight_creates_batch_and_linked_jobs() -> None:
    tmp, service, campaign, review, approval = _approved_flow(["09304000001"], ["bale"])
    with tmp:
        result = _execute(service, campaign, review, approval, "exec-1")
        job = result["jobs"][0]
        plan = json.loads(job["execution_plan_json"])
    assert result["created_job_count"] == 1
    assert result["batch"]["mode"] == "mock_only"
    assert job["execution_batch_id"] == result["batch"]["id"]
    assert job["platform_run_id"] == plan["platform_run_id"]


def test_execute_requires_approval_current_hash_snapshot_and_idempotency() -> None:
    tmp, service, campaign, review, approval = _approved_flow(["09304000002"], ["bale"])
    with tmp:
        checks = []
        for kwargs in [
            {"approval_id": "missing"},
            {"final_review_hash": "stale"},
            {"execution_snapshot_id": "stale"},
            {"idempotency_key": ""},
            {"mode": "disabled"},
        ]:
            try:
                service.request_controlled_execution(
                    campaign["id"],
                    kwargs.get("approval_id", approval["approval_id"]),
                    kwargs.get("final_review_hash", review["final_review_hash"]),
                    kwargs.get("execution_snapshot_id", review["execution_snapshot"]["snapshot_id"]),
                    kwargs.get("idempotency_key", "exec-invalid"),
                    mode=kwargs.get("mode", "mock_only"),
                )
            except (CampaignLifecycleError, KeyError):
                checks.append(True)
        jobs = service.list_jobs(campaign_id=campaign["id"], limit=100)["items"]
    assert checks == [True, True, True, True, True]
    assert jobs == []


def test_same_idempotency_key_returns_same_batch_without_duplicate_jobs() -> None:
    tmp, service, campaign, review, approval = _approved_flow(["09304000003"], ["bale"])
    with tmp:
        first = _execute(service, campaign, review, approval, "same-key")
        second = _execute(service, campaign, review, approval, "same-key")
        jobs = service.list_jobs(campaign_id=campaign["id"], limit=100)["items"]
    assert first["batch"]["id"] == second["batch"]["id"]
    assert len(jobs) == 1


def test_one_phone_three_platforms_creates_three_jobs() -> None:
    tmp, service, campaign, review, approval = _approved_flow(["09304000004"], ["bale", "telegram", "whatsapp"])
    with tmp:
        result = _execute(service, campaign, review, approval, "three-platforms")
    assert result["created_job_count"] == 3
    assert sorted(job["platform"] for job in result["jobs"]) == ["bale", "telegram", "whatsapp"]


def test_mixed_mock_results_complete_scenario_and_report_links() -> None:
    tmp, service, campaign, review, approval = _approved_flow(["09304000005"], ["bale", "telegram", "whatsapp"])
    with tmp:
        result = _execute(service, campaign, review, approval, "mixed")
        by_platform = {job["platform"]: job for job in result["jobs"]}
        service.apply_trusted_adapter_result(by_platform["bale"]["id"], {"outcome": "sent", "success": True, "trusted_result_key": "bale-ok"})
        service.apply_trusted_adapter_result(by_platform["telegram"]["id"], {"outcome": "account_not_found", "error_code": "account_not_found", "trusted_result_key": "tg-not-found"})
        service.apply_trusted_adapter_result(by_platform["whatsapp"]["id"], {"outcome": "failed_terminal", "error_code": "blocked", "trusted_result_key": "wa-terminal"})
        report = service.get_execution_batch_report(result["batch"]["id"])
        scenario = report["scenarios"][0]
    assert scenario["scenario_status"] == "completed"
    assert scenario["sent_count"] == 1
    assert scenario["not_found_count"] == 1
    assert scenario["failed_count"] == 1
    assert len(report["jobs"]) == 3


def test_retryable_failure_explicit_retry_only_requeues_failed_platform() -> None:
    tmp, service, campaign, review, approval = _approved_flow(["09304000006"], ["bale", "telegram"])
    with tmp:
        result = _execute(service, campaign, review, approval, "retry-source")
        by_platform = {job["platform"]: job for job in result["jobs"]}
        service.apply_trusted_adapter_result(by_platform["bale"]["id"], {"outcome": "sent", "success": True, "trusted_result_key": "sent"})
        service.apply_trusted_adapter_result(by_platform["telegram"]["id"], {"outcome": "failed_retryable", "error_code": "timeout", "retryable": True, "trusted_result_key": "retryable"})
        scenario = service.get_recipient_scenario(by_platform["telegram"]["campaign_recipient_run_id"])
        retry = _retry(service, campaign, scenario["id"], review, approval, "retry-key")
    assert scenario["scenario_status"] == "retry_pending"
    assert retry["created_job_count"] == 1
    assert retry["jobs"][0]["platform"] == "telegram"
    assert retry["jobs"][0]["previous_job_id"] == by_platform["telegram"]["id"]


def test_terminal_platforms_are_not_retried_by_default() -> None:
    tmp, service, campaign, review, approval = _approved_flow(["09304000007"], ["bale", "telegram"])
    with tmp:
        result = _execute(service, campaign, review, approval, "terminal")
        for job in result["jobs"]:
            outcome = "sent" if job["platform"] == "bale" else "account_not_found"
            service.apply_trusted_adapter_result(job["id"], {"outcome": outcome, "success": outcome == "sent", "trusted_result_key": f"{outcome}-key"})
        scenario = service.get_recipient_scenario(result["jobs"][0]["campaign_recipient_run_id"])
        raised = False
        try:
            _retry(service, campaign, scenario["id"], review, approval, "retry-terminal")
        except CampaignLifecycleError:
            raised = True
    assert raised is True


def test_sender_auth_failure_does_not_become_recipient_account_not_found() -> None:
    tmp, service, campaign, review, approval = _approved_flow(["09304000008"], ["bale"])
    with tmp:
        job = _execute(service, campaign, review, approval, "sender-auth")["jobs"][0]
        applied = service.apply_trusted_adapter_result(
            job["id"],
            {"outcome": "account_not_found", "error_category": "sender_auth", "error_code": "sender_auth_failed", "retryable": True, "trusted_result_key": "sender-auth"},
        )
        scenario = service.get_recipient_scenario(job["campaign_recipient_run_id"])
    assert applied["outcome"] == "failed_retryable"
    assert scenario["account_not_found_count"] == 0


def test_duplicate_callback_is_idempotent_and_stale_attempt_cannot_overwrite() -> None:
    tmp, service, campaign, review, approval = _approved_flow(["09304000009"], ["bale"])
    with tmp:
        first_job = _execute(service, campaign, review, approval, "stale")["jobs"][0]
        first = service.apply_trusted_adapter_result(first_job["id"], {"outcome": "failed_retryable", "error_code": "timeout", "retryable": True, "trusted_result_key": "dup"})
        duplicate = service.apply_trusted_adapter_result(first_job["id"], {"outcome": "failed_retryable", "error_code": "timeout", "retryable": True, "trusted_result_key": "dup"})
        retry = _retry(service, campaign, first_job["campaign_recipient_run_id"], review, approval, "stale-retry")
        stale = service.repository.apply_trusted_worker_result(first_job["id"], {"outcome": "sent", "trusted_result_key": "late"})
        scenario = service.get_recipient_scenario(first_job["campaign_recipient_run_id"])
    assert first["applied"] is True
    assert duplicate["idempotent"] is True
    assert retry["jobs"][0]["id"] != first_job["id"]
    assert stale["applied"] is False
    assert scenario["sent_platform_count"] == 0


def test_concurrent_claims_and_concurrent_execute_do_not_duplicate_jobs() -> None:
    tmp, service, campaign, review, approval = _approved_flow(["09304000010"], ["bale"])
    with tmp:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: _execute(service, campaign, review, approval, "concurrent"), range(2)))
        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = list(executor.map(lambda account: service.assign_jobs(account, campaign["id"], 1), ["bale_sender_1", "bale_sender_2"]))
        jobs = service.list_jobs(campaign_id=campaign["id"], limit=100)["items"]
    assert results[0]["batch"]["id"] == results[1]["batch"]["id"]
    assert len(jobs) == 1
    assert sum(len(claim["assigned_job_ids"]) for claim in claims) == 1


def test_cancellation_cancels_queued_jobs_and_preserves_terminal_outcomes() -> None:
    tmp, service, campaign, review, approval = _approved_flow(["09304000011"], ["bale", "telegram"])
    with tmp:
        result = _execute(service, campaign, review, approval, "cancel")
        service.apply_trusted_adapter_result(result["jobs"][0]["id"], {"outcome": "sent", "success": True, "trusted_result_key": "terminal"})
        cancelled = service.cancel_execution_batch(result["batch"]["id"], "test_cancel")
        scenario = service.get_recipient_scenario(result["jobs"][0]["campaign_recipient_run_id"])
    assert cancelled["status"] == "cancelled"
    assert scenario["sent_platform_count"] == 1
    assert scenario["pending_platform_count"] == 0


def test_mock_worker_uses_immutable_execution_plan_after_source_change() -> None:
    tmp, service, campaign, review, approval = _approved_flow(["09304000012"], ["bale"])
    with tmp:
        result = _execute(service, campaign, review, approval, "plan")
        job = result["jobs"][0]
        original_plan = json.loads(job["execution_plan_json"])
        service.repository.upsert_account_settings("bale_sender_1", {"source_channel_uid_override": "changed-after-job"})
        worker = service.run_controlled_mock_worker("bale_sender_1", campaign["id"], {job["id"]: {"outcome": "sent", "success": True, "trusted_result_key": "mock-worker"}})
        worker_plan = worker["results"][0]["execution_plan"]
    assert worker["real_adapter_called"] is False
    assert worker_plan["source"] == original_plan["source"]


def test_historical_job_never_reused_or_retried() -> None:
    tmp, service, campaign, review, approval = _approved_flow(["09304000013"], ["bale"])
    with tmp:
        legacy_campaign = service.create_campaign({"name": "Legacy", "platform": "bale"})
        legacy_recipient, legacy_job = service.repository.create_recipient_and_job(legacy_campaign, "09304000999", "989304000999", None, "historical")
        result = _execute(service, campaign, review, approval, "historical")
        job_ids = {job["id"] for job in result["jobs"]}
    assert legacy_job["id"] not in job_ids
    assert legacy_recipient["id"] not in {job["recipient_id"] for job in result["jobs"]}


def test_controlled_live_no_send_disabled_by_default_and_rejects_bulk_scope() -> None:
    old_env = os.environ.pop(CONTROLLED_LIVE_NO_SEND_ENV, None)
    tmp, service, campaign, review, approval = _authorized_no_send_flow()
    with tmp:
        try:
            _request_no_send(service, campaign, review, approval, "disabled")
            raise AssertionError("no-send execution must be disabled by default")
        except CampaignLifecycleError as exc:
            assert exc.error_code == "controlled_live_no_send_disabled"
        assert service.list_jobs(campaign_id=campaign["id"], limit=100)["items"] == []
    if old_env is not None:
        os.environ[CONTROLLED_LIVE_NO_SEND_ENV] = old_env

    os.environ[CONTROLLED_LIVE_NO_SEND_ENV] = "1"
    tmp, service, campaign, review, approval = _authorized_no_send_flow(["09304073331", "09304073332"])
    with tmp:
        try:
            _request_no_send(service, campaign, review, approval, "bulk")
            raise AssertionError("bulk campaigns must not enter controlled no-send execution")
        except CampaignLifecycleError as exc:
            assert exc.error_code in {"controlled_live_no_send_single_recipient_required", "controlled_live_no_send_recipient_not_authorized"}
        assert service.list_jobs(campaign_id=campaign["id"], limit=100)["items"] == []
    os.environ.pop(CONTROLLED_LIVE_NO_SEND_ENV, None)
    if old_env is not None:
        os.environ[CONTROLLED_LIVE_NO_SEND_ENV] = old_env


def test_controlled_live_no_send_job_cannot_report_sent_and_worker_uses_immutable_plan() -> None:
    old_env = os.environ.get(CONTROLLED_LIVE_NO_SEND_ENV)
    os.environ[CONTROLLED_LIVE_NO_SEND_ENV] = "1"
    tmp, service, campaign, review, approval = _authorized_no_send_flow()
    adapter = NoSendAdapter()
    service.platform_adapters["bale"] = adapter
    with tmp:
        result = _request_no_send(service, campaign, review, approval, "immutable-no-send")
        job = result["jobs"][0]
        original_plan = json.loads(job["execution_plan_json"])
        try:
            service.apply_trusted_adapter_result(job["id"], {"outcome": "sent", "success": True, "trusted_result_key": "bad-sent"})
            raise AssertionError("no-send jobs must not accept sent outcomes")
        except CampaignLifecycleError as exc:
            assert exc.error_code == "controlled_live_no_send_cannot_report_sent"
        service.repository.upsert_account_settings(CONTROLLED_SINGLE_RECIPIENT_ACCOUNT_ID, {"source_channel_uid_override": "changed-after-job"})
        worker = service.run_controlled_live_no_send_worker(CONTROLLED_SINGLE_RECIPIENT_ACCOUNT_ID, campaign["id"])
        used_plan = adapter.no_send_plans[0]
    assert result["created_job_count"] == 1
    assert result["batch"]["mode"] == CONTROLLED_LIVE_NO_SEND_MODE
    assert worker["stopped_before_send"] is True
    assert adapter.execute_plan_called == 0
    assert used_plan.source_channel_uid == original_plan["source_channel_uid"] == CONTROLLED_SINGLE_RECIPIENT_SOURCE_UID
    assert used_plan.phone == "989304073331"
    assert used_plan.display_name == AUTHORIZED_PHASE5D_NAME
    if old_env is None:
        os.environ.pop(CONTROLLED_LIVE_NO_SEND_ENV, None)
    else:
        os.environ[CONTROLLED_LIVE_NO_SEND_ENV] = old_env


if __name__ == "__main__":
    test_valid_approved_preflight_creates_batch_and_linked_jobs()
    test_execute_requires_approval_current_hash_snapshot_and_idempotency()
    test_same_idempotency_key_returns_same_batch_without_duplicate_jobs()
    test_one_phone_three_platforms_creates_three_jobs()
    test_mixed_mock_results_complete_scenario_and_report_links()
    test_retryable_failure_explicit_retry_only_requeues_failed_platform()
    test_terminal_platforms_are_not_retried_by_default()
    test_sender_auth_failure_does_not_become_recipient_account_not_found()
    test_duplicate_callback_is_idempotent_and_stale_attempt_cannot_overwrite()
    test_concurrent_claims_and_concurrent_execute_do_not_duplicate_jobs()
    test_cancellation_cancels_queued_jobs_and_preserves_terminal_outcomes()
    test_mock_worker_uses_immutable_execution_plan_after_source_change()
    test_historical_job_never_reused_or_retried()
    test_controlled_live_no_send_disabled_by_default_and_rejects_bulk_scope()
    test_controlled_live_no_send_job_cannot_report_sent_and_worker_uses_immutable_plan()
    print("Commercial controlled execution tests passed")
