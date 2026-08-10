from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

os.environ["CLINICOS_DISABLE_SCHEDULER_RUNTIME"] = "1"
os.environ["CLINICOS_DISABLE_AUTH_MAINTENANCE_RUNTIME"] = "1"

from modules.automation_engine.account_registry.bale_onboarding import BaleOnboardingService
from modules.automation_engine.commercial_queue.repository import CommercialQueueRepository
from modules.automation_engine.commercial_queue.service import CommercialQueueService


class FakeAccountStore:
    def __init__(self, count: int) -> None:
        self.items = [{"account_id": f"bale_{index:04d}", "phone": f"0912{index:07d}"} for index in range(count)]

    def list_accounts(self):
        return list(self.items)

    def get_account(self, account_id):
        return next((item for item in self.items if item["account_id"] == account_id), None)


def test_onboarding_list_query_shape_is_constant_for_9_40_and_257(monkeypatch, tmp_path: Path):
    for count in (9, 40, 257):
        service = BaleOnboardingService(
            database_path=tmp_path / f"accounts-{count}.db",
            account_store=FakeAccountStore(count),
            profile_root=tmp_path / f"profiles-{count}",
            process_inspector=lambda _profile: [],
        )
        calls = 0
        original = service.configuration

        def counted_configuration():
            nonlocal calls
            calls += 1
            return original()

        monkeypatch.setattr(service, "configuration", counted_configuration)
        started = time.perf_counter()
        result = service.list_accounts()
        assert len(result["items"]) == count
        assert calls == 1
        assert time.perf_counter() - started < 2.0


class SnapshotRepository:
    def __init__(self):
        self.calls = []

    def get_scheduler_state(self):
        self.calls.append("state")
        return {"scheduler_status": "paused", "last_tick_results_json": "[]", "updated_at": "2026-08-03T00:00:00+00:00"}

    get_scheduler_state_snapshot = get_scheduler_state

    def list_active_worker_locks(self):
        self.calls.append("locks")
        return []

    def count_jobs_by_status(self):
        self.calls.append("jobs")
        return {"queued": 139}

    def dashboard_aggregates(self):
        self.calls.append("dashboard")
        return {"jobs": {"queued": 139}, "jobs_today": {}, "campaigns": {}, "total_accounts": 9, "active_workers": 0}


def test_scheduler_status_is_snapshot_only_and_dashboard_is_bounded():
    repository = SnapshotRepository()
    service = object.__new__(CommercialQueueService)
    service.repository = repository
    service.scheduler_runtime = None
    service.reconcile_worker_states = lambda: (_ for _ in ()).throw(AssertionError("reconcile called"))
    service.account_readiness_matrix = lambda: (_ for _ in ()).throw(AssertionError("readiness called"))
    status = service.scheduler_status_snapshot()
    assert status["readiness_calculated"] is False
    assert repository.calls == ["state", "locks", "jobs"]
    repository.calls.clear()
    summary = service.dashboard_summary()
    assert summary["total_accounts"] == 9
    assert repository.calls == ["dashboard", "state", "locks", "jobs"]


def test_campaign_list_bulk_loads_reservations_without_readiness(tmp_path: Path):
    repository = CommercialQueueRepository(tmp_path / "campaign-list.db")
    service = CommercialQueueService(repository=repository)
    for index in range(40):
        repository.create_campaign({"name": f"c{index}", "platform": "bale"})
    service.campaign_capacity_pool = lambda: (_ for _ in ()).throw(AssertionError("capacity/readiness called"))
    result = service.list_campaigns(limit=100)
    assert len(result["items"]) == 40
    assert "capacity_pool" not in result


def test_resolved_test_databases_are_not_production(tmp_path: Path):
    production = (Path(__file__).parent / "clinicos.db").resolve()
    repository = CommercialQueueRepository(tmp_path / "isolated.db")
    assert repository.database_path.resolve() != production
    assert "clinicos.db" not in str(repository.database_path)
