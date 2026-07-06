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

                search_result = self._open_target_chat(page, contact_naming_value, normalized_phone)
                step_results.append(search_result)
                if search_result["status"] != "success":
                    diagnostics = {**self._page_debug_info(page, account_id), "search_attempts": search_result.get("search_attempts", [])}
                    return finish(
                        False,
                        "target_not_found",
                        "Target chat was not found after searching Bale.",
                        "open_target_chat",
                        diagnostics,
                    )

                message_input = self._first_visible_selector(page, selectors.MESSAGE_INPUT_SELECTORS, timeout_ms=5000)
                if not message_input:
                    diagnostics = self._page_debug_info(page, account_id)
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

                diagnostics = self._page_debug_info(page, account_id)
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

    def _open_target_chat(self, page: Any, contact_naming_value: str, normalized_phone: str) -> dict[str, Any]:
        search_attempts: list[dict[str, Any]] = []
        self._close_contact_modal_if_open(page)
        try:
            page.goto(self.web_url, wait_until="load")
        except Exception:
            pass
        chat_ready = self._first_visible_selector(page, selectors.SEARCH_ICON_SELECTORS + selectors.CHAT_ITEM_SELECTORS, timeout_ms=1500)
        search_icon = self._first_visible_selector(page, selectors.SEARCH_ICON_SELECTORS, timeout_ms=1500)
        if search_icon:
            self._click_if_possible(page, search_icon)
        search_input = self._first_visible_selector(page, selectors.TEXT_SEARCH_INPUT_SELECTORS, timeout_ms=2500)
        if not search_input:
            return {
                "step": "open_target_chat",
                "status": "failed",
                "error_code": "target_not_found",
                "current_url": _safe_page_url(page),
                "overlay_present": self._is_contact_modal_visible(page),
                "active_element": self._active_element_info(page),
                "chat_ready_selector": chat_ready or "",
                "search_attempts": [{"query": contact_naming_value or normalized_phone, "reason": "search_input_not_found"}],
            }

        for query in [contact_naming_value, normalized_phone]:
            query = str(query or "").strip()
            if not query:
                continue
            attempt = {"query": query, "matched": False}
            search_attempts.append(attempt)
            self._fill_or_type(page, search_input, query)
            chat_item = self._first_visible_selector(page, selectors.CHAT_ITEM_SELECTORS, timeout_ms=2500)
            if chat_item and self._selector_text_matches(page, chat_item, query):
                self._click_if_possible(page, chat_item)
                attempt["matched"] = True
                attempt["matched_selector"] = chat_item
                return {
                    "step": "open_target_chat",
                    "status": "success",
                    "matched_selector": chat_item,
                    "search_attempts": search_attempts,
                }
            if chat_item:
                self._click_if_possible(page, chat_item)
                attempt["matched"] = True
                attempt["matched_selector"] = chat_item
                attempt["match_mode"] = "first_visible_dialog_item"
                return {
                    "step": "open_target_chat",
                    "status": "success",
                    "matched_selector": chat_item,
                    "search_attempts": search_attempts,
                }
            attempt["reason"] = "dialog_item_not_found"
        return {
            "step": "open_target_chat",
            "status": "failed",
            "error_code": "target_not_found",
            "search_attempts": search_attempts,
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
                            id: el.id || "",
                            role: el.getAttribute("role") || "",
                            ariaLabel: el.getAttribute("aria-label") || "",
                            placeholder: el.getAttribute("placeholder") || ""
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
