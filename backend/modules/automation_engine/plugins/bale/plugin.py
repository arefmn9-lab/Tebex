from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from modules.automation_engine.browser import actions_browser
from modules.automation_engine.browser.browser_manager import BrowserManager
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

    def validate_session(self, account_id: str) -> dict[str, Any]:
        try:
            page = self._get_page(account_id)
            page.goto(self.web_url, wait_until="load")
            indicator = self._first_visible_selector(
                page,
                selectors.LOGIN_STATE_INDICATOR_SELECTORS,
                timeout_ms=5000,
            )
            logged_in = indicator is not None
            message = "Bale session appears logged in" if logged_in else "Manual Bale login is required"
            self._log_step(account_id, "validate_session", "success", message)
            return {
                "ok": logged_in,
                "logged_in": logged_in,
                "platform": self.platform_id,
                "account_id": account_id,
                "message": message,
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
                "error_code": "unknown_error",
                "error": error,
            }

    def send_test_message(self, account_id: str, target: str, message: str, provider_mode: str | None = None) -> dict[str, Any]:
        if not target.strip():
            return self._failed_result(account_id, "send_test_message", "target_not_found", "Target is required")
        if not message.strip():
            return self._failed_result(account_id, "send_test_message", "message_input_not_found", "Message is required")

        try:
            execution_logs = self._send_test_message_steps(account_id, target, message, provider_mode)
            result = {
                "ok": True,
                "logged_in": True,
                "platform": self.platform_id,
                "account_id": account_id,
                "target": target,
                "message": "One Bale test message action completed.",
                "logs": execution_logs,
            }
            self._log(account_id, "send_test_message", "success", result["message"])
            return result
        except Exception as exc:
            error_code = getattr(exc, "error_code", "unknown_error")
            error = str(exc)
            browser_path = getattr(self.browser_manager, "last_browser_path", None)
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
                "logged_in": error_code != "not_logged_in",
                "platform": self.platform_id,
                "account_id": account_id,
                "target": target,
                "message": "Bale test message failed",
                "error_code": error_code,
                "error": error,
                "browser_path": browser_path,
            }

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

    def _send_test_message_steps(self, account_id: str, target: str, message: str, provider_mode: str | None = None) -> list[str]:
        execution_logs: list[str] = []
        page = self._get_page(account_id, provider_mode)

        self._record_step(execution_logs, account_id, "open_bale_web", "started", "Opening Bale Web")
        page.goto(self.web_url, wait_until="load")
        self.browser_manager.save_session(account_id)
        self._record_step(execution_logs, account_id, "open_bale_web", "success", "Bale Web opened")

        self._record_step(execution_logs, account_id, "check_login", "started", "Checking Bale login state")
        login_indicator = self._first_visible_selector(
            page,
            selectors.LOGIN_STATE_INDICATOR_SELECTORS,
            timeout_ms=5000,
        )
        if login_indicator is None:
            self._record_step(
                execution_logs,
                account_id,
                "check_login",
                "failed",
                "Manual Bale login is required before sending a test message",
                error_code="not_logged_in",
            )
            raise BalePluginError("not_logged_in", "Manual Bale login is required before sending a test message")
        self._record_step(execution_logs, account_id, "check_login", "success", f"Logged-in UI detected: {login_indicator}")

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
        return execution_logs

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
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


bale_plugin = BalePlugin()
