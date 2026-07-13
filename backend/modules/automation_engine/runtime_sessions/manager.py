from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from modules.automation_engine.browser_identity.resolver import BrowserIdentityError, BrowserIdentityResolver
from modules.automation_engine.browser_identity.validation import profile_compare_key

from .models import RuntimeSession, utc_now


class RuntimeSessionError(RuntimeError):
    def __init__(self, error_code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.details = details or {}


class AccountRuntimeSessionManager:
    def __init__(self, adapter_registry: dict[str, Any] | None = None) -> None:
        self._sessions: dict[str, RuntimeSession] = {}
        self._sessions_by_profile: dict[str, RuntimeSession] = {}
        self._reservations_by_profile: dict[str, str] = {}
        self._lock = threading.RLock()
        self.adapter_registry = adapter_registry or {}
        self.identity_resolver = BrowserIdentityResolver()

    def register_adapter(self, platform: str, adapter: Any) -> None:
        with self._lock:
            self.adapter_registry[platform] = adapter

    def create_session(self, account_id: str, execution_context: Any, policy: dict[str, Any], owner_token: str | None = None, worker_round_id: str | None = None) -> RuntimeSession:
        token = owner_token or getattr(execution_context, "owner_token", None) or getattr(execution_context, "correlation_id", "")
        round_id = worker_round_id or getattr(execution_context, "worker_round_id", None)
        return self.acquire_or_create_session(account_id, str(token), str(round_id), policy)

    def get_session(self, account_id: str) -> RuntimeSession | None:
        with self._lock:
            session = self._sessions.get(account_id)
            if session is None or session.closed_at:
                return None
            return session

    def acquire_or_create_session(self, account_id: str, owner_token: str, worker_round_id: str, policy: dict[str, Any]) -> RuntimeSession:
        with self._lock:
            existing = self._sessions.get(account_id)
            if existing and not existing.closed_at:
                self._assert_owner(existing, owner_token, worker_round_id)
                if existing.invalidated:
                    raise RuntimeSessionError("session_invalidated", "Runtime session has been invalidated", existing.safe_diagnostics())
                return existing
        platform = str(policy.get("platform") or "bale")
        try:
            identity = self.identity_resolver.verify_launch_allowed(account_id, worker_round_id)
        except BrowserIdentityError as exc:
            raise RuntimeSessionError(exc.error_code, str(exc), exc.details) from exc
        profile_path = str(identity["profile_path"])
        normalized_profile_path = str(identity["normalized_profile_path"]).casefold()
        reservation_token = f"reservation_{account_id}_{worker_round_id}_{time.monotonic_ns()}"
        with self._lock:
            if normalized_profile_path in self._sessions_by_profile:
                raise RuntimeSessionError("profile_session_already_active", "Profile path already has an active runtime session")
            if normalized_profile_path in self._reservations_by_profile:
                raise RuntimeSessionError("profile_session_already_active", "Profile path launch is already reserved")
            self._reservations_by_profile[normalized_profile_path] = reservation_token
        Path(profile_path).mkdir(parents=True, exist_ok=True)
        adapter = self.adapter_registry.get(platform)
        started = time.perf_counter()
        runtime_payload: dict[str, Any] = {}
        try:
            if adapter is not None:
                runtime_payload = adapter.create_runtime_session_for_account(account_id=account_id, owner_token=owner_token, worker_round_id=worker_round_id, policy={**policy, "browser_identity": identity, "profile_path": profile_path})
            duration_ms = int((time.perf_counter() - started) * 1000)
            payload_path = str(runtime_payload.get("profile_path") or profile_path)
            if profile_compare_key(payload_path) != normalized_profile_path:
                raise RuntimeSessionError("profile_path_conflict", "Adapter returned a profile path that does not match BrowserIdentity")
            session = RuntimeSession.create(
                account_id=account_id,
                platform=platform,
                identity_id=str(identity.get("identity_id") or ""),
                owner_token=owner_token,
                worker_round_id=worker_round_id,
                profile_path=payload_path,
                normalized_profile_path=normalized_profile_path,
                browser=runtime_payload.get("browser"),
                context=runtime_payload.get("context"),
                page=runtime_payload.get("page"),
                browser_start_duration_ms=duration_ms,
                metadata={key: value for key, value in runtime_payload.items() if key not in {"browser", "context", "page"}},
            )
        except Exception:
            with self._lock:
                if self._reservations_by_profile.get(normalized_profile_path) == reservation_token:
                    self._reservations_by_profile.pop(normalized_profile_path, None)
            raise
        with self._lock:
            existing = self._sessions.get(account_id)
            if existing and not existing.closed_at:
                self._close_without_lock(session)
                if self._reservations_by_profile.get(normalized_profile_path) == reservation_token:
                    self._reservations_by_profile.pop(normalized_profile_path, None)
                self._assert_owner(existing, owner_token, worker_round_id)
                return existing
            if normalized_profile_path in self._sessions_by_profile:
                self._close_without_lock(session)
                raise RuntimeSessionError("profile_session_already_active", "Profile path already has an active runtime session")
            if self._reservations_by_profile.get(normalized_profile_path) == reservation_token:
                self._reservations_by_profile.pop(normalized_profile_path, None)
            self._sessions[account_id] = session
            self._sessions_by_profile[normalized_profile_path] = session
        return session

    def health_check(self, session: RuntimeSession) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            self._assert_live(session)
            page = session.page
            if page is None or self._is_closed(page):
                raise RuntimeSessionError("session_page_closed", "Runtime page is closed")
            try:
                title = getattr(page, "title", None)
                if callable(title):
                    title()
                elif hasattr(page, "evaluate"):
                    page.evaluate("() => document.readyState")
            except Exception as exc:
                raise RuntimeSessionError("session_health_check_failed", str(exc)) from exc
            return {"ok": True, "session_id": session.session_id}
        finally:
            session.last_health_check_duration_ms = int((time.perf_counter() - started) * 1000)

    def prepare_for_job(self, session: RuntimeSession, execution_plan: Any) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            self._assert_live(session)
            if session.account_id != execution_plan.account_id:
                raise RuntimeSessionError("session_account_mismatch", "Session account does not match execution plan")
            if session.worker_round_id != execution_plan.worker_round_id:
                raise RuntimeSessionError("session_round_mismatch", "Session round does not match execution plan")
            identity = self.identity_resolver.verify_launch_allowed(session.account_id, session.worker_round_id)
            if str(identity.get("identity_id") or "") != str(session.identity_id or ""):
                raise RuntimeSessionError("browser_identity_mismatch", "Runtime session identity does not match persisted identity")
            if profile_compare_key(identity["profile_path"]) != str(session.normalized_profile_path).casefold():
                raise RuntimeSessionError("session_profile_mismatch", "Session profile path does not belong to account")
            self.health_check(session)
            page = session.page
            if self._has_state(page, "stale_modal_state"):
                raise RuntimeSessionError("stale_modal_state", "A stale recipient picker/modal is still open")
            if self._has_state(page, "stale_recipient_selection"):
                raise RuntimeSessionError("stale_recipient_selection", "A stale recipient selection is still present")
            if self._has_state(page, "previous_delivery_state_uncertain"):
                raise RuntimeSessionError("previous_delivery_state_uncertain", "Previous delivery state is uncertain")
            self._clear_clinicos_markers(page)
            return {"ok": True, "session_id": session.session_id}
        finally:
            session.last_prepare_duration_ms = int((time.perf_counter() - started) * 1000)

    def invalidate_session(self, session: RuntimeSession, reason: str) -> RuntimeSession:
        with self._lock:
            session.healthy = False
            session.invalidated = True
            session.invalidated_reason = reason
        return session

    def close_session(self, session: RuntimeSession) -> dict[str, Any]:
        with self._lock:
            if session.closed_at:
                return {"ok": True, "session_id": session.session_id, "already_closed": True}
            session.close_requested = True
            if self._sessions.get(session.account_id) is session:
                self._sessions.pop(session.account_id, None)
            if self._sessions_by_profile.get(session.normalized_profile_path) is session:
                self._sessions_by_profile.pop(session.normalized_profile_path, None)
        result = self._close_without_lock(session)
        with self._lock:
            session.closed_at = session.closed_at or utc_now()
        return result

    def close_session_for_account(self, account_id: str) -> dict[str, Any]:
        session = self.get_session(account_id)
        if session is None:
            return {"ok": True, "account_id": account_id, "closed": False, "reason": "session_not_found"}
        if session.metadata.get("active_delivery_state") in {"confirm", "verification", "uncertain"}:
            return {"ok": False, "account_id": account_id, "closed": False, "error_code": "previous_delivery_state_uncertain", "message": "Active confirm/verification state cannot be force-closed safely"}
        return self.close_session(session)

    def list_active_sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            return [session.safe_diagnostics() for session in self._sessions.values() if not session.closed_at]

    def get_session_diagnostics(self, account_id: str) -> dict[str, Any] | None:
        session = self.get_session(account_id)
        return session.safe_diagnostics() if session else None

    def mark_job_complete(self, session: RuntimeSession, duration_ms: int) -> None:
        with self._lock:
            session.jobs_processed += 1
            session.last_used_at = utc_now()
            durations = list(session.metadata.get("job_durations_ms") or [])
            durations.append(int(duration_ms))
            session.metadata["job_durations_ms"] = durations

    def _assert_owner(self, session: RuntimeSession, owner_token: str, worker_round_id: str) -> None:
        if session.owner_token != owner_token:
            raise RuntimeSessionError("session_owner_mismatch", "Runtime session owner token mismatch")
        if session.worker_round_id != worker_round_id:
            raise RuntimeSessionError("session_round_mismatch", "Runtime session worker round mismatch")

    def _assert_live(self, session: RuntimeSession) -> None:
        if session.invalidated:
            raise RuntimeSessionError("session_invalidated", "Runtime session has been invalidated")
        if not session.healthy:
            raise RuntimeSessionError("reused_session_unhealthy", "Runtime session is unhealthy")
        if session.closed_at:
            raise RuntimeSessionError("session_not_found", "Runtime session is already closed")

    def _close_without_lock(self, session: RuntimeSession) -> dict[str, Any]:
        adapter = self.adapter_registry.get(session.platform)
        try:
            if adapter is not None:
                adapter.close_runtime_session(session)
            elif session.context is not None and hasattr(session.context, "close"):
                session.context.close()
            session.closed_at = session.closed_at or utc_now()
            return {"ok": True, "session_id": session.session_id, "closed": True}
        except Exception as exc:
            session.healthy = False
            session.invalidated = True
            session.invalidated_reason = "session_close_failed"
            session.closed_at = session.closed_at or utc_now()
            return {"ok": False, "session_id": session.session_id, "closed": False, "error_code": "session_close_failed", "message": str(exc)}

    def _is_closed(self, obj: Any) -> bool:
        try:
            value = getattr(obj, "is_closed", None)
            return bool(value() if callable(value) else value)
        except Exception:
            return True

    def _has_state(self, page: Any, flag: str) -> bool:
        try:
            states = getattr(page, "session_state", {})
            if isinstance(states, dict) and states.get(flag):
                return True
        except Exception:
            pass
        return False

    def _clear_clinicos_markers(self, page: Any) -> None:
        try:
            if hasattr(page, "evaluate"):
                page.evaluate(
                    """() => {
                        document.querySelectorAll('[data-clinicos-temp-marker]').forEach((el) => el.removeAttribute('data-clinicos-temp-marker'));
                        delete window.__clinicos_forward_uncertain;
                    }"""
                )
        except Exception:
            return


account_runtime_session_manager = AccountRuntimeSessionManager()
