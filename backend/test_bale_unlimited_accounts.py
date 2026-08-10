from __future__ import annotations
import sqlite3
from pathlib import Path

from modules.automation_engine.account_registry.bale_onboarding import BaleOnboardingService
from modules.automation_engine.commercial_queue.repository import CommercialQueueRepository
from modules.automation_engine.commercial_queue.resources import ResourceCapacityProvider, ResourceSnapshot
from modules.automation_engine.commercial_queue.service import CommercialQueueService
from modules.automation_engine.db.models import initialize_schema
from modules.automation_engine.plugins.bale.account_store import BaleAccountStore

def test_registry_supports_more_than_forty_accounts(tmp_path: Path) -> None:
    store = BaleAccountStore(tmp_path / "registry"); store._write_json(store.accounts_path, [])
    service = BaleOnboardingService(database_path=tmp_path / "accounts.db", account_store=store, profile_root=tmp_path / "profiles", process_inspector=lambda _: [])
    for index in range(101):
        phone = f"0912{index:07d}"
        service.provision({"identifier": phone, "idempotency_key": f"account-{index}", "created_by": "test"})
    assert len(service.list_accounts()["items"]) == 101
    assert len({item["account_id"] for item in service.list_accounts()["items"]}) == 101

def test_one_hundred_mocked_accounts_are_dynamic_and_account_scoped(tmp_path: Path) -> None:
    authenticated = {f"bale_scale_{index}" for index in range(100)}
    service = CommercialQueueService(repository=CommercialQueueRepository(tmp_path / "queue.db"), account_auth_checker=lambda account_id: account_id in authenticated)
    for index in range(100):
        service.update_account_settings(f"bale_scale_{index}", {"enabled": True, "priority": index, "daily_limit_override": 10, "worker_status": "idle"})
    rows = [row for row in service.account_readiness_matrix() if row["account_id"].startswith("bale_scale_")]
    assert len(rows) == 100
    assert all(row["worker_eligible"] for row in rows)
    service.update_account_settings("bale_scale_0", {"worker_status": "running", "current_daily_sent_count": 3})
    assert service.repository.get_account_settings("bale_scale_1")["worker_status"] == "idle"
    assert service.repository.get_account_settings("bale_scale_1")["current_daily_sent_count"] == 0

def test_newly_authenticated_account_is_discovered_without_restart(tmp_path: Path) -> None:
    authenticated: set[str] = set()
    service = CommercialQueueService(repository=CommercialQueueRepository(tmp_path / "dynamic.db"), account_auth_checker=lambda account_id: account_id in authenticated)
    service.update_account_settings("bale_dynamic", {"enabled": True, "worker_status": "idle"})
    assert next(row for row in service.account_readiness_matrix() if row["account_id"] == "bale_dynamic")["worker_eligible"] is False
    authenticated.add("bale_dynamic")
    assert next(row for row in service.account_readiness_matrix() if row["account_id"] == "bale_dynamic")["worker_eligible"] is True

def test_scheduler_wakes_on_eligibility_transition(tmp_path: Path) -> None:
    class Runtime:
        calls = 0
        def wake_eligibility(self): self.calls += 1; return True
    service = CommercialQueueService(repository=CommercialQueueRepository(tmp_path / "wake.db"), account_auth_checker=lambda _: True)
    service.update_account_settings("bale_wake", {"enabled": True, "worker_status": "idle"})
    runtime = Runtime(); service.scheduler_runtime = runtime
    result = service.activate_verified_account_eligibility("bale_wake", previously_eligible=False)
    assert result["worker_eligible"] is True and result["scheduler_woken"] is True and runtime.calls == 1

def test_concurrency_is_capacity_driven_not_registration_count(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CLINICOS_BROWSER_SLOT_CAPACITY", "7"); monkeypatch.setenv("CLINICOS_WORKER_SLOT_CAPACITY", "5")
    provider = ResourceCapacityProvider(CommercialQueueRepository(tmp_path / "capacity.db"))
    inputs = provider.configuration_inputs(configured_max=20, eligible_count=100, host_resource_capacity=9)
    assert inputs["effective_concurrency"] == 5
    assert inputs["eligible_account_count"] == 100

def test_account_contact_proof_uniqueness_is_account_scoped(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "proofs.db"); initialize_schema(connection)
    values=("proof-1","account-a","mapping-1","not_prepared","unverified","2026-01-01","2026-01-01")
    connection.execute("INSERT INTO bale_account_contact_proofs(id,account_id,mapping_id,preparation_status,verification_status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", values)
    connection.execute("INSERT INTO bale_account_contact_proofs(id,account_id,mapping_id,preparation_status,verification_status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", ("proof-2","account-b","mapping-1","not_prepared","unverified","2026-01-01","2026-01-01"))
    try:
        connection.execute("INSERT INTO bale_account_contact_proofs(id,account_id,mapping_id,preparation_status,verification_status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", ("proof-3","account-a","mapping-1","not_prepared","unverified","2026-01-01","2026-01-01"))
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("duplicate account_id + mapping_id must be rejected")
