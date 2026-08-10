from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from modules.automation_engine.account_registry.bale_onboarding import BaleOnboardingError, BaleOnboardingService
from modules.automation_engine.account_registry.bale_onboarding_migrations import (
    IncompatibleOnboardingSchema, LATEST_VERSION, migrate,
)
from modules.automation_engine.commercial_queue.bale_identity import BaleOwnIdentityClassifier
from modules.automation_engine.plugins.bale.account_store import BaleAccountStore


def make_service(tmp_path: Path, inspector=lambda _record: []) -> BaleOnboardingService:
    store = BaleAccountStore(tmp_path / "registry")
    store._write_json(store.accounts_path, [])
    return BaleOnboardingService(tmp_path / "state.db", store, tmp_path / "profiles", inspector)


def provision_ready(service: BaleOnboardingService, phone="09123456789") -> str:
    account_id = service.provision({"identifier": phone, "idempotency_key": phone})["account"]["account_id"]
    with service.connection() as connection:
        connection.execute("UPDATE bale_operational_accounts SET scheduling_enabled=1 WHERE account_id=?", (account_id,))
        connection.commit()
    return account_id


def test_migration_fresh_repeated_and_prior_upgrade(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "fresh.db")
    assert migrate(connection) == LATEST_VERSION
    assert migrate(connection) == LATEST_VERSION
    assert connection.execute("SELECT version FROM bale_schema_versions").fetchone()[0] == LATEST_VERSION
    prior = sqlite3.connect(tmp_path / "prior.db")
    prior.execute("CREATE TABLE bale_schema_versions(component TEXT PRIMARY KEY, version INTEGER, migration_name TEXT, applied_at TEXT)")
    prior.execute("INSERT INTO bale_schema_versions VALUES('bale_onboarding',0,'legacy','now')")
    prior.commit()
    assert migrate(prior) == LATEST_VERSION


def test_partial_migration_rolls_back_and_newer_schema_rejected(tmp_path: Path, monkeypatch) -> None:
    import modules.automation_engine.account_registry.bale_onboarding_migrations as module
    database = tmp_path / "partial.db"
    connection = sqlite3.connect(database)
    original = module.MIGRATIONS
    failing = module.Migration(2, "fail", lambda conn: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(module, "MIGRATIONS", (original[0], failing))
    with pytest.raises(RuntimeError):
        module.migrate(connection)
    assert connection.execute("SELECT version FROM bale_schema_versions WHERE component='bale_onboarding'").fetchone() is None
    monkeypatch.setattr(module, "MIGRATIONS", original)
    module.migrate(connection)
    connection.execute("UPDATE bale_schema_versions SET version=999")
    connection.commit()
    with pytest.raises(IncompatibleOnboardingSchema):
        module.migrate(connection)


def test_configuration_persists_validates_and_safety_gates(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    updated = service.update_configuration({"max_login_concurrency": 3, "timezone": "UTC"})
    restarted = make_service(tmp_path)
    assert restarted.configuration()["max_login_concurrency"] == 3
    assert restarted.configuration()["timezone"] == "UTC"
    with pytest.raises(BaleOnboardingError):
        restarted.update_configuration({"max_login_concurrency": 0})
    with pytest.raises(BaleOnboardingError):
        restarted.update_configuration({"live_sending_enabled": True})


def test_rate_limits_hour_boundary_timezone_daily_reset_cooldown_and_idempotency(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    account_id = provision_ready(service)
    service.update_configuration({
        "timezone": "Asia/Tehran", "default_hourly_limit": 2, "default_daily_limit": 3,
        "default_minimum_delay_seconds": 0, "default_cooldown_seconds": 0,
    })
    base = datetime(2026, 1, 1, 20, 29, tzinfo=timezone.utc)  # 23:59 Tehran
    assert service.claim_delivery(account_id, "a", base)["claimed"]
    assert service.claim_delivery(account_id, "a", base)["idempotent_replay"]
    assert service.claim_delivery(account_id, "b", base + timedelta(minutes=2))["claimed"]
    before = service.rate_limit_eligibility(account_id, base + timedelta(minutes=3))
    assert "rolling_hourly_limit_reached" in before["reasons"]
    after_hour = service.rate_limit_eligibility(account_id, base + timedelta(hours=1, seconds=1))
    assert after_hour["daily_count"] == 1
    assert after_hour["local_day"] == "2026-01-02"


def test_concurrent_claim_is_atomic(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    account_id = provision_ready(service)
    service.update_configuration({"default_hourly_limit": 1, "default_daily_limit": 1, "default_minimum_delay_seconds": 0, "default_cooldown_seconds": 0})
    now = datetime.now(timezone.utc)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda index: service.claim_delivery(account_id, f"claim-{index}", now), range(4)))
    assert sum(bool(item["claimed"]) for item in results) == 1


class FakeLocator:
    def __init__(self, values): self.values = values
    def count(self): return len(self.values)
    def nth(self, index): return FakeItem(self.values[index])


class FakeItem:
    def __init__(self, value): self.value = value
    def is_visible(self): return True
    def inner_text(self): return self.value


class FakePage:
    def __init__(self, values, url="https://web.bale.ai/"): self.values, self.url = values, url
    def locator(self, _selector): return FakeLocator(self.values)


@pytest.mark.parametrize(
    ("values", "url", "status"),
    [
        (["09123456789"], "https://web.bale.ai/", "match"),
        (["09120000000"], "https://web.bale.ai/", "mismatch"),
        ([], "https://web.bale.ai/", "missing"),
        (["09123456789", "09120000000"], "https://web.bale.ai/", "ambiguous"),
        (["09123456789"], "https://web.bale.ai/login", "stale"),
    ],
)
def test_identity_classifier_safe_states(values, url, status) -> None:
    result = BaleOwnIdentityClassifier().classify(FakePage(values, url), "09123456789")
    assert result["status"] == status
    assert result["secrets_accessed"] is False
    assert "09123456789" not in json.dumps(result)


def test_restart_recovery_distinguishes_browser_and_gone(tmp_path: Path) -> None:
    active = make_service(tmp_path, inspector=lambda _record: [{"pid": 1}])
    account_id = provision_ready(active)
    with active.connection() as connection:
        connection.execute(
            """INSERT INTO bale_maintenance_sessions
            (maintenance_session_id,account_id,purpose,status,backend_instance_id,runtime_session_id,
             opened_at,heartbeat_at,expires_at,safe_diagnostics_json)
            VALUES ('s1',?,'login','active','old','runtime','old','old','2000-01-01','{}')""",
            (account_id,),
        )
        connection.commit()
    result = active.recover_stale_sessions()
    assert result["items"][0]["classification"] == "browser_active_not_reattachable"
    assert not result["items"][0]["released"]
    with active.connection() as connection:
        connection.execute("UPDATE bale_maintenance_sessions SET status='active', expires_at='2000-01-01'")
        connection.commit()
    gone = make_service(tmp_path, inspector=lambda _record: [])
    result = gone.recover_stale_sessions()
    assert result["items"][0]["classification"] == "browser_gone"
    assert result["items"][0]["released"]
