from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from modules.automation_engine.commercial_queue.repository import utc_now
from modules.automation_engine.db.database import DATABASE_PATH
from modules.automation_engine.db.models import initialize_schema


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    if "enabled" in data:
        data["enabled"] = bool(data["enabled"])
    return data


class BrowserIdentityRepository:
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

    def get_by_account(self, account_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            return _row(connection.execute("SELECT * FROM commercial_browser_identities WHERE account_id = ?", (account_id,)).fetchone())

    def get_by_profile_key(self, normalized_profile_path: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            return _row(connection.execute("SELECT * FROM commercial_browser_identities WHERE normalized_profile_path = ?", (normalized_profile_path.casefold(),)).fetchone())

    def list_identities(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return [_row(row) or {} for row in connection.execute("SELECT * FROM commercial_browser_identities ORDER BY account_id ASC").fetchall()]

    def upsert_identity(self, payload: dict[str, Any], increment_version: bool = False) -> dict[str, Any]:
        existing = self.get_by_account(str(payload["account_id"]))
        now = utc_now()
        record = {
            "identity_id": (existing or {}).get("identity_id") or f"identity_{uuid4().hex[:12]}",
            "account_id": payload["account_id"],
            "platform": payload.get("platform") or "bale",
            "profile_path": payload["profile_path"],
            "normalized_profile_path": payload["normalized_profile_path"].casefold(),
            "locale": payload.get("locale") or "fa-IR",
            "timezone_id": payload.get("timezone_id") or "Asia/Tehran",
            "viewport_width": int(payload.get("viewport_width") or 1280),
            "viewport_height": int(payload.get("viewport_height") or 720),
            "device_scale_factor": float(payload.get("device_scale_factor") or 1),
            "chrome_channel": payload.get("chrome_channel") or "stable",
            "network_route_id": payload.get("network_route_id"),
            "worker_node_id": payload.get("worker_node_id") or "local_windows_1",
            "identity_version": int((existing or {}).get("identity_version") or 1) + (1 if existing and increment_version else 0),
            "enabled": bool(payload.get("enabled", (existing or {}).get("enabled", True))),
            "validation_status": payload.get("validation_status") or (existing or {}).get("validation_status") or "pending",
            "last_validated_at": payload.get("last_validated_at", (existing or {}).get("last_validated_at")),
            "last_validation_error_code": payload.get("last_validation_error_code", (existing or {}).get("last_validation_error_code")),
            "last_validation_error_message": payload.get("last_validation_error_message", (existing or {}).get("last_validation_error_message")),
            "created_at": (existing or {}).get("created_at") or now,
            "updated_at": now,
        }
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO commercial_browser_identities (
                    identity_id, account_id, platform, profile_path, normalized_profile_path,
                    locale, timezone_id, viewport_width, viewport_height, device_scale_factor,
                    chrome_channel, network_route_id, worker_node_id, identity_version,
                    enabled, validation_status, last_validated_at, last_validation_error_code,
                    last_validation_error_message, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    platform = excluded.platform,
                    profile_path = excluded.profile_path,
                    normalized_profile_path = excluded.normalized_profile_path,
                    locale = excluded.locale,
                    timezone_id = excluded.timezone_id,
                    viewport_width = excluded.viewport_width,
                    viewport_height = excluded.viewport_height,
                    device_scale_factor = excluded.device_scale_factor,
                    chrome_channel = excluded.chrome_channel,
                    network_route_id = excluded.network_route_id,
                    worker_node_id = excluded.worker_node_id,
                    identity_version = excluded.identity_version,
                    enabled = excluded.enabled,
                    validation_status = excluded.validation_status,
                    last_validated_at = excluded.last_validated_at,
                    last_validation_error_code = excluded.last_validation_error_code,
                    last_validation_error_message = excluded.last_validation_error_message,
                    updated_at = excluded.updated_at
                """,
                (
                    record["identity_id"], record["account_id"], record["platform"], record["profile_path"], record["normalized_profile_path"],
                    record["locale"], record["timezone_id"], record["viewport_width"], record["viewport_height"], record["device_scale_factor"],
                    record["chrome_channel"], record["network_route_id"], record["worker_node_id"], record["identity_version"],
                    int(record["enabled"]), record["validation_status"], record["last_validated_at"], record["last_validation_error_code"],
                    record["last_validation_error_message"], record["created_at"], record["updated_at"],
                ),
            )
            connection.commit()
        return self.get_by_account(str(record["account_id"])) or record
