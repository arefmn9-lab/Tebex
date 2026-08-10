from __future__ import annotations

import tempfile
from pathlib import Path

from app.main import app
from app.routes import automation as automation_routes
from fastapi.testclient import TestClient
from modules.automation_engine.commercial_queue.errors import classify_error
from modules.automation_engine.commercial_queue.execution_plan import build_execution_plan
from modules.automation_engine.commercial_queue.context import OperationContext
from modules.automation_engine.commercial_queue.operations import operation_registry
from modules.automation_engine.commercial_queue.repository import CommercialQueueRepository
from modules.automation_engine.commercial_queue.resources import ResourceCapacityProvider
from modules.automation_engine.commercial_queue.service import CommercialQueueService


class PlanRecorder:
    def __init__(self, result: dict | None = None) -> None:
        self.calls: list[dict] = []
        self.result = result or {"success": True, "forward_verified": True, "diagnostics_consistent": True}

    def __call__(self, **payload: object) -> dict:
        self.calls.append(dict(payload))
        return {"verified_forwarded_recipient_count": 1, **self.result}


def _service(path: Path, recorder: PlanRecorder | None = None) -> CommercialQueueService:
    service = CommercialQueueService(
        repository=CommercialQueueRepository(path),
        orchestrator=recorder or PlanRecorder(),
        account_auth_checker=lambda account_id: True,
        sleeper=lambda seconds: None,
    )
    service.update_global_settings(
        {
            "default_source_channel_uid": "global_channel",
            "deliveries_per_account_round": 5,
            "default_daily_limit_per_account": 50,
            "delay_between_deliveries_seconds": 0,
            "round_cooldown_seconds": 0,
            "max_concurrent_accounts": 5,
        }
    )
    return service


def _campaign(service: CommercialQueueService, **overrides: object) -> dict:
    return service.create_campaign({"name": "Architecture", "platform": "bale", "status": "running", **overrides})


def _job(service: CommercialQueueService, campaign_id: str) -> dict:
    service.repository.upsert_campaign_capacity_reservation(campaign_id, 1, 1)
    result = service.import_recipients(campaign_id, ["09304073331"])
    recipient = result["created_recipients"][0]
    job = result["created_jobs"][0]
    authorization = {
        "recipient_origin": "user_import",
        "live_execution_authorized": True,
        "live_authorized_by": "architecture_fixture",
        "live_authorized_at": "2026-07-14T00:00:00+00:00",
        "authorization_source": "architecture_fixture",
        "authorization_status": "authorized",
        "authorization_note": "Temp-DB architecture fixture only; no adapter execution.",
        "should_not_retry": False,
        "synthetic_test_data": False,
        "live_execution_blocked": False,
        "block_reason": None,
    }
    service.repository.update_recipient_authorization(recipient["id"], authorization)
    service.repository.update_job_authorization_metadata(job["id"], authorization)
    return service.list_jobs(campaign_id=campaign_id, limit=1)["items"][0]


def test_policy_resolution_precedence_and_null_fallbacks() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "arch.db")
        campaign = _campaign(service, policy_overrides={"source_channel_uid": "campaign_channel", "deliveries_per_round": 7, "delay_between_deliveries_seconds": 0})
        service.update_account_settings("acct_a", {"source_channel_uid_override": None, "deliveries_per_round_override": 9, "daily_limit_override": 20})
        resolved = service.resolve_effective_policy("acct_a", campaign["id"])

    policy = resolved["effective_policy"]
    assert policy["source_channel_uid"] == "campaign_channel"
    assert policy["deliveries_per_round"] == 9
    assert policy["daily_limit_per_account"] == 20
    assert policy["delay_between_deliveries_seconds"] == 0
    assert resolved["policy_resolution_source"]["source_channel_uid"] == "campaign"
    assert resolved["policy_resolution_source"]["deliveries_per_round"] == "account"


def test_policy_values_support_round_sizes_concurrency_and_distinct_scopes() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "arch.db")
        for value in [1, 5, 10, 20]:
            service.update_global_settings({"max_concurrent_accounts": value, "deliveries_per_account_round": value})
            resolved = service.resolve_effective_policy()
            assert resolved["effective_policy"]["max_concurrent_accounts"] == value
            assert resolved["effective_policy"]["deliveries_per_round"] == value
        first = _campaign(service, policy_overrides={"source_channel_uid": "one"})
        second = _campaign(service, policy_overrides={"source_channel_uid": "two"})
        service.update_account_settings("acct_a", {"daily_limit_override": 3})
        service.update_account_settings("acct_b", {"daily_limit_override": 8})
        assert service.resolve_effective_policy(campaign_id=first["id"])["effective_policy"]["source_channel_uid"] == "one"
        assert service.resolve_effective_policy(campaign_id=second["id"])["effective_policy"]["source_channel_uid"] == "two"
        assert service.resolve_effective_policy("acct_a")["effective_policy"]["daily_limit_per_account"] == 3
        assert service.resolve_effective_policy("acct_b")["effective_policy"]["daily_limit_per_account"] == 8


def test_operation_registry_validation() -> None:
    assert operation_registry.validate(["save_contact", "bad"]).validation_errors == ["unsupported_operation:bad", "missing_required_operation:forward_message"]
    assert "duplicate_operation:save_contact" in operation_registry.validate(["save_contact", "save_contact", "forward_message"]).validation_errors
    assert "unsafe_operation_order" in operation_registry.validate(["forward_message", "save_contact"]).validation_errors
    valid = operation_registry.validate(["save_contact", "forward_message"])
    assert valid.valid is True
    assert [step["operation"] for step in valid.normalized_execution_steps] == ["save_contact", "forward_message"]


def test_execution_plan_contains_all_correlation_ids() -> None:
    context = OperationContext.create(scheduler_tick_id="tick_1", worker_round_id="round_1", account_id="acct", campaign_id="campaign", job_id="job", recipient_id="recipient")
    job = {"id": "job", "campaign_id": "campaign", "recipient_id": "recipient", "phone_normalized": "9893", "source_channel_uid": None}
    policy = {"effective_policy": _service(Path(tempfile.mkdtemp()) / "tmp.db").resolve_effective_policy()["effective_policy"], "policy_resolution_source": {}}
    plan = build_execution_plan(context=context, job=job, job_details={"recipient_phone_normalized": "9893"}, policy_result=policy)
    data = plan.to_dict()
    for key in ["correlation_id", "scheduler_tick_id", "worker_round_id", "job_id", "campaign_id", "recipient_id", "account_id"]:
        assert data[key]


def test_scheduler_and_worker_use_resolved_policy_and_pass_plan() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        recorder = PlanRecorder()
        service = _service(Path(tmp_dir) / "arch.db", recorder)
        campaign = _campaign(service, policy_overrides={"source_channel_uid": "campaign_channel", "deliveries_per_round": 1})
        _job(service, campaign["id"])
        service.update_account_settings("acct_a", {"source_channel_uid_override": "account_channel", "deliveries_per_round_override": 1, "round_cooldown_override": 0})
        service.scheduler_start()
        result = service.scheduler_run_once(campaign["id"])

    assert result["started_accounts"] == ["acct_a"]
    call = recorder.calls[0]
    assert call["source_channel_uid"] == "account_channel"
    assert call["execution_plan"]["source_channel_uid"] == "account_channel"
    assert call["effective_policy"]["deliveries_per_round"] == 1


def test_bale_actions_do_not_read_commercial_policy_directly() -> None:
    plugin_text = Path("modules/automation_engine/plugins/bale/plugin.py").read_text(encoding="utf-8")
    assert "commercial_global_settings" not in plugin_text
    assert "get_global_settings" not in plugin_text


def test_structured_error_classification() -> None:
    confirm = classify_error({"error_code": "forward_confirm_failed", "error_message": "uncertain"}).to_dict()
    auth = classify_error({"error_code": "not_logged_in"}).to_dict()
    recipient = classify_error({"error_code": "recipient_not_found"}).to_dict()
    assert confirm["manual_review_required"] is True
    assert auth["account_blocking"] is True
    assert recipient["severity"] == "job_failure"
    assert recipient["account_blocking"] is False


def test_resource_capacity_blocks_new_work_only_and_leaves_jobs_queued() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "arch.db")
        campaign = _campaign(service)
        _job(service, campaign["id"])
        service.update_account_settings("acct_a", {"source_channel_uid_override": "global_channel"})
        service.update_global_settings({"max_concurrent_accounts": 0})
        service.scheduler_start()
        result = service.scheduler_run_once(campaign["id"])
        jobs = service.list_jobs(campaign_id=campaign["id"], limit=10)["items"]
        provider = ResourceCapacityProvider(service.repository, cpu_percent=99, memory_percent=99)
        decision = provider.decide({"max_concurrent_accounts": 1, "browser_start_batch_size": 1, "resource_guard_enabled": True, "max_system_cpu_percent": 90, "max_system_memory_percent": 90})

    assert result["started_accounts"] == []
    assert jobs[0]["status"] == "queued"
    assert decision.allow_new_worker is False
    assert "memory_threshold_reached" in decision.reason_codes


def test_feature_flags_default_safely_and_live_execution_cannot_be_bypassed() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "arch.db")
        flags = service.architecture_capabilities()["feature_flags"]
    assert flags["session_reuse_enabled"] is False
    assert flags["resource_guard_enabled"] is False
    assert flags["automatic_retry_enabled"] is False
    assert flags["live_campaign_execution_enabled"] is False


def test_architecture_endpoints_and_reload_persistence() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        previous_service = automation_routes.commercial_queue_service
        db_path = Path(tmp_dir) / "arch.db"
        service = _service(db_path)
        campaign = _campaign(service, policy_overrides={"source_channel_uid": "persisted"})
        automation_routes.commercial_queue_service = service
        try:
            client = TestClient(app)
            capabilities = client.get("/automation/architecture/capabilities")
            effective = client.get(f"/automation/policy/effective?campaign_id={campaign['id']}")
            resources = client.get("/automation/resources/status")
        finally:
            automation_routes.commercial_queue_service = previous_service
        reloaded = _service(db_path)
        reloaded_campaign = reloaded.get_campaign(campaign["id"])

    assert capabilities.status_code == 200
    assert "bale" in capabilities.json()["supported_platforms"]
    assert effective.json()["effective_policy"]["source_channel_uid"] == "persisted"
    assert resources.status_code == 200
    assert reloaded_campaign is not None
    assert "persisted" in str(reloaded_campaign.get("policy_overrides_json"))


if __name__ == "__main__":
    test_policy_resolution_precedence_and_null_fallbacks()
    test_policy_values_support_round_sizes_concurrency_and_distinct_scopes()
    test_operation_registry_validation()
    test_execution_plan_contains_all_correlation_ids()
    test_scheduler_and_worker_use_resolved_policy_and_pass_plan()
    test_bale_actions_do_not_read_commercial_policy_directly()
    test_structured_error_classification()
    test_resource_capacity_blocks_new_work_only_and_leaves_jobs_queued()
    test_feature_flags_default_safely_and_live_execution_cannot_be_bypassed()
    test_architecture_endpoints_and_reload_persistence()
    print("Commercial architecture tests passed")
