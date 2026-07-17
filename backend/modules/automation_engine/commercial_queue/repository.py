from __future__ import annotations

import json
import hashlib
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from modules.automation_engine.db.database import DATABASE_PATH
from modules.automation_engine.db.models import initialize_schema


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def manifest_hash(phones: list[str]) -> str:
    payload = json.dumps(sorted(str(phone) for phone in phones), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


PLATFORM_RUN_OUTCOMES = {
    "sent",
    "account_not_found",
    "not_reachable",
    "blocked",
    "failed_retryable",
    "failed_terminal",
    "skipped_by_policy",
    "cancelled",
    "pending",
    "queued",
    "assigned",
    "in_progress",
}
TERMINAL_PLATFORM_OUTCOMES = {
    "sent",
    "account_not_found",
    "not_reachable",
    "blocked",
    "failed_terminal",
    "skipped_by_policy",
    "cancelled",
}
ACTIVE_PLATFORM_OUTCOMES = {"queued", "assigned", "in_progress"}


def aggregate_scenario_status(outcomes: list[str]) -> str:
    if not outcomes:
        return "pending"
    if any(outcome == "failed_retryable" for outcome in outcomes):
        return "retry_pending"
    if all(outcome in TERMINAL_PLATFORM_OUTCOMES for outcome in outcomes):
        return "completed"
    if any(outcome in ACTIVE_PLATFORM_OUTCOMES for outcome in outcomes):
        return "in_progress"
    return "pending"


def aggregate_platform_counts(outcomes: list[str]) -> dict[str, int]:
    checked = [outcome for outcome in outcomes if outcome in TERMINAL_PLATFORM_OUTCOMES or outcome == "failed_retryable"]
    return {
        "selected_platform_count": len(outcomes),
        "checked_platform_count": len(checked),
        "sent_platform_count": sum(1 for outcome in outcomes if outcome == "sent"),
        "account_not_found_count": sum(1 for outcome in outcomes if outcome == "account_not_found"),
        "failed_platform_count": sum(1 for outcome in outcomes if outcome in {"failed_retryable", "failed_terminal"}),
        "retryable_platform_count": sum(1 for outcome in outcomes if outcome == "failed_retryable"),
        "pending_platform_count": sum(1 for outcome in outcomes if outcome in {"pending", "queued", "assigned", "in_progress"}),
    }


def queue_claim_eligibility_where(job_alias: str = "job", recipient_alias: str = "recipient", manifest_alias: str = "manifest") -> str:
    return f"""
        {recipient_alias}.input_manifest_id IS NOT NULL
        AND {recipient_alias}.input_manifest_hash IS NOT NULL
        AND {recipient_alias}.input_provenance_status = 'confirmed_manifest'
        AND {manifest_alias}.manifest_id = {recipient_alias}.input_manifest_id
        AND {manifest_alias}.campaign_id = {recipient_alias}.campaign_id
        AND {manifest_alias}.confirmation_status = 'confirmed'
        AND {manifest_alias}.manifest_hash = {recipient_alias}.input_manifest_hash
        AND {manifest_alias}.normalized_phones_json LIKE ('%' || '"' || {recipient_alias}.phone_normalized || '"' || '%')
        AND {recipient_alias}.recipient_origin IN ('user_provided', 'user_import', 'manual_import')
        AND COALESCE({recipient_alias}.live_execution_authorized, 0) = 1
        AND {recipient_alias}.authorization_status = 'authorized'
        AND COALESCE({recipient_alias}.live_execution_blocked, 0) = 0
        AND COALESCE({recipient_alias}.should_not_retry, 0) = 0
        AND COALESCE({recipient_alias}.synthetic_test_data, 0) = 0
        AND COALESCE({job_alias}.live_execution_authorized, {recipient_alias}.live_execution_authorized, 0) = 1
        AND COALESCE({job_alias}.authorization_status, {recipient_alias}.authorization_status) = 'authorized'
        AND COALESCE({job_alias}.live_execution_blocked, 0) = 0
        AND COALESCE({job_alias}.should_not_retry, {recipient_alias}.should_not_retry, 0) = 0
        AND COALESCE({job_alias}.synthetic_test_data, {recipient_alias}.synthetic_test_data, 0) = 0
        AND COALESCE({job_alias}.input_manifest_id, {recipient_alias}.input_manifest_id) = {recipient_alias}.input_manifest_id
        AND COALESCE({job_alias}.input_manifest_hash, {recipient_alias}.input_manifest_hash) = {recipient_alias}.input_manifest_hash
        AND COALESCE({job_alias}.input_provenance_status, {recipient_alias}.input_provenance_status) = 'confirmed_manifest'
        AND {job_alias}.attempt_count < {job_alias}.max_attempts
        AND (
            {job_alias}.execution_batch_id IS NULL
            OR EXISTS (
                SELECT 1 FROM commercial_execution_batches AS batch
                WHERE batch.id = {job_alias}.execution_batch_id
                  AND batch.campaign_id = {job_alias}.campaign_id
                  AND batch.status IN ('queued','in_progress')
                  AND batch.mode = COALESCE({job_alias}.adapter_mode, 'mock_only')
                  AND batch.mode = 'mock_only'
                  AND batch.cancellation_reason IS NULL
            )
        )
    """


class CommercialQueueRepository:
    def __init__(self, database_path: Path | None = None) -> None:
        self.database_path = database_path or DATABASE_PATH

    def connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        initialize_schema(connection)
        return connection

    @contextmanager
    def connection(self) -> Any:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    def get_global_settings(self) -> dict[str, Any] | None:
        with self.connection() as connection:
            return row_to_dict(connection.execute("SELECT * FROM commercial_global_settings WHERE id = 'global'").fetchone())

    def get_scheduler_state(self) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM commercial_scheduler_state WHERE id = 'default'").fetchone()
            if row is not None:
                return dict(row)
            now = utc_now()
            connection.execute(
                """
                INSERT INTO commercial_scheduler_state (
                    id, scheduler_status, round_robin_cursor, last_tick_at,
                    last_started_at, last_stopped_at, current_tick_id,
                    last_tick_results_json, created_at, updated_at
                )
                VALUES ('default', 'stopped', NULL, NULL, NULL, NULL, NULL, NULL, ?, ?)
                """,
                (now, now),
            )
            connection.commit()
            return dict(connection.execute("SELECT * FROM commercial_scheduler_state WHERE id = 'default'").fetchone())

    def update_scheduler_state(self, updates: dict[str, Any]) -> dict[str, Any]:
        current = self.get_scheduler_state()
        payload = {**current, **updates, "updated_at": utc_now()}
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO commercial_scheduler_state (
                    id, scheduler_status, round_robin_cursor, last_tick_at,
                    last_started_at, last_stopped_at, current_tick_id,
                    last_tick_results_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    scheduler_status = excluded.scheduler_status,
                    round_robin_cursor = excluded.round_robin_cursor,
                    last_tick_at = excluded.last_tick_at,
                    last_started_at = excluded.last_started_at,
                    last_stopped_at = excluded.last_stopped_at,
                    current_tick_id = excluded.current_tick_id,
                    last_tick_results_json = excluded.last_tick_results_json,
                    updated_at = excluded.updated_at
                """,
                (
                    "default",
                    payload["scheduler_status"],
                    payload.get("round_robin_cursor"),
                    payload.get("last_tick_at"),
                    payload.get("last_started_at"),
                    payload.get("last_stopped_at"),
                    payload.get("current_tick_id"),
                    payload.get("last_tick_results_json"),
                    payload.get("created_at") or utc_now(),
                    payload["updated_at"],
                ),
            )
            connection.commit()
        return self.get_scheduler_state()

    def upsert_global_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        existing = self.get_global_settings()
        now = utc_now()
        created_at = str((existing or {}).get("created_at") or now)
        record = {**payload, "id": "global", "created_at": created_at, "updated_at": now}
        defaults = {
            "send_method": "forward_latest_channel_message",
            "operation_order_json": json.dumps(["save_contact", "forward_message"], ensure_ascii=False),
            "link_open_delay_seconds": 0,
            "browser_start_batch_size": 10,
            "browser_start_stagger_ms": 250,
            "max_system_memory_percent": 90,
            "max_system_cpu_percent": 95,
            "session_reuse_enabled": False,
            "resource_guard_enabled": False,
            "campaign_overrides_enabled": True,
            "automatic_retry_enabled": False,
            "live_campaign_execution_enabled": False,
        }
        record = {**defaults, **record}
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO commercial_global_settings (
                    id, max_concurrent_accounts, deliveries_per_account_round,
                    delay_between_deliveries_seconds, round_cooldown_seconds,
                    default_daily_limit_per_account, default_source_channel_uid,
                    account_assignment_strategy, max_job_duration_seconds,
                    job_timeout_seconds, auto_pause_on_auth_error,
                    auto_pause_on_selector_error, send_method, operation_order_json,
                    link_open_delay_seconds, browser_start_batch_size,
                    browser_start_stagger_ms, max_system_memory_percent,
                    max_system_cpu_percent, session_reuse_enabled,
                    resource_guard_enabled, campaign_overrides_enabled,
                    automatic_retry_enabled, live_campaign_execution_enabled,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    max_concurrent_accounts = excluded.max_concurrent_accounts,
                    deliveries_per_account_round = excluded.deliveries_per_account_round,
                    delay_between_deliveries_seconds = excluded.delay_between_deliveries_seconds,
                    round_cooldown_seconds = excluded.round_cooldown_seconds,
                    default_daily_limit_per_account = excluded.default_daily_limit_per_account,
                    default_source_channel_uid = excluded.default_source_channel_uid,
                    account_assignment_strategy = excluded.account_assignment_strategy,
                    max_job_duration_seconds = excluded.max_job_duration_seconds,
                    job_timeout_seconds = excluded.job_timeout_seconds,
                    auto_pause_on_auth_error = excluded.auto_pause_on_auth_error,
                    auto_pause_on_selector_error = excluded.auto_pause_on_selector_error,
                    send_method = excluded.send_method,
                    operation_order_json = excluded.operation_order_json,
                    link_open_delay_seconds = excluded.link_open_delay_seconds,
                    browser_start_batch_size = excluded.browser_start_batch_size,
                    browser_start_stagger_ms = excluded.browser_start_stagger_ms,
                    max_system_memory_percent = excluded.max_system_memory_percent,
                    max_system_cpu_percent = excluded.max_system_cpu_percent,
                    session_reuse_enabled = excluded.session_reuse_enabled,
                    resource_guard_enabled = excluded.resource_guard_enabled,
                    campaign_overrides_enabled = excluded.campaign_overrides_enabled,
                    automatic_retry_enabled = excluded.automatic_retry_enabled,
                    live_campaign_execution_enabled = excluded.live_campaign_execution_enabled,
                    updated_at = excluded.updated_at
                """,
                (
                    record["id"],
                    record["max_concurrent_accounts"],
                    record["deliveries_per_account_round"],
                    record["delay_between_deliveries_seconds"],
                    record["round_cooldown_seconds"],
                    record["default_daily_limit_per_account"],
                    record["default_source_channel_uid"],
                    record["account_assignment_strategy"],
                    record["max_job_duration_seconds"],
                    record["job_timeout_seconds"],
                    int(bool(record["auto_pause_on_auth_error"])),
                    int(bool(record["auto_pause_on_selector_error"])),
                    record["send_method"],
                    record["operation_order_json"],
                    record["link_open_delay_seconds"],
                    record["browser_start_batch_size"],
                    record["browser_start_stagger_ms"],
                    record["max_system_memory_percent"],
                    record["max_system_cpu_percent"],
                    int(bool(record["session_reuse_enabled"])),
                    int(bool(record["resource_guard_enabled"])),
                    int(bool(record["campaign_overrides_enabled"])),
                    int(bool(record["automatic_retry_enabled"])),
                    int(bool(record["live_campaign_execution_enabled"])),
                    record["created_at"],
                    record["updated_at"],
                ),
            )
            connection.commit()
        return self.get_global_settings() or record

    def list_account_settings(self, limit: int, offset: int) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM commercial_account_settings ORDER BY priority DESC, account_id ASC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_all_account_settings(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM commercial_account_settings ORDER BY priority DESC, account_id ASC"
            ).fetchall()
            return [dict(row) for row in rows]

    def get_account_settings(self, account_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            return row_to_dict(
                connection.execute("SELECT * FROM commercial_account_settings WHERE account_id = ?", (account_id,)).fetchone()
            )

    def upsert_account_settings(self, account_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        existing = self.get_account_settings(account_id)
        now = utc_now()
        base = {
            "id": (existing or {}).get("id") or new_id("acct_settings"),
            "account_id": account_id,
            "enabled": True,
            "priority": 100,
            "daily_limit_override": None,
            "deliveries_per_round_override": None,
            "delay_between_deliveries_override": None,
            "round_cooldown_override": None,
            "source_channel_uid_override": None,
            "current_daily_sent_count": 0,
            "current_round_sent_count": 0,
            "worker_status": "idle",
            "cooldown_until": None,
            "last_job_started_at": None,
            "last_job_completed_at": None,
            "last_error_code": None,
            "last_error_message": None,
            "created_at": (existing or {}).get("created_at") or now,
            "updated_at": now,
        }
        record = {**base, **{key: value for key, value in payload.items() if value is not None or key in payload}}
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO commercial_account_settings (
                    id, account_id, enabled, priority, daily_limit_override,
                    deliveries_per_round_override, delay_between_deliveries_override,
                    round_cooldown_override, source_channel_uid_override,
                    current_daily_sent_count, current_round_sent_count, worker_status,
                    cooldown_until, last_job_started_at, last_job_completed_at,
                    last_error_code, last_error_message, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    priority = excluded.priority,
                    daily_limit_override = excluded.daily_limit_override,
                    deliveries_per_round_override = excluded.deliveries_per_round_override,
                    delay_between_deliveries_override = excluded.delay_between_deliveries_override,
                    round_cooldown_override = excluded.round_cooldown_override,
                    source_channel_uid_override = excluded.source_channel_uid_override,
                    current_daily_sent_count = excluded.current_daily_sent_count,
                    current_round_sent_count = excluded.current_round_sent_count,
                    worker_status = excluded.worker_status,
                    cooldown_until = excluded.cooldown_until,
                    last_job_started_at = excluded.last_job_started_at,
                    last_job_completed_at = excluded.last_job_completed_at,
                    last_error_code = excluded.last_error_code,
                    last_error_message = excluded.last_error_message,
                    updated_at = excluded.updated_at
                """,
                (
                    record["id"],
                    record["account_id"],
                    int(bool(record["enabled"])),
                    record["priority"],
                    record["daily_limit_override"],
                    record["deliveries_per_round_override"],
                    record["delay_between_deliveries_override"],
                    record["round_cooldown_override"],
                    record["source_channel_uid_override"],
                    record["current_daily_sent_count"],
                    record["current_round_sent_count"],
                    record["worker_status"],
                    record["cooldown_until"],
                    record["last_job_started_at"],
                    record["last_job_completed_at"],
                    record["last_error_code"],
                    record["last_error_message"],
                    record["created_at"],
                    record["updated_at"],
                ),
            )
            connection.commit()
        return self.get_account_settings(account_id) or record

    def create_campaign(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        record = {
            "id": payload.get("id") or new_id("campaign"),
            "name": payload["name"],
            "platform": payload.get("platform") or "bale",
            "status": payload.get("status") or "draft",
            "source_channel_uid": payload.get("source_channel_uid"),
            "policy_overrides_json": json.dumps(payload.get("policy_overrides") or {}, ensure_ascii=False) if payload.get("policy_overrides") is not None else payload.get("policy_overrides_json"),
            "total_recipients": 0,
            "queued_count": 0,
            "running_count": 0,
            "succeeded_count": 0,
            "failed_count": 0,
            "skipped_count": 0,
            "cancelled_count": 0,
            "created_at": now,
            "started_at": None,
            "paused_at": None,
            "completed_at": None,
            "updated_at": now,
        }
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO commercial_campaigns (
                    id, name, platform, status, source_channel_uid, policy_overrides_json, total_recipients,
                    queued_count, running_count, succeeded_count, failed_count,
                    skipped_count, cancelled_count, created_at, started_at,
                    paused_at, completed_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(record[key] for key in [
                    "id", "name", "platform", "status", "source_channel_uid", "policy_overrides_json", "total_recipients",
                    "queued_count", "running_count", "succeeded_count", "failed_count", "skipped_count",
                    "cancelled_count", "created_at", "started_at", "paused_at", "completed_at", "updated_at",
                ]),
            )
            connection.commit()
        return record

    def get_campaign(self, campaign_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            return row_to_dict(connection.execute("SELECT * FROM commercial_campaigns WHERE id = ?", (campaign_id,)).fetchone())

    def list_campaigns(self, status: str | None, limit: int, offset: int) -> list[dict[str, Any]]:
        query = "SELECT * FROM commercial_campaigns"
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(query, params).fetchall()]

    def update_campaign(self, campaign_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {"name", "platform", "status", "source_channel_uid", "policy_overrides_json", "started_at", "paused_at", "completed_at"}
        updates = {key: value for key, value in payload.items() if key in allowed}
        if "policy_overrides" in payload:
            updates["policy_overrides_json"] = json.dumps(payload.get("policy_overrides") or {}, ensure_ascii=False)
        if not updates:
            return self.get_campaign(campaign_id)
        updates["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in updates)
        with self.connection() as connection:
            connection.execute(f"UPDATE commercial_campaigns SET {assignments} WHERE id = ?", (*updates.values(), campaign_id))
            connection.commit()
        return self.get_campaign(campaign_id)

    def create_configuration_revision(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        record = {
            "revision_id": payload.get("revision_id") or new_id("cfgrev"),
            "campaign_id": payload["campaign_id"],
            "revision_number": int(payload["revision_number"]),
            "status": payload.get("status") or "draft",
            "configuration_json": payload["configuration_json"],
            "configuration_hash": payload["configuration_hash"],
            "resolved_configuration_json": payload["resolved_configuration_json"],
            "resolved_configuration_hash": payload["resolved_configuration_hash"],
            "origin_trace_json": payload["origin_trace_json"],
            "change_summary": payload.get("change_summary"),
            "created_by": payload.get("created_by"),
            "created_at": payload.get("created_at") or now,
            "validated_at": payload.get("validated_at"),
            "approved_at": payload.get("approved_at"),
            "activated_at": payload.get("activated_at"),
            "superseded_at": payload.get("superseded_at"),
            "effective_from_job_sequence": payload.get("effective_from_job_sequence"),
            "parent_revision_id": payload.get("parent_revision_id"),
        }
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO commercial_campaign_configuration_revisions (
                    revision_id, campaign_id, revision_number, status,
                    configuration_json, configuration_hash,
                    resolved_configuration_json, resolved_configuration_hash,
                    origin_trace_json, change_summary, created_by, created_at,
                    validated_at, approved_at, activated_at, superseded_at,
                    effective_from_job_sequence, parent_revision_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(record[key] for key in [
                    "revision_id", "campaign_id", "revision_number", "status",
                    "configuration_json", "configuration_hash", "resolved_configuration_json",
                    "resolved_configuration_hash", "origin_trace_json", "change_summary",
                    "created_by", "created_at", "validated_at", "approved_at",
                    "activated_at", "superseded_at", "effective_from_job_sequence",
                    "parent_revision_id",
                ]),
            )
            connection.commit()
        return self.get_configuration_revision(str(record["revision_id"])) or record

    def update_configuration_revision(self, revision_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {
            "status", "configuration_json", "configuration_hash",
            "resolved_configuration_json", "resolved_configuration_hash",
            "origin_trace_json", "change_summary", "validated_at", "approved_at",
            "activated_at", "superseded_at", "effective_from_job_sequence",
            "parent_revision_id",
        }
        updates = {key: value for key, value in payload.items() if key in allowed}
        if not updates:
            return self.get_configuration_revision(revision_id)
        assignments = ", ".join(f"{key} = ?" for key in updates)
        with self.connection() as connection:
            connection.execute(
                f"UPDATE commercial_campaign_configuration_revisions SET {assignments} WHERE revision_id = ?",
                (*updates.values(), revision_id),
            )
            connection.commit()
        return self.get_configuration_revision(revision_id)

    def get_configuration_revision(self, revision_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            return row_to_dict(connection.execute(
                "SELECT * FROM commercial_campaign_configuration_revisions WHERE revision_id = ?",
                (revision_id,),
            ).fetchone())

    def list_configuration_revisions(self, campaign_id: str) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM commercial_campaign_configuration_revisions WHERE campaign_id = ? ORDER BY revision_number ASC",
                (campaign_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_latest_configuration_revision(self, campaign_id: str, statuses: set[str] | None = None) -> dict[str, Any] | None:
        rows = self.list_configuration_revisions(campaign_id)
        if statuses:
            rows = [row for row in rows if row.get("status") in statuses]
        return rows[-1] if rows else None

    def create_configuration_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        record = {
            "snapshot_id": payload.get("snapshot_id") or new_id("cfgsnap"),
            "campaign_id": payload["campaign_id"],
            "revision_id": payload["revision_id"],
            "approval_id": payload.get("approval_id"),
            "configuration_json": payload["configuration_json"],
            "configuration_hash": payload["configuration_hash"],
            "origin_trace_json": payload["origin_trace_json"],
            "created_at": payload.get("created_at") or now,
            "created_by": payload.get("created_by"),
            "locked_at": payload.get("locked_at") or now,
            "consumed_at": payload.get("consumed_at"),
        }
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO commercial_execution_configuration_snapshots (
                    snapshot_id, campaign_id, revision_id, approval_id,
                    configuration_json, configuration_hash, origin_trace_json,
                    created_at, created_by, locked_at, consumed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(record[key] for key in [
                    "snapshot_id", "campaign_id", "revision_id", "approval_id",
                    "configuration_json", "configuration_hash", "origin_trace_json",
                    "created_at", "created_by", "locked_at", "consumed_at",
                ]),
            )
            connection.commit()
        return self.get_configuration_snapshot(str(record["snapshot_id"])) or record

    def get_configuration_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            return row_to_dict(connection.execute(
                "SELECT * FROM commercial_execution_configuration_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone())

    def get_latest_configuration_snapshot(self, campaign_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            return row_to_dict(connection.execute(
                "SELECT * FROM commercial_execution_configuration_snapshots WHERE campaign_id = ? ORDER BY created_at DESC LIMIT 1",
                (campaign_id,),
            ).fetchone())

    def assign_snapshot_to_campaign_jobs(self, campaign_id: str, snapshot: dict[str, Any]) -> int:
        with self.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE commercial_delivery_jobs
                SET configuration_revision_id = ?,
                    execution_snapshot_id = ?,
                    configuration_snapshot_hash = ?,
                    updated_at = ?
                WHERE campaign_id = ? AND status IN ('queued','assigned')
                """,
                (
                    snapshot["revision_id"],
                    snapshot["snapshot_id"],
                    snapshot["configuration_hash"],
                    utc_now(),
                    campaign_id,
                ),
            )
            connection.commit()
            return int(cursor.rowcount or 0)

    def get_campaign_phone_map(self, campaign_id: str) -> dict[str, str]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT id, phone_normalized FROM commercial_recipients WHERE campaign_id = ?",
                (campaign_id,),
            ).fetchall()
            return {str(row["phone_normalized"]): str(row["id"]) for row in rows}

    def get_or_create_global_contact(self, normalized_phone: str) -> dict[str, Any]:
        now = utc_now()
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM commercial_global_contacts WHERE normalized_phone = ?",
                (normalized_phone,),
            ).fetchone()
            if row is not None:
                return dict(row)
            record = {
                "id": new_id("global_contact"),
                "normalized_phone": normalized_phone,
                "created_at": now,
                "updated_at": now,
            }
            try:
                connection.execute(
                    """
                    INSERT INTO commercial_global_contacts (id, normalized_phone, created_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (record["id"], record["normalized_phone"], record["created_at"], record["updated_at"]),
                )
                connection.commit()
            except sqlite3.IntegrityError:
                connection.rollback()
                row = connection.execute(
                    "SELECT * FROM commercial_global_contacts WHERE normalized_phone = ?",
                    (normalized_phone,),
                ).fetchone()
                if row is not None:
                    return dict(row)
                raise
            return self.get_global_contact(str(record["id"])) or record

    def get_global_contact_by_phone(self, normalized_phone: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            return row_to_dict(
                connection.execute(
                    "SELECT * FROM commercial_global_contacts WHERE normalized_phone = ?",
                    (normalized_phone,),
                ).fetchone()
            )

    def get_existing_global_contact_phones(self, normalized_phones: list[str]) -> set[str]:
        values = sorted({str(phone) for phone in normalized_phones if str(phone)})
        if not values:
            return set()
        found: set[str] = set()
        with self.connection() as connection:
            for index in range(0, len(values), 900):
                chunk = values[index:index + 900]
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"SELECT normalized_phone FROM commercial_global_contacts WHERE normalized_phone IN ({placeholders})",
                    tuple(chunk),
                ).fetchall()
                found.update(str(row["normalized_phone"]) for row in rows)
        return found

    def get_global_contact(self, global_contact_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            return row_to_dict(connection.execute("SELECT * FROM commercial_global_contacts WHERE id = ?", (global_contact_id,)).fetchone())

    def count_global_contacts(self) -> int:
        with self.connection() as connection:
            return int(connection.execute("SELECT COUNT(*) AS count FROM commercial_global_contacts").fetchone()["count"])

    def create_recipient_only(
        self,
        campaign: dict[str, Any],
        phone_raw: str,
        phone_normalized: str,
        display_name: str | None,
        import_source: str,
        manifest: dict[str, Any],
        input_sequence: int,
        authorization: dict[str, Any] | None = None,
        skip_existing_lookup: bool = False,
    ) -> dict[str, Any]:
        if not skip_existing_lookup:
            existing_id = self.get_campaign_phone_map(str(campaign["id"])).get(phone_normalized)
            if existing_id:
                return self.get_recipient(existing_id) or {"id": existing_id}
        now = utc_now()
        authorization = authorization or {}
        recipient = {
            "id": new_id("recipient"),
            "campaign_id": campaign["id"],
            "phone_raw": phone_raw,
            "phone_normalized": phone_normalized,
            "display_name": display_name,
            "import_source": import_source,
            "validation_status": "valid",
            "duplicate_of_recipient_id": None,
            "recipient_origin": authorization.get("recipient_origin") or "user_import",
            "synthetic_test_data": int(bool(authorization.get("synthetic_test_data", False))),
            "live_execution_authorized": int(bool(authorization.get("live_execution_authorized", False))),
            "live_authorized_at": authorization.get("live_authorized_at"),
            "live_authorized_by": authorization.get("live_authorized_by"),
            "authorization_source": authorization.get("authorization_source") or "manifest_materialization",
            "authorization_note": authorization.get("authorization_note") or "Materialized from confirmed recipient manifest",
            "authorization_status": authorization.get("authorization_status") or "authorization_required",
            "should_not_retry": int(bool(authorization.get("should_not_retry", False))),
            "input_manifest_id": manifest["manifest_id"],
            "input_manifest_hash": manifest["manifest_hash"],
            "input_sequence": input_sequence,
            "input_provenance_status": "confirmed_manifest",
            "contact_preparation_allowed": int(bool(authorization.get("contact_preparation_allowed", False))),
            "live_execution_blocked": int(bool(authorization.get("live_execution_blocked", False))),
            "block_reason": authorization.get("block_reason"),
            "created_at": now,
            "updated_at": now,
        }
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO commercial_recipients (
                    id, campaign_id, phone_raw, phone_normalized, display_name,
                    import_source, validation_status, duplicate_of_recipient_id,
                    recipient_origin, synthetic_test_data, live_execution_authorized,
                    live_authorized_at, live_authorized_by, authorization_source,
                    authorization_note, authorization_status, should_not_retry,
                    input_manifest_id, input_manifest_hash, input_sequence,
                    input_provenance_status, contact_preparation_allowed,
                    live_execution_blocked, block_reason, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(recipient[key] for key in [
                    "id", "campaign_id", "phone_raw", "phone_normalized", "display_name",
                    "import_source", "validation_status", "duplicate_of_recipient_id",
                    "recipient_origin", "synthetic_test_data", "live_execution_authorized",
                    "live_authorized_at", "live_authorized_by", "authorization_source",
                    "authorization_note", "authorization_status", "should_not_retry",
                    "input_manifest_id", "input_manifest_hash", "input_sequence",
                    "input_provenance_status", "contact_preparation_allowed",
                    "live_execution_blocked", "block_reason", "created_at", "updated_at",
                ]),
            )
            connection.commit()
        self.refresh_campaign_counts(str(campaign["id"]))
        return self.get_recipient(str(recipient["id"])) or recipient

    def create_recipient_and_job(self, campaign: dict[str, Any], phone_raw: str, phone_normalized: str, display_name: str | None, import_source: str) -> tuple[dict[str, Any], dict[str, Any]]:
        now = utc_now()
        recipient = {
            "id": new_id("recipient"),
            "campaign_id": campaign["id"],
            "phone_raw": phone_raw,
            "phone_normalized": phone_normalized,
            "display_name": display_name,
            "import_source": import_source,
            "validation_status": "valid",
            "duplicate_of_recipient_id": None,
            "created_at": now,
            "updated_at": now,
        }
        job = {
            "id": new_id("job"),
            "campaign_id": campaign["id"],
            "recipient_id": recipient["id"],
            "account_id": None,
            "source_channel_uid": campaign.get("source_channel_uid"),
            "display_name": display_name,
            "phone_normalized": phone_normalized,
            "idempotency_key": f"{campaign['id']}:{phone_normalized}",
            "status": "skipped",
            "priority": 100,
            "attempt_count": 0,
            "max_attempts": 1,
            "scheduled_at": None,
            "claimed_at": None,
            "started_at": None,
            "completed_at": None,
            "last_error_code": "recipient_input_manifest_required",
            "last_error_message": "Job cannot be queued without confirmed recipient input provenance",
            "result_success": None,
            "verified_forwarded_recipient_count": None,
            "forward_verified": None,
            "diagnostics_consistent": None,
            "manual_review_required": 1,
            "safe_to_requeue": 0,
            "should_not_retry": 1,
            "live_execution_blocked": 1,
            "block_reason": "recipient_input_manifest_required",
            "created_at": now,
            "updated_at": now,
        }
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO commercial_recipients (
                    id, campaign_id, phone_raw, phone_normalized, display_name,
                    import_source, validation_status, duplicate_of_recipient_id,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(recipient[key] for key in [
                    "id", "campaign_id", "phone_raw", "phone_normalized", "display_name",
                    "import_source", "validation_status", "duplicate_of_recipient_id", "created_at", "updated_at",
                ]),
            )
            connection.execute(
                """
                INSERT INTO commercial_delivery_jobs (
                    id, campaign_id, recipient_id, account_id, source_channel_uid,
                    display_name, phone_normalized, idempotency_key, status,
                    priority, attempt_count, max_attempts, scheduled_at, claimed_at,
                    started_at, completed_at, last_error_code, last_error_message,
                    result_success, verified_forwarded_recipient_count, forward_verified,
                    diagnostics_consistent, manual_review_required, safe_to_requeue,
                    should_not_retry, live_execution_blocked, block_reason,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(job[key] for key in [
                    "id", "campaign_id", "recipient_id", "account_id", "source_channel_uid",
                    "display_name", "phone_normalized", "idempotency_key", "status", "priority",
                    "attempt_count", "max_attempts", "scheduled_at", "claimed_at", "started_at",
                    "completed_at", "last_error_code", "last_error_message", "result_success",
                    "verified_forwarded_recipient_count", "forward_verified", "diagnostics_consistent",
                    "manual_review_required", "safe_to_requeue", "should_not_retry",
                    "live_execution_blocked", "block_reason",
                    "created_at", "updated_at",
                ]),
            )
            connection.commit()
        self.create_job_event(
            {
                "job_id": job["id"],
                "campaign_id": job["campaign_id"],
                "account_id": None,
                "recipient_id": recipient["id"],
                "event_type": "job_created",
                "step_name": None,
                "status": "skipped",
                "message": "Delivery job created blocked until confirmed recipient provenance is present",
                "error_code": "recipient_input_manifest_required",
            }
        )
        self.refresh_campaign_counts(campaign["id"])
        return recipient, job

    def list_recipients(self, campaign_id: str, validation_status: str | None, limit: int, offset: int) -> list[dict[str, Any]]:
        query = "SELECT * FROM commercial_recipients WHERE campaign_id = ?"
        params: list[Any] = [campaign_id]
        if validation_status:
            query += " AND validation_status = ?"
            params.append(validation_status)
        query += " ORDER BY created_at ASC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(query, params).fetchall()]

    def get_recipient(self, recipient_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            return row_to_dict(connection.execute("SELECT * FROM commercial_recipients WHERE id = ?", (recipient_id,)).fetchone())

    def get_campaign_recipient_run_by_phone(self, campaign_id: str, phone_normalized: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT id FROM commercial_campaign_recipient_runs
                WHERE campaign_id = ? AND phone_normalized = ?
                """,
                (campaign_id, phone_normalized),
            ).fetchone()
            if row is None:
                return None
        return self.get_campaign_recipient_run(str(row["id"]))

    def _create_platform_run_event(
        self,
        connection: sqlite3.Connection,
        *,
        platform_run: sqlite3.Row | dict[str, Any],
        previous_status: str | None,
        new_status: str,
        event_type: str,
        job_id: str | None = None,
        actor: str | None = None,
        source: str | None = None,
        reason: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO commercial_platform_run_events (
                id, campaign_id, campaign_recipient_run_id, platform_run_id,
                job_id, correlation_id, platform, previous_status, new_status,
                event_type, actor, source, reason, error_code, error_message,
                metadata_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("platform_event"),
                platform_run["campaign_id"],
                platform_run["campaign_recipient_run_id"],
                platform_run["id"],
                job_id,
                platform_run["correlation_id"],
                platform_run["platform"],
                previous_status,
                new_status,
                event_type,
                actor,
                source,
                reason,
                error_code,
                error_message,
                json.dumps(metadata or {}, ensure_ascii=False),
                utc_now(),
            ),
        )

    def create_campaign_recipient_run(
        self,
        campaign: dict[str, Any],
        recipient: dict[str, Any],
        platforms: list[str],
        global_contact_id: str | None = None,
        correlation_id: str | None = None,
        create_delivery_jobs: bool = False,
    ) -> dict[str, Any]:
        normalized_platforms: list[str] = []
        seen: set[str] = set()
        for platform in platforms:
            normalized = str(platform or "").strip().lower()
            if normalized and normalized not in seen:
                normalized_platforms.append(normalized)
                seen.add(normalized)
        if not normalized_platforms:
            raise ValueError("selected_platform_required")
        existing = self.get_campaign_recipient_run_by_phone(str(campaign["id"]), str(recipient["phone_normalized"]))
        if existing is not None:
            return existing
        now = utc_now()
        run_id = new_id("recipient_run")
        run_correlation_id = correlation_id or new_id("corr")
        contact_id = global_contact_id or f"global:{recipient['phone_normalized']}"
        run = {
            "id": run_id,
            "campaign_id": campaign["id"],
            "recipient_id": recipient["id"],
            "global_contact_id": contact_id,
            "phone_normalized": recipient["phone_normalized"],
            "correlation_id": run_correlation_id,
            "scenario_status": "pending",
            "selected_platform_count": len(normalized_platforms),
            "checked_platform_count": 0,
            "sent_platform_count": 0,
            "account_not_found_count": 0,
            "failed_platform_count": 0,
            "retryable_platform_count": 0,
            "pending_platform_count": len(normalized_platforms),
            "created_at": now,
            "started_at": None,
            "completed_at": None,
            "updated_at": now,
        }
        platform_runs: list[dict[str, Any]] = []
        with self.connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO commercial_campaign_recipient_runs (
                        id, campaign_id, recipient_id, global_contact_id, phone_normalized,
                        correlation_id, scenario_status, selected_platform_count,
                        checked_platform_count, sent_platform_count, account_not_found_count,
                        failed_platform_count, retryable_platform_count, pending_platform_count, created_at,
                        started_at, completed_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    tuple(run[key] for key in [
                        "id", "campaign_id", "recipient_id", "global_contact_id", "phone_normalized",
                        "correlation_id", "scenario_status", "selected_platform_count",
                        "checked_platform_count", "sent_platform_count", "account_not_found_count",
                        "failed_platform_count", "retryable_platform_count", "pending_platform_count", "created_at",
                        "started_at", "completed_at", "updated_at",
                    ]),
                )
            except sqlite3.IntegrityError:
                connection.rollback()
                existing_after_race = self.get_campaign_recipient_run_by_phone(str(campaign["id"]), str(recipient["phone_normalized"]))
                if existing_after_race is not None:
                    return existing_after_race
                raise
            for platform in normalized_platforms:
                platform_run = {
                    "id": new_id("platform_run"),
                    "campaign_id": campaign["id"],
                    "campaign_recipient_run_id": run_id,
                    "recipient_id": recipient["id"],
                    "global_contact_id": contact_id,
                    "correlation_id": run_correlation_id,
                    "platform": platform,
                    "delivery_job_id": None,
                    "outcome": "queued" if create_delivery_jobs else "pending",
                    "attempt_count": 0,
                    "stable_display_name": None,
                    "last_error_code": None,
                    "last_error_message": None,
                    "started_at": None,
                    "completed_at": None,
                    "created_at": now,
                    "updated_at": now,
                }
                if create_delivery_jobs:
                    job_id = new_id("job")
                    platform_run["delivery_job_id"] = job_id
                    connection.execute(
                        """
                        INSERT INTO commercial_delivery_jobs (
                            id, campaign_id, recipient_id, account_id, source_channel_uid,
                            display_name, phone_normalized, idempotency_key, status,
                            priority, attempt_count, max_attempts, scheduled_at, claimed_at,
                            started_at, completed_at, last_error_code, last_error_message,
                            result_success, verified_forwarded_recipient_count, forward_verified,
                            diagnostics_consistent, campaign_recipient_run_id, platform_run_id,
                            platform, global_contact_id, correlation_id, recipient_origin,
                            synthetic_test_data, live_execution_authorized, live_authorized_at,
                            live_authorized_by, authorization_source, authorization_note,
                            authorization_status, should_not_retry, input_manifest_id,
                            input_manifest_hash, input_sequence, input_provenance_status,
                            live_execution_blocked, block_reason, created_at, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            job_id,
                            campaign["id"],
                            recipient["id"],
                            None,
                            campaign.get("source_channel_uid"),
                            recipient.get("display_name"),
                            recipient["phone_normalized"],
                            f"{campaign['id']}:{recipient['id']}:{platform}:attempt-1",
                            "queued",
                            100,
                            0,
                            1,
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            run_id,
                            platform_run["id"],
                            platform,
                            contact_id,
                            run_correlation_id,
                            recipient.get("recipient_origin"),
                            recipient.get("synthetic_test_data"),
                            recipient.get("live_execution_authorized"),
                            recipient.get("live_authorized_at"),
                            recipient.get("live_authorized_by"),
                            recipient.get("authorization_source"),
                            recipient.get("authorization_note"),
                            recipient.get("authorization_status"),
                            recipient.get("should_not_retry"),
                            recipient.get("input_manifest_id"),
                            recipient.get("input_manifest_hash"),
                            recipient.get("input_sequence"),
                            recipient.get("input_provenance_status"),
                            recipient.get("live_execution_blocked"),
                            recipient.get("block_reason"),
                            now,
                            now,
                        ),
                    )
                connection.execute(
                    """
                    INSERT INTO commercial_platform_runs (
                        id, campaign_id, campaign_recipient_run_id, recipient_id,
                        global_contact_id, correlation_id, platform, delivery_job_id,
                        outcome, attempt_count, stable_display_name, last_error_code,
                        last_error_message, started_at, completed_at, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    tuple(platform_run[key] for key in [
                        "id", "campaign_id", "campaign_recipient_run_id", "recipient_id",
                        "global_contact_id", "correlation_id", "platform", "delivery_job_id",
                        "outcome", "attempt_count", "stable_display_name", "last_error_code",
                        "last_error_message", "started_at", "completed_at", "created_at", "updated_at",
                    ]),
                )
                self._create_platform_run_event(
                    connection,
                    platform_run=platform_run,
                    previous_status=None,
                    new_status=str(platform_run["outcome"]),
                    event_type="platform_run_created",
                    job_id=platform_run.get("delivery_job_id"),
                    actor="system",
                    source="commercial_queue",
                    reason="recipient_scenario_started",
                )
                platform_runs.append(platform_run)
            connection.commit()
        self.refresh_campaign_counts(str(campaign["id"]))
        return {**run, "platform_runs": platform_runs}

    def get_campaign_recipient_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            run = row_to_dict(connection.execute("SELECT * FROM commercial_campaign_recipient_runs WHERE id = ?", (run_id,)).fetchone())
            if run is None:
                return None
            rows = connection.execute(
                "SELECT * FROM commercial_platform_runs WHERE campaign_recipient_run_id = ? ORDER BY platform ASC",
                (run_id,),
            ).fetchall()
            run["platform_runs"] = [dict(row) for row in rows]
            job_rows = connection.execute(
                """
                SELECT * FROM commercial_delivery_jobs
                WHERE campaign_recipient_run_id = ?
                ORDER BY created_at ASC
                """,
                (run_id,),
            ).fetchall()
            run["job_attempts"] = [dict(row) for row in job_rows]
            event_rows = connection.execute(
                """
                SELECT * FROM commercial_platform_run_events
                WHERE campaign_recipient_run_id = ?
                ORDER BY created_at ASC
                """,
                (run_id,),
            ).fetchall()
            run["platform_events"] = [dict(row) for row in event_rows]
            return run

    def _refresh_campaign_recipient_run_aggregate(self, connection: sqlite3.Connection, run_id: str) -> dict[str, Any]:
        rows = connection.execute(
            "SELECT outcome FROM commercial_platform_runs WHERE campaign_recipient_run_id = ?",
            (run_id,),
        ).fetchall()
        outcomes = [str(row["outcome"]) for row in rows]
        counts = aggregate_platform_counts(outcomes)
        scenario_status = aggregate_scenario_status(outcomes)
        now = utc_now()
        completed_at = now if scenario_status in {"completed", "cancelled"} else None
        connection.execute(
            """
            UPDATE commercial_campaign_recipient_runs SET
                scenario_status = ?,
                selected_platform_count = ?,
                checked_platform_count = ?,
                sent_platform_count = ?,
                account_not_found_count = ?,
                failed_platform_count = ?,
                retryable_platform_count = ?,
                pending_platform_count = ?,
                completed_at = CASE WHEN ? IS NOT NULL THEN COALESCE(completed_at, ?) ELSE NULL END,
                updated_at = ?
            WHERE id = ?
            """,
            (
                scenario_status,
                counts["selected_platform_count"],
                counts["checked_platform_count"],
                counts["sent_platform_count"],
                counts["account_not_found_count"],
                counts["failed_platform_count"],
                counts["retryable_platform_count"],
                counts["pending_platform_count"],
                completed_at,
                completed_at,
                now,
                run_id,
            ),
        )
        return {
            "scenario_status": scenario_status,
            **counts,
        }

    def update_platform_run_outcome(self, platform_run_id: str, outcome: str, payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
        if outcome not in PLATFORM_RUN_OUTCOMES:
            raise ValueError("unsupported_platform_outcome")
        payload = payload or {}
        now = utc_now()
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM commercial_platform_runs WHERE id = ?", (platform_run_id,)).fetchone()
            if row is None:
                return None
            previous_outcome = str(row["outcome"])
            if previous_outcome in TERMINAL_PLATFORM_OUTCOMES and previous_outcome != outcome:
                raise ValueError("terminal_platform_outcome_immutable")
            run_id = str(row["campaign_recipient_run_id"])
            started_at = payload.get("started_at") or (now if outcome == "in_progress" else row["started_at"])
            completed_at = payload.get("completed_at") or (now if outcome in TERMINAL_PLATFORM_OUTCOMES or outcome == "failed_retryable" else None)
            stable_display_name = payload.get("stable_display_name") if "stable_display_name" in payload else row["stable_display_name"]
            connection.execute(
                """
                UPDATE commercial_platform_runs SET
                    outcome = ?,
                    attempt_count = CASE WHEN ? = 'in_progress' THEN attempt_count + 1 ELSE attempt_count END,
                    stable_display_name = ?,
                    last_error_code = ?,
                    last_error_message = ?,
                    started_at = COALESCE(started_at, ?),
                    completed_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    outcome,
                    outcome,
                    stable_display_name,
                    payload.get("last_error_code"),
                    payload.get("last_error_message"),
                    started_at,
                    completed_at,
                    now,
                    platform_run_id,
                ),
            )
            updated_row = connection.execute("SELECT * FROM commercial_platform_runs WHERE id = ?", (platform_run_id,)).fetchone()
            if updated_row is not None:
                self._create_platform_run_event(
                    connection,
                    platform_run=updated_row,
                    previous_status=previous_outcome,
                    new_status=outcome,
                    event_type="platform_outcome_updated",
                    job_id=updated_row["delivery_job_id"],
                    actor=payload.get("actor"),
                    source=payload.get("source") or "internal_service",
                    reason=payload.get("reason"),
                    error_code=payload.get("last_error_code"),
                    error_message=payload.get("last_error_message"),
                    metadata={key: value for key, value in payload.items() if key not in {"actor", "source", "reason"}},
                )
            self._refresh_campaign_recipient_run_aggregate(connection, run_id)
            connection.commit()
        return self.get_platform_run(platform_run_id)

    def get_platform_run(self, platform_run_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            return row_to_dict(connection.execute("SELECT * FROM commercial_platform_runs WHERE id = ?", (platform_run_id,)).fetchone())

    def bulk_materialize_recipient_runs(
        self,
        campaign: dict[str, Any],
        phones: list[str],
        platforms: list[str],
        manifest: dict[str, Any],
        authorization: dict[str, Any],
    ) -> dict[str, Any]:
        now = utc_now()
        normalized_platforms = []
        seen_platforms: set[str] = set()
        for platform in platforms:
            value = str(platform or "").strip().lower()
            if value and value not in seen_platforms:
                normalized_platforms.append(value)
                seen_platforms.add(value)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_contacts = {
                str(row["normalized_phone"]): dict(row)
                for row in connection.execute("SELECT * FROM commercial_global_contacts").fetchall()
            }
            existing_recipients = {
                str(row["phone_normalized"]): dict(row)
                for row in connection.execute("SELECT * FROM commercial_recipients WHERE campaign_id = ?", (campaign["id"],)).fetchall()
            }
            existing_runs = {
                str(row["phone_normalized"]): dict(row)
                for row in connection.execute("SELECT * FROM commercial_campaign_recipient_runs WHERE campaign_id = ?", (campaign["id"],)).fetchall()
            }
            created_recipients = 0
            scenario_count = 0
            platform_run_count = 0
            for sequence, phone in enumerate(phones, start=1):
                phone_text = str(phone)
                global_contact = existing_contacts.get(phone_text)
                if global_contact is None:
                    global_contact = {
                        "id": new_id("global_contact"),
                        "normalized_phone": phone_text,
                        "created_at": now,
                        "updated_at": now,
                    }
                    connection.execute(
                        "INSERT INTO commercial_global_contacts (id, normalized_phone, created_at, updated_at) VALUES (?, ?, ?, ?)",
                        (global_contact["id"], phone_text, now, now),
                    )
                    existing_contacts[phone_text] = global_contact
                recipient = existing_recipients.get(phone_text)
                if recipient is None:
                    recipient = {
                        "id": new_id("recipient"),
                        "campaign_id": campaign["id"],
                        "phone_raw": phone_text,
                        "phone_normalized": phone_text,
                        "display_name": None,
                        "import_source": manifest.get("source_type") or "manual",
                        "validation_status": "valid",
                        "duplicate_of_recipient_id": None,
                        "recipient_origin": authorization.get("recipient_origin") or "user_import",
                        "synthetic_test_data": int(bool(authorization.get("synthetic_test_data", False))),
                        "live_execution_authorized": int(bool(authorization.get("live_execution_authorized", False))),
                        "live_authorized_at": authorization.get("live_authorized_at"),
                        "live_authorized_by": authorization.get("live_authorized_by"),
                        "authorization_source": authorization.get("authorization_source") or "manifest_materialization",
                        "authorization_note": authorization.get("authorization_note") or "Materialized from confirmed recipient manifest",
                        "authorization_status": authorization.get("authorization_status") or "authorization_required",
                        "should_not_retry": int(bool(authorization.get("should_not_retry", False))),
                        "input_manifest_id": manifest["manifest_id"],
                        "input_manifest_hash": manifest["manifest_hash"],
                        "input_sequence": sequence,
                        "input_provenance_status": "confirmed_manifest",
                        "contact_preparation_allowed": int(bool(authorization.get("contact_preparation_allowed", False))),
                        "live_execution_blocked": int(bool(authorization.get("live_execution_blocked", False))),
                        "block_reason": authorization.get("block_reason"),
                        "created_at": now,
                        "updated_at": now,
                    }
                    connection.execute(
                        """
                        INSERT INTO commercial_recipients (
                            id, campaign_id, phone_raw, phone_normalized, display_name,
                            import_source, validation_status, duplicate_of_recipient_id,
                            recipient_origin, synthetic_test_data, live_execution_authorized,
                            live_authorized_at, live_authorized_by, authorization_source,
                            authorization_note, authorization_status, should_not_retry,
                            input_manifest_id, input_manifest_hash, input_sequence,
                            input_provenance_status, contact_preparation_allowed,
                            live_execution_blocked, block_reason, created_at, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        tuple(recipient[key] for key in [
                            "id", "campaign_id", "phone_raw", "phone_normalized", "display_name",
                            "import_source", "validation_status", "duplicate_of_recipient_id",
                            "recipient_origin", "synthetic_test_data", "live_execution_authorized",
                            "live_authorized_at", "live_authorized_by", "authorization_source",
                            "authorization_note", "authorization_status", "should_not_retry",
                            "input_manifest_id", "input_manifest_hash", "input_sequence",
                            "input_provenance_status", "contact_preparation_allowed",
                            "live_execution_blocked", "block_reason", "created_at", "updated_at",
                        ]),
                    )
                    existing_recipients[phone_text] = recipient
                    created_recipients += 1
                run = existing_runs.get(phone_text)
                if run is None:
                    run = {
                        "id": new_id("recipient_run"),
                        "campaign_id": campaign["id"],
                        "recipient_id": recipient["id"],
                        "global_contact_id": global_contact["id"],
                        "phone_normalized": phone_text,
                        "correlation_id": new_id("corr"),
                        "scenario_status": "pending",
                        "selected_platform_count": len(normalized_platforms),
                        "checked_platform_count": 0,
                        "sent_platform_count": 0,
                        "account_not_found_count": 0,
                        "failed_platform_count": 0,
                        "retryable_platform_count": 0,
                        "pending_platform_count": len(normalized_platforms),
                        "created_at": now,
                        "started_at": None,
                        "completed_at": None,
                        "updated_at": now,
                    }
                    connection.execute(
                        """
                        INSERT INTO commercial_campaign_recipient_runs (
                            id, campaign_id, recipient_id, global_contact_id, phone_normalized,
                            correlation_id, scenario_status, selected_platform_count,
                            checked_platform_count, sent_platform_count, account_not_found_count,
                            failed_platform_count, retryable_platform_count, pending_platform_count,
                            created_at, started_at, completed_at, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        tuple(run[key] for key in [
                            "id", "campaign_id", "recipient_id", "global_contact_id", "phone_normalized",
                            "correlation_id", "scenario_status", "selected_platform_count",
                            "checked_platform_count", "sent_platform_count", "account_not_found_count",
                            "failed_platform_count", "retryable_platform_count", "pending_platform_count",
                            "created_at", "started_at", "completed_at", "updated_at",
                        ]),
                    )
                    existing_runs[phone_text] = run
                    scenario_count += 1
                existing_platforms = {
                    str(row["platform"])
                    for row in connection.execute(
                        "SELECT platform FROM commercial_platform_runs WHERE campaign_recipient_run_id = ?",
                        (run["id"],),
                    ).fetchall()
                }
                for platform in normalized_platforms:
                    if platform in existing_platforms:
                        continue
                    platform_run = {
                        "id": new_id("platform_run"),
                        "campaign_id": campaign["id"],
                        "campaign_recipient_run_id": run["id"],
                        "recipient_id": recipient["id"],
                        "global_contact_id": global_contact["id"],
                        "correlation_id": run["correlation_id"],
                        "platform": platform,
                        "delivery_job_id": None,
                        "outcome": "pending",
                        "attempt_count": 0,
                        "stable_display_name": None,
                        "last_error_code": None,
                        "last_error_message": None,
                        "started_at": None,
                        "completed_at": None,
                        "created_at": now,
                        "updated_at": now,
                    }
                    connection.execute(
                        """
                        INSERT INTO commercial_platform_runs (
                            id, campaign_id, campaign_recipient_run_id, recipient_id,
                            global_contact_id, correlation_id, platform, delivery_job_id,
                            outcome, attempt_count, stable_display_name, last_error_code,
                            last_error_message, started_at, completed_at, created_at, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        tuple(platform_run[key] for key in [
                            "id", "campaign_id", "campaign_recipient_run_id", "recipient_id",
                            "global_contact_id", "correlation_id", "platform", "delivery_job_id",
                            "outcome", "attempt_count", "stable_display_name", "last_error_code",
                            "last_error_message", "started_at", "completed_at", "created_at", "updated_at",
                        ]),
                    )
                    self._create_platform_run_event(
                        connection,
                        platform_run=platform_run,
                        previous_status=None,
                        new_status="pending",
                        event_type="platform_run_created",
                        actor="system",
                        source="commercial_queue",
                        reason="recipient_scenario_started",
                    )
                    platform_run_count += 1
            connection.commit()
        self.refresh_campaign_counts(str(campaign["id"]))
        return {
            "created_recipient_count": created_recipients,
            "scenario_count": len(phones),
            "created_scenario_count": scenario_count,
            "platform_run_count": platform_run_count,
        }

    def retry_retryable_platform_runs(self, run_id: str) -> dict[str, Any]:
        now = utc_now()
        with self.connection() as connection:
            rows = [dict(row) for row in connection.execute(
                "SELECT * FROM commercial_platform_runs WHERE campaign_recipient_run_id = ? AND outcome = 'failed_retryable'",
                (run_id,),
            ).fetchall()]
            for row in rows:
                new_job_id = None
                previous_outcome = str(row["outcome"])
                if row.get("delivery_job_id"):
                    old_job = connection.execute(
                        "SELECT * FROM commercial_delivery_jobs WHERE id = ?",
                        (row["delivery_job_id"],),
                    ).fetchone()
                    if old_job is not None:
                        new_job_id = new_id("job")
                        retry_number = int(row.get("attempt_count") or 0) + 1
                        connection.execute(
                            """
                            INSERT INTO commercial_delivery_jobs (
                                id, campaign_id, recipient_id, account_id, source_channel_uid,
                                display_name, phone_normalized, idempotency_key, status,
                                priority, attempt_count, max_attempts, scheduled_at, claimed_at,
                                started_at, completed_at, last_error_code, last_error_message,
                                result_success, verified_forwarded_recipient_count, forward_verified,
                                diagnostics_consistent, campaign_recipient_run_id, platform_run_id,
                                platform, global_contact_id, correlation_id, recipient_origin,
                                synthetic_test_data, live_execution_authorized, live_authorized_at,
                                live_authorized_by, authorization_source, authorization_note,
                                authorization_status, should_not_retry, input_manifest_id,
                                input_manifest_hash, input_sequence, input_provenance_status,
                                live_execution_blocked, block_reason, created_at, updated_at
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                new_job_id,
                                old_job["campaign_id"],
                                old_job["recipient_id"],
                                None,
                                old_job["source_channel_uid"],
                                old_job["display_name"],
                                old_job["phone_normalized"],
                                f"{old_job['campaign_id']}:{old_job['recipient_id']}:{row['platform']}:attempt-{retry_number + 1}",
                                "queued",
                                old_job["priority"],
                                0,
                                old_job["max_attempts"],
                                old_job["scheduled_at"],
                                None,
                                None,
                                None,
                                None,
                                None,
                                None,
                                None,
                                None,
                                None,
                                row["campaign_recipient_run_id"],
                                row["id"],
                                row["platform"],
                                row["global_contact_id"],
                                row["correlation_id"],
                                old_job["recipient_origin"],
                                old_job["synthetic_test_data"],
                                old_job["live_execution_authorized"],
                                old_job["live_authorized_at"],
                                old_job["live_authorized_by"],
                                old_job["authorization_source"],
                                old_job["authorization_note"],
                                old_job["authorization_status"],
                                old_job["should_not_retry"],
                                old_job["input_manifest_id"],
                                old_job["input_manifest_hash"],
                                old_job["input_sequence"],
                                old_job["input_provenance_status"],
                                old_job["live_execution_blocked"],
                                old_job["block_reason"],
                                now,
                                now,
                            ),
                        )
                connection.execute(
                    """
                    UPDATE commercial_platform_runs SET
                        outcome = ?,
                        delivery_job_id = COALESCE(?, delivery_job_id),
                        last_error_code = NULL,
                        last_error_message = NULL,
                        started_at = NULL,
                        completed_at = NULL,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    ("queued" if new_job_id else "pending", new_job_id, now, row["id"]),
                )
                updated_row = connection.execute("SELECT * FROM commercial_platform_runs WHERE id = ?", (row["id"],)).fetchone()
                if updated_row is not None:
                    self._create_platform_run_event(
                        connection,
                        platform_run=updated_row,
                        previous_status=previous_outcome,
                        new_status=str(updated_row["outcome"]),
                        event_type="platform_retry_requeued",
                        job_id=new_job_id,
                        actor="system",
                        source="commercial_queue",
                        reason="retry_failed_retryable_platform_only",
                    )
            aggregate = self._refresh_campaign_recipient_run_aggregate(connection, run_id)
            connection.commit()
        if rows:
            self.refresh_campaign_counts(str(rows[0]["campaign_id"]))
        return {
            "campaign_recipient_run_id": run_id,
            "requeued_platform_run_count": len(rows),
            "requeued_platforms": [str(row["platform"]) for row in rows],
            "scenario": self.get_campaign_recipient_run(run_id),
            "aggregate": aggregate,
        }

    def list_campaign_recipient_report_rows(self, campaign_id: str, limit: int, offset: int) -> list[dict[str, Any]]:
        with self.connection() as connection:
            runs = [dict(row) for row in connection.execute(
                """
                SELECT * FROM commercial_campaign_recipient_runs
                WHERE campaign_id = ?
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                (campaign_id, limit, offset),
            ).fetchall()]
            if not runs:
                return []
            run_ids = [str(run["id"]) for run in runs]
            placeholders = ",".join("?" for _ in run_ids)
            platform_rows = [dict(row) for row in connection.execute(
                f"SELECT * FROM commercial_platform_runs WHERE campaign_recipient_run_id IN ({placeholders})",
                tuple(run_ids),
            ).fetchall()]
        by_run: dict[str, list[dict[str, Any]]] = {}
        for row in platform_rows:
            by_run.setdefault(str(row["campaign_recipient_run_id"]), []).append(row)
        report: list[dict[str, Any]] = []
        for run in runs:
            platform_outcomes = {str(row["platform"]): str(row["outcome"]) for row in by_run.get(str(run["id"]), [])}
            active_pending_count = sum(1 for outcome in platform_outcomes.values() if outcome in {"pending", "queued", "assigned", "in_progress"})
            report.append(
                {
                    "campaign_recipient_run_id": run["id"],
                    "recipient_id": run["recipient_id"],
                    "global_contact_id": run["global_contact_id"],
                    "correlation_id": run["correlation_id"],
                    "phone": run["phone_normalized"],
                    "scenario_status": run["scenario_status"],
                    "selected_platforms": sorted(platform_outcomes),
                    "platform_outcomes": platform_outcomes,
                    "bale_outcome": platform_outcomes.get("bale"),
                    "telegram_outcome": platform_outcomes.get("telegram"),
                    "whatsapp_outcome": platform_outcomes.get("whatsapp"),
                    "sent_count": int(run["sent_platform_count"]),
                    "not_found_count": int(run["account_not_found_count"]),
                    "failed_count": int(run["failed_platform_count"]),
                    "retry_pending_count": int(run["retryable_platform_count"]),
                    "pending_platform_count": int(run.get("pending_platform_count") or active_pending_count),
                    "active_pending_count": active_pending_count,
                    "last_updated_at": run["updated_at"],
                }
            )
        return report

    def find_recipient_by_phone(self, phone_normalized: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            return row_to_dict(
                connection.execute(
                    """
                    SELECT * FROM commercial_recipients
                    WHERE phone_normalized = ?
                    ORDER BY
                        CASE WHEN recipient_origin = 'user_provided' THEN 0 ELSE 1 END,
                        created_at ASC
                    LIMIT 1
                    """,
                    (phone_normalized,),
                ).fetchone()
            )

    def create_contact_maintenance_recipient(self, campaign_id: str, phone_raw: str, phone_normalized: str, display_name: str | None, payload: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        record = {
            "id": new_id("recipient"),
            "campaign_id": campaign_id,
            "phone_raw": phone_raw,
            "phone_normalized": phone_normalized,
            "display_name": display_name,
            "import_source": "contact_maintenance",
            "validation_status": "valid",
            "duplicate_of_recipient_id": None,
            "recipient_origin": payload.get("recipient_origin") or "user_provided",
            "synthetic_test_data": int(bool(payload.get("synthetic_test_data", False))),
            "live_execution_authorized": int(bool(payload.get("live_execution_authorized", True))),
            "live_authorized_at": payload.get("live_authorized_at") or now,
            "live_authorized_by": payload.get("live_authorized_by") or "user",
            "authorization_source": payload.get("authorization_source") or "explicit_user_confirmation",
            "authorization_note": payload.get("authorization_note") or "",
            "authorization_status": payload.get("authorization_status") or "authorized",
            "should_not_retry": int(bool(payload.get("should_not_retry", False))),
            "test_data_origin": payload.get("test_data_origin"),
            "stable_display_name": payload.get("stable_display_name") or display_name,
            "bale_contact_preexisting": int(bool(payload.get("bale_contact_preexisting", False))),
            "bale_contact_verified": int(bool(payload.get("bale_contact_verified", False))),
            "bale_contact_created": int(bool(payload.get("bale_contact_created", False))),
            "bale_verified_at": payload.get("bale_verified_at"),
            "bale_verification_status": payload.get("bale_verification_status"),
            "bale_verification_error": payload.get("bale_verification_error"),
            "last_verified_account_id": payload.get("last_verified_account_id"),
            "contact_creation_expected": int(bool(payload.get("contact_creation_expected", False))),
            "contact_creation_attempted": int(bool(payload.get("contact_creation_attempted", False))),
            "created_at": now,
            "updated_at": now,
        }
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO commercial_recipients (
                    id, campaign_id, phone_raw, phone_normalized, display_name,
                    import_source, validation_status, duplicate_of_recipient_id,
                    recipient_origin, synthetic_test_data, live_execution_authorized,
                    live_authorized_at, live_authorized_by, authorization_source,
                    authorization_note, authorization_status, should_not_retry,
                    test_data_origin, stable_display_name, bale_contact_preexisting,
                    bale_contact_verified, bale_contact_created, bale_verified_at, bale_verification_status,
                    bale_verification_error, last_verified_account_id,
                    contact_creation_expected, contact_creation_attempted,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(record[key] for key in [
                    "id", "campaign_id", "phone_raw", "phone_normalized", "display_name",
                    "import_source", "validation_status", "duplicate_of_recipient_id",
                    "recipient_origin", "synthetic_test_data", "live_execution_authorized",
                    "live_authorized_at", "live_authorized_by", "authorization_source",
                    "authorization_note", "authorization_status", "should_not_retry",
                    "test_data_origin", "stable_display_name", "bale_contact_preexisting",
                    "bale_contact_verified", "bale_contact_created", "bale_verified_at", "bale_verification_status",
                    "bale_verification_error", "last_verified_account_id",
                    "contact_creation_expected", "contact_creation_attempted",
                    "created_at", "updated_at",
                ]),
            )
            connection.commit()
        return self.get_recipient(str(record["id"])) or record

    def update_recipient_authorization(self, recipient_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {
            "recipient_origin", "synthetic_test_data", "live_execution_authorized",
            "live_authorized_at", "live_authorized_by", "authorization_source",
            "authorization_note", "authorization_status", "should_not_retry",
            "test_data_origin", "bale_contact_preexisting", "bale_contact_verified",
            "bale_contact_created",
            "stable_display_name", "bale_verified_at", "bale_verification_status",
            "bale_verification_error", "last_verified_account_id",
            "contact_creation_expected", "contact_creation_attempted",
            "input_manifest_id", "input_manifest_hash", "input_sequence",
            "input_provenance_status", "contact_preparation_allowed",
            "live_execution_blocked", "block_reason",
        }
        payload = {key: value for key, value in updates.items() if key in allowed}
        if not payload:
            return self.get_recipient(recipient_id)
        for key in {
            "synthetic_test_data", "live_execution_authorized", "should_not_retry",
            "bale_contact_preexisting", "bale_contact_verified",
            "bale_contact_created", "contact_preparation_allowed",
            "live_execution_blocked",
            "contact_creation_expected", "contact_creation_attempted",
        }:
            if key in payload and payload[key] is not None:
                payload[key] = int(bool(payload[key]))
        payload["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in payload)
        with self.connection() as connection:
            connection.execute(f"UPDATE commercial_recipients SET {assignments} WHERE id = ?", (*payload.values(), recipient_id))
            self._promote_jobs_for_recipient_if_eligible(connection, recipient_id)
            connection.commit()
        return self.get_recipient(recipient_id)

    def update_job_authorization_metadata(self, job_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {
            "recipient_origin", "synthetic_test_data", "live_execution_authorized",
            "live_authorized_at", "live_authorized_by", "authorization_source",
            "authorization_note", "authorization_status", "should_not_retry",
            "live_authorization_missing", "historical_normal_mode_attempt",
            "historical_normal_mode_confirmed", "configuration_revision_id",
            "execution_snapshot_id", "configuration_snapshot_hash",
            "input_manifest_id", "input_manifest_hash", "input_sequence",
            "input_provenance_status", "live_execution_blocked", "block_reason",
        }
        payload = {key: value for key, value in updates.items() if key in allowed}
        if not payload:
            return self.get_job(job_id)
        for key in {
            "synthetic_test_data", "live_execution_authorized", "should_not_retry",
            "live_authorization_missing", "historical_normal_mode_attempt",
            "historical_normal_mode_confirmed", "live_execution_blocked",
        }:
            if key in payload and payload[key] is not None:
                payload[key] = int(bool(payload[key]))
        payload["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in payload)
        with self.connection() as connection:
            connection.execute(f"UPDATE commercial_delivery_jobs SET {assignments} WHERE id = ?", (*payload.values(), job_id))
            self._promote_job_if_eligible(connection, job_id)
            connection.commit()
        return self.get_job(job_id)

    def update_jobs_authorization_by_recipient(self, recipient_id: str, updates: dict[str, Any]) -> int:
        allowed = {
            "recipient_origin", "synthetic_test_data", "live_execution_authorized",
            "live_authorized_at", "live_authorized_by", "authorization_source",
            "authorization_note", "authorization_status", "should_not_retry",
            "live_authorization_missing", "historical_normal_mode_attempt",
            "historical_normal_mode_confirmed", "configuration_revision_id",
            "execution_snapshot_id", "configuration_snapshot_hash",
            "input_manifest_id", "input_manifest_hash", "input_sequence",
            "input_provenance_status", "live_execution_blocked", "block_reason",
        }
        payload = {key: value for key, value in updates.items() if key in allowed}
        if not payload:
            return 0
        for key in {
            "synthetic_test_data", "live_execution_authorized", "should_not_retry",
            "live_authorization_missing", "historical_normal_mode_attempt",
            "historical_normal_mode_confirmed", "live_execution_blocked",
        }:
            if key in payload and payload[key] is not None:
                payload[key] = int(bool(payload[key]))
        payload["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in payload)
        with self.connection() as connection:
            cursor = connection.execute(f"UPDATE commercial_delivery_jobs SET {assignments} WHERE recipient_id = ?", (*payload.values(), recipient_id))
            self._promote_jobs_for_recipient_if_eligible(connection, recipient_id)
            connection.commit()
            return int(cursor.rowcount or 0)

    def _promote_job_if_eligible(self, connection: sqlite3.Connection, job_id: str) -> None:
        connection.execute(
            """
            UPDATE commercial_delivery_jobs AS job SET
                status = 'queued',
                last_error_code = NULL,
                last_error_message = NULL,
                manual_review_required = 0,
                safe_to_requeue = NULL,
                should_not_retry = 0,
                live_execution_blocked = 0,
                block_reason = NULL,
                completed_at = NULL,
                updated_at = ?
            WHERE job.id = ?
              AND job.status = 'skipped'
              AND EXISTS (
                SELECT 1
                FROM commercial_recipients AS recipient
                JOIN commercial_recipient_input_manifests AS manifest ON manifest.manifest_id = recipient.input_manifest_id
                WHERE recipient.id = job.recipient_id
                  AND recipient.input_manifest_id IS NOT NULL
                  AND recipient.input_manifest_hash IS NOT NULL
                  AND recipient.input_provenance_status = 'confirmed_manifest'
                  AND manifest.campaign_id = recipient.campaign_id
                  AND manifest.confirmation_status = 'confirmed'
                  AND manifest.manifest_hash = recipient.input_manifest_hash
                  AND manifest.normalized_phones_json LIKE ('%' || '"' || recipient.phone_normalized || '"' || '%')
                  AND recipient.recipient_origin IN ('user_provided', 'user_import', 'manual_import')
                  AND COALESCE(recipient.live_execution_authorized, 0) = 1
                  AND recipient.authorization_status = 'authorized'
                  AND COALESCE(recipient.live_execution_blocked, 0) = 0
                  AND COALESCE(recipient.should_not_retry, 0) = 0
                  AND COALESCE(recipient.synthetic_test_data, 0) = 0
                  AND job.attempt_count < job.max_attempts
              )
            """,
            (utc_now(), job_id),
        )

    def _promote_jobs_for_recipient_if_eligible(self, connection: sqlite3.Connection, recipient_id: str) -> None:
        rows = connection.execute("SELECT id FROM commercial_delivery_jobs WHERE recipient_id = ?", (recipient_id,)).fetchall()
        for row in rows:
            self._promote_job_if_eligible(connection, str(row["id"]))

    def create_recipient_authorization_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        record = {
            "id": payload.get("id") or new_id("auth_event"),
            "recipient_id": payload["recipient_id"],
            "campaign_id": payload.get("campaign_id"),
            "job_id": payload.get("job_id"),
            "event_type": payload["event_type"],
            "actor": payload.get("actor"),
            "reason": payload.get("reason"),
            "metadata_json": json.dumps(payload.get("metadata") or {}, ensure_ascii=False) if payload.get("metadata") is not None else None,
            "created_at": payload.get("created_at") or utc_now(),
        }
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO commercial_recipient_authorization_events (
                    id, recipient_id, campaign_id, job_id, event_type,
                    actor, reason, metadata_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(record[key] for key in [
                    "id", "recipient_id", "campaign_id", "job_id", "event_type",
                    "actor", "reason", "metadata_json", "created_at",
                ]),
            )
            connection.commit()
        return record

    def create_live_execution_approval(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = payload.get("created_at") or utc_now()
        record = {
            "approval_id": payload.get("approval_id") or new_id("approval"),
            "campaign_id": payload["campaign_id"],
            "configuration_revision_id": payload.get("configuration_revision_id"),
            "execution_snapshot_id": payload.get("execution_snapshot_id"),
            "configuration_snapshot_hash": payload.get("configuration_snapshot_hash"),
            "recipient_manifest_id": payload.get("recipient_manifest_id"),
            "recipient_manifest_hash": payload.get("recipient_manifest_hash"),
            "requested_by": payload["requested_by"],
            "approved_by": payload.get("approved_by"),
            "approval_note": payload["approval_note"],
            "approval_scope": payload.get("approval_scope"),
            "source_uid": payload.get("source_uid"),
            "source_url": payload.get("source_url"),
            "account_ids_json": json.dumps(payload.get("account_ids"), ensure_ascii=False) if payload.get("account_ids") is not None else None,
            "recipient_count": payload.get("recipient_count"),
            "validation_result_json": json.dumps(payload.get("validation_result") or {}, ensure_ascii=False),
            "final_review_hash": payload.get("final_review_hash"),
            "requested_account_ids_json": json.dumps(payload.get("requested_account_ids"), ensure_ascii=False) if payload.get("requested_account_ids") is not None else None,
            "requested_max_jobs": payload.get("requested_max_jobs"),
            "readiness_snapshot_json": json.dumps(payload.get("readiness_snapshot") or {}, ensure_ascii=False),
            "approval_status": payload.get("approval_status") or "requested",
            "requested_at": payload.get("requested_at") or now,
            "invalidated_at": payload.get("invalidated_at"),
            "invalidation_reason": payload.get("invalidation_reason"),
            "created_at": now,
            "updated_at": payload.get("updated_at") or now,
            "approved_at": payload.get("approved_at"),
            "revoked_at": payload.get("revoked_at"),
            "consumed_at": payload.get("consumed_at"),
            "expires_at": payload["expires_at"],
            "revoke_reason": payload.get("revoke_reason"),
        }
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO commercial_live_execution_approvals (
                    approval_id, campaign_id, configuration_revision_id, execution_snapshot_id,
                    configuration_snapshot_hash, recipient_manifest_id, recipient_manifest_hash,
                    requested_by, approved_by, approval_note, approval_scope, source_uid,
                    source_url, account_ids_json, recipient_count, validation_result_json,
                    final_review_hash,
                    requested_account_ids_json, requested_max_jobs, readiness_snapshot_json,
                    approval_status, requested_at, invalidated_at, invalidation_reason,
                    created_at, updated_at, approved_at, revoked_at, consumed_at, expires_at,
                    revoke_reason
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(record[key] for key in [
                    "approval_id", "campaign_id", "configuration_revision_id", "execution_snapshot_id",
                    "configuration_snapshot_hash", "recipient_manifest_id", "recipient_manifest_hash",
                    "requested_by", "approved_by", "approval_note", "approval_scope", "source_uid",
                    "source_url", "account_ids_json", "recipient_count", "validation_result_json",
                    "final_review_hash",
                    "requested_account_ids_json", "requested_max_jobs", "readiness_snapshot_json",
                    "approval_status", "requested_at", "invalidated_at", "invalidation_reason",
                    "created_at", "updated_at", "approved_at", "revoked_at", "consumed_at",
                    "expires_at", "revoke_reason",
                ]),
            )
            connection.commit()
        return self.get_live_execution_approval(record["approval_id"]) or record

    def get_live_execution_approval(self, approval_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            return row_to_dict(connection.execute("SELECT * FROM commercial_live_execution_approvals WHERE approval_id = ?", (approval_id,)).fetchone())

    def list_live_execution_approvals(self, campaign_id: str | None = None, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        query = "SELECT * FROM commercial_live_execution_approvals"
        params: list[Any] = []
        if campaign_id:
            query += " WHERE campaign_id = ?"
            params.append(campaign_id)
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(query, params).fetchall()]

    def update_live_execution_approval(self, approval_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {
            "approved_by", "readiness_snapshot_json", "approval_status", "approved_at",
            "revoked_at", "consumed_at", "expires_at", "revoke_reason", "invalidated_at",
            "invalidation_reason", "updated_at",
        }
        payload = {key: value for key, value in updates.items() if key in allowed}
        if payload:
            payload.setdefault("updated_at", utc_now())
        if not payload:
            return self.get_live_execution_approval(approval_id)
        assignments = ", ".join(f"{key} = ?" for key in payload)
        with self.connection() as connection:
            connection.execute(f"UPDATE commercial_live_execution_approvals SET {assignments} WHERE approval_id = ?", (*payload.values(), approval_id))
            connection.commit()
        return self.get_live_execution_approval(approval_id)

    def get_active_send_approval_for_final_review(self, campaign_id: str, final_review_hash: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            return row_to_dict(connection.execute(
                """
                SELECT * FROM commercial_live_execution_approvals
                WHERE campaign_id = ?
                  AND final_review_hash = ?
                  AND approval_status IN ('requested', 'approved')
                  AND invalidated_at IS NULL
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (campaign_id, final_review_hash),
            ).fetchone())

    def create_execution_authorization(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = payload.get("created_at") or utc_now()
        record = {
            "execution_authorization_id": payload.get("execution_authorization_id") or new_id("execauth"),
            "campaign_id": payload["campaign_id"],
            "approval_id": payload["approval_id"],
            "snapshot_id": payload["snapshot_id"],
            "recipient_id": payload["recipient_id"],
            "manifest_hash": payload["manifest_hash"],
            "source_uid": payload["source_uid"],
            "expires_at": payload["expires_at"],
            "used_at": payload.get("used_at"),
            "status": payload.get("status") or "issued",
            "created_at": now,
            "updated_at": payload.get("updated_at") or now,
        }
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO commercial_execution_authorizations (
                    execution_authorization_id, campaign_id, approval_id, snapshot_id,
                    recipient_id, manifest_hash, source_uid, expires_at, used_at,
                    status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(record[key] for key in [
                    "execution_authorization_id", "campaign_id", "approval_id", "snapshot_id",
                    "recipient_id", "manifest_hash", "source_uid", "expires_at", "used_at",
                    "status", "created_at", "updated_at",
                ]),
            )
            connection.commit()
        return self.get_execution_authorization(record["execution_authorization_id"]) or record

    def get_execution_authorization(self, execution_authorization_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            return row_to_dict(connection.execute(
                "SELECT * FROM commercial_execution_authorizations WHERE execution_authorization_id = ?",
                (execution_authorization_id,),
            ).fetchone())

    def update_execution_authorization(self, execution_authorization_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {"used_at", "status", "updated_at"}
        payload = {key: value for key, value in updates.items() if key in allowed}
        if payload:
            payload.setdefault("updated_at", utc_now())
        if not payload:
            return self.get_execution_authorization(execution_authorization_id)
        assignments = ", ".join(f"{key} = ?" for key in payload)
        with self.connection() as connection:
            connection.execute(
                f"UPDATE commercial_execution_authorizations SET {assignments} WHERE execution_authorization_id = ?",
                (*payload.values(), execution_authorization_id),
            )
            connection.commit()
        return self.get_execution_authorization(execution_authorization_id)

    def create_live_execution_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        record = {
            "id": payload.get("id") or new_id("liveevent"),
            "event_type": payload["event_type"],
            "campaign_id": payload["campaign_id"],
            "approval_id": payload.get("approval_id"),
            "requested_by": payload.get("requested_by"),
            "approved_by": payload.get("approved_by"),
            "max_jobs": payload.get("max_jobs"),
            "account_scope_json": json.dumps(payload.get("account_scope"), ensure_ascii=False) if payload.get("account_scope") is not None else None,
            "blocking_reasons_json": json.dumps(payload.get("blocking_reasons") or [], ensure_ascii=False),
            "feature_flags_json": json.dumps(payload.get("feature_flags") or {}, ensure_ascii=False),
            "metadata_json": json.dumps(payload.get("metadata") or {}, ensure_ascii=False),
            "created_at": payload.get("created_at") or utc_now(),
        }
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO commercial_live_execution_events (
                    id, event_type, campaign_id, approval_id, requested_by, approved_by,
                    max_jobs, account_scope_json, blocking_reasons_json,
                    feature_flags_json, metadata_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(record[key] for key in [
                    "id", "event_type", "campaign_id", "approval_id", "requested_by", "approved_by",
                    "max_jobs", "account_scope_json", "blocking_reasons_json",
                    "feature_flags_json", "metadata_json", "created_at",
                ]),
            )
            connection.commit()
        return record

    def list_jobs(self, status: str | None, account_id: str | None, campaign_id: str | None, limit: int, offset: int) -> list[dict[str, Any]]:
        filters: list[str] = []
        params: list[Any] = []
        if status:
            filters.append("status = ?")
            params.append(status)
        if account_id:
            filters.append("account_id = ?")
            params.append(account_id)
        if campaign_id:
            filters.append("campaign_id = ?")
            params.append(campaign_id)
        query = "SELECT * FROM commercial_delivery_jobs"
        if filters:
            query += " WHERE " + " AND ".join(filters)
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(query, params).fetchall()]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            return row_to_dict(connection.execute("SELECT * FROM commercial_delivery_jobs WHERE id = ?", (job_id,)).fetchone())

    def list_campaign_jobs_all(self, campaign_id: str) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM commercial_delivery_jobs WHERE campaign_id = ? ORDER BY created_at ASC", (campaign_id,)).fetchall()]

    def campaign_job_counts(self, campaign_id: str) -> dict[str, int]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM commercial_delivery_jobs WHERE campaign_id = ? GROUP BY status",
                (campaign_id,),
            ).fetchall()
            return {str(row["status"]): int(row["count"]) for row in rows}

    def campaign_recipient_counts(self, campaign_id: str) -> dict[str, int]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT validation_status, COUNT(*) AS count FROM commercial_recipients WHERE campaign_id = ? GROUP BY validation_status",
                (campaign_id,),
            ).fetchall()
            return {str(row["validation_status"]): int(row["count"]) for row in rows}

    def campaign_has_importing_batch(self, campaign_id: str) -> bool:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT id FROM commercial_recipient_import_batches WHERE campaign_id = ? AND status IN ('uploaded','parsing','importing') LIMIT 1",
                (campaign_id,),
            ).fetchone()
            return row is not None

    def get_execution_batch(self, batch_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            return row_to_dict(connection.execute("SELECT * FROM commercial_execution_batches WHERE id = ?", (batch_id,)).fetchone())

    def get_execution_batch_by_idempotency(self, campaign_id: str, idempotency_key: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            return row_to_dict(
                connection.execute(
                    "SELECT * FROM commercial_execution_batches WHERE campaign_id = ? AND idempotency_key = ?",
                    (campaign_id, idempotency_key),
                ).fetchone()
            )

    def list_execution_batch_jobs(self, batch_id: str) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM commercial_delivery_jobs WHERE execution_batch_id = ? ORDER BY created_at ASC",
                    (batch_id,),
                ).fetchall()
            ]

    def list_execution_batches(self, campaign_id: str | None = None) -> list[dict[str, Any]]:
        with self.connection() as connection:
            if campaign_id:
                rows = connection.execute(
                    "SELECT * FROM commercial_execution_batches WHERE campaign_id = ? ORDER BY created_at DESC",
                    (campaign_id,),
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM commercial_execution_batches ORDER BY created_at DESC").fetchall()
            return [dict(row) for row in rows]

    def _refresh_execution_batch_aggregate(self, connection: sqlite3.Connection, batch_id: str) -> dict[str, Any] | None:
        batch = connection.execute("SELECT * FROM commercial_execution_batches WHERE id = ?", (batch_id,)).fetchone()
        if batch is None:
            return None
        rows = [dict(row) for row in connection.execute("SELECT status FROM commercial_delivery_jobs WHERE execution_batch_id = ?", (batch_id,)).fetchall()]
        total = len(rows)
        completed = sum(1 for row in rows if row["status"] in {"succeeded", "failed", "skipped", "cancelled"})
        succeeded = sum(1 for row in rows if row["status"] == "succeeded")
        failed = sum(1 for row in rows if row["status"] in {"failed", "skipped"})
        cancelled = sum(1 for row in rows if row["status"] == "cancelled")
        active = sum(1 for row in rows if row["status"] in {"assigned", "running"})
        if str(batch["status"]) == "cancelled":
            status = "cancelled"
        elif active:
            status = "in_progress"
        elif total and completed == total and failed == 0 and cancelled == 0:
            status = "completed"
        elif total and completed == total and succeeded > 0:
            status = "partially_completed"
        elif total and completed == total:
            status = "failed" if failed else "cancelled"
        else:
            status = "queued"
        now = utc_now()
        connection.execute(
            """
            UPDATE commercial_execution_batches SET
                status = ?,
                completed_job_count = ?,
                succeeded_job_count = ?,
                failed_job_count = ?,
                cancelled_job_count = ?,
                started_at = CASE WHEN ? = 'in_progress' AND started_at IS NULL THEN ? ELSE started_at END,
                completed_at = CASE WHEN ? IN ('completed','partially_completed','failed','cancelled') THEN COALESCE(completed_at, ?) ELSE completed_at END
            WHERE id = ?
            """,
            (status, completed, succeeded, failed, cancelled, status, now, status, now, batch_id),
        )
        return row_to_dict(connection.execute("SELECT * FROM commercial_execution_batches WHERE id = ?", (batch_id,)).fetchone())

    def create_controlled_execution_batch_and_jobs(self, payload: dict[str, Any]) -> dict[str, Any]:
        existing = self.get_execution_batch_by_idempotency(str(payload["campaign_id"]), str(payload["idempotency_key"]))
        if existing is not None:
            return {
                "batch": existing,
                "jobs": self.list_execution_batch_jobs(str(existing["id"])),
                "idempotent": True,
                "created_job_count": int(existing.get("created_job_count") or 0),
            }
        now = utc_now()
        batch_id = new_id("execution_batch")
        selected_platforms = [str(item) for item in payload.get("selected_platforms") or []]
        source_by_platform = dict(payload.get("source_by_platform") or {})
        sender_accounts_by_platform = dict(payload.get("sender_accounts_by_platform") or {})
        eligible_outcomes = tuple(payload.get("eligible_outcomes") or ["pending"])
        retry_only_scenario_id = payload.get("campaign_recipient_run_id")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = connection.execute(
                "SELECT * FROM commercial_execution_batches WHERE campaign_id = ? AND idempotency_key = ?",
                (payload["campaign_id"], payload["idempotency_key"]),
            ).fetchone()
            if duplicate is not None:
                connection.commit()
                duplicate_dict = dict(duplicate)
                return {"batch": duplicate_dict, "jobs": self.list_execution_batch_jobs(str(duplicate_dict["id"])), "idempotent": True, "created_job_count": int(duplicate_dict.get("created_job_count") or 0)}
            platform_placeholders = ",".join("?" for _ in selected_platforms)
            outcome_placeholders = ",".join("?" for _ in eligible_outcomes)
            filters = [
                "run.campaign_id = ?",
                f"platform_run.platform IN ({platform_placeholders})",
                f"platform_run.outcome IN ({outcome_placeholders})",
            ]
            params: list[Any] = [payload["campaign_id"], *selected_platforms, *eligible_outcomes]
            if retry_only_scenario_id:
                filters.append("run.id = ?")
                params.append(retry_only_scenario_id)
            rows = [
                dict(row)
                for row in connection.execute(
                    f"""
                    SELECT
                        platform_run.*,
                        run.id AS scenario_id,
                        run.phone_normalized AS scenario_phone,
                        recipient.phone_raw AS recipient_phone_raw,
                        recipient.display_name AS recipient_display_name,
                        recipient.input_manifest_id AS recipient_input_manifest_id,
                        recipient.input_manifest_hash AS recipient_input_manifest_hash,
                        recipient.input_sequence AS recipient_input_sequence,
                        recipient.input_provenance_status AS recipient_input_provenance_status,
                        recipient.recipient_origin AS recipient_origin,
                        recipient.synthetic_test_data AS recipient_synthetic_test_data,
                        recipient.live_execution_authorized AS recipient_live_execution_authorized,
                        recipient.live_authorized_at AS recipient_live_authorized_at,
                        recipient.live_authorized_by AS recipient_live_authorized_by,
                        recipient.authorization_source AS recipient_authorization_source,
                        recipient.authorization_note AS recipient_authorization_note,
                        recipient.authorization_status AS recipient_authorization_status,
                        recipient.should_not_retry AS recipient_should_not_retry,
                        recipient.live_execution_blocked AS recipient_live_execution_blocked,
                        recipient.block_reason AS recipient_block_reason
                    FROM commercial_platform_runs AS platform_run
                    JOIN commercial_campaign_recipient_runs AS run ON run.id = platform_run.campaign_recipient_run_id
                    JOIN commercial_recipients AS recipient ON recipient.id = platform_run.recipient_id
                    WHERE {' AND '.join(filters)}
                    ORDER BY run.created_at ASC, platform_run.platform ASC
                    """,
                    tuple(params),
                ).fetchall()
            ]
            jobs: list[dict[str, Any]] = []
            skipped = 0
            connection.execute(
                """
                INSERT INTO commercial_execution_batches (
                    id, campaign_id, approval_id, final_review_hash, execution_snapshot_id,
                    idempotency_key, requested_by, mode, status, selected_platforms_json,
                    eligible_platform_run_count, created_job_count, skipped_platform_run_count,
                    created_at, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'creating', ?, ?, 0, 0, ?, ?)
                """,
                (
                    batch_id,
                    payload["campaign_id"],
                    payload["approval_id"],
                    payload["final_review_hash"],
                    payload["execution_snapshot_id"],
                    payload["idempotency_key"],
                    payload.get("requested_by"),
                    payload.get("mode") or "mock_only",
                    json.dumps(selected_platforms, ensure_ascii=False),
                    len(rows),
                    now,
                    json.dumps(payload.get("metadata") or {}, ensure_ascii=False),
                ),
            )
            for row in rows:
                active = connection.execute(
                    """
                    SELECT id FROM commercial_delivery_jobs
                    WHERE platform_run_id = ? AND status IN ('queued','assigned','running')
                    LIMIT 1
                    """,
                    (row["id"],),
                ).fetchone()
                if active is not None or row["outcome"] in {"sent", "account_not_found", "failed_terminal", "skipped_by_policy", "cancelled"}:
                    skipped += 1
                    continue
                platform = str(row["platform"])
                account_ids = [str(item) for item in sender_accounts_by_platform.get(platform) or []]
                sender_account_id = account_ids[0] if account_ids else None
                source = dict(source_by_platform.get(platform) or {})
                previous_job_id = row.get("delivery_job_id") if row.get("outcome") == "failed_retryable" else None
                attempt_number = int(row.get("attempt_count") or 0) + 1
                job_id = new_id("job")
                plan = {
                    "job_id": job_id,
                    "execution_batch_id": batch_id,
                    "platform_run_id": row["id"],
                    "campaign_recipient_run_id": row["campaign_recipient_run_id"],
                    "campaign_id": payload["campaign_id"],
                    "recipient_id": row["recipient_id"],
                    "global_contact_id": row["global_contact_id"],
                    "platform": platform,
                    "sender_account_id": sender_account_id,
                    "source": source,
                    "phone_normalized": row["scenario_phone"],
                    "configuration_revision_id": payload.get("configuration_revision_id"),
                    "execution_snapshot_id": payload["execution_snapshot_id"],
                    "configuration_snapshot_hash": payload.get("configuration_snapshot_hash"),
                    "approval_id": payload["approval_id"],
                    "final_review_hash": payload["final_review_hash"],
                    "manifest_id": payload.get("recipient_manifest_id"),
                    "manifest_hash": payload.get("recipient_manifest_hash"),
                    "attempt_number": attempt_number,
                    "previous_job_id": previous_job_id,
                    "adapter_mode": payload.get("mode") or "mock_only",
                    "timeout_policy": payload.get("timeout_policy") or {},
                }
                connection.execute(
                    """
                    INSERT INTO commercial_delivery_jobs (
                        id, campaign_id, recipient_id, account_id, source_channel_uid,
                        display_name, phone_normalized, idempotency_key, status,
                        priority, attempt_count, max_attempts, scheduled_at, claimed_at,
                        started_at, completed_at, last_error_code, last_error_message,
                        result_success, verified_forwarded_recipient_count, forward_verified,
                        diagnostics_consistent, campaign_recipient_run_id, platform_run_id,
                        platform, global_contact_id, correlation_id, recipient_origin,
                        synthetic_test_data, live_execution_authorized, live_authorized_at,
                        live_authorized_by, authorization_source, authorization_note,
                        authorization_status, should_not_retry, configuration_revision_id,
                        execution_snapshot_id, configuration_snapshot_hash, input_manifest_id,
                        input_manifest_hash, input_sequence, input_provenance_status,
                        live_execution_blocked, block_reason, execution_batch_id,
                        execution_plan_json, adapter_mode, execution_attempt_number,
                        previous_job_id, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, 0, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        payload["campaign_id"],
                        row["recipient_id"],
                        sender_account_id,
                        source.get("source_uid"),
                        row.get("recipient_display_name"),
                        row["scenario_phone"],
                        f"{batch_id}:{row['id']}:attempt-{attempt_number}",
                        100,
                        int(payload.get("max_attempts") or 3),
                        row["campaign_recipient_run_id"],
                        row["id"],
                        platform,
                        row["global_contact_id"],
                        row["correlation_id"],
                        row.get("recipient_origin"),
                        row.get("recipient_synthetic_test_data"),
                        row.get("recipient_live_execution_authorized"),
                        row.get("recipient_live_authorized_at"),
                        row.get("recipient_live_authorized_by"),
                        row.get("recipient_authorization_source"),
                        row.get("recipient_authorization_note"),
                        row.get("recipient_authorization_status"),
                        row.get("recipient_should_not_retry"),
                        payload.get("configuration_revision_id"),
                        payload["execution_snapshot_id"],
                        payload.get("configuration_snapshot_hash"),
                        row.get("recipient_input_manifest_id"),
                        row.get("recipient_input_manifest_hash"),
                        row.get("recipient_input_sequence"),
                        row.get("recipient_input_provenance_status"),
                        row.get("recipient_live_execution_blocked"),
                        row.get("recipient_block_reason"),
                        batch_id,
                        json.dumps(plan, ensure_ascii=False, sort_keys=True),
                        payload.get("mode") or "mock_only",
                        attempt_number,
                        previous_job_id,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO commercial_execution_attempts (
                        id, execution_batch_id, job_id, platform_run_id, campaign_recipient_run_id,
                        campaign_id, platform, attempt_number, previous_job_id, status,
                        created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
                    """,
                    (new_id("attempt"), batch_id, job_id, row["id"], row["campaign_recipient_run_id"], payload["campaign_id"], platform, attempt_number, previous_job_id, now, now),
                )
                connection.execute(
                    """
                    UPDATE commercial_platform_runs SET
                        delivery_job_id = ?,
                        outcome = 'queued',
                        last_error_code = NULL,
                        last_error_message = NULL,
                        completed_at = NULL,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (job_id, now, row["id"]),
                )
                platform_row = connection.execute("SELECT * FROM commercial_platform_runs WHERE id = ?", (row["id"],)).fetchone()
                if platform_row is not None:
                    self._create_platform_run_event(
                        connection,
                        platform_run=platform_row,
                        previous_status=str(row["outcome"]),
                        new_status="queued",
                        event_type="execution_job_queued",
                        job_id=job_id,
                        actor=payload.get("requested_by") or "system",
                        source="controlled_execution",
                        reason="execution_requested",
                        metadata={"execution_batch_id": batch_id, "attempt_number": attempt_number},
                    )
                    self._refresh_campaign_recipient_run_aggregate(connection, str(row["campaign_recipient_run_id"]))
                jobs.append(dict(connection.execute("SELECT * FROM commercial_delivery_jobs WHERE id = ?", (job_id,)).fetchone()))
            status = "queued" if jobs else "failed"
            connection.execute(
                """
                UPDATE commercial_execution_batches SET
                    status = ?,
                    created_job_count = ?,
                    skipped_platform_run_count = ?
                WHERE id = ?
                """,
                (status, len(jobs), skipped, batch_id),
            )
            if jobs:
                connection.execute(
                    """
                    UPDATE commercial_campaigns SET
                        status = CASE WHEN status IN ('draft','queued','paused') THEN 'running' ELSE status END,
                        started_at = COALESCE(started_at, ?),
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (now, now, payload["campaign_id"]),
                )
            batch = dict(connection.execute("SELECT * FROM commercial_execution_batches WHERE id = ?", (batch_id,)).fetchone())
            connection.commit()
        self.refresh_campaign_counts(str(payload["campaign_id"]))
        return {"batch": batch, "jobs": jobs, "idempotent": False, "created_job_count": len(jobs)}

    def requeue_campaign_assigned_jobs(self, campaign_id: str) -> int:
        now = utc_now()
        eligibility = queue_claim_eligibility_where()
        with self.connection() as connection:
            cursor = connection.execute(
                f"""
                UPDATE commercial_delivery_jobs AS job SET
                    status = 'queued',
                    account_id = NULL,
                    claimed_at = NULL,
                    updated_at = ?
                WHERE job.campaign_id = ? AND job.status = 'assigned' AND job.started_at IS NULL
                  AND EXISTS (
                    SELECT 1
                    FROM commercial_recipients AS recipient
                    JOIN commercial_recipient_input_manifests AS manifest ON manifest.manifest_id = recipient.input_manifest_id
                    WHERE recipient.id = job.recipient_id AND {eligibility}
                  )
                """,
                (now, campaign_id),
            )
            connection.execute(
                f"""
                UPDATE commercial_delivery_jobs AS job SET
                    status = 'skipped',
                    completed_at = COALESCE(completed_at, ?),
                    last_error_code = COALESCE(last_error_code, 'recipient_provenance_unknown'),
                    last_error_message = COALESCE(last_error_message, 'Assigned job is not eligible for requeue'),
                    manual_review_required = 1,
                    safe_to_requeue = 0,
                    should_not_retry = 1,
                    updated_at = ?
                WHERE job.campaign_id = ? AND job.status = 'assigned' AND job.started_at IS NULL
                  AND NOT EXISTS (
                    SELECT 1
                    FROM commercial_recipients AS recipient
                    JOIN commercial_recipient_input_manifests AS manifest ON manifest.manifest_id = recipient.input_manifest_id
                    WHERE recipient.id = job.recipient_id AND {eligibility}
                  )
                """,
                (now, now, campaign_id),
            )
            connection.commit()
            count = int(cursor.rowcount or 0)
        self.refresh_campaign_counts(campaign_id)
        return count

    def cancel_campaign_pending_jobs(self, campaign_id: str) -> int:
        now = utc_now()
        with self.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE commercial_delivery_jobs SET
                    status = 'cancelled',
                    completed_at = ?,
                    last_error_code = COALESCE(last_error_code, 'campaign_cancelled'),
                    last_error_message = COALESCE(last_error_message, 'Campaign was cancelled'),
                    updated_at = ?
                WHERE campaign_id = ? AND (
                    status = 'queued' OR (status = 'assigned' AND started_at IS NULL)
                )
                """,
                (now, now, campaign_id),
            )
            connection.commit()
            count = int(cursor.rowcount or 0)
        self.refresh_campaign_counts(campaign_id)
        return count

    def maybe_complete_campaign(self, campaign_id: str) -> dict[str, Any] | None:
        campaign = self.get_campaign(campaign_id)
        if campaign is None or campaign.get("status") in {"draft", "queued", "paused", "cancelled", "completed"}:
            return campaign
        counts = self.campaign_job_counts(campaign_id)
        active = sum(int(counts.get(status, 0)) for status in ["queued", "assigned", "running"])
        terminal = sum(int(counts.get(status, 0)) for status in ["succeeded", "failed", "skipped", "cancelled"])
        if active == 0 and terminal > 0:
            return self.update_campaign(campaign_id, {"status": "completed", "completed_at": utc_now()})
        return campaign

    def get_job_with_recipient(self, job_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT
                    job.*,
                    recipient.phone_raw AS recipient_phone_raw,
                    recipient.phone_normalized AS recipient_phone_normalized,
                    recipient.display_name AS recipient_display_name,
                    recipient.recipient_origin AS recipient_recipient_origin,
                    recipient.synthetic_test_data AS recipient_synthetic_test_data,
                    recipient.live_execution_authorized AS recipient_live_execution_authorized,
                    recipient.live_authorized_at AS recipient_live_authorized_at,
                    recipient.live_authorized_by AS recipient_live_authorized_by,
                    recipient.authorization_source AS recipient_authorization_source,
                    recipient.authorization_note AS recipient_authorization_note,
                    recipient.authorization_status AS recipient_authorization_status,
                    recipient.should_not_retry AS recipient_should_not_retry,
                    recipient.input_manifest_id AS recipient_input_manifest_id,
                    recipient.input_manifest_hash AS recipient_input_manifest_hash,
                    recipient.input_sequence AS recipient_input_sequence,
                    recipient.input_provenance_status AS recipient_input_provenance_status,
                    recipient.contact_preparation_allowed AS recipient_contact_preparation_allowed,
                    recipient.live_execution_blocked AS recipient_live_execution_blocked,
                    recipient.block_reason AS recipient_block_reason
                FROM commercial_delivery_jobs AS job
                JOIN commercial_recipients AS recipient ON recipient.id = job.recipient_id
                WHERE job.id = ?
                """,
                (job_id,),
            ).fetchone()
            return row_to_dict(row)

    def get_active_job_for_account(self, account_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            return row_to_dict(
                connection.execute(
                    """
                    SELECT * FROM commercial_delivery_jobs
                    WHERE account_id = ? AND status IN ('assigned', 'running')
                    ORDER BY claimed_at ASC, started_at ASC
                    LIMIT 1
                    """,
                    (account_id,),
                ).fetchone()
            )

    def assign_queued_jobs_atomic(
        self,
        account_id: str,
        campaign_id: str | None,
        limit: int,
        source_channel_uid: str,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        now = utc_now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                """
                SELECT id FROM commercial_delivery_jobs
                WHERE account_id = ? AND status = 'running'
                LIMIT 1
                """,
                (account_id,),
            ).fetchone()
            if active is not None:
                connection.rollback()
                return []
            filters = ["job.status = 'queued'", queue_claim_eligibility_where()]
            params: list[Any] = []
            if campaign_id:
                filters.append("job.campaign_id = ?")
                params.append(campaign_id)
            else:
                filters.append("job.campaign_id IN (SELECT id FROM commercial_campaigns WHERE status = 'running')")
            query = f"""
                SELECT job.* FROM commercial_delivery_jobs AS job
                JOIN commercial_recipients AS recipient ON recipient.id = job.recipient_id
                JOIN commercial_recipient_input_manifests AS manifest ON manifest.manifest_id = recipient.input_manifest_id
                WHERE {' AND '.join(filters)}
                ORDER BY job.priority DESC,
                         job.scheduled_at IS NOT NULL ASC,
                         job.scheduled_at ASC,
                         job.created_at ASC
                LIMIT ?
            """
            rows = connection.execute(query, (*params, limit)).fetchall()
            job_ids = [str(row["id"]) for row in rows]
            if not job_ids:
                connection.commit()
                return []
            placeholders = ",".join("?" for _ in job_ids)
            connection.execute(
                f"""
                UPDATE commercial_delivery_jobs SET
                    account_id = ?,
                    status = 'assigned',
                    claimed_at = ?,
                    source_channel_uid = COALESCE(source_channel_uid, ?),
                    updated_at = ?
                WHERE id IN ({placeholders}) AND status = 'queued'
                """,
                (account_id, now, source_channel_uid, now, *job_ids),
            )
            assigned = connection.execute(
                f"SELECT * FROM commercial_delivery_jobs WHERE id IN ({placeholders}) ORDER BY priority DESC, scheduled_at IS NOT NULL ASC, scheduled_at ASC, created_at ASC",
                tuple(job_ids),
            ).fetchall()
            for assigned_row in assigned:
                if assigned_row["status"] != "assigned" or not assigned_row["platform_run_id"]:
                    continue
                platform_row = connection.execute(
                    "SELECT * FROM commercial_platform_runs WHERE id = ?",
                    (assigned_row["platform_run_id"],),
                ).fetchone()
                if platform_row is None:
                    continue
                previous_outcome = str(platform_row["outcome"])
                if previous_outcome not in TERMINAL_PLATFORM_OUTCOMES:
                    connection.execute(
                        """
                        UPDATE commercial_platform_runs SET
                            outcome = 'assigned',
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (now, assigned_row["platform_run_id"]),
                    )
                    self._create_platform_run_event(
                        connection,
                        platform_run=platform_row,
                        previous_status=previous_outcome,
                        new_status="assigned",
                        event_type="platform_job_assigned",
                        job_id=assigned_row["id"],
                        actor="worker",
                        source="commercial_queue",
                        reason="delivery_job_assigned",
                    )
                    self._refresh_campaign_recipient_run_aggregate(connection, str(platform_row["campaign_recipient_run_id"]))
                if assigned_row["execution_batch_id"]:
                    self._refresh_execution_batch_aggregate(connection, str(assigned_row["execution_batch_id"]))
            connection.commit()
        assigned_jobs = [dict(row) for row in assigned if row["status"] == "assigned" and row["account_id"] == account_id]
        for job in assigned_jobs:
            self.create_job_event(
                {
                    "job_id": job["id"],
                    "campaign_id": job["campaign_id"],
                    "account_id": account_id,
                    "recipient_id": job["recipient_id"],
                    "event_type": "job_assigned",
                    "status": "assigned",
                    "message": "Job assigned to account",
                }
            )
            self.refresh_campaign_counts(job["campaign_id"])
        return assigned_jobs

    def quarantine_ineligible_queued_jobs(self, campaign_id: str | None = None) -> int:
        now = utc_now()
        filters = ["job.status = 'queued'"]
        params: list[Any] = []
        if campaign_id:
            filters.append("job.campaign_id = ?")
            params.append(campaign_id)
        eligibility = queue_claim_eligibility_where()
        query = f"""
            SELECT job.id, job.campaign_id, job.recipient_id,
                CASE
                    WHEN recipient.input_manifest_id IS NULL OR recipient.input_manifest_hash IS NULL THEN 'recipient_input_manifest_required'
                    WHEN recipient.input_provenance_status != 'confirmed_manifest' THEN 'recipient_provenance_unknown'
                    WHEN manifest.manifest_id IS NULL THEN 'recipient_manifest_not_confirmed'
                    WHEN manifest.confirmation_status != 'confirmed' THEN 'recipient_manifest_not_confirmed'
                    WHEN manifest.normalized_phones_json NOT LIKE ('%' || '"' || recipient.phone_normalized || '"' || '%') THEN 'recipient_not_in_confirmed_manifest'
                    WHEN COALESCE(recipient.live_execution_authorized, 0) = 0 THEN 'recipient_live_execution_not_authorized'
                    WHEN recipient.authorization_status != 'authorized' THEN 'recipient_live_execution_not_authorized'
                    WHEN COALESCE(recipient.live_execution_blocked, 0) = 1 THEN 'recipient_live_execution_blocked'
                    WHEN COALESCE(recipient.should_not_retry, 0) = 1 THEN 'recipient_should_not_retry'
                    WHEN COALESCE(recipient.synthetic_test_data, 0) = 1 THEN 'synthetic_recipient_forbidden'
                    WHEN COALESCE(job.should_not_retry, 0) = 1 THEN 'recipient_should_not_retry'
                    WHEN COALESCE(job.live_execution_blocked, 0) = 1 THEN 'recipient_live_execution_blocked'
                    WHEN COALESCE(job.synthetic_test_data, recipient.synthetic_test_data, 0) = 1 THEN 'synthetic_recipient_forbidden'
                    ELSE 'recipient_provenance_unknown'
                END AS reason
            FROM commercial_delivery_jobs AS job
            JOIN commercial_recipients AS recipient ON recipient.id = job.recipient_id
            LEFT JOIN commercial_recipient_input_manifests AS manifest ON manifest.manifest_id = recipient.input_manifest_id
            WHERE {' AND '.join(filters)}
              AND NOT ({eligibility})
        """
        with self.connection() as connection:
            rows = [dict(row) for row in connection.execute(query, tuple(params)).fetchall()]
            if not rows:
                return 0
            for row in rows:
                connection.execute(
                    """
                    UPDATE commercial_delivery_jobs SET
                        status = 'skipped',
                        completed_at = COALESCE(completed_at, ?),
                        last_error_code = ?,
                        last_error_message = ?,
                        manual_review_required = 1,
                        safe_to_requeue = 0,
                        should_not_retry = 1,
                        live_execution_blocked = 1,
                        block_reason = ?,
                        updated_at = ?
                    WHERE id = ? AND status = 'queued'
                    """,
                    (now, row["reason"], "Job is not eligible for queue/claim without confirmed recipient provenance", row["reason"], now, row["id"]),
                )
            connection.commit()
        for row in rows:
            self.create_job_event(
                {
                    "job_id": row["id"],
                    "campaign_id": row["campaign_id"],
                    "recipient_id": row["recipient_id"],
                    "event_type": "queue_claim_rejected",
                    "status": "skipped",
                    "message": "Job blocked before claim by queue/claim provenance guard",
                    "error_code": row["reason"],
                    "error_message": "Job is not eligible for queue/claim without confirmed recipient provenance",
                    "component": "commercial_queue",
                    "step_name": "validate_job_queue_and_claim_eligibility",
                    "manual_review_required": True,
                    "diagnostics": {"adapter_called": False, "browser_launched": False},
                }
            )
            self.refresh_campaign_counts(row["campaign_id"])
        return len(rows)

    def list_assigned_jobs_for_account(self, account_id: str, campaign_id: str | None, limit: int) -> list[dict[str, Any]]:
        filters = ["account_id = ?", "status = 'assigned'"]
        params: list[Any] = [account_id]
        if campaign_id:
            filters.append("campaign_id = ?")
            params.append(campaign_id)
        query = f"""
            SELECT * FROM commercial_delivery_jobs
            WHERE {' AND '.join(filters)}
            ORDER BY priority DESC,
                     scheduled_at IS NOT NULL ASC,
                     scheduled_at ASC,
                     claimed_at ASC,
                     created_at ASC
            LIMIT ?
        """
        params.append(limit)
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(query, params).fetchall()]

    def mark_job_running(self, job_id: str) -> dict[str, Any] | None:
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE commercial_delivery_jobs SET
                    status = 'running',
                    attempt_count = attempt_count + 1,
                    started_at = ?,
                    updated_at = ?
                WHERE id = ? AND status = 'assigned'
                """,
                (now, now, job_id),
            )
            job_row = connection.execute("SELECT platform_run_id FROM commercial_delivery_jobs WHERE id = ?", (job_id,)).fetchone()
            if job_row is not None and job_row["platform_run_id"]:
                platform_row = connection.execute(
                    "SELECT * FROM commercial_platform_runs WHERE id = ?",
                    (job_row["platform_run_id"],),
                ).fetchone()
                previous_outcome = str(platform_row["outcome"]) if platform_row is not None else None
                connection.execute(
                    """
                    UPDATE commercial_platform_runs SET
                        outcome = 'in_progress',
                        attempt_count = attempt_count + 1,
                        started_at = COALESCE(started_at, ?),
                        completed_at = NULL,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (now, now, job_row["platform_run_id"]),
                )
                if platform_row is not None:
                    self._create_platform_run_event(
                        connection,
                        platform_run=platform_row,
                        previous_status=previous_outcome,
                        new_status="in_progress",
                        event_type="platform_job_started",
                        job_id=job_id,
                        actor="worker",
                        source="commercial_queue",
                        reason="delivery_job_running",
                    )
                    self._refresh_campaign_recipient_run_aggregate(connection, str(platform_row["campaign_recipient_run_id"]))
            batch_row = connection.execute("SELECT execution_batch_id FROM commercial_delivery_jobs WHERE id = ?", (job_id,)).fetchone()
            if batch_row is not None and batch_row["execution_batch_id"]:
                connection.execute(
                    "UPDATE commercial_execution_attempts SET status = 'running', started_at = COALESCE(started_at, ?), updated_at = ? WHERE job_id = ?",
                    (now, now, job_id),
                )
                self._refresh_execution_batch_aggregate(connection, str(batch_row["execution_batch_id"]))
            connection.commit()
        job = self.get_job(job_id)
        if job:
            self.refresh_campaign_counts(job["campaign_id"])
        return job

    def complete_job(self, job_id: str, status: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE commercial_delivery_jobs SET
                    status = ?,
                    completed_at = ?,
                    last_error_code = ?,
                    last_error_message = ?,
                    result_success = ?,
                    verified_forwarded_recipient_count = ?,
                    forward_verified = ?,
                    diagnostics_consistent = ?,
                    error_domain = ?,
                    severity = ?,
                    retryable = ?,
                    account_blocking = ?,
                    campaign_blocking = ?,
                    manual_review_required = ?,
                    failed_component = ?,
                    failed_step = ?,
                    safe_to_continue_round = ?,
                    safe_to_requeue = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    now,
                    payload.get("last_error_code"),
                    payload.get("last_error_message"),
                    None if payload.get("result_success") is None else int(bool(payload.get("result_success"))),
                    payload.get("verified_forwarded_recipient_count"),
                    None if payload.get("forward_verified") is None else int(bool(payload.get("forward_verified"))),
                    None if payload.get("diagnostics_consistent") is None else int(bool(payload.get("diagnostics_consistent"))),
                    payload.get("error_domain"),
                    payload.get("severity"),
                    None if payload.get("retryable") is None else int(bool(payload.get("retryable"))),
                    None if payload.get("account_blocking") is None else int(bool(payload.get("account_blocking"))),
                    None if payload.get("campaign_blocking") is None else int(bool(payload.get("campaign_blocking"))),
                    None if payload.get("manual_review_required") is None else int(bool(payload.get("manual_review_required"))),
                    payload.get("failed_component"),
                    payload.get("failed_step"),
                    None if payload.get("safe_to_continue_round") is None else int(bool(payload.get("safe_to_continue_round"))),
                    None if payload.get("safe_to_requeue") is None else int(bool(payload.get("safe_to_requeue"))),
                    now,
                    job_id,
                ),
            )
            job_row = connection.execute(
                "SELECT campaign_recipient_run_id, platform_run_id FROM commercial_delivery_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if job_row is not None and job_row["platform_run_id"]:
                platform_row = connection.execute(
                    "SELECT * FROM commercial_platform_runs WHERE id = ?",
                    (job_row["platform_run_id"],),
                ).fetchone()
                previous_outcome = str(platform_row["outcome"]) if platform_row is not None else None
                verified_success = (
                    status == "succeeded"
                    and bool(payload.get("result_success"))
                    and bool(payload.get("forward_verified"))
                    and bool(payload.get("diagnostics_consistent"))
                )
                if verified_success:
                    outcome = "sent"
                elif status == "cancelled":
                    outcome = "cancelled"
                elif status == "skipped":
                    outcome = "skipped_by_policy"
                elif payload.get("last_error_code") == "account_not_found":
                    outcome = "account_not_found"
                elif bool(payload.get("retryable")):
                    outcome = "failed_retryable"
                else:
                    outcome = "failed_terminal"
                if previous_outcome in TERMINAL_PLATFORM_OUTCOMES and previous_outcome != outcome:
                    raise ValueError("terminal_platform_outcome_immutable")
                connection.execute(
                    """
                    UPDATE commercial_platform_runs SET
                        outcome = ?,
                        last_error_code = ?,
                        last_error_message = ?,
                        completed_at = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        outcome,
                        payload.get("last_error_code"),
                        payload.get("last_error_message"),
                        now,
                        now,
                        job_row["platform_run_id"],
                    ),
                )
                if platform_row is not None:
                    self._create_platform_run_event(
                        connection,
                        platform_run=platform_row,
                        previous_status=previous_outcome,
                        new_status=outcome,
                        event_type="platform_job_completed",
                        job_id=job_id,
                        actor="worker",
                        source="commercial_queue",
                        reason="delivery_job_completed",
                        error_code=payload.get("last_error_code"),
                        error_message=payload.get("last_error_message"),
                        metadata={
                            "job_status": status,
                            "result_success": payload.get("result_success"),
                            "forward_verified": payload.get("forward_verified"),
                            "diagnostics_consistent": payload.get("diagnostics_consistent"),
                        },
                    )
                self._refresh_campaign_recipient_run_aggregate(connection, str(job_row["campaign_recipient_run_id"]))
            batch_row = connection.execute("SELECT execution_batch_id, platform_run_id FROM commercial_delivery_jobs WHERE id = ?", (job_id,)).fetchone()
            if batch_row is not None and batch_row["execution_batch_id"]:
                connection.execute(
                    """
                    UPDATE commercial_execution_attempts SET
                        status = ?,
                        outcome = (SELECT outcome FROM commercial_platform_runs WHERE id = ?),
                        error_code = ?,
                        retryable = ?,
                        finished_at = COALESCE(finished_at, ?),
                        updated_at = ?
                    WHERE job_id = ?
                    """,
                    (
                        status,
                        batch_row["platform_run_id"],
                        payload.get("last_error_code"),
                        None if payload.get("retryable") is None else int(bool(payload.get("retryable"))),
                        now,
                        now,
                        job_id,
                    ),
                )
                self._refresh_execution_batch_aggregate(connection, str(batch_row["execution_batch_id"]))
            connection.commit()
        job = self.get_job(job_id)
        if job:
            self.refresh_campaign_counts(job["campaign_id"])
        return job

    def apply_trusted_worker_result(self, job_id: str, result: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        allowed_outcomes = {
            "sent", "account_not_found", "not_reachable", "blocked",
            "failed_retryable", "failed_terminal", "cancelled",
        }
        outcome = str(result.get("outcome") or ("sent" if result.get("success") else "failed_terminal"))
        if outcome not in allowed_outcomes:
            outcome = "failed_terminal"
        trusted_result_key = str(result.get("trusted_result_key") or result.get("remote_message_id") or f"{job_id}:{outcome}")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute("SELECT * FROM commercial_delivery_jobs WHERE id = ?", (job_id,)).fetchone()
            if job is None:
                connection.rollback()
                return {"applied": False, "reason": "job_not_found"}
            job_dict = dict(job)
            if job_dict.get("result_applied_at"):
                connection.commit()
                return {
                    "applied": False,
                    "idempotent": job_dict.get("trusted_result_key") == trusted_result_key,
                    "reason": "result_already_applied",
                    "job": job_dict,
                }
            platform_run = connection.execute("SELECT * FROM commercial_platform_runs WHERE id = ?", (job_dict.get("platform_run_id"),)).fetchone()
            if platform_run is None:
                connection.rollback()
                return {"applied": False, "reason": "platform_run_not_found", "job": job_dict}
            platform_dict = dict(platform_run)
            if platform_dict.get("delivery_job_id") != job_id:
                connection.execute(
                    """
                    UPDATE commercial_execution_attempts SET
                        status = 'stale_rejected',
                        outcome = ?,
                        error_code = ?,
                        error_category = ?,
                        retryable = ?,
                        trusted_result_key = ?,
                        finished_at = ?,
                        diagnostics_json = ?,
                        updated_at = ?
                    WHERE job_id = ?
                    """,
                    (
                        outcome,
                        result.get("error_code") or "stale_attempt_result",
                        result.get("error_category"),
                        None if result.get("retryable") is None else int(bool(result.get("retryable"))),
                        trusted_result_key,
                        now,
                        json.dumps(result.get("diagnostics") or {}, ensure_ascii=False),
                        now,
                        job_id,
                    ),
                )
                connection.commit()
                return {"applied": False, "reason": "stale_attempt_result", "job": job_dict}
            previous_outcome = str(platform_dict["outcome"])
            if previous_outcome in TERMINAL_PLATFORM_OUTCOMES and previous_outcome != outcome:
                connection.rollback()
                return {"applied": False, "reason": "terminal_platform_outcome_immutable", "job": job_dict}
            job_status = "succeeded" if outcome == "sent" else ("cancelled" if outcome == "cancelled" else "failed")
            success = outcome == "sent"
            retryable = bool(result.get("retryable")) if result.get("retryable") is not None else outcome == "failed_retryable"
            connection.execute(
                """
                UPDATE commercial_delivery_jobs SET
                    status = ?,
                    completed_at = ?,
                    last_error_code = ?,
                    last_error_message = ?,
                    result_success = ?,
                    verified_forwarded_recipient_count = ?,
                    forward_verified = ?,
                    diagnostics_consistent = 1,
                    retryable = ?,
                    failed_component = 'worker',
                    failed_step = ?,
                    trusted_result_key = ?,
                    result_applied_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    job_status,
                    now,
                    result.get("error_code"),
                    result.get("error_message"),
                    int(success),
                    int(result.get("verified_forwarded_recipient_count") or (1 if success else 0)),
                    int(success),
                    int(retryable),
                    result.get("failed_step") or "apply_trusted_adapter_result",
                    trusted_result_key,
                    now,
                    now,
                    job_id,
                ),
            )
            connection.execute(
                """
                UPDATE commercial_platform_runs SET
                    outcome = ?,
                    last_error_code = ?,
                    last_error_message = ?,
                    completed_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    outcome,
                    result.get("error_code"),
                    result.get("error_message"),
                    now,
                    now,
                    platform_dict["id"],
                ),
            )
            self._create_platform_run_event(
                connection,
                platform_run=platform_run,
                previous_status=previous_outcome,
                new_status=outcome,
                event_type="trusted_worker_result_applied",
                job_id=job_id,
                actor="worker",
                source="controlled_execution",
                reason="adapter_result_received",
                error_code=result.get("error_code"),
                error_message=result.get("error_message"),
                metadata={
                    "execution_batch_id": job_dict.get("execution_batch_id"),
                    "trusted_result_key": trusted_result_key,
                    "error_category": result.get("error_category"),
                    "retryable": retryable,
                    "remote_message_id": result.get("remote_message_id"),
                    "diagnostics": result.get("diagnostics") or {},
                },
            )
            connection.execute(
                """
                UPDATE commercial_execution_attempts SET
                    status = ?,
                    trusted_result_key = ?,
                    outcome = ?,
                    error_code = ?,
                    error_category = ?,
                    retryable = ?,
                    started_at = COALESCE(started_at, ?),
                    finished_at = ?,
                    diagnostics_json = ?,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (
                    job_status,
                    trusted_result_key,
                    outcome,
                    result.get("error_code"),
                    result.get("error_category"),
                    int(retryable),
                    result.get("started_at") or now,
                    result.get("finished_at") or now,
                    json.dumps(result.get("diagnostics") or {}, ensure_ascii=False),
                    now,
                    job_id,
                ),
            )
            self._refresh_campaign_recipient_run_aggregate(connection, str(platform_dict["campaign_recipient_run_id"]))
            if job_dict.get("execution_batch_id"):
                self._refresh_execution_batch_aggregate(connection, str(job_dict["execution_batch_id"]))
            connection.commit()
        updated_job = self.get_job(job_id) or {}
        if updated_job:
            self.refresh_campaign_counts(str(updated_job["campaign_id"]))
        return {"applied": True, "idempotent": False, "outcome": outcome, "job": updated_job}

    def cancel_execution_batch(self, batch_id: str, reason: str) -> dict[str, Any]:
        now = utc_now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            batch = connection.execute("SELECT * FROM commercial_execution_batches WHERE id = ?", (batch_id,)).fetchone()
            if batch is None:
                connection.rollback()
                raise KeyError(batch_id)
            queued_jobs = [dict(row) for row in connection.execute("SELECT * FROM commercial_delivery_jobs WHERE execution_batch_id = ? AND status = 'queued'", (batch_id,)).fetchall()]
            for job in queued_jobs:
                connection.execute(
                    """
                    UPDATE commercial_delivery_jobs SET
                        status = 'cancelled',
                        completed_at = ?,
                        last_error_code = 'batch_cancelled',
                        last_error_message = ?,
                        cancellation_requested_at = ?,
                        updated_at = ?
                    WHERE id = ? AND status = 'queued'
                    """,
                    (now, reason, now, now, job["id"]),
                )
                platform_row = connection.execute("SELECT * FROM commercial_platform_runs WHERE id = ?", (job["platform_run_id"],)).fetchone()
                if platform_row is not None and str(platform_row["outcome"]) not in TERMINAL_PLATFORM_OUTCOMES:
                    connection.execute(
                        "UPDATE commercial_platform_runs SET outcome = 'cancelled', last_error_code = 'batch_cancelled', last_error_message = ?, completed_at = ?, updated_at = ? WHERE id = ?",
                        (reason, now, now, job["platform_run_id"]),
                    )
                    self._create_platform_run_event(
                        connection,
                        platform_run=platform_row,
                        previous_status=str(platform_row["outcome"]),
                        new_status="cancelled",
                        event_type="batch_cancelled",
                        job_id=job["id"],
                        actor="system",
                        source="controlled_execution",
                        reason=reason,
                    )
                    self._refresh_campaign_recipient_run_aggregate(connection, str(platform_row["campaign_recipient_run_id"]))
            connection.execute(
                "UPDATE commercial_delivery_jobs SET cancellation_requested_at = COALESCE(cancellation_requested_at, ?), updated_at = ? WHERE execution_batch_id = ? AND status = 'running'",
                (now, now, batch_id),
            )
            connection.execute(
                "UPDATE commercial_execution_batches SET status = 'cancelled', cancellation_reason = ?, completed_at = COALESCE(completed_at, ?) WHERE id = ?",
                (reason, now, batch_id),
            )
            self._refresh_execution_batch_aggregate(connection, batch_id)
            updated = dict(connection.execute("SELECT * FROM commercial_execution_batches WHERE id = ?", (batch_id,)).fetchone())
            connection.commit()
        self.refresh_campaign_counts(str(updated["campaign_id"]))
        return updated

    def requeue_assigned_jobs_for_account(self, account_id: str, exclude_job_ids: set[str] | None = None, reason: str = "round_stopped") -> int:
        exclude_job_ids = exclude_job_ids or set()
        eligibility = queue_claim_eligibility_where()
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM commercial_delivery_jobs WHERE account_id = ? AND status = 'assigned'",
                (account_id,),
            ).fetchall()
            requeued = 0
            for row in rows:
                if str(row["id"]) in exclude_job_ids:
                    continue
                eligible = connection.execute(
                    f"""
                    SELECT 1
                    FROM commercial_delivery_jobs AS job
                    JOIN commercial_recipients AS recipient ON recipient.id = job.recipient_id
                    JOIN commercial_recipient_input_manifests AS manifest ON manifest.manifest_id = recipient.input_manifest_id
                    WHERE job.id = ? AND {eligibility}
                    """,
                    (row["id"],),
                ).fetchone()
                if eligible is None:
                    connection.execute(
                        """
                        UPDATE commercial_delivery_jobs SET
                            status = 'skipped',
                            completed_at = COALESCE(completed_at, ?),
                            last_error_code = COALESCE(last_error_code, 'recipient_provenance_unknown'),
                            last_error_message = COALESCE(last_error_message, 'Assigned job is not eligible for requeue'),
                            manual_review_required = 1,
                            safe_to_requeue = 0,
                            should_not_retry = 1,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (utc_now(), utc_now(), row["id"]),
                    )
                    continue
                connection.execute(
                    """
                    UPDATE commercial_delivery_jobs SET
                        status = 'queued',
                        account_id = NULL,
                        claimed_at = NULL,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (utc_now(), row["id"]),
                )
                requeued += 1
            connection.commit()
        for row in rows:
            if str(row["id"]) not in exclude_job_ids:
                latest = self.get_job(row["id"])
                self.create_job_event(
                    {
                        "job_id": row["id"],
                        "campaign_id": row["campaign_id"],
                        "account_id": account_id,
                        "recipient_id": row["recipient_id"],
                        "event_type": "job_requeued" if (latest or {}).get("status") == "queued" else "queue_claim_rejected",
                        "status": (latest or {}).get("status") or "queued",
                        "message": reason,
                    }
                )
                self.refresh_campaign_counts(row["campaign_id"])
        return requeued

    def update_account_runtime(self, account_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        current = self.get_account_settings(account_id)
        payload = dict(current or {})
        payload.update(updates)
        return self.upsert_account_settings(account_id, payload)

    def increment_account_sent_counts(self, account_id: str) -> None:
        account = self.get_account_settings(account_id) or {}
        self.update_account_runtime(
            account_id,
            {
                "current_daily_sent_count": int(account.get("current_daily_sent_count") or 0) + 1,
                "current_round_sent_count": int(account.get("current_round_sent_count") or 0) + 1,
                "last_job_completed_at": utc_now(),
            },
        )

    def get_worker_lock(self, account_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            return row_to_dict(connection.execute("SELECT * FROM commercial_account_worker_locks WHERE account_id = ?", (account_id,)).fetchone())

    def list_active_worker_locks(self) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM commercial_account_worker_locks").fetchall()
            return [dict(row) for row in rows if (parse_time(row["expires_at"]) or now) > now]

    def count_jobs_by_status(self) -> dict[str, int]:
        with self.connection() as connection:
            rows = connection.execute("SELECT status, COUNT(*) AS count FROM commercial_delivery_jobs GROUP BY status").fetchall()
            return {str(row["status"]): int(row["count"]) for row in rows}

    def count_jobs_today_by_status(self, status: str) -> int:
        start = datetime.now(timezone.utc).date().isoformat()
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count FROM commercial_delivery_jobs
                WHERE status = ? AND completed_at >= ?
                """,
                (status, start),
            ).fetchone()
            return int(row["count"] if row else 0)

    def count_campaigns_by_status(self, status: str) -> int:
        with self.connection() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM commercial_campaigns WHERE status = ?", (status,)).fetchone()
            return int(row["count"] if row else 0)

    def acquire_worker_lock(self, account_id: str, owner: str, ttl_seconds: int) -> dict[str, Any] | None:
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        expires_at = (now_dt + timedelta(seconds=max(1, ttl_seconds))).isoformat()
        token = new_id("lock")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM commercial_account_worker_locks WHERE account_id = ?", (account_id,)).fetchone()
            if existing is not None:
                existing_expires = parse_time(existing["expires_at"])
                if existing_expires and existing_expires > now_dt:
                    connection.rollback()
                    return None
                connection.execute("DELETE FROM commercial_account_worker_locks WHERE account_id = ?", (account_id,))
            connection.execute(
                """
                INSERT INTO commercial_account_worker_locks (
                    account_id, lock_owner, lock_token, active_job_id,
                    acquired_at, heartbeat_at, expires_at
                )
                VALUES (?, ?, ?, NULL, ?, ?, ?)
                """,
                (account_id, owner, token, now, now, expires_at),
            )
            connection.commit()
        return self.get_worker_lock(account_id)

    def update_worker_lock_heartbeat(self, account_id: str, token: str, active_job_id: str | None, ttl_seconds: int) -> None:
        now_dt = datetime.now(timezone.utc)
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE commercial_account_worker_locks SET
                    active_job_id = ?,
                    heartbeat_at = ?,
                    expires_at = ?
                WHERE account_id = ? AND lock_token = ?
                """,
                (active_job_id, now_dt.isoformat(), (now_dt + timedelta(seconds=max(1, ttl_seconds))).isoformat(), account_id, token),
            )
            connection.commit()

    def release_worker_lock(self, account_id: str, token: str | None = None, stale_only: bool = False) -> bool:
        lock = self.get_worker_lock(account_id)
        if not lock:
            return False
        if token and lock.get("lock_token") != token:
            return False
        if stale_only:
            expires = parse_time(lock.get("expires_at"))
            if expires and expires > datetime.now(timezone.utc):
                return False
        with self.connection() as connection:
            connection.execute("DELETE FROM commercial_account_worker_locks WHERE account_id = ?", (account_id,))
            connection.commit()
        return True

    def recover_stale_jobs(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        recovered = 0
        manual_review = 0
        eligibility = queue_claim_eligibility_where()
        with self.connection() as connection:
            locks = {row["account_id"]: dict(row) for row in connection.execute("SELECT * FROM commercial_account_worker_locks").fetchall()}
            jobs = [dict(row) for row in connection.execute("SELECT * FROM commercial_delivery_jobs WHERE status IN ('assigned', 'running')").fetchall()]
            for job in jobs:
                lock = locks.get(str(job.get("account_id") or ""))
                lock_expired = lock is None or ((parse_time(lock.get("expires_at")) or now) <= now)
                if not lock_expired:
                    continue
                if job["status"] == "assigned":
                    eligible = connection.execute(
                        f"""
                        SELECT 1
                        FROM commercial_delivery_jobs AS job
                        JOIN commercial_recipients AS recipient ON recipient.id = job.recipient_id
                        JOIN commercial_recipient_input_manifests AS manifest ON manifest.manifest_id = recipient.input_manifest_id
                        WHERE job.id = ? AND {eligibility}
                        """,
                        (job["id"],),
                    ).fetchone()
                    if eligible is not None:
                        connection.execute(
                            """
                            UPDATE commercial_delivery_jobs SET
                                status = 'queued',
                                account_id = NULL,
                                claimed_at = NULL,
                                updated_at = ?
                            WHERE id = ?
                            """,
                            (utc_now(), job["id"]),
                        )
                        recovered += 1
                    else:
                        connection.execute(
                            """
                            UPDATE commercial_delivery_jobs SET
                                status = 'skipped',
                                last_error_code = 'recipient_provenance_unknown',
                                last_error_message = 'Assigned job had expired worker lock and is not eligible for requeue',
                                manual_review_required = 1,
                                safe_to_requeue = 0,
                                should_not_retry = 1,
                                updated_at = ?
                            WHERE id = ?
                            """,
                            (utc_now(), job["id"]),
                        )
                        manual_review += 1
                else:
                    connection.execute(
                        """
                        UPDATE commercial_delivery_jobs SET
                            status = 'paused',
                            last_error_code = 'manual_review_required',
                            last_error_message = 'Running job had expired worker lock; delivery outcome is uncertain',
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (utc_now(), job["id"]),
                    )
                    manual_review += 1
            for account_id, lock in locks.items():
                expires = parse_time(lock.get("expires_at"))
                if expires is None or expires <= now:
                    connection.execute("DELETE FROM commercial_account_worker_locks WHERE account_id = ?", (account_id,))
            connection.commit()
        return {"ok": True, "requeued_assigned_count": recovered, "manual_review_required_count": manual_review}

    def create_import_batch(
        self,
        campaign_id: str,
        import_source: str,
        original_filename: str | None,
        items: list[dict[str, Any]],
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        batch_id = new_id("import")
        counts = {
            "submitted_count": len(items),
            "valid_count": sum(1 for item in items if item["validation_status"] == "valid"),
            "invalid_count": sum(1 for item in items if item["validation_status"] == "invalid"),
            "duplicate_count": sum(1 for item in items if item["validation_status"] == "duplicate"),
            "blocked_count": sum(1 for item in items if item["validation_status"] == "blocked"),
            "opted_out_count": sum(1 for item in items if item["validation_status"] == "opted_out"),
        }
        batch = {
            "id": batch_id,
            "campaign_id": campaign_id,
            "import_source": import_source,
            "original_filename": original_filename,
            "status": "preview_ready",
            **counts,
            "created_recipient_count": 0,
            "created_job_count": 0,
            "created_at": now,
            "validated_at": now,
            "imported_at": None,
            "error_code": None,
            "error_message": None,
            "metadata_json": json.dumps(metadata or {}, ensure_ascii=False),
        }
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO commercial_recipient_import_batches (
                    id, campaign_id, import_source, original_filename, status,
                    submitted_count, valid_count, invalid_count, duplicate_count,
                    blocked_count, opted_out_count, created_recipient_count,
                    created_job_count, created_at, validated_at, imported_at,
                    error_code, error_message, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(batch[key] for key in [
                    "id", "campaign_id", "import_source", "original_filename", "status",
                    "submitted_count", "valid_count", "invalid_count", "duplicate_count",
                    "blocked_count", "opted_out_count", "created_recipient_count",
                    "created_job_count", "created_at", "validated_at", "imported_at",
                    "error_code", "error_message", "metadata_json",
                ]),
            )
            for item in items:
                record = {
                    "id": item.get("id") or new_id("import_item"),
                    "batch_id": batch_id,
                    "row_number": item["row_number"],
                    "phone_raw": item["phone_raw"],
                    "phone_normalized": item.get("phone_normalized"),
                    "display_name": item.get("display_name"),
                    "validation_status": item["validation_status"],
                    "duplicate_reason": item.get("duplicate_reason"),
                    "duplicate_recipient_id": item.get("duplicate_recipient_id"),
                    "error_code": item.get("error_code"),
                    "error_message": item.get("error_message"),
                    "selected_for_import": int(bool(item.get("selected_for_import", item["validation_status"] == "valid"))),
                    "created_recipient_id": None,
                    "created_job_id": None,
                    "created_at": now,
                }
                connection.execute(
                    """
                    INSERT INTO commercial_recipient_import_items (
                        id, batch_id, row_number, phone_raw, phone_normalized,
                        display_name, validation_status, duplicate_reason,
                        duplicate_recipient_id, error_code, error_message,
                        selected_for_import, created_recipient_id, created_job_id,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    tuple(record[key] for key in [
                        "id", "batch_id", "row_number", "phone_raw", "phone_normalized",
                        "display_name", "validation_status", "duplicate_reason",
                        "duplicate_recipient_id", "error_code", "error_message",
                        "selected_for_import", "created_recipient_id", "created_job_id",
                        "created_at",
                    ]),
                )
            connection.commit()
        return self.get_import_batch(batch_id) or batch

    def get_import_batch(self, batch_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            return row_to_dict(connection.execute("SELECT * FROM commercial_recipient_import_batches WHERE id = ?", (batch_id,)).fetchone())

    def list_import_items(
        self,
        batch_id: str,
        validation_status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        filters = ["batch_id = ?"]
        params: list[Any] = [batch_id]
        if validation_status:
            filters.append("validation_status = ?")
            params.append(validation_status)
        query = f"""
            SELECT * FROM commercial_recipient_import_items
            WHERE {' AND '.join(filters)}
            ORDER BY row_number ASC
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(query, params).fetchall()]

    def create_recipient_input_manifest(
        self,
        campaign_id: str,
        phones: list[str],
        batch_id: str | None,
        submitted_by: str | None,
        source_type: str,
        source_filename: str | None = None,
        confirmation_status: str = "confirmed",
        confirmed_by: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        normalized = [str(phone) for phone in phones]
        record = {
            "manifest_id": new_id("manifest"),
            "campaign_id": campaign_id,
            "batch_id": batch_id,
            "submitted_by": submitted_by,
            "submitted_at": now,
            "source_type": source_type,
            "source_filename": source_filename,
            "recipient_count": len(normalized),
            "normalized_phones_json": json.dumps(normalized, ensure_ascii=False),
            "manifest_hash": manifest_hash(normalized),
            "confirmation_status": confirmation_status,
            "confirmed_by": confirmed_by,
            "confirmed_at": now if confirmation_status == "confirmed" else None,
            "superseded_at": None,
        }
        with self.connection() as connection:
            if confirmation_status == "confirmed":
                connection.execute(
                    "UPDATE commercial_recipient_input_manifests SET confirmation_status='superseded', superseded_at=? WHERE campaign_id=? AND confirmation_status='confirmed'",
                    (now, campaign_id),
                )
            connection.execute(
                """
                INSERT INTO commercial_recipient_input_manifests (
                    manifest_id, campaign_id, batch_id, submitted_by, submitted_at,
                    source_type, source_filename, recipient_count, normalized_phones_json,
                    manifest_hash, confirmation_status, confirmed_by, confirmed_at,
                    superseded_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(record[key] for key in [
                    "manifest_id", "campaign_id", "batch_id", "submitted_by", "submitted_at",
                    "source_type", "source_filename", "recipient_count", "normalized_phones_json",
                    "manifest_hash", "confirmation_status", "confirmed_by", "confirmed_at",
                    "superseded_at",
                ]),
            )
            connection.commit()
        return record

    def get_confirmed_manifest_for_campaign(self, campaign_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            return row_to_dict(connection.execute(
                """
                SELECT * FROM commercial_recipient_input_manifests
                WHERE campaign_id = ? AND confirmation_status = 'confirmed'
                ORDER BY confirmed_at DESC LIMIT 1
                """,
                (campaign_id,),
            ).fetchone())

    def list_recipient_input_manifests(self, campaign_id: str) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM commercial_recipient_input_manifests WHERE campaign_id = ? ORDER BY submitted_at ASC",
                (campaign_id,),
            ).fetchall()]

    def create_dry_run_audit_record(self, campaign_id: str, source_endpoint: str, status: str, diagnostics: dict[str, Any], requested_by: str | None = None, forbidden_mutation_detected: bool = False) -> dict[str, Any]:
        record = {
            "dry_run_id": new_id("dryrun"),
            "campaign_id": campaign_id,
            "requested_by": requested_by,
            "requested_at": utc_now(),
            "source_endpoint": source_endpoint,
            "status": status,
            "diagnostics_json": json.dumps(diagnostics, ensure_ascii=False),
            "forbidden_mutation_detected": int(bool(forbidden_mutation_detected)),
        }
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO commercial_dry_run_audit_records (
                    dry_run_id, campaign_id, requested_by, requested_at,
                    source_endpoint, status, diagnostics_json,
                    forbidden_mutation_detected
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(record[key] for key in [
                    "dry_run_id", "campaign_id", "requested_by", "requested_at",
                    "source_endpoint", "status", "diagnostics_json",
                    "forbidden_mutation_detected",
                ]),
            )
            connection.commit()
        return record

    def delete_import_batch(self, batch_id: str) -> dict[str, Any]:
        batch = self.get_import_batch(batch_id)
        if batch is None:
            return {"deleted": False, "reason": "not_found"}
        if batch["status"] in {"completed", "importing"} or int(batch.get("created_recipient_count") or 0) > 0:
            return {"deleted": False, "reason": "batch_already_confirmed"}
        with self.connection() as connection:
            connection.execute("DELETE FROM commercial_recipient_import_items WHERE batch_id = ?", (batch_id,))
            connection.execute("DELETE FROM commercial_recipient_import_batches WHERE id = ?", (batch_id,))
            connection.commit()
        return {"deleted": True, "batch_id": batch_id}

    def confirm_import_batch(
        self,
        batch_id: str,
        include_valid: bool = True,
        selected_item_ids: list[str] | None = None,
        default_priority: int = 0,
        scheduled_at: str | None = None,
        authorization: dict[str, Any] | None = None,
        manifest: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        batch = self.get_import_batch(batch_id)
        if batch is None:
            raise KeyError(batch_id)
        if batch["status"] == "completed":
            return {
                "batch_id": batch_id,
                "status": "completed",
                "created_recipient_count": int(batch.get("created_recipient_count") or 0),
                "created_job_count": int(batch.get("created_job_count") or 0),
                "created_recipients": [],
                "created_jobs": [],
                "idempotent_replay": True,
            }
        if batch["status"] not in {"preview_ready", "failed"}:
            raise ValueError("batch_not_importable")
        selected_set = set(selected_item_ids or [])
        campaign = self.get_campaign(str(batch["campaign_id"]))
        if campaign is None:
            raise KeyError(str(batch["campaign_id"]))
        now = utc_now()
        created_recipients: list[dict[str, Any]] = []
        created_jobs: list[dict[str, Any]] = []
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE commercial_recipient_import_batches SET status = 'importing' WHERE id = ? AND status != 'completed'",
                (batch_id,),
            )
            rows = connection.execute(
                """
                SELECT * FROM commercial_recipient_import_items
                WHERE batch_id = ? AND validation_status = 'valid' AND selected_for_import = 1
                ORDER BY row_number ASC
                """,
                (batch_id,),
            ).fetchall()
            for row in rows:
                item = dict(row)
                if selected_set and item["id"] not in selected_set:
                    continue
                if not include_valid:
                    continue
                if item.get("created_recipient_id") and item.get("created_job_id"):
                    continue
                idempotency_key = f"{campaign['id']}:{item['phone_normalized']}"
                existing_job = connection.execute(
                    "SELECT * FROM commercial_delivery_jobs WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing_job is not None:
                    connection.execute(
                        """
                        UPDATE commercial_recipient_import_items SET
                            created_recipient_id = ?,
                            created_job_id = ?
                        WHERE id = ?
                        """,
                        (existing_job["recipient_id"], existing_job["id"], item["id"]),
                    )
                    continue
                recipient = {
                    "id": new_id("recipient"),
                    "campaign_id": campaign["id"],
                    "phone_raw": item["phone_raw"],
                    "phone_normalized": item["phone_normalized"],
                    "display_name": item.get("display_name"),
                    "import_source": batch["import_source"],
                    "validation_status": "valid",
                    "duplicate_of_recipient_id": None,
                    "recipient_origin": (authorization or {}).get("recipient_origin") or "user_import",
                    "synthetic_test_data": int(bool((authorization or {}).get("synthetic_test_data", False))),
                    "live_execution_authorized": int(bool((authorization or {}).get("live_execution_authorized", False))),
                    "live_authorized_at": (authorization or {}).get("live_authorized_at"),
                    "live_authorized_by": (authorization or {}).get("live_authorized_by"),
                    "authorization_source": (authorization or {}).get("authorization_source") or "import_confirm_default",
                    "authorization_note": (authorization or {}).get("authorization_note") or "Import confirmation does not grant live authorization by default",
                    "authorization_status": (authorization or {}).get("authorization_status") or "authorization_required",
                    "should_not_retry": int(bool((authorization or {}).get("should_not_retry", False))),
                    "input_manifest_id": (manifest or {}).get("manifest_id"),
                    "input_manifest_hash": (manifest or {}).get("manifest_hash"),
                    "input_sequence": item.get("row_number"),
                    "input_provenance_status": "confirmed_manifest" if manifest else "unknown",
                    "contact_preparation_allowed": 0,
                    "live_execution_blocked": 0 if manifest else 1,
                    "block_reason": None if manifest else "recipient_input_manifest_required",
                    "created_at": now,
                    "updated_at": now,
                }
                job = {
                    "id": new_id("job"),
                    "campaign_id": campaign["id"],
                    "recipient_id": recipient["id"],
                    "account_id": None,
                    "source_channel_uid": campaign.get("source_channel_uid"),
                    "display_name": item.get("display_name"),
                    "phone_normalized": item["phone_normalized"],
                    "idempotency_key": idempotency_key,
                    "status": "queued",
                    "priority": int(default_priority or 0),
                    "attempt_count": 0,
                    "max_attempts": 1,
                    "scheduled_at": scheduled_at,
                    "claimed_at": None,
                    "started_at": None,
                    "completed_at": None,
                    "last_error_code": None,
                    "last_error_message": None,
                    "result_success": None,
                    "verified_forwarded_recipient_count": None,
                    "forward_verified": None,
                    "diagnostics_consistent": None,
                    "recipient_origin": recipient["recipient_origin"],
                    "synthetic_test_data": recipient["synthetic_test_data"],
                    "live_execution_authorized": recipient["live_execution_authorized"],
                    "live_authorized_at": recipient["live_authorized_at"],
                    "live_authorized_by": recipient["live_authorized_by"],
                    "authorization_source": recipient["authorization_source"],
                    "authorization_note": recipient["authorization_note"],
                    "authorization_status": recipient["authorization_status"],
                    "should_not_retry": recipient["should_not_retry"],
                    "input_manifest_id": recipient["input_manifest_id"],
                    "input_manifest_hash": recipient["input_manifest_hash"],
                    "input_sequence": recipient["input_sequence"],
                    "input_provenance_status": recipient["input_provenance_status"],
                    "live_execution_blocked": recipient["live_execution_blocked"],
                    "block_reason": recipient["block_reason"],
                    "created_at": now,
                    "updated_at": now,
                }
                connection.execute(
                    """
                    INSERT INTO commercial_recipients (
                        id, campaign_id, phone_raw, phone_normalized, display_name,
                        import_source, validation_status, duplicate_of_recipient_id,
                        recipient_origin, synthetic_test_data, live_execution_authorized,
                        live_authorized_at, live_authorized_by, authorization_source,
                        authorization_note, authorization_status, should_not_retry,
                        input_manifest_id, input_manifest_hash, input_sequence,
                        input_provenance_status, contact_preparation_allowed,
                        live_execution_blocked, block_reason, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    tuple(recipient[key] for key in [
                        "id", "campaign_id", "phone_raw", "phone_normalized", "display_name",
                        "import_source", "validation_status", "duplicate_of_recipient_id",
                        "recipient_origin", "synthetic_test_data", "live_execution_authorized",
                        "live_authorized_at", "live_authorized_by", "authorization_source",
                        "authorization_note", "authorization_status", "should_not_retry",
                        "input_manifest_id", "input_manifest_hash", "input_sequence",
                        "input_provenance_status", "contact_preparation_allowed",
                        "live_execution_blocked", "block_reason",
                        "created_at", "updated_at",
                    ]),
                )
                connection.execute(
                    """
                    INSERT INTO commercial_delivery_jobs (
                        id, campaign_id, recipient_id, account_id, source_channel_uid,
                        display_name, phone_normalized, idempotency_key, status,
                        priority, attempt_count, max_attempts, scheduled_at, claimed_at,
                        started_at, completed_at, last_error_code, last_error_message,
                        result_success, verified_forwarded_recipient_count, forward_verified,
                        diagnostics_consistent, recipient_origin, synthetic_test_data,
                        live_execution_authorized, live_authorized_at, live_authorized_by,
                        authorization_source, authorization_note, authorization_status,
                        should_not_retry, input_manifest_id, input_manifest_hash,
                        input_sequence, input_provenance_status,
                        live_execution_blocked, block_reason, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    tuple(job[key] for key in [
                        "id", "campaign_id", "recipient_id", "account_id", "source_channel_uid",
                        "display_name", "phone_normalized", "idempotency_key", "status",
                        "priority", "attempt_count", "max_attempts", "scheduled_at", "claimed_at",
                        "started_at", "completed_at", "last_error_code", "last_error_message",
                        "result_success", "verified_forwarded_recipient_count", "forward_verified",
                        "diagnostics_consistent", "recipient_origin", "synthetic_test_data",
                        "live_execution_authorized", "live_authorized_at", "live_authorized_by",
                        "authorization_source", "authorization_note", "authorization_status",
                        "should_not_retry", "input_manifest_id", "input_manifest_hash",
                        "input_sequence", "input_provenance_status",
                        "live_execution_blocked", "block_reason", "created_at", "updated_at",
                    ]),
                )
                connection.execute(
                    """
                    UPDATE commercial_recipient_import_items SET
                        created_recipient_id = ?,
                        created_job_id = ?
                    WHERE id = ?
                    """,
                    (recipient["id"], job["id"], item["id"]),
                )
                created_recipients.append(recipient)
                created_jobs.append(job)
            connection.execute(
                """
                UPDATE commercial_recipient_import_batches SET
                    status = 'completed',
                    created_recipient_count = (
                        SELECT COUNT(*) FROM commercial_recipient_import_items
                        WHERE batch_id = ? AND created_recipient_id IS NOT NULL
                    ),
                    created_job_count = (
                        SELECT COUNT(*) FROM commercial_recipient_import_items
                        WHERE batch_id = ? AND created_job_id IS NOT NULL
                    ),
                    imported_at = ?
                WHERE id = ?
                """,
                (batch_id, batch_id, now, batch_id),
            )
            connection.commit()
        self.refresh_campaign_counts(str(campaign["id"]))
        updated = self.get_import_batch(batch_id) or {}
        return {
            "batch_id": batch_id,
            "status": updated.get("status"),
            "created_recipient_count": int(updated.get("created_recipient_count") or 0),
            "created_job_count": int(updated.get("created_job_count") or 0),
            "created_recipients": created_recipients,
            "created_jobs": created_jobs,
            "idempotent_replay": False,
        }

    def create_job_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        record = {
            "id": payload.get("id") or new_id("event"),
            "job_id": payload["job_id"],
            "campaign_id": payload["campaign_id"],
            "account_id": payload.get("account_id"),
            "recipient_id": payload["recipient_id"],
            "event_type": payload["event_type"],
            "step_name": payload.get("step_name"),
            "status": payload["status"],
            "message": payload.get("message"),
            "error_code": payload.get("error_code"),
            "error_message": payload.get("error_message"),
            "correlation_id": payload.get("correlation_id"),
            "scheduler_tick_id": payload.get("scheduler_tick_id"),
            "worker_round_id": payload.get("worker_round_id"),
            "platform": payload.get("platform"),
            "session_id": payload.get("session_id"),
            "component": payload.get("component"),
            "duration_ms": payload.get("duration_ms"),
            "error_domain": payload.get("error_domain"),
            "retryable": None if payload.get("retryable") is None else int(bool(payload.get("retryable"))),
            "manual_review_required": None if payload.get("manual_review_required") is None else int(bool(payload.get("manual_review_required"))),
            "diagnostics_json": json.dumps(payload.get("diagnostics"), ensure_ascii=False) if payload.get("diagnostics") is not None else payload.get("diagnostics_json"),
            "created_at": payload.get("created_at") or utc_now(),
        }
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO commercial_job_events (
                    id, job_id, campaign_id, account_id, recipient_id, event_type,
                    step_name, status, message, error_code, error_message,
                    correlation_id, scheduler_tick_id, worker_round_id, platform,
                    session_id, component, duration_ms, error_domain, retryable,
                    manual_review_required,
                    diagnostics_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(record[key] for key in [
                    "id", "job_id", "campaign_id", "account_id", "recipient_id", "event_type",
                    "step_name", "status", "message", "error_code", "error_message",
                    "correlation_id", "scheduler_tick_id", "worker_round_id", "platform",
                    "session_id", "component", "duration_ms", "error_domain", "retryable",
                    "manual_review_required",
                    "diagnostics_json", "created_at",
                ]),
            )
            connection.commit()
        return record

    def list_events(self, job_id: str | None, campaign_id: str | None, account_id: str | None, limit: int, offset: int) -> list[dict[str, Any]]:
        filters: list[str] = []
        params: list[Any] = []
        if job_id:
            filters.append("job_id = ?")
            params.append(job_id)
        if campaign_id:
            filters.append("campaign_id = ?")
            params.append(campaign_id)
        if account_id:
            filters.append("account_id = ?")
            params.append(account_id)
        query = "SELECT * FROM commercial_job_events"
        if filters:
            query += " WHERE " + " AND ".join(filters)
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(query, params).fetchall()]

    def refresh_campaign_counts(self, campaign_id: str) -> None:
        with self.connection() as connection:
            recipient_count = connection.execute(
                "SELECT COUNT(*) AS count FROM commercial_recipients WHERE campaign_id = ? AND validation_status = 'valid'",
                (campaign_id,),
            ).fetchone()["count"]
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM commercial_delivery_jobs WHERE campaign_id = ? GROUP BY status",
                (campaign_id,),
            ).fetchall()
            counts = {row["status"]: int(row["count"]) for row in rows}
            connection.execute(
                """
                UPDATE commercial_campaigns SET
                    total_recipients = ?,
                    queued_count = ?,
                    running_count = ?,
                    succeeded_count = ?,
                    failed_count = ?,
                    skipped_count = ?,
                    cancelled_count = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    int(recipient_count),
                    int(counts.get("queued", 0) + counts.get("assigned", 0)),
                    int(counts.get("running", 0)),
                    int(counts.get("succeeded", 0)),
                    int(counts.get("failed", 0)),
                    int(counts.get("skipped", 0)),
                    int(counts.get("cancelled", 0)),
                    utc_now(),
                    campaign_id,
                ),
            )
            connection.commit()
