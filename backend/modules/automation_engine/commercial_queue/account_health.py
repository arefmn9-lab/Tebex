from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from modules.automation_engine.commercial_queue.repository import utc_now
from modules.automation_engine.db.database import DATABASE_PATH
from modules.automation_engine.db.models import initialize_schema


BLOCKING_STATES = {"auth_required", "rate_limited", "platform_restricted", "profile_conflict", "session_error", "manual_review", "disabled"}


class AccountHealthRepository:
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

    def get(self, account_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM commercial_account_health WHERE account_id = ?", (account_id,)).fetchone()
            if row:
                data = dict(row)
                data["manual_review_required"] = bool(data["manual_review_required"])
                return data
        return self.upsert({"account_id": account_id, "health_status": "healthy"})

    def list(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM commercial_account_health ORDER BY account_id ASC").fetchall()
        return [{**dict(row), "manual_review_required": bool(row["manual_review_required"])} for row in rows]

    def upsert(self, payload: dict[str, Any]) -> dict[str, Any]:
        current = None
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM commercial_account_health WHERE account_id = ?", (payload["account_id"],)).fetchone()
            current = dict(row) if row else {}
            record = {
                "account_id": payload["account_id"],
                "health_status": payload.get("health_status", current.get("health_status", "healthy")),
                "last_success_at": payload.get("last_success_at", current.get("last_success_at")),
                "last_failure_at": payload.get("last_failure_at", current.get("last_failure_at")),
                "consecutive_failures": int(payload.get("consecutive_failures", current.get("consecutive_failures", 0))),
                "auth_failure_count": int(payload.get("auth_failure_count", current.get("auth_failure_count", 0))),
                "session_failure_count": int(payload.get("session_failure_count", current.get("session_failure_count", 0))),
                "profile_conflict_count": int(payload.get("profile_conflict_count", current.get("profile_conflict_count", 0))),
                "platform_warning_count": int(payload.get("platform_warning_count", current.get("platform_warning_count", 0))),
                "rate_limit_count": int(payload.get("rate_limit_count", current.get("rate_limit_count", 0))),
                "last_error_domain": payload.get("last_error_domain", current.get("last_error_domain")),
                "last_error_code": payload.get("last_error_code", current.get("last_error_code")),
                "last_error_message": payload.get("last_error_message", current.get("last_error_message")),
                "manual_review_required": bool(payload.get("manual_review_required", current.get("manual_review_required", False))),
                "paused_at": payload.get("paused_at", current.get("paused_at")),
                "pause_reason": payload.get("pause_reason", current.get("pause_reason")),
                "last_authentication_verified_at": payload.get("last_authentication_verified_at", current.get("last_authentication_verified_at")),
                "updated_at": utc_now(),
            }
            connection.execute(
                """
                INSERT INTO commercial_account_health (
                    account_id, health_status, last_success_at, last_failure_at,
                    consecutive_failures, auth_failure_count, session_failure_count,
                    profile_conflict_count, platform_warning_count, rate_limit_count,
                    last_error_domain, last_error_code, last_error_message,
                    manual_review_required, paused_at, pause_reason,
                    last_authentication_verified_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    health_status = excluded.health_status,
                    last_success_at = excluded.last_success_at,
                    last_failure_at = excluded.last_failure_at,
                    consecutive_failures = excluded.consecutive_failures,
                    auth_failure_count = excluded.auth_failure_count,
                    session_failure_count = excluded.session_failure_count,
                    profile_conflict_count = excluded.profile_conflict_count,
                    platform_warning_count = excluded.platform_warning_count,
                    rate_limit_count = excluded.rate_limit_count,
                    last_error_domain = excluded.last_error_domain,
                    last_error_code = excluded.last_error_code,
                    last_error_message = excluded.last_error_message,
                    manual_review_required = excluded.manual_review_required,
                    paused_at = excluded.paused_at,
                    pause_reason = excluded.pause_reason,
                    last_authentication_verified_at = excluded.last_authentication_verified_at,
                    updated_at = excluded.updated_at
                """,
                (
                    record["account_id"], record["health_status"], record["last_success_at"], record["last_failure_at"],
                    record["consecutive_failures"], record["auth_failure_count"], record["session_failure_count"],
                    record["profile_conflict_count"], record["platform_warning_count"], record["rate_limit_count"],
                    record["last_error_domain"], record["last_error_code"], record["last_error_message"],
                    int(record["manual_review_required"]), record["paused_at"], record["pause_reason"],
                    record["last_authentication_verified_at"], record["updated_at"],
                ),
            )
            connection.commit()
        return self.get(str(payload["account_id"]))


class AccountHealthService:
    def __init__(self, repository: AccountHealthRepository | None = None) -> None:
        self.repository = repository or AccountHealthRepository()

    def record_success(self, account_id: str) -> dict[str, Any]:
        now = utc_now()
        return self.repository.upsert({
            "account_id": account_id,
            "health_status": "healthy",
            "last_success_at": now,
            "consecutive_failures": 0,
            "last_error_domain": None,
            "last_error_code": None,
            "last_error_message": None,
            "manual_review_required": False,
            "pause_reason": None,
            "paused_at": None,
            "last_authentication_verified_at": now,
        })

    def record_failure(self, account_id: str, error: dict[str, Any]) -> dict[str, Any]:
        current = self.repository.get(account_id)
        code = str(error.get("error_code") or "")
        domain = str(error.get("error_domain") or "")
        status = current["health_status"]
        manual = bool(error.get("manual_review_required"))
        updates: dict[str, Any] = {
            "account_id": account_id,
            "last_failure_at": utc_now(),
            "consecutive_failures": int(current["consecutive_failures"]) + 1,
            "last_error_domain": domain,
            "last_error_code": code,
            "last_error_message": error.get("message") or error.get("error_message"),
            "manual_review_required": manual,
        }
        if code in {"not_logged_in", "bale_install_prompt", "login_state_unknown", "authentication_required"}:
            status = "auth_required"; updates["auth_failure_count"] = int(current["auth_failure_count"]) + 1
        elif code in {"profile_path_conflict", "profile_owned_by_another_account", "profile_path_outside_allowed_root", "browser_identity_mismatch", "profile_path_invalid"}:
            status = "profile_conflict"; updates["profile_conflict_count"] = int(current["profile_conflict_count"]) + 1
        elif code in {"browser_profile_corruption", "session_start_failed", "session_health_check_failed", "session_page_closed", "session_reset_failed", "session_invalidated"}:
            status = "session_error"; updates["session_failure_count"] = int(current["session_failure_count"]) + 1
        elif code in {"rate_limited", "platform_restricted"}:
            status = "rate_limited" if code == "rate_limited" else "platform_restricted"; updates["rate_limit_count"] = int(current["rate_limit_count"]) + 1
        elif manual or code in {"forward_confirm_failed", "multiple_recipients_selected", "diagnostics_inconsistent", "selector_regression"}:
            status = "manual_review"; updates["manual_review_required"] = True
        else:
            status = "warning" if bool(error.get("account_blocking")) else current["health_status"]
        if status in BLOCKING_STATES:
            updates["paused_at"] = utc_now(); updates["pause_reason"] = code
        updates["health_status"] = status
        return self.repository.upsert(updates)

    def set_status(self, account_id: str, status: str, reason: str | None = None) -> dict[str, Any]:
        return self.repository.upsert({"account_id": account_id, "health_status": status, "manual_review_required": status == "manual_review", "paused_at": utc_now() if status in BLOCKING_STATES else None, "pause_reason": reason})
