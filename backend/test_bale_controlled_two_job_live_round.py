from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from modules.automation_engine.commercial_queue.repository import CommercialQueueRepository
from modules.automation_engine.commercial_queue.service import CommercialQueueService
from modules.automation_engine.runtime_sessions.manager import AccountRuntimeSessionManager


ACCOUNT_ID = "bale_09211690533"
SOURCE_UID = "5613544284"


class FakePage:
    def __init__(self) -> None:
        self.closed = False
        self.session_state: dict[str, Any] = {}
        self.evaluations: list[str] = []

    def is_closed(self) -> bool:
        return self.closed

    def title(self) -> str:
        if self.closed:
            raise RuntimeError("closed")
        return "Bale"

    def evaluate(self, script: str) -> str:
        self.evaluations.append(script)
        return "complete"


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.closed = False

    def close(self) -> None:
        self.closed = True
        self.page.closed = True


class FakeAdapter:
    def __init__(self, reset_ok: bool = True) -> None:
        self.create_calls: list[dict[str, Any]] = []
        self.close_calls: list[str] = []
        self.reset_calls: list[str] = []
        self.reset_ok = reset_ok
        self.page = FakePage()

    def create_runtime_session_for_account(self, account_id: str, owner_token: str, worker_round_id: str, policy: dict[str, Any]) -> dict[str, Any]:
        self.create_calls.append({"account_id": account_id, "profile_path": policy.get("profile_path")})
        return {"page": self.page, "context": FakeContext(self.page), "profile_path": str(policy["profile_path"]), "browser_path": "fake-chrome"}

    def validate_execution_plan(self, plan: Any) -> dict[str, Any]:
        return {"validation_errors": []}

    def execute_plan(self, plan: Any, runtime_session: Any | None = None) -> dict[str, Any]:
        return {"success": False, "error_code": "unexpected_adapter_call"}

    def reset_session_after_job(self, session: Any, plan: Any, result: dict[str, Any]) -> dict[str, Any]:
        self.reset_calls.append(plan.job_id)
        if not self.reset_ok:
            return {"ok": False, "error_code": "session_reset_failed", "message": "forced reset failure"}
        return {"ok": True}

    def close_runtime_session(self, session: Any) -> dict[str, Any]:
        self.close_calls.append(session.session_id)
        if session.context:
            session.context.close()
        return {"ok": True, "closed": True}


class ForwardRecorder:
    def __init__(self, results: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.results = list(results or [])

    def __call__(self, **payload: Any) -> dict[str, Any]:
        self.calls.append(dict(payload))
        if self.results:
            return dict(self.results.pop(0))
        return {
            "success": True,
            "forward_verified": True,
            "diagnostics_consistent": True,
            "confirm_click_count": 1,
            "verified_forwarded_recipient_count": 1,
            "selected_names_before_confirm": [payload["display_name"]],
            "visible_exact_result_count": 1,
            "exact_match_count": 1,
            "recipient_click_count": 1,
            "selected_recipient_count": 1,
            "source_channel_uid": SOURCE_UID,
        }


def _service(path: Path, recorder: ForwardRecorder | None = None, adapter: FakeAdapter | None = None) -> tuple[CommercialQueueService, ForwardRecorder, FakeAdapter]:
    recorder = recorder or ForwardRecorder()
    adapter = adapter or FakeAdapter()
    service = CommercialQueueService(repository=CommercialQueueRepository(path), orchestrator=recorder, account_auth_checker=lambda account_id: True, sleeper=lambda seconds: None)
    service.platform_adapters["bale"] = adapter
    service.runtime_session_manager = AccountRuntimeSessionManager({"bale": adapter})
    service.runtime_session_manager.identity_resolver = service.browser_identity_resolver
    profile = path.parent / "browser_profiles" / ACCOUNT_ID
    profile.mkdir(parents=True, exist_ok=True)
    service.browser_identity_resolver.verify_launch_allowed = lambda account_id, worker_round_id: {
        "identity_id": "identity_test",
        "profile_path": str(profile),
        "normalized_profile_path": str(profile).casefold(),
    }
    service.update_global_settings({
        "default_source_channel_uid": SOURCE_UID,
        "deliveries_per_account_round": 2,
        "default_daily_limit_per_account": 10,
        "delay_between_deliveries_seconds": 0,
        "round_cooldown_seconds": 0,
        "session_reuse_enabled": True,
        "resource_guard_enabled": True,
        "automatic_retry_enabled": False,
    })
    service.update_account_settings(ACCOUNT_ID, {"enabled": True, "source_channel_uid_override": SOURCE_UID, "deliveries_per_round_override": 2, "daily_limit_override": 10, "round_cooldown_override": 0, "delay_between_deliveries_override": 0})
    return service, recorder, adapter


def _controlled_campaign(service: CommercialQueueService) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    campaign = service.create_campaign({
        "name": "Controlled Live Forward Verification 2",
        "platform": "bale",
        "status": "running",
        "source_channel_uid": SOURCE_UID,
        # Two recipient jobs are intentionally exercised through one
        # account/session; this fixture has no requested account demand.
        "capacity_reservation": 0,
        "policy_overrides": {
            "session_reuse_enabled": True,
            "resource_guard_enabled": True,
            "deliveries_per_account_round": 2,
            "automatic_retry_enabled": False,
            "round_cooldown_seconds": 0,
            "delay_between_deliveries_seconds": 0,
        },
    })
    specs = [("09050454491", "989050454491", "Bale-000008"), ("09377686492", "989377686492", "Bale-000009")]
    manifest = service.repository.create_recipient_input_manifest(
        campaign_id=campaign["id"],
        phones=[phone for _, phone, _ in specs],
        batch_id=None,
        submitted_by="test",
        source_type="test_manifest",
        confirmation_status="confirmed",
        confirmed_by="test",
    )
    recipients: list[dict[str, Any]] = []
    jobs: list[dict[str, Any]] = []
    for index, (raw, phone, name) in enumerate(specs, start=1):
        recipient, job = service.repository.create_recipient_and_job(campaign, raw, phone, name, "controlled_live_phase5f2")
        updates = {
            "recipient_origin": "user_provided",
            "synthetic_test_data": False,
            "live_execution_authorized": True,
            "live_authorized_at": "2026-07-13T00:00:00+00:00",
            "live_authorized_by": "user",
            "authorization_source": "explicit_user_confirmation",
            "authorization_note": "controlled Bale live verification",
            "authorization_status": "authorized",
            "should_not_retry": False,
            "stable_display_name": name,
            "bale_contact_verified": True,
            "input_manifest_id": manifest["manifest_id"],
            "input_manifest_hash": manifest["manifest_hash"],
            "input_sequence": index,
            "input_provenance_status": "confirmed_manifest",
        }
        recipients.append(service.repository.update_recipient_authorization(recipient["id"], updates) or recipient)
        service.repository.update_jobs_authorization_by_recipient(recipient["id"], updates)
        jobs.append(service.repository.get_job(job["id"]) or job)
    return campaign, recipients, jobs


def test_exact_two_job_campaign_scope_and_historical_excluded() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, _, _ = _service(Path(tmp) / "live.db")
        historical_campaign = service.create_campaign({"name": "Historical", "platform": "bale", "status": "completed", "source_channel_uid": SOURCE_UID})
        _, historical_job = service.repository.create_recipient_and_job(historical_campaign, "989304073331", "989304073331", "Bale-000001", "historical")
        service.repository.complete_job(historical_job["id"], "succeeded", {"result_success": True, "verified_forwarded_recipient_count": 1, "forward_verified": True, "diagnostics_consistent": True})
        campaign, recipients, jobs = _controlled_campaign(service)
        assert [item["phone_normalized"] for item in recipients] == ["989050454491", "989377686492"]
        assert len(jobs) == 2
        assert all(job["campaign_id"] == campaign["id"] for job in jobs)
        assert all(job["phone_normalized"] != "989304073331" for job in jobs)
        assert service.repository.get_job(historical_job["id"])["attempt_count"] == 0


def test_one_browser_session_two_jobs_second_reuses_session_counters_increment() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, recorder, adapter = _service(Path(tmp) / "live.db")
        campaign, _, _ = _controlled_campaign(service)
        before = service.resolve_account_settings(ACCOUNT_ID)
        result = service.run_account_round(ACCOUNT_ID, campaign["id"], max_jobs=2)
        after = service.resolve_account_settings(ACCOUNT_ID)
        jobs = service.repository.list_campaign_jobs_all(campaign["id"])
    assert result["processed_count"] == 2
    assert result["round_diagnostics"]["browser_start_count"] == 1
    assert result["round_diagnostics"]["browser_close_count"] == 1
    assert result["round_diagnostics"]["session_reused_job_count"] == 1
    assert result["results"][0]["session_reused"] is False
    assert result["results"][1]["session_reused"] is True
    assert len({item["session_id"] for item in result["results"]}) == 1
    assert [call["display_name"] for call in recorder.calls] == ["Bale-000008", "Bale-000009"]
    assert len(adapter.create_calls) == 1
    assert len(adapter.close_calls) == 1
    assert after["current_daily_sent_count"] - before["current_daily_sent_count"] == 2
    assert after["current_round_sent_count"] == 2
    assert all(job["status"] == "succeeded" and job["attempt_count"] == 1 for job in jobs)


def test_reset_failure_blocks_second_job_without_retry() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, recorder, _ = _service(Path(tmp) / "live.db", adapter=FakeAdapter(reset_ok=False))
        campaign, _, _ = _controlled_campaign(service)
        result = service.run_account_round(ACCOUNT_ID, campaign["id"], max_jobs=2)
        jobs = service.repository.list_campaign_jobs_all(campaign["id"])
    assert result["processed_count"] == 1
    assert result["stopped_early"] is True
    assert len(recorder.calls) == 1
    assert jobs[0]["status"] == "succeeded"
    assert jobs[0]["attempt_count"] == 1
    assert jobs[1]["status"] == "failed"
    assert jobs[1]["attempt_count"] == 0
    assert jobs[1]["last_error_code"] == "blocked_by_session_reset_failed"


def test_confirm_uncertainty_stops_round_no_retry_no_counter_increment_for_failure() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        recorder = ForwardRecorder([{"success": False, "forward_verified": False, "diagnostics_consistent": False, "error_code": "forward_confirm_failed", "confirm_click_count": 0, "verified_forwarded_recipient_count": 0}])
        service, recorder, _ = _service(Path(tmp) / "live.db", recorder=recorder)
        campaign, _, _ = _controlled_campaign(service)
        before = service.resolve_account_settings(ACCOUNT_ID)
        result = service.run_account_round(ACCOUNT_ID, campaign["id"], max_jobs=2)
        after = service.resolve_account_settings(ACCOUNT_ID)
        jobs = service.repository.list_campaign_jobs_all(campaign["id"])
    assert result["processed_count"] == 1
    assert result["stopped_early"] is True
    assert after["current_daily_sent_count"] == before["current_daily_sent_count"]
    assert jobs[0]["status"] == "failed"
    assert jobs[0]["attempt_count"] == 1
    assert jobs[1]["status"] == "queued"
    assert len(recorder.calls) == 1


def test_exact_recipient_selection_mismatch_stops_before_second_job() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        recorder = ForwardRecorder([{
            "success": False,
            "forward_verified": False,
            "diagnostics_consistent": True,
            "error_code": "unexpected_selected_recipient",
            "error_message": "Selected recipient did not resolve to exactly the requested target",
            "failed_step": "select_recipient",
            "confirm_click_count": 0,
            "verified_forwarded_recipient_count": 0,
        }])
        service, recorder, _ = _service(Path(tmp) / "live.db", recorder=recorder)
        campaign, _, _ = _controlled_campaign(service)
        result = service.run_account_round(ACCOUNT_ID, campaign["id"], max_jobs=2)
        jobs = service.repository.list_campaign_jobs_all(campaign["id"])
    assert result["processed_count"] == 1
    assert result["stopped_early"] is True
    assert len(recorder.calls) == 1
    assert jobs[0]["status"] == "failed"
    assert jobs[0]["attempt_count"] == 1
    assert jobs[0]["last_error_code"] == "unexpected_selected_recipient"
    assert jobs[1]["status"] == "queued"
    assert jobs[1]["attempt_count"] == 0


def test_synthetic_or_unverified_recipient_rejected_before_adapter() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, recorder, _ = _service(Path(tmp) / "live.db")
        campaign, recipients, _ = _controlled_campaign(service)
        service.repository.update_recipient_authorization(recipients[0]["id"], {"synthetic_test_data": True, "live_execution_authorized": False, "authorization_status": "dry_run_only"})
        service.repository.update_jobs_authorization_by_recipient(recipients[0]["id"], {"synthetic_test_data": True, "live_execution_authorized": False, "authorization_status": "dry_run_only"})
        result = service.run_account_round(ACCOUNT_ID, campaign["id"], max_jobs=2)
    assert result["processed_count"] >= 1
    assert recorder.calls and recorder.calls[0]["display_name"] == "Bale-000009"


if __name__ == "__main__":
    test_exact_two_job_campaign_scope_and_historical_excluded()
    test_one_browser_session_two_jobs_second_reuses_session_counters_increment()
    test_reset_failure_blocks_second_job_without_retry()
    test_confirm_uncertainty_stops_round_no_retry_no_counter_increment_for_failure()
    test_exact_recipient_selection_mismatch_stops_before_second_job()
    test_synthetic_or_unverified_recipient_rejected_before_adapter()
    print("Bale controlled two-job live round tests passed")
