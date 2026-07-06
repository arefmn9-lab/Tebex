from __future__ import annotations

import json
import time
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from modules.automation_engine.browser import actions_browser
from modules.automation_engine.browser.browser_manager import BrowserManager, resolve_system_browser_executable
from modules.automation_engine.browser.providers import get_provider

from .account_store import bale_account_store
from . import selectors


class BalePlugin:
    platform_id = "bale"
    web_url = "https://web.bale.ai"
    default_timeout_ms = 15000

    def __init__(self, browser_manager: BrowserManager | None = None) -> None:
        self.browser_manager = browser_manager or actions_browser.browser_manager
        self._logs: list[dict[str, Any]] = []
        self.scenario_dir = Path(__file__).resolve().parent / "scenarios"

    def open_account(self, account_id: str) -> dict[str, Any]:
        try:
            account = bale_account_store.get_account(account_id)
            if account is None:
                raise RuntimeError(f"Bale account not found: {account_id}")
            browser_provider = str(account.get("browser_provider") or "adspower")
            if browser_provider == "adspower":
                profile_id = str(account.get("adspower_profile_id") or "")
                if not profile_id:
                    result = {
                        "ok": False,
                        "browser_provider": "adspower",
                        "account_id": account_id,
                        "profile_id": profile_id,
                        "profile_group_id": account.get("profile_group_id"),
                        "error_code": "profile_not_configured",
                        "message": "AdsPower profile_id is required for this account",
                    }
                    self._log_open_account_attempt(account, "failed", result["message"], result["error_code"])
                    return result
                result = get_provider("adspower").open_profile(account_id, profile_id, self.web_url)
                result["profile_group_id"] = account.get("profile_group_id")
                self._log_open_account_attempt(
                    account,
                    "success" if result.get("ok") else "failed",
                    str(result.get("message") or ""),
                    result.get("error_code"),
                )
                return result

            if not self.browser_manager.is_available():
                raise RuntimeError(
                    "Playwright is not available. Install dependencies and browser binaries."
                )

            page = self.browser_manager.get_page(
                account_id,
                headless=False,
                login_required=True,
                profile_metadata=account,
            )
            page.goto(self.web_url, wait_until="load")
            self.browser_manager.save_session(account_id)
            result = {
                "ok": True,
                "platform": self.platform_id,
                "account_id": account_id,
                "message": "Bale Web opened. Complete login manually in the browser if needed.",
                "url": self.web_url,
            }
            self._log_open_account_attempt(account, "success", result["message"])
            return result
        except Exception as exc:
            message = str(exc)
            account = bale_account_store.get_account(account_id) or {"account_id": account_id, "browser_provider": "unknown"}
            browser_path = getattr(self.browser_manager, "last_browser_path", None)
            self._log_open_account_attempt(
                account,
                "failed",
                f"Failed to open Bale Web; browser_path={browser_path}",
                "unknown_error",
                message,
            )
            return {
                "ok": False,
                "platform": self.platform_id,
                "account_id": account_id,
                "browser_provider": account.get("browser_provider"),
                "profile_id": account.get("adspower_profile_id") or account.get("profile_id"),
                "profile_group_id": account.get("profile_group_id"),
                "message": "Failed to open Bale Web",
                "error": message,
                "browser_path": browser_path,
            }

    def open_login(self, account_id: str) -> dict[str, Any]:
        profile_dir = _native_profile_dir(account_id)
        profile_dir.mkdir(parents=True, exist_ok=True)
        account = bale_account_store.get_account(account_id) or {"account_id": account_id}
        profile_metadata = {
            **account,
            "browser_provider": "native_chrome",
            "adspower_profile_id": "",
            "user_data_dir": str(profile_dir),
        }
        try:
            page = self.browser_manager.get_page(
                account_id,
                headless=False,
                login_required=True,
                profile_metadata=profile_metadata,
            )
            page.goto(self.web_url, wait_until="load")
            return {
                "ok": True,
                "platform": self.platform_id,
                "account_id": account_id,
                "provider_mode": "native_chrome",
                "profile_dir": str(profile_dir),
                "url": self.web_url,
                "message": "Chrome opened for Bale login. Complete login manually, then check login.",
            }
        except Exception as exc:
            return {
                "ok": False,
                "platform": self.platform_id,
                "account_id": account_id,
                "provider_mode": "native_chrome",
                "profile_dir": str(profile_dir),
                "error_code": _browser_error_code(exc),
                "message": "Failed to open Bale login window",
                "error": str(exc),
            }

    def check_login(self, account_id: str) -> dict[str, Any]:
        started = time.perf_counter()
        profile_dir = _native_profile_dir(account_id)
        profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            page = self.browser_manager.get_page(
                account_id,
                headless=False,
                login_required=True,
                profile_metadata={
                    "account_id": account_id,
                    "browser_provider": "native_chrome",
                    "adspower_profile_id": "",
                    "user_data_dir": str(profile_dir),
                },
            )
            page.goto(self.web_url, wait_until="load")
            login_check = self._detect_login_state(page, timeout_ms=10000)
            error_code = None
            message = "Bale login detected" if login_check["logged_in"] else "Manual Bale login is required"
            if login_check["install_prompt_detected"]:
                error_code = "bale_install_prompt"
                message = "Bale install/help prompt is visible. Dismiss it, then log in."
            elif not login_check["logged_in"]:
                error_code = "not_logged_in"
            return {
                "ok": bool(login_check["logged_in"]),
                "logged_in": bool(login_check["logged_in"]),
                "platform": self.platform_id,
                "account_id": account_id,
                "provider_mode": "native_chrome",
                "profile_dir": str(profile_dir),
                "message": message,
                "error_code": error_code,
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "login_check": login_check,
            }
        except Exception as exc:
            return {
                "ok": False,
                "logged_in": False,
                "platform": self.platform_id,
                "account_id": account_id,
                "provider_mode": "native_chrome",
                "profile_dir": str(profile_dir),
                "message": "Bale login check failed",
                "error_code": _browser_error_code(exc),
                "error": str(exc),
                "duration_ms": int((time.perf_counter() - started) * 1000),
            }

    def validate_session(self, account_id: str) -> dict[str, Any]:
        try:
            page = self._get_page(account_id)
            page.goto(self.web_url, wait_until="load")
            login_check = self._detect_login_state(page, timeout_ms=5000)
            logged_in = bool(login_check["logged_in"])
            message = "Bale session appears logged in" if logged_in else "Manual Bale login is required"
            self._log_step(account_id, "validate_session", "success", message)
            return {
                "ok": logged_in,
                "logged_in": logged_in,
                "platform": self.platform_id,
                "account_id": account_id,
                "message": message,
                "error_code": "bale_install_prompt" if login_check["install_prompt_detected"] else (None if logged_in else "not_logged_in"),
                "login_check": login_check,
            }
        except Exception as exc:
            error = str(exc)
            self._log_step(account_id, "validate_session", "failed", "Session validation failed", error)
            return {
                "ok": False,
                "logged_in": False,
                "platform": self.platform_id,
                "account_id": account_id,
                "message": "Session validation failed",
                "error_code": _browser_error_code(exc),
                "error": error,
            }

    def send_test_message(self, account_id: str, target: str, message: str, provider_mode: str | None = None) -> dict[str, Any]:
        started_at = datetime.now(timezone.utc).isoformat()
        started_monotonic = time.perf_counter()
        effective_provider = str(provider_mode or "native_chrome")
        if not target.strip():
            return self._failed_result(account_id, "send_test_message", "target_not_found", "Target is required")
        if not message.strip():
            return self._failed_result(account_id, "send_test_message", "message_input_not_found", "Message is required")

        try:
            execution_logs, browser_meta = self._send_test_message_steps(account_id, target, message, provider_mode)
            finished_at = datetime.now(timezone.utc).isoformat()
            result = {
                "ok": True,
                "logged_in": True,
                "platform": self.platform_id,
                "account_id": account_id,
                "target": target,
                "message": "One Bale test message action completed.",
                "logs": execution_logs,
                "started_at": started_at,
                "finished_at": finished_at,
                "duration_ms": int((time.perf_counter() - started_monotonic) * 1000),
                **browser_meta,
            }
            self._log(account_id, "send_test_message", "success", result["message"])
            return result
        except Exception as exc:
            error_code = _browser_error_code(exc)
            error = str(exc)
            browser_path = getattr(self.browser_manager, "last_browser_path", None)
            finished_at = datetime.now(timezone.utc).isoformat()
            failure_meta = self._browser_failure_meta(account_id, effective_provider)
            diagnostics = getattr(exc, "diagnostics", {}) or {}
            self._log_step(
                account_id,
                "send_test_message",
                "failed",
                f"Bale test message failed; browser_path={browser_path}",
                error,
                error_code,
            )
            return {
                "ok": False,
                "logged_in": error_code not in {"not_logged_in", "bale_install_prompt"},
                "platform": self.platform_id,
                "account_id": account_id,
                "target": target,
                "message": "Bale test message failed",
                "error_code": error_code,
                "error": error,
                "browser_path": browser_path,
                "started_at": started_at,
                "finished_at": finished_at,
                "duration_ms": int((time.perf_counter() - started_monotonic) * 1000),
                **diagnostics,
                **failure_meta,
            }

    def send_text_message(
        self,
        account_id: str,
        normalized_phone: str,
        message_text: str,
        contact_naming_value: str = "",
        provider_mode: str | None = None,
    ) -> dict[str, Any]:
        started_at = datetime.now(timezone.utc).isoformat()
        started_monotonic = time.perf_counter()
        effective_provider = str(provider_mode or "native_chrome")
        step_results: list[dict[str, Any]] = []
        contact_save_status = "not_attempted"
        browser_meta: dict[str, Any] = self._browser_failure_meta(account_id, effective_provider)
        current_url_value = ""

        def finish(
            ok: bool,
            error_code: str | None = None,
            user_message: str = "",
            failed_step: str | None = None,
            extra: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            finished_at = datetime.now(timezone.utc).isoformat()
            result = {
                "ok": ok,
                "success": ok,
                "logged_in": error_code not in {"not_logged_in", "bale_install_prompt"},
                "platform": self.platform_id,
                "account_id": account_id,
                "normalized_phone": normalized_phone,
                "target": normalized_phone,
                "contact_naming_value": contact_naming_value,
                "contact_save_status": contact_save_status,
                "current_url": (extra or {}).get("current_url") or current_url_value,
                "message": user_message or ("Bale text message sent" if ok else "Bale text message failed"),
                "user_message": user_message,
                "error_code": error_code,
                "failed_step": failed_step,
                "last_successful_step": _last_successful_step(step_results) if not ok else None,
                "step_results": step_results,
                "started_at": started_at,
                "finished_at": finished_at,
                "duration_ms": int((time.perf_counter() - started_monotonic) * 1000),
                **browser_meta,
                **(extra or {}),
            }
            return result

        if not normalized_phone.strip():
            return finish(False, "target_not_found", "Target phone is required", "load_target")
        if not message_text.strip():
            return finish(False, "message_text_empty", "Message text is empty", "load_message_text")

        try:
            with self._page_session(account_id, provider_mode) as (page, session_meta):
                browser_meta = {**browser_meta, **session_meta}
                self._add_step(step_results, "open_bale_web", "started")
                page.goto(self.web_url, wait_until="load")
                current_url_value = _safe_page_url(page)
                self._add_step(step_results, "open_bale_web", "success", current_url=current_url_value)

                self._add_step(step_results, "verify_login", "started")
                login_check = self._detect_login_state(page, timeout_ms=10000)
                if login_check["install_prompt_detected"]:
                    diagnostics = {"login_check": login_check, **self._page_debug_info(page, account_id)}
                    self._add_step(step_results, "verify_login", "failed", error_code="bale_install_prompt", **diagnostics)
                    return finish(
                        False,
                        "bale_install_prompt",
                        "صفحه راهنمای نصب بله نمایش داده شده است. روی «متوجه شدم» بزنید و وارد بله شوید.",
                        "verify_login",
                        diagnostics,
                    )
                if not login_check["logged_in"]:
                    diagnostics = {"login_check": login_check, **self._page_debug_info(page, account_id)}
                    self._add_step(step_results, "verify_login", "failed", error_code="not_logged_in", **diagnostics)
                    return finish(
                        False,
                        "not_logged_in",
                        "Manual Bale login is required before sending a text message",
                        "verify_login",
                        diagnostics,
                    )
                self._add_step(step_results, "verify_login", "success", login_check=login_check)

                self._add_step(step_results, "load_message_text", "success", message_length=len(message_text.strip()))
                contact_save_result = self.save_contact_by_phone(
                    page,
                    normalized_phone=normalized_phone,
                    contact_naming_value=contact_naming_value,
                    account_id=account_id,
                )
                contact_save_status = str(contact_save_result.get("contact_save_status") or "unknown")
                step_results.append(contact_save_result)
                if contact_save_status not in {"saved", "already_exists"}:
                    diagnostics = {**self._page_debug_info(page, account_id), "contact_save_result": contact_save_result}
                    return finish(
                        False,
                        str(contact_save_result.get("error_code") or "contact_save_failed"),
                        str(contact_save_result.get("user_message") or "مخاطب در بله ذخیره نشد"),
                        "save_or_resolve_contact",
                        diagnostics,
                    )

                return_result = self._return_to_chat_after_contact_save(page, contact_naming_value, normalized_phone)
                step_results.append(return_result)
                if return_result["status"] != "success":
                    diagnostics = {**self._page_debug_info(page, account_id), **return_result}
                    return finish(
                        False,
                        str(return_result.get("error_code") or "return_to_chat_failed"),
                        "Bale chat/search UI was not ready after saving contact.",
                        "return_to_chat_after_contact_save",
                        diagnostics,
                    )

                search_result = self._open_target_chat(page, contact_naming_value, normalized_phone)
                step_results.append(search_result)
                if search_result["status"] != "success":
                    diagnostics = {**self._page_debug_info(page, account_id), **search_result}
                    return finish(
                        False,
                        "target_not_found",
                        "Target chat was not found after searching Bale.",
                        "open_target_chat",
                        diagnostics,
                    )

                message_input = self._first_visible_selector(page, selectors.MESSAGE_INPUT_SELECTORS, timeout_ms=2500)
                if not message_input:
                    diagnostics = {
                        **self._page_debug_info(page, account_id),
                        **self._post_save_ui_diagnostics(page, contact_naming_value, normalized_phone, searched_value=contact_naming_value or normalized_phone),
                    }
                    self._add_step(step_results, "type_message", "failed", error_code="message_input_not_found", **diagnostics)
                    return finish(False, "message_input_not_found", "Bale message input was not found", "type_message", diagnostics)
                self._add_step(step_results, "type_message", "started", selector=message_input)
                self._click_if_possible(page, message_input)
                self._fill_or_type(page, message_input, message_text)
                self._add_step(step_results, "type_message", "success", selector=message_input)

                self._add_step(step_results, "click_send", "started", action="press_enter")
                self._press_key(page, "Enter")
                self._add_step(step_results, "click_send", "success", action="press_enter")

                sent_selector = self._first_visible_selector(page, selectors.MESSAGE_SENT_INDICATOR_SELECTORS, timeout_ms=3000)
                if sent_selector:
                    self.browser_manager.save_session(account_id)
                    self._add_step(step_results, "confirm_sent", "success", matched_selector=sent_selector)
                    return finish(True, None, "Bale text message sent", None, {"current_url": _safe_page_url(page)})

                diagnostics = {
                    **self._page_debug_info(page, account_id),
                    **self._post_save_ui_diagnostics(page, contact_naming_value, normalized_phone, searched_value=contact_naming_value or normalized_phone),
                }
                self._add_step(step_results, "confirm_sent", "failed", error_code="send_confirmation_not_implemented", **diagnostics)
                return finish(
                    False,
                    "send_confirmation_not_implemented",
                    "Message was typed and Enter was pressed, but sent confirmation was not detected.",
                    "confirm_sent",
                    diagnostics,
                )
        except Exception as exc:
            error_code = _browser_error_code(exc)
            diagnostics = getattr(exc, "diagnostics", {}) or {}
            self._add_step(step_results, "unexpected_error", "failed", error_code=error_code, error=str(exc))
            return finish(False, error_code, str(exc), "unexpected_error", diagnostics)

    def save_contact_by_phone(
        self,
        page: Any,
        normalized_phone: str,
        contact_naming_value: str,
        account_id: str | None = None,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        result = self._save_contact_from_modal(
            page,
            normalized_phone=normalized_phone,
            contact_naming_value=contact_naming_value,
            account_id=account_id,
            job_id=job_id,
        )
        if result.get("contact_save_status") in {"saved", "already_exists"}:
            return result

        contact_steps = result.get("contact_steps") if isinstance(result.get("contact_steps"), list) else []
        failed_step = str(result.get("failed_step") or "")
        if not failed_step:
            for step in reversed(contact_steps):
                if isinstance(step, dict) and step.get("status") == "failed":
                    failed_step = str(step.get("step") or "")
                    break
        if not failed_step:
            failed_step = "save_or_resolve_contact"

        detailed_error_code = str(result.get("error_code") or result.get("reason") or "contact_save_failed")
        public_error_code = (
            detailed_error_code
            if detailed_error_code in {"contact_save_not_confirmed", "add_contact_button_disabled"}
            else "contact_save_failed"
        )
        user_message = str(result.get("user_message") or "مخاطب در بله ذخیره نشد")
        result.update(
            {
                "status": "failed",
                "contact_save_status": "failed",
                "error_code": public_error_code,
                "detail_error_code": detailed_error_code,
                "user_message": user_message,
                "failed_step": failed_step,
                "step_results": contact_steps,
            }
        )
        if account_id and not result.get("screenshot_path"):
            screenshot_path = _save_login_debug_screenshot(page, account_id)
            if screenshot_path:
                result["screenshot_path"] = screenshot_path
        return result

    def list_logs(self) -> list[dict[str, Any]]:
        return list(self._logs)

    def log_scenario_step(
        self,
        account_id: str,
        scenario_id: str,
        step: str,
        status: str,
        message: str,
        error: str | None = None,
        error_code: str | None = None,
        reason: str | None = None,
        dry_run: bool = True,
    ) -> None:
        self._logs.append(
            {
                "id": str(uuid4()),
                "account_id": account_id,
                "platform": self.platform_id,
                "platform_id": self.platform_id,
                "scenario_id": scenario_id,
                "action": step,
                "step": step,
                "operation_type": step,
                "event_key": f"bale.{scenario_id}.{step}",
                "status": status,
                "message": message,
                "error": error,
                "error_code": error_code,
                "reason": reason,
                "dry_run": dry_run,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        self._logs = self._logs[-500:]

    def _send_test_message_steps(self, account_id: str, target: str, message: str, provider_mode: str | None = None) -> tuple[list[str], dict[str, Any]]:
        execution_logs: list[str] = []
        with self._page_session(account_id, provider_mode) as (page, browser_meta):
            self._send_test_message_on_page(page, execution_logs, account_id, target, message)
            return execution_logs, browser_meta

    def _send_test_message_on_page(
        self,
        page: Any,
        execution_logs: list[str],
        account_id: str,
        target: str,
        message: str,
    ) -> None:
        self._record_step(execution_logs, account_id, "open_bale_web", "started", "Opening Bale Web")
        page.goto(self.web_url, wait_until="load")
        self.browser_manager.save_session(account_id)
        self._record_step(execution_logs, account_id, "open_bale_web", "success", "Bale Web opened")

        self._record_step(execution_logs, account_id, "check_login", "started", "Checking Bale login state")
        login_check = self._detect_login_state(page, timeout_ms=10000)
        if login_check["install_prompt_detected"]:
            self._record_step(
                execution_logs,
                account_id,
                "check_login",
                "failed",
                "Bale install/help prompt is visible",
                error_code="bale_install_prompt",
            )
            raise BalePluginError(
                "bale_install_prompt",
                "صفحه راهنمای نصب بله نمایش داده شده است. روی «متوجه شدم» بزنید و وارد بله شوید.",
                {"login_check": login_check, **self._page_debug_info(page, account_id)},
            )
        if not login_check["logged_in"]:
            self._record_step(
                execution_logs,
                account_id,
                "check_login",
                "failed",
                "Manual Bale login is required before sending a test message",
                error_code="not_logged_in",
            )
            raise BalePluginError(
                "not_logged_in",
                "Manual Bale login is required before sending a test message",
                {"login_check": login_check, **self._page_debug_info(page, account_id)},
            )
        self._record_step(execution_logs, account_id, "check_login", "success", f"Logged-in UI detected: {login_check.get('matched_selector')}")

        search_input = self._require_selector(
            page,
            selectors.SEARCH_INPUT_SELECTORS,
            "target_not_found",
            "Bale search input was not found",
        )
        self._record_step(execution_logs, account_id, "search_target", "started", f"Searching target: {target}")
        page.fill(search_input, target, timeout=self.default_timeout_ms)
        chat_item = self._require_selector(
            page,
            selectors.CHAT_ITEM_SELECTORS,
            "target_not_found",
            "Target chat was not found after search",
        )
        self._record_step(execution_logs, account_id, "search_target", "success", f"Target candidate found: {chat_item}")

        self._record_step(execution_logs, account_id, "open_chat", "started", "Opening target chat")
        page.click(chat_item, timeout=self.default_timeout_ms)
        self._record_step(execution_logs, account_id, "open_chat", "success", "Target chat opened")

        message_input = self._require_selector(
            page,
            selectors.MESSAGE_INPUT_SELECTORS,
            "message_input_not_found",
            "Bale message input was not found",
        )
        self._record_step(execution_logs, account_id, "type_message", "started", "Typing one test message")
        page.fill(message_input, message, timeout=self.default_timeout_ms)
        self._record_step(execution_logs, account_id, "type_message", "success", "Test message typed")

        send_button = self._require_selector(
            page,
            selectors.SEND_BUTTON_SELECTORS,
            "send_button_not_found",
            "Bale send button was not found",
        )
        self._record_step(execution_logs, account_id, "click_send", "started", "Clicking send button once")
        page.click(send_button, timeout=self.default_timeout_ms)

        sent_indicator = self._first_visible_selector(
            page,
            selectors.MESSAGE_SENT_INDICATOR_SELECTORS,
            timeout_ms=5000,
        )
        if sent_indicator is None:
            self._record_step(
                execution_logs,
                account_id,
                "detect_sent",
                "failed",
                "Send click completed, but sent-message indicator was not detected",
                error_code="send_timeout",
            )
            raise BalePluginError("send_timeout", "Send click completed, but sent-message indicator was not detected")

        self.browser_manager.save_session(account_id)
        self._record_step(execution_logs, account_id, "detect_sent", "success", f"Sent indicator detected: {sent_indicator}")

    def _execute_steps(self, account_id: str, steps: list[dict[str, Any]]) -> list[str]:
        execution_logs: list[str] = []
        handlers = {
            "open_url": actions_browser.open_url,
            "type_text": actions_browser.type_text,
            "click_element": actions_browser.click_element,
        }

        for index, step in enumerate(steps):
            action = step["action"]
            handler = handlers.get(action)
            if handler is None:
                raise ValueError(f"Unsupported Bale plugin action: {action}")

            params = dict(step.get("params") or {})
            params["account_id"] = account_id
            params.setdefault("headless", False)
            if action == "open_url":
                params.setdefault("login_required", True)

            result = handler(params)
            execution_logs.append(f"Step {index} {action}: {result.get('message', '')}")
            if not result.get("ok", False):
                raise RuntimeError(str(result.get("message", f"Step {index} failed")))

        return execution_logs

    def _load_scenario(self, filename: str) -> dict[str, Any]:
        path = self.scenario_dir / filename
        with path.open("r", encoding="utf-8") as scenario_file:
            return json.load(scenario_file)

    def _substitute_scenario(
        self,
        scenario: dict[str, Any],
        replacements: dict[str, str],
    ) -> dict[str, Any]:
        data = deepcopy(scenario)

        def replace_value(value: Any) -> Any:
            if isinstance(value, str):
                for token, replacement in replacements.items():
                    value = value.replace(token, replacement)
                return value
            if isinstance(value, list):
                return [replace_value(item) for item in value]
            if isinstance(value, dict):
                return {key: replace_value(item) for key, item in value.items()}
            return value

        return replace_value(data)

    def _get_page(self, account_id: str, provider_mode: str | None = None) -> Any:
        if not self.browser_manager.is_available():
            raise BalePluginError(
                "unknown_error",
                "Playwright is not available. Install dependencies and browser binaries.",
            )
        account = bale_account_store.get_account(account_id) or {}
        effective_provider = str(provider_mode or account.get("browser_provider") or "native_chrome")
        if effective_provider == "adspower":
            health = get_provider("adspower").health_check()
            if not health.get("ok"):
                raise BalePluginError(
                    "adspower_unavailable",
                    "AdsPower در دسترس نیست. برای تست محلی از Chrome معمولی استفاده کنید.",
                )
        profile_metadata = {**account, "browser_provider": effective_provider}
        if effective_provider == "native_chrome":
            profile_metadata["adspower_profile_id"] = ""
        return self.browser_manager.get_page(
            account_id,
            headless=False,
            login_required=True,
            profile_metadata=profile_metadata,
        )

    @contextmanager
    def _page_session(self, account_id: str, provider_mode: str | None = None) -> Any:
        account = bale_account_store.get_account(account_id) or {}
        effective_provider = str(provider_mode or account.get("browser_provider") or "native_chrome")
        if effective_provider == "native_chrome" and self.browser_manager is actions_browser.browser_manager:
            with self._isolated_native_chrome_page(account_id) as session:
                yield session
            return
        yield (
            self._get_page(account_id, provider_mode),
            {
                "provider_mode": effective_provider,
                "browser_reused": True,
                "profile_dir": str(account.get("user_data_dir") or ""),
                "browser_path": getattr(self.browser_manager, "last_browser_path", None),
            },
        )

    @contextmanager
    def _isolated_native_chrome_page(self, account_id: str) -> Any:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise BalePluginError("unknown_error", "Playwright is not installed or not importable") from exc

        browser_path = resolve_system_browser_executable()
        self.browser_manager.last_browser_path = browser_path
        if not browser_path:
            raise BalePluginError("browser_start_timeout", "No system Chrome/Edge found")

        profile_dir = _native_profile_dir(account_id)
        profile_dir.mkdir(parents=True, exist_ok=True)
        playwright = None
        context = None
        try:
            playwright = sync_playwright().start()
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                executable_path=browser_path,
                headless=False,
                args=[],
            )
            page = context.pages[0] if context.pages else context.new_page()
            yield (
                page,
                {
                    "provider_mode": "native_chrome",
                    "browser_reused": False,
                    "profile_dir": str(profile_dir),
                    "browser_path": browser_path,
                },
            )
        finally:
            if context is not None:
                context.close()
            if playwright is not None:
                playwright.stop()

    def _browser_failure_meta(self, account_id: str, provider_mode: str) -> dict[str, Any]:
        if provider_mode == "native_chrome":
            profile_dir = _native_profile_dir(account_id)
            profile_dir.mkdir(parents=True, exist_ok=True)
            return {
                "provider_mode": "native_chrome",
                "browser_reused": False,
                "profile_dir": str(profile_dir),
            }
        account = bale_account_store.get_account(account_id) or {}
        return {
            "provider_mode": provider_mode,
            "browser_reused": True,
            "profile_dir": str(account.get("user_data_dir") or ""),
        }

    def _detect_login_state(self, page: Any, timeout_ms: int = 5000) -> dict[str, Any]:
        current_url = _safe_page_url(page)
        login_page_detected = "/login" in current_url.lower()
        install_prompt = self._first_visible_selector(page, _INSTALL_PROMPT_SELECTORS, timeout_ms=1000)
        login_form = self._first_visible_selector(page, _LOGIN_FORM_SELECTORS, timeout_ms=1000)

        dialog_selector = self._first_visible_selector(page, selectors.CHAT_ITEM_SELECTORS, timeout_ms=1000)
        message_selector = self._first_visible_selector(page, selectors.MESSAGE_INPUT_SELECTORS, timeout_ms=1000)
        search_icon_selector = self._first_visible_selector(page, selectors.SEARCH_ICON_SELECTORS, timeout_ms=1000)
        matched_selector = self._first_visible_selector(
            page,
            _CHAT_UI_SELECTORS,
            timeout_ms=timeout_ms,
        )
        search_selector = self._first_visible_selector(page, selectors.SEARCH_INPUT_SELECTORS, timeout_ms=1000)
        url_chat_detected = "/chat" in current_url.lower()
        chat_ui_detected = any([matched_selector, search_selector, message_selector, dialog_selector, search_icon_selector, url_chat_detected])
        blocking_login_ui = bool(login_form or install_prompt)
        logged_in = bool(chat_ui_detected and not blocking_login_ui)

        return {
            "logged_in": logged_in,
            "current_url": current_url,
            "matched_selector": matched_selector or dialog_selector or message_selector or search_icon_selector or search_selector or ("url:/chat" if url_chat_detected else ""),
            "login_page_detected": bool(login_page_detected or login_form),
            "install_prompt_detected": bool(install_prompt),
            "chat_ui_detected": bool(chat_ui_detected),
            "dialog_items_detected": bool(dialog_selector),
            "search_input_detected": bool(search_selector),
            "search_icon_detected": bool(search_icon_selector),
            "message_input_detected": bool(message_selector),
            "login_form_selector": login_form or "",
            "install_prompt_selector": install_prompt or "",
        }

    def _page_debug_info(self, page: Any, account_id: str) -> dict[str, Any]:
        info: dict[str, Any] = {
            "current_url": _safe_page_url(page),
            "page_title": _safe_page_title(page),
        }
        screenshot_path = _save_login_debug_screenshot(page, account_id)
        if screenshot_path:
            info["screenshot_path"] = screenshot_path
        return info

    def _return_to_chat_after_contact_save(self, page: Any, contact_naming_value: str, normalized_phone: str) -> dict[str, Any]:
        started = time.perf_counter()
        result: dict[str, Any] = {
            "step": "return_to_chat_after_contact_save",
            "status": "failed",
            "contact_naming_value": contact_naming_value,
            "normalized_phone": normalized_phone,
        }

        modal_was_visible = self._is_contact_modal_visible(page)
        if modal_was_visible:
            self._close_contact_modal_if_open(page)

        main_ready = self._first_visible_selector(
            page,
            selectors.SEARCH_ICON_SELECTORS + selectors.TEXT_SEARCH_INPUT_SELECTORS + selectors.CHAT_ITEM_SELECTORS,
            timeout_ms=250,
        )
        contacts_visible = self._contacts_ui_visible(page)
        if main_ready and not contacts_visible:
            result.update(
                {
                    "status": "success",
                    "mode": "already_ready",
                    "main_chat_ui_visible": True,
                    "contacts_ui_visible": False,
                    "ready_selector": main_ready,
                    "page_url": _safe_page_url(page),
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                }
            )
            return result

        if "/contacts" in _safe_page_url(page).lower() or contacts_visible:
            try:
                page.goto(self.web_url, wait_until="load")
                result["navigation"] = "goto_home"
            except Exception as exc:
                result["navigation_error"] = str(exc)

        main_ready = self._first_visible_selector(
            page,
            selectors.SEARCH_ICON_SELECTORS + selectors.TEXT_SEARCH_INPUT_SELECTORS + selectors.CHAT_ITEM_SELECTORS,
            timeout_ms=800,
        )
        if not main_ready:
            chat_entrypoint = self._first_visible_selector(page, selectors.CHAT_PAGE_ENTRYPOINT_SELECTORS, timeout_ms=300)
            if chat_entrypoint:
                self._click_if_possible(page, chat_entrypoint)
                result["chat_entrypoint_selector"] = chat_entrypoint
                main_ready = self._first_visible_selector(
                    page,
                    selectors.SEARCH_ICON_SELECTORS + selectors.TEXT_SEARCH_INPUT_SELECTORS + selectors.CHAT_ITEM_SELECTORS,
                    timeout_ms=700,
                )

        diagnostics = self._post_save_ui_diagnostics(page, contact_naming_value, normalized_phone, searched_value="")
        result.update(diagnostics)
        result["duration_ms"] = int((time.perf_counter() - started) * 1000)
        if main_ready:
            result.update({"status": "success", "ready_selector": main_ready, "main_chat_ui_visible": True})
            return result

        result.update({"error_code": "main_chat_ui_not_ready", "failed_step": "return_to_chat_after_contact_save"})
        return result

    def _open_target_chat(self, page: Any, contact_naming_value: str, normalized_phone: str) -> dict[str, Any]:
        started = time.perf_counter()
        search_attempts: list[dict[str, Any]] = []
        search_open = self._open_chat_search_from_main_ui(page, contact_naming_value, normalized_phone)
        search_input = str(search_open.get("search_input_selector") or "")
        if not search_input:
            diagnostics = self._post_save_ui_diagnostics(page, contact_naming_value, normalized_phone, searched_value=contact_naming_value or normalized_phone)
            return {
                "step": "open_target_chat",
                "status": "failed",
                "error_code": "search_input_not_found",
                "page_url": _safe_page_url(page),
                "active_element": search_open.get("active_element") or self._active_element_info(page),
                "search_icon_visible": search_open.get("search_icon_visible", False),
                "search_icon_clicked": search_open.get("search_icon_clicked", ""),
                "activation_attempts": search_open.get("activation_attempts", []),
                "dom_diagnostics": self._compact_chat_search_dom_diagnostics(page),
                "search_attempts": [{"query": contact_naming_value or normalized_phone, "reason": "search_input_not_found"}],
                "duration_ms": int((time.perf_counter() - started) * 1000),
                **diagnostics,
            }

        for query in [contact_naming_value, normalized_phone]:
            query = str(query or "").strip()
            if not query:
                continue
            attempt = {"query": query, "matched": False}
            search_attempts.append(attempt)
            if len(search_attempts) > 1:
                self._clear_search_input(page, search_input)
            self._fill_or_type(page, search_input, query)
            candidates = self._collect_search_result_candidates(page, timeout_ms=1500)
            match = self._match_search_result_candidate(candidates, query, contact_naming_value, normalized_phone)
            attempt["result_candidate_count"] = len(candidates)
            attempt["result_candidates_text"] = [item.get("text", "") for item in candidates[:15]]
            attempt["normalized_candidates"] = [item.get("normalized_text", "") for item in candidates[:15]]
            if match:
                click_result = self._click_search_result_candidate(page, match)
                attempt["matched"] = click_result["status"] == "success"
                attempt["matched_selector"] = match.get("click_selector") or match.get("selector")
                attempt["matched_candidate_text"] = match.get("text", "")
                attempt["match_mode"] = match.get("match_mode", "normalized_text_match")
                attempt["click_method"] = click_result.get("click_method", "")
                chat_opened = self._confirm_chat_opened(page)
                attempt["chat_open_confirmed"] = chat_opened
                if chat_opened:
                    return {
                        "step": "open_target_chat",
                        "status": "success",
                        "matched_selector": attempt["matched_selector"],
                        "matched_candidate_text": attempt["matched_candidate_text"],
                        "search_attempts": search_attempts,
                        "chat_open_confirmed": True,
                        "duration_ms": int((time.perf_counter() - started) * 1000),
                    }
                attempt["reason"] = "chat_open_not_confirmed"
            else:
                attempt["reason"] = "dialog_item_not_found"
        searched_value = search_attempts[-1]["query"] if search_attempts else ""
        last_attempt = search_attempts[-1] if search_attempts else {}
        contacts_fallback = self._open_chat_from_contacts_fallback(page, contact_naming_value, normalized_phone)
        if contacts_fallback.get("status") == "success":
            return {
                "step": "open_target_chat",
                "status": "success",
                "search_attempts": search_attempts,
                "chat_search_no_result": True,
                "contacts_fallback_attempted": True,
                "contacts_fallback": contacts_fallback,
                "contacts_query_attempts": contacts_fallback.get("contacts_query_attempts", []),
                "matched_selector": contacts_fallback.get("matched_selector", ""),
                "matched_candidate_text": contacts_fallback.get("matched_contact_text", ""),
                "chat_open_confirmed": True,
                "duration_ms": int((time.perf_counter() - started) * 1000),
            }
        return {
            "step": "open_target_chat",
            "status": "failed",
            "error_code": "target_not_found",
            "search_attempts": search_attempts,
            "chat_search_no_result": True,
            "contacts_fallback_attempted": True,
            "contacts_fallback": contacts_fallback,
            "contacts_query_attempts": contacts_fallback.get("contacts_query_attempts", []),
            "contacts_search_value": contacts_fallback.get("contacts_search_value", ""),
            "contacts_result_count": contacts_fallback.get("contacts_result_count", 0),
            "contacts_result_text": contacts_fallback.get("contacts_result_text", []),
            "matched_contact_text": contacts_fallback.get("matched_contact_text", ""),
            "matched_contact_role": contacts_fallback.get("matched_contact_role", ""),
            "matched_contact_box": contacts_fallback.get("matched_contact_box", {}),
            "right_chat_header_text": contacts_fallback.get("right_chat_header_text", ""),
            "contact_profile_visible": contacts_fallback.get("contact_profile_visible", False),
            "message_button_visible": contacts_fallback.get("message_button_visible", False),
            "contacts_search_input_selector": contacts_fallback.get("contacts_search_input_selector", ""),
            "contacts_search_input_value": contacts_fallback.get("contacts_search_input_value", ""),
            "contacts_search_input_visible": contacts_fallback.get("contacts_search_input_visible", False),
            "contacts_list_scoped": contacts_fallback.get("contacts_list_scoped", False),
            "chat_url_has_uid": contacts_fallback.get("chat_url_has_uid", False),
            "message_input_visible": contacts_fallback.get("message_input_visible", False),
            "contacts_search_ready_attempts": contacts_fallback.get("contacts_search_ready_attempts", []),
            "contacts_search_icon_visible": contacts_fallback.get("contacts_search_icon_visible", False),
            "contacts_search_icon_clicked": contacts_fallback.get("contacts_search_icon_clicked", ""),
            "contacts_page_url_before": contacts_fallback.get("contacts_page_url_before", ""),
            "contacts_page_url_after": contacts_fallback.get("contacts_page_url_after", ""),
            "contacts_header_text": contacts_fallback.get("contacts_header_text", ""),
            "visible_inputs": contacts_fallback.get("visible_inputs", []),
            "visible_top_svgs": contacts_fallback.get("visible_top_svgs", []),
            "contacts_panel_box": contacts_fallback.get("contacts_panel_box", {}),
            "search_input_value": self._search_input_value(page, search_input),
            "result_candidate_count": last_attempt.get("result_candidate_count", 0),
            "result_candidates_text": last_attempt.get("result_candidates_text", []),
            "matched_candidate_text": last_attempt.get("matched_candidate_text", ""),
            "normalized_candidates": last_attempt.get("normalized_candidates", []),
            "visible_no_result_text": self._visible_no_result_text(page),
            "search_url": _safe_page_url(page),
            "active_element": self._active_element_info(page),
            "search_input_placeholder": self._locator_attribute(page, search_input, "placeholder"),
            "duration_ms": int((time.perf_counter() - started) * 1000),
            **self._post_save_ui_diagnostics(page, contact_naming_value, normalized_phone, searched_value=searched_value),
        }

    def _save_contact_from_modal(
        self,
        page: Any,
        normalized_phone: str,
        contact_naming_value: str,
        account_id: str | None = None,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        contact_name = str(contact_naming_value or normalized_phone).strip()
        phone_value = phone_for_bale_contact_field(normalized_phone)
        result: dict[str, Any] = {
            "step": "save_or_resolve_contact",
            "status": "failed",
            "contact_save_status": "failed",
            "contact_name": contact_name,
            "normalized_phone": normalized_phone,
            "contact_phone_value": phone_value,
            "contact_steps": [],
        }
        if account_id:
            result["account_id"] = account_id
        if job_id:
            result["job_id"] = job_id

        contact_steps = result["contact_steps"]
        def add_contact_step(step: str, status: str, started: float, **details: Any) -> None:
            contact_steps.append({"step": step, "status": status, "duration_ms": int((time.perf_counter() - started) * 1000), **details})

        contacts_url = f"{self.web_url}/contacts"
        current_url = _safe_page_url(page).lower()
        contacts_entrypoint = ""
        step_started = time.perf_counter()
        if "/contacts" in current_url:
            add_contact_step("open_contacts", "success", step_started, mode="already_open", current_url=_safe_page_url(page))
        else:
            try:
                page.goto(contacts_url, wait_until="load")
                current_url = _safe_page_url(page).lower()
            except Exception as exc:
                result["open_contacts_navigation_error"] = str(exc)
            if "/contacts" in current_url:
                add_contact_step("open_contacts", "success", step_started, mode="navigate", url=contacts_url, current_url=_safe_page_url(page))
            else:
                contacts_entrypoint = self._first_visible_selector(page, selectors.CONTACTS_PAGE_ENTRYPOINT_SELECTORS, timeout_ms=1000) or ""
                if contacts_entrypoint:
                    self._click_if_possible(page, contacts_entrypoint)
                    result["contacts_entrypoint_selector"] = contacts_entrypoint
                    add_contact_step("open_contacts", "success", step_started, mode="click_icon", selector=contacts_entrypoint)

        if not contact_steps or contact_steps[-1]["status"] != "success":
            result["error_code"] = "contacts_page_not_opened"
            result["reason"] = "contacts_entrypoint_not_found"
            result["message"] = "Bale contacts page entrypoint was not found"
            add_contact_step("open_contacts", "failed", step_started, error_code="contacts_page_not_opened")
            return result

        step_started = time.perf_counter()
        contacts_ready = self._first_visible_selector(page, selectors.CONTACTS_UI_READY_SELECTORS, timeout_ms=1500)
        if not contacts_ready:
            result["error_code"] = "contacts_ui_not_ready"
            result["reason"] = "contacts_ui_not_ready"
            result["message"] = "Bale contacts UI was not ready"
            add_contact_step("wait_contacts_ui", "failed", step_started, error_code="contacts_ui_not_ready")
            return result
        add_contact_step("wait_contacts_ui", "success", step_started, selector=contacts_ready)

        step_started = time.perf_counter()
        entrypoint = self._first_visible_selector(page, selectors.ADD_CONTACT_ENTRYPOINT_SELECTORS, timeout_ms=1000)
        if not entrypoint:
            result["error_code"] = "add_contact_entrypoint_not_found"
            result["reason"] = "add_contact_entrypoint_not_found"
            result["message"] = "Bale Add Contact entrypoint was not found"
            add_contact_step("open_add_contact_menu", "failed", step_started, error_code="add_contact_entrypoint_not_found")
            return result
        self._click_if_possible(page, entrypoint)
        result["entrypoint_selector"] = entrypoint
        add_contact_step("open_add_contact_menu", "success", step_started, selector=entrypoint)

        step_started = time.perf_counter()
        menu_item = self._first_visible_selector(page, selectors.ADD_CONTACT_MENU_ITEM_SELECTORS, timeout_ms=1000)
        if not menu_item:
            result["error_code"] = "add_contact_menu_item_not_found"
            result["reason"] = "add_contact_menu_item_not_found"
            result["message"] = "Bale Add Contact menu item was not found"
            add_contact_step("wait_add_contact_menu_item", "failed", step_started, error_code="add_contact_menu_item_not_found")
            return result
        add_contact_step("wait_add_contact_menu_item", "success", step_started, selector=menu_item)
        step_started = time.perf_counter()
        self._click_if_possible(page, menu_item)
        result["menu_item_selector"] = menu_item
        add_contact_step("open_add_contact_modal", "success", step_started, selector=menu_item)

        step_started = time.perf_counter()
        modal = self._first_visible_selector(page, selectors.ADD_CONTACT_MODAL_SELECTORS, timeout_ms=2500)
        if modal:
            result["modal_selector"] = modal
            add_contact_step("wait_add_contact_modal", "success", step_started, selector=modal)
        else:
            result["error_code"] = "add_contact_modal_not_found"
            result["reason"] = "add_contact_modal_not_found"
            result["message"] = "Bale Add Contact modal was not found"
            add_contact_step("wait_add_contact_modal", "failed", step_started, error_code="add_contact_modal_not_found")
            return result

        step_started = time.perf_counter()
        phone_mode = self._first_visible_selector(page, selectors.ADD_CONTACT_PHONE_MODE_SELECTORS, timeout_ms=100)
        if phone_mode:
            self._click_if_possible(page, phone_mode)
            result["phone_mode_selector"] = phone_mode
            add_contact_step("select_mobile_number_tab", "success", step_started, selector=phone_mode)
        else:
            add_contact_step("select_mobile_number_tab", "skipped", step_started, reason="already_default_or_not_required")
        username_mode = self._first_visible_selector(page, selectors.ADD_CONTACT_USERNAME_MODE_SELECTORS, timeout_ms=300)
        if username_mode:
            result["username_mode_selector"] = username_mode

        modal_inputs = self._visible_modal_inputs(page, modal)
        name_selector = self._first_visible_selector(page, selectors.ADD_CONTACT_NAME_INPUT_SELECTORS, timeout_ms=1500)
        name_input_fallback_used = False
        if not name_selector:
            name_selector = self._first_visible_selector(page, selectors.ADD_CONTACT_NAME_INPUT_FALLBACK_SELECTORS, timeout_ms=750)
            name_input_fallback_used = bool(name_selector)
        phone_selector = self._first_visible_selector(page, selectors.ADD_CONTACT_PHONE_INPUT_SELECTORS, timeout_ms=1500)
        country_selector = ""
        phone_input_fallback_used = False
        if not phone_selector:
            country_selector = self._first_visible_selector(page, selectors.ADD_CONTACT_COUNTRY_SELECTOR_SELECTORS, timeout_ms=500) or ""
            if country_selector:
                result["country_selector"] = country_selector
                phone_selector = self._first_visible_selector(page, selectors.ADD_CONTACT_PHONE_INPUT_FALLBACK_SELECTORS, timeout_ms=750)
                phone_input_fallback_used = bool(phone_selector)
        if not phone_selector:
            phone_selector = self._modal_phone_input_selector(modal_inputs)
            phone_input_fallback_used = bool(phone_selector)
        if not name_selector:
            name_selector = self._modal_name_input_selector(modal_inputs, exclude_selector=phone_selector)
            name_input_fallback_used = bool(name_selector)
        if not name_selector or not phone_selector:
            result.update(
                {
                    "error_code": "add_contact_modal_fields_not_found",
                    "reason": "add_contact_modal_fields_not_found",
                    "message": "Bale Add Contact modal fields were not found",
                    "name_input_found": bool(name_selector),
                    "phone_input_found": bool(phone_selector),
                    "country_selector_found": bool(country_selector),
                }
            )
            return result

        first_name, last_name = _split_contact_name(contact_name)
        step_started = time.perf_counter()
        try:
            self._fill_or_type(page, phone_selector, phone_value)
        except Exception as exc:
            result.update(
                {
                    "error_code": "fill_phone_failed",
                    "reason": "fill_phone_failed",
                    "message": "Bale Add Contact phone field could not be filled",
                    "phone_input_selector": phone_selector,
                    "error": str(exc),
                }
            )
            add_contact_step("fill_phone", "failed", step_started, error_code="fill_phone_failed", selector=phone_selector)
            return result
        add_contact_step("fill_phone", "success", step_started, selector=phone_selector, value=phone_value)

        step_started = time.perf_counter()
        try:
            self._fill_or_type(page, name_selector, first_name)
        except Exception as exc:
            result.update(
                {
                    "error_code": "fill_name_failed",
                    "reason": "fill_name_failed",
                    "message": "Bale Add Contact name field could not be filled",
                    "name_input_selector": name_selector,
                    "error": str(exc),
                }
            )
            add_contact_step("fill_name", "failed", step_started, error_code="fill_name_failed", selector=name_selector)
            return result
        add_contact_step("fill_name", "success", step_started, selector=name_selector, value=first_name)
        last_name_selector = self._first_visible_selector(page, selectors.ADD_CONTACT_LAST_NAME_INPUT_SELECTORS, timeout_ms=300)
        if last_name_selector and last_name:
            self._fill_or_type(page, last_name_selector, last_name)

        step_started = time.perf_counter()
        button_result = self._click_enabled_add_contact_button(page)
        if button_result["status"] != "success":
            button_details = {key: value for key, value in button_result.items() if key != "status"}
            result.update(
                {
                    "contact_save_status": "failed",
                    "status": "failed",
                    "error_code": "add_contact_button_disabled",
                    "reason": "add_contact_button_disabled",
                    "failed_step": "click_add_contact",
                    "user_message": "دکمه افزودن مخاطب فعال نشد",
                    "message": "Bale Add Contact button was not enabled",
                    "name_input_selector": name_selector,
                    "phone_input_selector": phone_selector,
                }
            )
            add_contact_step("click_add_contact", "failed", step_started, **button_details)
            return result

        save_button = str(button_result.get("selector") or "")
        button_details = {key: value for key, value in button_result.items() if key != "status"}
        add_contact_step("click_add_contact", "success", step_started, **button_details)
        step_started = time.perf_counter()
        confirmation = self._confirm_contact_saved(page, modal, contact_name, normalized_phone, phone_value, timeout_ms=5000)
        confirmation_details = {key: value for key, value in confirmation.items() if key != "status"}
        add_contact_step("confirm_contact_saved", confirmation["status"], step_started, **confirmation_details)
        if confirmation["status"] == "failed":
            result.update(
                {
                    "status": "failed",
                    "contact_save_status": "failed",
                    "error_code": "contact_save_not_confirmed",
                    "reason": "contact_save_not_confirmed",
                    "failed_step": "confirm_contact_saved",
                    "user_message": "ذخیره مخاطب در بله تایید نشد",
                    "message": "Bale contact save was not confirmed",
                    "name_input_selector": name_selector,
                    "phone_input_selector": phone_selector,
                    "save_button_selector": save_button,
                }
            )
            if account_id:
                screenshot_path = _save_login_debug_screenshot(page, account_id)
                if screenshot_path:
                    result["screenshot_path"] = screenshot_path
            return result

        self._close_contact_modal_if_open(page)
        result.update(
            {
                "status": "success",
                "contact_save_status": confirmation["contact_save_status"],
                "message": "Bale contact saved",
                "name_input_selector": name_selector,
                "name_input_fallback_used": name_input_fallback_used,
                "last_name_input_selector": last_name_selector or "",
                "phone_input_selector": phone_selector,
                "phone_input_fallback_used": phone_input_fallback_used,
                "save_button_selector": save_button,
            }
        )
        return result

    def _visible_modal_inputs(self, page: Any, modal_selector: str) -> list[dict[str, str]]:
        inputs: list[dict[str, str]] = []
        roots = [modal_selector, ".ReactModal__Content", ".ReactModal__Overlay", '[role="dialog"]']
        seen: set[str] = set()
        for root in roots:
            if not root:
                continue
            input_group = f"{root} input"
            try:
                count = page.locator(input_group).count()
            except Exception:
                count = 0
            for index in range(count):
                selector = f"{input_group} >> nth={index}"
                if selector in seen:
                    continue
                try:
                    locator = page.locator(input_group).nth(index)
                    locator.wait_for(state="visible", timeout=1000)
                    placeholder = str(locator.get_attribute("placeholder", timeout=1000) or "")
                    input_type = str(locator.get_attribute("type", timeout=1000) or "text")
                except Exception:
                    continue
                if input_type.lower() in {"hidden", "button", "submit"}:
                    continue
                seen.add(selector)
                inputs.append({"selector": selector, "placeholder": placeholder, "type": input_type})
        return inputs

    def _modal_phone_input_selector(self, modal_inputs: list[dict[str, str]]) -> str:
        for item in modal_inputs:
            placeholder = item.get("placeholder", "")
            if any(token in placeholder for token in ("912", "345", "6789")):
                return item.get("selector", "")
        return modal_inputs[0].get("selector", "") if modal_inputs else ""

    def _modal_name_input_selector(self, modal_inputs: list[dict[str, str]], exclude_selector: str | None = None) -> str:
        for item in modal_inputs:
            selector = item.get("selector", "")
            if selector == exclude_selector:
                continue
            placeholder = item.get("placeholder", "")
            if any(token in placeholder for token in ("Name", "required", "نام")):
                return selector
        remaining = [item.get("selector", "") for item in modal_inputs if item.get("selector") != exclude_selector]
        if len(remaining) >= 2:
            return remaining[1]
        return remaining[0] if remaining else ""

    def _click_enabled_add_contact_button(self, page: Any) -> dict[str, Any]:
        button_selectors = [
            '.ReactModal__Overlay button:has-text("افزودن")',
            '.ReactModal__Overlay button:has-text("Add")',
            '.ReactModal__Overlay [role="button"]:has-text("افزودن")',
            '.ReactModal__Overlay [role="button"]:has-text("Add")',
            '.ReactModal__Content button:has-text("افزودن")',
            '.ReactModal__Content button:has-text("Add")',
            '.ReactModal__Content [role="button"]:has-text("افزودن")',
            '.ReactModal__Content [role="button"]:has-text("Add")',
            '[role="dialog"] button:has-text("افزودن")',
            '[role="dialog"] button:has-text("Add")',
            '[role="dialog"] [role="button"]:has-text("افزودن")',
            '[role="dialog"] [role="button"]:has-text("Add")',
        ]
        button_count = 0
        disabled_seen = False
        last_selector = ""
        last_text = ""
        for selector in button_selectors:
            locator = page.locator(selector).first
            try:
                locator.wait_for(state="visible", timeout=250)
            except Exception:
                continue
            button_count += 1
            last_selector = selector
            last_text = self._locator_text(locator)
            button_disabled = self._locator_disabled(locator)
            if button_disabled:
                disabled_seen = True
                continue
            try:
                locator.click(timeout=2000)
                return {
                    "status": "success",
                    "selector": selector,
                    "button_text": last_text,
                    "button_disabled": False,
                    "button_count": button_count,
                    "click_method": "playwright_click",
                }
            except Exception as click_error:
                try:
                    locator.evaluate("(el) => el.click()")
                    return {
                        "status": "success",
                        "selector": selector,
                        "button_text": last_text,
                        "button_disabled": False,
                        "button_count": button_count,
                        "click_method": "dom_click",
                        "playwright_click_error": str(click_error),
                    }
                except Exception as dom_error:
                    return {
                        "status": "failed",
                        "selector": selector,
                        "button_text": last_text,
                        "button_disabled": False,
                        "button_count": button_count,
                        "click_error": str(click_error),
                        "dom_click_error": str(dom_error),
                    }

        return {
            "status": "failed",
            "selector": last_selector,
            "button_text": last_text,
            "button_disabled": disabled_seen,
            "button_count": button_count,
            "error_code": "add_contact_button_disabled",
        }

    def _locator_disabled(self, locator: Any) -> bool:
        try:
            if hasattr(locator, "is_enabled") and not locator.is_enabled(timeout=250):
                return True
        except Exception:
            pass
        try:
            disabled_attr = locator.get_attribute("disabled", timeout=250)
            if disabled_attr is not None:
                return True
        except Exception:
            pass
        try:
            aria_disabled = str(locator.get_attribute("aria-disabled", timeout=250) or "").lower()
            if aria_disabled == "true":
                return True
        except Exception:
            pass
        return False

    def _locator_text(self, locator: Any) -> str:
        try:
            return str(locator.inner_text(timeout=250) or "")
        except Exception:
            return ""

    def _confirm_contact_saved(
        self,
        page: Any,
        modal_selector: str,
        contact_name: str,
        normalized_phone: str,
        phone_value: str,
        timeout_ms: int = 4500,
    ) -> dict[str, Any]:
        duplicate_selectors = [
            'text=/.*already.*exists.*/i',
            'text=/.*duplicate.*/i',
            'text=/.*قبلا.*/',
            'text=/.*موجود.*/',
            'text=/.*تکراری.*/',
        ]
        contact_selectors = [
            f"text={contact_name}",
            f"text={normalized_phone}",
            f"text={phone_value}",
        ]
        deadline = time.monotonic() + (timeout_ms / 1000)
        while time.monotonic() < deadline:
            if not self._is_selector_visible(page, modal_selector, timeout_ms=150):
                return {
                    "status": "success",
                    "contact_save_status": "saved",
                    "confirmation": "modal_closed",
                }

            duplicate = self._first_visible_selector(page, duplicate_selectors, timeout_ms=100)
            if duplicate:
                return {
                    "status": "success",
                    "contact_save_status": "already_exists",
                    "confirmation": "duplicate_detected",
                    "selector": duplicate,
                }

            contact_match = self._first_visible_selector(page, contact_selectors, timeout_ms=100)
            if contact_match:
                return {
                    "status": "success",
                    "contact_save_status": "saved",
                    "confirmation": "contact_list_updated",
                    "selector": contact_match,
                }
            time.sleep(0.2)

        return {
            "status": "failed",
            "contact_save_status": "failed",
            "error_code": "contact_save_not_confirmed",
            "modal_still_open": self._is_selector_visible(page, modal_selector, timeout_ms=250),
            "overlay_present": self._is_contact_modal_visible(page),
        }

    def _close_contact_modal_if_open(self, page: Any) -> None:
        if not self._is_contact_modal_visible(page):
            return
        try:
            self._press_key(page, "Escape")
        except Exception:
            pass
        if not self._is_contact_modal_visible(page):
            return
        close_selector = self._first_visible_selector(
            page,
            [
                ".ReactModal__Content button[aria-label*='Close']",
                ".ReactModal__Content button:has-text('×')",
                ".ReactModal__Content button:has-text('Cancel')",
                ".ReactModal__Content button:has-text('لغو')",
                "[role='dialog'] button[aria-label*='Close']",
            ],
            timeout_ms=500,
        )
        if close_selector:
            self._click_if_possible(page, close_selector)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if not self._is_contact_modal_visible(page):
                return
            time.sleep(0.1)

    def _is_contact_modal_visible(self, page: Any) -> bool:
        return bool(self._first_visible_selector(page, selectors.ADD_CONTACT_MODAL_SELECTORS, timeout_ms=100))

    def _contacts_ui_visible(self, page: Any) -> bool:
        if "/contacts" in _safe_page_url(page).lower():
            return True
        return bool(self._first_visible_selector(page, selectors.CONTACTS_UI_READY_SELECTORS, timeout_ms=100))

    def _main_chat_ui_visible(self, page: Any) -> bool:
        return bool(
            self._first_visible_selector(
                page,
                selectors.SEARCH_ICON_SELECTORS + selectors.TEXT_SEARCH_INPUT_SELECTORS + selectors.CHAT_ITEM_SELECTORS,
                timeout_ms=100,
            )
        )

    def _open_chat_search_from_main_ui(self, page: Any, contact_naming_value: str, normalized_phone: str) -> dict[str, Any]:
        started = time.perf_counter()
        attempts: list[dict[str, Any]] = []
        result: dict[str, Any] = {
            "status": "failed",
            "search_input_selector": "",
            "search_icon_visible": False,
            "search_icon_clicked": "",
            "activation_attempts": attempts,
            "contact_naming_value": contact_naming_value,
            "normalized_phone": normalized_phone,
        }

        def check_input(mode: str, timeout_ms: int = 200) -> str:
            selector = self._first_visible_selector(page, selectors.TEXT_SEARCH_INPUT_SELECTORS, timeout_ms=timeout_ms)
            if selector:
                attempts.append({"mode": mode, "status": "success", "selector": selector})
                return selector
            focused = self._focused_editable_selector(page)
            if focused:
                attempts.append({"mode": mode, "status": "success", "selector": focused, "focused": True})
                return focused
            attempts.append({"mode": mode, "status": "failed"})
            return ""

        search_input = check_input("existing_input", timeout_ms=200)
        if search_input:
            result.update({"status": "success", "search_input_selector": search_input, "duration_ms": int((time.perf_counter() - started) * 1000)})
            return result

        search_icon = self._first_visible_selector(page, selectors.SEARCH_ICON_SELECTORS, timeout_ms=300)
        result["search_icon_visible"] = bool(search_icon)
        if search_icon:
            click_result = self._click_chat_search_icon_target(page, search_icon)
            result.update({key: value for key, value in click_result.items() if key != "status"})
            if click_result.get("status") == "success":
                result["search_icon_clicked"] = str(click_result.get("selector") or "")
            attempts.append({"mode": "click_search_icon", **click_result})
            search_input = check_input("after_search_icon", timeout_ms=350)
            if search_input:
                result.update({"status": "success", "search_input_selector": search_input, "duration_ms": int((time.perf_counter() - started) * 1000)})
                return result

        for shortcut in ["Control+K", "Control+F"]:
            if int((time.perf_counter() - started) * 1000) >= 1500:
                break
            try:
                self._press_key(page, shortcut)
                attempts.append({"mode": "keyboard_shortcut", "status": "success", "shortcut": shortcut})
            except Exception as exc:
                attempts.append({"mode": "keyboard_shortcut", "status": "failed", "shortcut": shortcut, "error": str(exc)})
            search_input = check_input(f"after_{shortcut}", timeout_ms=300)
            if search_input:
                result.update({"status": "success", "search_input_selector": search_input, "duration_ms": int((time.perf_counter() - started) * 1000)})
                return result

        result.update(
            {
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "active_element": self._active_element_info(page),
            }
        )
        return result

    def _click_chat_search_icon_target(self, page: Any, search_icon_selector: str) -> dict[str, Any]:
        target = self._resolve_search_icon_click_target(page, search_icon_selector)
        selector = str(target.get("selector") or search_icon_selector)
        locator = page.locator(selector).first
        base = {
            "selector": selector,
            "search_icon_svg_selector": target.get("search_icon_svg_selector") or search_icon_selector,
            "clickable_parent_tag": target.get("tag", ""),
            "clickable_parent_class": target.get("className", ""),
            "clickable_parent_role": target.get("role", ""),
            "clickable_parent_aria_label": target.get("ariaLabel", ""),
            "clickable_parent_text": target.get("text", ""),
        }
        for method, kwargs in [
            ("playwright_click", {"timeout": 400}),
            ("force_click", {"timeout": 400, "force": True}),
        ]:
            try:
                locator.click(**kwargs)
                return {"status": "success", "click_method": method, **base}
            except Exception as exc:
                base["pointer_intercepted_by"] = _pointer_interceptor_from_error(str(exc)) or base.get("pointer_intercepted_by", "")
                base[f"{method}_error"] = str(exc)

        for method, script in [
            ("dom_click", "(el) => el.click()"),
            ("mouse_event_click", '(el) => el.dispatchEvent(new MouseEvent("click", {bubbles: true, cancelable: true, view: window}))'),
        ]:
            try:
                locator.evaluate(script)
                return {"status": "success", "click_method": method, **base}
            except Exception as exc:
                base[f"{method}_error"] = str(exc)

        try:
            box = locator.bounding_box(timeout=200)
            if box:
                page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                return {"status": "success", "click_method": "coordinate_click", **base}
        except Exception as exc:
            base["coordinate_click_error"] = str(exc)

        return {"status": "failed", "click_method": "", **base}

    def _resolve_search_icon_click_target(self, page: Any, search_icon_selector: str) -> dict[str, Any]:
        direct_target = self._first_visible_selector(page, selectors.SEARCH_ICON_CLICK_TARGET_SELECTORS, timeout_ms=150)
        if direct_target and direct_target not in selectors.SEARCH_ICON_SVG_SELECTORS:
            return self._click_target_info(page, direct_target, search_icon_selector)
        try:
            info = page.locator(search_icon_selector).first.evaluate(
                """(el) => {
                    let node = el;
                    for (let depth = 0; node && depth <= 5; depth += 1, node = node.parentElement) {
                        if (!(node instanceof HTMLElement)) continue;
                        const style = window.getComputedStyle(node);
                        const role = node.getAttribute("role") || "";
                        const clickable = node.tagName === "BUTTON" ||
                            role === "button" ||
                            style.cursor === "pointer" ||
                            typeof node.onclick === "function";
                        if (clickable && node !== el) {
                            return {
                                selector: node.id ? `#${node.id}` : "",
                                tag: node.tagName || "",
                                className: node.className || "",
                                role,
                                ariaLabel: node.getAttribute("aria-label") || "",
                                text: (node.innerText || node.textContent || "").trim().slice(0, 120)
                            };
                        }
                    }
                    return {};
                }"""
            )
            if isinstance(info, dict) and info:
                if not info.get("selector"):
                    info["selector"] = search_icon_selector + " >> xpath=ancestor::*[self::button or @role='button' or contains(@style,'cursor')][1]"
                info["search_icon_svg_selector"] = search_icon_selector
                return info
        except Exception:
            pass
        return self._click_target_info(page, search_icon_selector, search_icon_selector)

    def _click_target_info(self, page: Any, selector: str, svg_selector: str) -> dict[str, Any]:
        info = {
            "selector": selector,
            "search_icon_svg_selector": svg_selector,
            "tag": "",
            "className": "",
            "role": "",
            "ariaLabel": "",
            "text": "",
        }
        try:
            details = page.locator(selector).first.evaluate(
                """(el) => ({
                    tag: el.tagName || "",
                    className: el.className || "",
                    role: el.getAttribute("role") || "",
                    ariaLabel: el.getAttribute("aria-label") || "",
                    text: (el.innerText || el.textContent || "").trim().slice(0, 120)
                })"""
            )
            if isinstance(details, dict):
                info.update(details)
        except Exception:
            pass
        return info

    def _collect_search_result_candidates(self, page: Any, timeout_ms: int = 1500) -> list[dict[str, Any]]:
        deadline = time.monotonic() + (timeout_ms / 1000)
        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        while time.monotonic() < deadline:
            candidates = self._visible_search_result_candidates(page)
            filtered = []
            for item in candidates:
                key = str(item.get("selector") or item.get("text") or "")
                if not key or key in seen:
                    continue
                seen.add(key)
                filtered.append(item)
            if filtered:
                return filtered[:30]
            time.sleep(0.15)
        return candidates[:30]

    def _visible_search_result_candidates(self, page: Any) -> list[dict[str, Any]]:
        try:
            data = page.evaluate(
                """() => {
                    const visible = (el) => {
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style && style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
                    };
                    const clickableParent = (el) => {
                        let node = el;
                        for (let depth = 0; node && depth <= 5; depth += 1, node = node.parentElement) {
                            if (!(node instanceof HTMLElement)) continue;
                            const style = window.getComputedStyle(node);
                            const role = node.getAttribute("role") || "";
                            if (node.tagName === "BUTTON" || node.tagName === "A" || role === "button" || role === "listitem" || style.cursor === "pointer" || typeof node.onclick === "function") {
                                return node;
                            }
                        }
                        return el;
                    };
                    const selectorFor = (el) => {
                        if (!el) return "";
                        const tag = el.tagName ? el.tagName.toLowerCase() : "*";
                        if (el.id) return `${tag}#${CSS.escape(el.id)}`;
                        const aria = el.getAttribute("aria-label");
                        if (aria) return `${tag}[aria-label="${aria.replaceAll('"', '\\"')}"]`;
                        const role = el.getAttribute("role");
                        if (role) return `${tag}[role="${role}"]`;
                        const testid = el.getAttribute("data-testid");
                        if (testid) return `${tag}[data-testid="${testid.replaceAll('"', '\\"')}"]`;
                        return "";
                    };
                    const nodes = Array.from(document.querySelectorAll('[aria-label="dialog-item"], [data-testid="chat-list-item"], [role="listitem"], [data-testid*="chat"], [role="button"], a, div'));
                    const rows = [];
                    const seen = new Set();
                    for (const el of nodes) {
                        if (!visible(el)) continue;
                        const text = (el.innerText || el.textContent || "").trim();
                        if (!text || text.length > 500) continue;
                        const click = clickableParent(el);
                        const selector = selectorFor(el);
                        const clickSelector = selectorFor(click) || selector;
                        const key = `${selector}|${text}`;
                        if (seen.has(key)) continue;
                        seen.add(key);
                        rows.push({
                            selector,
                            click_selector: clickSelector,
                            text,
                            tag: el.tagName || "",
                            role: el.getAttribute("role") || "",
                            ariaLabel: el.getAttribute("aria-label") || "",
                            clickTag: click ? click.tagName || "" : "",
                            clickRole: click ? click.getAttribute("role") || "" : "",
                            clickText: click ? (click.innerText || click.textContent || "").trim().slice(0, 200) : ""
                        });
                        if (rows.length >= 30) break;
                    }
                    return rows;
                }"""
            )
            if isinstance(data, list):
                return [self._candidate_with_normalized_text(item) for item in data if isinstance(item, dict)]
        except Exception:
            pass
        return self._selector_search_result_candidates(page)

    def _selector_search_result_candidates(self, page: Any) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for selector in selectors.SEARCH_RESULT_CANDIDATE_SELECTORS:
            try:
                locator = page.locator(selector).first
                locator.wait_for(state="visible", timeout=150)
                text = str(locator.inner_text(timeout=150) or "").strip()
            except Exception:
                continue
            if not text:
                continue
            candidates.append(
                self._candidate_with_normalized_text(
                    {"selector": selector, "click_selector": selector, "text": text}
                )
            )
        return candidates[:30]

    def _candidate_with_normalized_text(self, item: dict[str, Any]) -> dict[str, Any]:
        text = str(item.get("text") or "")
        item["normalized_text"] = _normalize_bale_match_text(text)
        item["normalized_phone_text"] = _normalize_bale_phone_text(text)
        return item

    def _match_search_result_candidate(
        self,
        candidates: list[dict[str, Any]],
        query: str,
        contact_naming_value: str,
        normalized_phone: str,
    ) -> dict[str, Any] | None:
        name = str(contact_naming_value or "").strip().lower()
        phone_values = _bale_phone_match_values(normalized_phone)
        query_phone_values = _bale_phone_match_values(query)
        query_is_phone = len(_normalize_bale_phone_text(query)) >= 10
        all_phone_values = (phone_values | query_phone_values) if query_is_phone else set()
        for candidate in candidates:
            text = str(candidate.get("text") or "")
            normalized_text = str(candidate.get("normalized_text") or "")
            normalized_phone_text = str(candidate.get("normalized_phone_text") or "")
            if name and name in text.lower():
                candidate["match_mode"] = "contact_name"
                return candidate
            for phone_value in all_phone_values:
                if phone_value and phone_value in normalized_phone_text:
                    candidate["match_mode"] = "phone"
                    return candidate
            query_text = _normalize_bale_match_text(query)
            if query_text and query_text in normalized_text:
                candidate["match_mode"] = "query_text"
                return candidate
        return None

    def _click_search_result_candidate(self, page: Any, candidate: dict[str, Any]) -> dict[str, Any]:
        selector = str(candidate.get("click_selector") or candidate.get("selector") or "")
        if not selector:
            return {"status": "failed", "error": "candidate_selector_missing"}
        locator = page.locator(selector).first
        try:
            locator.click(timeout=800)
            return {"status": "success", "click_method": "playwright_click"}
        except Exception as click_error:
            try:
                locator.evaluate("(el) => el.click()")
                return {"status": "success", "click_method": "dom_click", "playwright_click_error": str(click_error)}
            except Exception as dom_error:
                return {"status": "failed", "click_error": str(click_error), "dom_click_error": str(dom_error)}

    def _confirm_chat_opened(self, page: Any) -> bool:
        if self._first_visible_selector(page, selectors.MESSAGE_INPUT_SELECTORS, timeout_ms=1200):
            return True
        current_url = _safe_page_url(page).lower()
        return "/chat/" in current_url or "uid=" in current_url

    def _confirm_chat_opened_for_contact(
        self,
        page: Any,
        contact_naming_value: str,
        matched_contact_text: str = "",
    ) -> bool:
        message_input_visible = bool(self._first_visible_selector(page, selectors.MESSAGE_INPUT_SELECTORS, timeout_ms=400))
        if "uid=" in _safe_page_url(page).lower():
            return True
        header_text = self._right_chat_header_text(page)
        name = str(contact_naming_value or "").strip().lower()
        if name and name in header_text.lower():
            return True
        return bool(message_input_visible and matched_contact_text and name and name in matched_contact_text.lower())

    def _right_chat_header_text(self, page: Any) -> str:
        try:
            text = page.evaluate(
                """() => {
                    const visible = (el) => {
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style && style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
                    };
                    const nodes = Array.from(document.querySelectorAll("body *")).filter(visible).filter((el) => {
                        const rect = el.getBoundingClientRect();
                        return rect.x > 500 && rect.y < 180;
                    });
                    return nodes.map((el) => (el.innerText || el.textContent || "").trim()).filter(Boolean).slice(0, 20).join("\\n").slice(0, 1000);
                }"""
            )
            return str(text or "")
        except Exception:
            return ""

    def _open_chat_from_contacts_fallback(self, page: Any, contact_naming_value: str, normalized_phone: str) -> dict[str, Any]:
        started = time.perf_counter()
        result: dict[str, Any] = {
            "status": "failed",
            "error_code": "target_not_found",
            "contacts_search_value": "",
            "contacts_query_attempts": [],
            "contacts_result_count": 0,
            "contacts_result_text": [],
            "matched_contact_text": "",
            "matched_contact_role": "",
            "matched_contact_box": {},
            "right_chat_header_text": "",
            "contact_profile_visible": False,
            "message_button_visible": False,
            "contacts_search_input_selector": "",
            "contacts_search_input_value": "",
            "contacts_search_input_visible": False,
            "contacts_list_scoped": True,
            "chat_url_has_uid": False,
            "message_input_visible": False,
            "contacts_search_ready_attempts": [],
            "contacts_search_icon_visible": False,
            "contacts_search_icon_clicked": "",
            "contacts_page_url_before": "",
            "contacts_page_url_after": "",
            "contacts_header_text": "",
            "visible_inputs": [],
            "visible_top_svgs": [],
            "contacts_panel_box": {},
            "page_url": _safe_page_url(page),
        }

        contacts_url = f"{self.web_url}/contacts"
        try:
            self._goto_with_timeout(page, contacts_url, timeout_ms=3000)
        except Exception as exc:
            result["contacts_open_error"] = str(exc)

        contacts_ready = self._first_visible_selector(page, selectors.CONTACTS_UI_READY_SELECTORS + selectors.CONTACTS_SEARCH_INPUT_SELECTORS, timeout_ms=1500)
        if not contacts_ready:
            result["reason"] = "contacts_ui_not_ready"
            result["page_url"] = _safe_page_url(page)
            result["duration_ms"] = int((time.perf_counter() - started) * 1000)
            return result

        search_ready = self._ensure_contacts_search_ready(page)
        result.update(search_ready)
        contacts_input = str(search_ready.get("contacts_search_input_selector") or "")
        result["contacts_search_input_selector"] = contacts_input or ""
        result["contacts_search_input_visible"] = bool(contacts_input)
        if not contacts_input:
            result.update(
                {
                    "error_code": "contacts_search_input_not_found",
                    "reason": "contacts_search_input_not_found",
                    "page_url": _safe_page_url(page),
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                }
            )
            return result
        name_query = str(contact_naming_value or "").strip()
        phone_queries = sorted(_bale_phone_match_values(normalized_phone), key=len, reverse=True)
        queries = [name_query, *phone_queries]
        for query in [item for item in queries if item]:
            result["contacts_search_value"] = query
            if contacts_input:
                self._clear_contacts_search_input(page, contacts_input)
                self._fill_contacts_search_input(page, contacts_input, query)
                result["contacts_search_input_value"] = self._search_input_value(page, contacts_input)
            candidates = self._collect_contacts_result_candidates(page, timeout_ms=1500)
            result["contacts_result_count"] = len(candidates)
            result["contacts_result_text"] = [item.get("text", "") for item in candidates[:20]]
            match = self._match_search_result_candidate(candidates, query, contact_naming_value, normalized_phone)
            attempt = {
                "query": query,
                "input_value": result["contacts_search_input_value"],
                "result_count": len(candidates),
                "result_text": result["contacts_result_text"],
                "matched_text": str(match.get("text") or "") if match else "",
            }
            result["contacts_query_attempts"].append(attempt)
            if not match:
                continue
            result["matched_contact_text"] = str(match.get("text") or "")
            result["matched_contact_role"] = str(match.get("role") or "")
            result["matched_contact_box"] = match.get("box") or {}
            click_result = self._click_search_result_candidate(page, match)
            if click_result.get("status") != "success":
                result["contact_click_error"] = click_result
                result["page_url"] = _safe_page_url(page)
                result["chat_open_confirmed"] = False
                result["duration_ms"] = int((time.perf_counter() - started) * 1000)
                return result

            chat_opened = self._confirm_chat_opened_for_contact(page, contact_naming_value, result["matched_contact_text"])
            result["message_input_visible"] = bool(self._first_visible_selector(page, selectors.MESSAGE_INPUT_SELECTORS, timeout_ms=100))
            result["chat_url_has_uid"] = "uid=" in _safe_page_url(page).lower()
            result["right_chat_header_text"] = self._right_chat_header_text(page)
            if chat_opened:
                result.update({
                    "status": "success",
                    "matched_selector": match.get("click_selector") or match.get("selector"),
                    "page_url": _safe_page_url(page),
                    "chat_open_confirmed": True,
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                })
                return result

            profile_visible = bool(self._first_visible_selector(page, selectors.CONTACT_PROFILE_SELECTORS, timeout_ms=800))
            result["contact_profile_visible"] = profile_visible
            message_button = self._first_visible_selector(page, selectors.CONTACT_MESSAGE_BUTTON_SELECTORS, timeout_ms=1000)
            result["message_button_visible"] = bool(message_button)
            if message_button:
                self._click_selector_short(page, message_button, timeout_ms=500)
                chat_opened = self._confirm_chat_opened_for_contact(page, contact_naming_value, result["matched_contact_text"])
                result["message_input_visible"] = bool(self._first_visible_selector(page, selectors.MESSAGE_INPUT_SELECTORS, timeout_ms=100))
                result["chat_url_has_uid"] = "uid=" in _safe_page_url(page).lower()
                result["right_chat_header_text"] = self._right_chat_header_text(page)
                if chat_opened:
                    result.update(
                        {
                            "status": "success",
                            "matched_selector": match.get("click_selector") or match.get("selector"),
                            "message_button_selector": message_button,
                            "page_url": _safe_page_url(page),
                            "chat_open_confirmed": True,
                            "duration_ms": int((time.perf_counter() - started) * 1000),
                        }
                    )
                    return result

        result["page_url"] = _safe_page_url(page)
        result["chat_open_confirmed"] = False
        result["duration_ms"] = int((time.perf_counter() - started) * 1000)
        return result

    def _collect_contacts_result_candidates(self, page: Any, timeout_ms: int = 1500) -> list[dict[str, Any]]:
        deadline = time.monotonic() + (timeout_ms / 1000)
        while time.monotonic() < deadline:
            candidates = self._visible_contacts_result_candidates(page)
            if candidates:
                return candidates[:30]
            time.sleep(0.15)
        return []

    def _ensure_contacts_search_ready(self, page: Any) -> dict[str, Any]:
        started = time.perf_counter()
        attempts: list[dict[str, Any]] = []
        result: dict[str, Any] = {
            "contacts_search_input_selector": "",
            "contacts_search_input_visible": False,
            "contacts_search_ready_attempts": attempts,
            "contacts_search_icon_visible": False,
            "contacts_search_icon_clicked": "",
            "contacts_page_url_before": _safe_page_url(page),
        }

        def capture_diagnostics() -> None:
            result.update(self._contacts_search_dom_diagnostics(page))
            result["contacts_page_url_after"] = _safe_page_url(page)

        def probe(label: str, timeout_ms: int = 300) -> str:
            selector = self._first_visible_selector(page, selectors.CONTACTS_SEARCH_INPUT_SELECTORS, timeout_ms=timeout_ms)
            attempts.append({"step": label, "status": "success" if selector else "failed", "selector": selector or ""})
            if selector and self._is_contacts_search_editable(page, selector):
                result["contacts_search_input_selector"] = selector
                result["contacts_search_input_visible"] = True
                result["duration_ms"] = int((time.perf_counter() - started) * 1000)
                capture_diagnostics()
                return selector
            return ""

        selector = probe("direct_selector", timeout_ms=300)
        if selector:
            return result

        search_icon = self._first_visible_selector(page, selectors.SEARCH_ICON_CLICK_TARGET_SELECTORS + selectors.SEARCH_ICON_SVG_SELECTORS, timeout_ms=250)
        result["contacts_search_icon_visible"] = bool(search_icon)
        if search_icon:
            click_result = self._click_chat_search_icon_target(page, search_icon)
            attempts.append({"step": "click_contacts_search_icon", **click_result})
            if click_result.get("status") == "success":
                result["contacts_search_icon_clicked"] = str(click_result.get("selector") or "")
            selector = probe("after_search_icon", timeout_ms=300)
            if selector:
                return result

        panel_click = self._click_contacts_header_or_panel(page)
        attempts.append({"step": "click_contacts_header_or_panel", **panel_click})
        selector = probe("after_panel_click", timeout_ms=300)
        if selector:
            return result

        if int((time.perf_counter() - started) * 1000) < 2000:
            try:
                self._goto_with_timeout(page, f"{self.web_url}/contacts", timeout_ms=3000)
                attempts.append({"step": "reload_contacts_route", "status": "success"})
            except Exception as exc:
                attempts.append({"step": "reload_contacts_route", "status": "failed", "error": str(exc)})
            selector = probe("after_reload", timeout_ms=300)
            if selector:
                return result

        result["duration_ms"] = int((time.perf_counter() - started) * 1000)
        capture_diagnostics()
        return result

    def _goto_with_timeout(self, page: Any, url: str, timeout_ms: int) -> None:
        try:
            page.goto(url, wait_until="load", timeout=timeout_ms)
        except TypeError:
            page.goto(url, wait_until="load")

    def _is_contacts_search_editable(self, page: Any, selector: str) -> bool:
        try:
            locator = page.locator(selector).first
            locator.click(timeout=300)
            return True
        except Exception:
            return False

    def _click_contacts_header_or_panel(self, page: Any) -> dict[str, Any]:
        selectors_to_try = [
            'text=مخاطبین',
            '[aria-label="Contacts-icon"]',
            'svg[aria-label="Contacts-icon"]',
            'input[type="search"]',
        ]
        for selector in selectors_to_try:
            found = self._first_visible_selector(page, [selector], timeout_ms=150)
            if not found:
                continue
            try:
                page.locator(found).first.click(timeout=300)
                return {"status": "success", "selector": found}
            except Exception as exc:
                return {"status": "failed", "selector": found, "error": str(exc)}
        return {"status": "failed", "reason": "no_header_or_panel_target"}

    def _contacts_search_dom_diagnostics(self, page: Any) -> dict[str, Any]:
        try:
            data = page.evaluate(
                """() => {
                    const visible = (el) => {
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style && style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
                    };
                    const compact = (el) => {
                        const rect = el.getBoundingClientRect();
                        return {
                            tag: el.tagName || "",
                            type: el.getAttribute("type") || "",
                            placeholder: el.getAttribute("placeholder") || "",
                            ariaLabel: el.getAttribute("aria-label") || "",
                            title: el.getAttribute("title") || "",
                            text: (el.innerText || el.textContent || "").trim().slice(0, 80),
                            box: {x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height)}
                        };
                    };
                    const inputs = Array.from(document.querySelectorAll("input, textarea, [contenteditable='true']")).filter(visible).slice(0, 20).map(compact);
                    const svgs = Array.from(document.querySelectorAll("svg")).filter(visible).filter((el) => el.getBoundingClientRect().y < 120).slice(0, 20).map(compact);
                    const header = Array.from(document.querySelectorAll("body *")).filter(visible).filter((el) => el.getBoundingClientRect().y < 120).map((el) => (el.innerText || el.textContent || "").trim()).filter(Boolean).slice(0, 10).join("\\n");
                    const panel = Array.from(document.querySelectorAll("body *")).filter(visible).find((el) => {
                        const text = (el.innerText || el.textContent || "");
                        const rect = el.getBoundingClientRect();
                        return text.includes("مخاطبین") && rect.width > 200 && rect.height > 200;
                    });
                    const box = panel ? panel.getBoundingClientRect() : null;
                    return {
                        visible_inputs: inputs,
                        visible_top_svgs: svgs,
                        contacts_header_text: header.slice(0, 500),
                        contacts_panel_box: box ? {x: Math.round(box.x), y: Math.round(box.y), w: Math.round(box.width), h: Math.round(box.height)} : {}
                    };
                }"""
            )
            return dict(data) if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _clear_contacts_search_input(self, page: Any, selector: str) -> None:
        try:
            locator = page.locator(selector).first
            locator.click(timeout=500)
            self._press_key(page, "Control+A")
            self._press_key(page, "Backspace")
            if not self._search_input_value(page, selector):
                return
            locator.fill("", timeout=500)
        except Exception:
            self._clear_search_input(page, selector)

    def _fill_contacts_search_input(self, page: Any, selector: str, text: str) -> None:
        try:
            page.fill(selector, text, timeout=500)
            return
        except Exception:
            pass
        locator = page.locator(selector).first
        try:
            locator.fill(text, timeout=500)
            return
        except Exception:
            pass
        locator.type(text, timeout=500)

    def _click_selector_short(self, page: Any, selector: str, timeout_ms: int = 500) -> dict[str, Any]:
        try:
            page.locator(selector).first.click(timeout=timeout_ms)
            return {"status": "success", "click_method": "playwright_click", "selector": selector}
        except Exception as click_error:
            try:
                page.locator(selector).first.evaluate("(el) => el.click()")
                return {"status": "success", "click_method": "dom_click", "selector": selector, "playwright_click_error": str(click_error)}
            except Exception as dom_error:
                return {"status": "failed", "selector": selector, "click_error": str(click_error), "dom_click_error": str(dom_error)}

    def _visible_contacts_result_candidates(self, page: Any) -> list[dict[str, Any]]:
        try:
            data = page.evaluate(
                """() => {
                    const visible = (el) => {
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style && style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
                    };
                    const clickableParent = (el) => {
                        let node = el;
                        for (let depth = 0; node && depth <= 5; depth += 1, node = node.parentElement) {
                            if (!(node instanceof HTMLElement)) continue;
                            const style = window.getComputedStyle(node);
                            const role = node.getAttribute("role") || "";
                            if (node.tagName === "BUTTON" || node.tagName === "A" || role === "button" || role === "listitem" || style.cursor === "pointer" || typeof node.onclick === "function") return node;
                        }
                        return el;
                    };
                    const selectorFor = (el) => {
                        if (!el) return "";
                        const tag = el.tagName ? el.tagName.toLowerCase() : "*";
                        if (el.id) return `${tag}#${CSS.escape(el.id)}`;
                        const aria = el.getAttribute("aria-label");
                        if (aria) return `${tag}[aria-label="${aria.replaceAll('"', '\\"')}"]`;
                        const role = el.getAttribute("role");
                        if (role) return `${tag}[role="${role}"]`;
                        const testid = el.getAttribute("data-testid");
                        if (testid) return `${tag}[data-testid="${testid.replaceAll('"', '\\"')}"]`;
                        return "";
                    };
                    const nodes = Array.from(document.querySelectorAll('div[role="list"], [role="listitem"], [role="button"], [data-testid*="contact"], [aria-label*="contact"], [aria-label*="Contact"], a, div'));
                    const rows = [];
                    const seen = new Set();
                    for (const el of nodes) {
                        if (!visible(el)) continue;
                        const text = (el.innerText || el.textContent || "").trim();
                        if (!text || text.length > 500) continue;
                        const rect = el.getBoundingClientRect();
                        if (rect.x > 500 || rect.width > 500 || rect.height > 180) continue;
                        if (/گفتگو\\s+مجله\\s+خدمات\\s+مخاطبین/.test(text)) continue;
                        if (/ساخت گروه|ساخت کانال|افزودن مخاطب|مرتب‌شده/.test(text) && text.length > 120) continue;
                        const click = clickableParent(el);
                        const selector = selectorFor(el);
                        const clickSelector = selectorFor(click) || selector;
                        const key = `${selector}|${text}`;
                        if (seen.has(key)) continue;
                        seen.add(key);
                        rows.push({
                            selector,
                            click_selector: clickSelector,
                            text,
                            role: el.getAttribute("role") || "",
                            box: {x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height)}
                        });
                        if (rows.length >= 30) break;
                    }
                    return rows;
                }"""
            )
            if isinstance(data, list):
                return [self._candidate_with_normalized_text(item) for item in data if isinstance(item, dict)]
        except Exception:
            pass
        candidates: list[dict[str, Any]] = []
        for selector in selectors.CONTACTS_RESULT_CANDIDATE_SELECTORS:
            try:
                locator = page.locator(selector).first
                locator.wait_for(state="visible", timeout=150)
                text = str(locator.inner_text(timeout=150) or "").strip()
            except Exception:
                continue
            if text and self._is_scoped_contact_candidate_text(text):
                candidates.append(
                    self._candidate_with_normalized_text(
                        {"selector": selector, "click_selector": selector, "text": text, "role": "list" if 'role="list"' in selector else "", "box": {}}
                    )
                )
        return candidates[:30]

    def _is_scoped_contact_candidate_text(self, text: str) -> bool:
        value = str(text or "").strip()
        if not value or len(value) > 500:
            return False
        if "گفتگو" in value and "مجله" in value and "مخاطبین" in value:
            return False
        if len(value) > 120 and any(token in value for token in ["ساخت گروه", "ساخت کانال", "افزودن مخاطب", "مرتب‌شده"]):
            return False
        return True

    def _focused_editable_selector(self, page: Any) -> str:
        try:
            focused = page.evaluate(
                """() => {
                    const el = document.activeElement;
                    if (!el) return "";
                    const editable = el.matches('input, textarea, [contenteditable="true"], [role="textbox"]');
                    if (!editable) return "";
                    const tag = el.tagName ? el.tagName.toLowerCase() : "";
                    if (el.id) return `${tag}#${el.id}`;
                    const label = el.getAttribute("aria-label");
                    if (label) return `${tag}[aria-label="${label}"]`;
                    const placeholder = el.getAttribute("placeholder");
                    if (placeholder) return `${tag}[placeholder="${placeholder}"]`;
                    if (el.getAttribute("role")) return `[role="${el.getAttribute("role")}"]`;
                    if (el.getAttribute("contenteditable") === "true") return '[contenteditable="true"]';
                    return "";
                }"""
            )
            return str(focused or "")
        except Exception:
            return ""

    def _clear_search_input(self, page: Any, selector: str) -> None:
        try:
            page.locator(selector).first.fill("", timeout=500)
            return
        except Exception:
            pass
        try:
            self._click_if_possible(page, selector)
            self._press_key(page, "Control+A")
            self._press_key(page, "Backspace")
        except Exception:
            pass

    def _search_input_value(self, page: Any, selector: str) -> str:
        try:
            return str(page.locator(selector).first.input_value(timeout=200) or "")
        except Exception:
            try:
                return str(page.locator(selector).first.get_attribute("value", timeout=200) or "")
            except Exception:
                return ""

    def _locator_attribute(self, page: Any, selector: str, name: str) -> str:
        try:
            return str(page.locator(selector).first.get_attribute(name, timeout=200) or "")
        except Exception:
            return ""

    def _visible_no_result_text(self, page: Any) -> str:
        for selector in ['text=/.*not found.*/i', 'text=/.*no result.*/i', 'text=/.*یافت نشد.*/', 'text=/.*نتیجه.*/']:
            try:
                locator = page.locator(selector).first
                locator.wait_for(state="visible", timeout=100)
                return str(locator.inner_text(timeout=100) or "")
            except Exception:
                continue
        return ""

    def _post_save_ui_diagnostics(
        self,
        page: Any,
        contact_naming_value: str,
        normalized_phone: str,
        searched_value: str,
    ) -> dict[str, Any]:
        search_input_selector = self._first_visible_selector(page, selectors.TEXT_SEARCH_INPUT_SELECTORS, timeout_ms=100)
        return {
            "page_url": _safe_page_url(page),
            "visible_modal_text": self._visible_modal_or_dialog_text(page),
            "search_input_visible": bool(search_input_selector),
            "search_input_selector": search_input_selector or "",
            "searched_value": searched_value,
            "contact_naming_value": contact_naming_value,
            "normalized_phone": normalized_phone,
            "contacts_ui_visible": self._contacts_ui_visible(page),
            "main_chat_ui_visible": self._main_chat_ui_visible(page),
        }

    def _visible_modal_or_dialog_text(self, page: Any) -> str:
        for selector in [".ReactModal__Content", ".ReactModal__Overlay", '[role="dialog"]']:
            try:
                locator = page.locator(selector).first
                locator.wait_for(state="visible", timeout=100)
                return str(locator.inner_text(timeout=300) or "")[:1000]
            except Exception:
                continue
        return ""

    def _compact_chat_search_dom_diagnostics(self, page: Any) -> dict[str, Any]:
        try:
            return dict(
                page.evaluate(
                    """() => {
                        const visible = (el) => {
                            const style = window.getComputedStyle(el);
                            const rect = el.getBoundingClientRect();
                            return style && style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
                        };
                        const compact = (items) => items.filter(visible).slice(0, 20).map((el) => ({
                            tag: el.tagName || "",
                            type: el.getAttribute("type") || "",
                            placeholder: el.getAttribute("placeholder") || "",
                            ariaLabel: el.getAttribute("aria-label") || "",
                            title: el.getAttribute("title") || "",
                            role: el.getAttribute("role") || "",
                            text: (el.innerText || el.textContent || "").trim().slice(0, 80)
                        }));
                        return {
                            inputs: compact(Array.from(document.querySelectorAll("input"))),
                            textareas: compact(Array.from(document.querySelectorAll("textarea"))),
                            contenteditables: compact(Array.from(document.querySelectorAll('[contenteditable="true"]'))),
                            buttons: compact(Array.from(document.querySelectorAll("button"))),
                            roleButtons: compact(Array.from(document.querySelectorAll('[role="button"]')))
                        };
                    }"""
                )
            )
        except Exception:
            return {}

    def _is_selector_visible(self, page: Any, selector: str, timeout_ms: int = 250) -> bool:
        if not selector:
            return False
        try:
            page.locator(selector).first.wait_for(state="visible", timeout=timeout_ms)
            return True
        except Exception:
            return False

    def _active_element_info(self, page: Any) -> dict[str, str]:
        try:
            return dict(
                page.evaluate(
                    """() => {
                        const el = document.activeElement;
                        if (!el) return {};
                        return {
                            tag: el.tagName || "",
                            type: el.getAttribute("type") || "",
                            id: el.id || "",
                            role: el.getAttribute("role") || "",
                            ariaLabel: el.getAttribute("aria-label") || "",
                            placeholder: el.getAttribute("placeholder") || "",
                            text: (el.innerText || el.textContent || "").trim().slice(0, 120)
                        };
                    }"""
                )
            )
        except Exception:
            return {}

    def _add_step(self, step_results: list[dict[str, Any]], step: str, status: str, **details: Any) -> None:
        step_results.append({"step": step, "status": status, **details})

    def _click_if_possible(self, page: Any, selector: str) -> None:
        try:
            page.click(selector, timeout=self.default_timeout_ms)
        except Exception:
            try:
                page.locator(selector).first.click(timeout=self.default_timeout_ms)
            except Exception:
                pass

    def _fill_or_type(self, page: Any, selector: str, text: str) -> None:
        try:
            page.fill(selector, text, timeout=self.default_timeout_ms)
            return
        except Exception:
            pass
        locator = page.locator(selector).first
        try:
            locator.fill(text, timeout=self.default_timeout_ms)
            return
        except Exception:
            pass
        locator.type(text, timeout=self.default_timeout_ms)

    def _press_key(self, page: Any, key: str) -> None:
        keyboard = getattr(page, "keyboard", None)
        if keyboard is not None and hasattr(keyboard, "press"):
            keyboard.press(key)
            return
        try:
            page.press("body", key, timeout=self.default_timeout_ms)
        except Exception as exc:
            raise BalePluginError("send_button_not_found", f"Unable to press {key}") from exc

    def _selector_text_matches(self, page: Any, selector: str, query: str) -> bool:
        try:
            text = page.locator(selector).first.inner_text(timeout=1000)
            return query in str(text)
        except Exception:
            return False

    def _first_visible_selector(
        self,
        page: Any,
        selector_list: list[str],
        timeout_ms: int | None = None,
    ) -> str | None:
        timeout = timeout_ms if timeout_ms is not None else self.default_timeout_ms
        for selector in selector_list:
            try:
                locator = page.locator(selector).first
                locator.wait_for(state="visible", timeout=timeout)
                return selector
            except Exception:
                continue
        return None

    def _require_selector(
        self,
        page: Any,
        selector_list: list[str],
        error_code: str,
        message: str,
    ) -> str:
        selector = self._first_visible_selector(page, selector_list)
        if selector is None:
            raise BalePluginError(error_code, message)
        return selector

    def _failed_result(self, account_id: str, action: str, error_code: str, error: str) -> dict[str, Any]:
        self._log_step(account_id, action, "failed", error, error, error_code)
        return {
            "ok": False,
            "logged_in": error_code != "not_logged_in",
            "platform": self.platform_id,
            "account_id": account_id,
            "message": error,
            "error_code": error_code,
            "error": error,
        }

    def _record_step(
        self,
        execution_logs: list[str],
        account_id: str,
        step: str,
        status: Literal["started", "success", "failed"],
        message: str,
        error: str | None = None,
        error_code: str | None = None,
    ) -> None:
        execution_logs.append(f"{step} | {status} | {message}")
        self._log_step(account_id, step, status, message, error, error_code)

    def _log_step(
        self,
        account_id: str,
        step: str,
        status: str,
        message: str,
        error: str | None = None,
        error_code: str | None = None,
    ) -> None:
        self._logs.append(
            {
                "id": str(uuid4()),
                "account_id": account_id,
                "platform": self.platform_id,
                "action": step,
                "step": step,
                "operation_type": step,
                "event_key": f"bale.{step}",
                "status": status,
                "message": message,
                "error": error,
                "error_code": error_code,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        self._logs = self._logs[-500:]

    def _log(
        self,
        account_id: str,
        action: str,
        status: str,
        message: str,
        error: str | None = None,
    ) -> None:
        self._logs.append(
            {
                "id": str(uuid4()),
                "account_id": account_id,
                "platform": self.platform_id,
                "action": action,
                "operation_type": action,
                "event_key": f"bale.{action}",
                "status": status,
                "message": message,
                "error": error,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        self._logs = self._logs[-500:]

    def _log_open_account_attempt(
        self,
        account: dict[str, Any],
        status: str,
        message: str,
        error_code: str | None = None,
        error: str | None = None,
    ) -> None:
        self._logs.append(
            {
                "id": str(uuid4()),
                "account_id": account.get("account_id"),
                "platform": self.platform_id,
                "platform_id": self.platform_id,
                "browser_provider": account.get("browser_provider"),
                "profile_id": account.get("adspower_profile_id") or account.get("profile_id"),
                "profile_group_id": account.get("profile_group_id"),
                "action": "open_account",
                "step": "open_account",
                "operation_type": "open_account",
                "event_key": "bale.open_account",
                "status": status,
                "message": message,
                "error": error,
                "error_code": error_code,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        self._logs = self._logs[-500:]


class BalePluginError(RuntimeError):
    def __init__(self, error_code: str, message: str, diagnostics: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.diagnostics = diagnostics or {}


def _native_profile_dir(account_id: str) -> Path:
    backend_dir = Path(__file__).resolve().parents[4]
    return backend_dir / "runtime" / "browser_profiles" / account_id


_CHAT_UI_SELECTORS = [
    *selectors.LOGIN_STATE_INDICATOR_SELECTORS,
    *selectors.SEARCH_INPUT_SELECTORS,
    *selectors.MESSAGE_INPUT_SELECTORS,
    *selectors.SEARCH_ICON_SELECTORS,
    *selectors.MESSAGE_TOOLBAR_SIGNAL_SELECTORS,
    "[data-testid*='sidebar']",
    "[class*='sidebar']",
    "[data-testid*='conversation']",
    "[class*='conversation']",
    "[class*='chat-list']",
    "[class*='ChatList']",
    "main",
]

_LOGIN_FORM_SELECTORS = [
    "input[type='tel']",
    "input[name*='phone']",
    "input[autocomplete='tel']",
    "[data-testid*='login']",
    "[class*='login']",
    "text=ورود",
    "text=شماره موبایل",
]

_INSTALL_PROMPT_SELECTORS = [
    "text=متوجه شدم",
    "text=نصب",
    "text=install",
    "text=Install",
    "[data-testid*='install']",
    "[class*='install']",
]


def _safe_page_url(page: Any) -> str:
    try:
        value = getattr(page, "url", "")
        if value:
            return str(value)
    except Exception:
        pass
    try:
        urls = getattr(page, "urls", [])
        if urls:
            return str(urls[-1])
    except Exception:
        pass
    return ""


def _safe_page_title(page: Any) -> str:
    try:
        title = getattr(page, "title", None)
        if callable(title):
            return str(title())
        return str(title or "")
    except Exception:
        return ""


def _save_login_debug_screenshot(page: Any, account_id: str) -> str:
    try:
        screenshot = getattr(page, "screenshot", None)
        if not callable(screenshot):
            return ""
        debug_dir = Path(__file__).resolve().parents[4] / "runtime" / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        screenshot_path = debug_dir / f"bale_login_{account_id}_{timestamp}.png"
        screenshot(path=str(screenshot_path), full_page=True)
        return str(screenshot_path)
    except Exception:
        return ""


def _split_contact_name(value: str) -> tuple[str, str]:
    parts = str(value or "").strip().split(maxsplit=1)
    if not parts:
        return "Bale Contact", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def _last_successful_step(step_results: list[dict[str, Any]]) -> str:
    for step in reversed(step_results):
        if not isinstance(step, dict) or step.get("status") != "success":
            continue
        return str(step.get("step") or "")
    return ""


def _pointer_interceptor_from_error(message: str) -> str:
    marker = "intercepts pointer events"
    if marker not in message:
        return ""
    lines = [line.strip() for line in message.splitlines()]
    for line in lines:
        if marker in line:
            return line.replace(marker, "").strip()
    return ""


def _normalize_bale_match_text(value: str) -> str:
    return _english_digits(str(value or "")).lower().strip()


def _normalize_bale_phone_text(value: str) -> str:
    return "".join(ch for ch in _english_digits(str(value or "")) if ch.isdigit())


def _bale_phone_match_values(value: str) -> set[str]:
    digits = _normalize_bale_phone_text(value)
    values = {digits} if digits else set()
    if digits.startswith("98") and len(digits) >= 12:
        local = digits[2:]
        values.add(local)
        values.add("0" + local)
    elif digits.startswith("0") and len(digits) >= 11:
        local = digits[1:]
        values.add(local)
        values.add("98" + local)
    elif len(digits) == 10:
        values.add("0" + digits)
        values.add("98" + digits)
    if len(digits) >= 10:
        values.add(digits[-10:])
    return {item for item in values if item}


def _english_digits(value: str) -> str:
    translation = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    return str(value or "").translate(translation)


def phone_for_bale_contact_field(normalized_phone: str) -> str:
    value = str(normalized_phone or "").strip()
    if value.startswith("98"):
        return value[2:]
    if value.startswith("+98"):
        return value[3:]
    if value.startswith("0"):
        return value[1:]
    return value


_bale_contact_phone = phone_for_bale_contact_field


def _browser_error_code(exc: Exception) -> str:
    explicit = getattr(exc, "error_code", "")
    if explicit:
        return str(explicit)
    message = str(exc).lower()
    if "cannot switch to a different thread" in message or "greenlet" in message:
        return "browser_thread_error"
    if "timeout" in message and "browser" in message:
        return "browser_start_timeout"
    return "unknown_error"


bale_plugin = BalePlugin()
