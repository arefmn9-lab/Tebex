from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from modules.automation_engine.browser_identity.validation import profile_compare_key
from modules.automation_engine.plugins.bale.plugin import _native_profile_dir

from .account_health import AccountHealthService
from .repository import utc_now


class BaleAuthenticationMaintenanceError(RuntimeError):
    def __init__(self, error_code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.details = details or {}


class BaleAuthenticationMaintenanceService:
    def __init__(
        self,
        *,
        runtime_session_manager: Any,
        browser_identity_resolver: Any,
        account_health: AccountHealthService,
        plugin: Any,
    ) -> None:
        self.runtime_session_manager = runtime_session_manager
        self.browser_identity_resolver = browser_identity_resolver
        self.account_health = account_health
        self.plugin = plugin
        self._sessions: dict[str, dict[str, Any]] = {}
        self._session_by_account: dict[str, str] = {}

    def audit_profile_paths(self, account_id: str) -> dict[str, Any]:
        identity = self.browser_identity_resolver.validate_identity(account_id, require_directory=True)
        identity_record = identity.get("identity") if isinstance(identity.get("identity"), dict) else identity
        identity_profile = str(identity_record.get("profile_path") or "")
        native_profile = str(_native_profile_dir(account_id))
        expected_suffix = str(Path("backend") / "runtime" / "browser_profiles" / account_id)
        paths = {
            "browser_identity_resolver": identity_profile,
            "account_runtime_session_manager": identity_profile,
            "bale_adapter": identity_profile,
            "bale_plugin_isolated_native_chrome_page": native_profile,
            "browser_manager": native_profile,
        }
        normalized = {key: profile_compare_key(value) for key, value in paths.items()}
        expected_key = profile_compare_key(native_profile)
        all_match = all(value == expected_key for value in normalized.values())
        profile_dir = Path(native_profile)
        default_dir = profile_dir / "Default"
        entries = [item.name for item in profile_dir.iterdir()] if profile_dir.exists() else []
        default_entries = [item.name for item in default_dir.iterdir()] if default_dir.exists() else []
        lock_files = [name for name in default_entries + entries if name.upper() in {"LOCK", "LOCKFILE", "LOCK-FILE"} or name.startswith("Singleton")]
        recent_profile_files = []
        for path in [profile_dir / "Local State", default_dir / "Preferences", default_dir / "History", default_dir / "Network"]:
            if path.exists():
                recent_profile_files.append({"name": path.name, "last_write_time": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()})
        return {
            "account_id": account_id,
            "expected_profile_suffix": expected_suffix,
            "paths": paths,
            "normalized_paths": normalized,
            "all_paths_match_expected": all_match,
            "legacy_profile_references": [value for value in paths.values() if profile_compare_key(value) != expected_key],
            "historical_success_used_same_profile": all_match,
            "profile_directory_exists": profile_dir.exists(),
            "default_profile_directory_exists": default_dir.exists(),
            "profile_top_level_entries": sorted(entries)[:80],
            "default_profile_state_markers": sorted(
                name for name in default_entries if name in {"Preferences", "History", "Network", "Local Storage", "IndexedDB", "Sessions", "Service Worker"}
            ),
            "lock_files_present": lock_files,
            "recent_profile_files": recent_profile_files,
            "profile_subdirectory_selected": "Default",
            "secrets_exposed": False,
            "browser_identity_validation": {"ok": bool(identity.get("ok", identity.get("valid", False))), "validation_errors": identity.get("validation_errors", [])},
        }

    def open(self, account_id: str) -> dict[str, Any]:
        audit = self.audit_profile_paths(account_id)
        if not audit["all_paths_match_expected"]:
            raise BaleAuthenticationMaintenanceError("profile_path_mismatch", "Bale profile paths do not match", audit)
        if self._session_by_account.get(account_id):
            raise BaleAuthenticationMaintenanceError("maintenance_session_already_active", "A Bale authentication maintenance session is already active for this account")
        if self.runtime_session_manager.get_session(account_id) is not None:
            raise BaleAuthenticationMaintenanceError("profile_session_already_active", "Account profile already has an active runtime session")

        owner_token = f"auth_maintenance_{uuid4().hex[:12]}"
        worker_round_id = f"auth_maintenance_{uuid4().hex[:12]}"
        session = self.runtime_session_manager.acquire_or_create_session(
            account_id,
            owner_token,
            worker_round_id,
            {"platform": "bale", "session_reuse_enabled": True, "resource_guard_enabled": True},
        )
        maintenance_session_id = f"bale_auth_{uuid4().hex[:12]}"
        page = session.page
        try:
            if page is not None and hasattr(page, "goto"):
                page.goto(self.plugin.web_url, wait_until="load")
            self._dismiss_install_help_prompt(page)
            auth = self._classify(page)
            if auth.get("auth_state") in {"login_required", "verification_code_required", "qr_login_required"}:
                self.account_health.record_failure(account_id, {"error_code": "authentication_required", "error_domain": "authentication", "error_message": "Manual Bale authentication is required"})
            payload = {
                "maintenance_session_id": maintenance_session_id,
                "account_id": account_id,
                "runtime_session_id": session.session_id,
                "owner_token": owner_token,
                "worker_round_id": worker_round_id,
                "profile_path": session.profile_path,
                "opened_at": utc_now(),
                "last_checked_at": utc_now(),
                "auth": auth,
                "audit": audit,
                "closed": False,
            }
            self._sessions[maintenance_session_id] = payload
            self._session_by_account[account_id] = maintenance_session_id
            return self._safe_session_payload(payload)
        except Exception:
            self.runtime_session_manager.close_session(session)
            raise

    def status(self, maintenance_session_id: str) -> dict[str, Any]:
        session_payload = self._require(maintenance_session_id)
        runtime = self.runtime_session_manager.get_session(str(session_payload["account_id"]))
        if runtime is None:
            session_payload["closed"] = True
            self._session_by_account.pop(str(session_payload["account_id"]), None)
            return self._safe_session_payload({**session_payload, "auth": {"auth_state": "unknown_auth_state", "authenticated": False, "diagnostics_consistent": False, "error_code": "session_not_found"}})
        try:
            self.runtime_session_manager.health_check(runtime)
        except Exception as exc:
            self.close(maintenance_session_id)
            return self._safe_session_payload({**session_payload, "closed": True, "auth": {"auth_state": "unknown_auth_state", "authenticated": False, "diagnostics_consistent": False, "error_code": getattr(exc, "error_code", "session_closed")}})
        auth = self._classify(runtime.page)
        session_payload["auth"] = auth
        session_payload["last_checked_at"] = utc_now()
        return self._safe_session_payload(session_payload)

    def verify(self, maintenance_session_id: str) -> dict[str, Any]:
        session_payload = self._require(maintenance_session_id)
        runtime = self.runtime_session_manager.get_session(str(session_payload["account_id"]))
        if runtime is None:
            raise BaleAuthenticationMaintenanceError("maintenance_session_closed", "Maintenance session is closed")
        self.runtime_session_manager.health_check(runtime)
        page = runtime.page
        if page is not None and hasattr(page, "goto"):
            page.goto(self.plugin.web_url, wait_until="load")
        self._dismiss_install_help_prompt(page)
        auth = self._classify(page)
        contacts = self._verify_contacts_available(page)
        if contacts.get("ok") and page is not None and hasattr(page, "goto"):
            page.goto(self.plugin.web_url, wait_until="load")
        verified = bool(auth.get("authenticated") and auth.get("chat_shell_visible") and contacts.get("contacts_ui_available"))
        if verified:
            self.account_health.record_success(str(session_payload["account_id"]))
        else:
            self.account_health.record_failure(str(session_payload["account_id"]), {"error_code": auth.get("error_code") or "authentication_required", "error_domain": "authentication", "error_message": "Bale authentication verification failed"})
        session_payload["auth"] = {**auth, "contacts_ui_available": bool(contacts.get("contacts_ui_available"))}
        session_payload["last_checked_at"] = utc_now()
        return {**self._safe_session_payload(session_payload), "verified": verified, "contacts_check": contacts}

    def close(self, maintenance_session_id: str) -> dict[str, Any]:
        session_payload = self._sessions.get(maintenance_session_id)
        if session_payload is None:
            return {"ok": True, "maintenance_session_id": maintenance_session_id, "closed": False, "already_closed": True}
        account_id = str(session_payload["account_id"])
        if session_payload.get("closed"):
            self._session_by_account.pop(account_id, None)
            return {"ok": True, "maintenance_session_id": maintenance_session_id, "closed": False, "already_closed": True}
        runtime = self.runtime_session_manager.get_session(account_id)
        close_result = {"ok": True, "closed": False, "reason": "runtime_session_not_found"}
        if runtime is not None:
            close_result = self.runtime_session_manager.close_session(runtime)
        session_payload["closed"] = True
        session_payload["closed_at"] = utc_now()
        self._session_by_account.pop(account_id, None)
        return {"ok": bool(close_result.get("ok", True)), "maintenance_session_id": maintenance_session_id, "closed": bool(close_result.get("closed")), "close_result": close_result}

    def list_sessions(self) -> dict[str, Any]:
        return {"items": [self._safe_session_payload(item) for item in self._sessions.values() if not item.get("closed")]}

    def _require(self, maintenance_session_id: str) -> dict[str, Any]:
        session = self._sessions.get(maintenance_session_id)
        if session is None:
            raise BaleAuthenticationMaintenanceError("maintenance_session_not_found", "Bale authentication maintenance session was not found")
        return session

    def _classify(self, page: Any) -> dict[str, Any]:
        return self.plugin.classify_authentication_state(page, timeout_ms=3000)

    def _dismiss_install_help_prompt(self, page: Any) -> None:
        if page is None:
            return
        selector = self.plugin._first_visible_selector(page, ["text=متوجه شدم", "button:has-text('متوجه شدم')"], timeout_ms=750)
        if selector:
            self.plugin._click_if_possible(page, selector)

    def _verify_contacts_available(self, page: Any) -> dict[str, Any]:
        if page is None:
            return {"ok": False, "contacts_ui_available": False, "error_code": "page_not_found"}
        contacts_url = f"{self.plugin.web_url}/contacts"
        try:
            if hasattr(page, "goto"):
                page.goto(contacts_url, wait_until="load")
            available = bool(self.plugin._contacts_ui_visible(page))
            return {"ok": available, "contacts_ui_available": available, "page_url": getattr(page, "url", "")}
        except Exception as exc:
            return {"ok": False, "contacts_ui_available": False, "error_code": "contacts_navigation_failed", "error_message": str(exc)}

    def _safe_session_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "maintenance_session_id": payload.get("maintenance_session_id"),
            "account_id": payload.get("account_id"),
            "runtime_session_id": payload.get("runtime_session_id"),
            "profile_path": payload.get("profile_path"),
            "opened_at": payload.get("opened_at"),
            "last_checked_at": payload.get("last_checked_at"),
            "closed": bool(payload.get("closed")),
            "auth": payload.get("auth") or {},
            "profile_lock": {
                "owned": not bool(payload.get("closed")),
                "owner": "bale_authentication_maintenance" if not payload.get("closed") else None,
            },
            "secrets_exposed": False,
            "audit": payload.get("audit"),
        }
