from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path

from app.main import app
from app.routes import automation as automation_routes
from fastapi.testclient import TestClient
from modules.automation_engine.commercial_queue.repository import CommercialQueueRepository
from modules.automation_engine.commercial_queue.service import CommercialQueueService


class SchedulerOrchestratorStub:
    def __init__(self, fail_accounts: set[str] | None = None, sleep_seconds: float = 0.0) -> None:
        self.fail_accounts = fail_accounts or set()
        self.sleep_seconds = sleep_seconds
        self.calls: list[dict] = []
        self.lock = threading.Lock()

    def __call__(self, **payload: object) -> dict:
        start = time.perf_counter()
        if self.sleep_seconds:
            time.sleep(self.sleep_seconds)
        end = time.perf_counter()
        with self.lock:
            self.calls.append({"account_id": payload["account_id"], "job_id": payload["job_id"], "start": start, "end": end})
        if payload["account_id"] in self.fail_accounts:
            raise RuntimeError(f"forced failure for {payload['account_id']}")
        return {
            "success": True,
            "forward_verified": False if payload.get("dry_run") else True,
            "diagnostics_consistent": True,
            "verified_forwarded_recipient_count": 0 if payload.get("dry_run") else 1,
            "confirm_click_count": 0 if payload.get("dry_run") else 1,
            "dry_run": bool(payload.get("dry_run")),
        }


def _service(path: Path, orchestrator: SchedulerOrchestratorStub | None = None, auth_blocked: set[str] | None = None) -> CommercialQueueService:
    service = CommercialQueueService(
        repository=CommercialQueueRepository(path),
        orchestrator=orchestrator or SchedulerOrchestratorStub(),
        account_auth_checker=lambda account_id: account_id not in (auth_blocked or set()),
        sleeper=lambda seconds: None,
    )
    service.update_global_settings(
        {
            "max_concurrent_accounts": 2,
            "deliveries_per_account_round": 1,
            "default_daily_limit_per_account": 10,
            "default_source_channel_uid": "5613544284",
            "delay_between_deliveries_seconds": 0,
            "round_cooldown_seconds": 0,
            "account_assignment_strategy": "priority_then_least_sent",
        }
    )
    return service


def _seed_accounts(service: CommercialQueueService, ids: list[str] | None = None) -> None:
    ids = ids or ["bale_a", "bale_b", "bale_c"]
    for index, account_id in enumerate(ids):
        service.update_account_settings(
            account_id,
            {
                "enabled": True,
                "priority": 100 - index,
                "source_channel_uid_override": "5613544284",
                "deliveries_per_round_override": 1,
                "round_cooldown_override": 0,
                "delay_between_deliveries_override": 0,
                "worker_status": "idle",
            },
        )


def _seed_jobs(service: CommercialQueueService, count: int = 6, source_channel_uid: str | None = "5613544284") -> dict:
    campaign = service.create_campaign({"name": "Scheduler Campaign", "platform": "bale", "status": "running", "source_channel_uid": source_channel_uid})
    phones = [f"093040734{index:02d}" for index in range(count)]
    service.import_recipients(campaign["id"], phones)
    for recipient in service.list_recipients(campaign["id"], limit=100)["items"]:
        service.repository.update_recipient_authorization(
            recipient["id"],
            {
                "recipient_origin": "user_provided",
                "synthetic_test_data": False,
                "live_execution_authorized": True,
                "live_authorized_at": "2026-07-13T00:00:00+00:00",
                "live_authorized_by": "test",
                "authorization_source": "test_fixture",
                "authorization_note": "mock scheduler recipient",
                "authorization_status": "authorized",
                "should_not_retry": False,
            },
        )
    return campaign


def test_max_concurrent_accounts_enforced_and_third_waits() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "scheduler.db")
        _seed_accounts(service)
        campaign = _seed_jobs(service, 6)
        service.scheduler_start()
        result = service.scheduler_run_once(campaign["id"], dry_run=True)
        status = service.scheduler_status()

    assert len(result["started_accounts"]) == 2
    assert len(set(result["started_accounts"])) == 2
    assert "bale_c" not in result["started_accounts"]
    assert status["queued_job_count"] == 4


def test_zero_slots_starts_no_workers() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "scheduler.db")
        _seed_accounts(service)
        campaign = _seed_jobs(service, 3, source_channel_uid=None)
        service.update_global_settings({"max_concurrent_accounts": 0})
        service.scheduler_start()
        result = service.scheduler_run_once(campaign["id"], dry_run=True)

    assert result["started_accounts"] == []
    assert result["reason"] == "no_available_slots"


def test_different_accounts_overlap_but_jobs_inside_account_are_sequential() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        orchestrator = SchedulerOrchestratorStub(sleep_seconds=0.2)
        service = _service(Path(tmp_dir) / "scheduler.db", orchestrator)
        _seed_accounts(service, ["bale_a", "bale_b"])
        campaign = _seed_jobs(service, 4)
        for account_id in ["bale_a", "bale_b"]:
            service.update_account_settings(account_id, {"deliveries_per_round_override": 2})
        service.update_global_settings({"deliveries_per_account_round": 2, "max_concurrent_accounts": 2})
        service.scheduler_start()
        result = service.scheduler_run_once(campaign["id"], dry_run=True)
        calls_by_account: dict[str, list[dict]] = {}
        for call in orchestrator.calls:
            calls_by_account.setdefault(call["account_id"], []).append(call)

    assert set(result["started_accounts"]) == {"bale_a", "bale_b"}
    first_a = calls_by_account["bale_a"][0]
    first_b = calls_by_account["bale_b"][0]
    assert first_a["start"] < first_b["end"] and first_b["start"] < first_a["end"]
    for calls in calls_by_account.values():
        calls.sort(key=lambda item: item["start"])
        assert calls[0]["end"] <= calls[1]["start"]


def test_ineligible_accounts_excluded() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "scheduler.db", auth_blocked={"bale_auth"})
        _seed_accounts(service, ["bale_disabled", "bale_cool", "bale_limit", "bale_auth", "bale_source", "bale_ok"])
        service.update_account_settings("bale_disabled", {"enabled": False})
        service.update_account_settings("bale_cool", {"cooldown_until": "2999-01-01T00:00:00+00:00"})
        service.update_account_settings("bale_limit", {"daily_limit_override": 1, "current_daily_sent_count": 1})
        service.update_account_settings("bale_source", {"source_channel_uid_override": ""})
        service.update_global_settings({"default_source_channel_uid": ""})
        campaign = _seed_jobs(service, 3, source_channel_uid=None)
        service.scheduler_start()
        result = service.scheduler_run_once(campaign["id"], dry_run=True)

    assert result["started_accounts"] == ["bale_ok"]


def test_assignment_strategies_are_deterministic() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "scheduler.db")
        _seed_accounts(service, ["bale_a", "bale_b", "bale_c"])
        service.update_account_settings("bale_a", {"priority": 50, "current_daily_sent_count": 2})
        service.update_account_settings("bale_b", {"priority": 90, "current_daily_sent_count": 4})
        service.update_account_settings("bale_c", {"priority": 90, "current_daily_sent_count": 1})
        _seed_jobs(service, 6)
        service.scheduler_start()
        service.update_global_settings({"account_assignment_strategy": "least_daily_sent"})
        least = service.scheduler_run_once(dry_run=True)["started_accounts"]
        _seed_jobs(service, 6)
        service.update_global_settings({"account_assignment_strategy": "priority_then_least_sent"})
        priority = service.scheduler_run_once(dry_run=True)["started_accounts"]
        _seed_jobs(service, 6)
        service.update_global_settings({"account_assignment_strategy": "round_robin"})
        rr1 = service.scheduler_run_once(dry_run=True)["started_accounts"]
        _seed_jobs(service, 6)
        rr2 = service.scheduler_run_once(dry_run=True)["started_accounts"]

    assert least[0] == "bale_c"
    assert priority[0] == "bale_c"
    assert rr1 != rr2


def test_one_worker_failure_isolated_from_other_account() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        orchestrator = SchedulerOrchestratorStub(fail_accounts={"bale_a"}, sleep_seconds=0.05)
        service = _service(Path(tmp_dir) / "scheduler.db", orchestrator)
        _seed_accounts(service, ["bale_a", "bale_b"])
        campaign = _seed_jobs(service, 4)
        service.scheduler_start()
        result = service.scheduler_run_once(campaign["id"], dry_run=True)

    by_account = {item["account_id"]: item for item in result["results"]}
    assert by_account["bale_a"]["error"]
    assert by_account["bale_b"]["processed_count"] == 1


def test_pause_stop_and_resume_semantics() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "scheduler.db")
        _seed_accounts(service)
        campaign = _seed_jobs(service, 3)
        service.scheduler_start()
        service.scheduler_pause()
        paused = service.scheduler_run_once(campaign["id"], dry_run=True)
        service.scheduler_stop()
        stopped = service.scheduler_run_once(campaign["id"], dry_run=True)
        service.scheduler_resume()
        resumed = service.scheduler_run_once(campaign["id"], dry_run=True)

    assert paused["reason"] == "scheduler_paused"
    assert stopped["reason"] == "scheduler_stopped"
    assert resumed["started_accounts"]


def test_state_persists_and_restart_invokes_stale_recovery() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "scheduler.db"
        service = _service(db_path)
        _seed_accounts(service, ["bale_a"])
        campaign = _seed_jobs(service, 1)
        service.scheduler_start()
        service.assign_jobs("bale_a", campaign["id"], 1)
        job = service.list_jobs(campaign_id=campaign["id"], limit=1)["items"][0]
        with service.repository.connection() as connection:
            connection.execute("UPDATE commercial_delivery_jobs SET status = 'running' WHERE id = ?", (job["id"],))
            connection.commit()
        reloaded = _service(db_path)
        recovered = reloaded.get_job(job["id"])
        state = reloaded.repository.get_scheduler_state()

    assert state["scheduler_status"] == "running"
    assert recovered is not None
    assert recovered["status"] == "paused"
    assert recovered["last_error_code"] == "manual_review_required"


def test_dashboard_summary_and_runtime_status_api() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        previous_service = automation_routes.commercial_queue_service
        service = _service(Path(tmp_dir) / "scheduler.db")
        automation_routes.commercial_queue_service = service
        try:
            _seed_accounts(service, ["bale_a", "bale_b", "bale_c"])
            service.update_account_settings("bale_c", {"enabled": False})
            _seed_jobs(service, 3)
            service.scheduler_start()
            client = TestClient(app)
            runtime = client.get("/automation/accounts/runtime-status?enabled=true&limit=2").json()
            summary = client.get("/automation/dashboard/summary").json()
            status = client.get("/automation/scheduler/status").json()
            run_once = client.post("/automation/scheduler/run-once", json={"dry_run": True}).json()
            pause = client.post("/automation/scheduler/pause").json()
            stop = client.post("/automation/scheduler/stop").json()
            resume = client.post("/automation/scheduler/resume").json()
        finally:
            automation_routes.commercial_queue_service = previous_service

    assert runtime["total"] == 2
    assert len(runtime["items"]) == 2
    assert summary["queued_jobs"] == 3
    assert status["scheduler_status"] == "running"
    assert run_once["started_accounts"]
    assert pause["scheduler_status"] == "paused"
    assert stop["scheduler_status"] == "stopped"
    assert resume["scheduler_status"] == "running"


if __name__ == "__main__":
    test_max_concurrent_accounts_enforced_and_third_waits()
    test_zero_slots_starts_no_workers()
    test_different_accounts_overlap_but_jobs_inside_account_are_sequential()
    test_ineligible_accounts_excluded()
    test_assignment_strategies_are_deterministic()
    test_one_worker_failure_isolated_from_other_account()
    test_pause_stop_and_resume_semantics()
    test_state_persists_and_restart_invokes_stale_recovery()
    test_dashboard_summary_and_runtime_status_api()
    print("Commercial scheduler tests passed")
