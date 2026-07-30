from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4
from zoneinfo import ZoneInfo

from modules.automation_engine.browser_identity.bale_profile_contract import (
    LOCK_FILENAME,
    PROFILE_ROOT,
    canonical_profile_dir,
    chrome_processes_for_profile,
    resolve_profile_record,
)
from modules.automation_engine.browser_identity.validation import profile_compare_key
from modules.automation_engine.db.database import DATABASE_PATH
from modules.automation_engine.db.models import initialize_schema
from modules.automation_engine.plugins.bale.account_store import BaleAccountStore, bale_account_store, normalize_bale_identifier
from modules.automation_engine.account_registry.bale_onboarding_migrations import migrate


LIFECYCLE_TRANSITIONS = {
    "discovered_existing": {"disabled", "login_required", "blocked", "retired"},
    "provisioned": {"login_required", "disabled", "retired", "provisioning_failed"},
    "login_required": {"login_in_progress", "disabled", "blocked", "retired"},
    "login_in_progress": {"authenticated", "authentication_failed", "login_required", "blocked"},
    "authenticated": {"persistence_check_required", "verification_expired", "disabled", "blocked", "retired"},
    "persistence_check_required": {"ready", "authentication_failed", "blocked", "disabled"},
    "ready": {"verification_expired", "disabled", "blocked", "retired"},
    "verification_expired": {"login_in_progress", "disabled", "blocked", "retired"},
    "authentication_failed": {"login_in_progress", "disabled", "blocked", "retired"},
    "blocked": {"disabled", "login_required", "retired"},
    "disabled": {"login_required", "ready", "retired"},
    "provisioning_failed": {"provisioned", "retired"},
    "retired": set(),
}

DEFAULT_CONFIGURATION = {
    "onboarding_mode": True,
    "live_sending_enabled": False,
    "queue_enabled": False,
    "automatic_retry_enabled": False,
    "stop_on_first_failure": True,
    "max_login_concurrency": 1,
    "max_operational_browser_concurrency": 1,
    "max_active_sessions": 1,
    "max_worker_concurrency": 1,
    "max_accounts_per_scheduler_cycle": 1,
    "authentication_verification_ttl_seconds": 86400,
    "profile_lock_ttl_seconds": 300,
    "stale_session_recovery_interval_seconds": 60,
    "cpu_threshold_percent": 90,
    "memory_threshold_percent": 85,
    "timezone": "Asia/Tehran",
    "daily_reset_policy": "local_midnight",
    "default_hourly_limit": 10,
    "default_daily_limit": 30,
    "default_minimum_delay_seconds": 60,
    "default_round_size": 1,
    "default_cooldown_seconds": 300,
}

LEGACY_SCHEMA_REFERENCE = """
CREATE TABLE IF NOT EXISTS bale_operational_accounts (
    account_id TEXT PRIMARY KEY,
    normalized_identifier TEXT NOT NULL UNIQUE,
    masked_identifier TEXT NOT NULL,
    lifecycle_status TEXT NOT NULL,
    scheduling_enabled INTEGER NOT NULL DEFAULT 0,
    authentication_status TEXT NOT NULL DEFAULT 'unverified',
    authentication_verified_at TEXT,
    verification_expires_at TEXT,
    session_persistence_status TEXT NOT NULL DEFAULT 'unknown',
    health_status TEXT NOT NULL DEFAULT 'unknown',
    blocking_reason TEXT,
    last_error_code TEXT,
    safe_error_message TEXT,
    canonical_profile_path TEXT NOT NULL UNIQUE,
    normalized_profile_path TEXT NOT NULL UNIQUE,
    browser_identity_id TEXT,
    onboarding_blocked INTEGER NOT NULL DEFAULT 0,
    retired_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    audit_metadata_json TEXT
);
CREATE TABLE IF NOT EXISTS bale_onboarding_batches (
    onboarding_batch_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    target_count INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bale_onboarding_batch_accounts (
    onboarding_batch_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    membership_kind TEXT NOT NULL,
    added_at TEXT NOT NULL,
    PRIMARY KEY(onboarding_batch_id, account_id)
);
CREATE TABLE IF NOT EXISTS bale_onboarding_operations (
    operation_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    operation_type TEXT NOT NULL,
    account_id TEXT,
    onboarding_batch_id TEXT,
    status TEXT NOT NULL,
    current_step TEXT,
    progress_json TEXT NOT NULL,
    error_code TEXT,
    safe_error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bale_account_audit_events (
    event_id TEXT PRIMARY KEY,
    account_id TEXT,
    onboarding_batch_id TEXT,
    operation_id TEXT,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    safe_message TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bale_maintenance_sessions (
    maintenance_session_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL UNIQUE,
    purpose TEXT NOT NULL,
    status TEXT NOT NULL,
    backend_instance_id TEXT NOT NULL,
    runtime_session_id TEXT,
    opened_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    closed_at TEXT,
    last_auth_state TEXT,
    safe_diagnostics_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bale_profile_launch_locks (
    normalized_profile_path TEXT PRIMARY KEY,
    account_id TEXT NOT NULL UNIQUE,
    owner_id TEXT NOT NULL,
    backend_instance_id TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def mask_identifier(value: str) -> str:
    value = str(value or "")
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:3]}{'*' * max(3, len(value) - 5)}{value[-2:]}"


def canonical_account_id(normalized_identifier: str) -> str:
    return f"bale_{normalized_identifier}"


class BaleOnboardingError(RuntimeError):
    def __init__(self, error_code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.details = details or {}


class BaleOnboardingService:
    def __init__(
        self,
        database_path: Path | None = None,
        account_store: BaleAccountStore | None = None,
        profile_root: Path | None = None,
        process_inspector: Callable[[Any], list[dict[str, Any]]] | None = None,
        auth_ttl_seconds: int = 86400,
    ) -> None:
        self.database_path = Path(database_path or DATABASE_PATH)
        self.account_store = account_store or bale_account_store
        self.profile_root = Path(profile_root or os.environ.get("CLINICOS_BALE_PROFILE_ROOT") or PROFILE_ROOT).resolve(strict=False)
        self.process_inspector = process_inspector or chrome_processes_for_profile
        self.auth_ttl_seconds = auth_ttl_seconds
        self.backend_instance_id = f"backend_{uuid4().hex[:12]}"

    @contextmanager
    def connection(self, *, initialize: bool = True):
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        if initialize:
            initialize_schema(connection)
            migrate(connection)
        try:
            yield connection
        finally:
            connection.close()

    def _profile_path(self, account_id: str) -> Path:
        if self.profile_root == PROFILE_ROOT.resolve(strict=False):
            return canonical_profile_dir(account_id)
        return (self.profile_root / account_id).resolve(strict=False)

    def preview(self, identifier: str, account_id: str | None = None) -> dict[str, Any]:
        normalized = normalize_bale_identifier(identifier)
        proposed_id = str(account_id or canonical_account_id(normalized))
        return {
            "normalized_identifier": normalized,
            "masked_identifier": mask_identifier(normalized),
            "account_id": proposed_id,
            "canonical_profile_path": str(self._profile_path(proposed_id)),
        }

    def preflight(self, identifier: str, account_id: str | None = None) -> dict[str, Any]:
        preview = self.preview(identifier, account_id)
        normalized = preview["normalized_identifier"]
        proposed_id = preview["account_id"]
        profile_path = Path(preview["canonical_profile_path"])
        checks: list[dict[str, Any]] = []

        def add(name: str, status: str, code: str, message: str, action: str = "") -> None:
            checks.append({"check_name": name, "status": status, "error_code": code, "message_fa": message, "recommended_action": action})

        add("identifier_present", "pass" if normalized else "fail", "identifier_required", "شماره یا شناسه الزامی است.")
        valid_phone = bool(re.fullmatch(r"09\d{9}", normalized))
        add("iranian_phone_format", "pass" if valid_phone else "fail", "invalid_iranian_phone", "شماره باید یک شماره موبایل معتبر ایران باشد.")
        valid_id = bool(re.fullmatch(r"[A-Za-z0-9_.-]{3,96}", proposed_id))
        add("account_id_format", "pass" if valid_id else "fail", "invalid_account_id", "شناسه اکانت فقط باید شامل حروف، عدد، نقطه، خط تیره یا زیرخط باشد.")
        try:
            relative = profile_path.relative_to(self.profile_root)
            canonical = len(relative.parts) == 1 and relative.parts[0] == proposed_id
        except ValueError:
            canonical = False
        add("canonical_profile_path", "pass" if canonical else "fail", "profile_path_not_canonical", "مسیر پروفایل باید دقیقاً زیر شاخه مجاز همان اکانت باشد.")

        registry = self.account_store.list_accounts()
        duplicate_id = next((item for item in registry if str(item.get("account_id")) == proposed_id), None)
        duplicate_phone = next((item for item in registry if normalize_bale_identifier(str(item.get("phone") or item.get("username_or_number") or "")) == normalized), None)
        add("duplicate_account_id", "fail" if duplicate_id else "pass", "duplicate_account_id", "این شناسه اکانت قبلاً ثبت شده است.", "از عملیات تطبیق اکانت موجود استفاده کنید.")
        add("duplicate_identifier", "fail" if duplicate_phone else "pass", "duplicate_bale_identifier", "این شماره با قالب دیگری قبلاً ثبت شده است.", "اکانت موجود را تطبیق دهید.")

        db_rows: dict[str, Any] = {}
        if self.database_path.exists():
            connection = sqlite3.connect(f"file:{self.database_path.as_posix()}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            try:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table, key in [
                    ("bale_operational_accounts", "operational"),
                    ("commercial_account_settings", "settings"),
                    ("commercial_browser_identities", "identity"),
                    ("commercial_account_health", "health"),
                ]:
                    if table in tables:
                        db_rows[key] = connection.execute(f"SELECT * FROM {table} WHERE account_id = ?", (proposed_id,)).fetchone()
                if "bale_operational_accounts" in tables:
                    db_rows["phone_owner"] = connection.execute("SELECT account_id FROM bale_operational_accounts WHERE normalized_identifier = ?", (normalized,)).fetchone()
                    db_rows["profile_owner"] = connection.execute("SELECT account_id FROM bale_operational_accounts WHERE normalized_profile_path = ?", (profile_compare_key(profile_path),)).fetchone()
            finally:
                connection.close()
        stale_sqlite = any(db_rows.get(key) is not None for key in ("operational", "settings", "identity", "health")) and not duplicate_id
        add("sqlite_state", "fail" if stale_sqlite else "pass", "sqlite_only_stale_account", "برای این شناسه رکورد دیتابیس بدون رکورد رجیستری وجود دارد.", "ابتدا تعارض را بررسی کنید.")
        phone_owner = db_rows.get("phone_owner")
        phone_conflict = bool(phone_owner and str(phone_owner["account_id"]) != proposed_id)
        add("transactional_identifier_unique", "fail" if phone_conflict else "pass", "duplicate_bale_identifier", "این شماره در منبع عملیاتی به اکانت دیگری تعلق دارد.")
        profile_owner = db_rows.get("profile_owner")
        profile_conflict = bool(profile_owner and str(profile_owner["account_id"]) != proposed_id)
        add("profile_owner_unique", "fail" if profile_conflict else "pass", "profile_owned_by_another_account", "این مسیر پروفایل متعلق به اکانت دیگری است.")

        profile_exists = profile_path.exists()
        orphan = profile_exists and not duplicate_id and not db_rows.get("identity")
        add("profile_directory", "fail" if orphan else "pass", "profile_only_orphan", "یک پوشه پروفایل بدون مالک معتبر پیدا شد.", "پروفایل را خودکار قبول نکنید؛ بررسی دستی لازم است.")
        record = resolve_profile_record(proposed_id) if self.profile_root == PROFILE_ROOT.resolve(strict=False) and valid_id else None
        active = self.process_inspector(record) if record is not None else []
        add("active_chrome", "fail" if active else "pass", "profile_already_in_use", "مرورگر دیگری در حال استفاده از این پروفایل است.")
        marker = profile_path / LOCK_FILENAME
        add("ownership_marker", "fail" if marker.exists() and not active else "pass", "stale_ownership_marker", "نشانگر مالکیت قدیمی دیده شد؛ بازیابی صریح لازم است.")

        blocking = [item for item in checks if item["status"] == "fail"]
        return {**preview, "checks": checks, "provisioning_allowed": not blocking, "blocking_error_codes": [item["error_code"] for item in blocking], "side_effect_free": True}

    def provision(self, payload: dict[str, Any]) -> dict[str, Any]:
        identifier = str(payload.get("identifier") or payload.get("phone") or "")
        preflight = self.preflight(identifier, payload.get("account_id"))
        idem = str(payload.get("idempotency_key") or "").strip()
        if not idem:
            raise BaleOnboardingError("idempotency_key_required", "کلید یکتای عملیات الزامی است.")
        request_body = {
            "account_id": preflight["account_id"],
            "normalized_identifier": preflight["normalized_identifier"],
            "onboarding_batch_id": payload.get("onboarding_batch_id"),
        }
        request_hash = hashlib.sha256(json.dumps(request_body, sort_keys=True).encode()).hexdigest()
        self._ensure_schema()
        with self.connection() as connection:
            previous = connection.execute("SELECT * FROM bale_onboarding_operations WHERE idempotency_key = ?", (idem,)).fetchone()
            if previous:
                if previous["request_hash"] != request_hash:
                    raise BaleOnboardingError("idempotency_conflict", "این کلید قبلاً برای درخواست دیگری استفاده شده است.")
                if previous["status"] == "completed":
                    return {"operation": dict(previous), "account": self.get_account(preflight["account_id"]), "idempotent_replay": True}
        if not preflight["provisioning_allowed"]:
            raise BaleOnboardingError("preflight_failed", "پیش‌بررسی اکانت ناموفق بود.", preflight)

        operation_id = f"onboard_{uuid4().hex}"
        now = utc_now()
        account_id = preflight["account_id"]
        created_registry = False
        created_profile = False
        try:
            with self.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO bale_onboarding_operations VALUES (?, ?, ?, 'provision_account', ?, ?, 'running', 'metadata', ?, NULL, NULL, ?, ?)",
                    (operation_id, idem, request_hash, account_id, payload.get("onboarding_batch_id"), json.dumps(["validated"]), now, now),
                )
                profile_path = preflight["canonical_profile_path"]
                connection.execute(
                    """INSERT INTO bale_operational_accounts (
                       account_id, normalized_identifier, masked_identifier, lifecycle_status, scheduling_enabled,
                       authentication_status, session_persistence_status, health_status, canonical_profile_path,
                       normalized_profile_path, onboarding_blocked, created_at, updated_at, audit_metadata_json
                    ) VALUES (?, ?, ?, 'provisioned', 0, 'unverified', 'unknown', 'unknown', ?, ?, 0, ?, ?, ?)""",
                    (account_id, preflight["normalized_identifier"], preflight["masked_identifier"], profile_path, profile_compare_key(profile_path), now, now, json.dumps({"created_by": payload.get("created_by") or "operator"})),
                )
                connection.execute(
                    """INSERT OR IGNORE INTO commercial_account_settings
                    (id, account_id, enabled, priority, daily_limit_override, deliveries_per_round_override,
                     delay_between_deliveries_override, round_cooldown_override, source_channel_uid_override,
                     current_daily_sent_count, current_round_sent_count, worker_status, created_at, updated_at)
                    VALUES (?, ?, 0, 100, NULL, NULL, NULL, NULL, NULL, 0, 0, 'idle', ?, ?)""",
                    (f"acct_settings_{uuid4().hex[:12]}", account_id, now, now),
                )
                identity_id = f"identity_{uuid4().hex[:12]}"
                connection.execute(
                    """INSERT INTO commercial_browser_identities
                    (identity_id, account_id, platform, profile_path, normalized_profile_path, locale, timezone_id,
                     viewport_width, viewport_height, device_scale_factor, chrome_channel, identity_version, enabled,
                     validation_status, created_at, updated_at)
                    VALUES (?, ?, 'bale', ?, ?, 'fa-IR', 'Asia/Tehran', 1365, 768, 1, 'chrome', 1, 1, 'provisioned', ?, ?)""",
                    (identity_id, account_id, profile_path, profile_compare_key(profile_path), now, now),
                )
                connection.execute("UPDATE bale_operational_accounts SET browser_identity_id = ? WHERE account_id = ?", (identity_id, account_id))
                connection.execute(
                    """INSERT OR IGNORE INTO commercial_account_health
                    (account_id, health_status, consecutive_failures, auth_failure_count, session_failure_count,
                     profile_conflict_count, platform_warning_count, rate_limit_count, manual_review_required, updated_at)
                    VALUES (?, 'unknown', 0, 0, 0, 0, 0, 0, 0, ?)""",
                    (account_id, now),
                )
                if payload.get("onboarding_batch_id"):
                    connection.execute(
                        "INSERT OR IGNORE INTO bale_onboarding_batch_accounts VALUES (?, ?, 'new', ?)",
                        (payload["onboarding_batch_id"], account_id, now),
                    )
                connection.commit()
            self.account_store.create_account({
                "account_id": account_id,
                "phone": preflight["normalized_identifier"],
                "username_or_number": preflight["normalized_identifier"],
                "status": "disabled",
                "browser_provider": "native_chrome",
                "enabled_for_scheduling": False,
            })
            created_registry = True
            Path(preflight["canonical_profile_path"]).mkdir(parents=True, exist_ok=False)
            created_profile = True
            with self.connection() as connection:
                connection.execute("UPDATE bale_operational_accounts SET lifecycle_status='login_required', updated_at=? WHERE account_id=?", (utc_now(), account_id))
                connection.execute("UPDATE bale_onboarding_operations SET status='completed', current_step='complete', progress_json=?, updated_at=? WHERE operation_id=?", (json.dumps(["validated", "metadata", "registry", "profile_reserved", "complete"]), utc_now(), operation_id))
                self._audit(connection, account_id, payload.get("onboarding_batch_id"), operation_id, "account_provisioned", "اکانت با زمان‌بندی غیرفعال ایجاد شد.")
                connection.commit()
            return {"operation": self.get_operation(operation_id), "account": self.get_account(account_id), "idempotent_replay": False}
        except Exception as exc:
            if created_profile:
                try:
                    Path(preflight["canonical_profile_path"]).rmdir()
                except OSError:
                    pass
            if created_registry:
                try:
                    self.account_store.delete_account(account_id)
                except Exception:
                    pass
            with self.connection() as connection:
                connection.execute("DELETE FROM commercial_browser_identities WHERE account_id=?", (account_id,))
                connection.execute("DELETE FROM commercial_account_health WHERE account_id=?", (account_id,))
                connection.execute("DELETE FROM commercial_account_settings WHERE account_id=?", (account_id,))
                connection.execute("DELETE FROM bale_operational_accounts WHERE account_id=?", (account_id,))
                connection.execute("DELETE FROM bale_onboarding_batch_accounts WHERE account_id=?", (account_id,))
                connection.execute("UPDATE bale_onboarding_operations SET status='failed', error_code=?, safe_error_message=?, updated_at=? WHERE operation_id=?", ("provisioning_failed", "ایجاد اکانت کامل نشد؛ تغییرات جدید بازگردانده شد.", utc_now(), operation_id))
                connection.commit()
            raise BaleOnboardingError("provisioning_failed", "ایجاد اکانت کامل نشد؛ تغییرات جدید بازگردانده شد.", {"cause": type(exc).__name__}) from exc

    def reconcile(self, account_id: str) -> dict[str, Any]:
        registry = self.account_store.get_account(account_id)
        profile_path = self._profile_path(account_id)
        result: dict[str, Any] = {
            "account_id": account_id,
            "masked_identifier": mask_identifier(str((registry or {}).get("phone") or (registry or {}).get("username_or_number") or "")),
            "registry_present": registry is not None,
            "profile_present": profile_path.exists(),
            "profile_is_canonical": True,
            "conflicts": [],
        }
        if self.database_path.exists():
            with self.connection(initialize=False) as connection:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table, field in [
                    ("commercial_account_settings", "settings_present"),
                    ("commercial_browser_identities", "identity_present"),
                    ("commercial_account_health", "health_present"),
                    ("commercial_account_worker_locks", "worker_lock_present"),
                ]:
                    result[field] = bool(table in tables and connection.execute(f"SELECT 1 FROM {table} WHERE account_id=?", (account_id,)).fetchone())
                identity = connection.execute("SELECT * FROM commercial_browser_identities WHERE account_id=?", (account_id,)).fetchone() if "commercial_browser_identities" in tables else None
                result["profile_owner_matches"] = bool(identity and profile_compare_key(identity["profile_path"]) == profile_compare_key(profile_path))
                health = connection.execute("SELECT * FROM commercial_account_health WHERE account_id=?", (account_id,)).fetchone() if "commercial_account_health" in tables else None
                verified_at = health["last_authentication_verified_at"] if health and "last_authentication_verified_at" in health.keys() else None
                result["authentication_verified"] = bool(verified_at)
                result["verification_expired"] = self._expired(verified_at)
        for field in ("settings_present", "identity_present", "health_present", "worker_lock_present", "profile_owner_matches", "authentication_verified", "verification_expired"):
            result.setdefault(field, False)
        if result["identity_present"] and not result["profile_owner_matches"]:
            result["conflicts"].append("profile_identity_mismatch")
        if not result["registry_present"]:
            result["conflicts"].append("registry_missing")
        result["session_persistence_known"] = bool((self.get_account(account_id) or {}).get("session_persistence_status") not in {None, "unknown"})
        result["safe_to_preserve"] = result["registry_present"] and not result["conflicts"]
        result["recommended_action"] = "preserve_and_verify" if result["safe_to_preserve"] else "manual_conflict_review"
        result["read_only"] = True
        return result

    def list_accounts(self) -> dict[str, Any]:
        self._ensure_schema()
        registry = {str(item["account_id"]): item for item in self.account_store.list_accounts()}
        with self.connection() as connection:
            rows = {str(row["account_id"]): dict(row) for row in connection.execute("SELECT * FROM bale_operational_accounts ORDER BY created_at, account_id")}
        ids = sorted(set(registry) | set(rows))
        items = [self._merge_account(account_id, registry.get(account_id), rows.get(account_id)) for account_id in ids]
        return {"items": items, "summary": self._summary(items), "configuration": self.configuration()}

    def get_account(self, account_id: str) -> dict[str, Any] | None:
        self._ensure_schema()
        registry = self.account_store.get_account(account_id)
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM bale_operational_accounts WHERE account_id=?", (account_id,)).fetchone()
        if registry is None and row is None:
            return None
        return self._merge_account(account_id, registry, dict(row) if row else None)

    def transition(self, account_id: str, target: str, *, reason: str | None = None) -> dict[str, Any]:
        self._ensure_operational_record(account_id)
        account = self.get_account(account_id)
        if not account:
            raise BaleOnboardingError("account_not_found", "اکانت پیدا نشد.")
        current = str(account["lifecycle_status"])
        if target not in LIFECYCLE_TRANSITIONS.get(current, set()):
            raise BaleOnboardingError("invalid_lifecycle_transition", "تغییر وضعیت در حالت فعلی مجاز نیست.", {"from": current, "to": target})
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                "UPDATE bale_operational_accounts SET lifecycle_status=?, retired_at=?, scheduling_enabled=CASE WHEN ?='retired' THEN 0 ELSE scheduling_enabled END, blocking_reason=?, updated_at=? WHERE account_id=?",
                (target, now if target == "retired" else None, target, reason, now, account_id),
            )
            if target == "retired":
                connection.execute("UPDATE commercial_account_settings SET enabled=0, updated_at=? WHERE account_id=?", (now, account_id))
            self._audit(connection, account_id, None, None, f"lifecycle_{target}", "وضعیت چرخه عمر اکانت تغییر کرد.", {"from": current, "to": target, "reason": reason})
            connection.commit()
        return self.get_account(account_id) or {}

    def set_scheduling(self, account_id: str, enabled: bool) -> dict[str, Any]:
        account = self.get_account(account_id)
        if not account:
            raise BaleOnboardingError("account_not_found", "اکانت پیدا نشد.")
        if enabled and not account["eligible_if_enabled"]:
            raise BaleOnboardingError("account_not_eligible", "اکانت هنوز شرایط فعال‌سازی زمان‌بندی را ندارد.", {"reasons": account["eligibility_reasons"]})
        with self.connection() as connection:
            connection.execute("UPDATE bale_operational_accounts SET scheduling_enabled=?, updated_at=? WHERE account_id=?", (int(enabled), utc_now(), account_id))
            connection.execute("UPDATE commercial_account_settings SET enabled=?, updated_at=? WHERE account_id=?", (int(enabled), utc_now(), account_id))
            self._audit(connection, account_id, None, None, "scheduling_enabled" if enabled else "scheduling_disabled", "وضعیت زمان‌بندی تغییر کرد.")
            connection.commit()
        return self.get_account(account_id) or {}

    def disable_account(self, account_id: str, reason: str = "disabled_by_operator") -> dict[str, Any]:
        account = self.get_account(account_id)
        if not account:
            raise BaleOnboardingError("account_not_found", "اکانت پیدا نشد.")
        if account["lifecycle_status"] == "retired":
            raise BaleOnboardingError("retired_account", "اکانت بازنشسته قابل تغییر نیست.")
        if account["lifecycle_status"] == "login_in_progress":
            raise BaleOnboardingError("active_maintenance_session", "ابتدا مرورگر ورود را به‌صورت امن ببندید.")
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                "UPDATE bale_operational_accounts SET lifecycle_status='disabled', scheduling_enabled=0, blocking_reason=?, updated_at=? WHERE account_id=?",
                (reason, now, account_id),
            )
            connection.execute("UPDATE commercial_account_settings SET enabled=0, updated_at=? WHERE account_id=?", (now, account_id))
            self._audit(connection, account_id, None, None, "account_disabled", "اکانت بدون حذف پروفایل غیرفعال شد.", {"reason": reason})
            connection.commit()
        return self.get_account(account_id) or {}

    def record_authentication_open(self, account_id: str, result: dict[str, Any], purpose: str = "login") -> dict[str, Any]:
        self._ensure_operational_record(account_id)
        session_id = str(result.get("maintenance_session_id") or "")
        if not session_id:
            raise BaleOnboardingError("maintenance_session_missing", "شناسه نشست ورود دریافت نشد.")
        now = utc_now()
        expires = (datetime.now(timezone.utc) + timedelta(seconds=300)).isoformat()
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO bale_maintenance_sessions
                (maintenance_session_id, account_id, purpose, status, backend_instance_id, runtime_session_id,
                 opened_at, heartbeat_at, expires_at, safe_diagnostics_json)
                VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET maintenance_session_id=excluded.maintenance_session_id,
                 purpose=excluded.purpose, status='active', backend_instance_id=excluded.backend_instance_id,
                 runtime_session_id=excluded.runtime_session_id, opened_at=excluded.opened_at,
                 heartbeat_at=excluded.heartbeat_at, expires_at=excluded.expires_at,
                 closed_at=NULL, safe_diagnostics_json=excluded.safe_diagnostics_json""",
                (session_id, account_id, purpose, self.backend_instance_id, result.get("runtime_session_id"), now, now, expires, json.dumps({"auth_state": (result.get("auth") or {}).get("auth_state"), "profile_lock_owned": True})),
            )
            connection.execute("UPDATE bale_operational_accounts SET lifecycle_status='login_in_progress', updated_at=? WHERE account_id=? AND lifecycle_status!='retired'", (now, account_id))
            self._audit(connection, account_id, None, None, f"{purpose}_opened", "مرورگر کنترل‌شده ورود باز شد.")
            connection.commit()
        return self.get_account(account_id) or {}

    def acquire_profile_launch_lock(self, account_id: str, ttl_seconds: int = 300) -> dict[str, Any]:
        self._ensure_operational_record(account_id)
        account = self.get_account(account_id)
        if not account or account["retired"]:
            raise BaleOnboardingError("account_not_launchable", "اکانت برای بازکردن مرورگر مجاز نیست.")
        normalized_path = profile_compare_key(account["canonical_profile_path"])
        owner_id = f"login_{uuid4().hex[:16]}"
        now_dt = datetime.now(timezone.utc)
        max_login_concurrency = int(self.configuration()["max_login_concurrency"])
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active_count = int(connection.execute("SELECT COUNT(*) FROM bale_profile_launch_locks WHERE expires_at > ?", (now_dt.isoformat(),)).fetchone()[0])
            if active_count >= max_login_concurrency:
                connection.rollback()
                raise BaleOnboardingError("login_concurrency_reached", "ظرفیت هم‌زمانی ورود تکمیل است؛ ابتدا مرورگر فعلی را ببندید.")
            existing = connection.execute("SELECT * FROM bale_profile_launch_locks WHERE normalized_profile_path=? OR account_id=?", (normalized_path, account_id)).fetchone()
            if existing and str(existing["expires_at"]) > now_dt.isoformat():
                connection.rollback()
                raise BaleOnboardingError("profile_launch_locked", "این پروفایل در اختیار یک عملیات دیگر است.")
            if existing:
                connection.execute("DELETE FROM bale_profile_launch_locks WHERE normalized_profile_path=?", (existing["normalized_profile_path"],))
            connection.execute(
                "INSERT INTO bale_profile_launch_locks VALUES (?, ?, ?, ?, ?, ?, ?)",
                (normalized_path, account_id, owner_id, self.backend_instance_id, now_dt.isoformat(), now_dt.isoformat(), (now_dt + timedelta(seconds=ttl_seconds)).isoformat()),
            )
            connection.commit()
        return {"account_id": account_id, "owner_id": owner_id, "normalized_profile_path": normalized_path, "expires_at": (now_dt + timedelta(seconds=ttl_seconds)).isoformat()}

    def release_profile_launch_lock(self, account_id: str, owner_id: str | None = None) -> bool:
        with self.connection() as connection:
            if owner_id:
                cursor = connection.execute("DELETE FROM bale_profile_launch_locks WHERE account_id=? AND owner_id=?", (account_id, owner_id))
            else:
                cursor = connection.execute("DELETE FROM bale_profile_launch_locks WHERE account_id=? AND backend_instance_id=?", (account_id, self.backend_instance_id))
            connection.commit()
            return cursor.rowcount > 0

    def record_authentication_status(self, maintenance_session_id: str, result: dict[str, Any]) -> None:
        now = utc_now()
        auth_state = str((result.get("auth") or {}).get("auth_state") or "unknown_auth_state")
        with self.connection() as connection:
            connection.execute(
                "UPDATE bale_maintenance_sessions SET heartbeat_at=?, expires_at=?, last_auth_state=?, safe_diagnostics_json=? WHERE maintenance_session_id=?",
                (now, (datetime.now(timezone.utc) + timedelta(seconds=300)).isoformat(), auth_state, json.dumps({"auth_state": auth_state, "closed": bool(result.get("closed"))}), maintenance_session_id),
            )
            connection.commit()

    def record_authentication_verified(self, maintenance_session_id: str, result: dict[str, Any]) -> dict[str, Any]:
        with self.connection() as connection:
            session = connection.execute("SELECT * FROM bale_maintenance_sessions WHERE maintenance_session_id=?", (maintenance_session_id,)).fetchone()
            if not session:
                raise BaleOnboardingError("maintenance_session_not_found", "نشست ورود پیدا نشد.")
            account_id = str(session["account_id"])
            verified = bool(result.get("verified"))
            now = utc_now()
            if verified:
                expires = (datetime.now(timezone.utc) + timedelta(seconds=self.auth_ttl_seconds)).isoformat()
                purpose = str(session["purpose"])
                persistence = "verified" if purpose == "persistence" else "pending"
                lifecycle = "ready" if purpose == "persistence" else "persistence_check_required"
                connection.execute(
                    """UPDATE bale_operational_accounts SET authentication_status='authenticated',
                    authentication_verified_at=?, verification_expires_at=?, session_persistence_status=?,
                    lifecycle_status=?, health_status='healthy', blocking_reason=NULL, last_error_code=NULL,
                    safe_error_message=NULL, updated_at=? WHERE account_id=?""",
                    (now, expires, persistence, lifecycle, now, account_id),
                )
            else:
                connection.execute(
                    """UPDATE bale_operational_accounts SET authentication_status='unverified',
                    lifecycle_status='authentication_failed', health_status='auth_required',
                    last_error_code=?, safe_error_message=?, updated_at=? WHERE account_id=?""",
                    ((result.get("auth") or {}).get("error_code") or "authentication_required", "تأیید ورود کامل نشد.", now, account_id),
                )
            self._audit(connection, account_id, None, None, "authentication_verified" if verified else "authentication_failed", "نتیجه بررسی ورود ثبت شد.")
            connection.commit()
        return self.get_account(account_id) or {}

    def record_authentication_closed(self, maintenance_session_id: str, result: dict[str, Any]) -> dict[str, Any] | None:
        with self.connection() as connection:
            session = connection.execute("SELECT * FROM bale_maintenance_sessions WHERE maintenance_session_id=?", (maintenance_session_id,)).fetchone()
            if not session:
                return None
            account_id = str(session["account_id"])
            connection.execute("UPDATE bale_maintenance_sessions SET status='closed', closed_at=?, heartbeat_at=? WHERE maintenance_session_id=?", (utc_now(), utc_now(), maintenance_session_id))
            self._audit(connection, account_id, None, None, "authentication_closed", "مرورگر ورود بسته و مالکیت آزاد شد.")
            connection.commit()
        self.release_profile_launch_lock(account_id)
        return self.get_account(account_id)

    def recover_stale_sessions(self) -> dict[str, Any]:
        now = utc_now()
        results: list[dict[str, Any]] = []
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM bale_maintenance_sessions WHERE status='active' AND expires_at < ?", (now,)).fetchall()
            for row in rows:
                account_id = str(row["account_id"])
                record = resolve_profile_record(account_id) if self.profile_root == PROFILE_ROOT.resolve(strict=False) else {"profile_dir": self._profile_path(account_id)}
                try:
                    processes = list(self.process_inspector(record) or [])
                except Exception:
                    processes = []
                lock = connection.execute("SELECT * FROM bale_profile_launch_locks WHERE account_id=?", (account_id,)).fetchone()
                if processes:
                    classification, action, released = "browser_active_not_reattachable", "close_browser_then_retry_recovery", False
                    connection.execute("UPDATE bale_maintenance_sessions SET status='orphan_browser_active', safe_diagnostics_json=? WHERE maintenance_session_id=?", (json.dumps({"recovery_classification": classification, "active_process_count": len(processes)}), row["maintenance_session_id"]))
                elif lock and str(lock["expires_at"]) >= now:
                    classification, action, released = "profile_lock_conflict", "wait_for_lock_expiry_or_investigate_owner", False
                    connection.execute("UPDATE bale_maintenance_sessions SET status='profile_lock_conflict', safe_diagnostics_json=? WHERE maintenance_session_id=?", (json.dumps({"recovery_classification": classification}), row["maintenance_session_id"]))
                else:
                    classification = "browser_gone" if row["runtime_session_id"] else "stale_metadata"
                    action, released = "ownership_released_safe_to_reopen", True
                    connection.execute("UPDATE bale_maintenance_sessions SET status='stale_released', closed_at=?, safe_diagnostics_json=? WHERE maintenance_session_id=?", (now, json.dumps({"recovery_classification": classification, "profile_modified": False}), row["maintenance_session_id"]))
                    connection.execute("DELETE FROM bale_profile_launch_locks WHERE account_id=?", (account_id,))
                results.append({"maintenance_session_id": str(row["maintenance_session_id"]), "account_id": account_id, "classification": classification, "recommended_action": action, "released": released})
                self._audit(connection, account_id, None, None, "maintenance_session_recovery_checked", "نشست قدیمی با بررسی فرایند و قفل ارزیابی شد؛ محتوای پروفایل تغییر نکرد.", {"classification": classification})
            connection.commit()
        return {"checked_count": len(results), "released_count": sum(int(item["released"]) for item in results), "items": results, "profiles_deleted": False, "reattachment_supported": False, "degraded_recovery": True}

    def create_batch(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._ensure_schema()
        account_ids = list(dict.fromkeys(str(item) for item in payload.get("account_ids") or [] if str(item)))
        target = int(payload.get("target_count") or len(account_ids))
        if target < len(account_ids):
            raise BaleOnboardingError("invalid_target_count", "تعداد هدف نمی‌تواند از تعداد اعضای انتخاب‌شده کمتر باشد.")
        batch_id = f"batch_{uuid4().hex[:16]}"
        now = utc_now()
        with self.connection() as connection:
            connection.execute("INSERT INTO bale_onboarding_batches VALUES (?, ?, 'draft', ?, ?, ?, NULL, NULL, ?)", (batch_id, str(payload.get("name") or "بچ ورود بله"), target, str(payload.get("created_by") or "operator"), now, now))
            for account_id in account_ids:
                connection.execute("INSERT INTO bale_onboarding_batch_accounts VALUES (?, ?, 'existing', ?)", (batch_id, account_id, now))
            self._audit(connection, None, batch_id, None, "batch_created", "بچ ورود ایجاد شد.", {"target_count": target, "account_ids": account_ids})
            connection.commit()
        return self.get_batch(batch_id)

    def list_batches(self) -> dict[str, Any]:
        self._ensure_schema()
        with self.connection() as connection:
            ids = [row[0] for row in connection.execute("SELECT onboarding_batch_id FROM bale_onboarding_batches ORDER BY created_at DESC")]
        return {"items": [self.get_batch(batch_id) for batch_id in ids]}

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        self._ensure_schema()
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM bale_onboarding_batches WHERE onboarding_batch_id=?", (batch_id,)).fetchone()
            if not row:
                raise BaleOnboardingError("batch_not_found", "بچ ورود پیدا نشد.")
            members = [dict(item) for item in connection.execute("SELECT * FROM bale_onboarding_batch_accounts WHERE onboarding_batch_id=? ORDER BY added_at", (batch_id,))]
        accounts = [self.get_account(item["account_id"]) for item in members]
        counts = {"completed_count": 0, "failed_count": 0, "blocked_count": 0, "ready_count": 0, "authenticated_count": 0, "login_required_count": 0}
        for account in [item for item in accounts if item]:
            lifecycle = account["lifecycle_status"]
            counts["ready_count"] += int(lifecycle == "ready")
            counts["completed_count"] += int(lifecycle == "ready")
            counts["failed_count"] += int(lifecycle in {"provisioning_failed", "authentication_failed"})
            counts["blocked_count"] += int(lifecycle == "blocked")
            counts["authenticated_count"] += int(account["authentication_status"] == "authenticated")
            counts["login_required_count"] += int(lifecycle in {"login_required", "verification_expired"})
        return {**dict(row), "account_ids": [item["account_id"] for item in members], "accounts": accounts, **counts, "current_batch_size": len(members), "new_accounts_expected": max(0, int(row["target_count"]) - sum(1 for item in members if item["membership_kind"] == "existing"))}

    def get_operation(self, operation_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM bale_onboarding_operations WHERE operation_id=?", (operation_id,)).fetchone()
        return dict(row) if row else None

    def audit_events(self, account_id: str | None = None, batch_id: str | None = None) -> dict[str, Any]:
        self._ensure_schema()
        filters, params = [], []
        if account_id:
            filters.append("account_id=?")
            params.append(account_id)
        if batch_id:
            filters.append("onboarding_batch_id=?")
            params.append(batch_id)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        with self.connection() as connection:
            rows = [dict(row) for row in connection.execute(f"SELECT * FROM bale_account_audit_events {where} ORDER BY created_at DESC LIMIT 200", params)]
        return {"items": rows}

    def configuration(self) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT configuration_json, revision, updated_at FROM bale_operational_configuration WHERE id='global'"
            ).fetchone()
            if not row:
                now = utc_now()
                payload = dict(DEFAULT_CONFIGURATION)
                payload["authentication_verification_ttl_seconds"] = self.auth_ttl_seconds
                connection.execute(
                    "INSERT INTO bale_operational_configuration VALUES('global', ?, 1, 'system-default', ?)",
                    (json.dumps(payload, sort_keys=True), now),
                )
                connection.commit()
                return {**payload, "revision": 1, "updated_at": now}
            return {**json.loads(row["configuration_json"]), "revision": row["revision"], "updated_at": row["updated_at"]}

    def update_configuration(self, changes: dict[str, Any], actor: str = "operator-ui") -> dict[str, Any]:
        current = self.configuration()
        allowed = set(DEFAULT_CONFIGURATION)
        unknown = sorted(set(changes) - allowed)
        if unknown:
            raise BaleOnboardingError("unknown_configuration", "تنظیم ناشناخته ارسال شد.", {"fields": unknown})
        candidate = {key: current.get(key, value) for key, value in DEFAULT_CONFIGURATION.items()}
        candidate.update(changes)
        integer_ranges = {
            "max_login_concurrency": (1, 20), "max_operational_browser_concurrency": (1, 100),
            "max_active_sessions": (1, 100), "max_worker_concurrency": (1, 100),
            "max_accounts_per_scheduler_cycle": (1, 1000),
            "authentication_verification_ttl_seconds": (60, 2592000),
            "profile_lock_ttl_seconds": (30, 86400),
            "stale_session_recovery_interval_seconds": (10, 86400),
            "cpu_threshold_percent": (20, 100), "memory_threshold_percent": (20, 100),
            "default_hourly_limit": (1, 10000), "default_daily_limit": (1, 100000),
            "default_minimum_delay_seconds": (0, 86400), "default_round_size": (1, 1000),
            "default_cooldown_seconds": (0, 604800),
        }
        for key, (low, high) in integer_ranges.items():
            try:
                candidate[key] = int(candidate[key])
            except (TypeError, ValueError) as exc:
                raise BaleOnboardingError("invalid_configuration", "مقدار تنظیم معتبر نیست.", {"field": key}) from exc
            if not low <= candidate[key] <= high:
                raise BaleOnboardingError("configuration_out_of_range", "مقدار تنظیم خارج از بازه مجاز است.", {"field": key, "minimum": low, "maximum": high})
        try:
            ZoneInfo(str(candidate["timezone"]))
        except Exception as exc:
            raise BaleOnboardingError("invalid_timezone", "منطقه زمانی معتبر نیست.", {"field": "timezone"}) from exc
        if candidate["daily_reset_policy"] != "local_midnight":
            raise BaleOnboardingError("invalid_daily_reset_policy", "سیاست بازنشانی روزانه پشتیبانی نمی‌شود.")
        if candidate["onboarding_mode"] and (candidate["queue_enabled"] or candidate["live_sending_enabled"]):
            raise BaleOnboardingError("onboarding_safety_gate", "در حالت ورود حساب، صف و ارسال زنده باید خاموش بمانند.")
        now = utc_now()
        revision = int(current.get("revision") or 1) + 1
        with self.connection() as connection:
            connection.execute(
                "UPDATE bale_operational_configuration SET configuration_json=?, revision=?, updated_by=?, updated_at=? WHERE id='global'",
                (json.dumps(candidate, sort_keys=True), revision, actor, now),
            )
            self._audit(connection, None, None, None, "operational_configuration_updated", "تنظیمات عملیاتی به‌روزرسانی شد.", {"changed_fields": sorted(changes), "revision": revision})
            connection.commit()
        return {**candidate, "revision": revision, "updated_at": now}

    def rate_limit_eligibility(self, account_id: str, at: datetime | None = None) -> dict[str, Any]:
        now = (at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        config = self.configuration()
        timezone_name = str(config["timezone"])
        local_day = now.astimezone(ZoneInfo(timezone_name)).date().isoformat()
        with self.connection() as connection:
            override = connection.execute("SELECT * FROM bale_account_limit_overrides WHERE account_id=?", (account_id,)).fetchone()
            limits = {
                "hourly_limit": int((override and override["hourly_limit"]) or config["default_hourly_limit"]),
                "daily_limit": int((override and override["daily_limit"]) or config["default_daily_limit"]),
                "minimum_delay_seconds": int((override and override["minimum_delay_seconds"]) or config["default_minimum_delay_seconds"]),
                "round_size": int((override and override["round_size"]) or config["default_round_size"]),
                "cooldown_seconds": int((override and override["cooldown_seconds"]) or config["default_cooldown_seconds"]),
            }
            hourly_since = (now - timedelta(hours=1)).isoformat()
            hourly = int(connection.execute("SELECT COUNT(*) FROM bale_delivery_events WHERE account_id=? AND occurred_at>?", (account_id, hourly_since)).fetchone()[0])
            daily = int(connection.execute("SELECT COUNT(*) FROM bale_delivery_events WHERE account_id=? AND local_day=?", (account_id, local_day)).fetchone()[0])
            last = connection.execute("SELECT occurred_at FROM bale_delivery_events WHERE account_id=? ORDER BY occurred_at DESC LIMIT 1", (account_id,)).fetchone()
        reasons = []
        if hourly >= limits["hourly_limit"]: reasons.append("rolling_hourly_limit_reached")
        if daily >= limits["daily_limit"]: reasons.append("daily_limit_reached")
        if last and (now - datetime.fromisoformat(last["occurred_at"])).total_seconds() < max(limits["minimum_delay_seconds"], limits["cooldown_seconds"]):
            reasons.append("account_cooldown_active")
        account = self.get_account(account_id)
        if not account or not account.get("scheduling_enabled"): reasons.append("account_disabled")
        return {"eligible": not reasons, "reasons": reasons, "hourly_count": hourly, "daily_count": daily, "local_day": local_day, "timezone": timezone_name, **limits}

    def claim_delivery(self, account_id: str, claim_token: str, at: datetime | None = None) -> dict[str, Any]:
        now = (at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        eligibility = self.rate_limit_eligibility(account_id, now)
        if not eligibility["eligible"]:
            return {"claimed": False, **eligibility}
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT event_id FROM bale_delivery_events WHERE claim_token=?", (claim_token,)).fetchone()
            if existing:
                connection.rollback()
                return {"claimed": True, "idempotent_replay": True, "event_id": existing["event_id"]}
            hourly = int(connection.execute("SELECT COUNT(*) FROM bale_delivery_events WHERE account_id=? AND occurred_at>?", (account_id, (now - timedelta(hours=1)).isoformat())).fetchone()[0])
            daily = int(connection.execute("SELECT COUNT(*) FROM bale_delivery_events WHERE account_id=? AND local_day=?", (account_id, eligibility["local_day"])).fetchone()[0])
            last = connection.execute("SELECT occurred_at FROM bale_delivery_events WHERE account_id=? ORDER BY occurred_at DESC LIMIT 1", (account_id,)).fetchone()
            cooldown_active = bool(last and (now - datetime.fromisoformat(last["occurred_at"])).total_seconds() < max(eligibility["minimum_delay_seconds"], eligibility["cooldown_seconds"]))
            if hourly >= eligibility["hourly_limit"] or daily >= eligibility["daily_limit"] or cooldown_active:
                connection.rollback()
                return {"claimed": False, **self.rate_limit_eligibility(account_id, now)}
            event_id = f"delivery_{uuid4().hex}"
            local_day = now.astimezone(ZoneInfo(eligibility["timezone"])).date().isoformat()
            connection.execute("INSERT INTO bale_delivery_events VALUES(?, ?, ?, ?, ?)", (event_id, account_id, now.isoformat(), local_day, claim_token))
            connection.commit()
        return {"claimed": True, "idempotent_replay": False, "event_id": event_id}

    def scheduler_authentication_available(self, account_id: str) -> bool:
        account = self.get_account(account_id)
        return bool(
            account
            and account["authentication_status"] == "authenticated"
            and not account["verification_expired"]
            and account["session_persistence_status"] == "verified"
            and account["lifecycle_status"] == "ready"
            and not account["retired"]
        )

    def _ensure_schema(self) -> None:
        with self.connection() as connection:
            connection.commit()

    def _ensure_operational_record(self, account_id: str) -> None:
        if self.get_account(account_id) and self._operational_exists(account_id):
            return
        registry = self.account_store.get_account(account_id)
        if not registry:
            raise BaleOnboardingError("account_not_found", "اکانت پیدا نشد.")
        normalized = normalize_bale_identifier(str(registry.get("phone") or registry.get("username_or_number") or ""))
        profile = str(self._profile_path(account_id))
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO bale_operational_accounts
                (account_id, normalized_identifier, masked_identifier, lifecycle_status, scheduling_enabled,
                 authentication_status, session_persistence_status, health_status, canonical_profile_path,
                 normalized_profile_path, onboarding_blocked, created_at, updated_at, audit_metadata_json)
                VALUES (?, ?, ?, 'discovered_existing', 0, 'unverified', 'unknown', 'unknown', ?, ?, 0, ?, ?, '{}')""",
                (account_id, normalized, mask_identifier(normalized), profile, profile_compare_key(profile), now, now),
            )
            connection.commit()

    def _operational_exists(self, account_id: str) -> bool:
        with self.connection() as connection:
            return connection.execute("SELECT 1 FROM bale_operational_accounts WHERE account_id=?", (account_id,)).fetchone() is not None

    def _merge_account(self, account_id: str, registry: dict[str, Any] | None, operational: dict[str, Any] | None) -> dict[str, Any]:
        normalized = str((operational or {}).get("normalized_identifier") or normalize_bale_identifier(str((registry or {}).get("phone") or (registry or {}).get("username_or_number") or "")))
        profile_path = str((operational or {}).get("canonical_profile_path") or self._profile_path(account_id))
        lifecycle = str((operational or {}).get("lifecycle_status") or "discovered_existing")
        verified_at = (operational or {}).get("authentication_verified_at")
        expired = self._expired(verified_at)
        health = str((operational or {}).get("health_status") or "unknown")
        persistence = str((operational or {}).get("session_persistence_status") or "unknown")
        scheduling = bool((operational or {}).get("scheduling_enabled"))
        reasons = []
        if lifecycle != "ready":
            reasons.append("lifecycle_not_ready")
        if expired or not verified_at:
            reasons.append("authentication_verification_required")
        if persistence != "verified":
            reasons.append("session_persistence_not_verified")
        if health in {"auth_required", "blocked", "disabled", "manual_review"}:
            reasons.append(f"health_{health}")
        if not Path(profile_path).exists():
            reasons.append("profile_missing")
        if (operational or {}).get("onboarding_blocked"):
            reasons.append("onboarding_blocked")
        eligible_base = not reasons and lifecycle != "retired"
        return {
            **(registry or {}),
            **(operational or {}),
            "account_id": account_id,
            "platform": "bale",
            "normalized_identifier": normalized,
            "masked_identifier": str((operational or {}).get("masked_identifier") or mask_identifier(normalized)),
            "lifecycle_status": lifecycle,
            "canonical_profile_path": profile_path,
            "profile_present": Path(profile_path).exists(),
            "verification_expired": expired,
            "scheduling_enabled": scheduling,
            "queue_eligible": bool(eligible_base and scheduling),
            "eligible_if_enabled": eligible_base,
            "eligibility_reasons": reasons,
            "authentication_status": str((operational or {}).get("authentication_status") or "unverified"),
            "session_persistence_status": persistence,
            "health_status": health,
            "retired": lifecycle == "retired",
        }

    def _summary(self, items: list[dict[str, Any]]) -> dict[str, int]:
        active_sessions = 0
        if self.database_path.exists():
            with self.connection() as connection:
                active_sessions = int(connection.execute("SELECT COUNT(*) FROM bale_maintenance_sessions WHERE status='active'").fetchone()[0])
        return {
            "total_accounts": len(items),
            "authenticated_accounts": sum(item["authentication_status"] == "authenticated" and not item["verification_expired"] for item in items),
            "login_required_accounts": sum(item["lifecycle_status"] in {"login_required", "verification_expired"} for item in items),
            "ready_accounts": sum(item["lifecycle_status"] == "ready" for item in items),
            "blocked_accounts": sum(item["lifecycle_status"] == "blocked" for item in items),
            "scheduling_enabled_accounts": sum(bool(item["scheduling_enabled"]) for item in items),
            "queue_eligible_accounts": sum(bool(item["queue_eligible"]) for item in items),
            "active_login_sessions": active_sessions,
        }

    def _expired(self, verified_at: str | None) -> bool:
        if not verified_at:
            return False
        try:
            return datetime.fromisoformat(str(verified_at).replace("Z", "+00:00")) + timedelta(seconds=self.auth_ttl_seconds) <= datetime.now(timezone.utc)
        except ValueError:
            return True

    def _audit(self, connection: sqlite3.Connection, account_id: str | None, batch_id: str | None, operation_id: str | None, event_type: str, message: str, metadata: dict[str, Any] | None = None) -> None:
        connection.execute(
            "INSERT INTO bale_account_audit_events VALUES (?, ?, ?, ?, ?, 'operator', ?, ?, ?)",
            (f"event_{uuid4().hex}", account_id, batch_id, operation_id, event_type, message, json.dumps(metadata or {}, ensure_ascii=False), utc_now()),
        )


bale_onboarding_service = BaleOnboardingService()
