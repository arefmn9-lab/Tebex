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

def _v3(connection: sqlite3.Connection) -> None:
    existing = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(bale_operational_accounts)").fetchall()
    }
    columns = {
        "last_authenticated_at": "TEXT",
        "last_auth_verified_at": "TEXT",
        "last_profile_probe_at": "TEXT",
        "profile_probe_result": "TEXT",
        "auth_state_version": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, definition in columns.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE bale_operational_accounts ADD COLUMN {name} {definition}")

def _v4(connection: sqlite3.Connection) -> None:
    existing = {str(row[1]) for row in connection.execute("PRAGMA table_info(bale_operational_accounts)").fetchall()}
    if "verification_source" not in existing:
        connection.execute("ALTER TABLE bale_operational_accounts ADD COLUMN verification_source TEXT")

def _v5(connection: sqlite3.Connection) -> None:
    existing = {str(row[1]) for row in connection.execute("PRAGMA table_info(bale_operational_accounts)").fetchall()}
    if "authentication_state" not in existing:
        connection.execute("ALTER TABLE bale_operational_accounts ADD COLUMN authentication_state TEXT NOT NULL DEFAULT 'initial'")

def _v6(connection: sqlite3.Connection) -> None:
    """Separate durable identity binding, session health, and profile persistence evidence."""
    existing = {str(row[1]) for row in connection.execute("PRAGMA table_info(bale_operational_accounts)").fetchall()}
    columns = {
        "identity_bound_phone": "TEXT",
        "identity_verification_status": "TEXT NOT NULL DEFAULT 'unverified'",
        "identity_verified_at": "TEXT",
        "identity_verification_source": "TEXT",
        "identity_profile_generation_id": "TEXT",
        "last_identity_mismatch_at": "TEXT",
        "observed_identity_masked": "TEXT",
        "session_status": "TEXT NOT NULL DEFAULT 'unknown'",
        "last_session_probe_at": "TEXT",
        "last_session_probe_result": "TEXT",
        "last_authenticated_shell_at": "TEXT",
        "last_negative_auth_evidence_at": "TEXT",
        "last_negative_auth_evidence": "TEXT",
        "temporary_inconclusive_since": "TEXT",
        "session_probe_due_at": "TEXT",
        "profile_generation_id": "TEXT",
        "profile_created_at": "TEXT",
        "last_opened_at": "TEXT",
        "last_clean_close_at": "TEXT",
        "persistence_verified_at": "TEXT",
        "profile_health": "TEXT NOT NULL DEFAULT 'unknown'",
        "current_browser_owner": "TEXT",
        "profile_bound_identity": "TEXT",
    }
    for name, definition in columns.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE bale_operational_accounts ADD COLUMN {name} {definition}")

    # Safely promote previously proven exact visible-identity evidence.  This is a
    # schema interpretation, not fabricated authentication or a new probe.
    connection.execute(
        """UPDATE bale_operational_accounts SET
           identity_bound_phone=normalized_identifier,
           identity_verification_status='verified',
           identity_verified_at=COALESCE(identity_verified_at, authentication_verified_at),
           identity_verification_source=COALESCE(identity_verification_source, verification_source),
           session_status=CASE WHEN session_persistence_status='verified' THEN 'authenticated' ELSE session_status END,
           last_session_probe_at=COALESCE(last_session_probe_at, last_profile_probe_at),
           last_session_probe_result=COALESCE(last_session_probe_result, profile_probe_result),
           last_authenticated_shell_at=COALESCE(last_authenticated_shell_at, last_authenticated_at),
           persistence_verified_at=COALESCE(persistence_verified_at, last_profile_probe_at),
           profile_health=CASE WHEN session_persistence_status='verified' THEN 'healthy' ELSE profile_health END,
           profile_bound_identity=normalized_identifier
           WHERE profile_probe_result='authenticated_identity_verified'"""
    )

def _v7(connection: sqlite3.Connection) -> None:
    """Durable onboarding and explicit browser-launch provenance."""
    existing = {str(row[1]) for row in connection.execute("PRAGMA table_info(bale_operational_accounts)").fetchall()}
    columns = {
        "onboarding_completed": "INTEGER NOT NULL DEFAULT 0",
        "profile_persistence_verified": "INTEGER NOT NULL DEFAULT 0",
        "last_browser_launch_reason": "TEXT",
        "last_browser_launch_initiator": "TEXT",
        "last_browser_launch_at": "TEXT",
    }
    for name, definition in columns.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE bale_operational_accounts ADD COLUMN {name} {definition}")
    connection.execute(
        """UPDATE bale_operational_accounts SET onboarding_completed=1,
        profile_persistence_verified=1,
        identity_profile_generation_id=COALESCE(identity_profile_generation_id, profile_generation_id)
        WHERE identity_verification_status='verified'
          AND session_status='authenticated'
          AND session_persistence_status='verified'"""
    )


def _add_columns(connection: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, definition in columns.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _v8(connection: sqlite3.Connection) -> None:
    """Persist account-scoped operation and profile-lease ownership.

    Old rows intentionally remain readable with null ownership.  They are
    treated as legacy, reclaimable metadata when there is no live browser
    process; new rows always carry a runtime/PID/profile-generation binding.
    """
    _add_columns(connection, "bale_onboarding_operations", {
        "owner_runtime_id": "TEXT",
        "owner_pid": "INTEGER",
        "profile_generation_id": "TEXT",
        "heartbeat_at": "TEXT",
        "lease_released_at": "TEXT",
    })
    _add_columns(connection, "bale_profile_launch_locks", {
        "operation_id": "TEXT",
        "profile_generation_id": "TEXT",
        "owner_pid": "INTEGER",
    })
    _add_columns(connection, "bale_maintenance_sessions", {
        "operation_id": "TEXT",
        "profile_generation_id": "TEXT",
        "owner_pid": "INTEGER",
    })
    connection.execute(
        "CREATE INDEX IF NOT EXISTS ix_bale_operations_account_status_updated "
        "ON bale_onboarding_operations(account_id, status, updated_at DESC)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS ix_bale_profile_locks_operation "
        "ON bale_profile_launch_locks(operation_id)"
    )


def _v9(connection: sqlite3.Connection) -> None:
    """Persist the absolute deadline and terminal reason for account actions.

    ``heartbeat_at`` is liveness/progress evidence only.  It must never extend
    an operation's absolute execution deadline, so the deadline is stored as a
    separate durable field that startup recovery can evaluate without the
    in-memory task registry.
    """
    _add_columns(connection, "bale_onboarding_operations", {
        "deadline_at": "TEXT",
        "terminal_reason": "TEXT",
    })
    connection.execute(
        "CREATE INDEX IF NOT EXISTS ix_bale_operations_deadline_status "
        "ON bale_onboarding_operations(status, deadline_at)"
    )

MIGRATIONS = (
    Migration(1, "onboarding_core", _v1),
    Migration(2, "policy_and_rate_limits", _v2),
    Migration(3, "canonical_profile_authentication_state", _v3),
    Migration(4, "authentication_verification_evidence_source", _v4),
    Migration(5, "account_scoped_authentication_state_machine", _v5),
    Migration(6, "durable_identity_session_and_profile_evidence", _v6),
    Migration(7, "durable_onboarding_and_launch_provenance", _v7),
    Migration(8, "account_operation_and_profile_lease_ownership", _v8),
    Migration(9, "account_operation_absolute_deadline", _v9),
)
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
