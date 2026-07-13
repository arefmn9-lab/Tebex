from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from modules.automation_engine.commercial_queue.repository import CommercialQueueRepository
from modules.automation_engine.commercial_queue.service import CommercialQueueService
from modules.automation_engine.runtime_sessions.manager import AccountRuntimeSessionManager


ACCOUNT_ID = "bale_guard"
SOURCE_UID = "5613544284"


class FakeAdapter:
    def __init__(self) -> None:
        self.created = 0
        self.executed = 0

    def create_runtime_session_for_account(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.created += 1
        raise AssertionError("Chrome must not launch for invalid queue/claim jobs")

    def execute_plan(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.executed += 1
        raise AssertionError("Adapter must not execute for invalid queue/claim jobs")


class SuccessAdapter(FakeAdapter):
    def create_runtime_session_for_account(self, account_id: str, owner_token: str, worker_round_id: str, policy: dict[str, Any]) -> dict[str, Any]:
        self.created += 1
        return {"page": object(), "context": None, "profile_path": str(policy["profile_path"]), "browser_path": "fake-chrome"}

    def close_runtime_session(self, session: Any) -> dict[str, Any]:
        return {"ok": True, "closed": True}


def _service(path: Path, adapter: FakeAdapter | None = None) -> tuple[CommercialQueueService, FakeAdapter]:
    adapter = adapter or FakeAdapter()
    service = CommercialQueueService(
        repository=CommercialQueueRepository(path),
        orchestrator=lambda **payload: {"success": True, "forward_verified": True, "diagnostics_consistent": True, "confirm_click_count": 1, "verified_forwarded_recipient_count": 1},
        account_auth_checker=lambda account_id: True,
        sleeper=lambda seconds: None,
    )
    service.platform_adapters["bale"] = adapter
    service.runtime_session_manager = AccountRuntimeSessionManager({"bale": adapter})
    service.runtime_session_manager.identity_resolver = service.browser_identity_resolver
    profile = path.parent / "profiles" / ACCOUNT_ID
    profile.mkdir(parents=True, exist_ok=True)
    service.browser_identity_resolver.verify_launch_allowed = lambda account_id, worker_round_id: {
        "identity_id": "identity_guard",
        "profile_path": str(profile),
        "normalized_profile_path": str(profile).casefold(),
    }
    service.update_global_settings({
        "default_source_channel_uid": SOURCE_UID,
        "deliveries_per_account_round": 2,
        "default_daily_limit_per_account": 10,
        "delay_between_deliveries_seconds": 0,
        "round_cooldown_seconds": 0,
        "session_reuse_enabled": False,
    })
    service.update_account_settings(ACCOUNT_ID, {"enabled": True, "daily_limit_override": 10, "deliveries_per_round_override": 2, "source_channel_uid_override": SOURCE_UID})
    return service, adapter


def _campaign(service: CommercialQueueService) -> dict[str, Any]:
    return service.create_campaign({"name": "Queue Claim Guard", "platform": "bale", "status": "running", "source_channel_uid": SOURCE_UID})


def _raw_job(service: CommercialQueueService, campaign: dict[str, Any], phone: str = "989300000001") -> tuple[dict[str, Any], dict[str, Any]]:
    recipient, job = service.repository.create_recipient_and_job(campaign, phone, phone, "Bale-X", "test")
    return recipient, service.repository.get_job(job["id"]) or job


def _force_queued(service: CommercialQueueService, job_id: str) -> None:
    with service.repository.connection() as connection:
        connection.execute(
            "UPDATE commercial_delivery_jobs SET status='queued', last_error_code=NULL, manual_review_required=0, safe_to_requeue=NULL, should_not_retry=0, live_execution_blocked=0, block_reason=NULL WHERE id=?",
            (job_id,),
        )
        connection.commit()


def _confirmed_manifest(service: CommercialQueueService, campaign_id: str, phone: str) -> dict[str, Any]:
    return service.repository.create_recipient_input_manifest(
        campaign_id=campaign_id,
        phones=[phone],
        batch_id=None,
        submitted_by="test",
        source_type="test_manifest",
        confirmation_status="confirmed",
        confirmed_by="test",
    )


def _authorize_valid(service: CommercialQueueService, recipient: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    manifest = _confirmed_manifest(service, recipient["campaign_id"], recipient["phone_normalized"])
    updates = {
        "recipient_origin": "user_provided",
        "synthetic_test_data": False,
        "live_execution_authorized": True,
        "authorization_status": "authorized",
        "should_not_retry": False,
        "live_execution_blocked": False,
        "input_manifest_id": manifest["manifest_id"],
        "input_manifest_hash": manifest["manifest_hash"],
        "input_sequence": 1,
        "input_provenance_status": "confirmed_manifest",
    }
    service.repository.update_recipient_authorization(recipient["id"], updates)
    service.repository.update_job_authorization_metadata(job["id"], updates)
    return service.repository.get_job(job["id"]) or job


def _assert_not_claimed(service: CommercialQueueService, adapter: FakeAdapter, campaign: dict[str, Any], job_id: str) -> None:
    result = service.run_account_round(ACCOUNT_ID, campaign["id"], max_jobs=1, dry_run=False)
    stored = service.repository.get_job(job_id)
    assert result["processed_count"] == 0
    assert result["assigned_count"] == 0
    assert stored["status"] == "skipped"
    assert stored["manual_review_required"] == 1
    assert stored["safe_to_requeue"] == 0
    assert adapter.created == 0
    assert adapter.executed == 0


def test_missing_manifest_queued_job_not_claimed_or_executed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, adapter = _service(Path(tmp) / "guard.db")
        campaign = _campaign(service)
        _, job = _raw_job(service, campaign)
        _force_queued(service, job["id"])
        _assert_not_claimed(service, adapter, campaign, job["id"])


def test_draft_manifest_queued_job_not_claimed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, adapter = _service(Path(tmp) / "guard.db")
        campaign = _campaign(service)
        recipient, job = _raw_job(service, campaign)
        manifest = service.repository.create_recipient_input_manifest(campaign["id"], [recipient["phone_normalized"]], None, "test", "test", confirmation_status="draft")
        service.repository.update_recipient_authorization(recipient["id"], {"input_manifest_id": manifest["manifest_id"], "input_manifest_hash": manifest["manifest_hash"], "input_provenance_status": "confirmed_manifest"})
        _force_queued(service, job["id"])
        _assert_not_claimed(service, adapter, campaign, job["id"])


def test_unknown_provenance_queued_job_not_claimed_and_manual_review() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, adapter = _service(Path(tmp) / "guard.db")
        campaign = _campaign(service)
        _, job = _raw_job(service, campaign)
        service.repository.update_job_authorization_metadata(job["id"], {"input_provenance_status": "unknown"})
        _force_queued(service, job["id"])
        _assert_not_claimed(service, adapter, campaign, job["id"])


def test_unauthorized_blocked_should_not_retry_and_synthetic_cannot_claim() -> None:
    cases = [
        {"live_execution_authorized": False, "authorization_status": "authorized"},
        {"live_execution_blocked": True},
        {"should_not_retry": True},
        {"synthetic_test_data": True},
    ]
    for updates in cases:
        with tempfile.TemporaryDirectory() as tmp:
            service, adapter = _service(Path(tmp) / "guard.db")
            campaign = _campaign(service)
            recipient, job = _raw_job(service, campaign)
            job = _authorize_valid(service, recipient, job)
            service.repository.update_recipient_authorization(recipient["id"], updates)
            service.repository.update_job_authorization_metadata(job["id"], updates)
            _force_queued(service, job["id"])
            _assert_not_claimed(service, adapter, campaign, job["id"])


def test_valid_confirmed_manifest_job_is_claimable_in_mock() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, adapter = _service(Path(tmp) / "guard.db")
        campaign = _campaign(service)
        recipient, job = _raw_job(service, campaign)
        job = _authorize_valid(service, recipient, job)
        assigned = service.assign_jobs(ACCOUNT_ID, campaign["id"], limit=1)
        stored = service.repository.get_job(job["id"])
        assert assigned["assigned_count"] == 1
        assert assigned["assigned_job_ids"] == [job["id"]]
        assert stored["status"] == "assigned"
        assert adapter.created == 0
        assert adapter.executed == 0


def test_legacy_invalid_queued_job_quarantined_without_retry_loop() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, adapter = _service(Path(tmp) / "guard.db")
        campaign = _campaign(service)
        _, job = _raw_job(service, campaign)
        _force_queued(service, job["id"])
        assigned = service.assign_jobs(ACCOUNT_ID, campaign["id"], limit=1)
        stored = service.repository.get_job(job["id"])
        assert assigned["assigned_count"] == 0
        assert assigned["blocked_ineligible_queued_count"] == 1
        assert stored["status"] == "skipped"
        assert stored["should_not_retry"] == 1
        assert adapter.created == 0


def test_toctou_invalidated_after_assignment_rejected_before_adapter() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, adapter = _service(Path(tmp) / "guard.db")
        campaign = _campaign(service)
        recipient, job = _raw_job(service, campaign)
        job = _authorize_valid(service, recipient, job)
        assigned = service.assign_jobs(ACCOUNT_ID, campaign["id"], limit=1)
        assert assigned["assigned_count"] == 1
        service.repository.update_recipient_authorization(recipient["id"], {"live_execution_blocked": True})
        stored = service.repository.get_job(job["id"])
        details = service.repository.get_job_with_recipient(job["id"])
        check = service.validate_live_recipient_authorization(stored, details, dry_run=False)
        assert check["ok"] is False
        assert check["error_code"] == "recipient_live_execution_blocked"
        assert adapter.created == 0
        assert adapter.executed == 0


if __name__ == "__main__":
    test_missing_manifest_queued_job_not_claimed_or_executed()
    test_draft_manifest_queued_job_not_claimed()
    test_unknown_provenance_queued_job_not_claimed_and_manual_review()
    test_unauthorized_blocked_should_not_retry_and_synthetic_cannot_claim()
    test_valid_confirmed_manifest_job_is_claimable_in_mock()
    test_legacy_invalid_queued_job_quarantined_without_retry_loop()
    test_toctou_invalidated_after_assignment_rejected_before_adapter()
    print("Commercial queue claim provenance guard tests passed")
