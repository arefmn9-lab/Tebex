from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable


class IncompatibleOnboardingSchema(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _execute_statements(connection: sqlite3.Connection, script: str) -> None:
    for statement in script.split(";"):
        if statement.strip():
            connection.execute(statement)

def _v1(connection: sqlite3.Connection) -> None:
    _execute_statements(connection,
        """
        CREATE TABLE IF NOT EXISTS bale_operational_accounts (
            account_id TEXT PRIMARY KEY, normalized_identifier TEXT NOT NULL UNIQUE,
            masked_identifier TEXT NOT NULL, lifecycle_status TEXT NOT NULL,
            scheduling_enabled INTEGER NOT NULL DEFAULT 0,
            authentication_status TEXT NOT NULL DEFAULT 'unverified',
            authentication_verified_at TEXT, verification_expires_at TEXT,
            session_persistence_status TEXT NOT NULL DEFAULT 'unknown',
            health_status TEXT NOT NULL DEFAULT 'unknown', blocking_reason TEXT,
            last_error_code TEXT, safe_error_message TEXT,
            canonical_profile_path TEXT NOT NULL UNIQUE,
            normalized_profile_path TEXT NOT NULL UNIQUE, browser_identity_id TEXT,
            onboarding_blocked INTEGER NOT NULL DEFAULT 0, retired_at TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, audit_metadata_json TEXT
        );
        CREATE TABLE IF NOT EXISTS bale_onboarding_batches (
            onboarding_batch_id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL,
            target_count INTEGER NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
            started_at TEXT, completed_at TEXT, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bale_onboarding_batch_accounts (
            onboarding_batch_id TEXT NOT NULL, account_id TEXT NOT NULL,
            membership_kind TEXT NOT NULL, added_at TEXT NOT NULL,
            PRIMARY KEY(onboarding_batch_id, account_id)
        );
        CREATE TABLE IF NOT EXISTS bale_onboarding_operations (
            operation_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
            request_hash TEXT NOT NULL, operation_type TEXT NOT NULL, account_id TEXT,
            onboarding_batch_id TEXT, status TEXT NOT NULL, current_step TEXT,
            progress_json TEXT NOT NULL, error_code TEXT, safe_error_message TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bale_account_audit_events (
            event_id TEXT PRIMARY KEY, account_id TEXT, onboarding_batch_id TEXT,
            operation_id TEXT, event_type TEXT NOT NULL, actor TEXT NOT NULL,
            safe_message TEXT NOT NULL, metadata_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bale_maintenance_sessions (
            maintenance_session_id TEXT PRIMARY KEY, account_id TEXT NOT NULL UNIQUE,
            purpose TEXT NOT NULL, status TEXT NOT NULL, backend_instance_id TEXT NOT NULL,
            runtime_session_id TEXT, opened_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL,
            expires_at TEXT NOT NULL, closed_at TEXT, last_auth_state TEXT,
            safe_diagnostics_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bale_profile_launch_locks (
            normalized_profile_path TEXT PRIMARY KEY, account_id TEXT NOT NULL UNIQUE,
            owner_id TEXT NOT NULL, backend_instance_id TEXT NOT NULL,
            acquired_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL, expires_at TEXT NOT NULL
        );
        """
    )


def _v2(connection: sqlite3.Connection) -> None:
    _execute_statements(connection,
        """
        CREATE TABLE IF NOT EXISTS bale_operational_configuration (
            id TEXT PRIMARY KEY CHECK(id='global'), configuration_json TEXT NOT NULL,
            revision INTEGER NOT NULL, updated_by TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bale_delivery_events (
            event_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, occurred_at TEXT NOT NULL,
            local_day TEXT NOT NULL, claim_token TEXT NOT NULL UNIQUE
        );
        CREATE INDEX IF NOT EXISTS ix_bale_delivery_events_account_time
            ON bale_delivery_events(account_id, occurred_at);
        CREATE TABLE IF NOT EXISTS bale_account_limit_overrides (
            account_id TEXT PRIMARY KEY, hourly_limit INTEGER, daily_limit INTEGER,
            minimum_delay_seconds INTEGER, round_size INTEGER, cooldown_seconds INTEGER,
            updated_at TEXT NOT NULL
        );
        """
    )


MIGRATIONS = (Migration(1, "onboarding_core", _v1), Migration(2, "policy_and_rate_limits", _v2))
LATEST_VERSION = MIGRATIONS[-1].version


def migrate(connection: sqlite3.Connection) -> int:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS bale_schema_versions (
        component TEXT PRIMARY KEY, version INTEGER NOT NULL, migration_name TEXT NOT NULL,
        applied_at TEXT NOT NULL)"""
    )
    row = connection.execute(
        "SELECT version FROM bale_schema_versions WHERE component='bale_onboarding'"
    ).fetchone()
    current = int(row[0]) if row else 0
    if current > LATEST_VERSION:
        raise IncompatibleOnboardingSchema(
            f"database schema {current} is newer than supported {LATEST_VERSION}"
        )
    try:
        connection.execute("BEGIN IMMEDIATE")
        for migration in MIGRATIONS:
            if migration.version <= current:
                continue
            migration.apply(connection)
            connection.execute(
                """INSERT INTO bale_schema_versions(component, version, migration_name, applied_at)
                VALUES('bale_onboarding', ?, ?, ?)
                ON CONFLICT(component) DO UPDATE SET version=excluded.version,
                migration_name=excluded.migration_name, applied_at=excluded.applied_at""",
                (migration.version, migration.name, _utc_now()),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    verify_schema(connection)
    return LATEST_VERSION


def verify_schema(connection: sqlite3.Connection) -> None:
    required = {
        "bale_operational_accounts",
        "bale_onboarding_batches",
        "bale_maintenance_sessions",
        "bale_operational_configuration",
        "bale_delivery_events",
    }
    found = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = sorted(required - found)
    if missing:
        raise IncompatibleOnboardingSchema(f"missing required tables: {', '.join(missing)}")
