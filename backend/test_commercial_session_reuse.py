from __future__ import annotations

import tempfile
from pathlib import Path

from app.main import app
from app.routes import automation as automation_routes
from fastapi.testclient import TestClient
from modules.automation_engine.commercial_queue.repository import CommercialQueueRepository
from modules.automation_engine.commercial_queue.resources import ResourceCapacityProvider
from modules.automation_engine.commercial_queue.service import CommercialQueueService
from modules.automation_engine.runtime_sessions.manager import AccountRuntimeSessionManager, RuntimeSessionError


class FakeKeyboard:
    def __init__(self) -> None:
        self.presses: list[str] = []

    def press(self, key: str) -> None:
        self.presses.append(key)


class FakePage:
    def __init__(self, closed: bool = False) -> None:
        self.closed = closed
        self.keyboard = FakeKeyboard()
        self.session_state: dict[str, object] = {}
        self.evaluations: list[str] = []

    def is_closed(self) -> bool:
        return self.closed

    def title(self) -> str:
        if self.closed:
            raise RuntimeError("closed")
        return "Bale"

    def evaluate(self, script: str) -> object:
        self.evaluations.append(script)
        return "complete"


class FakeContext:
    def __init__(self, page: FakePage | None = None) -> None:
        self.closed = False
        self.page = page or FakePage()

    def close(self) -> None:
        self.closed = True


class FakeAdapter:
    platform_name = "bale"

    def __init__(self, fail_accounts: set[str] | None = None) -> None:
        self.create_calls: list[str] = []
        self.close_calls: list[str] = []
        self.reset_calls: list[str] = []
        self.fail_accounts = fail_accounts or set()
        self.sessions_by_account: dict[str, object] = {}

    def create_runtime_session_for_account(self, account_id: str, owner_token: str, worker_round_id: str, policy: dict) -> dict:
        self.create_calls.append(account_id)
        if account_id in self.fail_accounts:
            raise RuntimeError("forced session failure")
        page = FakePage()
        context = FakeContext(page)
        profile_path = str(policy.get("profile_path") or Path(tempfile.gettempdir()) / "clinicos_test_profiles" / account_id)
        return {"page": page, "context": context, "profile_path": profile_path, "browser": None, "browser_path": "mock"}

    def validate_execution_plan(self, plan: object) -> dict:
        return {"validation_errors": []}

    def create_runtime_session(self, plan: object) -> dict:
        return self.create_runtime_session_for_account(plan.account_id, "", plan.worker_round_id, plan.effective_policy)

    def health_check_session(self, session: object) -> dict:
        return {"ok": True}

    def prepare_session_for_job(self, session: object, plan: object) -> dict:
        return {"ok": True}

    def prepare_recipient(self, session: object, plan: object) -> dict:
        return {"ok": True}

    def deliver(self, session: object, plan: object) -> dict:
        return self.execute_plan(plan, runtime_session=session)

    def execute_plan(self, plan: object, runtime_session: object | None = None) -> dict:
        return {"success": True, "forward_verified": False if plan.dry_run else True, "diagnostics_consistent": True, "confirm_click_count": 0 if plan.dry_run else 1, "verified_forwarded_recipient_count": 0 if plan.dry_run else 1}

    def reset_session_after_job(self, session: object, plan: object, result: dict) -> dict:
        self.reset_calls.append(plan.job_id)
        return {"ok": True}

    def verify_delivery(self, session: object, plan: object, result: dict) -> dict:
        return result

    def close_runtime_session(self, session: object) -> dict:
        self.close_calls.append(session.session_id)
        if session.context:
            session.context.close()
        return {"ok": True}

    def classify_error(self, result: dict) -> dict:
        return {}


class Recorder:
    def __init__(self, results: list[dict] | None = None) -> None:
        self.calls: list[dict] = []
        self.results = list(results or [])

    def __call__(self, **payload: object) -> dict:
        self.calls.append(dict(payload))
        if self.results:
            return dict(self.results.pop(0))
        return {"success": True, "forward_verified": False if payload.get("dry_run") else True, "diagnostics_consistent": True, "confirm_click_count": 0, "verified_forwarded_recipient_count": 0}


def _service(path: Path, recorder: Recorder | None = None, adapter: FakeAdapter | None = None) -> CommercialQueueService:
    service = CommercialQueueService(
        repository=CommercialQueueRepository(path),
        orchestrator=recorder or Recorder(),
        account_auth_checker=lambda account_id: True,
        sleeper=lambda seconds: None,
    )
    fake = adapter or FakeAdapter()
    service.platform_adapters["bale"] = fake
    service.runtime_session_manager = AccountRuntimeSessionManager({"bale": fake})
    service.update_global_settings(
        {
            "default_source_channel_uid": "5613544284",
            "deliveries_per_account_round": 3,
            "default_daily_limit_per_account": 20,
            "delay_between_deliveries_seconds": 0,
            "round_cooldown_seconds": 0,
            "max_concurrent_accounts": 3,
            "session_reuse_enabled": False,
            "browser_start_batch_size": 2,
            "browser_start_stagger_ms": 0,
        }
    )
    return service


def _seed(service: CommercialQueueService, account_id: str = "acct_a", count: int = 3) -> dict:
    service.update_account_settings(account_id, {"enabled": True, "source_channel_uid_override": "5613544284", "deliveries_per_round_override": count, "round_cooldown_override": 0, "delay_between_deliveries_override": 0})
    campaign = service.create_campaign({"name": "Reuse", "platform": "bale", "status": "running", "source_channel_uid": "5613544284"})
    service.import_recipients(campaign["id"], [f"0930407333{index}" for index in range(1, count + 1)])
    for recipient in service.list_recipients(campaign["id"], limit=100)["items"]:
        service.repository.update_recipient_authorization(
            recipient["id"],
            {
                "recipient_origin": "user_provided",
                "synthetic_test_data": False,
                "live_execution_authorized": True,
                "live_authorized_at": "2026-07-12T00:00:00+00:00",
                "live_authorized_by": "test",
                "authorization_source": "test_fixture",
                "authorization_note": "mock recipient",
                "authorization_status": "authorized",
                "should_not_retry": False,
            },
        )
    return campaign


def test_feature_flag_false_preserves_legacy_lifecycle_and_standalone_supported() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        recorder = Recorder()
        adapter = FakeAdapter()
        service = _service(Path(tmp_dir) / "reuse.db", recorder, adapter)
        campaign = _seed(service, count=2)
        result = service.run_account_round("acct_a", campaign["id"], max_jobs=2, dry_run=True)

    assert result["processed_count"] == 2
    assert adapter.create_calls == []
    assert recorder.calls[0]["runtime_session"] is None


def test_session_manager_ownership_idempotent_close_and_isolation() -> None:
    manager = AccountRuntimeSessionManager({"bale": FakeAdapter()})
    policy = {"platform": "bale", "session_reuse_enabled": True}
    first = manager.acquire_or_create_session("acct_a", "token_a", "round_a", policy)
    again = manager.acquire_or_create_session("acct_a", "token_a", "round_a", policy)
    second = manager.acquire_or_create_session("acct_b", "token_b", "round_b", policy)
    assert first is again
    assert first.session_id != second.session_id
    assert first.profile_path != second.profile_path
    for token, round_id, code in [("bad", "round_a", "session_owner_mismatch"), ("token_a", "bad", "session_round_mismatch")]:
        try:
            manager.acquire_or_create_session("acct_a", token, round_id, policy)
        except RuntimeSessionError as exc:
            assert exc.error_code == code
    assert manager.close_session(first)["closed"] is True
    assert manager.close_session(first)["already_closed"] is True


def test_health_prepare_blocks_closed_stale_and_mismatched_state() -> None:
    manager = AccountRuntimeSessionManager({"bale": FakeAdapter()})
    policy = {"platform": "bale", "session_reuse_enabled": True}
    session = manager.acquire_or_create_session("acct_a", "token", "round", policy)
    plan = type("Plan", (), {"account_id": "acct_a", "worker_round_id": "round"})()
    assert manager.prepare_for_job(session, plan)["ok"] is True
    for flag, code in [("stale_modal_state", "stale_modal_state"), ("stale_recipient_selection", "stale_recipient_selection"), ("previous_delivery_state_uncertain", "previous_delivery_state_uncertain")]:
        session.page.session_state[flag] = True
        try:
            manager.prepare_for_job(session, plan)
        except RuntimeSessionError as exc:
            assert exc.error_code == code
        session.page.session_state[flag] = False
    session.page.closed = True
    try:
        manager.health_check(session)
    except RuntimeSessionError as exc:
        assert exc.error_code == "session_page_closed"


def test_worker_round_reuses_one_session_for_three_dry_run_jobs() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        recorder = Recorder()
        adapter = FakeAdapter()
        service = _service(Path(tmp_dir) / "reuse.db", recorder, adapter)
        service.update_global_settings({"session_reuse_enabled": True})
        campaign = _seed(service, count=3)
        result = service.run_account_round("acct_a", campaign["id"], max_jobs=3, dry_run=True)
        settings = service.resolve_account_settings("acct_a")
        active = service.runtime_session_manager.list_active_sessions()

    diag = result["round_diagnostics"]
    assert result["processed_count"] == 3
    assert diag["browser_start_count"] == 1
    assert diag["browser_close_count"] == 1
    assert diag["session_reused_job_count"] == 2
    assert len({item["session_id"] for item in result["results"]}) == 1
    assert result["results"][0]["session_reused"] is False
    assert result["results"][1]["session_reused"] is True
    assert adapter.close_calls and len(adapter.close_calls) == 1
    assert len(adapter.reset_calls) == 3
    assert all(item["result"]["confirm_click_count"] == 0 for item in result["results"])
    assert settings["current_daily_sent_count"] == 0
    assert settings["current_round_sent_count"] == 0
    assert active == []


def test_unsafe_failures_invalidate_stop_and_requeue() -> None:
    for payload in [
        {"success": False, "forward_verified": False, "diagnostics_consistent": True, "error_code": "forward_confirm_failed"},
        {"success": False, "forward_verified": False, "diagnostics_consistent": False, "error_code": "diagnostics_inconsistent"},
        {"success": False, "forward_verified": False, "diagnostics_consistent": True, "error_code": "selector_regression"},
    ]:
        with tempfile.TemporaryDirectory() as tmp_dir:
            recorder = Recorder([payload])
            service = _service(Path(tmp_dir) / "reuse.db", recorder)
            service.update_global_settings({"session_reuse_enabled": True})
            campaign = _seed(service, count=3)
            result = service.run_account_round("acct_a", campaign["id"], max_jobs=3, dry_run=False)
            queued = service.list_jobs(campaign_id=campaign["id"], status="queued", limit=10)["items"]
        assert result["stopped_early"] is True
        assert result["processed_count"] == 1
        assert result["round_diagnostics"]["session_invalidated"] is True
        assert len(queued) == 2


def test_ordinary_recipient_failure_can_continue_when_safe() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        recorder = Recorder([
            {"success": False, "forward_verified": False, "diagnostics_consistent": True, "error_code": "recipient_not_found"},
            {"success": True, "forward_verified": True, "diagnostics_consistent": True, "confirm_click_count": 1, "verified_forwarded_recipient_count": 1},
        ])
        service = _service(Path(tmp_dir) / "reuse.db", recorder)
        service.update_global_settings({"session_reuse_enabled": True})
        campaign = _seed(service, count=2)
        result = service.run_account_round("acct_a", campaign["id"], max_jobs=2, dry_run=False)
    assert result["processed_count"] == 2
    assert result["stopped_early"] is False


def test_capacity_denial_prevents_browser_creation_and_leaves_unclaimed() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        adapter = FakeAdapter()
        service = _service(Path(tmp_dir) / "reuse.db", adapter=adapter)
        service.update_global_settings({"session_reuse_enabled": True, "resource_guard_enabled": True, "max_system_cpu_percent": 50})
        service.resource_provider = ResourceCapacityProvider(service.repository, cpu_percent=99)
        campaign = _seed(service, count=1)
        result = service.run_account_round("acct_a", campaign["id"], max_jobs=1, dry_run=True)
        jobs = service.list_jobs(campaign_id=campaign["id"], limit=10)["items"]
    assert adapter.create_calls == []
    assert result["processed_count"] == 0
    assert jobs[0]["status"] == "queued"


def test_scheduler_batching_zero_stagger_20_account_isolation_and_failure_isolation() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        adapter = FakeAdapter(fail_accounts={"acct_00"})
        service = _service(Path(tmp_dir) / "reuse.db", adapter=adapter)
        sleeps: list[float] = []
        service.sleeper = lambda seconds: sleeps.append(seconds)
        service.update_global_settings({"session_reuse_enabled": True, "max_concurrent_accounts": 20, "browser_start_batch_size": 5, "browser_start_stagger_ms": 0, "deliveries_per_account_round": 1})
        campaign = service.create_campaign({"name": "Many", "platform": "bale", "status": "running", "source_channel_uid": "5613544284"})
        for index in range(20):
            account_id = f"acct_{index:02d}"
            service.update_account_settings(account_id, {"enabled": True, "source_channel_uid_override": "5613544284", "deliveries_per_round_override": 1, "round_cooldown_override": 0})
            service.import_recipients(campaign["id"], [f"09304073{index:03d}"])
        service.scheduler_start()
        result = service.scheduler_run_once(campaign["id"], dry_run=True)
    assert sleeps == []
    assert len(result["started_accounts"]) == 20
    assert len(set(adapter.close_calls)) == 19
    by_account = {item["account_id"]: item for item in result["results"]}
    assert by_account["acct_00"]["processed_count"] == 0
    assert sum(1 for item in result["results"] if item["processed_count"] == 1) == 19


def test_runtime_session_apis_expose_safe_diagnostics_only() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        previous = automation_routes.commercial_queue_service
        service = _service(Path(tmp_dir) / "reuse.db")
        service.update_global_settings({"session_reuse_enabled": True})
        session = service.runtime_session_manager.acquire_or_create_session("acct_a", "token", "round", service.resolve_effective_policy("acct_a")["effective_policy"])
        session.metadata["token"] = "secret"
        automation_routes.commercial_queue_service = service
        try:
            client = TestClient(app)
            listing = client.get("/automation/runtime-sessions").json()
            detail = client.get("/automation/runtime-sessions/acct_a").json()
            close = client.post("/automation/runtime-sessions/acct_a/close").json()
        finally:
            automation_routes.commercial_queue_service = previous
    assert listing["items"][0]["session_id"] == session.session_id
    assert "token" not in detail["metadata"]
    assert close["ok"] is True


if __name__ == "__main__":
    test_feature_flag_false_preserves_legacy_lifecycle_and_standalone_supported()
    test_session_manager_ownership_idempotent_close_and_isolation()
    test_health_prepare_blocks_closed_stale_and_mismatched_state()
    test_worker_round_reuses_one_session_for_three_dry_run_jobs()
    test_unsafe_failures_invalidate_stop_and_requeue()
    test_ordinary_recipient_failure_can_continue_when_safe()
    test_capacity_denial_prevents_browser_creation_and_leaves_unclaimed()
    test_scheduler_batching_zero_stagger_20_account_isolation_and_failure_isolation()
    test_runtime_session_apis_expose_safe_diagnostics_only()
    print("Commercial session reuse tests passed")
