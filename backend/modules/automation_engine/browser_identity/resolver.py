from __future__ import annotations

from pathlib import Path
from typing import Any

from modules.automation_engine.plugins.bale.account_store import bale_account_store

from .repository import BrowserIdentityRepository
from .validation import default_profile_path, normalize_profile_path, profile_compare_key, validate_identity_payload


class BrowserIdentityError(RuntimeError):
    def __init__(self, error_code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.details = details or {}


class BrowserIdentityResolver:
    def __init__(self, repository: BrowserIdentityRepository | None = None) -> None:
        self.repository = repository or BrowserIdentityRepository()

    def get_or_create(self, account_id: str, platform: str = "bale") -> dict[str, Any]:
        existing = self.repository.get_by_account(account_id)
        if existing:
            return existing
        profile_path = normalize_profile_path(default_profile_path(account_id), account_id)
        Path(profile_path).mkdir(parents=True, exist_ok=True)
        return self.repository.upsert_identity(
            {
                "account_id": account_id,
                "platform": platform,
                "profile_path": profile_path,
                "normalized_profile_path": profile_compare_key(profile_path),
                "validation_status": "adopted_existing_profile" if Path(profile_path).exists() else "pending",
            }
        )

    def validate_identity(self, account_id: str, require_directory: bool = True) -> dict[str, Any]:
        identity = self.get_or_create(account_id)
        errors = self.validate_record(identity, require_directory=require_directory)
        status = "valid" if not errors else "invalid"
        updated = self.repository.upsert_identity(
            {
                **identity,
                "validation_status": status,
                "last_validated_at": __import__("modules.automation_engine.commercial_queue.repository", fromlist=["utc_now"]).utc_now(),
                "last_validation_error_code": errors[0] if errors else None,
                "last_validation_error_message": ", ".join(errors) if errors else None,
            }
        )
        return {"identity": updated, "ok": not errors, "validation_errors": errors}

    def validate_record(self, identity: dict[str, Any], require_directory: bool = True, allow_disabled: bool = False) -> list[str]:
        errors: list[str] = []
        try:
            normalized = normalize_profile_path(identity.get("profile_path"), identity.get("account_id"))
        except ValueError as exc:
            return [str(exc)]
        if profile_compare_key(normalized) != str(identity.get("normalized_profile_path") or "").casefold():
            errors.append("browser_identity_mismatch")
        if not allow_disabled and not bool(identity.get("enabled", True)):
            errors.append("browser_identity_disabled")
        if require_directory and not Path(normalized).exists():
            errors.append("profile_directory_missing")
        owner = self.repository.get_by_profile_key(profile_compare_key(normalized))
        if owner and owner.get("account_id") != identity.get("account_id"):
            errors.append("profile_owned_by_another_account")
        errors.extend(validate_identity_payload(identity))
        return errors

    def update_identity(self, account_id: str, payload: dict[str, Any], active_session_exists: bool = False) -> dict[str, Any]:
        if active_session_exists:
            raise BrowserIdentityError("identity_validation_failed", "Cannot edit identity while an active session exists")
        current = self.get_or_create(account_id)
        profile_path = normalize_profile_path(payload.get("profile_path", current["profile_path"]), account_id)
        record = {**current, **payload, "account_id": account_id, "profile_path": profile_path, "normalized_profile_path": profile_compare_key(profile_path)}
        errors = self.validate_record(record, require_directory=False, allow_disabled=True)
        if errors:
            raise BrowserIdentityError(errors[0], "Identity update rejected", {"validation_errors": errors})
        return self.repository.upsert_identity(record, increment_version=True)

    def verify_launch_allowed(self, account_id: str, worker_round_id: str | None = None) -> dict[str, Any]:
        identity = self.get_or_create(account_id)
        errors = self.validate_record(identity, require_directory=False)
        if errors:
            raise BrowserIdentityError(errors[0], "Browser identity validation failed", {"validation_errors": errors, "identity": identity})
        return identity

    def migration_preview(self, dry_run: bool = True) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        seen: dict[str, str] = {}
        for account in bale_account_store.list_accounts():
            account_id = str(account["account_id"])
            profile_path = normalize_profile_path(default_profile_path(account_id), account_id)
            key = profile_compare_key(profile_path)
            item = {
                "account_id": account_id,
                "platform": "bale",
                "profile_path": profile_path,
                "normalized_profile_path": key,
                "exists": Path(profile_path).exists(),
                "action": "would_create_identity" if self.repository.get_by_account(account_id) is None else "already_exists",
            }
            if key in seen and seen[key] != account_id:
                conflict = {"account_id": account_id, "other_account_id": seen[key], "profile_path": profile_path, "error_code": "profile_path_conflict"}
                conflicts.append(conflict)
                item["conflict"] = conflict
            seen[key] = account_id
            items.append(item)
            if not dry_run and not item.get("conflict"):
                self.get_or_create(account_id)
        return {"dry_run": dry_run, "changed": False if dry_run else True, "items": items, "conflicts": conflicts}
