from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from modules.automation_engine.browser_identity.validation import profile_compare_key
from modules.automation_engine.plugins.bale.plugin import _native_profile_dir

from .account_health import AccountHealthService
from .repository import utc_now
from .bale_identity import BaleOwnIdentityClassifier
from modules.automation_engine.plugins.bale.account_store import bale_account_store


logger = logging.getLogger(__name__)

# Bale is a SPA: DOM readiness is sufficient evidence to begin bounded
# classification.  Navigation is never allowed to wait for an unbounded
# network/load event.
BALE_BOOTSTRAP_NAVIGATION_TIMEOUT_MS = max(
    1_000,
    int(os.environ.get("CLINICOS_BALE_BOOTSTRAP_NAVIGATION_TIMEOUT_MS", "15000")),
)


class BaleAuthenticationMaintenanceError(RuntimeError):
    def __init__(self, error_code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.details = details or {}

class FakeBaleAuthenticationMaintenanceService:
    """Explicit no-browser implementation for isolated tests and UI smoke runs."""
    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        configured_mode = str(os.environ.get("CLINICOS_FAKE_BALE_AUTH_MODE") or "verification_code_required").strip().casefold()
        self.auth_mode = configured_mode if configured_mode in {"authenticated", "login_required", "verification_code_required"} else "verification_code_required"
        self.failure_suffixes = tuple(
            suffix for suffix in (
                "".join(character for character in item.strip() if character.isdigit())
                for item in str(os.environ.get("CLINICOS_FAKE_BALE_AUTH_FAILURE_SUFFIXES") or "").split(",")
            )
            if suffix
        )

    def audit_profile_paths(self, account_id: str) -> dict[str, Any]:
        return {"account_id": account_id, "all_paths_match_expected": True, "mock_mode": True, "secrets_exposed": False}

    def open(self, account_id: str) -> dict[str, Any]:
        session_id = f"fake_bale_auth_{uuid4().hex[:12]}"
        failure = self._is_configured_failure(account_id)
        if failure:
            auth = {
                "auth_state": "login_required",
                "authenticated": False,
                "chat_shell_visible": False,
                "chat_list_ready": False,
                "error_code": "fake_session_recheck_failure",
            }
        elif self.auth_mode == "authenticated":
            auth = {
                "auth_state": "authenticated",
                "authenticated": True,
                "chat_shell_visible": True,
                "chat_list_ready": True,
                "chat_readiness_state": "bale_authenticated_and_chats_loaded",
                "chat_row_count": 1,
            }
        else:
            auth = {
                "auth_state": self.auth_mode,
                "authenticated": False,
                "chat_shell_visible": False,
                "chat_list_ready": False,
            }
        payload = {
            "maintenance_session_id": session_id,
            "account_id": account_id,
            "runtime_session_id": "fake-runtime",
            "closed": False,
            "auth": auth,
            "state": "authenticated_shell_detected" if auth["authenticated"] else auth["auth_state"],
            "terminal": failure,
            "retryable": not failure,
            "mock_mode": True,
        }
        self.sessions[session_id] = payload
        return dict(payload)

    def prepare_open(self, account_id: str) -> dict[str, Any]:
        return {"action": "new", "account_id": account_id}

    def status(self, maintenance_session_id: str) -> dict[str, Any]:
        return dict(self._require(maintenance_session_id))

    def verify(self, maintenance_session_id: str) -> dict[str, Any]:
        payload = self._require(maintenance_session_id)
        if self._is_configured_failure(str(payload["account_id"])):
            payload.update({"verified": False, "terminal": True, "retryable": False})
            return {
                **payload,
                "identity_check": {
                    "status": "unavailable",
                    "verified_match": False,
                    "error_code": "fake_session_recheck_failure",
                    "mock_mode": True,
                },
            }
        payload.update({
            "auth": {
                "auth_state": "authenticated",
                "authenticated": True,
                "chat_shell_visible": True,
                "chat_list_ready": True,
                "chat_readiness_state": "bale_authenticated_and_chats_loaded",
                "chat_row_count": 1,
                "contacts_ui_available": True,
            },
            "verified": True,
            "state": "identity_verified",
            "terminal": True,
            "retryable": False,
        })
        return {**payload, "identity_check": {"status": "match", "verified_match": True, "mock_mode": True}}

    def close(self, maintenance_session_id: str) -> dict[str, Any]:
        payload = self.sessions.get(maintenance_session_id)
        if not payload or payload.get("closed"):
            return {"ok": True, "maintenance_session_id": maintenance_session_id, "already_closed": True, "closed": False, "mock_mode": True}
        payload["closed"] = True
        return {"ok": True, "maintenance_session_id": maintenance_session_id, "closed": True, "mock_mode": True}

    def list_sessions(self) -> dict[str, Any]:
        return {"items": [dict(item) for item in self.sessions.values() if not item.get("closed")], "mock_mode": True}

    def _require(self, session_id: str) -> dict[str, Any]:
        if session_id not in self.sessions:
            raise BaleAuthenticationMaintenanceError("maintenance_session_not_found", "Fake maintenance session was not found")
        return self.sessions[session_id]

    def _is_configured_failure(self, account_id: str) -> bool:
        digits = "".join(character for character in account_id if character.isdigit())
        return any(digits.endswith(suffix) for suffix in self.failure_suffixes)


class BaleAuthenticationMaintenanceService:
    def __init__(
        self,
        *,
        runtime_session_manager: Any,
        browser_identity_resolver: Any,
        account_health: AccountHealthService,
        plugin: Any,
        identity_classifier: Any | None = None,
        account_store: Any | None = None,
    ) -> None:
        self.runtime_session_manager = runtime_session_manager
        self.browser_identity_resolver = browser_identity_resolver
        self.account_health = account_health
        self.plugin = plugin
        self.identity_classifier = identity_classifier or BaleOwnIdentityClassifier()
        self.account_store = account_store or bale_account_store
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

    # ============================================================
    # BLOCK: BALE_AUTHENTICATION_OPEN_IDEMPOTENCY
    # PURPOSE:
    # Reuses a healthy same-account authentication session or recovers a stale one.
    # ACCOUNT_SCOPE:
    # Exactly one Bale account; cross-account reuse is forbidden.
    # DEPENDENCIES:
    # AccountRuntimeSessionManager
    # LAYER:
    # SERVICE
    # ============================================================

    # FUNCTION:
    # prepare_open
    # RESPONSIBILITY:
    # Determines whether authentication open should reuse, recover, or create.
    # INPUT:
    # account_id
    # OUTPUT:
    # Action descriptor and optional existing safe session payload
    # SIDE EFFECTS:
    # Closes a stale same-account runtime session through its owning manager.
    def prepare_open(self, account_id: str) -> dict[str, Any]:
        maintenance_session_id = self._session_by_account.get(account_id)
        if not maintenance_session_id:
            logger.info("[BALE_AUTH_OPEN] account_id=%s action=new result=no_maintenance_session", account_id)
            return {"action": "new", "account_id": account_id}

        payload = self._sessions.get(maintenance_session_id)
        if not payload or str(payload.get("account_id") or "") != account_id or payload.get("closed"):
            self._session_by_account.pop(account_id, None)
            logger.info(
                "[BALE_SESSION_RECOVERY] account_id=%s session_id=%s result=stale_mapping_removed",
                account_id,
                maintenance_session_id,
            )
            return {"action": "recovered", "account_id": account_id, "maintenance_session_id": maintenance_session_id}

        runtime = self.runtime_session_manager.get_session(account_id)
        if runtime is not None and str(runtime.session_id) == str(payload.get("runtime_session_id") or ""):
            try:
                self.runtime_session_manager.health_check(runtime)
            except Exception as exc:
                logger.info(
                    "[BALE_SESSION_RECOVERY] account_id=%s session_id=%s result=unhealthy error_code=%s",
                    account_id,
                    maintenance_session_id,
                    getattr(exc, "error_code", type(exc).__name__),
                )
            else:
                existing = self._safe_session_payload(payload)
                existing["reused"] = True
                logger.info(
                    "[BALE_SESSION_REUSE] account_id=%s session_id=%s runtime_session_id=%s result=healthy",
                    account_id,
                    maintenance_session_id,
                    runtime.session_id,
                )
                return {"action": "reuse", "account_id": account_id, "session": existing}

        if runtime is None:
            recover_lease = getattr(self.plugin, "recover_stale_reusable_runtime_lease", None)
            if callable(recover_lease):
                lease_recovery = recover_lease(account_id)
                logger.info(
                    "[BALE_SESSION_RECOVERY] account_id=%s session_id=%s profile_lease_recovered=%s reason=%s",
                    account_id,
                    maintenance_session_id,
                    bool(lease_recovery.get("recovered")),
                    lease_recovery.get("reason"),
                )

        self.close(maintenance_session_id)
        logger.info(
            "[BALE_SESSION_RECOVERY] account_id=%s session_id=%s result=closed",
            account_id,
            maintenance_session_id,
        )
        return {"action": "recovered", "account_id": account_id, "maintenance_session_id": maintenance_session_id}

    # FUNCTION:
    # open
    # RESPONSIBILITY:
    # Creates a new session only when no healthy same-account session is reusable.
    # INPUT:
    # account_id
    # OUTPUT:
    # New or reused authentication session payload
    # SIDE EFFECTS:
    # May create a persistent account browser session.
    def open(self, account_id: str) -> dict[str, Any]:
        prepared = self.prepare_open(account_id)
        if prepared["action"] == "reuse":
            return dict(prepared["session"])
        audit = self.audit_profile_paths(account_id)
        if not audit["all_paths_match_expected"]:
            raise BaleAuthenticationMaintenanceError("profile_path_mismatch", "Bale profile paths do not match", audit)
        if self._session_by_account.get(account_id):
            raise BaleAuthenticationMaintenanceError("maintenance_session_already_active", "A Bale authentication maintenance session is already active for this account")
        if self.runtime_session_manager.get_session(account_id) is not None:
            raise BaleAuthenticationMaintenanceError("profile_session_already_active", "Account profile already has an active runtime session")

        owner_token = f"auth_maintenance_{uuid4().hex[:12]}"
        worker_round_id = f"auth_maintenance_{uuid4().hex[:12]}"
        account_record = self.account_store.get_account(account_id)
        if not account_record:
            raise BaleAuthenticationMaintenanceError(
                "UNKNOWN_ACCOUNT_PROFILE",
                "Bale account profile is not registered",
                {"account_id": account_id, "registration_source": type(self.account_store).__name__},
            )
        session = self.runtime_session_manager.acquire_or_create_session(
            account_id,
            owner_token,
            worker_round_id,
            {
                "platform": "bale",
                "session_reuse_enabled": True,
                "resource_guard_enabled": True,
                # Registration is resolved once by the authoritative onboarding
                # store.  The browser adapter must not perform a second lookup
                # in a potentially different process-global JSON registry.
                "account_record": account_record,
            },
        )
        maintenance_session_id = f"bale_auth_{uuid4().hex[:12]}"
        page = session.page
        session.metadata.setdefault("original_page_id", self.runtime_session_manager._page_id(page))
        session.metadata.setdefault("page_replacement_count", 0)
        try:
            self._navigate_to_bale_shell(page)
            page = self.runtime_session_manager.resolve_live_page(session)
            session.metadata["last_successful_step"] = "bale_loaded"
            self._dismiss_install_help_prompt(page)
            auth = self._classify(page)
            login_visible = bool((auth.get("login_check") or {}).get("login_form_visible"))
            otp_visible = str(auth.get("legacy_auth_state") or auth.get("auth_state")) in {"verification_code_required", "otp_required"}
            if login_visible or otp_visible:
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
                "account_state": self._account_state(auth, opening=True),
                "audit": audit,
                "closed": False,
                "state": self._account_state(auth, opening=True),
                "terminal": False,
                "retryable": True,
            }
            self._sessions[maintenance_session_id] = payload
            self._session_by_account[account_id] = maintenance_session_id
            logger.info(
                "[BALE_AUTH_OPEN] account_id=%s session_id=%s runtime_session_id=%s result=created",
                account_id,
                maintenance_session_id,
                session.session_id,
            )
            return self._safe_session_payload(payload)
        except Exception:
            self.runtime_session_manager.close_session(session)
            raise

    # ============================================================
    # END BLOCK: BALE_AUTHENTICATION_OPEN_IDEMPOTENCY
    # ============================================================

    def _navigate_to_bale_shell(self, page: Any) -> dict[str, Any]:
        """Navigate without treating a SPA load-abort as a logout.

        Bale Web can replace the initial document while Chromium is still
        healthy.  A full-load/network-idle prerequisite turns that harmless
        SPA bootstrap behavior into a long or indefinite wait before the
        visible authenticated shell can be classified.  We require the
        resulting live Bale page, not a specific original Page handle or a
        full-load event, and navigation is bounded independently.
        """
        if page is None or not hasattr(page, "goto"):
            return {"navigated": False, "reason": "page_unavailable"}
        try:
            page.goto(
                self.plugin.web_url,
                wait_until="domcontentloaded",
                timeout=BALE_BOOTSTRAP_NAVIGATION_TIMEOUT_MS,
            )
            return {"navigated": True, "reason": "domcontentloaded"}
        except Exception as exc:
            try:
                alive = not bool(page.is_closed())
                url = str(getattr(page, "url", "") or "").lower()
            except Exception:
                alive, url = False, ""
            if alive and "bale.ai" in url:
                return {"navigated": True, "reason": "navigation_replaced", "error_type": type(exc).__name__}
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
            details = getattr(exc, "details", {}) or {}
            session_payload.update({"state": "temporarily_inconclusive", "terminal": False, "retryable": True, "error_code": getattr(exc, "error_code", "session_closed"), "error_message": str(exc), "page_error_details": details})
            return self._safe_session_payload({**session_payload, "auth": {"auth_state": "auth_probe_inconclusive", "authenticated": False, "diagnostics_consistent": False, "error_code": getattr(exc, "error_code", "session_closed")}})
        page = self.runtime_session_manager.resolve_live_page(runtime)
        if runtime.metadata.pop("page_recovery_required_navigation", False):
            self._navigate_to_bale_shell(page)
            page = self.runtime_session_manager.resolve_live_page(runtime)
        auth = self._classify(page)
        session_payload["auth"] = auth
        session_payload["account_state"] = self._account_state(auth)
        session_payload["state"] = session_payload["account_state"]
        session_payload["authenticated_shell_detected"] = bool(auth.get("authenticated") and auth.get("chat_shell_visible"))
        session_payload["login_screen_detected"] = bool((auth.get("login_check") or {}).get("login_form_visible"))
        session_payload["otp_screen_detected"] = str(auth.get("legacy_auth_state") or auth.get("auth_state")) in {"verification_code_required", "otp_required"}
        session_payload["last_checked_at"] = utc_now()
        return self._safe_session_payload(session_payload)

    def verify(self, maintenance_session_id: str) -> dict[str, Any]:
        session_payload = self._require(maintenance_session_id)
        runtime = self.runtime_session_manager.get_session(str(session_payload["account_id"]))
        if runtime is None:
            raise BaleAuthenticationMaintenanceError("maintenance_session_closed", "Maintenance session is closed")
        self.runtime_session_manager.health_check(runtime)
        page = self.runtime_session_manager.resolve_live_page(runtime)
        session_payload.update({"state": "identity_probe_running", "identity_probe_state": "running", "terminal": False, "last_successful_step": "authenticated_shell_detected"})
        self._navigate_to_bale_shell(page)
        page = self.runtime_session_manager.resolve_live_page(runtime)
        self._dismiss_install_help_prompt(page)
        auth = self._classify(page)
        screenshot_path = ""
        if page is not None:
            diagnostic_dir = Path(__file__).resolve().parents[3] / "runtime" / "diagnostics" / "bale_authentication_verification"
            diagnostic_dir.mkdir(parents=True, exist_ok=True)
            screenshot_file = diagnostic_dir / f"{session_payload['account_id']}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.png"
            try:
                page.screenshot(path=str(screenshot_file), full_page=True)
                screenshot_path = str(screenshot_file)
            except Exception:
                screenshot_path = ""
        account = self.account_store.get_account(str(session_payload["account_id"])) or {}
        registered = str(
            account.get("phone")
            or account.get("username_or_number")
            or account.get("normalized_identifier")
            or account.get("normalized_phone")
            or ""
        )
        identity = self._verify_own_profile_identity(page, registered, page_resolver=lambda: self.runtime_session_manager.resolve_live_page(runtime))
        page = self.runtime_session_manager.resolve_live_page(runtime)
        self._navigate_to_bale_shell(page)
        chat_ready = bool(
            auth.get("chat_list_ready")
            and str(auth.get("chat_readiness_state") or "") == "bale_authenticated_and_chats_loaded"
        )
        verified = bool(
            auth.get("auth_state") == "authenticated"
            and auth.get("authenticated")
            and auth.get("chat_shell_visible")
            and chat_ready
            and identity.get("verified_match")
        )
        if verified:
            self.account_health.record_success(str(session_payload["account_id"]))
        else:
            self.account_health.record_failure(str(session_payload["account_id"]), {"error_code": identity.get("error_code") or auth.get("error_code") or "authentication_required", "error_domain": "authentication", "error_message": "Bale authentication or account identity verification failed"})
        session_payload["auth"] = {
            **auth,
            "identity_probe_status": identity.get("status"),
            "chat_readiness_verified": chat_ready,
        }
        session_payload["verified"] = verified
        session_payload["identity_check"] = identity
        session_payload.update({"identity_probe_state": "completed", "identity_probe_result": identity.get("status"), "state": "identity_verified" if verified else ("identity_mismatch" if identity.get("status") == "mismatch" else "temporarily_inconclusive"), "terminal": bool(verified or identity.get("status") == "mismatch"), "retryable": not bool(verified or identity.get("status") == "mismatch"), "last_successful_step": "visible_profile_identity_matched" if verified else "authenticated_shell_detected"})
        session_payload["last_checked_at"] = utc_now()
        login_check = auth.get("login_check") or {}
        try:
            page_title = str(page.title()) if page is not None else ""
        except Exception:
            page_title = ""
        evidence = {
            "page_url": auth.get("page_url") or "",
            "page_title": page_title,
            "authenticated_shell_selector": login_check.get("matched_selector") if auth.get("chat_shell_visible") else "",
            "chat_list_selector": login_check.get("chat_list_selector") or "",
            "login_form_selector_state": {"visible": bool(login_check.get("login_form_visible")), "selector": login_check.get("login_form_selector") or ""},
            "otp_selector_state": {"visible": auth.get("legacy_auth_state") == "verification_code_required", "selector": "text:verification-code" if auth.get("legacy_auth_state") == "verification_code_required" else ""},
            "screenshot_path": screenshot_path,
            "failed_step": None if verified else "verify_visible_authentication_evidence",
            "last_successful_step": "visible_profile_probe" if verified else "open_canonical_profile",
            "verification_method": "visible_profile_probe",
            "chat_readiness_state": auth.get("chat_readiness_state") or "not_measured",
            "chat_list_ready": chat_ready,
            "chat_row_count": int(auth.get("chat_row_count") or 0),
        }
        return {**self._safe_session_payload(session_payload), "verified": verified, "identity_check": identity, "verification_evidence": evidence, "account_state": "identity_verified" if verified else ("identity_mismatch" if identity.get("status") == "mismatch" else "auth_probe_inconclusive")}

    def _verify_own_profile_identity(self, page: Any, registered: str, page_resolver: Any | None = None) -> dict[str, Any]:
        classify_values = getattr(self.identity_classifier, "classify_visible_values", None)
        if not callable(classify_values):
            return self.identity_classifier.classify(page, registered)
        if page is None:
            return classify_values([], registered, [registered])
        selectors = (
            '[aria-label="profile"]',
            '[aria-label*="profile" i]',
            '[aria-label*="پروفایل"]',
            '[data-testid*="profile" i]',
            'button[aria-label*="profile" i]',
        )
        selector = selectors[0]
        try:
            clicked = False
            for candidate in selectors:
                try:
                    locator = page.locator(candidate)
                    if int(locator.count()) <= 0:
                        continue
                    locator.first.click(timeout=1500)
                    selector = candidate
                    clicked = True
                    break
                except Exception:
                    continue
            if callable(page_resolver):
                page = page_resolver()
            if hasattr(page, "wait_for_timeout"):
                page.wait_for_timeout(500 if clicked else 150)
            values = page.evaluate("""() => Array.from(document.querySelectorAll('body *')).filter(el=>{
              const r=el.getBoundingClientRect(),s=getComputedStyle(el),t=String(el.innerText||el.textContent||'').trim();
              const profileHint=el.closest('[role=dialog],[data-testid*=profile i],[aria-label*=profile i],[aria-label*=پروفایل]');
              return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none'&&t&&t.length<140&&el.children.length===0&&(profileHint||((r.x>=Math.max(0,window.innerWidth-720))&&r.y>=0&&r.y<300));
            }).map(el=>String(el.innerText||el.textContent||'').trim()).slice(0,60)""")
            known = [
                str(
                    item.get("phone")
                    or item.get("username_or_number")
                    or item.get("normalized_identifier")
                    or item.get("normalized_phone")
                    or ""
                )
                for item in self.account_store.list_accounts()
            ]
            result = classify_values(list(values or []), registered, known)
            return {**result, "profile_selector": selector, "profile_page_url": str(getattr(page, "url", ""))}
        except Exception as exc:
            result = classify_values([], registered, [registered])
            return {**result, "error_code": "own_identity_probe_failed", "safe_error": type(exc).__name__, "profile_selector": selector}

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

    @staticmethod
    def _account_state(auth: dict[str, Any], opening: bool = False) -> str:
        legacy = str(auth.get("legacy_auth_state") or auth.get("auth_state") or "")
        if legacy in {"verification_code_required", "otp_required"}: return "otp_required"
        if legacy in {"login_required", "qr_login_required", "unauthenticated"}: return "login_required"
        if legacy == "authenticated": return "authenticated_shell_detected"
        if legacy in {"loading", "reconnecting"}: return "login_in_progress"
        return "profile_opening" if opening else "auth_probe_inconclusive"

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
        account_id = str(payload.get("account_id") or "")
        runtime = self.runtime_session_manager.get_session(account_id) if account_id else None
        metadata = getattr(runtime, "metadata", {}) or {}
        page = getattr(runtime, "page", None)
        context = getattr(runtime, "context", None)
        page_error = payload.get("page_error_details") or metadata.get("last_page_resolution_error") or {}
        auth = payload.get("auth") or {}
        identity = payload.get("identity_check") or {}
        login_check = auth.get("login_check") or {}
        return {
            "ok": True,
            "authentication_id": payload.get("maintenance_session_id"),
            "maintenance_session_id": payload.get("maintenance_session_id"),
            "account_id": payload.get("account_id"),
            "runtime_session_id": payload.get("runtime_session_id"),
            "profile_path": payload.get("profile_path"),
            "opened_at": payload.get("opened_at"),
            "last_checked_at": payload.get("last_checked_at"),
            "closed": bool(payload.get("closed")),
            "state": payload.get("state") or payload.get("account_state") or "opening_profile",
            "terminal": bool(payload.get("terminal")),
            "verified": bool(payload.get("verified")),
            "retryable": bool(payload.get("retryable", True)),
            "browser_open": bool(runtime is not None and not payload.get("closed")),
            "browser_pid": ((metadata.get("runtime_process_identity") or {}).get("root_process") or {}).get("ProcessId"),
            "context_open": bool(context is not None and not self.runtime_session_manager._is_closed(context)),
            "page_open": bool(page is not None and not self.runtime_session_manager._is_closed(page)),
            "current_page_url": str(getattr(page, "url", "") or "") if page is not None else str(page_error.get("last_page_url") or ""),
            "current_page_count": int(metadata.get("current_page_count") or 0),
            "original_page_id": metadata.get("original_page_id"),
            "current_page_id": metadata.get("current_page_id"),
            "page_replacement_count": int(metadata.get("page_replacement_count") or 0),
            "page_close_event_at": metadata.get("page_close_event_at"),
            "close_initiator": page_error.get("close_initiator") or metadata.get("page_close_initiator"),
            "another_live_page_existed": page_error.get("another_live_page_existed"),
            "authenticated_shell_detected": bool(payload.get("authenticated_shell_detected") or (auth.get("authenticated") and auth.get("chat_shell_visible"))),
            "login_screen_detected": bool(payload.get("login_screen_detected") or login_check.get("login_form_visible")),
            "otp_screen_detected": bool(payload.get("otp_screen_detected") or str(auth.get("legacy_auth_state") or auth.get("auth_state")) in {"verification_code_required", "otp_required"}),
            "identity_probe_state": payload.get("identity_probe_state") or "pending",
            "identity_probe_result": payload.get("identity_probe_result"),
            "expected_masked_phone": identity.get("expected_masked"),
            "observed_masked_phone": identity.get("observed_masked"),
            "last_successful_step": payload.get("last_successful_step") or metadata.get("last_successful_step"),
            "error_code": payload.get("error_code") or auth.get("error_code"),
            "error_message": payload.get("error_message"),
            "auth": auth,
            "profile_lock": {
                "owned": not bool(payload.get("closed")),
                "owner": "bale_authentication_maintenance" if not payload.get("closed") else None,
            },
            "secrets_exposed": False,
            "audit": payload.get("audit"),
        }
