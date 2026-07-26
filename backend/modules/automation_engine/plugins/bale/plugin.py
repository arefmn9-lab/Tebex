from __future__ import annotations

import json
import re
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

from .account_store import bale_account_store, normalize_source_channel_uid
from .contact_store import BaleContactError, bale_contact_store
from . import selectors


class BalePlugin:
    platform_id = "bale"
    web_url = "https://web.bale.ai"
    default_timeout_ms = 15000

    def __init__(self, browser_manager: BrowserManager | None = None) -> None:
        self.browser_manager = browser_manager or actions_browser.browser_manager
        self.contact_store = bale_contact_store
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
            login_check = self._detect_login_state(page, timeout_ms=3000)
            error_code = None
            message = "Bale login detected" if login_check["logged_in"] else "Manual Bale login is required"
            if login_check["install_prompt_detected"]:
                error_code = "bale_install_prompt"
                message = "Bale install/help prompt is visible. Dismiss it, then log in."
            elif not login_check["logged_in"]:
                error_code = str(login_check.get("error_code") or "not_logged_in")
                if error_code == "login_state_unknown":
                    message = "Bale login state could not be determined"
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

    def preview_latest_channel_message(
        self,
        account_id: str,
        source_channel_url: str,
        provider_mode: str | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        last_successful_step = "validate_input"
        effective_provider = provider_mode or "native_chrome"
        browser_meta = self._browser_failure_meta(account_id, effective_provider)
        if not str(source_channel_url or "").strip():
            return {
                "success": False,
                "ok": False,
                "account_id": account_id,
                "action": "preview_latest_channel_message",
                "source_channel_url": source_channel_url,
                "error_code": "source_channel_not_configured",
                "error_message": "Bale source channel URL is not configured",
                "failed_step": "load_source_channel",
                "last_successful_step": None,
                "diagnostics": {},
                **browser_meta,
                "duration_ms": int((time.perf_counter() - started) * 1000),
            }

        page = None
        try:
            with self._page_session(account_id, provider_mode=provider_mode or "native_chrome") as (page, session_meta):
                browser_meta = session_meta
                self._goto_with_timeout(page, str(source_channel_url).strip(), timeout_ms=15000, wait_until="load")
                last_successful_step = "open_source_channel"
                _safe_wait_for_timeout(page, 1500)
                preview = self._extract_latest_channel_message_preview(page)
                diagnostics = {
                    "page_url": _safe_page_url(page),
                    "page_title": _safe_page_title(page),
                    "channel_view_visible": bool(preview.get("channel_view_visible")),
                    "message_selector_used": preview.get("message_selector_used") or "",
                    "latest_message_visible": bool(preview.get("latest_message_visible")),
                    "candidate_count": int(preview.get("candidate_count") or 0),
                    "visible_text_sample": _visible_text_sample(page),
                }
                if not preview.get("message_found"):
                    screenshot_path = _save_login_debug_screenshot(page, account_id)
                    return {
                        "success": False,
                        "ok": False,
                        "account_id": account_id,
                        "action": "preview_latest_channel_message",
                        "source_channel_url": source_channel_url,
                        "message_found": False,
                        "error_code": "latest_channel_message_not_found",
                        "error_message": "Latest channel message could not be identified",
                        "failed_step": "locate_latest_channel_message",
                        "last_successful_step": last_successful_step,
                        "current_url": diagnostics["page_url"],
                        "page_url": diagnostics["page_url"],
                        "page_title": diagnostics["page_title"],
                        "screenshot_path": screenshot_path,
                        "diagnostics": diagnostics,
                        **browser_meta,
                        "duration_ms": int((time.perf_counter() - started) * 1000),
                    }

                return {
                    "success": True,
                    "ok": True,
                    "account_id": account_id,
                    "action": "preview_latest_channel_message",
                    "source_channel_url": source_channel_url,
                    "message_found": True,
                    "text_preview": str(preview.get("text_preview") or ""),
                    "has_text": bool(preview.get("has_text")),
                    "has_image": bool(preview.get("has_image")),
                    "has_video": bool(preview.get("has_video")),
                    "has_file": bool(preview.get("has_file")),
                    "message_dom_id": preview.get("message_dom_id"),
                    "message_timestamp_text": preview.get("message_timestamp_text"),
                    "candidate_count": int(preview.get("candidate_count") or 0),
                    "diagnostics": diagnostics,
                    **browser_meta,
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                }
        except Exception as exc:
            diagnostics = self._page_debug_info(page, account_id) if page is not None else {}
            return {
                "success": False,
                "ok": False,
                "account_id": account_id,
                "action": "preview_latest_channel_message",
                "source_channel_url": source_channel_url,
                "message_found": False,
                "error_code": _browser_error_code(exc),
                "error_message": str(exc),
                "failed_step": "preview_latest_channel_message",
                "last_successful_step": last_successful_step,
                "current_url": diagnostics.get("current_url") or "",
                "page_url": diagnostics.get("page_url") or "",
                "page_title": diagnostics.get("page_title") or "",
                "screenshot_path": diagnostics.get("screenshot_path"),
                "diagnostics": diagnostics,
                **browser_meta,
                "duration_ms": int((time.perf_counter() - started) * 1000),
            }

    def validate_session(self, account_id: str) -> dict[str, Any]:
        try:
            page = self._get_page(account_id)
            page.goto(self.web_url, wait_until="load")
            login_check = self._detect_login_state(page, timeout_ms=3000)
            logged_in = bool(login_check["logged_in"])
            message = "Bale session appears logged in" if logged_in else "Manual Bale login is required"
            self._log_step(account_id, "validate_session", "success", message)
            return {
                "ok": logged_in,
                "logged_in": logged_in,
                "platform": self.platform_id,
                "account_id": account_id,
                "message": message,
                "error_code": "bale_install_prompt" if login_check["install_prompt_detected"] else (None if logged_in else str(login_check.get("error_code") or "not_logged_in")),
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
                "logged_in": error_code not in {"not_logged_in", "bale_install_prompt", "login_state_unknown"},
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
            final_extra = dict(extra or {})
            page_url = str(final_extra.get("page_url") or final_extra.get("current_url") or current_url_value)
            current_url = str(final_extra.get("current_url") or page_url)
            result = {
                "ok": ok,
                "success": ok,
                "action": "send_text_message",
                "logged_in": error_code not in {"not_logged_in", "bale_install_prompt"},
                "platform": self.platform_id,
                "account_id": account_id,
                "normalized_phone": normalized_phone,
                "target": normalized_phone,
                "contact_naming_value": contact_naming_value,
                "contact_save_status": contact_save_status,
                "current_url": current_url,
                "page_url": page_url,
                "page_title": str(final_extra.get("page_title") or ""),
                "message": user_message or ("Bale text message sent" if ok else "Bale text message failed"),
                "user_message": user_message,
                "error_message": "" if ok else user_message,
                "error_code": error_code,
                "failed_step": failed_step,
                "last_successful_step": _last_successful_step(step_results) if not ok else None,
                "step_results": step_results,
                "started_at": started_at,
                "finished_at": finished_at,
                "duration_ms": int((time.perf_counter() - started_monotonic) * 1000),
                **browser_meta,
                **final_extra,
            }
            result["current_url"] = str(result.get("current_url") or current_url)
            result["page_url"] = str(result.get("page_url") or result.get("current_url") or page_url)
            result["page_title"] = str(result.get("page_title") or "")
            result["error_message"] = str(result.get("error_message") or ("" if ok else result.get("message") or ""))
            result["browser_path"] = result.get("browser_path") or getattr(self.browser_manager, "last_browser_path", None)
            if ok:
                result.setdefault("send_triggered", True)
                result.setdefault("confirm_sent_status", "confirmed" if not result.get("warning_code") else "assumed")
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
                login_check = self._detect_login_state(page, timeout_ms=3000)
                if login_check["install_prompt_detected"]:
                    diagnostics = {"login_check": login_check, **self._page_debug_info(page, account_id)}
                    self._add_step(step_results, "verify_login", "failed", error_code="bale_install_prompt", **diagnostics)
                    return finish(
                        False,
                        "bale_install_prompt",
                        "ØµÙØ­Ù‡ Ø±Ø§Ù‡Ù†Ù…Ø§ÛŒ Ù†ØµØ¨ Ø¨Ù„Ù‡ Ù†Ù…Ø§ÛŒØ´ Ø¯Ø§Ø¯Ù‡ Ø´Ø¯Ù‡ Ø§Ø³Øª. Ø±ÙˆÛŒ Â«Ù…ØªÙˆØ¬Ù‡ Ø´Ø¯Ù…Â» Ø¨Ø²Ù†ÛŒØ¯ Ùˆ ÙˆØ§Ø±Ø¯ Ø¨Ù„Ù‡ Ø´ÙˆÛŒØ¯.",
                        "verify_login",
                        diagnostics,
                    )
                if not login_check["logged_in"]:
                    diagnostics = {"login_check": login_check, **self._page_debug_info(page, account_id)}
                    error_code = str(login_check.get("error_code") or "not_logged_in")
                    self._add_step(step_results, "verify_login", "failed", error_code=error_code, **diagnostics)
                    return finish(
                        False,
                        error_code,
                        "Bale login state could not be determined" if error_code == "login_state_unknown" else "Manual Bale login is required before sending a text message",
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

                message_input = self._first_visible_selector(page, selectors.MESSAGE_INPUT_SELECTORS, timeout_ms=1500)
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
                try:
                    self._press_key(page, "Enter")
                except Exception as exc:
                    diagnostics = {
                        **self._page_debug_info(page, account_id),
                        **self._send_button_diagnostics(page, message_input),
                    }
                    self._add_step(step_results, "click_send", "failed", error_code=_browser_error_code(exc), error=str(exc), **diagnostics)
                    return finish(False, _browser_error_code(exc), str(exc), "click_send", diagnostics)
                self._add_step(step_results, "click_send", "success", action="press_enter")

                sent_selector = self._first_visible_selector(page, selectors.MESSAGE_SENT_INDICATOR_SELECTORS, timeout_ms=1500)
                if sent_selector:
                    self.browser_manager.save_session(account_id)
                    self._add_step(step_results, "confirm_sent", "success", matched_selector=sent_selector)
                    return finish(
                        True,
                        None,
                        "Bale text message sent",
                        None,
                        {
                            "current_url": _safe_page_url(page),
                            "page_url": _safe_page_url(page),
                            "page_title": _safe_page_title(page),
                            "send_triggered": True,
                            "confirm_sent_status": "confirmed",
                        },
                    )

                diagnostics = {
                    **self._page_debug_info(page, account_id),
                    **self._post_save_ui_diagnostics(page, contact_naming_value, normalized_phone, searched_value=contact_naming_value or normalized_phone),
                }
                warning = {
                    "warning_code": "send_confirmation_not_implemented",
                    "warning_message": "Message send was triggered, but delivery confirmation is not implemented yet.",
                    "send_triggered": True,
                    "confirm_sent_status": "assumed",
                    **diagnostics,
                }
                self._add_step(step_results, "confirm_sent", "assumed_success", reason="send_confirmation_not_implemented", **warning)
                return finish(
                    True,
                    None,
                    "Bale text message sent",
                    None,
                    {"current_url": _safe_page_url(page), **warning},
                )
        except Exception as exc:
            error_code = _browser_error_code(exc)
            diagnostics = getattr(exc, "diagnostics", {}) or {}
            self._add_step(step_results, "unexpected_error", "failed", error_code=error_code, error=str(exc))
            return finish(False, error_code, str(exc), "unexpected_error", diagnostics)

    def save_bale_contact(
        self,
        account_id: str,
        phone: str,
        provider_mode: str | None = None,
        runtime_session: Any | None = None,
        close_session_when_done: bool = True,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        action = "save_bale_contact"
        step_results: list[dict[str, Any]] = []
        last_successful_step: str | None = None
        browser_meta = self._browser_failure_meta(account_id, provider_mode or "native_chrome")

        def finish(
            success: bool,
            error_code: str | None = None,
            error_message: str | None = None,
            failed_step: str | None = None,
            extra: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            payload = {
                "success": success,
                "ok": success,
                "action": action,
                "account_id": account_id,
                "phone": phone,
                "failed_step": failed_step,
                "last_successful_step": last_successful_step,
                "error_code": error_code,
                "error_message": error_message,
                "step_results": step_results,
                "duration_ms": int((time.perf_counter() - started) * 1000),
                **browser_meta,
            }
            if extra:
                payload.update(extra)
            return payload

        try:
            contact, created = self.contact_store.get_or_create_bale_contact(account_id, phone)
            normalized_phone = str(contact.get("phone_normalized") or "")
            display_name = str(contact.get("display_name") or "")
            last_successful_step = "resolve_contact_name"
            self._add_step(
                step_results,
                "resolve_contact_name",
                "success",
                phone_normalized=normalized_phone,
                display_name=display_name,
                contact_store_status="created" if created else "existing",
                contact_id=contact.get("id"),
                sequence_number=contact.get("sequence_number"),
            )
        except BaleContactError as exc:
            self._add_step(step_results, "normalize_phone", "failed", error_code=exc.error_code, phone=phone)
            return finish(False, exc.error_code, str(exc), "normalize_phone")

        page = None
        try:
            with self._runtime_or_page_session(account_id, provider_mode=provider_mode or "native_chrome", runtime_session=runtime_session) as (page, session_meta):
                browser_meta = session_meta
                contact_result = self.save_contact_by_phone(
                    page,
                    normalized_phone=normalized_phone,
                    contact_naming_value=display_name,
                    account_id=account_id,
                )
                contact_steps = contact_result.get("contact_steps") if isinstance(contact_result.get("contact_steps"), list) else []
                step_results.extend(contact_steps)
                contact_status = str(contact_result.get("contact_save_status") or "failed")
                success = contact_status in {"saved", "already_exists"}
                if success:
                    last_successful_step = "verify_result"
                    self._add_step(
                        step_results,
                        "verify_result",
                        "success",
                        contact_save_status=contact_status,
                        current_url=_safe_page_url(page),
                    )
                    self.browser_manager.save_session(account_id)
                    return finish(
                        True,
                        extra={
                            "phone_normalized": normalized_phone,
                            "display_name": display_name,
                            "contact_id": contact.get("id"),
                            "sequence_number": contact.get("sequence_number"),
                            "contact_store_status": "created" if created else "existing",
                            "contact_save_status": contact_status,
                            "current_url": _safe_page_url(page),
                            "page_url": _safe_page_url(page),
                            "page_title": _safe_page_title(page),
                        },
                    )

                failed_step = str(contact_result.get("failed_step") or "save_contact_in_bale")
                error_code = str(contact_result.get("error_code") or "contact_save_failed")
                error_message = str(contact_result.get("user_message") or contact_result.get("message") or "Bale contact save failed")
                diagnostics = self._page_debug_info(page, account_id)
                return finish(
                    False,
                    error_code,
                    error_message,
                    failed_step,
                    {
                        "phone_normalized": normalized_phone,
                        "display_name": display_name,
                        "contact_id": contact.get("id"),
                        "sequence_number": contact.get("sequence_number"),
                        "contact_store_status": "created" if created else "existing",
                        "contact_save_status": contact_status,
                        "contact_save_result": contact_result,
                        **diagnostics,
                    },
                )
        except Exception as exc:
            diagnostics = self._page_debug_info(page, account_id) if page is not None else {}
            return finish(
                False,
                _browser_error_code(exc),
                str(exc),
                "save_bale_contact",
                diagnostics,
            )

    def open_bale_source_channel(
        self,
        account_id: str,
        source_channel_uid: str,
        provider_mode: str | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        action = "open_bale_source_channel"
        source_channel_uid = str(source_channel_uid or "").strip()
        requested_channel_url = ""
        readiness_attempts: list[dict[str, Any]] = []
        last_successful_step: str | None = None
        browser_meta = self._browser_failure_meta(account_id, provider_mode or "native_chrome")

        def finish(
            success: bool,
            error_code: str | None = None,
            error_message: str | None = None,
            failed_step: str | None = None,
            extra: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            payload = {
                "success": success,
                "ok": success,
                "action": action,
                "account_id": account_id,
                "source_channel_uid": source_channel_uid,
                "requested_channel_url": requested_channel_url,
                "final_page_url": "",
                "target_channel_panel_visible": False,
                "target_channel_panel_selector": "",
                "target_channel_header_text": "",
                "target_channel_header_selector": "",
                "message_stream_visible": False,
                "message_stream_selector": "",
                "center_panel_visible_text_sample": "",
                "full_page_visible_text_sample": "",
                "readiness_attempts": readiness_attempts,
                "readiness_duration_ms": int((time.perf_counter() - started) * 1000),
                "failed_step": failed_step,
                "last_successful_step": last_successful_step,
                "error_code": error_code,
                "error_message": error_message,
                "step_results": readiness_attempts,
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "screenshot_path": "",
                **browser_meta,
            }
            if extra:
                payload.update(extra)
            return payload

        if not _valid_bale_channel_uid(source_channel_uid):
            readiness_attempts.append(
                {
                    "step": "validate_source_channel_uid",
                    "status": "failed",
                    "error_code": "invalid_source_channel_uid",
                    "source_channel_uid": source_channel_uid,
                }
            )
            return finish(
                False,
                "invalid_source_channel_uid",
                "source_channel_uid must contain only letters, numbers, underscore, or dash",
                "validate_source_channel_uid",
            )

        requested_channel_url = f"{self.web_url}/chat?uid={source_channel_uid}"
        readiness_attempts.append(
            {
                "step": "validate_source_channel_uid",
                "status": "success",
                "source_channel_uid": source_channel_uid,
                "requested_channel_url": requested_channel_url,
            }
        )
        last_successful_step = "validate_source_channel_uid"

        page = None
        try:
            with self._page_session(account_id, provider_mode=provider_mode or "native_chrome") as (page, session_meta):
                browser_meta = session_meta
                self._goto_with_timeout(page, requested_channel_url, timeout_ms=15000, wait_until="load")
                last_successful_step = "navigate_source_channel"
                readiness_attempts.append(
                    {
                        "step": "navigate_source_channel",
                        "status": "success",
                        "requested_channel_url": requested_channel_url,
                        "final_page_url": _safe_page_url(page),
                    }
                )
                readiness_started = time.perf_counter()
                latest_readiness: dict[str, Any] = {}
                for attempt_index in range(1, 11):
                    latest_readiness = self._source_channel_readiness(page)
                    attempt = {
                        "step": "wait_source_channel_ready",
                        "status": "success" if latest_readiness.get("ready") else "pending",
                        "attempt": attempt_index,
                        "final_page_url": _safe_page_url(page),
                        "target_channel_panel_visible": bool(latest_readiness.get("target_channel_panel_visible")),
                        "target_channel_panel_selector": str(latest_readiness.get("target_channel_panel_selector") or ""),
                        "target_channel_header_text": str(latest_readiness.get("target_channel_header_text") or ""),
                        "target_channel_header_selector": str(latest_readiness.get("target_channel_header_selector") or ""),
                        "message_stream_visible": bool(latest_readiness.get("message_stream_visible")),
                        "message_stream_selector": str(latest_readiness.get("message_stream_selector") or ""),
                    }
                    readiness_attempts.append(attempt)
                    if latest_readiness.get("ready"):
                        last_successful_step = "wait_source_channel_ready"
                        self.browser_manager.save_session(account_id)
                        return finish(
                            True,
                            extra={
                                **latest_readiness,
                                "final_page_url": _safe_page_url(page),
                                "full_page_visible_text_sample": _visible_text_sample(page),
                                "readiness_duration_ms": int((time.perf_counter() - readiness_started) * 1000),
                            },
                        )
                    _safe_wait_for_timeout(page, 500)

                screenshot_path = _save_login_debug_screenshot(page, account_id)
                return finish(
                    False,
                    "source_channel_not_ready",
                    "Bale source channel center panel and message stream were not detected",
                    "wait_source_channel_ready",
                    {
                        **latest_readiness,
                        "final_page_url": _safe_page_url(page),
                        "full_page_visible_text_sample": _visible_text_sample(page),
                        "readiness_duration_ms": int((time.perf_counter() - readiness_started) * 1000),
                        "screenshot_path": screenshot_path,
                    },
                )
        except Exception as exc:
            diagnostics = self._page_debug_info(page, account_id) if page is not None else {}
            return finish(
                False,
                _browser_error_code(exc),
                str(exc),
                "open_bale_source_channel",
                {
                    "final_page_url": diagnostics.get("page_url") or diagnostics.get("current_url") or "",
                    "full_page_visible_text_sample": diagnostics.get("visible_text_sample") or "",
                    "screenshot_path": diagnostics.get("screenshot_path") or "",
                },
            )

    def locate_latest_channel_message(
        self,
        account_id: str,
        source_channel_uid: str,
        provider_mode: str | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        action = "locate_latest_channel_message"
        source_channel_uid = str(source_channel_uid or "").strip()
        requested_channel_url = ""
        step_results: list[dict[str, Any]] = []
        last_successful_step: str | None = None
        browser_meta = self._browser_failure_meta(account_id, provider_mode or "native_chrome")

        def finish(
            success: bool,
            error_code: str | None = None,
            error_message: str | None = None,
            failed_step: str | None = None,
            extra: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            payload = {
                "success": success,
                "ok": success,
                "action": action,
                "account_id": account_id,
                "source_channel_uid": source_channel_uid,
                "requested_channel_url": requested_channel_url,
                "final_page_url": "",
                "message_found": False,
                "candidate_count": 0,
                "message_selector_used": "",
                "candidate_debug": [],
                "text_preview": "",
                "has_text": False,
                "has_image": False,
                "has_video": False,
                "has_file": False,
                "message_dom_id": None,
                "message_timestamp_text": None,
                "failed_step": failed_step,
                "last_successful_step": last_successful_step,
                "error_code": error_code,
                "error_message": error_message,
                "screenshot_path": "",
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "step_results": step_results,
                **browser_meta,
            }
            if extra:
                payload.update(extra)
            return payload

        if not _valid_bale_channel_uid(source_channel_uid):
            self._add_step(step_results, "validate_source_channel_uid", "failed", error_code="invalid_source_channel_uid", source_channel_uid=source_channel_uid)
            return finish(
                False,
                "invalid_source_channel_uid",
                "source_channel_uid must contain only letters, numbers, underscore, or dash",
                "validate_source_channel_uid",
            )

        requested_channel_url = f"{self.web_url}/chat?uid={source_channel_uid}"
        self._add_step(step_results, "validate_source_channel_uid", "success", source_channel_uid=source_channel_uid, requested_channel_url=requested_channel_url)
        last_successful_step = "validate_source_channel_uid"
        page = None
        try:
            with self._page_session(account_id, provider_mode=provider_mode or "native_chrome") as (page, session_meta):
                browser_meta = session_meta
                self._goto_with_timeout(page, requested_channel_url, timeout_ms=15000, wait_until="load")
                stale_picker_state = self._forward_picker_state(page)
                if stale_picker_state.get("forward_picker_visible"):
                    self._press_key(page, "Escape")
                    _safe_wait_for_timeout(page, 300)
                last_successful_step = "navigate_source_channel"
                self._add_step(step_results, "navigate_source_channel", "success", requested_channel_url=requested_channel_url, final_page_url=_safe_page_url(page))

                readiness: dict[str, Any] = {}
                for attempt_index in range(1, 11):
                    readiness = self._source_channel_readiness(page)
                    ready = bool(readiness.get("ready"))
                    self._add_step(
                        step_results,
                        "wait_source_channel_ready",
                        "success" if ready else "pending",
                        attempt=attempt_index,
                        final_page_url=_safe_page_url(page),
                        target_channel_panel_visible=bool(readiness.get("target_channel_panel_visible")),
                        target_channel_header_selector=str(readiness.get("target_channel_header_selector") or ""),
                        message_stream_visible=bool(readiness.get("message_stream_visible")),
                        message_stream_selector=str(readiness.get("message_stream_selector") or ""),
                    )
                    if ready:
                        last_successful_step = "wait_source_channel_ready"
                        break
                    _safe_wait_for_timeout(page, 500)
                if not readiness.get("ready"):
                    screenshot_path = _save_login_debug_screenshot(page, account_id)
                    return finish(
                        False,
                        "source_channel_not_ready",
                        "Bale source channel message stream was not detected",
                        "wait_source_channel_ready",
                        {
                            "final_page_url": _safe_page_url(page),
                            "screenshot_path": screenshot_path,
                            **readiness,
                        },
                    )

                latest = self._locate_latest_channel_message_in_stream(page)
                self._add_step(
                    step_results,
                    "locate_latest_channel_message",
                    "success" if latest.get("message_found") else "failed",
                    candidate_count=int(latest.get("candidate_count") or 0),
                    message_selector_used=str(latest.get("message_selector_used") or ""),
                )
                if not latest.get("message_found"):
                    screenshot_path = _save_login_debug_screenshot(page, account_id)
                    return finish(
                        False,
                        "latest_channel_message_not_found",
                        "No real message container was found inside the Bale source channel message stream",
                        "locate_latest_channel_message",
                        {
                            **latest,
                            "final_page_url": _safe_page_url(page),
                            "screenshot_path": screenshot_path,
                        },
                    )

                last_successful_step = "locate_latest_channel_message"
                self.browser_manager.save_session(account_id)
                return finish(
                    True,
                    extra={
                        **latest,
                        "final_page_url": _safe_page_url(page),
                    },
                )
        except Exception as exc:
            diagnostics = self._page_debug_info(page, account_id) if page is not None else {}
            return finish(
                False,
                _browser_error_code(exc),
                str(exc),
                "locate_latest_channel_message",
                {
                    "final_page_url": diagnostics.get("page_url") or diagnostics.get("current_url") or "",
                    "screenshot_path": diagnostics.get("screenshot_path") or "",
                },
            )

    def open_message_forward(
        self,
        account_id: str,
        source_channel_uid: str,
        provider_mode: str | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        action = "open_message_forward"
        source_channel_uid = str(source_channel_uid or "").strip()
        requested_channel_url = ""
        step_results: list[dict[str, Any]] = []
        click_attempts: list[dict[str, Any]] = []
        candidate_debug: list[dict[str, Any]] = []
        recipient_candidate_debug: list[dict[str, Any]] = []
        last_successful_step: str | None = None
        browser_meta = self._browser_failure_meta(account_id, provider_mode or "native_chrome")

        def finish(
            success: bool,
            error_code: str | None = None,
            error_message: str | None = None,
            failed_step: str | None = None,
            extra: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            payload = {
                "success": success,
                "ok": success,
                "action": action,
                "account_id": account_id,
                "source_channel_uid": source_channel_uid,
                "requested_channel_url": requested_channel_url,
                "final_page_url": "",
                "latest_message_selector": "",
                "latest_message_text_preview": "",
                "latest_message_html_summary": "",
                "message_menu_opened": False,
                "message_menu_selector": "",
                "forward_option_found": False,
                "forward_option_selector": "",
                "forward_picker_visible": False,
                "forward_picker_selector": "",
                "candidate_debug": candidate_debug,
                "click_attempts": click_attempts,
                "visible_menu_text": "",
                "failed_step": failed_step,
                "last_successful_step": last_successful_step,
                "error_code": error_code,
                "error_message": error_message,
                "screenshot_path": "",
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "step_results": step_results,
                **browser_meta,
            }
            if extra:
                payload.update(extra)
            return payload

        if not _valid_bale_channel_uid(source_channel_uid):
            self._add_step(step_results, "validate_source_channel_uid", "failed", error_code="invalid_source_channel_uid", source_channel_uid=source_channel_uid)
            return finish(
                False,
                "invalid_source_channel_uid",
                "source_channel_uid must contain only letters, numbers, underscore, or dash",
                "validate_source_channel_uid",
            )

        requested_channel_url = f"{self.web_url}/chat?uid={source_channel_uid}"
        self._add_step(step_results, "validate_source_channel_uid", "success", source_channel_uid=source_channel_uid, requested_channel_url=requested_channel_url)
        last_successful_step = "validate_source_channel_uid"
        page = None

        try:
            with self._page_session(account_id, provider_mode=provider_mode or "native_chrome") as (page, session_meta):
                browser_meta = session_meta
                self._goto_with_timeout(page, requested_channel_url, timeout_ms=15000, wait_until="load")
                last_successful_step = "navigate_source_channel"
                self._add_step(step_results, "navigate_source_channel", "success", requested_channel_url=requested_channel_url, final_page_url=_safe_page_url(page))

                readiness: dict[str, Any] = {}
                for attempt_index in range(1, 11):
                    readiness = self._source_channel_readiness(page)
                    ready = bool(readiness.get("ready"))
                    self._add_step(
                        step_results,
                        "wait_source_channel_ready",
                        "success" if ready else "pending",
                        attempt=attempt_index,
                        final_page_url=_safe_page_url(page),
                        target_channel_panel_visible=bool(readiness.get("target_channel_panel_visible")),
                        message_stream_visible=bool(readiness.get("message_stream_visible")),
                        message_stream_selector=str(readiness.get("message_stream_selector") or ""),
                    )
                    if ready:
                        last_successful_step = "wait_source_channel_ready"
                        break
                    _safe_wait_for_timeout(page, 500)

                if not readiness.get("ready"):
                    screenshot_path = _save_login_debug_screenshot(page, account_id)
                    return finish(
                        False,
                        "source_channel_not_ready",
                        "Bale source channel message stream was not detected",
                        "wait_source_channel_ready",
                        {
                            "final_page_url": _safe_page_url(page),
                            "screenshot_path": screenshot_path,
                            **readiness,
                        },
                    )

                latest = self._resolve_latest_forward_message_target(page)
                candidate_debug = latest.get("candidate_debug") if isinstance(latest.get("candidate_debug"), list) else []
                self._add_step(
                    step_results,
                    "locate_latest_channel_message",
                    "success" if latest.get("message_found") else "failed",
                    candidate_count=int(latest.get("candidate_count") or 0),
                    latest_message_selector=str(latest.get("latest_message_selector") or ""),
                )
                if not latest.get("message_found"):
                    screenshot_path = _save_login_debug_screenshot(page, account_id)
                    return finish(
                        False,
                        "latest_message_not_found",
                        "No latest real message was found inside the Bale source channel stream",
                        "locate_latest_channel_message",
                        {
                            **latest,
                            "final_page_url": _safe_page_url(page),
                            "screenshot_path": screenshot_path,
                        },
                    )

                last_successful_step = "locate_latest_channel_message"
                latest_selector = str(latest.get("latest_message_selector") or "")
                latest_text = str(latest.get("latest_message_text_preview") or latest.get("text_preview") or "")
                locator = page.locator(latest_selector).first
                try:
                    locator.scroll_into_view_if_needed(timeout=1000)
                except Exception:
                    pass
                try:
                    locator.hover(timeout=1000)
                except Exception as hover_error:
                    click_attempts.append({"step": "hover_latest_message", "status": "failed", "selector": latest_selector, "error": str(hover_error)})
                else:
                    click_attempts.append({"step": "hover_latest_message", "status": "success", "selector": latest_selector})

                menu_state = self._message_forward_menu_candidates(page, latest_selector)
                candidate_debug.extend(menu_state.get("candidate_debug") if isinstance(menu_state.get("candidate_debug"), list) else [])
                menu_selector = str(menu_state.get("message_menu_selector") or "")
                self._add_step(
                    step_results,
                    "discover_message_menu",
                    "success" if menu_selector else "failed",
                    message_menu_selector=menu_selector,
                    attempted_selectors=menu_state.get("attempted_selectors") or [],
                )
                if not menu_selector:
                    screenshot_path = _save_login_debug_screenshot(page, account_id)
                    return finish(
                        False,
                        "message_menu_not_found",
                        "No message context menu control was found inside the latest message",
                        "discover_message_menu",
                        {
                            **latest,
                            "final_page_url": _safe_page_url(page),
                            "screenshot_path": screenshot_path,
                            "selector_attempts": menu_state.get("attempted_selectors") or [],
                        },
                    )

                menu_click = self._click_selector_short(page, menu_selector, timeout_ms=1000)
                menu_click["step"] = "open_message_menu"
                click_attempts.append(menu_click)
                menu_opened = menu_click.get("status") == "success"
                self._add_step(step_results, "open_message_menu", "success" if menu_opened else "failed", click_result=menu_click)
                if not menu_opened:
                    screenshot_path = _save_login_debug_screenshot(page, account_id)
                    return finish(
                        False,
                        "message_menu_open_failed",
                        "Latest message context menu control could not be clicked",
                        "open_message_menu",
                        {
                            **latest,
                            "message_menu_selector": menu_selector,
                            "final_page_url": _safe_page_url(page),
                            "screenshot_path": screenshot_path,
                        },
                    )

                last_successful_step = "open_message_menu"
                _safe_wait_for_timeout(page, 300)
                forward_state = self._forward_option_candidates(page)
                candidate_debug.extend(forward_state.get("candidate_debug") if isinstance(forward_state.get("candidate_debug"), list) else [])
                forward_selector = str(forward_state.get("forward_option_selector") or "")
                self._add_step(
                    step_results,
                    "discover_forward_option",
                    "success" if forward_selector else "failed",
                    forward_option_selector=forward_selector,
                    visible_menu_text=str(forward_state.get("visible_menu_text") or ""),
                )
                if not forward_selector:
                    screenshot_path = _save_login_debug_screenshot(page, account_id)
                    return finish(
                        False,
                        "forward_option_not_found",
                        "Forward option was not found in the opened message menu",
                        "discover_forward_option",
                        {
                            **latest,
                            "message_menu_opened": True,
                            "message_menu_selector": menu_selector,
                            "visible_menu_text": str(forward_state.get("visible_menu_text") or ""),
                            "final_page_url": _safe_page_url(page),
                            "screenshot_path": screenshot_path,
                        },
                    )

                forward_click = self._click_selector_short(page, forward_selector, timeout_ms=1000)
                forward_click["step"] = "click_forward_option"
                click_attempts.append(forward_click)
                forward_clicked = forward_click.get("status") == "success"
                self._add_step(step_results, "click_forward_option", "success" if forward_clicked else "failed", click_result=forward_click)
                if not forward_clicked:
                    screenshot_path = _save_login_debug_screenshot(page, account_id)
                    return finish(
                        False,
                        "forward_option_not_found",
                        "Forward option could not be clicked",
                        "click_forward_option",
                        {
                            **latest,
                            "message_menu_opened": True,
                            "message_menu_selector": menu_selector,
                            "forward_option_found": True,
                            "forward_option_selector": forward_selector,
                            "visible_menu_text": str(forward_state.get("visible_menu_text") or ""),
                            "final_page_url": _safe_page_url(page),
                            "screenshot_path": screenshot_path,
                        },
                    )

                last_successful_step = "click_forward_option"
                _safe_wait_for_timeout(page, 500)
                picker_state = self._forward_picker_state(page)
                picker_visible = bool(picker_state.get("forward_picker_visible"))
                picker_selector = str(picker_state.get("forward_picker_selector") or "")
                self._add_step(step_results, "verify_forward_picker", "success" if picker_visible else "failed", forward_picker_selector=picker_selector)
                if not picker_visible:
                    screenshot_path = _save_login_debug_screenshot(page, account_id)
                    return finish(
                        False,
                        "forward_picker_not_visible",
                        "Forward recipient picker was not visible after clicking Forward",
                        "verify_forward_picker",
                        {
                            **latest,
                            "message_menu_opened": True,
                            "message_menu_selector": menu_selector,
                            "forward_option_found": True,
                            "forward_option_selector": forward_selector,
                            "forward_picker_selector": picker_selector,
                            "visible_menu_text": str(forward_state.get("visible_menu_text") or ""),
                            "final_page_url": _safe_page_url(page),
                            "screenshot_path": screenshot_path,
                        },
                    )

                last_successful_step = "verify_forward_picker"
                self.browser_manager.save_session(account_id)
                return finish(
                    True,
                    extra={
                        **latest,
                        "latest_message_text_preview": latest_text,
                        "message_menu_opened": True,
                        "message_menu_selector": menu_selector,
                        "forward_option_found": True,
                        "forward_option_selector": forward_selector,
                        "forward_picker_visible": True,
                        "forward_picker_selector": picker_selector,
                        "visible_menu_text": str(forward_state.get("visible_menu_text") or ""),
                        "final_page_url": _safe_page_url(page),
                    },
                )
        except Exception as exc:
            diagnostics = self._page_debug_info(page, account_id) if page is not None else {}
            return finish(
                False,
                _browser_error_code(exc),
                str(exc),
                "open_message_forward",
                {
                    "final_page_url": diagnostics.get("page_url") or diagnostics.get("current_url") or "",
                    "screenshot_path": diagnostics.get("screenshot_path") or "",
                },
            )

    def forward_message_to_contact(
        self,
        account_id: str,
        source_channel_uid: str,
        display_name: str,
        dry_run: bool = False,
        selection_only: bool = False,
        provider_mode: str | None = None,
        runtime_session: Any | None = None,
        close_session_when_done: bool = True,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        action = "forward_message_to_contact"
        source_channel_uid = str(source_channel_uid or "").strip()
        display_name = str(display_name or "").strip()
        requested_channel_url = ""
        step_results: list[dict[str, Any]] = []
        click_attempts: list[dict[str, Any]] = []
        candidate_debug: list[dict[str, Any]] = []
        recipient_candidate_debug: list[dict[str, Any]] = []
        destructive_clicks_attempted = 0
        confirm_click_count = 0
        click_classifications: list[dict[str, Any]] = []
        clicks_before_search = 0
        preselected_count_initial = 0
        preselected_names_initial: list[Any] = []
        reset_attempted = False
        escape_pressed = False
        picker_closed_after_escape = False
        page_reloaded = False
        channel_verified_after_reload = False
        picker_reopened = False
        selected_count_after_reset = 0
        selected_names_after_reset: list[Any] = []
        reset_cycle_count = 0
        last_successful_step: str | None = None
        browser_meta = self._browser_failure_meta(account_id, provider_mode or "native_chrome")

        def finish(
            success: bool,
            error_code: str | None = None,
            error_message: str | None = None,
            failed_step: str | None = None,
            extra: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            payload = {
                "success": success,
                "ok": success,
                "action": action,
                "account_id": account_id,
                "source_channel_uid": source_channel_uid,
                "display_name": display_name,
                "dry_run": bool(dry_run),
                "selection_only": bool(selection_only),
                "destructive_clicks_attempted": destructive_clicks_attempted,
                "clicks_before_search": clicks_before_search,
                "recipient_click_count": 0,
                "recipient_picker_visible": False,
                "recipient_search_selector": "",
                "searched_value": display_name,
                "exact_recipient_found": False,
                "recipient_result_selector": "",
                "target_row_selector": "",
                "target_row_text": "",
                "target_click_selector": "",
                "target_click_bounding_box": None,
                "target_click_point": None,
                "element_from_point_tag": "",
                "element_from_point_text": "",
                "element_from_point_row_name": "",
                "overlapping_recipient_rows": [],
                "selected_names_immediately_after_click": [],
                "selected_names_after_500ms": [],
                "sahar_selected_immediately": False,
                "sahar_selected_after_500ms": False,
                "dom_mutations_after_click": {},
                "recipient_selected": False,
                "confirm_button_selector": "",
                "confirm_clicked": False,
                "confirm_click_count": confirm_click_count,
                "final_forwarded_recipient_count": 0,
                "forward_verified": False,
                "success_toast_text": "",
                "verified_forwarded_recipient_count": 0,
                "verified_forward_recipient_count": 0,
                "diagnostics_consistent": False,
                "diagnostics_consistency_errors": [],
                "candidate_debug": candidate_debug,
                "recipient_candidate_debug": recipient_candidate_debug,
                "click_classifications": click_classifications,
                "preselected_count": 0,
                "preselected_names": [],
                "preselected_count_initial": preselected_count_initial,
                "preselected_names_initial": preselected_names_initial,
                "reset_attempted": reset_attempted,
                "escape_pressed": escape_pressed,
                "picker_closed_after_escape": picker_closed_after_escape,
                "page_reloaded": page_reloaded,
                "channel_verified_after_reload": channel_verified_after_reload,
                "picker_reopened": picker_reopened,
                "selected_count_after_reset": selected_count_after_reset,
                "selected_names_after_reset": selected_names_after_reset,
                "reset_cycle_count": reset_cycle_count,
                "cleared_preselected_count": 0,
                "search_input_selector": "",
                "search_input_value": "",
                "result_set_stable": False,
                "visible_result_count": 0,
                "visible_result_names": [],
                "exact_match_count": 0,
                "selected_count_before_target": 0,
                "selected_count_after_target": 0,
                "selected_names_after_target": [],
                "selected_count_before_confirm": 0,
                "selected_names_before_confirm": [],
                "requested_source_channel_uid": source_channel_uid,
                "configured_source_channel_uid": source_channel_uid,
                "effective_source_channel_uid": source_channel_uid,
                "source_channel_value_origin": "request",
                "final_channel_url": "",
                "channel_uid_verified": False,
                "selected_message_preview": "",
                "selected_message_data_date": None,
                "selected_message_signature": "",
                "picker_outer_html_excerpt": "",
                "modal_root_selector": "",
                "modal_root_outer_html_excerpt": "",
                "picker_bounding_box": None,
                "picker_search_inputs": [],
                "visible_buttons": [],
                "visible_rows": [],
                "selected_row_candidates": [],
                "selected_chip_candidates": [],
                "selected_names_before_search": [],
                "selected_count_before_search": 0,
                "sahar_selected": False,
                "rejected_selected_candidates": [],
                "rejected_candidate_reasons": [],
                "remove_control_candidates": [],
                "destructive_recipient_clicks": destructive_clicks_attempted,
                "click_attempts": click_attempts,
                "failed_step": failed_step,
                "last_successful_step": last_successful_step,
                "error_code": error_code,
                "error_message": error_message,
                "screenshot_path": "",
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "step_results": step_results,
                **browser_meta,
            }
            if extra:
                payload.update(extra)
            self._normalize_forward_message_to_contact_diagnostics(payload)
            return payload

        if not str(account_id or "").strip():
            self._add_step(step_results, "validate_inputs", "failed", error_code="invalid_account_id")
            return finish(False, "invalid_account_id", "account_id is required", "validate_inputs")
        if not _valid_bale_channel_uid(source_channel_uid):
            self._add_step(step_results, "validate_inputs", "failed", error_code="invalid_source_channel_uid")
            return finish(False, "invalid_source_channel_uid", "source_channel_uid must contain only letters, numbers, underscore, or dash", "validate_inputs")
        if not display_name:
            self._add_step(step_results, "validate_inputs", "failed", error_code="invalid_display_name")
            return finish(False, "invalid_display_name", "display_name is required", "validate_inputs")

        requested_channel_url = f"{self.web_url}/chat?uid={source_channel_uid}"
        self._add_step(step_results, "validate_inputs", "success", requested_channel_url=requested_channel_url)
        last_successful_step = "validate_inputs"
        page = None

        try:
            with self._runtime_or_page_session(account_id, provider_mode=provider_mode or "native_chrome", runtime_session=runtime_session) as (page, session_meta):
                browser_meta = session_meta
                self._goto_with_timeout(page, requested_channel_url, timeout_ms=15000, wait_until="load")
                last_successful_step = "navigate_source_channel"
                self._add_step(step_results, "navigate_source_channel", "success", requested_channel_url=requested_channel_url, final_page_url=_safe_page_url(page))
                if f"uid={source_channel_uid}" not in _safe_page_url(page):
                    return finish(False, "channel_navigation_not_verified", "Opened channel URL did not verify the requested uid", "navigate_source_channel", {"final_page_url": _safe_page_url(page), "final_channel_url": _safe_page_url(page)})

                readiness: dict[str, Any] = {}
                for attempt_index in range(1, 11):
                    readiness = self._source_channel_readiness(page)
                    ready = bool(readiness.get("ready"))
                    self._add_step(step_results, "wait_source_channel_ready", "success" if ready else "pending", attempt=attempt_index, message_stream_selector=str(readiness.get("message_stream_selector") or ""))
                    if ready:
                        last_successful_step = "wait_source_channel_ready"
                        break
                    _safe_wait_for_timeout(page, 500)
                if not readiness.get("ready"):
                    screenshot_path = _save_login_debug_screenshot(page, account_id)
                    return finish(False, "source_channel_not_ready", "Bale source channel message stream was not detected", "wait_source_channel_ready", {"screenshot_path": screenshot_path, "final_page_url": _safe_page_url(page), **readiness})

                latest = self._resolve_latest_forward_message_target(page)
                candidate_debug = latest.get("candidate_debug") if isinstance(latest.get("candidate_debug"), list) else []
                self._add_step(
                    step_results,
                    "locate_latest_channel_message",
                    "success" if latest.get("message_found") else "failed",
                    latest_message_selector=str(latest.get("latest_message_selector") or ""),
                    selected_message_preview=str(latest.get("latest_message_text_preview") or ""),
                    selected_message_data_date=latest.get("latest_message_data_date"),
                    selected_message_signature=str(latest.get("latest_message_signature") or ""),
                )
                if not latest.get("message_found"):
                    screenshot_path = _save_login_debug_screenshot(page, account_id)
                    return finish(False, "latest_message_not_found", "No latest real message was found inside the Bale source channel stream", "locate_latest_channel_message", {"screenshot_path": screenshot_path, "final_page_url": _safe_page_url(page), **latest})

                last_successful_step = "locate_latest_channel_message"
                latest_selector = str(latest.get("latest_message_selector") or "")
                locator = page.locator(latest_selector).first
                try:
                    locator.scroll_into_view_if_needed(timeout=1000)
                    locator.hover(timeout=1000)
                    click_attempts.append({"step": "hover_latest_message", "status": "success", "selector": latest_selector})
                except Exception as hover_error:
                    click_attempts.append({"step": "hover_latest_message", "status": "failed", "selector": latest_selector, "error": str(hover_error)})

                menu_state = self._message_forward_menu_candidates(page, latest_selector)
                candidate_debug.extend(menu_state.get("candidate_debug") if isinstance(menu_state.get("candidate_debug"), list) else [])
                menu_selector = str(menu_state.get("message_menu_selector") or "")
                self._add_step(step_results, "discover_message_menu", "success" if menu_selector else "failed", message_menu_selector=menu_selector)
                if not menu_selector:
                    screenshot_path = _save_login_debug_screenshot(page, account_id)
                    return finish(False, "message_menu_not_found", "No message context menu control was found inside the latest message", "discover_message_menu", {"screenshot_path": screenshot_path, "final_page_url": _safe_page_url(page)})
                menu_click = self._click_selector_short(page, menu_selector, timeout_ms=1000)
                menu_click["step"] = "open_message_menu"
                click_attempts.append(menu_click)
                if menu_click.get("status") != "success":
                    screenshot_path = _save_login_debug_screenshot(page, account_id)
                    return finish(False, "message_menu_open_failed", "Latest message context menu control could not be clicked", "open_message_menu", {"screenshot_path": screenshot_path, "final_page_url": _safe_page_url(page)})

                last_successful_step = "open_message_menu"
                _safe_wait_for_timeout(page, 300)
                direct_picker_state = self._forward_picker_state(page)
                if direct_picker_state.get("forward_picker_visible"):
                    forward_selector = menu_selector
                    self._add_step(step_results, "discover_forward_option", "success", forward_option_selector=forward_selector, direct_forward_picker=True)
                else:
                    forward_state = self._forward_option_candidates(page)
                    candidate_debug.extend(forward_state.get("candidate_debug") if isinstance(forward_state.get("candidate_debug"), list) else [])
                    forward_selector = str(forward_state.get("forward_option_selector") or "")
                    self._add_step(step_results, "discover_forward_option", "success" if forward_selector else "failed", forward_option_selector=forward_selector)
                    if not forward_selector:
                        screenshot_path = _save_login_debug_screenshot(page, account_id)
                        return finish(False, "forward_option_not_found", "Forward option was not found in the opened message menu", "discover_forward_option", {"screenshot_path": screenshot_path, "final_page_url": _safe_page_url(page), "visible_menu_text": str(forward_state.get("visible_menu_text") or "")})
                    forward_click = self._click_selector_short(page, forward_selector, timeout_ms=1000)
                    forward_click["step"] = "click_forward_option"
                    click_attempts.append(forward_click)
                    if forward_click.get("status") != "success":
                        screenshot_path = _save_login_debug_screenshot(page, account_id)
                        return finish(False, "forward_option_not_found", "Forward option could not be clicked", "click_forward_option", {"screenshot_path": screenshot_path, "final_page_url": _safe_page_url(page)})

                last_successful_step = "click_forward_option"
                _safe_wait_for_timeout(page, 500)
                picker_state = self._forward_picker_state(page)
                picker_visible = bool(picker_state.get("forward_picker_visible"))
                self._add_step(step_results, "verify_forward_picker", "success" if picker_visible else "failed", forward_picker_selector=str(picker_state.get("forward_picker_selector") or ""))
                if not picker_visible:
                    screenshot_path = _save_login_debug_screenshot(page, account_id)
                    return finish(False, "recipient_picker_not_visible", "Forward recipient picker was not visible after clicking Forward", "verify_forward_picker", {"screenshot_path": screenshot_path, "final_page_url": _safe_page_url(page), **self._forward_failure_debug(page)})

                last_successful_step = "verify_forward_picker"
                picker_forensics = self._forward_picker_forensics(page, display_name)
                if dry_run:
                    if destructive_clicks_attempted:
                        screenshot_path = _save_login_debug_screenshot(page, account_id)
                        return finish(False, "dry_run_safety_violation", "Dry-run attempted a destructive click", "dry_run_forensic_inspection", {"screenshot_path": screenshot_path, **picker_forensics})
                    self._add_step(step_results, "dry_run_forensic_inspection", "success", destructive_clicks_attempted=0)
                    return finish(True, extra={
                        "recipient_picker_visible": True,
                        "forward_verified": False,
                        "confirm_clicked": False,
                        "final_page_url": _safe_page_url(page),
                        "final_channel_url": _safe_page_url(page),
                        "channel_uid_verified": True,
                        **picker_forensics,
                    })
                preselected_state = self._forward_selected_recipients_state(page)
                preselected_count = int(preselected_state.get("selected_count") or 0)
                preselected_names = preselected_state.get("selected_names") if isinstance(preselected_state.get("selected_names"), list) else []
                preselected_count_initial = preselected_count
                preselected_names_initial = list(preselected_names)
                self._add_step(step_results, "inspect_preselected_recipients", "success" if preselected_count == 0 else "pending", selected_count=preselected_count, selected_names=preselected_names)
                if preselected_count:
                    if reset_cycle_count >= 1:
                        screenshot_path = _save_login_debug_screenshot(page, account_id)
                        return finish(False, "reset_cycle_limit_reached", "Forward recipient reset cycle limit was reached", "inspect_preselected_recipients", {"recipient_picker_visible": True, "preselected_count": preselected_count, "preselected_names": preselected_names, "screenshot_path": screenshot_path, "final_page_url": _safe_page_url(page), **picker_forensics})
                    reset_attempted = True
                    reset_cycle_count += 1
                    self._press_key(page, "Escape")
                    escape_pressed = True
                    _safe_wait_for_timeout(page, 300)
                    closed_state = self._forward_picker_state(page)
                    picker_closed_after_escape = not bool(closed_state.get("forward_picker_visible"))
                    self._add_step(step_results, "close_preselected_forward_picker", "success" if picker_closed_after_escape else "failed", preselected_count=preselected_count, preselected_names=preselected_names, escape_pressed=True, picker_closed_after_escape=picker_closed_after_escape)
                    if not picker_closed_after_escape:
                        screenshot_path = _save_login_debug_screenshot(page, account_id)
                        return finish(False, "forward_picker_close_failed", "Forward picker did not close after Escape", "close_preselected_forward_picker", {"recipient_picker_visible": True, "preselected_count": preselected_count, "preselected_names": preselected_names, "screenshot_path": screenshot_path, "final_page_url": _safe_page_url(page), **picker_forensics})

                    try:
                        self._goto_with_timeout(page, requested_channel_url, timeout_ms=15000, wait_until="load")
                        page_reloaded = True
                    except Exception as reload_error:
                        screenshot_path = _save_login_debug_screenshot(page, account_id)
                        return finish(False, "channel_reload_failed", str(reload_error), "reload_source_channel_after_forward_reset", {"screenshot_path": screenshot_path, "final_page_url": _safe_page_url(page)})
                    channel_verified_after_reload = f"uid={source_channel_uid}" in _safe_page_url(page)
                    self._add_step(step_results, "reload_source_channel_after_forward_reset", "success" if channel_verified_after_reload else "failed", requested_channel_url=requested_channel_url, final_page_url=_safe_page_url(page), channel_verified_after_reload=channel_verified_after_reload)
                    if not channel_verified_after_reload:
                        screenshot_path = _save_login_debug_screenshot(page, account_id)
                        return finish(False, "channel_reload_failed", "Reloaded channel URL did not verify the requested uid", "reload_source_channel_after_forward_reset", {"screenshot_path": screenshot_path, "final_page_url": _safe_page_url(page)})

                    reset_readiness: dict[str, Any] = {}
                    for attempt_index in range(1, 11):
                        reset_readiness = self._source_channel_readiness(page)
                        ready = bool(reset_readiness.get("ready"))
                        self._add_step(step_results, "wait_source_channel_ready_after_forward_reset", "success" if ready else "pending", attempt=attempt_index, message_stream_selector=str(reset_readiness.get("message_stream_selector") or ""))
                        if ready:
                            break
                        _safe_wait_for_timeout(page, 500)
                    if not reset_readiness.get("ready"):
                        screenshot_path = _save_login_debug_screenshot(page, account_id)
                        return finish(False, "channel_reload_failed", "Bale source channel message stream was not detected after reset reload", "wait_source_channel_ready_after_forward_reset", {"screenshot_path": screenshot_path, "final_page_url": _safe_page_url(page), **reset_readiness})

                    latest_after_reset = self._resolve_latest_forward_message_target(page)
                    reset_latest_selector = str(latest_after_reset.get("latest_message_selector") or "")
                    self._add_step(
                        step_results,
                        "locate_latest_channel_message_after_forward_reset",
                        "success" if latest_after_reset.get("message_found") else "failed",
                        latest_message_selector=reset_latest_selector,
                        selected_message_preview=str(latest_after_reset.get("latest_message_text_preview") or ""),
                        selected_message_data_date=latest_after_reset.get("latest_message_data_date"),
                        selected_message_signature=str(latest_after_reset.get("latest_message_signature") or ""),
                    )
                    if not latest_after_reset.get("message_found"):
                        screenshot_path = _save_login_debug_screenshot(page, account_id)
                        return finish(False, "forward_picker_reopen_failed", "No latest real message was found after reset reload", "locate_latest_channel_message_after_forward_reset", {"screenshot_path": screenshot_path, "final_page_url": _safe_page_url(page), **latest_after_reset})
                    latest_selector = reset_latest_selector
                    locator = page.locator(latest_selector).first
                    try:
                        locator.scroll_into_view_if_needed(timeout=1000)
                        locator.hover(timeout=1000)
                        click_attempts.append({"step": "hover_latest_message_after_forward_reset", "status": "success", "selector": latest_selector})
                    except Exception as hover_error:
                        click_attempts.append({"step": "hover_latest_message_after_forward_reset", "status": "failed", "selector": latest_selector, "error": str(hover_error)})

                    menu_state = self._message_forward_menu_candidates(page, latest_selector)
                    candidate_debug.extend(menu_state.get("candidate_debug") if isinstance(menu_state.get("candidate_debug"), list) else [])
                    menu_selector = str(menu_state.get("message_menu_selector") or "")
                    self._add_step(step_results, "discover_message_menu_after_forward_reset", "success" if menu_selector else "failed", message_menu_selector=menu_selector)
                    if not menu_selector:
                        screenshot_path = _save_login_debug_screenshot(page, account_id)
                        return finish(False, "forward_picker_reopen_failed", "No message context menu control was found after reset reload", "discover_message_menu_after_forward_reset", {"screenshot_path": screenshot_path, "final_page_url": _safe_page_url(page)})
                    menu_click = self._click_selector_short(page, menu_selector, timeout_ms=1000)
                    menu_click["step"] = "open_message_menu_after_forward_reset"
                    click_attempts.append(menu_click)
                    if menu_click.get("status") != "success":
                        screenshot_path = _save_login_debug_screenshot(page, account_id)
                        return finish(False, "forward_picker_reopen_failed", "Latest message context menu control could not be clicked after reset reload", "open_message_menu_after_forward_reset", {"screenshot_path": screenshot_path, "final_page_url": _safe_page_url(page)})

                    _safe_wait_for_timeout(page, 300)
                    direct_picker_state = self._forward_picker_state(page)
                    if direct_picker_state.get("forward_picker_visible"):
                        forward_selector = menu_selector
                        self._add_step(step_results, "discover_forward_option_after_forward_reset", "success", forward_option_selector=forward_selector, direct_forward_picker=True)
                    else:
                        forward_state = self._forward_option_candidates(page)
                        candidate_debug.extend(forward_state.get("candidate_debug") if isinstance(forward_state.get("candidate_debug"), list) else [])
                        forward_selector = str(forward_state.get("forward_option_selector") or "")
                        self._add_step(step_results, "discover_forward_option_after_forward_reset", "success" if forward_selector else "failed", forward_option_selector=forward_selector)
                        if not forward_selector:
                            screenshot_path = _save_login_debug_screenshot(page, account_id)
                            return finish(False, "forward_picker_reopen_failed", "Forward option was not found after reset reload", "discover_forward_option_after_forward_reset", {"screenshot_path": screenshot_path, "final_page_url": _safe_page_url(page), "visible_menu_text": str(forward_state.get("visible_menu_text") or "")})
                        forward_click = self._click_selector_short(page, forward_selector, timeout_ms=1000)
                        forward_click["step"] = "click_forward_option_after_forward_reset"
                        click_attempts.append(forward_click)
                        if forward_click.get("status") != "success":
                            screenshot_path = _save_login_debug_screenshot(page, account_id)
                            return finish(False, "forward_picker_reopen_failed", "Forward option could not be clicked after reset reload", "click_forward_option_after_forward_reset", {"screenshot_path": screenshot_path, "final_page_url": _safe_page_url(page)})

                    _safe_wait_for_timeout(page, 500)
                    picker_state = self._forward_picker_state(page)
                    picker_visible = bool(picker_state.get("forward_picker_visible"))
                    picker_reopened = picker_visible
                    self._add_step(step_results, "verify_forward_picker_after_forward_reset", "success" if picker_visible else "failed", forward_picker_selector=str(picker_state.get("forward_picker_selector") or ""), picker_reopened=picker_reopened)
                    if not picker_visible:
                        screenshot_path = _save_login_debug_screenshot(page, account_id)
                        return finish(False, "forward_picker_reopen_failed", "Forward recipient picker was not visible after reset reopen", "verify_forward_picker_after_forward_reset", {"screenshot_path": screenshot_path, "final_page_url": _safe_page_url(page), **self._forward_failure_debug(page)})

                    picker_forensics = self._forward_picker_forensics(page, display_name)
                    reset_selected_state = self._forward_selected_recipients_state(page)
                    selected_count_after_reset = int(reset_selected_state.get("selected_count") or 0)
                    selected_names_after_reset = reset_selected_state.get("selected_names") if isinstance(reset_selected_state.get("selected_names"), list) else []
                    self._add_step(step_results, "inspect_preselected_recipients_after_forward_reset", "success" if selected_count_after_reset == 0 else "failed", selected_count=selected_count_after_reset, selected_names=selected_names_after_reset)
                    if selected_count_after_reset != 0:
                        screenshot_path = _save_login_debug_screenshot(page, account_id)
                        return finish(False, "stale_forward_recipient_state", "Forward picker still contained preselected recipients after the reset cycle", "inspect_preselected_recipients_after_forward_reset", {"recipient_picker_visible": True, "preselected_count": preselected_count, "preselected_names": preselected_names, "selected_count_after_reset": selected_count_after_reset, "selected_names_after_reset": selected_names_after_reset, "screenshot_path": screenshot_path, "final_page_url": _safe_page_url(page), **picker_forensics})

                search_state = self._forward_recipient_search_state(page)
                candidate_debug.extend(search_state.get("candidate_debug") if isinstance(search_state.get("candidate_debug"), list) else [])
                search_selector = str(search_state.get("recipient_search_selector") or "")
                self._add_step(step_results, "inspect_recipient_search_input", "success" if search_selector else "failed", recipient_search_selector=search_selector, selector_attempts=search_state.get("selector_attempts") or [])
                if not search_selector:
                    screenshot_path = _save_login_debug_screenshot(page, account_id)
                    return finish(False, "recipient_search_input_not_found", "Recipient picker search input was not found", "inspect_recipient_search_input", {"recipient_picker_visible": True, "screenshot_path": screenshot_path, "final_page_url": _safe_page_url(page), **self._forward_failure_debug(page), "selector_attempts": search_state.get("selector_attempts") or []})
                if clicks_before_search:
                    screenshot_path = _save_login_debug_screenshot(page, account_id)
                    return finish(False, "click_before_search_detected", "Recipient picker click was detected before search fill", "inspect_recipient_search_input", {"recipient_picker_visible": True, "clicks_before_search": clicks_before_search, "screenshot_path": screenshot_path})

                page.locator(search_selector).first.fill(display_name, timeout=min(self.default_timeout_ms, 1500))
                search_input_value = self._forward_search_input_value(page, search_selector)
                if search_input_value != display_name:
                    screenshot_path = _save_login_debug_screenshot(page, account_id)
                    return finish(False, "search_value_not_verified", "Recipient search input value did not match the exact display name", "search_recipient", {"recipient_picker_visible": True, "recipient_search_selector": search_selector, "search_input_selector": search_selector, "search_input_value": search_input_value, "screenshot_path": screenshot_path, "final_page_url": _safe_page_url(page)})
                _safe_wait_for_timeout(page, 500)
                last_successful_step = "search_recipient"
                self._add_step(step_results, "search_recipient", "success", recipient_search_selector=search_selector, searched_value=display_name, search_input_value=search_input_value, clicks_before_search=clicks_before_search)

                stability_state = self._forward_recipient_results_stability(page, display_name)
                if not stability_state.get("result_set_stable"):
                    screenshot_path = _save_login_debug_screenshot(page, account_id)
                    return finish(False, "recipient_results_not_stable", "Recipient results did not remain stable for the required interval", "inspect_recipient_results", {"recipient_picker_visible": True, "recipient_search_selector": search_selector, "search_input_selector": search_selector, "search_input_value": search_input_value, "screenshot_path": screenshot_path, "final_page_url": _safe_page_url(page), **stability_state})

                recipient_state = stability_state
                candidates = recipient_state.get("recipient_candidates") if isinstance(recipient_state.get("recipient_candidates"), list) else []
                candidate_debug.extend(candidates)
                recipient_candidate_debug = candidates
                exact_matches = [item for item in candidates if isinstance(item, dict) and item.get("exact_match")]
                visible_result_count = int(recipient_state.get("visible_result_count") or len(candidates))
                visible_result_names = recipient_state.get("visible_result_names") if isinstance(recipient_state.get("visible_result_names"), list) else [str(item.get("row_name") or item.get("text") or "") for item in candidates if isinstance(item, dict)]
                self._add_step(step_results, "inspect_recipient_results", "success" if len(exact_matches) == 1 and visible_result_count == 1 else "failed", exact_match_count=len(exact_matches), candidate_count=len(candidates), visible_result_count=visible_result_count, visible_result_names=visible_result_names)
                if not exact_matches:
                    screenshot_path = _save_login_debug_screenshot(page, account_id)
                    return finish(False, "recipient_not_found", "No exact recipient match was found", "inspect_recipient_results", {"recipient_picker_visible": True, "recipient_search_selector": search_selector, "search_input_selector": search_selector, "search_input_value": search_input_value, "exact_match_count": 0, "visible_result_count": visible_result_count, "visible_result_names": visible_result_names, "screenshot_path": screenshot_path, "final_page_url": _safe_page_url(page), **self._forward_failure_debug(page), "recipient_candidates": candidates})
                if visible_result_count != 1:
                    screenshot_path = _save_login_debug_screenshot(page, account_id)
                    return finish(False, "recipient_results_not_unique", "Recipient search did not reduce the picker to exactly one visible result", "inspect_recipient_results", {"recipient_picker_visible": True, "recipient_search_selector": search_selector, "search_input_selector": search_selector, "search_input_value": search_input_value, "exact_recipient_found": len(exact_matches) == 1, "exact_match_count": len(exact_matches), "visible_result_count": visible_result_count, "visible_result_names": visible_result_names, "target_row_name": str(exact_matches[0].get("row_name") or exact_matches[0].get("exact_text") or "") if exact_matches else "", "screenshot_path": screenshot_path, "final_page_url": _safe_page_url(page), "recipient_candidates": candidates})
                if len(exact_matches) != 1:
                    screenshot_path = _save_login_debug_screenshot(page, account_id)
                    return finish(False, "exact_recipient_not_only_result", "The single visible result was not exactly the requested recipient", "inspect_recipient_results", {"recipient_picker_visible": True, "recipient_search_selector": search_selector, "search_input_selector": search_selector, "search_input_value": search_input_value, "exact_recipient_found": False, "exact_match_count": len(exact_matches), "visible_result_count": visible_result_count, "visible_result_names": visible_result_names, "target_row_name": visible_result_names[0] if visible_result_names else "", "screenshot_path": screenshot_path, "final_page_url": _safe_page_url(page), "recipient_candidates": candidates})
                if str(exact_matches[0].get("row_name") or exact_matches[0].get("exact_text") or "").strip() != display_name:
                    screenshot_path = _save_login_debug_screenshot(page, account_id)
                    return finish(False, "exact_recipient_not_only_result", "The single visible result was not exactly the requested recipient", "inspect_recipient_results", {"recipient_picker_visible": True, "recipient_search_selector": search_selector, "search_input_selector": search_selector, "search_input_value": search_input_value, "exact_match_count": len(exact_matches), "visible_result_count": visible_result_count, "visible_result_names": visible_result_names, "target_row_name": str(exact_matches[0].get("row_name") or exact_matches[0].get("exact_text") or ""), "screenshot_path": screenshot_path, "final_page_url": _safe_page_url(page), "recipient_candidates": candidates})

                selected_before_target_state = self._forward_selected_recipients_state(page)
                selected_count_before_target = int(selected_before_target_state.get("selected_count") or 0)
                self._add_step(step_results, "verify_no_recipient_selected_before_target", "success" if selected_count_before_target == 0 else "failed", selected_count=selected_count_before_target, selected_names=selected_before_target_state.get("selected_names") or [])
                if selected_count_before_target != 0:
                    screenshot_path = _save_login_debug_screenshot(page, account_id)
                    return finish(False, "multiple_recipients_selected", "Recipient picker was not clean before selecting the target recipient", "verify_no_recipient_selected_before_target", {"recipient_picker_visible": True, "recipient_search_selector": search_selector, "search_input_selector": search_selector, "exact_recipient_found": True, "exact_match_count": len(exact_matches), "selected_count_before_target": selected_count_before_target, "selected_names_before_confirm": selected_before_target_state.get("selected_names") or [], "screenshot_path": screenshot_path, "final_page_url": _safe_page_url(page), **self._forward_failure_debug(page), "recipient_candidates": candidates})

                recipient_selector = str(exact_matches[0].get("click_selector") or exact_matches[0].get("selector") or "")
                if not recipient_selector.startswith('[data-clinicos-recipient-result='):
                    return finish(False, "destructive_click_blocked", "Recipient click selector was not classified as exact_recipient_select", "select_exact_recipient", {"recipient_result_selector": recipient_selector})
                click_diagnostic = self._forward_recipient_click_diagnostic(page, recipient_selector, display_name)
                if not click_diagnostic.get("click_safe"):
                    screenshot_path = _save_login_debug_screenshot(page, account_id)
                    return finish(False, "destructive_click_blocked", "Recipient click target was not proven to belong exclusively to the exact target row", "select_exact_recipient", {"recipient_picker_visible": True, "recipient_search_selector": search_selector, "search_input_selector": search_selector, "exact_recipient_found": True, "exact_match_count": len(exact_matches), "recipient_result_selector": recipient_selector, "screenshot_path": screenshot_path, "final_page_url": _safe_page_url(page), "recipient_candidates": candidates, **click_diagnostic})
                click_classifications.append({"category": "exact_recipient_select", "selector": recipient_selector, "status": "allowed"})
                destructive_clicks_attempted += 1
                select_click = self._click_selector_short(page, recipient_selector, timeout_ms=1000)
                select_click["step"] = "select_exact_recipient"
                click_attempts.append(select_click)
                self._add_step(step_results, "select_exact_recipient", "success" if select_click.get("status") == "success" else "failed", recipient_result_selector=recipient_selector, click_result=select_click)
                if select_click.get("status") != "success":
                    screenshot_path = _save_login_debug_screenshot(page, account_id)
                    return finish(False, "recipient_select_failed", "Exact recipient match could not be selected", "select_exact_recipient", {"recipient_picker_visible": True, "recipient_search_selector": search_selector, "exact_recipient_found": True, "recipient_result_selector": recipient_selector, "screenshot_path": screenshot_path, "final_page_url": _safe_page_url(page), **self._forward_failure_debug(page), "recipient_candidates": candidates})

                last_successful_step = "select_exact_recipient"
                if selection_only:
                    immediate_state = self._forward_selected_recipients_state(page)
                    immediate_snapshot = self._forward_modal_selection_snapshot(page, display_name)
                    screenshot_path = _save_login_debug_screenshot(page, account_id)
                    _safe_wait_for_timeout(page, 500)
                    delayed_state = self._forward_selected_recipients_state(page)
                    delayed_snapshot = self._forward_modal_selection_snapshot(page, display_name)
                    selected_names_immediate = immediate_state.get("selected_names") if isinstance(immediate_state.get("selected_names"), list) else []
                    selected_names_delayed = delayed_state.get("selected_names") if isinstance(delayed_state.get("selected_names"), list) else []
                    selection_only_ok = selected_names_delayed == [display_name]
                    self._add_step(step_results, "selection_only_snapshot", "success" if selection_only_ok else "failed", selected_names_immediately_after_click=selected_names_immediate, selected_names_after_500ms=selected_names_delayed)
                    selection_only_extra = {
                        "recipient_picker_visible": True,
                        "recipient_search_selector": search_selector,
                        "search_input_selector": search_selector,
                        "search_input_value": search_input_value,
                        "clicks_before_search": clicks_before_search,
                        "result_set_stable": True,
                        "visible_result_count": visible_result_count,
                        "visible_result_names": visible_result_names,
                        "exact_recipient_found": True,
                        "exact_match_count": len(exact_matches),
                        "recipient_result_selector": recipient_selector,
                        "recipient_selected": bool(selected_names_delayed),
                        "recipient_click_count": 1,
                        "confirm_clicked": False,
                        "confirm_click_count": 0,
                        "final_forwarded_recipient_count": 0,
                        "forward_verified": False,
                        "verified_forward_recipient_count": 0,
                        "selected_count_before_target": selected_count_before_target,
                        "selected_names_immediately_after_click": selected_names_immediate,
                        "selected_names_after_click": selected_names_delayed,
                        "selected_names_after_500ms": selected_names_delayed,
                        "selected_count_immediately_after_click": int(immediate_state.get("selected_count") or 0),
                        "selected_count_after_500ms": int(delayed_state.get("selected_count") or 0),
                        "selected_count_after_target": int(delayed_state.get("selected_count") or 0),
                        "selected_names_after_target": selected_names_delayed,
                        "bale_target_selected": any(str(name).strip() == display_name for name in selected_names_delayed),
                        "sahar_selected": any(str(name).strip().lower() == "sahar" for name in selected_names_delayed),
                        "sahar_selected_immediately": any(str(name).strip().lower() == "sahar" for name in selected_names_immediate),
                        "sahar_selected_after_500ms": any(str(name).strip().lower() == "sahar" for name in selected_names_delayed),
                        "screenshot_path": screenshot_path,
                        "final_page_url": _safe_page_url(page),
                        "recipient_candidates": candidates,
                        "dom_mutations_after_click": {"immediate": immediate_snapshot, "after_500ms": delayed_snapshot},
                        **click_diagnostic,
                    }
                    if not selection_only_ok:
                        return finish(False, "unexpected_selected_recipient", "Selection-only diagnostic did not produce exactly the requested recipient", "selection_only_snapshot", selection_only_extra)
                    return finish(True, extra=selection_only_extra)
                _safe_wait_for_timeout(page, 300)
                selected_after_target_state = self._forward_selected_recipients_state(page)
                selected_count_after_target = int(selected_after_target_state.get("selected_count") or 0)
                selected_names_after_target = selected_after_target_state.get("selected_names") if isinstance(selected_after_target_state.get("selected_names"), list) else []
                target_selected = selected_count_after_target == 1 and selected_names_after_target == [display_name]
                self._add_step(step_results, "verify_target_recipient_selected", "success" if target_selected else "failed", selected_count=selected_count_after_target, selected_names=selected_names_after_target)
                if not target_selected:
                    screenshot_path = _save_login_debug_screenshot(page, account_id)
                    error_code = "multiple_recipients_selected" if selected_count_after_target > 1 else "unexpected_selected_recipient"
                    return finish(False, error_code, "Selected recipient did not resolve to exactly the requested target", "verify_target_recipient_selected", {"recipient_picker_visible": True, "recipient_search_selector": search_selector, "search_input_selector": search_selector, "exact_recipient_found": True, "exact_match_count": len(exact_matches), "recipient_result_selector": recipient_selector, "recipient_selected": bool(selected_count_after_target), "selected_count_before_target": selected_count_before_target, "selected_count_after_target": selected_count_after_target, "selected_names_after_target": selected_names_after_target, "screenshot_path": screenshot_path, "final_page_url": _safe_page_url(page), **self._forward_failure_debug(page), "recipient_candidates": candidates})

                confirm_state = self._forward_confirm_button_state(page)
                candidate_debug.extend(confirm_state.get("candidate_debug") if isinstance(confirm_state.get("candidate_debug"), list) else [])
                confirm_selector = str(confirm_state.get("confirm_button_selector") or "")
                self._add_step(step_results, "inspect_forward_confirm_button", "success" if confirm_selector else "failed", confirm_button_selector=confirm_selector)
                if not confirm_selector:
                    screenshot_path = _save_login_debug_screenshot(page, account_id)
                    return finish(False, "forward_confirm_button_not_found", "Forward confirmation button was not found or enabled", "inspect_forward_confirm_button", {"recipient_picker_visible": True, "recipient_search_selector": search_selector, "search_input_selector": search_selector, "exact_recipient_found": True, "exact_match_count": len(exact_matches), "recipient_result_selector": recipient_selector, "recipient_selected": True, "selected_count_before_target": selected_count_before_target, "selected_count_after_target": selected_count_after_target, "selected_names_after_target": selected_names_after_target, "screenshot_path": screenshot_path, "final_page_url": _safe_page_url(page), **self._forward_failure_debug(page), "recipient_candidates": candidates})

                before_confirm_state = self._forward_selected_recipients_state(page)
                selected_count_before_confirm = int(before_confirm_state.get("selected_count") or 0)
                selected_names_before_confirm = before_confirm_state.get("selected_names") if isinstance(before_confirm_state.get("selected_names"), list) else []
                confirm_safe = selected_count_before_confirm == 1 and selected_names_before_confirm == [display_name]
                self._add_step(step_results, "verify_target_recipient_before_confirm", "success" if confirm_safe else "failed", selected_count=selected_count_before_confirm, selected_names=selected_names_before_confirm)
                if not confirm_safe:
                    screenshot_path = _save_login_debug_screenshot(page, account_id)
                    if selected_count_before_confirm > 1:
                        error_code = "multiple_recipients_selected"
                    elif selected_count_before_confirm == 1:
                        error_code = "wrong_recipient_selected"
                    else:
                        error_code = "target_selection_not_verified"
                    return finish(False, error_code, "Forward confirmation blocked because the selected recipient set was not exactly the requested target", "verify_target_recipient_before_confirm", {"recipient_picker_visible": True, "recipient_search_selector": search_selector, "search_input_selector": search_selector, "exact_recipient_found": True, "exact_match_count": len(exact_matches), "recipient_result_selector": recipient_selector, "recipient_selected": bool(selected_count_before_confirm), "confirm_button_selector": confirm_selector, "selected_count_before_target": selected_count_before_target, "selected_count_after_target": selected_count_after_target, "selected_names_after_target": selected_names_after_target, "selected_count_before_confirm": selected_count_before_confirm, "selected_names_before_confirm": selected_names_before_confirm, "screenshot_path": screenshot_path, "final_page_url": _safe_page_url(page), **self._forward_failure_debug(page), "recipient_candidates": candidates})

                if confirm_selector != '.ReactModal__Overlay [role="button"][aria-label="send-button-forward-messages"][data-testid="bold-send2-icon"]':
                    return finish(False, "destructive_click_blocked", "Confirm click selector was not classified as forward_confirm", "click_forward_confirm", {"confirm_button_selector": confirm_selector})
                click_classifications.append({"category": "forward_confirm", "selector": confirm_selector, "status": "allowed"})
                destructive_clicks_attempted += 1
                confirm_click_count += 1
                if confirm_click_count > 1:
                    screenshot_path = _save_login_debug_screenshot(page, account_id)
                    return finish(False, "destructive_click_blocked", "Forward confirmation click count exceeded one", "click_forward_confirm", {"screenshot_path": screenshot_path, "confirm_button_selector": confirm_selector})
                confirm_click = self._click_selector_short(page, confirm_selector, timeout_ms=1000)
                confirm_click["step"] = "click_forward_confirm"
                click_attempts.append(confirm_click)
                self._add_step(step_results, "click_forward_confirm", "success" if confirm_click.get("status") == "success" else "failed", confirm_button_selector=confirm_selector, click_result=confirm_click)
                if confirm_click.get("status") != "success":
                    screenshot_path = _save_login_debug_screenshot(page, account_id)
                    return finish(False, "forward_confirm_failed", "Forward confirmation button could not be clicked", "click_forward_confirm", {"recipient_picker_visible": True, "recipient_search_selector": search_selector, "search_input_selector": search_selector, "exact_recipient_found": True, "exact_match_count": len(exact_matches), "recipient_result_selector": recipient_selector, "recipient_selected": True, "confirm_button_selector": confirm_selector, "selected_count_before_target": selected_count_before_target, "selected_count_after_target": selected_count_after_target, "selected_names_after_target": selected_names_after_target, "selected_count_before_confirm": selected_count_before_confirm, "selected_names_before_confirm": selected_names_before_confirm, "screenshot_path": screenshot_path, "final_page_url": _safe_page_url(page), **self._forward_failure_debug(page), "recipient_candidates": candidates})

                last_successful_step = "click_forward_confirm"
                _safe_wait_for_timeout(page, 800)
                verify_state = self._forward_success_state(page, display_name)
                verified_forward_recipient_count = int(verify_state.get("verified_forward_recipient_count") or 0)
                if verified_forward_recipient_count > 1:
                    screenshot_path = _save_login_debug_screenshot(page, account_id)
                    return finish(False, "unexpected_forward_recipient_count", "Forward success text reported more than one recipient", "verify_forward_success", {"recipient_picker_visible": bool(verify_state.get("recipient_picker_visible", True)), "recipient_search_selector": search_selector, "search_input_selector": search_selector, "exact_recipient_found": True, "exact_match_count": len(exact_matches), "recipient_result_selector": recipient_selector, "recipient_selected": True, "confirm_button_selector": confirm_selector, "confirm_clicked": True, "selected_count_before_target": selected_count_before_target, "selected_count_after_target": selected_count_after_target, "selected_names_after_target": selected_names_after_target, "selected_count_before_confirm": selected_count_before_confirm, "selected_names_before_confirm": selected_names_before_confirm, "screenshot_path": screenshot_path, "final_page_url": _safe_page_url(page), **self._forward_failure_debug(page), "recipient_candidates": candidates, **verify_state})
                verified = bool(verify_state.get("forward_verified"))
                self._add_step(step_results, "verify_forward_success", "success" if verified else "failed", verification_method=str(verify_state.get("verification_method") or ""))
                if not verified:
                    screenshot_path = _save_login_debug_screenshot(page, account_id)
                    return finish(False, "forward_not_verified", "Forward was not verified after confirmation", "verify_forward_success", {"recipient_picker_visible": True, "recipient_search_selector": search_selector, "search_input_selector": search_selector, "exact_recipient_found": True, "exact_match_count": len(exact_matches), "recipient_result_selector": recipient_selector, "recipient_selected": True, "confirm_button_selector": confirm_selector, "confirm_clicked": True, "selected_count_before_target": selected_count_before_target, "selected_count_after_target": selected_count_after_target, "selected_names_after_target": selected_names_after_target, "selected_count_before_confirm": selected_count_before_confirm, "selected_names_before_confirm": selected_names_before_confirm, "screenshot_path": screenshot_path, "final_page_url": _safe_page_url(page), **self._forward_failure_debug(page), "recipient_candidates": candidates, **verify_state})

                last_successful_step = "verify_forward_success"
                self.browser_manager.save_session(account_id)
                return finish(True, extra={"recipient_picker_visible": bool(verify_state.get("recipient_picker_visible", False)), "recipient_search_selector": search_selector, "search_input_selector": search_selector, "search_input_value": search_input_value, "clicks_before_search": clicks_before_search, "result_set_stable": True, "visible_result_count": visible_result_count, "visible_result_names": visible_result_names, "exact_recipient_found": True, "exact_match_count": len(exact_matches), "recipient_result_selector": recipient_selector, "recipient_selected": True, "recipient_click_count": 1, "confirm_button_selector": confirm_selector, "confirm_clicked": True, "forward_verified": True, "final_forwarded_recipient_count": verified_forward_recipient_count, "preselected_count": preselected_count, "preselected_names": preselected_names, "cleared_preselected_count": 0, "selected_count_before_target": selected_count_before_target, "selected_count_after_target": selected_count_after_target, "selected_names_after_target": selected_names_after_target, "selected_names_after_click": selected_names_after_target, "sahar_selected": any(str(name).strip().lower() == "sahar" for name in selected_names_after_target), "selected_count_before_confirm": selected_count_before_confirm, "selected_names_before_confirm": selected_names_before_confirm, "final_page_url": _safe_page_url(page), "recipient_candidates": candidates, **click_diagnostic, **verify_state})
        except Exception as exc:
            diagnostics = self._page_debug_info(page, account_id) if page is not None else {}
            return finish(False, _browser_error_code(exc), str(exc), "forward_message_to_contact", {"final_page_url": diagnostics.get("page_url") or diagnostics.get("current_url") or "", "screenshot_path": diagnostics.get("screenshot_path") or ""})

    def forward_latest_channel_message(
        self,
        account_id: str,
        phone: str,
        source_channel_uid: str | None = None,
        display_name: str | None = None,
        recipient_id: str | None = None,
        job_id: str | None = None,
        campaign_id: str | None = None,
        idempotency_key: str | None = None,
        dry_run: bool = False,
        provider_mode: str | None = None,
        execution_plan: Any | None = None,
        runtime_session: Any | None = None,
        close_session_when_done: bool = True,
        controlled_live_no_send: bool = False,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        action = "forward_latest_channel_message"
        step_results: list[dict[str, Any]] = []
        last_successful_step: str | None = None
        account_id = str(account_id or "").strip()
        requested_source_channel_uid = str(source_channel_uid or "").strip()
        configured_source_channel_uid = ""
        effective_source_channel_uid = ""
        phone_input = phone
        resolved_display_name = str(display_name or "").strip()
        resolved_recipient_id = str(recipient_id or "").strip()
        contact_reused = False
        contact_created = False

        def finish(
            success: bool,
            error_code: str | None = None,
            error_message: str | None = None,
            failed_step: str | None = None,
            extra: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            payload: dict[str, Any] = {
                "success": success,
                "ok": success,
                "action": action,
                "scenario_id": None,
                "job_id": job_id,
                "campaign_id": campaign_id,
                "account_id": account_id,
                "recipient_id": resolved_recipient_id,
                "idempotency_key": idempotency_key,
                "phone": phone_input,
                "display_name": resolved_display_name,
                "requested_source_channel_uid": requested_source_channel_uid,
                "configured_source_channel_uid": configured_source_channel_uid,
                "effective_source_channel_uid": effective_source_channel_uid,
                "channel_uid_verified": False,
                "contact_reused": contact_reused,
                "contact_created": contact_created,
                "selected_message_data_date": None,
                "selected_message_preview": "",
                "selected_message_signature": "",
                "exact_match_count": 0,
                "selected_names_before_confirm": [],
                "confirm_click_count": 0,
                "success_toast_text": "",
                "verified_forwarded_recipient_count": 0,
                "final_forwarded_recipient_count": 0,
                "forward_verified": False,
                "diagnostics_consistent": False,
                "diagnostics_consistency_errors": [],
                "failed_step": failed_step,
                "last_successful_step": last_successful_step,
                "error_code": error_code,
                "error_message": error_message,
                "screenshot_path": "",
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "step_results": step_results,
                "dry_run": bool(dry_run),
                "controlled_live_no_send": bool(controlled_live_no_send),
                "stopped_before_send": bool(controlled_live_no_send),
                "remote_message_id": None,
                "outcome": None if not controlled_live_no_send else "cancelled",
            }
            if extra:
                payload.update(extra)
            return payload

        if isinstance(phone, (list, tuple, set)) or isinstance(display_name, (list, tuple, set)) or isinstance(account_id, (list, tuple, set)):
            self._add_step(step_results, "validate_request", "failed", error_code="single_recipient_required")
            return finish(False, "single_recipient_required", "One delivery job targets exactly one recipient through exactly one account", "validate_request")
        phone_text = str(phone or "").strip()
        if not account_id:
            self._add_step(step_results, "validate_request", "failed", error_code="invalid_account_id")
            return finish(False, "invalid_account_id", "account_id is required", "validate_request")
        if not phone_text or any(separator in phone_text for separator in [",", ";", "\n", "\r", "|"]):
            self._add_step(step_results, "validate_request", "failed", error_code="single_phone_required")
            return finish(False, "single_phone_required", "Exactly one phone number is required", "validate_request")
        if resolved_display_name and any(separator in resolved_display_name for separator in [",", ";", "\n", "\r", "|"]):
            self._add_step(step_results, "validate_request", "failed", error_code="single_display_name_required")
            return finish(False, "single_display_name_required", "Exactly one display name is allowed", "validate_request")
        if not bale_account_store.get_account(account_id):
            self._add_step(step_results, "validate_request", "failed", error_code="account_not_found", account_id=account_id)
            return finish(False, "account_not_found", f"Bale account not found: {account_id}", "validate_request")
        self._add_step(step_results, "validate_request", "success", account_id=account_id, phone=phone_text)
        last_successful_step = "validate_request"

        configured = bale_account_store.get_source_channel(account_id) or {}
        configured_source_channel_uid = str(configured.get("source_channel_uid") or "")
        try:
            effective_source_channel_uid = normalize_source_channel_uid(requested_source_channel_uid or configured_source_channel_uid or configured.get("source_channel_url") or "")
        except ValueError as exc:
            self._add_step(step_results, "resolve_source_channel", "failed", error_code="source_channel_not_configured")
            return finish(False, "source_channel_not_configured", str(exc), "resolve_source_channel")
        requested_source_channel_uid = normalize_source_channel_uid(requested_source_channel_uid) if requested_source_channel_uid else ""
        self._add_step(
            step_results,
            "resolve_source_channel",
            "success",
            requested_source_channel_uid=requested_source_channel_uid,
            configured_source_channel_uid=configured_source_channel_uid,
            effective_source_channel_uid=effective_source_channel_uid,
        )
        last_successful_step = "resolve_source_channel"

        try:
            if controlled_live_no_send:
                if not resolved_display_name:
                    self._add_step(step_results, "save_or_resolve_contact", "failed", error_code="controlled_live_no_send_display_name_required")
                    return finish(False, "controlled_live_no_send_display_name_required", "No-send mode requires the immutable stored Bale display name", "save_or_resolve_contact")
                resolved_contact, preexisting_contact = {"id": resolved_recipient_id, "phone_normalized": phone_text, "display_name": resolved_display_name}, False
            elif dry_run:
                existing_contact = self.contact_store.get_bale_contact(account_id, phone_text)
                if existing_contact is None:
                    self._add_step(step_results, "save_or_resolve_contact", "skipped", reason="dry_run_no_stable_name_allocation")
                    return finish(
                        False,
                        "dry_run_contact_not_prepared",
                        "Dry-run does not allocate stable contact names",
                        "save_or_resolve_contact",
                    )
                resolved_contact, preexisting_contact = existing_contact, False
            else:
                resolved_contact, preexisting_contact = self.contact_store.get_or_create_bale_contact(account_id, phone_text)
        except BaleContactError as exc:
            self._add_step(step_results, "save_or_resolve_contact", "failed", error_code=exc.error_code, phone=phone_text)
            return finish(False, exc.error_code, str(exc), "save_or_resolve_contact")

        resolved_display_name = str(resolved_contact.get("display_name") or resolved_display_name).strip()
        resolved_recipient_id = resolved_recipient_id or str(resolved_contact.get("id") or "")
        contact_created = bool(preexisting_contact)
        contact_reused = not bool(preexisting_contact)
        if controlled_live_no_send:
            contact_result = {
                "success": True,
                "ok": True,
                "action": "resolve_bale_contact_no_send",
                "account_id": account_id,
                "phone": phone_text,
                "phone_normalized": phone_text,
                "display_name": resolved_display_name,
                "contact_id": resolved_recipient_id,
                "contact_store_status": "immutable_execution_plan",
                "contact_save_status": "not_attempted",
                "failed_step": None,
                "last_successful_step": "resolve_contact_name",
                "error_code": None,
                "error_message": None,
                "step_results": [{"step": "resolve_contact_name", "status": "success", "display_name": resolved_display_name, "contact_store_status": "immutable_execution_plan"}],
            }
        elif not preexisting_contact:
            contact_result = {
                "success": True,
                "ok": True,
                "action": "save_bale_contact",
                "account_id": account_id,
                "phone": phone_text,
                "phone_normalized": str(resolved_contact.get("phone_normalized") or ""),
                "display_name": resolved_display_name,
                "contact_id": resolved_recipient_id,
                "sequence_number": resolved_contact.get("sequence_number"),
                "contact_store_status": "existing",
                "contact_save_status": "already_exists",
                "failed_step": None,
                "last_successful_step": "resolve_contact_name",
                "error_code": None,
                "error_message": None,
                "step_results": [
                    {
                        "step": "resolve_contact_name",
                        "status": "success",
                        "phone_normalized": str(resolved_contact.get("phone_normalized") or ""),
                        "display_name": resolved_display_name,
                        "contact_store_status": "existing",
                        "contact_id": resolved_recipient_id,
                        "sequence_number": resolved_contact.get("sequence_number"),
                    }
                ],
            }
        else:
            contact_result = self.save_bale_contact(
                account_id,
                phone_text,
                provider_mode=provider_mode or "native_chrome",
                runtime_session=runtime_session,
                close_session_when_done=close_session_when_done,
            )
        step_results.append({"step": "save_or_resolve_contact", "status": "success" if contact_result.get("success") else "failed", "action_result": contact_result})
        if not contact_result.get("success"):
            return finish(
                False,
                str(contact_result.get("error_code") or "contact_save_failed"),
                str(contact_result.get("error_message") or "Bale contact save failed"),
                "save_or_resolve_contact",
                {"screenshot_path": contact_result.get("screenshot_path") or "", "contact_result": contact_result},
            )
        resolved_display_name = str(contact_result.get("display_name") or resolved_display_name).strip()
        resolved_recipient_id = resolved_recipient_id or str(contact_result.get("contact_id") or "")
        last_successful_step = "save_or_resolve_contact"

        forward_result = self.forward_message_to_contact(
            account_id=account_id,
            source_channel_uid=effective_source_channel_uid,
            display_name=resolved_display_name,
            dry_run=dry_run,
            selection_only=bool(controlled_live_no_send),
            provider_mode=provider_mode or "native_chrome",
            runtime_session=runtime_session,
            close_session_when_done=close_session_when_done,
        )
        self._normalize_forward_message_to_contact_diagnostics(forward_result)
        forward_steps = forward_result.get("step_results") if isinstance(forward_result.get("step_results"), list) else []
        forward_extra = {
            "channel_uid_verified": bool(forward_result.get("channel_uid_verified")),
            "selected_message_data_date": forward_result.get("selected_message_data_date"),
            "selected_message_preview": str(forward_result.get("selected_message_preview") or ""),
            "selected_message_signature": str(forward_result.get("selected_message_signature") or ""),
            "exact_match_count": int(forward_result.get("exact_match_count") or 0),
            "selected_names_before_confirm": forward_result.get("selected_names_before_confirm") if isinstance(forward_result.get("selected_names_before_confirm"), list) else [],
            "confirm_click_count": int(forward_result.get("confirm_click_count") or 0),
            "success_toast_text": str(forward_result.get("success_toast_text") or ""),
            "verified_forwarded_recipient_count": int(forward_result.get("verified_forwarded_recipient_count") or 0),
            "final_forwarded_recipient_count": int(forward_result.get("final_forwarded_recipient_count") or 0),
            "forward_verified": bool(forward_result.get("forward_verified")),
            "diagnostics_consistent": bool(forward_result.get("diagnostics_consistent")),
            "diagnostics_consistency_errors": forward_result.get("diagnostics_consistency_errors") if isinstance(forward_result.get("diagnostics_consistency_errors"), list) else [],
            "screenshot_path": forward_result.get("screenshot_path") or "",
            "forward_result": forward_result,
        }
        step_results.append({"step": "open_source_channel", "status": "success" if any(step.get("step") == "wait_source_channel_ready" and step.get("status") == "success" for step in forward_steps if isinstance(step, dict)) else "failed" if not forward_result.get("success") and forward_result.get("failed_step") in {"navigate_source_channel", "wait_source_channel_ready"} else "success"})
        if not forward_result.get("success") and forward_result.get("failed_step") in {"navigate_source_channel", "wait_source_channel_ready"}:
            return finish(False, str(forward_result.get("error_code") or "source_channel_open_failed"), str(forward_result.get("error_message") or "Bale source channel verification failed"), "open_source_channel", forward_extra)
        last_successful_step = "open_source_channel"

        public_step_map = [
            ("locate_latest_message", {"locate_latest_channel_message"}),
            ("open_forward_picker", {"discover_message_menu", "open_message_menu", "discover_forward_option", "click_forward_option", "verify_forward_picker"}),
            ("select_recipient", {"inspect_preselected_recipients", "inspect_preselected_recipients_after_forward_reset", "inspect_recipient_search_input", "search_recipient", "inspect_recipient_results", "verify_no_recipient_selected_before_target", "select_exact_recipient", "selection_only_snapshot", "verify_target_recipient_selected"}),
            ("confirm_forward", {"inspect_forward_confirm_button", "verify_target_recipient_before_confirm", "click_forward_confirm"}),
            ("verify_forward", {"verify_forward_success", "dry_run_forensic_inspection"}),
        ]
        failed_internal_step = str(forward_result.get("failed_step") or "")
        failed_public_step = ""
        for public_step, internal_names in public_step_map:
            if dry_run and forward_result.get("success") and public_step in {"select_recipient", "confirm_forward", "verify_forward"}:
                status = "skipped"
                step_results.append({"step": public_step, "status": "skipped", "reason": "dry_run_no_recipient_selection_or_confirm"})
            else:
                status = "success" if any(isinstance(step, dict) and step.get("step") in internal_names and step.get("status") in {"success", "assumed_success"} for step in forward_steps) else "failed" if not forward_result.get("success") and str(forward_result.get("failed_step") or "") in internal_names else "pending"
                step_results.append({"step": public_step, "status": status})
            if status == "failed":
                failed_public_step = public_step
                return finish(False, str(forward_result.get("error_code") or "forward_failed"), str(forward_result.get("error_message") or "Bale forward failed"), public_step, forward_extra)
            if status == "success":
                last_successful_step = public_step
            if failed_internal_step in internal_names and not failed_public_step:
                failed_public_step = public_step

        success = bool(forward_result.get("success"))
        if success and controlled_live_no_send:
            forward_extra.update(
                {
                    "diagnostics_consistent": True,
                    "diagnostics_consistency_errors": [],
                    "forward_verified": False,
                    "confirm_click_count": 0,
                    "verified_forwarded_recipient_count": 0,
                    "final_forwarded_recipient_count": 0,
                    "source_resolved": bool(forward_result.get("channel_uid_verified")),
                    "recipient_resolved": True,
                    "composer_visible": bool(forward_result.get("recipient_picker_visible")),
                    "final_send_control_visible": bool(forward_result.get("confirm_button_selector")),
                    "authentication_state": "authenticated",
                    "sender_account_state": "available",
                    "recipient_account_state": "resolved",
                    "stopped_before_send": True,
                    "remote_message_id": None,
                    "outcome": "cancelled",
                }
            )
            step_results.append({"step": "controlled_live_no_send_boundary", "status": "success", "stopped_before_send": True})
            last_successful_step = "controlled_live_no_send_boundary"
            final_extra = {
                "phone": str(contact_result.get("phone_normalized") or phone_text),
                "display_name": resolved_display_name,
                "recipient_id": resolved_recipient_id,
                "contact_result": contact_result,
                **forward_extra,
            }
            step_results.append({"step": "persist_result", "status": "success"})
            last_successful_step = "persist_result"
            return finish(True, None, None, None, final_extra)
        if success and not dry_run and int(forward_result.get("verified_forwarded_recipient_count") or 0) != 1:
            success = False
            failed_public_step = "verify_forward"
            step_results.append({"step": "verify_forward_single_recipient_count", "status": "failed", "verified_forwarded_recipient_count": int(forward_result.get("verified_forwarded_recipient_count") or 0)})
        if success and dry_run:
            forward_extra.update(
                {
                    "diagnostics_consistent": True,
                    "diagnostics_consistency_errors": [],
                    "forward_verified": False,
                    "confirm_click_count": 0,
                    "verified_forwarded_recipient_count": 0,
                    "final_forwarded_recipient_count": 0,
                }
            )
        final_extra = {
            "phone": str(contact_result.get("phone_normalized") or phone_text),
            "display_name": resolved_display_name,
            "recipient_id": resolved_recipient_id,
            "contact_result": contact_result,
            **forward_extra,
        }
        step_results.append({"step": "persist_result", "status": "success"})
        last_successful_step = "persist_result"
        return finish(
            success,
            None if success else str(forward_result.get("error_code") or "forward_failed"),
            None if success else str(forward_result.get("error_message") or "Bale forward failed"),
            None if success else (failed_public_step or str(forward_result.get("failed_step") or "verify_forward")),
            final_extra,
        )

    def _normalize_forward_message_to_contact_diagnostics(self, payload: dict[str, Any]) -> None:
        target = str(payload.get("display_name") or "").strip()
        effective_uid = str(payload.get("effective_source_channel_uid") or payload.get("source_channel_uid") or "").strip()
        steps = payload.get("step_results") if isinstance(payload.get("step_results"), list) else []

        if not payload.get("channel_uid_verified"):
            for step in steps:
                if not isinstance(step, dict):
                    continue
                if step.get("step") in {"navigate_source_channel", "reload_source_channel_after_forward_reset"} and step.get("status") == "success":
                    final_url = str(step.get("final_page_url") or "")
                    if effective_uid and f"uid={effective_uid}" in final_url:
                        payload["channel_uid_verified"] = True
                        break
            if not payload.get("channel_uid_verified"):
                final_url = str(payload.get("final_page_url") or payload.get("final_channel_url") or "")
                if effective_uid and f"uid={effective_uid}" in final_url:
                    payload["channel_uid_verified"] = True

        latest_step = None
        for step in steps:
            if isinstance(step, dict) and step.get("step") in {"locate_latest_channel_message", "locate_latest_channel_message_after_forward_reset"} and step.get("status") == "success":
                latest_step = step
        if latest_step:
            if not payload.get("selected_message_preview"):
                payload["selected_message_preview"] = str(latest_step.get("selected_message_preview") or latest_step.get("latest_message_text_preview") or "")
            if payload.get("selected_message_data_date") is None:
                payload["selected_message_data_date"] = latest_step.get("selected_message_data_date")
            if not payload.get("selected_message_signature"):
                payload["selected_message_signature"] = str(latest_step.get("selected_message_signature") or "")

        selected_before_confirm = payload.get("selected_names_before_confirm") if isinstance(payload.get("selected_names_before_confirm"), list) else []
        selected_single_target = len(selected_before_confirm) == 1 and str(selected_before_confirm[0]).strip() == target
        toast = str(payload.get("success_toast_text") or "")
        multi_toast = bool(re.search(r"(?:\b[2-9]\b|[Û²-Û¹Ù¢-Ù©]|two|three|four|five|six|seven|eight|nine|Ø¯Ùˆ|Ø³Ù‡|Ú†Ù†Ø¯)\s*(?:chat|chats|Ú¯ÙØªÚ¯Ùˆ|Ú¯ÙØªâ€ŒÙˆÚ¯Ùˆ|Ú†Øª)", toast, re.IGNORECASE))
        single_toast = bool(target and target in toast) or bool(re.search(r"(?:\b1\b|[Û±Ù¡]|one|ÛŒÚ©)\s*(?:chat|chats|Ú¯ÙØªÚ¯Ùˆ|Ú¯ÙØªâ€ŒÙˆÚ¯Ùˆ|Ú†Øª)", toast, re.IGNORECASE))
        canonical_count = 0
        if multi_toast:
            canonical_count = 2
        elif bool(payload.get("forward_verified")) and selected_single_target and single_toast:
            canonical_count = 1
        payload["verified_forwarded_recipient_count"] = canonical_count
        payload["verified_forward_recipient_count"] = canonical_count
        payload["final_forwarded_recipient_count"] = canonical_count
        payload["destructive_click_classifications"] = payload.get("click_classifications") if isinstance(payload.get("click_classifications"), list) else []

        consistency_errors: list[str] = []
        if payload.get("success") is True and payload.get("forward_verified") is not True:
            consistency_errors.append("success_without_forward_verified")
        if payload.get("forward_verified") is True and int(payload.get("confirm_click_count") or 0) != 1:
            consistency_errors.append("forward_verified_without_single_confirm_click")
        if payload.get("forward_verified") is True and canonical_count != 1:
            consistency_errors.append("forward_verified_without_single_verified_recipient_count")
        if payload.get("success") is True and payload.get("channel_uid_verified") is not True:
            consistency_errors.append("success_without_channel_uid_verified")
        if payload.get("success") is True and not selected_single_target:
            consistency_errors.append("success_without_single_selected_recipient_before_confirm")
        payload["diagnostics_consistency_errors"] = consistency_errors
        payload["diagnostics_consistent"] = len(consistency_errors) == 0

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
        login_check = self._detect_login_state(page, timeout_ms=3000)
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
                "ØµÙØ­Ù‡ Ø±Ø§Ù‡Ù†Ù…Ø§ÛŒ Ù†ØµØ¨ Ø¨Ù„Ù‡ Ù†Ù…Ø§ÛŒØ´ Ø¯Ø§Ø¯Ù‡ Ø´Ø¯Ù‡ Ø§Ø³Øª. Ø±ÙˆÛŒ Â«Ù…ØªÙˆØ¬Ù‡ Ø´Ø¯Ù…Â» Ø¨Ø²Ù†ÛŒØ¯ Ùˆ ÙˆØ§Ø±Ø¯ Ø¨Ù„Ù‡ Ø´ÙˆÛŒØ¯.",
                {"login_check": login_check, **self._page_debug_info(page, account_id)},
            )
        if not login_check["logged_in"]:
            self._record_step(
                execution_logs,
                account_id,
                "check_login",
                "failed",
                "Manual Bale login is required before sending a test message",
                error_code=str(login_check.get("error_code") or "not_logged_in"),
            )
            error_code = str(login_check.get("error_code") or "not_logged_in")
            raise BalePluginError(
                error_code,
                "Bale login state could not be determined" if error_code == "login_state_unknown" else "Manual Bale login is required before sending a test message",
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
        page.fill(search_input, target, timeout=min(self.default_timeout_ms, 2000))
        chat_item = self._require_selector(
            page,
            selectors.CHAT_ITEM_SELECTORS,
            "target_not_found",
            "Target chat was not found after search",
        )
        self._record_step(execution_logs, account_id, "search_target", "success", f"Target candidate found: {chat_item}")

        self._record_step(execution_logs, account_id, "open_chat", "started", "Opening target chat")
        page.click(chat_item, timeout=min(self.default_timeout_ms, 2000))
        self._record_step(execution_logs, account_id, "open_chat", "success", "Target chat opened")

        message_input = self._require_selector(
            page,
            selectors.MESSAGE_INPUT_SELECTORS,
            "message_input_not_found",
            "Bale message input was not found",
        )
        self._record_step(execution_logs, account_id, "type_message", "started", "Typing one test message")
        page.fill(message_input, message, timeout=min(self.default_timeout_ms, 2000))
        self._record_step(execution_logs, account_id, "type_message", "success", "Test message typed")

        send_button = self._require_selector(
            page,
            selectors.SEND_BUTTON_SELECTORS,
            "send_button_not_found",
            "Bale send button was not found",
        )
        self._record_step(execution_logs, account_id, "click_send", "started", "Clicking send button once")
        page.click(send_button, timeout=min(self.default_timeout_ms, 2000))

        sent_indicator = self._first_visible_selector(
            page,
            selectors.MESSAGE_SENT_INDICATOR_SELECTORS,
            timeout_ms=1500,
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
                    "AdsPower Ø¯Ø± Ø¯Ø³ØªØ±Ø³ Ù†ÛŒØ³Øª. Ø¨Ø±Ø§ÛŒ ØªØ³Øª Ù…Ø­Ù„ÛŒ Ø§Ø² Chrome Ù…Ø¹Ù…ÙˆÙ„ÛŒ Ø§Ø³ØªÙØ§Ø¯Ù‡ Ú©Ù†ÛŒØ¯.",
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
    def _runtime_or_page_session(self, account_id: str, provider_mode: str | None = None, runtime_session: Any | None = None) -> Any:
        if runtime_session is not None:
            page = getattr(runtime_session, "page", None)
            if page is None and isinstance(runtime_session, dict):
                page = runtime_session.get("page")
            profile_path = getattr(runtime_session, "profile_path", "") if not isinstance(runtime_session, dict) else runtime_session.get("profile_path", "")
            session_id = getattr(runtime_session, "session_id", "") if not isinstance(runtime_session, dict) else runtime_session.get("session_id", "")
            yield (
                page,
                {
                    "provider_mode": provider_mode or "native_chrome",
                    "browser_reused": True,
                    "session_reused": True,
                    "session_id": session_id,
                    "profile_dir": str(profile_path or ""),
                    "browser_path": getattr(self.browser_manager, "last_browser_path", None),
                },
            )
            return
        with self._page_session(account_id, provider_mode=provider_mode) as session:
            yield session

    def create_reusable_runtime_session(self, account_id: str, provider_mode: str | None = None, profile_path: str | None = None) -> dict[str, Any]:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise BalePluginError("unknown_error", "Playwright is not installed or not importable") from exc

        browser_path = resolve_system_browser_executable()
        self.browser_manager.last_browser_path = browser_path
        if not browser_path:
            raise BalePluginError("browser_start_timeout", "No system Chrome/Edge found")

        profile_dir = Path(profile_path) if profile_path else _native_profile_dir(account_id)
        profile_dir.mkdir(parents=True, exist_ok=True)
        playwright = sync_playwright().start()
        context = None
        try:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                executable_path=browser_path,
                headless=False,
                args=[],
            )
            page = context.pages[0] if context.pages else context.new_page()
            return {
                "playwright": playwright,
                "context": context,
                "page": page,
                "profile_path": str(profile_dir),
                "provider_mode": provider_mode or "native_chrome",
                "browser_path": browser_path,
            }
        except Exception:
            if context is not None:
                context.close()
            playwright.stop()
            raise

    def close_reusable_runtime_session(self, runtime_session: Any) -> dict[str, Any]:
        metadata = getattr(runtime_session, "metadata", {}) or {}
        context = getattr(runtime_session, "context", None)
        playwright = metadata.get("playwright") if isinstance(metadata, dict) else None
        try:
            if context is not None:
                context.close()
            if playwright is not None:
                playwright.stop()
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error_code": "session_close_failed", "message": str(exc)}

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
                "browser_path": getattr(self.browser_manager, "last_browser_path", None),
            }
        account = bale_account_store.get_account(account_id) or {}
        return {
            "provider_mode": provider_mode,
            "browser_reused": True,
            "profile_dir": str(account.get("user_data_dir") or ""),
            "browser_path": getattr(self.browser_manager, "last_browser_path", None),
        }

    def _detect_login_state(self, page: Any, timeout_ms: int = 3000) -> dict[str, Any]:
        _safe_wait_for_timeout(page, min(750, max(0, timeout_ms // 10)))
        current_url = _safe_page_url(page)
        normalized_url = current_url.lower()
        visible_text_sample = _visible_text_sample(page)
        install_prompt = self._first_visible_selector(page, _INSTALL_PROMPT_SELECTORS, timeout_ms=750)
        login_form = self._first_visible_selector(page, _CLEAR_LOGIN_FORM_SELECTORS, timeout_ms=750)

        dialog_selector = self._first_visible_selector(page, selectors.CHAT_ITEM_SELECTORS, timeout_ms=750)
        message_selector = self._first_visible_selector(page, selectors.MESSAGE_INPUT_SELECTORS, timeout_ms=750)
        search_icon_selector = self._first_visible_selector(page, selectors.SEARCH_ICON_SELECTORS, timeout_ms=750)
        chat_list_selector = self._first_visible_selector(page, _CHAT_LIST_SELECTORS, timeout_ms=750)
        tabs_selector = self._first_visible_selector(page, _BALE_CHAT_TAB_SELECTORS, timeout_ms=750)
        side_menu_selector = self._first_visible_selector(page, _BALE_SIDE_MENU_SELECTORS, timeout_ms=750)
        matched_selector = self._first_visible_selector(
            page,
            _CHAT_UI_SELECTORS,
            timeout_ms=min(timeout_ms, 1500),
        )
        search_selector = self._first_visible_selector(page, selectors.SEARCH_INPUT_SELECTORS, timeout_ms=750)
        url_chat_detected = "/chat" in normalized_url
        login_form_visible = bool(login_form)
        chat_list_visible = bool(chat_list_selector or dialog_selector)
        search_icon_visible = bool(search_icon_selector)
        tabs_visible = bool(tabs_selector)
        side_menu_visible = bool(side_menu_selector)
        logged_in_ui_detected = any(
            [
                chat_list_visible,
                search_icon_visible,
                tabs_visible,
                side_menu_visible,
                bool(message_selector),
            ]
        )
        logged_in = bool(logged_in_ui_detected and not install_prompt)
        login_page_detected = bool(("/login" in normalized_url or login_form_visible) and not logged_in_ui_detected)
        if install_prompt:
            error_code = "bale_install_prompt"
            detector_reason = "install_prompt_visible"
        elif logged_in:
            error_code = None
            detector_reason = "authenticated_app_shell_visible"
        elif login_page_detected:
            error_code = "not_logged_in"
            detector_reason = "clear_login_ui_visible"
        else:
            error_code = "login_state_unknown"
            detector_reason = "no_clear_login_or_app_shell_signal"

        return {
            "logged_in": logged_in,
            "current_url": current_url,
            "matched_selector": matched_selector or chat_list_selector or dialog_selector or message_selector or search_icon_selector or tabs_selector or side_menu_selector or search_selector or ("url:/chat" if url_chat_detected else ""),
            "error_code": error_code,
            "login_page_detected": login_page_detected,
            "install_prompt_detected": bool(install_prompt),
            "logged_in_ui_detected": bool(logged_in_ui_detected),
            "chat_ui_detected": bool(logged_in_ui_detected),
            "dialog_items_detected": bool(dialog_selector),
            "chat_list_visible": chat_list_visible,
            "search_input_detected": bool(search_selector),
            "search_icon_detected": search_icon_visible,
            "search_icon_visible": search_icon_visible,
            "message_input_detected": bool(message_selector),
            "tabs_visible": tabs_visible,
            "side_menu_visible": side_menu_visible,
            "login_form_visible": login_form_visible,
            "login_form_selector": login_form or "",
            "install_prompt_selector": install_prompt or "",
            "visible_text_sample": visible_text_sample,
            "login_detector_reason": detector_reason,
        }

    def classify_authentication_state(self, page: Any, timeout_ms: int = 3000) -> dict[str, Any]:
        login = self._detect_login_state(page, timeout_ms=timeout_ms)
        visible_text = _visible_text_sample(page, limit=1200)
        text_lower = visible_text.lower()
        page_url = _safe_page_url(page)
        normalized_url = page_url.lower()
        install_prompt = bool(login.get("install_prompt_detected"))
        contacts_ui_available = bool(self._contacts_ui_visible(page))
        loading_visible = bool(self._first_visible_selector(page, ['[aria-label="Loading-icon"]', '[role="progressbar"]', '[class*="loading" i]', '[class*="spinner" i]'], timeout_ms=500))
        reconnect_visible = any(token in text_lower for token in ["offline", "reconnect", "connecting", "connection", "disconnected"]) or "Ã˜Â¯Ã˜Â±Ã˜Â­Ã˜Â§Ã™â€ž Ã˜Â§Ã˜ÂªÃ˜ÂµÃ˜Â§Ã™â€ž" in visible_text
        strong_chat_evidence = bool(
            login.get("chat_list_visible")
            or login.get("message_input_detected")
            or login.get("search_input_detected")
            or login.get("search_icon_visible")
            or contacts_ui_available
        )
        login_url_visible = "/login" in normalized_url
        login_ui_visible = bool(login.get("login_page_detected") or login.get("login_form_visible") or (login_url_visible and not strong_chat_evidence))
        chat_shell_visible = bool(strong_chat_evidence and not install_prompt and not login_ui_visible)
        evidence: list[str] = []
        if install_prompt:
            evidence.append("install_help_prompt_visible")
        if login_ui_visible:
            evidence.append("login_ui_visible")
        if chat_shell_visible:
            evidence.append("chat_shell_visible")
        if contacts_ui_available:
            evidence.append("contacts_ui_available")
        if loading_visible:
            evidence.append("loading_visible")
        if reconnect_visible:
            evidence.append("offline_or_reconnecting_visible")

        if "qr" in text_lower or "Ã˜Â¨Ã˜Â§Ã˜Â±ÃšÂ©Ã˜Â¯" in visible_text or "ÃšÂ©Ã›Å’Ã™Ë†Ã˜Â¢Ã˜Â±" in visible_text:
            auth_state = "unauthenticated"
            legacy_auth_state = "qr_login_required"
            error_code = "authentication_required"
        elif "ÃšÂ©Ã˜Â¯" in visible_text and ("Ã˜ÂªÃ˜Â§Ã›Å’Ã›Å’Ã˜Â¯" in visible_text or "Ã˜ÂªÃ˜Â£Ã›Å’Ã›Å’Ã˜Â¯" in visible_text or "verification" in text_lower):
            auth_state = "unauthenticated"
            legacy_auth_state = "verification_code_required"
            error_code = "authentication_required"
        elif any(token in text_lower for token in ["restricted", "blocked", "suspended"]) or any(token in visible_text for token in ["Ã™â€¦Ã˜Â³Ã˜Â¯Ã™Ë†Ã˜Â¯", "Ã™â€¦Ã˜Â­Ã˜Â¯Ã™Ë†Ã˜Â¯"]):
            auth_state = "auth_unverified"
            legacy_auth_state = "account_restricted"
            error_code = "account_restricted"
        elif install_prompt:
            auth_state = "auth_unverified"
            legacy_auth_state = "install_help_prompt"
            error_code = "auth_unverified"
        elif loading_visible or reconnect_visible:
            auth_state = "auth_unverified"
            legacy_auth_state = "loading"
            error_code = "auth_unverified"
        elif chat_shell_visible:
            auth_state = "authenticated"
            legacy_auth_state = "authenticated"
            error_code = None
        elif login_ui_visible:
            auth_state = "unauthenticated"
            legacy_auth_state = "login_required"
            error_code = "authentication_required"
        elif not page_url or "loading" in text_lower:
            auth_state = "auth_unverified"
            legacy_auth_state = "loading"
            error_code = "auth_unverified"
        else:
            auth_state = "auth_unverified"
            legacy_auth_state = "unknown_auth_state"
            error_code = "auth_unverified"

        authenticated = auth_state == "authenticated"
        return {
            "auth_state": auth_state,
            "legacy_auth_state": legacy_auth_state,
            "authenticated": authenticated,
            "login_ui_visible": login_ui_visible,
            "chat_shell_visible": chat_shell_visible,
            "contacts_ui_available": contacts_ui_available,
            "loading_visible": loading_visible,
            "offline_or_reconnecting_visible": reconnect_visible,
            "page_url": page_url,
            "error_code": error_code,
            "detection_evidence": evidence,
            "diagnostics_consistent": bool(
                (authenticated and chat_shell_visible and not login_ui_visible)
                or (not authenticated and auth_state != "authenticated")
            ),
            "login_check": {
                key: login.get(key)
                for key in [
                    "matched_selector",
                    "error_code",
                    "login_detector_reason",
                    "install_prompt_detected",
                    "logged_in_ui_detected",
                    "login_page_detected",
                    "chat_list_visible",
                    "search_icon_visible",
                    "tabs_visible",
                    "side_menu_visible",
                ]
            },
        }

    def _extract_latest_channel_message_preview(self, page: Any) -> dict[str, Any]:
        script = """
        () => {
          const selectors = [
            "div[data-testid*='message']",
            "div[class*='Message']",
            "div[class*='message']",
            "article",
            "[role='listitem']"
          ];
          const seen = new Set();
          const candidates = [];
          const rejectText = (text) => {
            const normalized = String(text || "").replace(/\\s+/g, " ").trim();
            if (!normalized) return false;
            const lower = normalized.toLowerCase();
            const uiWords = ["search", "contacts", "settings", "install", "forward", "reply"];
            if (uiWords.some((word) => lower === word)) return true;
            if (/^\\d{1,2}:\\d{2}$/.test(normalized)) return true;
            if (/^(today|yesterday)$/i.test(normalized)) return true;
            return false;
          };
          for (const selector of selectors) {
            for (const node of Array.from(document.querySelectorAll(selector))) {
              if (seen.has(node)) continue;
              seen.add(node);
              const rect = node.getBoundingClientRect();
              const style = window.getComputedStyle(node);
              if (!rect || rect.width < 40 || rect.height < 20) continue;
              if (style.visibility === "hidden" || style.display === "none" || Number(style.opacity) === 0) continue;
              if (rect.bottom < 0 || rect.top > window.innerHeight || rect.right < 0 || rect.left > window.innerWidth) continue;
              const text = String(node.innerText || node.textContent || "").replace(/\\s+/g, " ").trim();
              const hasImage = Boolean(node.querySelector("img, [class*='image'], [class*='photo']"));
              const hasVideo = Boolean(node.querySelector("video, [class*='video']"));
              const hasFile = Boolean(node.querySelector("a[download], [class*='file'], [class*='document']"));
              if (!text && !hasImage && !hasVideo && !hasFile) continue;
              if (rejectText(text)) continue;
              const timestampNode = node.querySelector("time, [datetime], [class*='time'], [class*='Time']");
              candidates.push({
                selector,
                text,
                hasImage,
                hasVideo,
                hasFile,
                id: node.id || null,
                timestampText: timestampNode ? String(timestampNode.innerText || timestampNode.textContent || timestampNode.getAttribute("datetime") || "").trim() : null,
                top: rect.top,
                bottom: rect.bottom,
              });
            }
          }
          candidates.sort((a, b) => a.bottom - b.bottom);
          const latest = candidates[candidates.length - 1] || null;
          return {
            marker: "clinicos_bale_channel_preview",
            channel_view_visible: candidates.length > 0 || /bale\\.ai/i.test(location.href),
            message_selector_used: latest ? latest.selector : selectors.join(", "),
            latest_message_visible: Boolean(latest),
            candidate_count: candidates.length,
            latest,
          };
        }
        """
        raw = page.evaluate(script)
        if not isinstance(raw, dict):
            raw = {}
        latest = raw.get("latest") if isinstance(raw.get("latest"), dict) else None
        text_preview = str(latest.get("text") or "")[:1000] if latest else ""
        return {
            "message_found": bool(latest),
            "text_preview": text_preview,
            "has_text": bool(text_preview),
            "has_image": bool(latest and latest.get("hasImage")),
            "has_video": bool(latest and latest.get("hasVideo")),
            "has_file": bool(latest and latest.get("hasFile")),
            "message_dom_id": latest.get("id") if latest else None,
            "message_timestamp_text": latest.get("timestampText") if latest else None,
            "candidate_count": int(raw.get("candidate_count") or 0),
            "channel_view_visible": bool(raw.get("channel_view_visible")),
            "message_selector_used": str(raw.get("message_selector_used") or ""),
            "latest_message_visible": bool(raw.get("latest_message_visible")),
        }

    def _source_channel_readiness(self, page: Any) -> dict[str, Any]:
        script = """
        () => {
          const textOf = (node) => String((node && (node.innerText || node.textContent)) || "").replace(/\\s+/g, " ").trim();
          const visible = (node) => {
            if (!node) return false;
            const rect = node.getBoundingClientRect();
            const style = window.getComputedStyle(node);
            return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none" && Number(style.opacity) !== 0;
          };
          const boxOf = (node) => {
            const rect = node.getBoundingClientRect();
            return {x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height)};
          };
          const selectorOf = (node, fallback) => {
            if (!node) return "";
            if (node.id) return `#${CSS.escape(node.id)}`;
            for (const attr of ["data-testid", "role", "aria-label"]) {
              const value = node.getAttribute(attr);
              if (value) return `${node.tagName.toLowerCase()}[${attr}="${value.replace(/"/g, "\\\\\\"")}"]`;
            }
            return fallback;
          };
          const panelSelectors = [
            ".main-section-container",
            "#message_list_scroller_id",
            "[data-testid*='conversation']",
            "[data-testid*='chat-panel']",
            "[class*='ChatPanel']",
            "[class*='Conversation']",
            "main",
            "[role='main']"
          ];
          const panelCandidates = [];
          for (const selector of panelSelectors) {
            for (const node of Array.from(document.querySelectorAll(selector))) {
              if (!visible(node)) continue;
              const box = boxOf(node);
              const sidebarLike = node.id === "sidebar_wrapper" || box.x > window.innerWidth * 0.58 || box.w < 500;
              const conversationLike = box.w > Math.max(500, window.innerWidth * 0.45) && box.h > 280;
              if (!conversationLike || sidebarLike) continue;
              const panelNode = node.id === "message_list_scroller_id" ? (node.closest(".main-section-container") || node.parentElement || node) : node;
              panelCandidates.push({node: panelNode, selector, box: boxOf(panelNode), text: textOf(panelNode)});
            }
          }
          panelCandidates.sort((a, b) => (b.box.w * b.box.h) - (a.box.w * a.box.h));
          const panel = panelCandidates[0] || null;
          const panelNode = panel ? panel.node : null;
          const headerSelectors = [
            "[aria-label='ChatAppBar']",
            "[data-testid*='chat-header']",
            "[data-testid*='conversation-header']",
            "[class*='ChatAppBar']",
            "[class*='Header']",
            "header",
            "[role='heading']"
          ];
          let header = null;
          let headerSelector = "";
          if (panelNode) {
            for (const selector of headerSelectors) {
              header = Array.from(panelNode.querySelectorAll(selector)).find(visible);
              if (header) {
                headerSelector = selectorOf(header, selector);
                break;
              }
            }
          }
          const messageSelector = '[aria-label="message-item"], .message-item, ._message-item, .message-block, [data-testid*="message"]';
          if (!panelNode) {
            const messageLike = Array.from(document.querySelectorAll(messageSelector)).filter(visible);
            if (messageLike.length) {
              const message = messageLike[messageLike.length - 1];
              const parent = message.closest(".main-section-container, [class*='Conversation'], [class*='ChatPanel'], [role='main']") || message.parentElement;
              if (parent && visible(parent)) {
                const box = boxOf(parent);
                const sidebarLike = parent.id === "sidebar_wrapper" || box.x > window.innerWidth * 0.58 || box.w < 300;
                if (!sidebarLike) {
                  panelCandidates.push({node: parent, selector: "message-timeline-parent", box, text: textOf(parent)});
                }
              }
            }
          }
          const effectivePanel = panelNode || (panelCandidates[0] ? panelCandidates[0].node : null);
          const streamSelectors = [
            "#message_list_scroller_id",
            "[data-testid*='message-list']",
            "[data-testid*='messages']",
            "[class*='MessageList']",
            "[class*='message-list']",
            "[class*='Messages']",
            "[role='list']"
          ];
          let stream = null;
          let streamSelector = "";
          if (effectivePanel) {
            for (const selector of streamSelectors) {
              const nodes = Array.from(effectivePanel.querySelectorAll(selector)).filter((node) => {
                if (!visible(node)) return false;
                const box = boxOf(node);
                return box.h > 120 && box.w > 250;
              });
              if (nodes.length) {
                stream = nodes[nodes.length - 1];
                streamSelector = selectorOf(stream, selector);
                break;
              }
            }
            if (!stream) {
              const messageLike = Array.from(effectivePanel.querySelectorAll(messageSelector)).filter(visible);
              if (messageLike.length >= 1) {
                stream = messageLike[messageLike.length - 1].parentElement || effectivePanel;
                streamSelector = selectorOf(stream, "message-parent");
              }
            }
          }
          const headerText = textOf(header).slice(0, 500);
          const panelText = textOf(effectivePanel).slice(0, 1000);
          const messageCount = effectivePanel ? Array.from(effectivePanel.querySelectorAll(messageSelector)).filter(visible).length : 0;
          const pageText = textOf(document.body).slice(0, 1000);
          const authVisible = /ÙˆØ±ÙˆØ¯|login|log in|phone|Ø´Ù…Ø§Ø±Ù‡|Ú©Ø¯ ØªØ§ÛŒÛŒØ¯|otp/i.test(pageText);
          const loadingVisible = /loading|Ø¯Ø± Ø­Ø§Ù„|Ù„Ø·ÙØ§ ØµØ¨Ø±|please wait/i.test(pageText);
          const emptyVisible = /empty|Ù¾ÛŒØ§Ù…ÛŒ|Ù‡Ù†ÙˆØ²/i.test(pageText) && messageCount === 0;
          const ready = Boolean(effectivePanel && stream && messageCount > 0 && !authVisible);
          return {
            marker: "clinicos_bale_source_channel_readiness",
            ready,
            target_channel_panel_visible: Boolean(effectivePanel),
            target_channel_panel_selector: effectivePanel ? selectorOf(effectivePanel, panel ? panel.selector : "message-timeline-parent") : "",
            target_channel_header_text: headerText,
            target_channel_header_selector: headerSelector,
            message_stream_visible: Boolean(stream),
            message_stream_selector: streamSelector,
            message_count: messageCount,
            authentication_view_visible: authVisible,
            loading_indicator_visible: loadingVisible,
            empty_state_visible: emptyVisible,
            center_panel_visible_text_sample: panelText,
          };
        }
        """
        try:
            raw = page.evaluate(script)
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        return {
            "ready": bool(raw.get("ready")),
            "target_channel_panel_visible": bool(raw.get("target_channel_panel_visible")),
            "target_channel_panel_selector": str(raw.get("target_channel_panel_selector") or ""),
            "target_channel_header_text": str(raw.get("target_channel_header_text") or ""),
            "target_channel_header_selector": str(raw.get("target_channel_header_selector") or ""),
            "message_stream_visible": bool(raw.get("message_stream_visible")),
            "message_stream_selector": str(raw.get("message_stream_selector") or ""),
            "message_count": int(raw.get("message_count") or 0),
            "authentication_view_visible": bool(raw.get("authentication_view_visible")),
            "loading_indicator_visible": bool(raw.get("loading_indicator_visible")),
            "empty_state_visible": bool(raw.get("empty_state_visible")),
            "center_panel_visible_text_sample": str(raw.get("center_panel_visible_text_sample") or ""),
        }

    def _locate_latest_channel_message_in_stream(self, page: Any) -> dict[str, Any]:
        script = """
        () => {
          const streamSelectorCandidates = ["#message_list_scroller_id", "[data-testid*='message-list']", "[data-testid*='messages']", "[class*='MessageList']", "[class*='message-list']", "[class*='Messages']", "[role='list']"];
          const messageSelector = '[aria-label="message-item"], .message-item, ._message-item, .message-block';
          const textOf = (node) => String((node && (node.innerText || node.textContent)) || "").replace(/\\s+/g, " ").trim();
          const visible = (node) => {
            if (!node) return false;
            const rect = node.getBoundingClientRect();
            const style = window.getComputedStyle(node);
            return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none" && Number(style.opacity) !== 0;
          };
          let stream = null;
          let streamSelector = "";
          for (const selector of streamSelectorCandidates) {
            const nodes = Array.from(document.querySelectorAll(selector)).filter(visible);
            if (nodes.length) {
              stream = nodes[nodes.length - 1];
              streamSelector = selector;
              break;
            }
          }
          if (!stream) {
            const messageLike = Array.from(document.querySelectorAll(messageSelector)).filter(visible);
            if (messageLike.length) {
              stream = messageLike[messageLike.length - 1].parentElement || document.body;
              streamSelector = "message-parent";
            }
          }
          const boxOf = (node) => {
            const rect = node.getBoundingClientRect();
            return {x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height)};
          };
          const selectorOf = (node, fallback) => {
            if (!node) return "";
            if (node.id) return `#${CSS.escape(node.id)}`;
            const aria = node.getAttribute("aria-label");
            if (aria) return `${node.tagName.toLowerCase()}[aria-label="${aria.replace(/"/g, "\\\\\\"")}"]`;
            const cls = String(node.className || "").split(/\\s+/).filter(Boolean)[0];
            return cls ? `${node.tagName.toLowerCase()}.${CSS.escape(cls)}` : fallback;
          };
          const isDateRow = (text, node) => {
            const cls = String(node.className || "");
            if (/Wqgb2D|date|Date/i.test(cls)) return true;
            if (/^(Ø§Ù…Ø±ÙˆØ²|Ø¯ÛŒØ±ÙˆØ²|Ù¾Ø±ÛŒØ±ÙˆØ²)$/.test(text)) return true;
            if (/^\\d{1,2}\\s+\\S+$/.test(text) && text.length < 32) return true;
            return false;
          };
          const isServiceRow = (text, node) => {
            const cls = String(node.className || "");
            if (/service|system/i.test(cls)) return true;
            const serviceMarkers = ["Ø¹Ø¶Ùˆ Ø´Ø¯", "Ø®Ø§Ø±Ø¬ Ø´Ø¯", "Ù¾ÛŒØ§Ù… Ø³Ù†Ø¬Ø§Ù‚", "created", "joined", "left", "pinned"];
            return serviceMarkers.some((marker) => text.toLowerCase().includes(marker.toLowerCase())) && text.length < 180;
          };
          const debug = [];
          if (!stream) {
            return {marker: "clinicos_bale_latest_channel_message", message_found: false, candidate_count: 0, message_selector_used: streamSelector, candidate_debug: [{status: "rejected", reason: "message_stream_missing", selector: streamSelector}]};
          }
          const rawNodes = Array.from(stream.querySelectorAll(messageSelector));
          const accepted = [];
          rawNodes.forEach((node, index) => {
            const text = textOf(node);
            const hasImage = Boolean(node.querySelector("img"));
            const hasVideo = Boolean(node.querySelector("video"));
            const hasFile = Boolean(node.querySelector("a[download], [class*='file'], [class*='document'], [class*='File'], [class*='Document']"));
            const visibleNode = visible(node);
            let reason = "";
            if (!stream.contains(node)) reason = "outside_message_stream";
            else if (!visibleNode && !text && !hasImage && !hasVideo && !hasFile) reason = "empty_or_invisible";
            else if (!text && !hasImage && !hasVideo && !hasFile) reason = "empty_container";
            else if (isDateRow(text, node)) reason = "date_row";
            else if (isServiceRow(text, node)) reason = "service_row";
            const item = {
              index,
              selector: selectorOf(node, messageSelector),
              text: text.slice(0, 300),
              hasImage,
              hasVideo,
              hasFile,
              visible: visibleNode,
              box: visibleNode ? boxOf(node) : null,
              status: reason ? "rejected" : "accepted",
              reason,
              domId: node.id || null,
              timestampText: ""
            };
            const timeNode = node.querySelector("time, [datetime], [class*='time'], [class*='Time']");
            if (timeNode) item.timestampText = String(timeNode.innerText || timeNode.textContent || timeNode.getAttribute("datetime") || "").trim();
            debug.push(item);
            if (!reason) accepted.push({node, item});
          });
          const latest = accepted.length ? accepted[accepted.length - 1] : null;
          const latestItem = latest ? latest.item : null;
          return {
            marker: "clinicos_bale_latest_channel_message",
            message_found: Boolean(latestItem),
            candidate_count: accepted.length,
            message_selector_used: messageSelector,
            candidate_debug: debug.slice(-30),
            text_preview: latestItem ? latestItem.text : "",
            has_text: Boolean(latestItem && latestItem.text),
            has_image: Boolean(latestItem && latestItem.hasImage),
            has_video: Boolean(latestItem && latestItem.hasVideo),
            has_file: Boolean(latestItem && latestItem.hasFile),
            message_dom_id: latestItem ? latestItem.domId : null,
            message_timestamp_text: latestItem ? latestItem.timestampText || null : null
          };
        }
        """
        try:
            raw = page.evaluate(script)
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        return {
            "message_found": bool(raw.get("message_found")),
            "candidate_count": int(raw.get("candidate_count") or 0),
            "message_selector_used": str(raw.get("message_selector_used") or ""),
            "candidate_debug": raw.get("candidate_debug") if isinstance(raw.get("candidate_debug"), list) else [],
            "text_preview": str(raw.get("text_preview") or ""),
            "has_text": bool(raw.get("has_text")),
            "has_image": bool(raw.get("has_image")),
            "has_video": bool(raw.get("has_video")),
            "has_file": bool(raw.get("has_file")),
            "message_dom_id": raw.get("message_dom_id"),
            "message_timestamp_text": raw.get("message_timestamp_text"),
        }

    def _resolve_latest_forward_message_target(self, page: Any) -> dict[str, Any]:
        script = """
        () => {
          const streamSelectorCandidates = ["#message_list_scroller_id", "[data-testid*='message-list']", "[data-testid*='messages']", "[class*='MessageList']", "[class*='message-list']", "[class*='Messages']", "[role='list']"];
          const messageSelector = '[aria-label="message-item"], .message-item, ._message-item, .message-block';
          const textOf = (node) => String((node && (node.innerText || node.textContent)) || "").replace(/\\s+/g, " ").trim();
          const visible = (node) => {
            if (!node) return false;
            const rect = node.getBoundingClientRect();
            const style = window.getComputedStyle(node);
            return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none" && Number(style.opacity) !== 0;
          };
          let stream = null;
          let streamSelector = "";
          for (const selector of streamSelectorCandidates) {
            const nodes = Array.from(document.querySelectorAll(selector)).filter(visible);
            if (nodes.length) {
              stream = nodes[nodes.length - 1];
              streamSelector = selector;
              break;
            }
          }
          if (!stream) {
            const messageLike = Array.from(document.querySelectorAll(messageSelector)).filter(visible);
            if (messageLike.length) {
              stream = messageLike[messageLike.length - 1].parentElement || document.body;
              streamSelector = "message-parent";
            }
          }
          const boxOf = (node) => {
            const rect = node.getBoundingClientRect();
            return {x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height)};
          };
          const isDateRow = (text, node) => /date|Date/i.test(String(node.className || "")) || (/^\\d{1,2}\\s+\\S+$/.test(text) && text.length < 32);
          const isServiceRow = (text, node) => {
            const cls = String(node.className || "");
            if (/service|system/i.test(cls)) return true;
            return ["created", "joined", "left", "pinned"].some((marker) => text.toLowerCase().includes(marker)) && text.length < 180;
          };
          const debug = [];
          if (!stream) {
            return {marker: "clinicos_bale_forward_latest_message_target", message_found: false, candidate_count: 0, message_selector_used: streamSelector, candidate_debug: [{status: "rejected", reason: "message_stream_missing", selector: streamSelector}]};
          }
          stream.querySelectorAll("[data-clinicos-latest-channel-message]").forEach((node) => node.removeAttribute("data-clinicos-latest-channel-message"));
          const rawNodes = Array.from(stream.querySelectorAll(messageSelector));
          const accepted = [];
          rawNodes.forEach((node, index) => {
            const text = textOf(node);
            const hasImage = Boolean(node.querySelector("img"));
            const hasVideo = Boolean(node.querySelector("video"));
            const hasFile = Boolean(node.querySelector("a[download], [class*='file'], [class*='document'], [class*='File'], [class*='Document']"));
            const html = String(node.outerHTML || "");
            const isLocalDraftOrUpload = node.getAttribute("data-date") === "0"
              || Boolean(node.querySelector('[data-testid*="upload"], [data-test-id*="upload"], [class*="upload"], [class*="Upload"], [data-testid*="loading"], [data-test-id*="loading"]'))
              || /loading-wrapper-uploading|CancelableLoading|uploading/i.test(html);
            let reason = "";
            const visibleNode = visible(node);
            if (!stream.contains(node)) reason = "outside_message_stream";
            else if (isLocalDraftOrUpload) reason = "stale_or_uploading_message_panel";
            else if (!visibleNode && !text && !hasImage && !hasVideo && !hasFile) reason = "empty_or_invisible";
            else if (!text && !hasImage && !hasVideo && !hasFile) reason = "empty_container";
            else if (isDateRow(text, node)) reason = "date_row";
            else if (isServiceRow(text, node)) reason = "service_row";
            const item = {
              index,
              text: text.slice(0, 300),
              hasImage,
              hasVideo,
              hasFile,
              visible: visibleNode,
              box: visibleNode ? boxOf(node) : null,
              status: reason ? "rejected" : "accepted",
              reason,
              domId: node.id || null,
              dataDate: node.getAttribute("data-date") || "",
              className: String(node.className || "").slice(0, 200),
              htmlSummary: html.replace(/\\s+/g, " ").slice(0, 1200),
              timestampText: ""
            };
            const timeNode = node.querySelector("time, [datetime], [class*='time'], [class*='Time']");
            if (timeNode) item.timestampText = String(timeNode.innerText || timeNode.textContent || timeNode.getAttribute("datetime") || "").trim();
            debug.push(item);
            if (!reason) accepted.push({node, item});
          });
          const latest = accepted.length ? accepted[accepted.length - 1] : null;
          if (!latest) {
            return {marker: "clinicos_bale_forward_latest_message_target", message_found: false, candidate_count: 0, message_selector_used: messageSelector, candidate_debug: debug.slice(-30)};
          }
          latest.node.setAttribute("data-clinicos-latest-channel-message", "true");
          const selector = `${streamSelector} [data-clinicos-latest-channel-message="true"]`;
          return {
            marker: "clinicos_bale_forward_latest_message_target",
            message_found: true,
            candidate_count: accepted.length,
            message_selector_used: messageSelector,
            latest_message_selector: selector,
            latest_message_text_preview: latest.item.text,
            latest_message_data_date: latest.item.dataDate || null,
            latest_message_signature: `${latest.item.dataDate || ""}|${latest.item.domId || ""}|${latest.item.text || ""}`.slice(0, 500),
            latest_message_html_summary: latest.item.htmlSummary,
            has_text: Boolean(latest.item.text),
            has_image: Boolean(latest.item.hasImage),
            has_video: Boolean(latest.item.hasVideo),
            has_file: Boolean(latest.item.hasFile),
            message_dom_id: latest.item.domId,
            message_timestamp_text: latest.item.timestampText || null,
            candidate_debug: debug.slice(-30)
          };
        }
        """
        try:
            raw = page.evaluate(script)
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        return {
            "message_found": bool(raw.get("message_found")),
            "candidate_count": int(raw.get("candidate_count") or 0),
            "message_selector_used": str(raw.get("message_selector_used") or ""),
            "latest_message_selector": str(raw.get("latest_message_selector") or ""),
            "latest_message_text_preview": str(raw.get("latest_message_text_preview") or ""),
            "latest_message_data_date": raw.get("latest_message_data_date"),
            "latest_message_signature": str(raw.get("latest_message_signature") or ""),
            "latest_message_html_summary": str(raw.get("latest_message_html_summary") or ""),
            "has_text": bool(raw.get("has_text")),
            "has_image": bool(raw.get("has_image")),
            "has_video": bool(raw.get("has_video")),
            "has_file": bool(raw.get("has_file")),
            "message_dom_id": raw.get("message_dom_id"),
            "message_timestamp_text": raw.get("message_timestamp_text"),
            "candidate_debug": raw.get("candidate_debug") if isinstance(raw.get("candidate_debug"), list) else [],
        }

    def _message_forward_menu_candidates(self, page: Any, latest_message_selector: str) -> dict[str, Any]:
        script = """
        (latestSelector) => {
          const latest = document.querySelector(latestSelector);
          const textOf = (node) => String((node && (node.innerText || node.textContent)) || "").replace(/\\s+/g, " ").trim();
          const visible = (node) => {
            if (!node) return false;
            const rect = node.getBoundingClientRect();
            const style = window.getComputedStyle(node);
            return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none" && Number(style.opacity) !== 0;
          };
          const boxOf = (node) => {
            const rect = node.getBoundingClientRect();
            return {x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height)};
          };
          const attempted = [
            '[data-testid="message-side-option-forward"]',
            '[data-testid*="message-side-option-forward"]',
            '[data-testid*="forward"]',
            '[aria-label="Ø¨ÛŒØ´ØªØ±"]',
            '[aria-label*="Ø¨ÛŒØ´ØªØ±"]',
            '[aria-label*="More"]',
            '[title*="Forward"]',
            '[title*="Ø¨ÛŒØ´ØªØ±"]',
            '[role="button"]',
            'button',
            'svg'
          ];
          const debug = [];
          if (!latest) {
            return {marker: "clinicos_bale_message_menu_candidates", message_menu_selector: "", attempted_selectors: attempted, candidate_debug: [{status: "rejected", reason: "latest_message_missing", latestSelector}]};
          }
          latest.querySelectorAll("[data-clinicos-message-menu-candidate]").forEach((node) => node.removeAttribute("data-clinicos-message-menu-candidate"));
          const roots = [latest, latest.parentElement, latest.closest('[aria-label="message-item"]'), latest.closest('.message-item')].filter(Boolean);
          const seen = new Set();
          let selected = null;
          for (const selector of attempted) {
            for (const root of roots) {
              for (const node of Array.from(root.querySelectorAll(selector))) {
                if (seen.has(node)) continue;
                seen.add(node);
                const aria = node.getAttribute("aria-label") || "";
                const title = node.getAttribute("title") || "";
                const role = node.getAttribute("role") || "";
                const testid = node.getAttribute("data-testid") || "";
                const text = textOf(node);
                const box = visible(node) ? boxOf(node) : null;
                const label = `${aria} ${title} ${role} ${testid} ${text}`.trim();
                const looksLikeMenu = /Ø¨ÛŒØ´ØªØ±|More|more|menu|Menu|options|Options|ellipsis|Forward/i.test(label) || node.tagName === "SVG";
                const item = {
                  selector,
                  text: text.slice(0, 120),
                  aria_label: aria,
                  title,
                  role,
                  data_testid: testid,
                  className: String(node.className || "").slice(0, 160),
                  visible: Boolean(box),
                  box,
                  status: looksLikeMenu && box ? "candidate" : "observed"
                };
                debug.push(item);
                if (!selected && looksLikeMenu && box) {
                  const isDirectForwardControl = /(^|\\b)message-side-option-forward(\\b|$)/i.test(testid);
                  const clickTarget = isDirectForwardControl ? node : (node.closest('button, [role="button"], [aria-label], [title]') || node);
                  clickTarget.setAttribute("data-clinicos-message-menu-candidate", "0");
                  selected = clickTarget;
                }
              }
            }
          }
          return {
            marker: "clinicos_bale_message_menu_candidates",
            message_menu_selector: selected ? '[data-clinicos-message-menu-candidate="0"]' : "",
            attempted_selectors: attempted,
            candidate_debug: debug.slice(0, 80)
          };
        }
        """
        try:
            raw = page.evaluate(script, latest_message_selector)
        except TypeError:
            try:
                raw = page.evaluate(script)
            except Exception:
                raw = {}
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        return {
            "message_menu_selector": str(raw.get("message_menu_selector") or ""),
            "attempted_selectors": raw.get("attempted_selectors") if isinstance(raw.get("attempted_selectors"), list) else [],
            "candidate_debug": raw.get("candidate_debug") if isinstance(raw.get("candidate_debug"), list) else [],
        }

    def _forward_option_candidates(self, page: Any) -> dict[str, Any]:
        script = """
        () => {
          const textOf = (node) => String((node && (node.innerText || node.textContent)) || "").replace(/\\s+/g, " ").trim();
          const visible = (node) => {
            if (!node) return false;
            const rect = node.getBoundingClientRect();
            const style = window.getComputedStyle(node);
            return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none" && Number(style.opacity) !== 0;
          };
          const boxOf = (node) => {
            const rect = node.getBoundingClientRect();
            return {x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height)};
          };
          document.querySelectorAll("[data-clinicos-forward-option]").forEach((node) => node.removeAttribute("data-clinicos-forward-option"));
          const nodes = Array.from(document.querySelectorAll('[role="menuitem"], [role="button"], button, li, div, span'));
          const labels = [/^Forward$/i, /ÙÙˆØ±ÙˆØ§Ø±Ø¯/, /Ø§Ø±Ø³Ø§Ù„\\s*Ø¨Ù‡/, /^Ø§Ø±Ø³Ø§Ù„$/];
          const debug = [];
          let selected = null;
          for (const node of nodes) {
            if (!visible(node)) continue;
            const text = textOf(node);
            const aria = node.getAttribute("aria-label") || "";
            const title = node.getAttribute("title") || "";
            const label = `${text} ${aria} ${title}`.trim();
            const isForward = labels.some((pattern) => pattern.test(label));
            if (!text && !aria && !title) continue;
            const item = {
              text: text.slice(0, 160),
              aria_label: aria,
              title,
              role: node.getAttribute("role") || "",
              className: String(node.className || "").slice(0, 160),
              box: boxOf(node),
              status: isForward ? "candidate" : "observed"
            };
            debug.push(item);
            if (!selected && isForward) {
              selected = node.closest('button, [role="menuitem"], [role="button"]') || node;
              selected.setAttribute("data-clinicos-forward-option", "0");
            }
          }
          const visibleMenuText = debug.map((item) => item.text || item.aria_label || item.title).filter(Boolean).slice(0, 20).join(" | ");
          return {
            marker: "clinicos_bale_forward_option_candidates",
            forward_option_selector: selected ? '[data-clinicos-forward-option="0"]' : "",
            visible_menu_text: visibleMenuText.slice(0, 1000),
            candidate_debug: debug.slice(0, 80)
          };
        }
        """
        try:
            raw = page.evaluate(script)
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        return {
            "forward_option_selector": str(raw.get("forward_option_selector") or ""),
            "visible_menu_text": str(raw.get("visible_menu_text") or ""),
            "candidate_debug": raw.get("candidate_debug") if isinstance(raw.get("candidate_debug"), list) else [],
        }

    def _forward_picker_state(self, page: Any) -> dict[str, Any]:
        script = """
        () => {
          const textOf = (node) => String((node && (node.innerText || node.textContent)) || "").replace(/\\s+/g, " ").trim();
          const visible = (node) => {
            if (!node) return false;
            const rect = node.getBoundingClientRect();
            const style = window.getComputedStyle(node);
            return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none" && Number(style.opacity) !== 0;
          };
          const selectorFor = (node, fallback) => {
            if (!node) return "";
            if (node.id) return `#${CSS.escape(node.id)}`;
            const role = node.getAttribute("role");
            if (role) return `[role="${role.replace(/"/g, "\\\\\\"")}"]`;
            const aria = node.getAttribute("aria-label");
            if (aria) return `[aria-label="${aria.replace(/"/g, "\\\\\\"")}"]`;
            const cls = String(node.className || "").split(/\\s+/).filter(Boolean)[0];
            return cls ? `${node.tagName.toLowerCase()}.${CSS.escape(cls)}` : fallback;
          };
          const pickerSelectors = [
            '[role="dialog"]',
            '[class*="Modal"]',
            '[class*="modal"]',
            '[class*="Forward"]',
            'input[type="search"]',
            'input[placeholder*="Search"]',
            'input[placeholder*="Ø¬Ø³Øª"]'
          ];
          const scopedPickerSelectors = ['div.anWA5J'];
          const debug = [];
          let selected = null;
          for (const selector of scopedPickerSelectors) {
            for (const node of Array.from(document.querySelectorAll(selector))) {
              if (!visible(node)) continue;
              const text = textOf(node);
              const search = Array.from(node.querySelectorAll('input[type="search"], input, [role="searchbox"], [role="textbox"], [contenteditable="true"]')).find(visible);
              if (!search) {
                debug.push({selector, text: text.slice(0, 200), placeholder: "", has_search_input: false, status: "observed"});
                continue;
              }
              const placeholder = search.getAttribute("placeholder") || "";
              const label = `${text} ${placeholder} ${node.getAttribute("aria-label") || ""}`;
              const pickerLike = /Forward|forward|ÙÙˆØ±ÙˆØ§Ø±Ø¯|Ø§Ø±Ø³Ø§Ù„|Search|search|Ø¬Ø³Øª|Ø§Ù†ØªØ®Ø§Ø¨/.test(label) || selector.includes("input");
              debug.push({selector, text: text.slice(0, 200), placeholder, status: pickerLike ? "candidate" : "observed"});
              if (!selected && pickerLike) selected = node.closest('[role="dialog"], [class*="Modal"], [class*="modal"]') || node;
            }
          }
          return {
            marker: "clinicos_bale_forward_picker_state",
            forward_picker_visible: Boolean(selected),
            forward_picker_selector: selected ? selectorFor(selected, '[role="dialog"]') : "",
            candidate_debug: debug.slice(0, 40)
          };
        }
        """
        try:
            raw = page.evaluate(script)
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        return {
            "forward_picker_visible": bool(raw.get("forward_picker_visible")),
            "forward_picker_selector": str(raw.get("forward_picker_selector") or ""),
            "candidate_debug": raw.get("candidate_debug") if isinstance(raw.get("candidate_debug"), list) else [],
        }

    def _forward_recipient_search_state(self, page: Any) -> dict[str, Any]:
        script = """
        () => {
          const visible = (node) => {
            if (!node) return false;
            const rect = node.getBoundingClientRect();
            const style = window.getComputedStyle(node);
            return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none" && Number(style.opacity) !== 0;
          };
          const selectorFor = (node, fallback) => {
            if (!node) return "";
            if (node.id) return `#${CSS.escape(node.id)}`;
            const marker = node.getAttribute("data-clinicos-recipient-search");
            if (marker) return `[data-clinicos-recipient-search="${marker}"]`;
            const aria = node.getAttribute("aria-label");
            if (aria) return `${node.tagName.toLowerCase()}[aria-label="${aria.replace(/"/g, "\\\\\\"")}"]`;
            const placeholder = node.getAttribute("placeholder");
            if (placeholder) return `${node.tagName.toLowerCase()}[placeholder="${placeholder.replace(/"/g, "\\\\\\"")}"]`;
            const role = node.getAttribute("role");
            if (role) return `${node.tagName.toLowerCase()}[role="${role}"]`;
            return fallback;
          };
          const picker = Array.from(document.querySelectorAll('div.anWA5J')).find(visible);
          const selectorAttempts = [
            'div.anWA5J input[type="search"]',
            'div.anWA5J input',
            'div.anWA5J [role="searchbox"]',
            'div.anWA5J [role="textbox"]',
            'div.anWA5J [contenteditable="true"]'
          ];
          const debug = [];
          let selected = null;
          for (const selector of selectorAttempts) {
            for (const node of Array.from(document.querySelectorAll(selector))) {
              if (!visible(node)) continue;
              if (picker && !picker.contains(node)) continue;
              const item = {
                selector,
                tag: node.tagName.toLowerCase(),
                role: node.getAttribute("role") || "",
                aria_label: node.getAttribute("aria-label") || "",
                placeholder: node.getAttribute("placeholder") || "",
                className: String(node.className || "").slice(0, 120),
                enabled: !node.disabled && node.getAttribute("aria-disabled") !== "true",
                editable: Boolean(node.isContentEditable || ("readOnly" in node ? !node.readOnly : true)),
                status: "candidate"
              };
              debug.push(item);
              if (!selected && item.enabled && item.editable) selected = node;
            }
          }
          if (selected) selected.setAttribute("data-clinicos-recipient-search", "0");
          return {
            marker: "clinicos_bale_forward_recipient_search_state",
            recipient_picker_visible: Boolean(picker),
            recipient_search_selector: selected ? selectorFor(selected, '[data-clinicos-recipient-search="0"]') : "",
            selector_attempts: selectorAttempts,
            candidate_debug: debug.slice(0, 40)
          };
        }
        """
        try:
            raw = page.evaluate(script)
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        return {
            "recipient_picker_visible": bool(raw.get("recipient_picker_visible")),
            "recipient_search_selector": str(raw.get("recipient_search_selector") or ""),
            "selector_attempts": raw.get("selector_attempts") if isinstance(raw.get("selector_attempts"), list) else [],
            "candidate_debug": raw.get("candidate_debug") if isinstance(raw.get("candidate_debug"), list) else [],
        }

    def _forward_picker_forensics(self, page: Any, display_name: str) -> dict[str, Any]:
        script = """
        (displayName) => {
          const normalize = (value) => String(value || "").replace(/\\s+/g, " ").trim();
          const excerpt = (node) => String((node && node.outerHTML) || "").replace(/\\s+/g, " ").slice(0, 800);
          const visible = (node) => {
            if (!node) return false;
            const rect = node.getBoundingClientRect();
            const style = window.getComputedStyle(node);
            return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none" && Number(style.opacity) !== 0;
          };
          const boxOf = (node) => {
            if (!node) return null;
            const rect = node.getBoundingClientRect();
            return {x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height)};
          };
          const selectorFor = (node, fallback) => {
            if (!node) return "";
            const marker = node.getAttribute("data-clinicos-forensic");
            if (marker) return `[data-clinicos-forensic="${marker}"]`;
            if (node.id) return `#${CSS.escape(node.id)}`;
            const role = node.getAttribute("role");
            if (role) return `${node.tagName.toLowerCase()}[role="${role.replace(/"/g, "\\\\\\"")}"]`;
            const aria = node.getAttribute("aria-label");
            if (aria) return `${node.tagName.toLowerCase()}[aria-label="${aria.replace(/"/g, "\\\\\\"")}"]`;
            const cls = String(node.className || "").split(/\\s+/).filter(Boolean)[0];
            return cls ? `${node.tagName.toLowerCase()}.${CSS.escape(cls)}` : fallback;
          };
          const chain = (node) => {
            const rows = [];
            let current = node;
            for (let depth = 0; current && depth < 6; depth += 1, current = current.parentElement) {
              rows.push({
                tag: current.tagName.toLowerCase(),
                role: current.getAttribute("role") || "",
                aria_label: current.getAttribute("aria-label") || "",
                classes: String(current.className || "").slice(0, 120),
                text: normalize(current.innerText || current.textContent || "").slice(0, 160),
                selector: selectorFor(current, "")
              });
            }
            return rows;
          };
          const validName = (text) => {
            const value = normalize(text);
            if (value.length < 3) return {ok: false, reason: "name_too_short"};
            if (/^[\\d\\u06F0-\\u06F9]+$/.test(value)) return {ok: false, reason: "numeric_only"};
            if (/^(avatar|icon|close|remove|delete|Ã—|x)$/i.test(value)) return {ok: false, reason: "icon_label"};
            return {ok: true, reason: ""};
          };
          const picker = Array.from(document.querySelectorAll('div.anWA5J')).find(visible);
          const bodyText = normalize(document.body ? document.body.innerText || document.body.textContent || "" : "");
          const successToast = /sent to|Forwarded|Ø§Ø±Ø³Ø§Ù„ Ø´Ø¯|Ø¨Ø§Ø²Ø§Ø±Ø³Ø§Ù„ Ø´Ø¯/i.test(bodyText);
          if (!picker) {
            return {picker_verified: false, picker_structure: {has_picker: false, has_search_input: false, has_list_or_empty_state: false, success_toast_visible: successToast}, selected_row_candidates: [], selected_chip_candidates: [], rejected_selected_candidates: []};
          }
          const modalRoot = picker.closest('[role="dialog"], [class*="Modal"], [class*="modal"]') || picker;
          modalRoot.setAttribute("data-clinicos-forensic", "modal-root");
          const pickerRect = picker.getBoundingClientRect();
          const searchInputs = Array.from(picker.querySelectorAll('input, textarea')).filter((node) => visible(node) && !node.disabled && !node.readOnly).map((node, index) => {
            node.setAttribute("data-clinicos-forensic", `search-${index}`);
            return {selector: selectorFor(node, ""), tag: node.tagName.toLowerCase(), placeholder: node.getAttribute("placeholder") || "", role: node.getAttribute("role") || "", enabled: !node.disabled, editable: !node.readOnly, bounding_box: boxOf(node)};
          });
          const rows = Array.from(modalRoot.querySelectorAll('.qHFpb6, .dialog-item-content, [role="listitem"], [role="button"]')).filter(visible);
          const hasListOrEmpty = rows.length > 0 || /no result|empty|Ù†ØªÛŒØ¬Ù‡|ÛŒØ§ÙØª Ù†Ø´Ø¯/i.test(normalize(picker.innerText || picker.textContent || ""));
          const selectedRows = [];
          const selectedChips = [];
          const rejected = [];
          const reject = (node, reason, source) => rejected.push({
            rejected_candidate_reason: reason,
            candidate_text: normalize(node.innerText || node.textContent || "").slice(0, 160),
            candidate_role: node.getAttribute("role") || "",
            candidate_aria_label: node.getAttribute("aria-label") || "",
            candidate_aria_selected: node.getAttribute("aria-selected") || "",
            candidate_checked: Boolean(node.checked),
            candidate_classes: String(node.className || "").slice(0, 160),
            candidate_parent_text: normalize(node.parentElement ? node.parentElement.innerText || node.parentElement.textContent || "" : "").slice(0, 160),
            candidate_outer_html_excerpt: excerpt(node),
            candidate_bounding_box: boxOf(node),
            clickable_ancestor_chain: chain(node),
            source
          });
          for (const row of rows) {
            const text = normalize(row.innerText || row.textContent || "");
            const selected = row.getAttribute("aria-selected") === "true" || Boolean(row.querySelector('input[type="checkbox"]:checked, input[type="radio"]:checked, [aria-checked="true"]')) || /selected|checked/i.test(String(row.className || ""));
            if (!selected) continue;
            const name = validName(text);
            if (!name.ok) {
              reject(row, name.reason, "selected_row");
              continue;
            }
            selectedRows.push({text, selector: selectorFor(row, ""), selected: true, aria_selected: row.getAttribute("aria-selected") || "", checkbox_radio_state: Boolean(row.querySelector('input[type="checkbox"]:checked, input[type="radio"]:checked, [aria-checked="true"]')), bounding_box: boxOf(row), clickable_ancestor_chain: chain(row)});
          }
          const chipCandidates = Array.from(modalRoot.querySelectorAll('div, span, button, [role="button"]')).filter((node) => {
            if (!(node instanceof HTMLElement) || !visible(node)) return false;
            if (node.matches('input, textarea, [contenteditable="true"]')) return false;
            const rect = node.getBoundingClientRect();
            const text = normalize(node.innerText || node.textContent || "");
            return text && rect.y >= pickerRect.bottom - 150 && rect.y <= pickerRect.bottom - 30 && rect.width >= 40 && rect.width <= 280 && rect.height >= 18 && rect.height <= 70;
          });
          const chips = chipCandidates.filter((node) => !chipCandidates.some((other) => other !== node && other.contains(node)));
          for (const chip of chips) {
            const text = normalize(chip.innerText || chip.textContent || "").replace(/^[Ã—xX]\\s*/, "").replace(/\\s*[Ã—xX]$/, "");
            const name = validName(text);
            const controls = Array.from(chip.querySelectorAll('button, [role="button"], [aria-label], svg')).filter((node) => node instanceof Element && visible(node));
            const remove = controls.find((node) => /remove|delete|deselect|clear|close|Ø­Ø°Ù|Ù¾Ø§Ú©/i.test(`${node.getAttribute("aria-label") || ""} ${node.getAttribute("title") || ""} ${normalize(node.innerText || node.textContent || "")}`));
            if (!name.ok) {
              reject(chip, name.reason, "selected_chip");
              continue;
            }
            if (!remove) {
              reject(chip, "remove_control_not_verified", "selected_chip");
              continue;
            }
            remove.setAttribute("data-clinicos-forensic", `remove-${selectedChips.length}`);
            selectedChips.push({text, selector: selectorFor(chip, ""), selected: true, bounding_box: boxOf(chip), proposed_remove_selector: selectorFor(remove, ""), proposed_remove_parent_text: normalize(chip.innerText || chip.textContent || ""), candidate_outer_html_excerpt: excerpt(chip), clickable_ancestor_chain: chain(remove)});
          }
          const selectedNames = selectedRows.concat(selectedChips).map((item) => item.text);
          return {
            dry_run: true,
            picker_verified: Boolean(searchInputs.length && hasListOrEmpty && !successToast),
            picker_structure: {has_picker: true, has_search_input: Boolean(searchInputs.length), has_list_or_empty_state: hasListOrEmpty, success_toast_visible: successToast},
            picker_outer_html_excerpt: excerpt(picker),
            picker_selector: "div.anWA5J",
            modal_root_selector: selectorFor(modalRoot, '[data-clinicos-forensic="modal-root"]'),
            modal_root_outer_html_excerpt: excerpt(modalRoot),
            picker_bounding_box: boxOf(picker),
            picker_search_inputs: searchInputs,
            visible_buttons: Array.from(modalRoot.querySelectorAll('button, [role="button"], [aria-label]')).filter(visible).slice(0, 80).map((node) => ({text: normalize(node.innerText || node.textContent || ""), aria_label: node.getAttribute("aria-label") || "", role: node.getAttribute("role") || "", classes: String(node.className || "").slice(0, 120), bounding_box: boxOf(node)})),
            visible_rows: rows.slice(0, 80).map((node) => ({text: normalize(node.innerText || node.textContent || "").slice(0, 200), role: node.getAttribute("role") || "", aria_label: node.getAttribute("aria-label") || "", aria_selected: node.getAttribute("aria-selected") || "", classes: String(node.className || "").slice(0, 120), bounding_box: boxOf(node)})),
            selected_row_candidates: selectedRows,
            selected_chip_candidates: selectedChips,
            selected_names_before_search: selectedNames,
            selected_count_before_search: selectedNames.length,
            sahar_selected: selectedNames.some((name) => normalize(name).toLowerCase() === "sahar"),
            rejected_selected_candidates: rejected,
            rejected_candidate_reasons: rejected.map((item) => item.rejected_candidate_reason),
            remove_control_candidates: selectedChips.map((item) => ({text: item.text, proposed_remove_selector: item.proposed_remove_selector, proposed_remove_parent_text: item.proposed_remove_parent_text})),
            destructive_clicks_attempted: 0
          };
        }
        """
        try:
            raw = page.evaluate(script, display_name)
        except Exception:
            raw = {}
        return raw if isinstance(raw, dict) else {}

    def _forward_selected_recipients_state(self, page: Any) -> dict[str, Any]:
        script = """
        () => {
          const normalize = (value) => String(value || "").replace(/\\s+/g, " ").trim();
          const visible = (node) => {
            if (!node) return false;
            const rect = node.getBoundingClientRect();
            const style = window.getComputedStyle(node);
            return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none" && Number(style.opacity) !== 0;
          };
          const selectorFor = (node, fallback) => {
            if (!node) return "";
            const marker = node.getAttribute("data-clinicos-selected-recipient");
            if (marker) return `[data-clinicos-selected-recipient="${marker}"]`;
            if (node.id) return `#${CSS.escape(node.id)}`;
            const aria = node.getAttribute("aria-label");
            if (aria) return `${node.tagName.toLowerCase()}[aria-label="${aria.replace(/"/g, "\\\\\\"")}"]`;
            const role = node.getAttribute("role");
            if (role) return `${node.tagName.toLowerCase()}[role="${role}"]`;
            const cls = String(node.className || "").split(/\\s+/).filter(Boolean)[0];
            return cls ? `${node.tagName.toLowerCase()}.${CSS.escape(cls)}` : fallback;
          };
          const selectedLike = (node) => {
            if (!(node instanceof HTMLElement)) return false;
            const ariaSelected = node.getAttribute("aria-selected") || "";
            const ariaChecked = node.getAttribute("aria-checked") || "";
            const role = node.getAttribute("role") || "";
            const checkedInput = node.matches('input[type="checkbox"], input[type="radio"]') && Boolean(node.checked);
            const checkedChild = Boolean(node.querySelector('input[type="checkbox"]:checked, input[type="radio"]:checked, [aria-checked="true"]'));
            return ariaSelected === "true"
              || ariaChecked === "true"
              || checkedInput
              || checkedChild;
          };
          const selectedName = (row) => {
            const text = normalize(row.innerText || row.textContent || "");
            if (text && text.length <= 160) return text;
            const labelled = Array.from(row.querySelectorAll('[title], [aria-label], span, div')).map((node) => {
              return normalize(node.getAttribute("title") || node.getAttribute("aria-label") || node.innerText || node.textContent || "");
            }).filter(Boolean);
            return (labelled.find((value) => value && value.length <= 120) || text).slice(0, 160);
          };
          const validRecipientName = (value) => {
            const text = normalize(value);
            if (text.length < 3) return false;
            if (/^[\\d\\u06F0-\\u06F9\\u0660-\\u0669]+$/.test(text)) return false;
            if (/^[Ã—xX+\\-â€“â€”â€¢Â·\\.ØŒ,Ø›:;!?\\s]+$/.test(text)) return false;
            if (/^(close|remove|delete|clear|cancel|back|forward|send|confirm)$/i.test(text)) return false;
            return true;
          };
          const picker = Array.from(document.querySelectorAll('div.anWA5J')).find((node) => {
            if (!visible(node)) return false;
            return Boolean(Array.from(node.querySelectorAll('input[type="search"], input, [role="searchbox"], [role="textbox"], [contenteditable="true"]')).find(visible));
          });
          const modalRootFor = (node) => {
            if (!node) return null;
            let best = null;
            let current = node;
            for (let depth = 0; current && depth < 8; depth += 1, current = current.parentElement) {
              if (!visible(current)) continue;
              const rect = current.getBoundingClientRect();
              const modalSized = rect.width >= 300 && rect.width <= 700 && rect.height >= 360 && rect.height <= window.innerHeight;
              if (modalSized) best = current;
            }
            return best || node;
          };
          const searchInput = picker ? Array.from(picker.querySelectorAll('input[type="search"], input, [role="searchbox"], [role="textbox"], [contenteditable="true"]')).find(visible) : null;
          const pickerRoot = picker ? modalRootFor(searchInput || picker) : null;
          const nodes = pickerRoot ? Array.from(pickerRoot.querySelectorAll('.qHFpb6, .dialog-item-content, [role="listitem"], [role="button"], button, a, [aria-selected], [aria-checked], input[type="checkbox"], input[type="radio"]')) : [];
          const selected = [];
          const seen = new Set();
          const pushSelected = (row, click, text, source, selectedNode) => {
            if (!row || !click || !text) return;
            if (!validRecipientName(text)) return;
            const rect = row.getBoundingClientRect();
            const key = selectorFor(click, "") || `${text}|${Math.round(rect.x)}|${Math.round(rect.y)}`;
            if (seen.has(key)) return;
            seen.add(key);
            const index = selected.length;
            click.setAttribute("data-clinicos-selected-recipient", String(index));
            selected.push({
              selector: selectorFor(row, ""),
              click_selector: selectorFor(click, `[data-clinicos-selected-recipient="${index}"]`),
              text,
              selected: true,
              source,
              aria_selected: row.getAttribute("aria-selected") || (selectedNode && selectedNode.getAttribute("aria-selected")) || "",
              checkbox_radio_state: Boolean((selectedNode && selectedNode.checked === true) || row.querySelector('input[type="checkbox"]:checked, input[type="radio"]:checked, [aria-checked="true"]')),
              bounding_box: {x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height)}
            });
          };
          for (const node of nodes) {
            if (!visible(node) || !selectedLike(node)) continue;
            const row = rowParent(node);
            if (!row || !visible(row)) continue;
            const rootRect = pickerRoot.getBoundingClientRect();
            const rowRect = row.getBoundingClientRect();
            if (rowRect.left < rootRect.left - 8 || rowRect.right > rootRect.right + 8 || rowRect.top < rootRect.top - 8 || rowRect.bottom > rootRect.bottom + 8) continue;
            const click = clickableParent(row);
            const text = selectedName(row);
            pushSelected(row, click, text, "selected_state", node);
            if (selected.length >= 20) break;
          }
          if (pickerRoot) {
            const pickerRect = pickerRoot.getBoundingClientRect();
            const chipCandidates = Array.from(document.querySelectorAll('div, span, button, [role="button"]')).filter((node) => {
              if (!(node instanceof HTMLElement) || !visible(node)) return false;
              if (node.matches('input, textarea, [contenteditable="true"]')) return false;
              const rect = node.getBoundingClientRect();
              const horizontallyInsideModal = rect.left >= pickerRect.left - 8 && rect.right <= pickerRect.right + 8;
              if (!horizontallyInsideModal) return false;
              const text = normalize(node.innerText || node.textContent || "");
              if (/Ø­Ø°Ù|Ø±ÙˆÙ†ÙˆØ´Øª|Ø§Ø´ØªØ±Ø§Ú©|Ø§ÙØ²ÙˆØ¯Ù†|Ù¾ÛŒÙˆÙ†Ø¯|copy|share|link|story/i.test(text)) return false;
              if (!text || text.length > 120) return false;
              if (rect.y < pickerRect.bottom - 140 || rect.y > pickerRect.bottom - 35) return false;
              if (rect.width < 40 || rect.width > 260 || rect.height < 18 || rect.height > 60) return false;
              if (/Ø¬Ø³ØªØ¬Ùˆ|Ù†ÙˆØ´ØªÙ†|ØªÙˆØ¶ÛŒØ­Ø§Øª|Ø¨Ø§Ø²Ø§Ø±Ø³Ø§Ù„|Forward|Send|Confirm/i.test(text)) return false;
              return validRecipientName(text.replace(/^[Ãƒâ€”xX]\\s*/, "").replace(/\\s*[Ãƒâ€”xX]$/, ""));
            });
            const chipNodes = chipCandidates.filter((node) => !chipCandidates.some((other) => other !== node && other.contains(node)));
            for (const node of chipNodes) {
              const text = normalize(node.innerText || node.textContent || "").replace(/^Ã—\\s*/, "").replace(/\\s*Ã—$/, "");
              const chipText = normalize(text.replace(/^[Ã—xX]\\s*/, "").replace(/\\s*[Ã—xX]$/, ""));
              if (!chipText || chipText.length > 120 || !validRecipientName(chipText)) continue;
              const childControls = Array.from(node.querySelectorAll('svg, button, [role="button"], [aria-label]')).filter((child) => child instanceof HTMLElement && visible(child));
              const click = childControls.find((child) => {
                const rect = child.getBoundingClientRect();
                return rect.width <= 36 && rect.height <= 36;
              }) || childControls[0] || node;
              pushSelected(node, click, chipText, "selected_chip", node);
              if (selected.length >= 20) break;
            }
            const removeControls = Array.from(pickerRoot.querySelectorAll('button, [role="button"], [aria-label], svg')).filter((node) => {
              if (!(node instanceof Element) || !visible(node)) return false;
              const rect = node.getBoundingClientRect();
              const label = `${node.getAttribute("aria-label") || ""} ${node.getAttribute("title") || ""} ${normalize(node.innerText || node.textContent || "")}`;
              return rect.y >= pickerRect.bottom - 150 && rect.y <= pickerRect.bottom - 20 && /close|remove|delete|deselect|clear|Ãƒâ€”|x|Ã˜Â­Ã˜Â°Ã™Â|Ã™Â¾Ã˜Â§ÃšÂ©/i.test(label);
            });
            for (const control of removeControls) {
              let chip = control.parentElement;
              for (let depth = 0; chip && depth < 5; depth += 1, chip = chip.parentElement) {
                if (!(chip instanceof HTMLElement) || !visible(chip)) continue;
                const rect = chip.getBoundingClientRect();
                if (rect.width < 40 || rect.width > 280 || rect.height < 18 || rect.height > 80) continue;
                const chipText = normalize(chip.innerText || chip.textContent || "").replace(/^[Ãƒâ€”xX]\\s*/, "").replace(/\\s*[Ãƒâ€”xX]$/, "");
                if (!chipText || chipText.length > 120 || !validRecipientName(chipText)) continue;
                pushSelected(chip, control, chipText, "selected_chip", control);
                break;
              }
              if (selected.length >= 20) break;
            }
          }
          const uniqueSelected = [];
          const seenSelectedNames = new Set();
          for (const item of selected) {
            const key = normalize(item.text);
            if (!key || seenSelectedNames.has(key)) continue;
            seenSelectedNames.add(key);
            uniqueSelected.push(item);
          }
          return {
            marker: "clinicos_bale_forward_selected_recipients_state",
            selected_count: uniqueSelected.length,
            selected_names: uniqueSelected.map((item) => item.text),
            selected_recipients: uniqueSelected
          };
        }
        """
        try:
            raw = page.evaluate(script)
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        return {
            "selected_count": int(raw.get("selected_count") or 0),
            "selected_names": raw.get("selected_names") if isinstance(raw.get("selected_names"), list) else [],
            "selected_recipients": raw.get("selected_recipients") if isinstance(raw.get("selected_recipients"), list) else [],
        }

    def _forward_recipient_candidates(self, page: Any, display_name: str) -> dict[str, Any]:
        script = """
        (displayName) => {
          const normalize = (value) => String(value || "").replace(/\\s+/g, " ").trim();
          const visible = (node) => {
            if (!node) return false;
            const rect = node.getBoundingClientRect();
            const style = window.getComputedStyle(node);
            return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none" && Number(style.opacity) !== 0;
          };
          const selectorFor = (node, fallback) => {
            if (!node) return "";
            const marker = node.getAttribute("data-clinicos-recipient-result");
            if (marker) return `[data-clinicos-recipient-result="${marker}"]`;
            if (node.id) return `#${CSS.escape(node.id)}`;
            const aria = node.getAttribute("aria-label");
            if (aria) return `${node.tagName.toLowerCase()}[aria-label="${aria.replace(/"/g, "\\\\\\"")}"]`;
            const role = node.getAttribute("role");
            if (role) return `${node.tagName.toLowerCase()}[role="${role}"]`;
            const cls = String(node.className || "").split(/\\s+/).filter(Boolean)[0];
            return cls ? `${node.tagName.toLowerCase()}.${CSS.escape(cls)}` : fallback;
          };
          const nameElementFor = (row) => {
            if (!(row instanceof HTMLElement)) return null;
            const preferredSelectors = [
              '.oUKPfP',
              '[class*="name" i]',
              '[class*="title" i]',
              '[data-testid*="name" i]',
              '[data-testid*="title" i]',
              '[aria-label]'
            ];
            for (const selector of preferredSelectors) {
              for (const child of Array.from(row.querySelectorAll(selector))) {
                if (!visible(child)) continue;
                const childText = normalize(child.innerText || child.textContent || "");
                const childAria = normalize(child.getAttribute("aria-label") || "");
                const childTitle = normalize(child.getAttribute("title") || "");
                if (childText === target || childAria === target || childTitle === target) return child;
              }
            }
            for (const child of Array.from(row.querySelectorAll('*'))) {
              if (!visible(child)) continue;
              const childText = normalize(child.innerText || child.textContent || "");
              const childAria = normalize(child.getAttribute("aria-label") || "");
              const childTitle = normalize(child.getAttribute("title") || "");
              if (childText === target || childAria === target || childTitle === target) return child;
            }
            return null;
          };
          const picker = Array.from(document.querySelectorAll('div.anWA5J, [role="dialog"], [class*="Modal"], [class*="modal"]')).find(visible);
          const loadingIndicatorVisible = Boolean(picker && Array.from(picker.querySelectorAll('[class*="loading"], [class*="Loading"], [data-testid*="loading"], [aria-label*="loading"], svg, img')).find((node) => {
            if (!visible(node)) return false;
            const label = `${node.getAttribute("aria-label") || ""} ${node.getAttribute("data-testid") || ""} ${String(node.className || "")} ${normalize(node.innerText || node.textContent || "")}`;
            return /loading|spinner|progress|Ø¯Ø± Ø­Ø§Ù„|Ø¨Ø§Ø±Ú¯Ø°Ø§Ø±ÛŒ/i.test(label);
          }));
          const rowSelector = '.ReactModal__Overlay .qHFpb6';
          const allRows = Array.from(document.querySelectorAll(rowSelector));
          const nodes = allRows.filter((node) => picker && picker.contains(node) && visible(node));
          const target = normalize(displayName);
          const candidates = [];
          const seen = new Set();
          for (const [visibleIndex, row] of nodes.entries()) {
            const rowIndex = allRows.indexOf(row);
            const text = normalize(row.innerText || row.textContent || "");
            const aria = normalize(row.getAttribute("aria-label") || "");
            const title = normalize(row.getAttribute("title") || "");
            const combined = normalize(`${text} ${aria} ${title}`);
            if (!combined || combined.length > 500) continue;
            const hasTarget = text === target || aria === target || title === target || combined.includes(target);
            if (!hasTarget) continue;
            const rowText = normalize(row.innerText || row.textContent || text);
            const rowAria = normalize(row.getAttribute("aria-label") || aria);
            const rowTitle = normalize(row.getAttribute("title") || title);
            const nameElement = nameElementFor(row);
            const rowName = nameElement ? target : "";
            const exactWithinRow = rowName === target;
            const key = `${rowIndex}|${rowText}|${rowAria}|${rowTitle}`;
            if (seen.has(key)) continue;
            seen.add(key);
            const rowClickSelector = `${rowSelector}:has(.oUKPfP:text-is(${JSON.stringify(target)}))`;
            candidates.push({
              selector: rowClickSelector,
              click_selector: rowClickSelector,
              row_selector: rowSelector,
              row_index: rowIndex,
              visible_index: visibleIndex,
              text: rowText,
              row_name: rowName,
              normalized_name: rowName,
              selected: row.getAttribute("aria-selected") === "true" || Boolean(row.querySelector('input[type="checkbox"]:checked, input[type="radio"]:checked, [aria-checked="true"]')),
              checkbox_radio_state: Boolean(row.querySelector('input[type="checkbox"]:checked, input[type="radio"]:checked, [aria-checked="true"]')),
              aria_selected: row.getAttribute("aria-selected") || "",
              exact_text: rowName,
              name_selector: nameElement ? selectorFor(nameElement, "") : "",
              aria_label: rowAria,
              title: rowTitle,
              role: row.getAttribute("role") || "",
              className: String(row.className || "").slice(0, 120),
              exact_match: exactWithinRow,
              bounding_box: {x: Math.round(row.getBoundingClientRect().x), y: Math.round(row.getBoundingClientRect().y), width: Math.round(row.getBoundingClientRect().width), height: Math.round(row.getBoundingClientRect().height)}
            });
            if (candidates.length >= 20) break;
          }
          return {marker: "clinicos_bale_forward_recipient_candidates", recipient_candidates: candidates, loading_indicator_visible: loadingIndicatorVisible};
        }
        """
        try:
            raw = page.evaluate(script, display_name)
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        candidates = raw.get("recipient_candidates") if isinstance(raw.get("recipient_candidates"), list) else []
        return {
            "recipient_candidates": candidates,
            "visible_result_count": len(candidates),
            "visible_result_names": [str(item.get("row_name") or item.get("normalized_name") or item.get("text") or "") for item in candidates if isinstance(item, dict)],
            "loading_indicator_visible": bool(raw.get("loading_indicator_visible")),
        }

    def _forward_search_input_value(self, page: Any, search_selector: str) -> str:
        script = """
        (selector) => {
          const node = document.querySelector(selector);
          if (!node) return "";
          if ("value" in node) return String(node.value || "");
          return String(node.innerText || node.textContent || "");
        }
        """
        try:
            return str(page.evaluate(script, search_selector) or "")
        except Exception:
            return ""

    def _select_last_visible_fixed(self, page: Any, selector: str) -> dict[str, Any]:
        script = """
        (selector) => {
          const visible = (node) => {
            if (!node) return false;
            const rect = node.getBoundingClientRect();
            const style = window.getComputedStyle(node);
            return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none" && Number(style.opacity) !== 0;
          };
          const textOf = (node) => String((node && (node.innerText || node.textContent)) || "").replace(/\\s+/g, " ").trim();
          document.querySelectorAll("[data-clinicos-linear-latest-message]").forEach((node) => node.removeAttribute("data-clinicos-linear-latest-message"));
          const nodes = Array.from(document.querySelectorAll(selector)).filter(visible);
          const selected = nodes[nodes.length - 1] || null;
          if (!selected) return {ok: false, selector, visible_count: 0, text: ""};
          selected.setAttribute("data-clinicos-linear-latest-message", "true");
          return {
            ok: true,
            selector: '[data-clinicos-linear-latest-message="true"]',
            visible_count: nodes.length,
            text: textOf(selected).slice(0, 500)
          };
        }
        """
        try:
            raw = page.evaluate(script, selector)
        except Exception as exc:
            raw = {"ok": False, "message": str(exc)}
        return raw if isinstance(raw, dict) else {"ok": False, "message": "invalid_select_last_visible_result"}

    def _first_visible_fixed_result(self, page: Any, selector: str, timeout_ms: int = 4000) -> dict[str, Any]:
        started = time.perf_counter()
        state: dict[str, Any] = {}
        while (time.perf_counter() - started) * 1000 <= max(0, timeout_ms):
            state = self._first_visible_fixed_result_state(page, selector)
            if state.get("ok"):
                return state
            _safe_wait_for_timeout(page, 150)
        return state or {"ok": False, "visible_result_count": 0, "first_result_text": "", "selector": selector}

    def _first_visible_fixed_result_state(self, page: Any, selector: str) -> dict[str, Any]:
        script = """
        (selector) => {
          const visible = (node) => {
            if (!node) return false;
            const rect = node.getBoundingClientRect();
            const style = window.getComputedStyle(node);
            return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none" && Number(style.opacity) !== 0;
          };
          const textOf = (node) => String((node && (node.innerText || node.textContent)) || "").replace(/\\s+/g, " ").trim();
          document.querySelectorAll("[data-clinicos-linear-recipient-result]").forEach((node) => node.removeAttribute("data-clinicos-linear-recipient-result"));
          const nodes = Array.from(document.querySelectorAll(selector)).filter(visible);
          const first = nodes[0] || null;
          if (!first) return {ok: false, selector, visible_result_count: 0, first_result_text: ""};
          first.setAttribute("data-clinicos-linear-recipient-result", "0");
          return {
            ok: true,
            selector: '[data-clinicos-linear-recipient-result="0"]',
            visible_result_count: nodes.length,
            first_result_text: textOf(first),
            visible_result_texts: nodes.slice(0, 20).map(textOf)
          };
        }
        """
        try:
            raw = page.evaluate(script, selector)
        except Exception as exc:
            raw = {"ok": False, "message": str(exc), "visible_result_count": 0, "first_result_text": ""}
        return raw if isinstance(raw, dict) else {"ok": False, "visible_result_count": 0, "first_result_text": ""}

    def _forward_recipient_results_stability(self, page: Any, display_name: str) -> dict[str, Any]:
        first = self._forward_recipient_candidates(page, display_name)
        _safe_wait_for_timeout(page, 500)
        second = self._forward_recipient_candidates(page, display_name)

        def signature(state: dict[str, Any]) -> list[str]:
            candidates = state.get("recipient_candidates") if isinstance(state.get("recipient_candidates"), list) else []
            return [
                f"{item.get('row_name') or item.get('normalized_name') or item.get('text') or ''}|{item.get('selector') or ''}|{item.get('click_selector') or ''}"
                for item in candidates
                if isinstance(item, dict)
            ]

        stable = signature(first) == signature(second) and not bool(first.get("loading_indicator_visible")) and not bool(second.get("loading_indicator_visible"))
        second["result_set_stable"] = stable
        second["result_set_first_signature"] = signature(first)
        second["result_set_second_signature"] = signature(second)
        return second

    def _forward_recipient_click_diagnostic(self, page: Any, click_selector: str, display_name: str) -> dict[str, Any]:
        script = """
        ({clickSelector, displayName}) => {
          const normalize = (value) => String(value || "").replace(/\\s+/g, " ").trim();
          const visible = (node) => {
            if (!node) return false;
            const rect = node.getBoundingClientRect();
            const style = window.getComputedStyle(node);
            return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none" && Number(style.opacity) !== 0;
          };
          const boxOf = (node) => {
            if (!node) return null;
            const rect = node.getBoundingClientRect();
            return {x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height)};
          };
          const centerOf = (box) => box ? {x: Math.round(box.x + box.width / 2), y: Math.round(box.y + box.height / 2)} : null;
          const excerpt = (node) => String((node && node.outerHTML) || "").replace(/\\s+/g, " ").slice(0, 900);
          const selectorFor = (node, fallback) => {
            if (!node) return "";
            const resultMarker = node.getAttribute("data-clinicos-recipient-result");
            if (resultMarker) return `[data-clinicos-recipient-result="${resultMarker}"]`;
            if (node.id) return `#${CSS.escape(node.id)}`;
            const role = node.getAttribute("role");
            if (role) return `${node.tagName.toLowerCase()}[role="${role.replace(/"/g, "\\\\\\"")}"]`;
            const aria = node.getAttribute("aria-label");
            if (aria) return `${node.tagName.toLowerCase()}[aria-label="${aria.replace(/"/g, "\\\\\\"")}"]`;
            const cls = String(node.className || "").split(/\\s+/).filter(Boolean)[0];
            return cls ? `${node.tagName.toLowerCase()}.${CSS.escape(cls)}` : fallback;
          };
          const chain = (node) => {
            const rows = [];
            let current = node;
            for (let depth = 0; current && depth < 8; depth += 1, current = current.parentElement) {
              rows.push({tag: current.tagName.toLowerCase(), role: current.getAttribute("role") || "", aria_label: current.getAttribute("aria-label") || "", classes: String(current.className || "").slice(0, 140), text: normalize(current.innerText || current.textContent || "").slice(0, 180), selector: selectorFor(current, "")});
            }
            return rows;
          };
          const rowFor = (node) => node ? (node.closest('[data-clinicos-recipient-result], .qHFpb6, .dialog-item-content, [role="listitem"], [role="button"]') || node) : null;
          const target = normalize(displayName);
          const rowNameInfoFor = (row) => {
            if (!row) return {name: "", selector: ""};
            const exact = Array.from(row.querySelectorAll('*')).find((child) => {
              return normalize(child.innerText || child.textContent || "") === target
                || normalize(child.getAttribute("aria-label") || "") === target
                || normalize(child.getAttribute("title") || "") === target;
            });
            if (exact) return {name: target, selector: selectorFor(exact, "")};
            const text = normalize(row.innerText || row.textContent || "");
            return {name: text === target ? target : text, selector: selectorFor(row, "")};
          };
          const click = document.querySelector(clickSelector);
          if (!click || !visible(click)) return {click_safe: false, click_diagnostic_error: "click_target_missing_or_hidden", target_click_selector: clickSelector};
          const row = rowFor(click);
          const rowText = normalize(row && (row.innerText || row.textContent || ""));
          const rowNameInfo = rowNameInfoFor(row);
          const rowName = rowNameInfo.name;
          const clickBox = boxOf(click);
          const rowBox = boxOf(row);
          const point = centerOf(clickBox);
          const top = point ? document.elementFromPoint(point.x, point.y) : null;
          const topRow = rowFor(top);
          const topRowText = normalize(topRow && (topRow.innerText || topRow.textContent || ""));
          const topRowName = rowNameInfoFor(topRow).name;
          const picker = Array.from(document.querySelectorAll('div.anWA5J')).find(visible);
          const root = picker ? (picker.closest('[role="dialog"], [class*="Modal"], [class*="modal"]') || picker) : document;
          const rows = Array.from(root.querySelectorAll('[data-clinicos-recipient-result], .qHFpb6, .dialog-item-content, [role="listitem"], [role="button"]')).filter((candidate) => {
            if (!visible(candidate)) return false;
            const text = normalize(candidate.innerText || candidate.textContent || "");
            return text && text.length <= 300;
          });
          const overlappingRows = rows.map((candidate, index) => {
            const box = boxOf(candidate);
            const text = normalize(candidate.innerText || candidate.textContent || "");
            const containsPoint = Boolean(point && box && point.x >= box.x && point.x <= box.x + box.width && point.y >= box.y && point.y <= box.y + box.height);
            const overlapsTarget = Boolean(rowBox && box && !(box.x + box.width < rowBox.x || rowBox.x + rowBox.width < box.x || box.y + box.height < rowBox.y || rowBox.y + rowBox.height < box.y));
            return {index, text, row_name: rowNameInfoFor(candidate).name, selector: selectorFor(candidate, ""), bounding_box: box, contains_click_point: containsPoint, overlaps_target_row: overlapsTarget, z_index: window.getComputedStyle(candidate).zIndex || ""};
          }).filter((item) => item.contains_click_point || item.overlaps_target_row).slice(0, 20);
          const exactRowsAtPoint = overlappingRows.filter((item) => item.contains_click_point && normalize(item.row_name) === target);
          const otherRowsAtPoint = overlappingRows.filter((item) => item.contains_click_point && normalize(item.row_name) !== target);
          const uniqueRowKeys = Array.from(new Set(overlappingRows.filter((item) => item.contains_click_point).map((item) => item.row_name)));
          const uniqueRecipientRowsAtPoint = uniqueRowKeys.length;
          const nestedElementsSameRowCount = overlappingRows.filter((item) => item.contains_click_point && normalize(item.row_name) === target).length;
          const clickSafe = rowName === target && topRowName === target && exactRowsAtPoint.length >= 1 && otherRowsAtPoint.length === 0 && !/sahar/i.test(topRowName);
          return {
            marker: "clinicos_bale_forward_recipient_click_diagnostic",
            click_safe: clickSafe,
            target_row_selector: selectorFor(row, ""),
            target_row_text: rowText,
            target_row_name: rowName,
            target_name_selector: rowNameInfo.selector,
            target_click_selector: clickSelector,
            target_click_bounding_box: clickBox,
            target_click_point: point,
            element_from_point_tag: top ? top.tagName.toLowerCase() : "",
            element_from_point_text: normalize(top && (top.innerText || top.textContent || "")).slice(0, 180),
            element_from_point_row_name: topRowName,
            element_from_point_row_text: topRowText,
            element_from_point_outer_html_excerpt: excerpt(top),
            overlapping_recipient_rows: overlappingRows,
            unique_recipient_rows_at_click_point: uniqueRecipientRowsAtPoint,
            nested_elements_same_row_count: nestedElementsSameRowCount,
            target_row_outer_html_excerpt: excerpt(row),
            target_click_outer_html_excerpt: excerpt(click),
            clickable_descendants: Array.from(row.querySelectorAll('button, [role="button"], [aria-label], a, input, svg')).filter(visible).slice(0, 30).map((node) => ({tag: node.tagName.toLowerCase(), text: normalize(node.innerText || node.textContent || ""), aria_label: node.getAttribute("aria-label") || "", role: node.getAttribute("role") || "", selector: selectorFor(node, ""), bounding_box: boxOf(node), z_index: window.getComputedStyle(node).zIndex || ""})),
            clickable_ancestor_chain: chain(click),
            z_index: window.getComputedStyle(click).zIndex || ""
          };
        }
        """
        try:
            raw = page.evaluate(script, {"clickSelector": click_selector, "displayName": display_name})
        except Exception as exc:
            raw = {"click_safe": False, "click_diagnostic_error": str(exc)}
        return raw if isinstance(raw, dict) else {"click_safe": False, "click_diagnostic_error": "invalid_click_diagnostic_result"}

    def _forward_modal_selection_snapshot(self, page: Any, display_name: str) -> dict[str, Any]:
        selected = self._forward_selected_recipients_state(page)
        names = selected.get("selected_names") if isinstance(selected.get("selected_names"), list) else []
        return {
            "selected_names": names,
            "selected_count": int(selected.get("selected_count") or 0),
            "selected_rows": [item for item in selected.get("selected_recipients", []) if isinstance(item, dict) and item.get("source") == "selected_state"],
            "selected_footer_chips": [item for item in selected.get("selected_recipients", []) if isinstance(item, dict) and item.get("source") == "selected_chip"],
            "target_selected": any(str(name).strip() == display_name for name in names),
            "sahar_selected": any(str(name).strip().lower() == "sahar" for name in names),
        }

    def _forward_confirm_button_state(self, page: Any) -> dict[str, Any]:
        script = """
        () => {
          const visible = (node) => {
            if (!node) return false;
            const rect = node.getBoundingClientRect();
            const style = window.getComputedStyle(node);
            return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none" && Number(style.opacity) !== 0;
          };
          const textOf = (node) => String((node && (node.innerText || node.textContent)) || "").replace(/\\s+/g, " ").trim();
          const boxOf = (node) => {
            const rect = node.getBoundingClientRect();
            return {x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height)};
          };
          const hitOf = (node) => {
            const rect = node.getBoundingClientRect();
            const points = [
              [rect.x + rect.width / 2, rect.y + rect.height / 2],
              [rect.x + Math.min(6, rect.width / 3), rect.y + Math.min(6, rect.height / 3)],
              [rect.right - Math.min(6, rect.width / 3), rect.y + Math.min(6, rect.height / 3)],
              [rect.x + Math.min(6, rect.width / 3), rect.bottom - Math.min(6, rect.height / 3)],
              [rect.right - Math.min(6, rect.width / 3), rect.bottom - Math.min(6, rect.height / 3)]
            ];
            return points.map(([x, y]) => {
              const hit = document.elementFromPoint(x, y);
              return {tag: hit ? hit.tagName.toLowerCase() : "", className: hit ? String(hit.className || "").slice(0, 120) : "", aria_label: hit ? hit.getAttribute("aria-label") || "" : "", contained: Boolean(hit && (hit === node || node.contains(hit)))};
            });
          };
          const picker = Array.from(document.querySelectorAll('.ReactModal__Overlay')).find(visible);
          const selector = '.ReactModal__Overlay [role="button"][aria-label="send-button-forward-messages"][data-testid="bold-send2-icon"]';
          const nodes = Array.from(document.querySelectorAll(selector)).filter((node) => picker && picker.contains(node));
          const visibleNodes = nodes.filter(visible);
          const enabledNodes = visibleNodes.filter((node) => !node.disabled && node.getAttribute("aria-disabled") !== "true" && !node.closest("[disabled], [aria-disabled='true']"));
          const hitTestableNodes = enabledNodes.filter((node) => {
            const hits = hitOf(node);
            return Boolean(hits[0] && hits[0].contained);
          });
          const debug = nodes.map((node, index) => {
            const text = textOf(node);
            const aria = node.getAttribute("aria-label") || "";
            const role = node.getAttribute("role") || "";
            const className = String(node.className || "").slice(0, 160);
            const enabled = !node.disabled && node.getAttribute("aria-disabled") !== "true";
            const isVisible = visible(node);
            const icon = node.querySelector('svg[aria-label="BoldSend2-icon"]');
            const descriptionField = picker ? Array.from(picker.querySelectorAll('textarea, input, [contenteditable="true"], [placeholder]')).find((item) => /نوشتن توضیحات/.test(item.getAttribute("placeholder") || textOf(item))) : null;
            const badgeText = normalizeBadgeText(textOf(node.parentElement || node));
            const hits = hitOf(node);
            return {index, text, aria_label: aria, role, className, data_testid: node.getAttribute("data-testid") || "", visible: isVisible, enabled, hit_testable: Boolean(hits[0] && hits[0].contained), icon_aria_label: icon ? icon.getAttribute("aria-label") || "" : "", description_field_present: Boolean(descriptionField), recipient_count_badge: badgeText, bounding_box: boxOf(node), hit_tests: hits, outer_html: node.outerHTML.slice(0, 700), status: isVisible && enabled ? "candidate" : "observed"};
          });
          function normalizeBadgeText(value) {
            return String(value || "").replace(/[۰٠]/g, "0").replace(/[۱١]/g, "1").replace(/[۲٢]/g, "2").replace(/[۳٣]/g, "3").replace(/[۴٤]/g, "4").replace(/[۵٥]/g, "5").replace(/[۶٦]/g, "6").replace(/[۷٧]/g, "7").replace(/[۸٨]/g, "8").replace(/[۹٩]/g, "9").replace(/\\s+/g, " ").trim();
          }
          return {
            marker: "clinicos_bale_forward_confirm_button_state",
            confirm_button_selector: hitTestableNodes.length === 1 ? selector : "",
            final_forward_dom_count: nodes.length,
            final_forward_visible_count: visibleNodes.length,
            final_forward_enabled_count: enabledNodes.length,
            final_forward_hit_testable_count: hitTestableNodes.length,
            candidate_debug: debug.slice(0, 60)
          };
        }
        """
        try:
            raw = page.evaluate(script)
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        return {
            "confirm_button_selector": str(raw.get("confirm_button_selector") or ""),
            "final_forward_dom_count": int(raw.get("final_forward_dom_count") or 0),
            "final_forward_visible_count": int(raw.get("final_forward_visible_count") or 0),
            "final_forward_enabled_count": int(raw.get("final_forward_enabled_count") or 0),
            "final_forward_hit_testable_count": int(raw.get("final_forward_hit_testable_count") or 0),
            "candidate_debug": raw.get("candidate_debug") if isinstance(raw.get("candidate_debug"), list) else [],
        }

    def _forward_success_state(self, page: Any, expected_recipient_name: str = "") -> dict[str, Any]:
        script = """
        (expectedRecipientName) => {
          const visible = (node) => {
            if (!node) return false;
            const rect = node.getBoundingClientRect();
            const style = window.getComputedStyle(node);
            return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none" && Number(style.opacity) !== 0;
          };
          const normalize = (value) => String(value || "").replace(/\\s+/g, " ").trim();
          const expectedText = `Post forwarded to ${String(expectedRecipientName || "")}.`;
          const pickerVisible = Boolean(Array.from(document.querySelectorAll('div.anWA5J, [role="dialog"], [class*="Modal"], [class*="modal"]')).find(visible));
          const alerts = Array.from(document.querySelectorAll('[role="alert"]')).filter(visible);
          const toastText = normalize(alerts.map((node) => normalize(node.innerText || node.textContent || "")).find((text) => text.includes(expectedText)) || "");
          const tickVisible = Boolean(alerts.find((node) => toastText && node.querySelector('[aria-label="TickDone-icon"]')));
          const verified = Boolean(toastText);
          return {
            marker: "clinicos_bale_forward_success_state",
            recipient_picker_visible: pickerVisible,
            forward_verified: verified,
            send_success_verified: verified,
            verification_method: verified ? "explicit_success_toast" : "",
            verification_evidence: verified ? expectedText : "",
            remote_message_id: null,
            success_toast_text: toastText,
            expected_success_toast_text: expectedText,
            success_tick_visible: tickVisible,
            verified_forward_recipient_count: verified ? 1 : 0
          };
        }
        """
        try:
            raw = page.evaluate(script, expected_recipient_name)
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        return {
            "recipient_picker_visible": bool(raw.get("recipient_picker_visible")),
            "forward_verified": bool(raw.get("forward_verified")),
            "send_success_verified": bool(raw.get("send_success_verified") or raw.get("forward_verified")),
            "verification_method": str(raw.get("verification_method") or ""),
            "verification_evidence": str(raw.get("verification_evidence") or ""),
            "remote_message_id": None,
            "success_toast_text": str(raw.get("success_toast_text") or ""),
            "expected_success_toast_text": str(raw.get("expected_success_toast_text") or ""),
            "success_tick_visible": bool(raw.get("success_tick_visible")),
            "verified_forward_recipient_count": int(raw.get("verified_forward_recipient_count") or 0),
        }

    def _wait_forward_success_state(self, page: Any, expected_recipient_name: str = "", timeout_ms: int = 1500) -> dict[str, Any]:
        started = time.perf_counter()
        state: dict[str, Any] = {}
        while (time.perf_counter() - started) * 1000 <= max(0, timeout_ms):
            state = self._forward_success_state(page, expected_recipient_name)
            if state.get("send_success_verified"):
                break
            _safe_wait_for_timeout(page, 150)
        if not state:
            state = self._forward_success_state(page, expected_recipient_name)
        return state

    def _forward_failure_debug(self, page: Any) -> dict[str, Any]:
        picker_text = ""
        try:
            picker_text = str(page.locator("div.anWA5J").first.inner_text(timeout=300) or "")[:2000]
        except Exception:
            try:
                picker_text = str(page.locator('[role="dialog"]').first.inner_text(timeout=300) or "")[:2000]
            except Exception:
                picker_text = ""
        return {
            "page_url": _safe_page_url(page),
            "visible_picker_text": picker_text,
        }

    def _page_debug_info(self, page: Any, account_id: str) -> dict[str, Any]:
        current_url = _safe_page_url(page)
        info: dict[str, Any] = {
            "current_url": current_url,
            "page_url": current_url,
            "page_title": _safe_page_title(page),
            "visible_text_sample": _visible_text_sample(page),
        }
        screenshot_path = _save_login_debug_screenshot(page, account_id)
        if screenshot_path:
            info["screenshot_path"] = screenshot_path
        return info

    def _return_to_chat_after_contact_save(self, page: Any, contact_naming_value: str, normalized_phone: str) -> dict[str, Any]:
        started = time.perf_counter()
        url_before = _safe_page_url(page)
        result: dict[str, Any] = {
            "step": "return_to_chat_after_contact_save",
            "status": "failed",
            "contact_naming_value": contact_naming_value,
            "normalized_phone": normalized_phone,
            "return_to_chat_url_before": url_before,
            "return_to_chat_url_after_goto": "",
            "return_to_chat_final_url": url_before,
            "return_to_chat_ready_confirmed": False,
            "return_to_chat_retry_used": False,
            "return_to_chat_contacts_ui_visible": False,
            "return_to_chat_main_chat_ui_visible": False,
            "return_to_chat_ready_selector": "",
            "return_to_chat_ready_attempts": [],
            "return_to_chat_wait_budget_ms": 5000,
            "return_to_chat_poll_interval_ms": 100,
            "return_to_chat_visible_text_sample": "",
            "return_to_chat_text_shell_seen": False,
            "return_to_chat_real_ready_seen": False,
        }

        modal_was_visible = self._is_contact_modal_visible(page)
        if modal_was_visible:
            self._close_contact_modal_if_open(page)

        ready_state = self._return_to_chat_ready_state(page, timeout_ms=300, poll_interval_ms=100)
        result.update(ready_state)
        if ready_state["return_to_chat_ready_confirmed"]:
            result.update({"status": "success", "mode": "already_ready", "duration_ms": int((time.perf_counter() - started) * 1000)})
            return result

        chat_url = f"{self.web_url}/chat"
        for attempt_index in range(2):
            try:
                self._goto_with_timeout(page, chat_url, timeout_ms=1000, wait_until="domcontentloaded")
                result["navigation"] = "goto_chat"
                if attempt_index == 0:
                    result["return_to_chat_url_after_goto"] = _safe_page_url(page)
                else:
                    result["return_to_chat_retry_used"] = True
            except Exception as exc:
                result["navigation_error"] = str(exc)

            if "/contacts" in _safe_page_url(page).lower() and attempt_index == 0:
                result["return_to_chat_retry_used"] = True
                continue
            ready_state = self._return_to_chat_ready_state(page, timeout_ms=5000, poll_interval_ms=100)
            result.update(ready_state)
            if ready_state["return_to_chat_ready_confirmed"]:
                result.update({"status": "success", "duration_ms": int((time.perf_counter() - started) * 1000)})
                return result
            if "/contacts" not in _safe_page_url(page).lower():
                break

        diagnostics = self._post_save_ui_diagnostics(page, contact_naming_value, normalized_phone, searched_value="")
        result.update(diagnostics)
        result["return_to_chat_final_url"] = _safe_page_url(page)
        result["return_to_chat_visible_text_sample"] = result.get("return_to_chat_visible_text_sample") or _visible_text_sample(page)
        result["duration_ms"] = int((time.perf_counter() - started) * 1000)
        result.update({"error_code": "main_chat_ui_not_ready", "failed_step": "return_to_chat_after_contact_save"})
        return result

    def _return_to_chat_ready_state(self, page: Any, timeout_ms: int = 1000, poll_interval_ms: int = 100) -> dict[str, Any]:
        deadline = time.monotonic() + (max(0, timeout_ms) / 1000)
        ready_selector = ""
        current_url = _safe_page_url(page)
        contacts_active = "/contacts" in current_url.lower()
        attempts: list[dict[str, Any]] = []
        chat_ready_selectors = [
            '[aria-label="Search-icon"]',
            'div:has(> [aria-label="Search-icon"])',
            'div:has(> svg[aria-label="Search-icon"])',
            '[aria-label="dialog-item"]',
            *selectors.SEARCH_ICON_SELECTORS,
            *selectors.TEXT_SEARCH_INPUT_SELECTORS,
            *selectors.MESSAGE_INPUT_SELECTORS,
            *selectors.CHAT_ITEM_SELECTORS,
            *_CHAT_LIST_SELECTORS,
            "[data-testid*='sidebar']",
            "[class*='sidebar']",
            "[data-testid*='conversation']",
            "[class*='conversation']",
            "[class*='chat-list']",
            "[class*='ChatList']",
        ]
        text_shell_seen = False
        while True:
            current_url = _safe_page_url(page)
            normalized_url = current_url.lower()
            contacts_active = "/contacts" in normalized_url
            ready_selector = self._first_visible_selector(
                page,
                chat_ready_selectors,
                timeout_ms=80,
            ) or ""
            chat_url_active = "/chat" in normalized_url and not contacts_active
            contacts_ui_visible = bool(self._contacts_ui_visible(page)) if contacts_active else False
            visible_text_sample = _visible_text_sample(page)
            text_shell_visible = self._bale_chat_shell_text_visible(visible_text_sample)
            text_shell_seen = bool(text_shell_seen or text_shell_visible)
            attempt = {
                "url": current_url,
                "chat_url_active": chat_url_active,
                "contacts_ui_visible": contacts_ui_visible,
                "ready_selector": ready_selector,
                "text_shell_visible": text_shell_visible,
                "real_ready_seen": bool(ready_selector),
                "visible_text_sample": visible_text_sample[:160],
            }
            attempts.append(attempt)
            if chat_url_active and ready_selector:
                return {
                    "page_url": current_url,
                    "ready_selector": ready_selector,
                    "main_chat_ui_visible": True,
                    "contacts_ui_visible": False,
                    "return_to_chat_final_url": current_url,
                    "return_to_chat_ready_confirmed": True,
                    "return_to_chat_contacts_ui_visible": False,
                    "return_to_chat_main_chat_ui_visible": True,
                    "return_to_chat_ready_selector": ready_selector,
                    "return_to_chat_ready_attempts": attempts,
                    "return_to_chat_wait_budget_ms": timeout_ms,
                    "return_to_chat_poll_interval_ms": poll_interval_ms,
                    "return_to_chat_visible_text_sample": visible_text_sample,
                    "return_to_chat_text_shell_seen": text_shell_seen,
                    "return_to_chat_real_ready_seen": True,
                }
            if time.monotonic() >= deadline:
                break
            time.sleep(max(0.01, poll_interval_ms / 1000))
        contacts_ui_visible = bool(self._contacts_ui_visible(page)) if contacts_active else False
        visible_text_sample = _visible_text_sample(page)
        return {
            "page_url": current_url,
            "ready_selector": ready_selector,
            "main_chat_ui_visible": bool(ready_selector and "/chat" in current_url.lower() and not contacts_active),
            "contacts_ui_visible": contacts_ui_visible,
            "return_to_chat_final_url": current_url,
            "return_to_chat_ready_confirmed": False,
            "return_to_chat_contacts_ui_visible": contacts_ui_visible,
            "return_to_chat_main_chat_ui_visible": bool(ready_selector and "/chat" in current_url.lower() and not contacts_active),
            "return_to_chat_ready_selector": ready_selector,
            "return_to_chat_ready_attempts": attempts,
            "return_to_chat_wait_budget_ms": timeout_ms,
            "return_to_chat_poll_interval_ms": poll_interval_ms,
            "return_to_chat_visible_text_sample": visible_text_sample,
            "return_to_chat_text_shell_seen": text_shell_seen or self._bale_chat_shell_text_visible(visible_text_sample),
            "return_to_chat_real_ready_seen": False,
        }

    def _bale_chat_shell_text_visible(self, visible_text_sample: str) -> bool:
        text = str(visible_text_sample or "")
        return any(token in text for token in ["\u06af\u0641\u062a\u06af\u0648", "\u0647\u0645\u0647", "\u0634\u062e\u0635\u06cc", "\u06af\u0631\u0648\u0647", "\u06a9\u0627\u0646\u0627\u0644", "\u0628\u0627\u0632\u0648"])

    def _target_already_open_state(self, page: Any, contact_naming_value: str) -> dict[str, Any]:
        header_text = self._chat_app_bar_text(page)
        message_input_visible = bool(self._first_visible_selector(page, selectors.MESSAGE_INPUT_SELECTORS, timeout_ms=150))
        name = str(contact_naming_value or "").strip().lower()
        detected = bool(name and name in header_text.lower() and message_input_visible)
        return {
            "target_already_open_detected": detected,
            "target_already_open_chat_app_bar_text": header_text,
            "target_already_open_message_input_visible": message_input_visible,
        }

    def _chat_app_bar_text(self, page: Any) -> str:
        try:
            value = page.evaluate(
                """() => {
                    const nodes = Array.from(document.querySelectorAll('[aria-label="ChatAppBar"], [aria-label*="ChatAppBar"], [class*="ChatAppBar"], [class*="chat-app-bar"]'));
                    for (const el of nodes) {
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        if (style && style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0) {
                            const text = (el.innerText || el.textContent || "").trim();
                            if (text) return text;
                        }
                    }
                    return "";
                }"""
            )
            if value:
                return str(value)
        except Exception:
            pass
        return ""

    def _open_target_chat(self, page: Any, contact_naming_value: str, normalized_phone: str) -> dict[str, Any]:
        started = time.perf_counter()
        search_attempts: list[dict[str, Any]] = []
        contact_name = str(contact_naming_value or "").strip()
        ready_guard_meta = {
            "open_target_chat_url_before_ready_guard": _safe_page_url(page),
            "open_target_chat_ready_guard_used": False,
            "open_target_chat_ready_guard_success": False,
            "open_target_chat_url_after_ready_guard": _safe_page_url(page),
            "open_target_chat_ready_guard_checks": [],
            "open_target_chat_recovered_from_contacts_count": 0,
            "open_target_chat_search_activation_url_before": "",
            "open_target_chat_search_activation_url_after": "",
            "open_target_chat_failed_due_to_contacts_page": False,
            "target_already_open_detected": False,
            "target_already_open_chat_app_bar_text": "",
            "target_already_open_message_input_visible": False,
            "qhfpb6_candidate_count": 0,
            "qhfpb6_candidate_debug": [],
            "qhfpb6_click_box": {},
            "qhfpb6_click_coordinates": {},
            "qhfpb6_click_confirmed": False,
            "broad_candidate_rejected_count": 0,
            "selected_candidate_reason": "",
            "contacts_fallback_skipped_reason": "",
            "normal_chat_list_scan_attempted": False,
            "normal_chat_list_candidate_count": 0,
            "normal_chat_list_candidate_debug": [],
            "normal_chat_list_click_box": {},
            "normal_chat_list_click_coordinates": {},
            "normal_chat_list_click_confirmed": False,
            "normal_chat_list_selector_used": "",
            "text_node_chat_list_candidate_count": 0,
            "search_activation_skipped_reason": "",
            "contacts_fallback_blocked_reason": "",
        }

        def ensure_chat_ready_for_search(reason: str) -> bool:
            url_before = _safe_page_url(page)
            contacts_ui_visible_before = self._contacts_ui_visible(page)
            guard_used = False
            guard_error = ""
            ready_state = self._return_to_chat_ready_state(page, timeout_ms=250, poll_interval_ms=100)
            if contacts_ui_visible_before or not ready_state.get("return_to_chat_ready_confirmed"):
                guard_used = True
                ready_guard_meta["open_target_chat_ready_guard_used"] = True
                if contacts_ui_visible_before:
                    ready_guard_meta["open_target_chat_recovered_from_contacts_count"] = int(ready_guard_meta["open_target_chat_recovered_from_contacts_count"]) + 1
                try:
                    self._goto_with_timeout(page, f"{self.web_url}/chat", timeout_ms=1000, wait_until="domcontentloaded")
                except Exception as exc:
                    guard_error = str(exc)
                    ready_guard_meta["open_target_chat_ready_guard_error"] = guard_error
                ready_state = self._return_to_chat_ready_state(page, timeout_ms=5000, poll_interval_ms=100)
            ready = bool(ready_state.get("return_to_chat_ready_confirmed"))
            url_after = _safe_page_url(page)
            contacts_ui_visible_after = self._contacts_ui_visible(page)
            ready_guard_meta["open_target_chat_ready_guard_success"] = bool(ready_guard_meta["open_target_chat_ready_guard_success"] or ready)
            ready_guard_meta["open_target_chat_url_after_ready_guard"] = url_after
            ready_guard_meta["open_target_chat_ready_guard_state"] = ready_state
            ready_guard_meta["open_target_chat_failed_due_to_contacts_page"] = bool(not ready and contacts_ui_visible_after)
            ready_guard_meta["open_target_chat_ready_guard_checks"].append(
                {
                    "reason": reason,
                    "url_before": url_before,
                    "guard_used": guard_used,
                    "url_after": url_after,
                    "ready": ready,
                    "contacts_ui_visible": contacts_ui_visible_after,
                    "error": guard_error,
                }
            )
            return ready

        def failed_open_result(details: dict[str, Any]) -> dict[str, Any]:
            result = {
                "step": "open_target_chat",
                "status": "failed",
                "searched_value": details.get("searched_value") or contact_naming_value or normalized_phone,
                "search_phase": details.get("search_phase") or "",
                "search_attempts": details.get("search_attempts") or search_attempts,
                "chat_query_attempts": details.get("chat_query_attempts") or details.get("search_attempts") or search_attempts,
                "contacts_fallback_attempted": bool(details.get("contacts_fallback_attempted", False)),
                "contacts_result_count": int(details.get("contacts_result_count") or 0),
                "matched_contact_text": str(details.get("matched_contact_text") or ""),
                "matched_candidate_text": str(details.get("matched_candidate_text") or ""),
                "clicked_result": bool(details.get("clicked_result", False)),
                "click_method": str(details.get("click_method") or ""),
                "click_attempts": details.get("click_attempts") or [],
                "chat_open_confirmed": bool(details.get("chat_open_confirmed", False)),
                "chat_open_confirmed_by": str(details.get("chat_open_confirmed_by") or ""),
                "normal_chat_list_candidate_count": int(details.get("normal_chat_list_candidate_count") or ready_guard_meta.get("normal_chat_list_candidate_count") or 0),
                "normal_chat_list_candidate_debug": details.get("normal_chat_list_candidate_debug") or ready_guard_meta.get("normal_chat_list_candidate_debug") or [],
                "target_already_open_detected": bool(details.get("target_already_open_detected", ready_guard_meta.get("target_already_open_detected", False))),
                "target_already_open_chat_app_bar_text": str(details.get("target_already_open_chat_app_bar_text") or ready_guard_meta.get("target_already_open_chat_app_bar_text") or ""),
                "target_already_open_message_input_visible": bool(details.get("target_already_open_message_input_visible", ready_guard_meta.get("target_already_open_message_input_visible", False))),
                "visible_text_sample": _visible_text_sample(page),
                "page_url": _safe_page_url(page),
                "current_url": _safe_page_url(page),
                "page_title": _safe_page_title(page),
                **details,
            }
            result["chat_query_attempts"] = result.get("chat_query_attempts") or result.get("search_attempts") or []
            return result

        if not ensure_chat_ready_for_search("open_target_chat_start"):
            return failed_open_result({
                "step": "open_target_chat",
                "status": "failed",
                "error_code": "main_chat_ui_not_ready",
                "search_attempts": search_attempts,
                "chat_open_confirmed": False,
                "duration_ms": int((time.perf_counter() - started) * 1000),
                **ready_guard_meta,
            })

        already_open = self._target_already_open_state(page, contact_naming_value)
        ready_guard_meta.update(already_open)
        if already_open["target_already_open_detected"]:
            ready_guard_meta["contacts_fallback_skipped_reason"] = "target_already_open"
            return {
                "step": "open_target_chat",
                "status": "success",
                "search_attempts": search_attempts,
                "chat_query_attempts": search_attempts,
                "searched_value": contact_naming_value,
                "search_phase": "already_open",
                "matched_candidate_text": already_open["target_already_open_chat_app_bar_text"],
                "matched_contact_text": already_open["target_already_open_chat_app_bar_text"],
                "chat_open_confirmed": True,
                "chat_open_confirmed_by": "target_already_open_chat_app_bar",
                "message_input_visible": already_open["target_already_open_message_input_visible"],
                "contacts_fallback_skipped_reason": "target_already_open",
                "duration_ms": int((time.perf_counter() - started) * 1000),
                **ready_guard_meta,
            }

        normal_list_result = (
            self._click_normal_chat_list_result(page, contact_naming_value)
            if "/chat/search" not in _safe_page_url(page).lower()
            else {
                "status": "failed",
                "visible_result": False,
                "normal_chat_list_candidate_count": 0,
                "normal_chat_list_candidate_debug": [],
                "normal_chat_list_click_box": {},
                "normal_chat_list_click_coordinates": {},
                "normal_chat_list_click_confirmed": False,
                "normal_chat_list_selector_used": "",
                "text_node_chat_list_candidate_count": 0,
            }
        )
        ready_guard_meta.update(
            {
                "normal_chat_list_scan_attempted": True,
                "normal_chat_list_candidate_count": normal_list_result.get("normal_chat_list_candidate_count", 0),
                "normal_chat_list_candidate_debug": normal_list_result.get("normal_chat_list_candidate_debug", []),
                "normal_chat_list_click_box": normal_list_result.get("normal_chat_list_click_box", {}),
                "normal_chat_list_click_coordinates": normal_list_result.get("normal_chat_list_click_coordinates", {}),
                "normal_chat_list_click_confirmed": normal_list_result.get("normal_chat_list_click_confirmed", False),
                "normal_chat_list_selector_used": normal_list_result.get("normal_chat_list_selector_used", ""),
                "text_node_chat_list_candidate_count": normal_list_result.get("text_node_chat_list_candidate_count", 0),
            }
        )
        if normal_list_result.get("status") == "success":
            ready_guard_meta["search_activation_skipped_reason"] = "normal_chat_list_result_clicked"
            ready_guard_meta["contacts_fallback_skipped_reason"] = "normal_chat_list_result_clicked"
            return {
                "step": "open_target_chat",
                "status": "success",
                "search_attempts": search_attempts,
                "chat_query_attempts": search_attempts,
                "searched_value": contact_naming_value,
                "search_phase": "normal_chat_list",
                "matched_candidate_text": normal_list_result.get("matched_contact_text", ""),
                "matched_contact_text": normal_list_result.get("matched_contact_text", ""),
                "clicked_result": True,
                "click_method": normal_list_result.get("click_method", ""),
                "click_attempts": normal_list_result.get("click_attempts", []),
                "chat_open_confirmed": True,
                "chat_open_confirmed_by": normal_list_result.get("chat_open_confirmed_by", ""),
                "contacts_fallback_skipped_reason": "normal_chat_list_result_clicked",
                "search_activation_skipped_reason": "normal_chat_list_result_clicked",
                "duration_ms": int((time.perf_counter() - started) * 1000),
                **ready_guard_meta,
            }
        if normal_list_result.get("visible_result"):
            ready_guard_meta["search_activation_skipped_reason"] = "normal_chat_list_result_click_not_confirmed"
            ready_guard_meta["contacts_fallback_blocked_reason"] = "normal_chat_list_result_visible"
            ready_guard_meta["contacts_fallback_skipped_reason"] = "normal_chat_list_result_visible"
            return failed_open_result({
                "step": "open_target_chat",
                "status": "failed",
                "error_code": "result_click_not_confirmed",
                "search_attempts": search_attempts,
                "chat_query_attempts": search_attempts,
                "searched_value": contact_naming_value,
                "search_phase": "normal_chat_list",
                "matched_candidate_text": normal_list_result.get("matched_contact_text", ""),
                "matched_contact_text": normal_list_result.get("matched_contact_text", ""),
                "clicked_result": bool(normal_list_result.get("clicked_result")),
                "click_method": normal_list_result.get("click_method", ""),
                "click_attempts": normal_list_result.get("click_attempts", []),
                "chat_open_confirmed": False,
                "contacts_fallback_blocked_reason": "normal_chat_list_result_visible",
                "contacts_fallback_skipped_reason": "normal_chat_list_result_visible",
                "duration_ms": int((time.perf_counter() - started) * 1000),
                **ready_guard_meta,
            })

        search_open = self._open_chat_search_from_main_ui(page, contact_naming_value, normalized_phone, ensure_chat_ready_for_search)
        search_input = str(search_open.get("search_input_selector") or "")
        if not search_input:
            if str(search_open.get("error_code") or "") == "main_chat_ui_not_ready":
                return failed_open_result({
                    "step": "open_target_chat",
                    "status": "failed",
                    "error_code": "main_chat_ui_not_ready",
                    "page_url": _safe_page_url(page),
                    "active_element": search_open.get("active_element") or self._active_element_info(page),
                    "activation_attempts": search_open.get("activation_attempts", []),
                    "search_attempts": search_attempts,
                    "chat_open_confirmed": False,
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                    **ready_guard_meta,
                })
            if not ensure_chat_ready_for_search("before_contacts_fallback"):
                return failed_open_result({
                    "step": "open_target_chat",
                    "status": "failed",
                    "error_code": "main_chat_ui_not_ready",
                    "page_url": _safe_page_url(page),
                    "active_element": search_open.get("active_element") or self._active_element_info(page),
                    "activation_attempts": search_open.get("activation_attempts", []),
                    "search_attempts": search_attempts,
                    "chat_open_confirmed": False,
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                    **ready_guard_meta,
                })
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
                    **ready_guard_meta,
                }
            diagnostics = self._post_save_ui_diagnostics(page, contact_naming_value, normalized_phone, searched_value=contact_naming_value or normalized_phone)
            return failed_open_result({
                "step": "open_target_chat",
                "status": "failed",
                "error_code": contacts_fallback.get("error_code") or "search_input_not_found",
                "page_url": _safe_page_url(page),
                "active_element": search_open.get("active_element") or self._active_element_info(page),
                "search_icon_visible": search_open.get("search_icon_visible", False),
                "search_icon_clicked": search_open.get("search_icon_clicked", ""),
                "activation_attempts": search_open.get("activation_attempts", []),
                "dom_diagnostics": self._compact_chat_search_dom_diagnostics(page),
                "search_attempts": [{"query": contact_naming_value or normalized_phone, "reason": "search_input_not_found"}],
                "contacts_fallback_attempted": True,
                "contacts_fallback": contacts_fallback,
                "contacts_query_attempts": contacts_fallback.get("contacts_query_attempts", []),
                "duration_ms": int((time.perf_counter() - started) * 1000),
                **ready_guard_meta,
                **diagnostics,
            })

        for query in [contact_naming_value, normalized_phone]:
            query = str(query or "").strip()
            if not query:
                continue
            is_name_query = bool(contact_name and query.lower() == contact_name.lower())
            attempt = {
                "query": query,
                "input_value": query,
                "matched": False,
                "search_phase": "chat_search_name" if is_name_query else "chat_search_phone",
                "name_result_visible": False,
                "matched_contact_text": "",
                "matched_result_selector": "",
                "matched_result_tag": "",
                "matched_result_role": "",
                "matched_result_class": "",
                "matched_result_box": {},
                "clickable_ancestor_selector": "",
                "clickable_ancestor_class": "",
                "clickable_ancestor_text": "",
                "clickable_ancestor_box": {},
                "rejected_broad_candidates": [],
                "clicked_result": False,
                "click_method": "",
                "click_attempts": [],
                "page_url_before_click": "",
                "page_url_after_click": "",
                "chat_open_confirmed": False,
                "chat_open_confirmed_by": "",
                "message_input_visible": False,
                "right_chat_header_text": "",
                "scoped_header_text": "",
                "right_header_text_source": "",
                "false_positive_confirmation_prevented": False,
                "phone_fallback_skipped_reason": "",
                "row_candidate_count": 0,
                "accepted_row_candidate_count": 0,
                "row_candidate_debug": [],
                "first_result_fallback_used": False,
                "first_result_fallback_selector": "",
                "first_result_fallback_reason": "",
                "text_node_fallback_used": False,
                "text_node_candidate_count": 0,
                "text_node_candidate_debug": [],
                "text_node_click_box": {},
                "text_node_click_coordinates": {},
                "text_node_rejection_reasons": [],
                "qhfpb6_candidate_count": 0,
                "qhfpb6_candidate_debug": [],
                "qhfpb6_click_box": {},
                "qhfpb6_click_coordinates": {},
                "qhfpb6_click_confirmed": False,
                "selected_candidate_reason": "",
            }
            search_attempts.append(attempt)
            if len(search_attempts) > 1:
                self._clear_search_input(page, search_input)
            ready_guard_meta["open_target_chat_search_activation_url_before"] = _safe_page_url(page)
            if not ensure_chat_ready_for_search("before_typing_search_query"):
                return failed_open_result({
                    "step": "open_target_chat",
                    "status": "failed",
                    "error_code": "main_chat_ui_not_ready",
                    "searched_value": query,
                    "search_phase": attempt["search_phase"],
                    "search_attempts": search_attempts,
                    "chat_query_attempts": search_attempts,
                    "chat_open_confirmed": False,
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                    **ready_guard_meta,
                })
            ready_guard_meta["open_target_chat_search_activation_url_after"] = _safe_page_url(page)
            self._fill_or_type(page, search_input, query)
            already_open = self._target_already_open_state(page, contact_naming_value)
            ready_guard_meta.update(already_open)
            if is_name_query and already_open["target_already_open_detected"]:
                return {
                    "step": "open_target_chat",
                    "status": "success",
                    "searched_value": query,
                    "search_phase": "already_open_after_query",
                    "name_result_visible": True,
                    "matched_candidate_text": already_open["target_already_open_chat_app_bar_text"],
                    "matched_contact_text": already_open["target_already_open_chat_app_bar_text"],
                    "search_attempts": search_attempts,
                    "chat_query_attempts": search_attempts,
                    "chat_open_confirmed": True,
                    "chat_open_confirmed_by": "target_already_open_chat_app_bar",
                    "message_input_visible": already_open["target_already_open_message_input_visible"],
                    "contacts_fallback_skipped_reason": "target_already_open",
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                    **ready_guard_meta,
                }
            qhfpb6_candidates = self._qhfpb6_search_result_candidates(page, query)
            qhfpb6_accepted = [
                item
                for item in qhfpb6_candidates
                if not item.get("rejected_reason")
                and (
                    str(item.get("accepted_reason") or "") == "qhfpb6_row_contains_contact"
                    or "qHFpb6" in str(item.get("className") or "")
                )
            ]
            attempt["qhfpb6_candidate_count"] = len(qhfpb6_accepted)
            attempt["qhfpb6_candidate_debug"] = self._search_candidate_debug(qhfpb6_candidates)
            ready_guard_meta["qhfpb6_candidate_count"] = len(qhfpb6_accepted)
            ready_guard_meta["qhfpb6_candidate_debug"] = attempt["qhfpb6_candidate_debug"]
            ready_guard_meta["broad_candidate_rejected_count"] = len([item for item in qhfpb6_candidates if item.get("rejected_reason")])
            if is_name_query and qhfpb6_accepted:
                qhfpb6_match = qhfpb6_accepted[0]
                qhfpb6_click = self._click_qhfpb6_candidate(page, qhfpb6_match, contact_naming_value)
                attempt["clicked_result"] = bool(qhfpb6_click.get("clicked_result"))
                attempt["click_method"] = str(qhfpb6_click.get("click_method") or "")
                attempt["click_attempts"] = qhfpb6_click.get("click_attempts", [])
                attempt["qhfpb6_click_box"] = qhfpb6_click.get("qhfpb6_click_box", {})
                attempt["qhfpb6_click_coordinates"] = qhfpb6_click.get("qhfpb6_click_coordinates", {})
                attempt["qhfpb6_click_confirmed"] = qhfpb6_click.get("status") == "success"
                attempt["selected_candidate_reason"] = str(qhfpb6_match.get("accepted_reason") or "qhfpb6_row_contains_contact")
                ready_guard_meta["qhfpb6_click_box"] = attempt["qhfpb6_click_box"]
                ready_guard_meta["qhfpb6_click_coordinates"] = attempt["qhfpb6_click_coordinates"]
                ready_guard_meta["qhfpb6_click_confirmed"] = attempt["qhfpb6_click_confirmed"]
                ready_guard_meta["selected_candidate_reason"] = attempt["selected_candidate_reason"]
                ready_guard_meta["contacts_fallback_skipped_reason"] = "qhfpb6_result_visible"
                confirmation_state = self._chat_open_confirmation_state(page, contact_naming_value, str(qhfpb6_match.get("text") or ""))
                confirmation_reason = str(qhfpb6_click.get("chat_open_confirmed_by") or confirmation_state.get("reason") or "")
                if confirmation_reason:
                    return {
                        "step": "open_target_chat",
                        "status": "success",
                        "searched_value": query,
                        "search_phase": attempt["search_phase"],
                        "name_result_visible": True,
                        "matched_selector": "",
                        "matched_result_selector": "",
                        "matched_candidate_text": str(qhfpb6_match.get("text") or ""),
                        "matched_contact_text": str(qhfpb6_match.get("text") or ""),
                        "clicked_result": True,
                        "click_method": attempt["click_method"],
                        "click_attempts": attempt["click_attempts"],
                        "search_attempts": search_attempts,
                        "chat_query_attempts": search_attempts,
                        "chat_open_confirmed": True,
                        "chat_open_confirmed_by": confirmation_reason,
                        "message_input_visible": bool(confirmation_state.get("message_input_visible") or self._first_visible_selector(page, selectors.MESSAGE_INPUT_SELECTORS, timeout_ms=100)),
                        "qhfpb6_candidate_count": attempt["qhfpb6_candidate_count"],
                        "qhfpb6_candidate_debug": attempt["qhfpb6_candidate_debug"],
                        "qhfpb6_click_box": attempt["qhfpb6_click_box"],
                        "qhfpb6_click_coordinates": attempt["qhfpb6_click_coordinates"],
                        "qhfpb6_click_confirmed": True,
                        "selected_candidate_reason": attempt["selected_candidate_reason"],
                        "contacts_fallback_skipped_reason": "qhfpb6_result_visible",
                        "duration_ms": int((time.perf_counter() - started) * 1000),
                        **ready_guard_meta,
                    }
                return {
                    "step": "open_target_chat",
                    "status": "failed",
                    "error_code": "result_click_not_confirmed",
                    "searched_value": query,
                    "search_phase": attempt["search_phase"],
                    "name_result_visible": True,
                    "matched_candidate_text": str(qhfpb6_match.get("text") or ""),
                    "matched_contact_text": str(qhfpb6_match.get("text") or ""),
                    "clicked_result": True,
                    "click_method": attempt["click_method"],
                    "click_attempts": attempt["click_attempts"],
                    "search_attempts": search_attempts,
                    "chat_query_attempts": search_attempts,
                    "chat_open_confirmed": False,
                    "qhfpb6_candidate_count": attempt["qhfpb6_candidate_count"],
                    "qhfpb6_candidate_debug": attempt["qhfpb6_candidate_debug"],
                    "qhfpb6_click_box": attempt["qhfpb6_click_box"],
                    "qhfpb6_click_coordinates": attempt["qhfpb6_click_coordinates"],
                    "qhfpb6_click_confirmed": False,
                    "selected_candidate_reason": attempt["selected_candidate_reason"],
                    "contacts_fallback_skipped_reason": "qhfpb6_result_visible",
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                    **ready_guard_meta,
                }
            candidates = self._collect_search_result_candidates(page, query=query, timeout_ms=900)
            rejected_broad_candidates = [item for item in candidates if self._is_broad_chat_search_candidate(item)]
            row_candidates = [
                item
                for item in candidates
                if not self._is_broad_chat_search_candidate(item)
                and not self._is_search_input_candidate(item)
                and self._is_likely_chat_search_row_candidate(item)
            ]
            accepted_row_candidates = [
                item
                for item in row_candidates
                if self._candidate_text_contains_query(item, query) or not is_name_query
            ]
            matchable_candidates = accepted_row_candidates
            name_visible_in_candidates = bool(
                is_name_query
                and contact_name
                and any(contact_name.lower() in str(item.get("text") or "").lower() for item in candidates)
            )
            match = self._match_search_result_candidate(matchable_candidates, query, contact_naming_value, normalized_phone)
            attempt["result_candidate_count"] = len(candidates)
            attempt["broad_candidate_count"] = len(rejected_broad_candidates)
            attempt["tight_candidate_count"] = len(matchable_candidates)
            attempt["row_candidate_count"] = len(row_candidates)
            attempt["accepted_row_candidate_count"] = len(accepted_row_candidates)
            attempt["candidate_debug"] = self._search_candidate_debug(candidates)
            attempt["row_candidate_debug"] = self._search_candidate_debug(row_candidates)
            attempt["result_candidates_text"] = [item.get("text", "") for item in candidates[:15]]
            attempt["normalized_candidates"] = [item.get("normalized_text", "") for item in candidates[:15]]
            attempt["rejected_broad_candidates"] = [
                {
                    "selector": item.get("selector", ""),
                    "text": item.get("text", ""),
                    "box": item.get("box", {}),
                    "reason": "broad_search_panel_text",
                }
                for item in rejected_broad_candidates[:10]
            ]
            if match:
                attempt["first_result_fallback_used"] = bool(accepted_row_candidates and match is accepted_row_candidates[0])
                attempt["first_result_fallback_selector"] = str(match.get("click_selector") or match.get("selector") or "")
                attempt["first_result_fallback_reason"] = "first_accepted_row_candidate" if attempt["first_result_fallback_used"] else str(match.get("accepted_reason") or "")
                attempt["name_result_visible"] = bool(is_name_query)
                attempt["matched_contact_text"] = str(match.get("text", ""))
                attempt["matched_result_selector"] = str(match.get("selector") or "")
                attempt["matched_result_tag"] = str(match.get("tag") or "")
                attempt["matched_result_role"] = str(match.get("role") or "")
                attempt["matched_result_class"] = str(match.get("className") or "")
                attempt["matched_result_box"] = match.get("box") or {}
                attempt["clickable_ancestor_selector"] = str(match.get("click_selector") or match.get("selector") or "")
                attempt["clickable_ancestor_class"] = str(match.get("clickClass") or "")
                attempt["clickable_ancestor_text"] = str(match.get("clickText") or match.get("text") or "")
                attempt["clickable_ancestor_box"] = match.get("clickBox") or match.get("box") or {}
                attempt["page_url_before_click"] = _safe_page_url(page)
                click_result = self._click_and_confirm_search_result(page, match, contact_naming_value)
                attempt["matched"] = click_result["status"] == "success"
                attempt["clicked_result"] = bool(click_result.get("clicked_result") or click_result.get("status") == "success")
                attempt["matched_selector"] = match.get("click_selector") or match.get("selector")
                attempt["matched_candidate_text"] = match.get("text", "")
                attempt["match_mode"] = match.get("match_mode", "normalized_text_match")
                attempt["click_method"] = click_result.get("click_method", "")
                attempt["click_attempts"] = click_result.get("click_attempts", [])
                attempt["page_url_after_click"] = _safe_page_url(page)
                confirmation_state = self._chat_open_confirmation_state(page, contact_naming_value, str(match.get("text") or ""))
                confirmation_reason = str(click_result.get("chat_open_confirmed_by") or confirmation_state.get("reason") or "")
                chat_opened = bool(confirmation_reason)
                attempt["chat_open_confirmed"] = chat_opened
                attempt["chat_open_confirmed_by"] = confirmation_reason
                attempt["message_input_visible"] = bool(self._first_visible_selector(page, selectors.MESSAGE_INPUT_SELECTORS, timeout_ms=100))
                attempt["right_chat_header_text"] = self._right_chat_header_text(page)
                attempt["scoped_header_text"] = str(confirmation_state.get("scoped_header_text") or "")
                attempt["right_header_text_source"] = str(confirmation_state.get("right_header_text_source") or "")
                attempt["false_positive_confirmation_prevented"] = bool(confirmation_state.get("false_positive_confirmation_prevented"))
                if chat_opened:
                    return {
                        "step": "open_target_chat",
                        "status": "success",
                        "searched_value": query,
                        "search_phase": attempt["search_phase"],
                        "name_result_visible": attempt["name_result_visible"],
                        "matched_selector": attempt["matched_selector"],
                        "matched_result_selector": attempt["matched_result_selector"],
                        "matched_result_tag": attempt["matched_result_tag"],
                        "matched_result_role": attempt["matched_result_role"],
                        "matched_result_class": attempt["matched_result_class"],
                        "matched_result_box": attempt["matched_result_box"],
                        "clickable_ancestor_selector": attempt["clickable_ancestor_selector"],
                        "clickable_ancestor_class": attempt["clickable_ancestor_class"],
                        "clickable_ancestor_text": attempt["clickable_ancestor_text"],
                        "clickable_ancestor_box": attempt["clickable_ancestor_box"],
                        "rejected_broad_candidates": attempt["rejected_broad_candidates"],
                        "result_candidate_count": attempt["result_candidate_count"],
                        "broad_candidate_count": attempt["broad_candidate_count"],
                        "tight_candidate_count": attempt["tight_candidate_count"],
                        "row_candidate_count": attempt["row_candidate_count"],
                        "accepted_row_candidate_count": attempt["accepted_row_candidate_count"],
                        "candidate_debug": attempt["candidate_debug"],
                        "row_candidate_debug": attempt["row_candidate_debug"],
                        "first_result_fallback_used": attempt["first_result_fallback_used"],
                        "first_result_fallback_selector": attempt["first_result_fallback_selector"],
                        "first_result_fallback_reason": attempt["first_result_fallback_reason"],
                        "matched_candidate_text": attempt["matched_candidate_text"],
                        "matched_contact_text": attempt["matched_contact_text"],
                        "clicked_result": attempt["clicked_result"],
                        "click_method": attempt["click_method"],
                        "click_attempts": attempt["click_attempts"],
                        "page_url_before_click": attempt["page_url_before_click"],
                        "page_url_after_click": attempt["page_url_after_click"],
                        "search_attempts": search_attempts,
                        "chat_query_attempts": search_attempts,
                        "chat_open_confirmed": True,
                        "chat_open_confirmed_by": confirmation_reason,
                        "message_input_visible": attempt["message_input_visible"],
                        "right_chat_header_text": attempt["right_chat_header_text"],
                        "scoped_header_text": attempt["scoped_header_text"],
                        "right_header_text_source": attempt["right_header_text_source"],
                        "false_positive_confirmation_prevented": attempt["false_positive_confirmation_prevented"],
                        "phone_fallback_skipped_reason": "name_result_opened" if is_name_query else "",
                        "duration_ms": int((time.perf_counter() - started) * 1000),
                        **ready_guard_meta,
                    }
                if is_name_query:
                    error_code = "chat_open_not_confirmed" if click_result.get("clicked_result") else "name_result_click_failed"
                    attempt["reason"] = error_code
                    attempt["phone_fallback_skipped_reason"] = "visible_name_result_not_confirmed"
                    return {
                        "step": "open_target_chat",
                        "status": "failed",
                        "error_code": error_code,
                        "searched_value": query,
                        "search_phase": attempt["search_phase"],
                        "name_result_visible": True,
                        "matched_selector": attempt["matched_selector"],
                        "matched_result_selector": attempt["matched_result_selector"],
                        "matched_result_tag": attempt["matched_result_tag"],
                        "matched_result_role": attempt["matched_result_role"],
                        "matched_result_class": attempt["matched_result_class"],
                        "matched_result_box": attempt["matched_result_box"],
                        "clickable_ancestor_selector": attempt["clickable_ancestor_selector"],
                        "clickable_ancestor_class": attempt["clickable_ancestor_class"],
                        "clickable_ancestor_text": attempt["clickable_ancestor_text"],
                        "clickable_ancestor_box": attempt["clickable_ancestor_box"],
                        "rejected_broad_candidates": attempt["rejected_broad_candidates"],
                        "result_candidate_count": attempt["result_candidate_count"],
                        "broad_candidate_count": attempt["broad_candidate_count"],
                        "tight_candidate_count": attempt["tight_candidate_count"],
                        "row_candidate_count": attempt["row_candidate_count"],
                        "accepted_row_candidate_count": attempt["accepted_row_candidate_count"],
                        "candidate_debug": attempt["candidate_debug"],
                        "row_candidate_debug": attempt["row_candidate_debug"],
                        "first_result_fallback_used": attempt["first_result_fallback_used"],
                        "first_result_fallback_selector": attempt["first_result_fallback_selector"],
                        "first_result_fallback_reason": attempt["first_result_fallback_reason"],
                        "matched_candidate_text": attempt["matched_candidate_text"],
                        "matched_contact_text": attempt["matched_contact_text"],
                        "clicked_result": attempt["clicked_result"],
                        "click_method": attempt["click_method"],
                        "click_attempts": attempt["click_attempts"],
                        "click_error": click_result.get("click_error", ""),
                        "dom_click_error": click_result.get("dom_click_error", ""),
                        "page_url_before_click": attempt["page_url_before_click"],
                        "page_url_after_click": attempt["page_url_after_click"],
                        "search_attempts": search_attempts,
                        "chat_query_attempts": search_attempts,
                        "chat_open_confirmed": False,
                        "chat_open_confirmed_by": "",
                        "message_input_visible": attempt["message_input_visible"],
                        "right_chat_header_text": attempt["right_chat_header_text"],
                        "scoped_header_text": attempt["scoped_header_text"],
                        "right_header_text_source": attempt["right_header_text_source"],
                        "false_positive_confirmation_prevented": attempt["false_positive_confirmation_prevented"],
                        "phone_fallback_skipped_reason": "visible_name_result_not_confirmed",
                        "duration_ms": int((time.perf_counter() - started) * 1000),
                        **ready_guard_meta,
                    }
                attempt["reason"] = "chat_open_not_confirmed"
            else:
                if is_name_query and name_visible_in_candidates:
                    if attempt["row_candidate_count"] == 0:
                        fallback_result = self._click_text_node_search_result_fallback(page, query, contact_naming_value)
                        attempt["text_node_fallback_used"] = bool(fallback_result.get("text_node_fallback_used"))
                        attempt["text_node_candidate_count"] = int(fallback_result.get("text_node_candidate_count") or 0)
                        attempt["text_node_candidate_debug"] = fallback_result.get("text_node_candidate_debug", [])
                        attempt["text_node_click_box"] = fallback_result.get("text_node_click_box", {})
                        attempt["text_node_click_coordinates"] = fallback_result.get("text_node_click_coordinates", {})
                        attempt["text_node_rejection_reasons"] = fallback_result.get("text_node_rejection_reasons", [])
                        attempt["click_attempts"] = fallback_result.get("click_attempts", [])
                        attempt["clicked_result"] = bool(fallback_result.get("clicked_result"))
                        attempt["click_method"] = str(fallback_result.get("click_method") or "")
                        attempt["page_url_before_click"] = str(fallback_result.get("page_url_before_click") or _safe_page_url(page))
                        attempt["page_url_after_click"] = _safe_page_url(page)
                        confirmation_state = self._chat_open_confirmation_state(page, contact_naming_value, str(fallback_result.get("matched_contact_text") or ""))
                        confirmation_reason = str(fallback_result.get("chat_open_confirmed_by") or confirmation_state.get("reason") or "")
                        attempt["chat_open_confirmed"] = bool(confirmation_reason)
                        attempt["chat_open_confirmed_by"] = confirmation_reason
                        attempt["message_input_visible"] = bool(confirmation_state.get("message_input_visible") or self._first_visible_selector(page, selectors.MESSAGE_INPUT_SELECTORS, timeout_ms=100))
                        attempt["right_chat_header_text"] = self._right_chat_header_text(page)
                        attempt["scoped_header_text"] = str(confirmation_state.get("scoped_header_text") or "")
                        attempt["right_header_text_source"] = str(confirmation_state.get("right_header_text_source") or "")
                        attempt["false_positive_confirmation_prevented"] = bool(confirmation_state.get("false_positive_confirmation_prevented"))
                        if confirmation_reason:
                            return {
                                "step": "open_target_chat",
                                "status": "success",
                                "searched_value": query,
                                "search_phase": attempt["search_phase"],
                                "name_result_visible": True,
                                "matched_selector": "",
                                "matched_result_selector": "",
                                "matched_result_tag": "",
                                "matched_result_role": "",
                                "matched_result_class": "",
                                "matched_result_box": attempt["text_node_click_box"],
                                "clickable_ancestor_selector": "",
                                "clickable_ancestor_class": "",
                                "clickable_ancestor_text": str(fallback_result.get("matched_contact_text") or ""),
                                "clickable_ancestor_box": attempt["text_node_click_box"],
                                "rejected_broad_candidates": attempt["rejected_broad_candidates"],
                                "result_candidate_count": attempt["result_candidate_count"],
                                "broad_candidate_count": attempt["broad_candidate_count"],
                                "tight_candidate_count": attempt["tight_candidate_count"],
                                "row_candidate_count": attempt["row_candidate_count"],
                                "accepted_row_candidate_count": attempt["accepted_row_candidate_count"],
                                "candidate_debug": attempt["candidate_debug"],
                                "row_candidate_debug": attempt["row_candidate_debug"],
                                "first_result_fallback_used": attempt["first_result_fallback_used"],
                                "first_result_fallback_selector": attempt["first_result_fallback_selector"],
                                "first_result_fallback_reason": attempt["first_result_fallback_reason"],
                                "text_node_fallback_used": attempt["text_node_fallback_used"],
                                "text_node_candidate_count": attempt["text_node_candidate_count"],
                                "text_node_candidate_debug": attempt["text_node_candidate_debug"],
                                "text_node_click_box": attempt["text_node_click_box"],
                                "text_node_click_coordinates": attempt["text_node_click_coordinates"],
                                "text_node_rejection_reasons": attempt["text_node_rejection_reasons"],
                                "matched_candidate_text": str(fallback_result.get("matched_contact_text") or ""),
                                "matched_contact_text": str(fallback_result.get("matched_contact_text") or ""),
                                "clicked_result": True,
                                "click_method": attempt["click_method"],
                                "click_attempts": attempt["click_attempts"],
                                "page_url_before_click": attempt["page_url_before_click"],
                                "page_url_after_click": attempt["page_url_after_click"],
                                "search_attempts": search_attempts,
                                "chat_query_attempts": search_attempts,
                                "chat_open_confirmed": True,
                                "chat_open_confirmed_by": confirmation_reason,
                                "message_input_visible": attempt["message_input_visible"],
                                "right_chat_header_text": attempt["right_chat_header_text"],
                                "scoped_header_text": attempt["scoped_header_text"],
                                "right_header_text_source": attempt["right_header_text_source"],
                                "false_positive_confirmation_prevented": attempt["false_positive_confirmation_prevented"],
                                "phone_fallback_skipped_reason": "name_result_opened",
                                "duration_ms": int((time.perf_counter() - started) * 1000),
                                **ready_guard_meta,
                            }
                        if fallback_result.get("clicked_result"):
                            attempt["reason"] = "chat_open_not_confirmed"
                            attempt["phone_fallback_skipped_reason"] = "visible_name_result_not_confirmed"
                            return {
                                "step": "open_target_chat",
                                "status": "failed",
                                "error_code": "chat_open_not_confirmed",
                                "searched_value": query,
                                "search_phase": attempt["search_phase"],
                                "name_result_visible": True,
                                "matched_contact_text": str(fallback_result.get("matched_contact_text") or ""),
                                "matched_result_selector": "",
                                "matched_result_box": attempt["text_node_click_box"],
                                "clickable_ancestor_selector": "",
                                "clickable_ancestor_class": "",
                                "clickable_ancestor_text": str(fallback_result.get("matched_contact_text") or ""),
                                "clickable_ancestor_box": attempt["text_node_click_box"],
                                "rejected_broad_candidates": attempt["rejected_broad_candidates"],
                                "result_candidate_count": attempt["result_candidate_count"],
                                "broad_candidate_count": attempt["broad_candidate_count"],
                                "tight_candidate_count": attempt["tight_candidate_count"],
                                "row_candidate_count": attempt["row_candidate_count"],
                                "accepted_row_candidate_count": attempt["accepted_row_candidate_count"],
                                "candidate_debug": attempt["candidate_debug"],
                                "row_candidate_debug": attempt["row_candidate_debug"],
                                "first_result_fallback_used": attempt["first_result_fallback_used"],
                                "first_result_fallback_selector": attempt["first_result_fallback_selector"],
                                "first_result_fallback_reason": attempt["first_result_fallback_reason"],
                                "text_node_fallback_used": attempt["text_node_fallback_used"],
                                "text_node_candidate_count": attempt["text_node_candidate_count"],
                                "text_node_candidate_debug": attempt["text_node_candidate_debug"],
                                "text_node_click_box": attempt["text_node_click_box"],
                                "text_node_click_coordinates": attempt["text_node_click_coordinates"],
                                "text_node_rejection_reasons": attempt["text_node_rejection_reasons"],
                                "click_attempts": attempt["click_attempts"],
                                "clicked_result": True,
                                "click_method": attempt["click_method"],
                                "page_url_before_click": attempt["page_url_before_click"],
                                "page_url_after_click": attempt["page_url_after_click"],
                                "search_attempts": search_attempts,
                                "chat_query_attempts": search_attempts,
                                "chat_open_confirmed": False,
                                "chat_open_confirmed_by": "",
                                "message_input_visible": attempt["message_input_visible"],
                                "right_chat_header_text": attempt["right_chat_header_text"],
                                "scoped_header_text": attempt["scoped_header_text"],
                                "right_header_text_source": attempt["right_header_text_source"],
                                "false_positive_confirmation_prevented": attempt["false_positive_confirmation_prevented"],
                                "phone_fallback_skipped_reason": "visible_name_result_not_confirmed",
                                "duration_ms": int((time.perf_counter() - started) * 1000),
                                **ready_guard_meta,
                            }
                    attempt["name_result_visible"] = True
                    attempt["reason"] = "result_row_not_found"
                    attempt["phone_fallback_skipped_reason"] = "visible_name_result_not_confirmed"
                    return {
                        "step": "open_target_chat",
                        "status": "failed",
                        "error_code": "result_row_not_found",
                        "searched_value": query,
                        "search_phase": attempt["search_phase"],
                        "name_result_visible": True,
                        "matched_contact_text": "",
                        "matched_result_selector": "",
                        "matched_result_box": {},
                        "clickable_ancestor_selector": "",
                        "clickable_ancestor_class": "",
                        "clickable_ancestor_text": "",
                        "clickable_ancestor_box": {},
                        "rejected_broad_candidates": attempt["rejected_broad_candidates"],
                        "result_candidate_count": attempt["result_candidate_count"],
                        "broad_candidate_count": attempt["broad_candidate_count"],
                        "tight_candidate_count": attempt["tight_candidate_count"],
                        "row_candidate_count": attempt["row_candidate_count"],
                        "accepted_row_candidate_count": attempt["accepted_row_candidate_count"],
                        "candidate_debug": attempt["candidate_debug"],
                        "row_candidate_debug": attempt["row_candidate_debug"],
                        "first_result_fallback_used": attempt["first_result_fallback_used"],
                        "first_result_fallback_selector": attempt["first_result_fallback_selector"],
                        "first_result_fallback_reason": attempt["first_result_fallback_reason"],
                        "text_node_fallback_used": attempt["text_node_fallback_used"],
                        "text_node_candidate_count": attempt["text_node_candidate_count"],
                        "text_node_candidate_debug": attempt["text_node_candidate_debug"],
                        "text_node_click_box": attempt["text_node_click_box"],
                        "text_node_click_coordinates": attempt["text_node_click_coordinates"],
                        "text_node_rejection_reasons": attempt["text_node_rejection_reasons"],
                        "click_attempts": [],
                        "clicked_result": False,
                        "click_method": "",
                        "page_url_before_click": _safe_page_url(page),
                        "page_url_after_click": _safe_page_url(page),
                        "search_attempts": search_attempts,
                        "chat_query_attempts": search_attempts,
                        "chat_open_confirmed": False,
                        "chat_open_confirmed_by": "",
                        "message_input_visible": bool(self._first_visible_selector(page, selectors.MESSAGE_INPUT_SELECTORS, timeout_ms=100)),
                        "right_chat_header_text": self._right_chat_header_text(page),
                        "scoped_header_text": "",
                        "right_header_text_source": "broad_search_panel_rejected",
                        "false_positive_confirmation_prevented": True,
                        "phone_fallback_skipped_reason": "visible_name_result_not_confirmed",
                        "duration_ms": int((time.perf_counter() - started) * 1000),
                        **ready_guard_meta,
                    }
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
                **ready_guard_meta,
            }
        return failed_open_result({
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
            "broad_candidate_count": last_attempt.get("broad_candidate_count", 0),
            "tight_candidate_count": last_attempt.get("tight_candidate_count", 0),
            "row_candidate_count": last_attempt.get("row_candidate_count", 0),
            "accepted_row_candidate_count": last_attempt.get("accepted_row_candidate_count", 0),
            "candidate_debug": last_attempt.get("candidate_debug", []),
            "row_candidate_debug": last_attempt.get("row_candidate_debug", []),
            "first_result_fallback_used": last_attempt.get("first_result_fallback_used", False),
            "first_result_fallback_selector": last_attempt.get("first_result_fallback_selector", ""),
            "first_result_fallback_reason": last_attempt.get("first_result_fallback_reason", ""),
            "text_node_fallback_used": last_attempt.get("text_node_fallback_used", False),
            "text_node_candidate_count": last_attempt.get("text_node_candidate_count", 0),
            "text_node_candidate_debug": last_attempt.get("text_node_candidate_debug", []),
            "text_node_click_box": last_attempt.get("text_node_click_box", {}),
            "text_node_click_coordinates": last_attempt.get("text_node_click_coordinates", {}),
            "text_node_rejection_reasons": last_attempt.get("text_node_rejection_reasons", []),
            "result_candidates_text": last_attempt.get("result_candidates_text", []),
            "matched_candidate_text": last_attempt.get("matched_candidate_text", ""),
            "normalized_candidates": last_attempt.get("normalized_candidates", []),
            "visible_no_result_text": self._visible_no_result_text(page),
            "search_url": _safe_page_url(page),
            "active_element": self._active_element_info(page),
            "search_input_placeholder": self._locator_attribute(page, search_input, "placeholder"),
            "duration_ms": int((time.perf_counter() - started) * 1000),
            **ready_guard_meta,
            **self._post_save_ui_diagnostics(page, contact_naming_value, normalized_phone, searched_value=searched_value),
        })

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
        install_ack = self._first_visible_selector(
            page,
            ["text=Ù…ØªÙˆØ¬Ù‡ Ø´Ø¯Ù…", "button:has-text('Ù…ØªÙˆØ¬Ù‡ Ø´Ø¯Ù…')"],
            timeout_ms=750,
        )
        if install_ack:
            self._click_if_possible(page, install_ack)
            _safe_wait_for_timeout(page, 500)
            try:
                if "/contacts" not in _safe_page_url(page).lower():
                    page.goto(contacts_url, wait_until="load")
            except Exception as exc:
                result["install_prompt_contacts_navigation_error"] = str(exc)
            add_contact_step("dismiss_install_prompt", "success", step_started, selector=install_ack, current_url=_safe_page_url(page))

        step_started = time.perf_counter()
        contacts_ready = self._first_visible_selector(page, selectors.CONTACTS_UI_READY_SELECTORS, timeout_ms=1500)
        if not contacts_ready:
            login_state = self._detect_login_state(page, timeout_ms=1500)
            if login_state.get("install_prompt_detected"):
                result["error_code"] = "bale_install_prompt"
                result["reason"] = "install_prompt_visible"
                result["message"] = "Bale install/help prompt is still visible"
                result["login_check"] = login_state
                add_contact_step("wait_contacts_ui", "failed", step_started, error_code="bale_install_prompt", login_check=login_state)
                return result
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
        modal = self._first_visible_selector(page, selectors.ADD_CONTACT_MODAL_SELECTORS, timeout_ms=2000)
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
        confirmation = self._confirm_contact_saved(page, modal, contact_name, normalized_phone, phone_value, timeout_ms=1500)
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
                    locator.wait_for(state="visible", timeout=500)
                    placeholder = str(locator.get_attribute("placeholder", timeout=500) or "")
                    input_type = str(locator.get_attribute("type", timeout=500) or "text")
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
            if any(token in placeholder for token in ("Name", "required", "Ù†Ø§Ù…")):
                return selector
        remaining = [item.get("selector", "") for item in modal_inputs if item.get("selector") != exclude_selector]
        if len(remaining) >= 2:
            return remaining[1]
        return remaining[0] if remaining else ""

    def _click_enabled_add_contact_button(self, page: Any) -> dict[str, Any]:
        button_selectors = [
            '.ReactModal__Overlay button:has-text("Ø§ÙØ²ÙˆØ¯Ù†")',
            '.ReactModal__Overlay button:has-text("Add")',
            '.ReactModal__Overlay [role="button"]:has-text("Ø§ÙØ²ÙˆØ¯Ù†")',
            '.ReactModal__Overlay [role="button"]:has-text("Add")',
            '.ReactModal__Content button:has-text("Ø§ÙØ²ÙˆØ¯Ù†")',
            '.ReactModal__Content button:has-text("Add")',
            '.ReactModal__Content [role="button"]:has-text("Ø§ÙØ²ÙˆØ¯Ù†")',
            '.ReactModal__Content [role="button"]:has-text("Add")',
            '[role="dialog"] button:has-text("Ø§ÙØ²ÙˆØ¯Ù†")',
            '[role="dialog"] button:has-text("Add")',
            '[role="dialog"] [role="button"]:has-text("Ø§ÙØ²ÙˆØ¯Ù†")',
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
                locator.click(timeout=1000)
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
            'text=/.*Ù‚Ø¨Ù„Ø§.*/',
            'text=/.*Ù…ÙˆØ¬ÙˆØ¯.*/',
            'text=/.*ØªÚ©Ø±Ø§Ø±ÛŒ.*/',
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
                ".ReactModal__Content button:has-text('Ã—')",
                ".ReactModal__Content button:has-text('Cancel')",
                ".ReactModal__Content button:has-text('Ù„ØºÙˆ')",
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

    def _open_chat_search_from_main_ui(self, page: Any, contact_naming_value: str, normalized_phone: str, ensure_chat_ready: Any | None = None) -> dict[str, Any]:
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
            if ensure_chat_ready is not None and not ensure_chat_ready(f"before_{mode}"):
                attempts.append({"mode": mode, "status": "failed", "error_code": "main_chat_ui_not_ready"})
                result["error_code"] = "main_chat_ui_not_ready"
                return ""
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
            if ensure_chat_ready is not None and not ensure_chat_ready("after_click_search_icon"):
                result.update(
                    {
                        "error_code": "main_chat_ui_not_ready",
                        "duration_ms": int((time.perf_counter() - started) * 1000),
                        "active_element": self._active_element_info(page),
                    }
                )
                return result
            search_input = check_input("after_search_icon", timeout_ms=350)
            if search_input:
                result.update({"status": "success", "search_input_selector": search_input, "duration_ms": int((time.perf_counter() - started) * 1000)})
                return result

        recovered_from_contacts = False
        for shortcut in ["Control+K", "Control+F"]:
            if int((time.perf_counter() - started) * 1000) >= 1500 and not recovered_from_contacts:
                break
            try:
                self._press_key(page, shortcut)
                attempts.append({"mode": "keyboard_shortcut", "status": "success", "shortcut": shortcut})
            except Exception as exc:
                attempts.append({"mode": "keyboard_shortcut", "status": "failed", "shortcut": shortcut, "error": str(exc)})
            url_after_shortcut = _safe_page_url(page).lower()
            if ensure_chat_ready is not None and not ensure_chat_ready(f"after_{shortcut}"):
                result.update(
                    {
                        "error_code": "main_chat_ui_not_ready",
                        "duration_ms": int((time.perf_counter() - started) * 1000),
                        "active_element": self._active_element_info(page),
                    }
                )
                return result
            recovered_from_contacts = bool("/contacts" in url_after_shortcut and "/contacts" not in _safe_page_url(page).lower())
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

    def _collect_search_result_candidates(self, page: Any, query: str = "", timeout_ms: int = 1500) -> list[dict[str, Any]]:
        deadline = time.monotonic() + (timeout_ms / 1000)
        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        while time.monotonic() < deadline:
            candidates = self._visible_search_result_candidates(page, query=query)
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

    def _visible_search_result_candidates(self, page: Any, query: str = "") -> list[dict[str, Any]]:
        query_literal = json.dumps(str(query or ""))
        try:
            script = (
                """() => {
                    const query = __QUERY_LITERAL__;
                    const queryLower = String(query || "").trim().toLowerCase();
                    const visible = (el) => {
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style && style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
                    };
                    const textOf = (el) => (el.innerText || el.textContent || "").trim();
                    const isSearchInput = (el) => {
                        if (!el) return false;
                        const tag = el.tagName || "";
                        const cls = String(el.className || "");
                        const role = el.getAttribute("role") || "";
                        return tag === "INPUT" || tag === "TEXTAREA" || role === "searchbox" || el.isContentEditable || cls.includes("e8AzTv");
                    };
                    const hasBroadText = (text) => {
                        return text.includes("Ø¨Ø±Ø§ÛŒ Ø´Ø±ÙˆØ¹ ÛŒÚ©ÛŒ Ø§Ø² Ú¯ÙØªÚ¯ÙˆÙ‡Ø§ Ø±Ø§ Ø§Ù†ØªØ®Ø§Ø¨ Ú©Ù†ÛŒØ¯")
                            || text.includes("Ã˜Â¨Ã˜Â±Ã˜Â§Ã›Å’ Ã˜Â´Ã˜Â±Ã™Ë†Ã˜Â¹ Ã›Å’ÃšÂ©Ã›Å’ Ã˜Â§Ã˜Â² ÃšÂ¯Ã™ÂÃ˜ÂªÃšÂ¯Ã™Ë†Ã™â€¡Ã˜Â§ Ã˜Â±Ã˜Â§ Ã˜Â§Ã™â€ Ã˜ÂªÃ˜Â®Ã˜Â§Ã˜Â¨ ÃšÂ©Ã™â€ Ã›Å’Ã˜Â¯")
                            || text.includes("Ú¯ÙØªÚ¯ÙˆÙ‡Ø§")
                            || text.includes("ÃšÂ¯Ã™ÂÃ˜ÂªÃšÂ¯Ã™Ë†Ã™â€¡Ã˜Â§");
                    };
                    const boxOf = (el) => {
                        const rect = el.getBoundingClientRect();
                        return {x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height)};
                    };
                    const clickableParent = (el) => {
                        let node = el;
                        for (let depth = 0; node && depth <= 5; depth += 1, node = node.parentElement) {
                            if (!(node instanceof HTMLElement)) continue;
                            const cls = String(node.className || "");
                            if (cls.includes("dialog-item-content")) return node;
                            if (cls.includes("qHFpb6")) return node;
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
                        const existing = el.getAttribute("data-clinicos-bale-result");
                        if (existing) return `[data-clinicos-bale-result="${existing}"]`;
                        const assigned = `r${Math.random().toString(36).slice(2)}`;
                        el.setAttribute("data-clinicos-bale-result", assigned);
                        return `[data-clinicos-bale-result="${assigned}"]`;
                    };
                    const rows = [];
                    const seen = new Set();
                    const isLikelyRow = (el) => {
                        if (!el) return false;
                        const cls = String(el.className || "");
                        return cls.includes("qHFpb6")
                            || cls.includes("dialog-item-content")
                            || el.getAttribute("aria-label") === "dialog-item";
                    };
                    const pushCandidate = (textEl, clickEl, acceptedReason) => {
                        if (!textEl || !clickEl || isSearchInput(textEl) || isSearchInput(clickEl)) return;
                        if (!visible(textEl) || !visible(clickEl)) return;
                        if (!isLikelyRow(clickEl)) return;
                        const text = textOf(clickEl) || textOf(textEl);
                        if (!text || !queryLower || !text.toLowerCase().includes(queryLower)) return;
                        if (hasBroadText(text)) return;
                        const clickBox = boxOf(clickEl);
                        if (!clickBox.w || !clickBox.h || clickBox.w > 650 || clickBox.h > 160) return;
                        const selector = selectorFor(textEl);
                        const clickSelector = selectorFor(clickEl);
                        const key = `${selector}|${clickSelector}|${text}`;
                        if (seen.has(key)) return;
                        seen.add(key);
                        rows.push({
                            selector,
                            click_selector: clickSelector,
                            text,
                            tag: textEl.tagName || "",
                            className: textEl.className || "",
                            role: textEl.getAttribute("role") || "",
                            ariaLabel: textEl.getAttribute("aria-label") || "",
                            clickTag: clickEl.tagName || "",
                            clickClass: clickEl.className || "",
                            clickRole: clickEl.getAttribute("role") || "",
                            clickText: text,
                            box: boxOf(textEl),
                            clickBox,
                            accepted_reason: acceptedReason
                        });
                    };
                    for (const nameEl of Array.from(document.querySelectorAll(".oUKPfP"))) {
                        if (isSearchInput(nameEl)) continue;
                        const text = textOf(nameEl);
                        if (!queryLower || text.toLowerCase() !== queryLower) continue;
                        const dialogRow = nameEl.closest(".dialog-item-content");
                        const qRow = nameEl.closest(".qHFpb6");
                        pushCandidate(nameEl, dialogRow, "exact_oUKPfP_dialog_item_content");
                        pushCandidate(nameEl, qRow, "exact_oUKPfP_qHFpb6");
                    }
                    if (rows.length) return rows;
                    for (const row of Array.from(document.querySelectorAll('div.qHFpb6, div.z8DuPl.I2osyO.dialog-item-content, [class*="dialog-item-content"], [class*="qHFpb6"], [aria-label="dialog-item"]'))) {
                        if (!visible(row) || isSearchInput(row)) continue;
                        const text = textOf(row);
                        if (!queryLower || !text.toLowerCase().includes(queryLower) || hasBroadText(text)) continue;
                        const nameEl = Array.from(row.querySelectorAll(".oUKPfP, div, span")).find((el) => visible(el) && !isSearchInput(el) && textOf(el).toLowerCase() === queryLower) || row;
                        pushCandidate(nameEl, row, String(row.className || "").includes("dialog-item-content") ? "dialog_item_content_contains_name" : "qHFpb6_contains_name");
                    }
                    return rows;
                }"""
            ).replace("__QUERY_LITERAL__", query_literal)
            data = page.evaluate(script)
            if isinstance(data, list):
                dynamic_candidates = [
                    self._candidate_with_normalized_text(item)
                    for item in data
                    if isinstance(item, dict) and not self._is_search_input_candidate(item)
                ]
                if dynamic_candidates:
                    return dynamic_candidates
        except Exception:
            pass
        return self._selector_search_result_candidates(page)

    def _selector_search_result_candidates(self, page: Any) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        fallback_selectors = [
            "div:nth-child(2) > div > .qHFpb6 > .ZGzps0",
            *selectors.SEARCH_RESULT_CANDIDATE_SELECTORS,
        ]
        for selector in fallback_selectors:
            try:
                locator = page.locator(selector).first
                locator.wait_for(state="visible", timeout=150)
                text = str(locator.inner_text(timeout=150) or "").strip()
                box = locator.bounding_box(timeout=150)
            except Exception:
                continue
            if not text:
                continue
            if not box:
                continue
            width = float(box.get("w", box.get("width", 0)) or 0)
            height = float(box.get("h", box.get("height", 0)) or 0)
            if width <= 0 or height <= 0:
                continue
            candidates.append(
                self._candidate_with_normalized_text(
                    {
                        "selector": selector,
                        "click_selector": selector,
                        "text": text,
                        "box": box,
                        "clickBox": box,
                        "accepted_reason": "codegen_fallback_selector" if selector == fallback_selectors[0] else "selector_fallback",
                    }
                )
            )
        return candidates[:30]

    def _normal_chat_list_candidates(self, page: Any, query: str) -> list[dict[str, Any]]:
        query_literal = json.dumps(str(query or ""))
        try:
            script = (
                """() => {
                    const query = __QUERY_LITERAL__;
                    const needle = String(query || "").trim().toLowerCase();
                    const viewportW = Math.max(document.documentElement.clientWidth || 0, window.innerWidth || 0);
                    const viewportH = Math.max(document.documentElement.clientHeight || 0, window.innerHeight || 0);
                    const boxOf = (el) => {
                        const rect = el.getBoundingClientRect();
                        return {x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height)};
                    };
                    const visible = (el) => {
                        if (!el || !(el instanceof HTMLElement)) return false;
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style && style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
                    };
                    const textOf = (el) => (el.innerText || el.textContent || "").trim();
                    const broadWords = ["Ú¯ÙØªÚ¯Ùˆ", "Ù…Ø¬Ù„Ù‡", "Ø®Ø¯Ù…Ø§Øª", "Ù…Ø®Ø§Ø·Ø¨ÛŒÙ†"];
                    const selectorFor = (el) => {
                        if (!el) return "";
                        const tag = el.tagName ? el.tagName.toLowerCase() : "*";
                        const aria = el.getAttribute("aria-label");
                        if (aria) return `${tag}[aria-label="${aria.replaceAll('"', '\\"')}"]`;
                        const role = el.getAttribute("role");
                        if (role) return `${tag}[role="${role}"]`;
                        const existing = el.getAttribute("data-clinicos-bale-chat-list");
                        if (existing) return `[data-clinicos-bale-chat-list="${existing}"]`;
                        const assigned = `n${Math.random().toString(36).slice(2)}`;
                        el.setAttribute("data-clinicos-bale-chat-list", assigned);
                        return `[data-clinicos-bale-chat-list="${assigned}"]`;
                    };
                    const findRow = (el) => {
                        let best = el;
                        for (let node = el; node && node instanceof HTMLElement && node !== document.body && node !== document.documentElement; node = node.parentElement) {
                            if (!visible(node)) continue;
                            const text = textOf(node);
                            if (!text || !text.toLowerCase().includes(needle)) continue;
                            const box = boxOf(node);
                            const aria = node.getAttribute("aria-label") || "";
                            const cls = String(node.className || "");
                            const rowSized = box.h >= 40 && box.h <= 120 && box.w >= 120 && box.w <= Math.min(520, viewportW * 0.55) && box.x <= Math.min(520, viewportW * 0.55);
                            if (aria === "dialog-item" || rowSized || cls.includes("dialog") || cls.includes("chat")) best = node;
                            if (aria === "dialog-item") break;
                        }
                        return best;
                    };
                    const rows = [];
                    const rejected = [];
                    const seen = new Set();
                    const sourceNodes = Array.from(document.querySelectorAll('[aria-label="dialog-item"], [role="listitem"], [data-testid*="chat"], [class*="chat"], [class*="dialog"], div, span'));
                    for (const el of sourceNodes) {
                        if (!visible(el)) continue;
                        const text = textOf(el);
                        if (!text || !needle || !text.toLowerCase().includes(needle)) continue;
                        const row = findRow(el);
                        const rowText = textOf(row);
                        const box = boxOf(row);
                        const cls = String(row.className || "");
                        const selector = selectorFor(row);
                        let rejected_reason = "";
                        if (box.w >= viewportW * 0.72 || box.h >= viewportH * 0.45) rejected_reason = "broad_container_box";
                        else if (broadWords.every((word) => rowText.includes(word))) rejected_reason = "broad_navigation_text";
                        else if (box.h < 24 || box.h > 140) rejected_reason = "not_row_sized";
                        if (rejected_reason) {
                            rejected.push({selector, text: rowText.slice(0, 220), className: cls, box, rejected_reason});
                            continue;
                        }
                        const key = `${box.x}|${box.y}|${box.w}|${box.h}|${rowText}`;
                        if (seen.has(key)) continue;
                        seen.add(key);
                        rows.push({selector, click_selector: selector, text: rowText, className: cls, box, clickBox: box, accepted_reason: row.getAttribute("aria-label") === "dialog-item" ? "dialog_item_contains_contact" : "left_chat_list_row_contains_contact"});
                    }
                    return rows.concat(rejected).slice(0, 30);
                }"""
            ).replace("__QUERY_LITERAL__", query_literal)
            data = page.evaluate(script)
            if isinstance(data, list):
                return [self._candidate_with_normalized_text(item) for item in data if isinstance(item, dict)]
        except Exception:
            pass
        return self._normal_chat_list_selector_candidates(page, query)

    def _normal_chat_list_selector_candidates(self, page: Any, query: str) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for selector in [*selectors.CHAT_ITEM_SELECTORS, *_CHAT_LIST_SELECTORS]:
            try:
                locator = page.locator(selector).first
                locator.wait_for(state="visible", timeout=120)
                text = str(locator.inner_text(timeout=120) or "").strip()
                box = locator.bounding_box(timeout=120)
            except Exception:
                continue
            if not text or str(query or "").lower() not in text.lower() or not box:
                continue
            width = float(box.get("w", box.get("width", 0)) or 0)
            height = float(box.get("h", box.get("height", 0)) or 0)
            if width <= 0 or height <= 0 or width > 650 or height > 180:
                continue
            candidates.append(self._candidate_with_normalized_text({"selector": selector, "click_selector": selector, "text": text, "box": box, "clickBox": box, "accepted_reason": "selector_chat_list_contains_contact"}))
        return candidates[:20]

    def _click_normal_chat_list_result(self, page: Any, contact_naming_value: str) -> dict[str, Any]:
        candidates = self._normal_chat_list_candidates(page, contact_naming_value)
        accepted = [item for item in candidates if not item.get("rejected_reason")]
        result: dict[str, Any] = {
            "status": "failed",
            "visible_result": bool(accepted),
            "clicked_result": False,
            "normal_chat_list_candidate_count": len(accepted),
            "normal_chat_list_candidate_debug": self._search_candidate_debug(candidates),
            "text_node_chat_list_candidate_count": len(accepted),
            "normal_chat_list_click_box": {},
            "normal_chat_list_click_coordinates": {},
            "normal_chat_list_click_confirmed": False,
            "normal_chat_list_selector_used": "",
            "matched_contact_text": "",
            "click_method": "",
            "click_attempts": [],
        }
        if not accepted:
            return result
        candidate = accepted[0]
        box = candidate.get("clickBox") or candidate.get("box") or {}
        width = float(box.get("w", box.get("width", 0)) or 0)
        height = float(box.get("h", box.get("height", 0)) or 0)
        if width <= 0 or height <= 0:
            return result
        x = float(box.get("x", 0)) + width / 2
        y = float(box.get("y", 0)) + height / 2
        before_url = _safe_page_url(page)
        try:
            page.mouse.click(x, y)
            result["clicked_result"] = True
            result["click_method"] = "mouse_click_normal_chat_list"
            result["normal_chat_list_click_box"] = box
            result["normal_chat_list_click_coordinates"] = {"x": x, "y": y}
            result["normal_chat_list_selector_used"] = str(candidate.get("click_selector") or candidate.get("selector") or "")
            result["matched_contact_text"] = str(candidate.get("text") or "")
            _safe_wait_for_timeout(page, 300)
        except Exception as exc:
            result["click_attempts"] = [{"method": "mouse_click_normal_chat_list", "before_url": before_url, "after_url": _safe_page_url(page), "success": False, "error": str(exc), "box": box, "coordinates": {"x": x, "y": y}}]
            return result
        confirmation_state = self._chat_open_confirmation_state(page, contact_naming_value, str(candidate.get("text") or ""))
        confirmation = str(confirmation_state.get("reason") or "")
        result["normal_chat_list_click_confirmed"] = bool(confirmation)
        result["chat_open_confirmed_by"] = confirmation
        result["click_attempts"] = [{"method": "mouse_click_normal_chat_list", "before_url": before_url, "after_url": _safe_page_url(page), "success": bool(confirmation), "chat_open_confirmed_by": confirmation, "box": box, "coordinates": {"x": x, "y": y}}]
        if confirmation:
            result["status"] = "success"
        return result

    def _qhfpb6_search_result_candidates(self, page: Any, query: str) -> list[dict[str, Any]]:
        query_literal = json.dumps(str(query or ""))
        try:
            script = (
                """() => {
                    const query = __QUERY_LITERAL__;
                    const needle = String(query || "").trim().toLowerCase();
                    const viewportW = Math.max(document.documentElement.clientWidth || 0, window.innerWidth || 0);
                    const viewportH = Math.max(document.documentElement.clientHeight || 0, window.innerHeight || 0);
                    const boxOf = (el) => {
                        const rect = el.getBoundingClientRect();
                        return {x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height)};
                    };
                    const visible = (el) => {
                        if (!el || !(el instanceof HTMLElement)) return false;
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style && style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
                    };
                    const textOf = (el) => (el.innerText || el.textContent || "").trim();
                    const broadWords = ["Ú¯ÙØªÚ¯Ùˆ", "Ù…Ø¬Ù„Ù‡", "Ø®Ø¯Ù…Ø§Øª", "Ù…Ø®Ø§Ø·Ø¨ÛŒÙ†"];
                    const rows = [];
                    const rejected = [];
                    for (const row of Array.from(document.querySelectorAll(".qHFpb6, [class*='qHFpb6']"))) {
                        if (!visible(row)) continue;
                        const text = textOf(row);
                        const box = boxOf(row);
                        const cls = String(row.className || "");
                        let rejected_reason = "";
                        if (!text || !needle || !text.toLowerCase().includes(needle)) rejected_reason = "text_mismatch";
                        else if (box.w >= viewportW * 0.72 || box.h >= viewportH * 0.45) rejected_reason = "broad_container_box";
                        else if (broadWords.every((word) => text.includes(word))) rejected_reason = "broad_navigation_text";
                        if (rejected_reason) {
                            rejected.push({text: text.slice(0, 220), className: cls, box, rejected_reason});
                            continue;
                        }
                        rows.push({text, className: cls, box, clickBox: box, accepted_reason: "qhfpb6_row_contains_contact"});
                    }
                    return rows.concat(rejected).slice(0, 30);
                }"""
            ).replace("__QUERY_LITERAL__", query_literal)
            data = page.evaluate(script)
            if isinstance(data, list):
                return [self._candidate_with_normalized_text(item) for item in data if isinstance(item, dict)]
        except Exception:
            pass
        return []

    def _click_qhfpb6_candidate(self, page: Any, candidate: dict[str, Any], contact_naming_value: str) -> dict[str, Any]:
        box = candidate.get("clickBox") or candidate.get("box") or {}
        width = float(box.get("w", box.get("width", 0)) or 0)
        height = float(box.get("h", box.get("height", 0)) or 0)
        if width <= 0 or height <= 0:
            return {"status": "failed", "clicked_result": False, "error": "qhfpb6_box_missing"}
        x = float(box.get("x", 0)) + width / 2
        y = float(box.get("y", 0)) + height / 2
        before_url = _safe_page_url(page)
        try:
            page.mouse.click(x, y)
            _safe_wait_for_timeout(page, 300)
        except Exception as exc:
            return {"status": "failed", "clicked_result": False, "error": str(exc), "qhfpb6_click_box": box, "qhfpb6_click_coordinates": {"x": x, "y": y}}
        confirmation_state = self._chat_open_confirmation_state(page, contact_naming_value, str(candidate.get("text") or ""))
        confirmation = str(confirmation_state.get("reason") or "")
        return {
            "status": "success" if confirmation else "failed",
            "clicked_result": True,
            "click_method": "mouse_click_qhfpb6",
            "chat_open_confirmed_by": confirmation,
            "qhfpb6_click_box": box,
            "qhfpb6_click_coordinates": {"x": x, "y": y},
            "click_attempts": [
                {
                    "method": "mouse_click_qhfpb6",
                    "before_url": before_url,
                    "after_url": _safe_page_url(page),
                    "success": bool(confirmation),
                    "chat_open_confirmed_by": confirmation,
                    "box": box,
                    "coordinates": {"x": x, "y": y},
                }
            ],
        }

    def _click_text_node_search_result_fallback(self, page: Any, query: str, contact_naming_value: str) -> dict[str, Any]:
        result: dict[str, Any] = {
            "text_node_fallback_used": False,
            "text_node_candidate_count": 0,
            "text_node_candidate_debug": [],
            "text_node_click_box": {},
            "text_node_click_coordinates": {},
            "text_node_rejection_reasons": [],
            "clicked_result": False,
            "click_method": "",
            "click_attempts": [],
            "matched_contact_text": "",
            "page_url_before_click": _safe_page_url(page),
            "chat_open_confirmed_by": "",
        }
        candidates = self._text_node_search_result_candidates(page, query)
        accepted = [item for item in candidates if not item.get("rejected_reason")]
        result["text_node_candidate_count"] = len(accepted)
        result["text_node_candidate_debug"] = candidates[:20]
        result["text_node_rejection_reasons"] = [
            {"reason": item.get("rejected_reason", ""), "text": item.get("text", ""), "box": item.get("box", {})}
            for item in candidates
            if item.get("rejected_reason")
        ][:20]
        if not accepted:
            return result

        candidate = accepted[0]
        box = candidate.get("clickBox") or candidate.get("box") or {}
        width = float(box.get("w", box.get("width", 0)) or 0)
        height = float(box.get("h", box.get("height", 0)) or 0)
        if width <= 0 or height <= 0:
            return result
        x = float(box.get("x", 0)) + width / 2
        y = float(box.get("y", 0)) + height / 2
        result["text_node_fallback_used"] = True
        result["text_node_click_box"] = box
        result["text_node_click_coordinates"] = {"x": x, "y": y}
        result["matched_contact_text"] = str(candidate.get("text") or "")
        before_url = _safe_page_url(page)
        try:
            page.mouse.click(x, y)
            _safe_wait_for_timeout(page, 300)
            confirmation_state = self._chat_open_confirmation_state(page, contact_naming_value, str(candidate.get("text") or ""))
            confirmation = str(confirmation_state.get("reason") or "")
            result["clicked_result"] = True
            result["click_method"] = "mouse_click_text_node_fallback"
            result["chat_open_confirmed_by"] = confirmation
            result["click_attempts"] = [
                {
                    "method": "mouse_click_text_node_fallback",
                    "selector": "",
                    "before_url": before_url,
                    "after_url": _safe_page_url(page),
                    "success": bool(confirmation),
                    "error": "",
                    "chat_open_confirmed_by": confirmation,
                    "message_input_visible": bool(confirmation_state.get("message_input_visible")),
                    "scoped_header_text": confirmation_state.get("scoped_header_text", ""),
                    "right_header_text_source": confirmation_state.get("right_header_text_source", ""),
                    "false_positive_confirmation_prevented": bool(confirmation_state.get("false_positive_confirmation_prevented")),
                    "coordinates": result["text_node_click_coordinates"],
                    "box": box,
                }
            ]
        except Exception as exc:
            result["click_attempts"] = [
                {
                    "method": "mouse_click_text_node_fallback",
                    "selector": "",
                    "before_url": before_url,
                    "after_url": _safe_page_url(page),
                    "success": False,
                    "error": str(exc),
                    "coordinates": result["text_node_click_coordinates"],
                    "box": box,
                }
            ]
        return result

    def _text_node_search_result_candidates(self, page: Any, query: str) -> list[dict[str, Any]]:
        query_literal = json.dumps(str(query or ""))
        try:
            script = (
                """() => {
                    const query = __QUERY_LITERAL__;
                    const needle = String(query || "").trim().toLowerCase();
                    const viewportW = Math.max(document.documentElement.clientWidth || 0, window.innerWidth || 0);
                    const viewportH = Math.max(document.documentElement.clientHeight || 0, window.innerHeight || 0);
                    const broadWords = ["Ú¯ÙØªÚ¯Ùˆ", "Ù…Ø¬Ù„Ù‡", "Ø®Ø¯Ù…Ø§Øª", "Ù…Ø®Ø§Ø·Ø¨ÛŒÙ†"];
                    const badTags = new Set(["INPUT", "TEXTAREA", "SCRIPT", "STYLE", "SVG", "PATH"]);
                    const textOf = (el) => (el.innerText || el.textContent || "").trim();
                    const boxOf = (el) => {
                        const rect = el.getBoundingClientRect();
                        return {x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height)};
                    };
                    const visible = (el) => {
                        if (!el || !(el instanceof HTMLElement)) return false;
                        if (badTags.has(el.tagName)) return false;
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        if (!style || style.display === "none" || style.visibility === "hidden" || Number(style.opacity || 1) === 0) return false;
                        if (rect.width <= 0 || rect.height <= 0) return false;
                        return rect.bottom >= 0 && rect.right >= 0 && rect.top <= viewportH && rect.left <= viewportW;
                    };
                    const isSearchInput = (el) => {
                        if (!el) return false;
                        const tag = el.tagName || "";
                        const cls = String(el.className || "");
                        const role = el.getAttribute("role") || "";
                        return tag === "INPUT" || tag === "TEXTAREA" || role === "searchbox" || el.isContentEditable || cls.includes("e8AzTv");
                    };
                    const isFullPage = (box) => {
                        if (!box || !viewportW || !viewportH) return false;
                        return box.w >= viewportW * 0.72 && box.h >= viewportH * 0.45;
                    };
                    const hasBroadNavigation = (text) => broadWords.every((word) => text.includes(word));
                    const normalized = (text) => String(text || "").trim().toLowerCase().replace(/\s+/g, " ");
                    const clickableParent = (el) => {
                        let best = el;
                        for (let node = el; node && node instanceof HTMLElement && node !== document.body && node !== document.documentElement; node = node.parentElement) {
                            if (!visible(node) || isSearchInput(node)) continue;
                            const text = textOf(node);
                            const box = boxOf(node);
                            if (!text || !normalized(text).includes(needle) || hasBroadNavigation(text) || isFullPage(box)) continue;
                            if (box.h >= 28 && box.h <= 140 && box.w >= 40 && box.w <= Math.min(760, viewportW * 0.72)) {
                                best = node;
                            }
                        }
                        return best;
                    };
                    const rows = [];
                    const rejected = [];
                    const seen = new Set();
                    for (const el of Array.from(document.querySelectorAll("body *"))) {
                        if (!visible(el)) continue;
                        const text = textOf(el);
                        if (!text || !needle || !normalized(text).includes(needle)) continue;
                        const baseBox = boxOf(el);
                        let rejectedReason = "";
                        if (isSearchInput(el)) rejectedReason = "search_input";
                        else if (hasBroadNavigation(text)) rejectedReason = "broad_navigation_text";
                        else if (isFullPage(baseBox)) rejectedReason = "full_page_container";
                        if (rejectedReason) {
                            rejected.push({text: text.slice(0, 220), box: baseBox, rejected_reason: rejectedReason});
                            continue;
                        }
                        const clickEl = clickableParent(el);
                        const clickText = textOf(clickEl);
                        const clickBox = boxOf(clickEl);
                        if (!clickText || !normalized(clickText).includes(needle)) {
                            rejected.push({text: text.slice(0, 220), box: baseBox, rejected_reason: "click_parent_text_mismatch"});
                            continue;
                        }
                        if (hasBroadNavigation(clickText) || isFullPage(clickBox) || clickBox.w <= 0 || clickBox.h <= 0) {
                            rejected.push({text: clickText.slice(0, 220), box: clickBox, rejected_reason: hasBroadNavigation(clickText) ? "broad_navigation_text" : "bad_click_box"});
                            continue;
                        }
                        const key = `${clickBox.x}|${clickBox.y}|${clickBox.w}|${clickBox.h}|${normalized(clickText)}`;
                        if (seen.has(key)) continue;
                        seen.add(key);
                        const exact = normalized(text) === needle || normalized(clickText) === needle;
                        const picture = text.includes("ØªØµÙˆÛŒØ±") || clickText.includes("ØªØµÙˆÛŒØ±");
                        rows.push({
                            text: clickText.slice(0, 220),
                            box: baseBox,
                            clickBox,
                            tag: el.tagName || "",
                            clickTag: clickEl.tagName || "",
                            exact,
                            picture,
                            score: (exact ? 0 : 1000) + (picture ? 0 : 100) + Math.round(clickBox.w * clickBox.h)
                        });
                    }
                    rows.sort((a, b) => a.score - b.score);
                    return rows.slice(0, 15).concat(rejected.slice(0, 15));
                }"""
            ).replace("__QUERY_LITERAL__", query_literal)
            data = page.evaluate(script)
            if isinstance(data, list):
                return [item for item in data if isinstance(item, dict)]
        except Exception:
            pass
        return []

    def _is_search_input_candidate(self, candidate: dict[str, Any]) -> bool:
        tag = str(candidate.get("tag") or "").upper()
        class_name = str(candidate.get("className") or "")
        selector = str(candidate.get("selector") or "")
        role = str(candidate.get("role") or "").lower()
        return tag in {"INPUT", "TEXTAREA"} or role == "searchbox" or "e8AzTv" in class_name or "input" in selector.lower()

    def _candidate_with_normalized_text(self, item: dict[str, Any]) -> dict[str, Any]:
        text = str(item.get("text") or "")
        item["normalized_text"] = _normalize_bale_match_text(text)
        item["normalized_phone_text"] = _normalize_bale_phone_text(text)
        return item

    def _search_candidate_debug(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        debug: list[dict[str, Any]] = []
        for item in candidates[:30]:
            rejected_reason = str(item.get("rejected_reason") or "")
            if not rejected_reason and self._is_broad_chat_search_candidate(item):
                rejected_reason = "broad_search_panel_text"
            accepted_reason = str(item.get("accepted_reason") or "")
            if not rejected_reason and not accepted_reason:
                accepted_reason = "matchable_candidate"
            debug.append(
                {
                    "selector": item.get("selector", ""),
                    "text": item.get("text", ""),
                    "class": item.get("className", ""),
                    "role": item.get("role", ""),
                    "box": item.get("box", {}),
                    "rejected_reason": rejected_reason,
                    "accepted_reason": "" if rejected_reason else accepted_reason,
                }
            )
        return debug

    def _is_broad_chat_search_candidate(self, candidate: dict[str, Any]) -> bool:
        if candidate.get("rejected_reason"):
            return True
        text = str(candidate.get("text") or "")
        normalized = _normalize_bale_match_text(text)
        if not normalized:
            return False
        broad_markers = [
            "Ø¨Ø±Ø§ÛŒ Ø´Ø±ÙˆØ¹ ÛŒÚ©ÛŒ Ø§Ø² Ú¯ÙØªÚ¯ÙˆÙ‡Ø§ Ø±Ø§ Ø§Ù†ØªØ®Ø§Ø¨ Ú©Ù†ÛŒØ¯",
            "Ù‡Ù…Ù‡ Ù¾ÛŒØ§Ù…â€ŒÙ‡Ø§",
            "Ú¯ÙØªÚ¯ÙˆÙ‡Ø§",
            "Ù…Ø®Ø§Ø·Ø¨ÛŒÙ†",
            "Ø®Ø¯Ù…Ø§Øª",
            "Ù…Ø¬Ù„Ù‡",
        ]
        if _is_broad_chat_text(text):
            return True
        marker_count = sum(1 for marker in broad_markers if marker and marker in text)
        line_count = len([line for line in text.splitlines() if line.strip()])
        box = candidate.get("box") or {}
        width = float(box.get("w", box.get("width", 0)) or 0)
        height = float(box.get("h", box.get("height", 0)) or 0)
        if len(text) > 220 and marker_count >= 2:
            return True
        if line_count >= 8 and marker_count >= 2:
            return True
        if len(text) > 220 and line_count >= 8:
            return True
        if len(text) > 120 and line_count >= 12:
            return True
        if width > 650 or height > 260:
            return True
        return False

    def _is_likely_chat_search_row_candidate(self, candidate: dict[str, Any]) -> bool:
        selector = str(candidate.get("click_selector") or candidate.get("selector") or "")
        class_name = str(candidate.get("className") or "")
        click_class = str(candidate.get("clickClass") or "")
        aria_label = str(candidate.get("ariaLabel") or "")
        row_signal = " ".join([selector, class_name, click_class, aria_label])
        if "dialog-item" in aria_label or '[aria-label="dialog-item"]' in selector:
            return True
        return "qHFpb6" in row_signal or "dialog-item-content" in row_signal

    def _candidate_text_contains_query(self, candidate: dict[str, Any], query: str) -> bool:
        query_text = _normalize_bale_match_text(query)
        if not query_text:
            return False
        return query_text in str(candidate.get("normalized_text") or _normalize_bale_match_text(str(candidate.get("text") or "")))

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

    def _click_and_confirm_search_result(self, page: Any, candidate: dict[str, Any], contact_naming_value: str) -> dict[str, Any]:
        click_attempts: list[dict[str, Any]] = []
        clicked_any = False
        selectors_to_try: list[str] = []
        for selector in [
            str(candidate.get("click_selector") or ""),
            str(candidate.get("selector") or ""),
            "div:nth-child(2) > div > .qHFpb6 > .ZGzps0",
        ]:
            if selector and selector not in selectors_to_try:
                selectors_to_try.append(selector)

        if not selectors_to_try:
            return {"status": "failed", "error": "candidate_selector_missing", "click_attempts": click_attempts}

        def record_attempt(method: str, selector: str, success: bool, before_url: str, error: str = "") -> str:
            _safe_wait_for_timeout(page, 200)
            after_url = _safe_page_url(page)
            confirmation_state = self._chat_open_confirmation_state(page, contact_naming_value, str(candidate.get("text") or ""))
            confirmation = str(confirmation_state.get("reason") or "")
            click_attempts.append(
                {
                    "method": method,
                    "selector": selector,
                    "before_url": before_url,
                    "after_url": after_url,
                    "success": bool(success and confirmation),
                    "error": error,
                    "chat_open_confirmed_by": confirmation,
                    "message_input_visible": bool(confirmation_state.get("message_input_visible")),
                    "scoped_header_text": confirmation_state.get("scoped_header_text", ""),
                    "right_header_text_source": confirmation_state.get("right_header_text_source", ""),
                    "false_positive_confirmation_prevented": bool(confirmation_state.get("false_positive_confirmation_prevented")),
                }
            )
            return confirmation

        for selector in selectors_to_try:
            locator = page.locator(selector).first
            for method, kwargs in [
                ("normal_click", {"timeout": 500}),
                ("force_click", {"timeout": 500, "force": True}),
            ]:
                before_url = _safe_page_url(page)
                try:
                    locator.click(**kwargs)
                    clicked_any = True
                    confirmation = record_attempt(method, selector, True, before_url)
                    if confirmation:
                        return {"status": "success", "clicked_result": True, "click_method": method, "selector": selector, "click_attempts": click_attempts, "chat_open_confirmed_by": confirmation}
                except Exception as exc:
                    record_attempt(method, selector, False, before_url, str(exc))

            for method, script in [
                ("js_click", "(el) => el.click()"),
                ("mouse_event_click", '(el) => el.dispatchEvent(new MouseEvent("click", {bubbles: true, cancelable: true, view: window}))'),
            ]:
                before_url = _safe_page_url(page)
                try:
                    locator.evaluate(script)
                    clicked_any = True
                    confirmation = record_attempt(method, selector, True, before_url)
                    if confirmation:
                        return {"status": "success", "clicked_result": True, "click_method": method, "selector": selector, "click_attempts": click_attempts, "chat_open_confirmed_by": confirmation}
                except Exception as exc:
                    record_attempt(method, selector, False, before_url, str(exc))

            before_url = _safe_page_url(page)
            try:
                box = candidate.get("box") or locator.bounding_box(timeout=200)
                if box:
                    x = float(box.get("x", 0)) + float(box.get("w", box.get("width", 0))) / 2
                    y = float(box.get("y", 0)) + float(box.get("h", box.get("height", 0))) / 2
                    page.mouse.click(x, y)
                    clicked_any = True
                    confirmation = record_attempt("coordinate_click", selector, True, before_url)
                    if confirmation:
                        return {"status": "success", "clicked_result": True, "click_method": "coordinate_click", "selector": selector, "click_attempts": click_attempts, "chat_open_confirmed_by": confirmation}
                else:
                    record_attempt("coordinate_click", selector, False, before_url, "candidate_box_missing")
            except Exception as exc:
                record_attempt("coordinate_click", selector, False, before_url, str(exc))

        return {"status": "failed", "clicked_result": clicked_any, "click_method": "", "selector": selectors_to_try[0], "click_attempts": click_attempts}

    def _confirm_chat_opened(self, page: Any) -> bool:
        if self._first_visible_selector(page, selectors.MESSAGE_INPUT_SELECTORS, timeout_ms=1200):
            return True
        current_url = _safe_page_url(page).lower()
        return "/chat/" in current_url or "uid=" in current_url

    def _chat_open_confirmation_reason(self, page: Any, contact_naming_value: str, matched_contact_text: str = "") -> str:
        return str(self._chat_open_confirmation_state(page, contact_naming_value, matched_contact_text).get("reason") or "")

    def _chat_open_confirmation_state(self, page: Any, contact_naming_value: str, matched_contact_text: str = "") -> dict[str, Any]:
        current_url = _safe_page_url(page).lower()
        message_input_visible = bool(self._first_visible_selector(page, selectors.MESSAGE_INPUT_SELECTORS, timeout_ms=400))
        state: dict[str, Any] = {
            "reason": "",
            "current_url": _safe_page_url(page),
            "message_input_visible": message_input_visible,
            "scoped_header_text": "",
            "right_header_text_source": "",
            "false_positive_confirmation_prevented": False,
        }
        if "/chat?uid=" in current_url or ("chat" in current_url and "uid=" in current_url):
            state["reason"] = "chat_uid_url"
            return state
        if message_input_visible:
            state["reason"] = "message_input_visible"
            return state
        name = str(contact_naming_value or "").strip().lower()
        chat_app_bar_text = self._chat_app_bar_text(page)
        if name and name in chat_app_bar_text.lower() and bool(self._first_visible_selector(page, selectors.MESSAGE_INPUT_SELECTORS, timeout_ms=100)):
            state["scoped_header_text"] = chat_app_bar_text
            state["right_header_text_source"] = "chat_app_bar"
            state["reason"] = "chat_app_bar_match"
            return state
        if "/chat/search" in current_url:
            broad_text = self._right_chat_header_text(page)
            if name and name in broad_text.lower():
                state["scoped_header_text"] = broad_text
                state["right_header_text_source"] = "search_panel_rejected"
                state["false_positive_confirmation_prevented"] = True
            return state
        header_text = self._scoped_opened_chat_header_text(page)
        state["scoped_header_text"] = header_text
        state["right_header_text_source"] = "opened_chat_header"
        if name and name in header_text.lower() and not _is_broad_chat_text(header_text):
            state["reason"] = "scoped_header_match"
            return state
        if name and self._opened_chat_panel_contains_contact(page, name):
            state["reason"] = "opened_chat_panel_match"
            return state
        if name and matched_contact_text and name in matched_contact_text.lower() and "uid=" in current_url:
            state["reason"] = "matched_result_with_uid"
            return state
        return state

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

    def _contact_open_confirmation_reason(self, page: Any, contact_naming_value: str, matched_contact_text: str = "") -> str:
        current_url = _safe_page_url(page).lower()
        if "/contacts?uid=" in current_url or ("contacts" in current_url and "uid=" in current_url):
            return "contacts_uid_url"
        if "uid=" in current_url:
            return "uid_url"
        header_text = self._right_chat_header_text(page)
        name = str(contact_naming_value or "").strip().lower()
        if name and name in header_text.lower():
            return "right_header_match"
        message_input_visible = bool(self._first_visible_selector(page, selectors.MESSAGE_INPUT_SELECTORS, timeout_ms=100))
        if message_input_visible and matched_contact_text and name and name in matched_contact_text.lower():
            return "message_input_visible"
        return ""

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

    def _scoped_opened_chat_header_text(self, page: Any) -> str:
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
                        if (rect.x < 500 || rect.y > 180 || rect.height > 140 || rect.width > 520) return false;
                        const text = (el.innerText || el.textContent || "").trim();
                        if (!text || text.length > 180) return false;
                        if (text.includes("Ø¨Ø±Ø§ÛŒ Ø´Ø±ÙˆØ¹ ÛŒÚ©ÛŒ Ø§Ø² Ú¯ÙØªÚ¯ÙˆÙ‡Ø§ Ø±Ø§ Ø§Ù†ØªØ®Ø§Ø¨ Ú©Ù†ÛŒØ¯")) return false;
                        if (text.includes("Ù‡Ù…Ù‡ Ù¾ÛŒØ§Ù…â€ŒÙ‡Ø§") || text.includes("Ú¯ÙØªÚ¯ÙˆÙ‡Ø§")) return false;
                        return true;
                    });
                    return nodes.map((el) => (el.innerText || el.textContent || "").trim()).filter(Boolean).slice(0, 10).join("\\n").slice(0, 500);
                }"""
            )
            return str(text or "")
        except Exception:
            return ""

    def _opened_chat_panel_contains_contact(self, page: Any, contact_name_lower: str) -> bool:
        try:
            return bool(
                page.evaluate(
                    """(name) => {
                        const visible = (el) => {
                            const style = window.getComputedStyle(el);
                            const rect = el.getBoundingClientRect();
                            return style && style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
                        };
                        return Array.from(document.querySelectorAll("body *")).some((el) => {
                            if (!visible(el)) return false;
                            const rect = el.getBoundingClientRect();
                            if (rect.x < 450 || rect.width < 100) return false;
                            const text = (el.innerText || el.textContent || "").trim().toLowerCase();
                            return text.includes(name);
                        });
                    }""",
                    contact_name_lower,
                )
            )
        except Exception:
            return False

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
                "stopped_after_match": False,
                "clicked_selector": "",
                "page_url_before_click": "",
                "page_url_after_click": "",
                "contact_open_confirmed_by": "",
            }
            result["contacts_query_attempts"].append(attempt)
            if not match:
                continue
            result["matched_contact_text"] = str(match.get("text") or "")
            result["matched_contact_role"] = str(match.get("role") or "")
            result["matched_contact_box"] = match.get("box") or {}
            clicked_selector = str(match.get("click_selector") or match.get("selector") or "")
            attempt["clicked_selector"] = clicked_selector
            attempt["page_url_before_click"] = _safe_page_url(page)
            click_result = self._click_search_result_candidate(page, match)
            if click_result.get("status") != "success":
                result["contact_click_error"] = click_result
                result["page_url"] = _safe_page_url(page)
                result["chat_open_confirmed"] = False
                result["duration_ms"] = int((time.perf_counter() - started) * 1000)
                return result
            attempt["page_url_after_click"] = _safe_page_url(page)

            chat_opened = self._confirm_chat_opened_for_contact(page, contact_naming_value, result["matched_contact_text"])
            confirmation_reason = self._contact_open_confirmation_reason(page, contact_naming_value, result["matched_contact_text"])
            attempt["contact_open_confirmed_by"] = confirmation_reason
            result["message_input_visible"] = bool(self._first_visible_selector(page, selectors.MESSAGE_INPUT_SELECTORS, timeout_ms=100))
            result["chat_url_has_uid"] = "uid=" in _safe_page_url(page).lower()
            result["right_chat_header_text"] = self._right_chat_header_text(page)
            if chat_opened or confirmation_reason:
                attempt["stopped_after_match"] = True
                result.update({
                    "status": "success",
                    "matched_selector": match.get("click_selector") or match.get("selector"),
                    "page_url": _safe_page_url(page),
                    "chat_open_confirmed": True,
                    "contact_open_confirmed_by": confirmation_reason or "chat_opened",
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
                confirmation_reason = self._contact_open_confirmation_reason(page, contact_naming_value, result["matched_contact_text"])
                attempt["contact_open_confirmed_by"] = confirmation_reason
                result["message_input_visible"] = bool(self._first_visible_selector(page, selectors.MESSAGE_INPUT_SELECTORS, timeout_ms=100))
                result["chat_url_has_uid"] = "uid=" in _safe_page_url(page).lower()
                result["right_chat_header_text"] = self._right_chat_header_text(page)
                if chat_opened or confirmation_reason:
                    attempt["stopped_after_match"] = True
                    attempt["page_url_after_click"] = _safe_page_url(page)
                    result.update(
                        {
                            "status": "success",
                            "matched_selector": match.get("click_selector") or match.get("selector"),
                            "message_button_selector": message_button,
                            "page_url": _safe_page_url(page),
                            "chat_open_confirmed": True,
                            "contact_open_confirmed_by": confirmation_reason or "chat_opened",
                            "duration_ms": int((time.perf_counter() - started) * 1000),
                        }
                    )
                    return result
            if query == name_query:
                attempt["stopped_after_match"] = True
                attempt["page_url_after_click"] = _safe_page_url(page)
                result.update(
                    {
                        "error_code": "chat_open_not_confirmed",
                        "reason": "name_match_click_not_confirmed",
                        "page_url": _safe_page_url(page),
                        "chat_open_confirmed": False,
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

        search_icon = self._first_visible_selector(page, selectors.CONTACTS_SEARCH_ICON_SELECTORS, timeout_ms=250)
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

    def _goto_with_timeout(self, page: Any, url: str, timeout_ms: int, wait_until: str = "load") -> None:
        try:
            page.goto(url, wait_until=wait_until, timeout=timeout_ms)
        except TypeError:
            page.goto(url, wait_until=wait_until)

    def _is_contacts_search_editable(self, page: Any, selector: str) -> bool:
        try:
            locator = page.locator(selector).first
            locator.click(timeout=300)
            return True
        except Exception:
            return False

    def _click_contacts_header_or_panel(self, page: Any) -> dict[str, Any]:
        selectors_to_try = [
            'text=Ù…Ø®Ø§Ø·Ø¨ÛŒÙ†',
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
                        return text.includes("Ù…Ø®Ø§Ø·Ø¨ÛŒÙ†") && rect.width > 200 && rect.height > 200;
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

    def _click_selector_short(
        self,
        page: Any,
        selector: str,
        timeout_ms: int = 500,
        postcondition_selector: str | None = None,
        postcondition_timeout_ms: int = 0,
    ) -> dict[str, Any]:
        try:
            page.locator(selector).first.click(timeout=timeout_ms)
            return {"status": "success", "click_method": "playwright_click", "selector": selector}
        except Exception as click_error:
            try:
                page.locator(selector).first.evaluate("(el) => el.click()")
            except Exception as dom_error:
                return {"status": "failed", "selector": selector, "click_error": str(click_error), "dom_click_error": str(dom_error)}
            if postcondition_selector:
                postcondition_found = self._first_visible_selector(
                    page,
                    [postcondition_selector],
                    timeout_ms=postcondition_timeout_ms,
                )
                if postcondition_found:
                    return {
                        "status": "success",
                        "click_method": "dom_click",
                        "selector": selector,
                        "playwright_click_error": str(click_error),
                        "postcondition_selector": postcondition_selector,
                    }
            return {
                "status": "failed",
                "selector": selector,
                "click_error": str(click_error),
                "dom_click_attempted": True,
                "error_code": "click_postcondition_not_met",
                "postcondition_selector": postcondition_selector or "",
            }

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
                        if (/Ú¯ÙØªÚ¯Ùˆ\\s+Ù…Ø¬Ù„Ù‡\\s+Ø®Ø¯Ù…Ø§Øª\\s+Ù…Ø®Ø§Ø·Ø¨ÛŒÙ†/.test(text)) continue;
                        if (/Ø³Ø§Ø®Øª Ú¯Ø±ÙˆÙ‡|Ø³Ø§Ø®Øª Ú©Ø§Ù†Ø§Ù„|Ø§ÙØ²ÙˆØ¯Ù† Ù…Ø®Ø§Ø·Ø¨|Ù…Ø±ØªØ¨â€ŒØ´Ø¯Ù‡/.test(text) && text.length > 120) continue;
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
        if "Ú¯ÙØªÚ¯Ùˆ" in value and "Ù…Ø¬Ù„Ù‡" in value and "Ù…Ø®Ø§Ø·Ø¨ÛŒÙ†" in value:
            return False
        if len(value) > 120 and any(token in value for token in ["Ø³Ø§Ø®Øª Ú¯Ø±ÙˆÙ‡", "Ø³Ø§Ø®Øª Ú©Ø§Ù†Ø§Ù„", "Ø§ÙØ²ÙˆØ¯Ù† Ù…Ø®Ø§Ø·Ø¨", "Ù…Ø±ØªØ¨â€ŒØ´Ø¯Ù‡"]):
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
        for selector in ['text=/.*not found.*/i', 'text=/.*no result.*/i', 'text=/.*ÛŒØ§ÙØª Ù†Ø´Ø¯.*/', 'text=/.*Ù†ØªÛŒØ¬Ù‡.*/']:
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
        message_input_selector = self._first_visible_selector(page, selectors.MESSAGE_INPUT_SELECTORS, timeout_ms=100)
        return {
            "page_url": _safe_page_url(page),
            "page_title": _safe_page_title(page),
            "visible_modal_text": self._visible_modal_or_dialog_text(page),
            "search_input_visible": bool(search_input_selector),
            "search_input_selector": search_input_selector or "",
            "searched_value": searched_value,
            "contact_naming_value": contact_naming_value,
            "normalized_phone": normalized_phone,
            "contacts_ui_visible": self._contacts_ui_visible(page),
            "main_chat_ui_visible": self._main_chat_ui_visible(page),
            "message_input_visible": bool(message_input_selector),
            "message_input_selector": message_input_selector or "",
            "message_input_detected_count": self._visible_selector_count(page, selectors.MESSAGE_INPUT_SELECTORS),
            "contenteditable_count": self._dom_count(page, "[contenteditable='true'], [contenteditable=true]"),
            "textarea_count": self._dom_count(page, "textarea"),
            "input_count": self._dom_count(page, "input"),
            "chat_app_bar_text": self._right_chat_header_text(page),
            "visible_text_sample": _visible_text_sample(page),
        }

    def _visible_selector_count(self, page: Any, selector_list: list[str]) -> int:
        count = 0
        for selector in selector_list:
            if self._is_selector_visible(page, selector, timeout_ms=50):
                count += 1
        return count

    def _dom_count(self, page: Any, selector: str) -> int:
        try:
            value = page.evaluate(
                """(selector) => {
                    try {
                        return document.querySelectorAll(selector).length;
                    } catch (_) {
                        return 0;
                    }
                }""",
                selector,
            )
            return int(value or 0)
        except Exception:
            return 0

    def _send_button_diagnostics(self, page: Any, message_input_selector: str = "") -> dict[str, Any]:
        send_button_selector = self._first_visible_selector(page, selectors.SEND_BUTTON_SELECTORS, timeout_ms=100)
        return {
            "send_button_visible": bool(send_button_selector),
            "send_button_selector": send_button_selector or "",
            "send_button_candidates": self._visible_selector_snapshot(page, selectors.SEND_BUTTON_SELECTORS),
            "message_input_text_before_send": self._input_text_value(page, message_input_selector),
            "current_url": _safe_page_url(page),
            "page_url": _safe_page_url(page),
        }

    def _visible_selector_snapshot(self, page: Any, selector_list: list[str]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for selector in selector_list:
            visible = self._is_selector_visible(page, selector, timeout_ms=50)
            text = ""
            if visible:
                try:
                    text = str(page.locator(selector).first.inner_text(timeout=100) or "")[:160]
                except Exception:
                    text = ""
            items.append({"selector": selector, "visible": visible, "text": text})
        return items

    def _input_text_value(self, page: Any, selector: str) -> str:
        if not selector:
            return ""
        try:
            return str(page.locator(selector).first.input_value(timeout=100) or "")
        except Exception:
            pass
        try:
            return str(page.locator(selector).first.inner_text(timeout=100) or "")
        except Exception:
            return ""

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
            page.click(selector, timeout=min(self.default_timeout_ms, 1500))
        except Exception:
            try:
                page.locator(selector).first.click(timeout=min(self.default_timeout_ms, 1500))
            except Exception:
                pass

    def _fill_or_type(self, page: Any, selector: str, text: str) -> None:
        try:
            page.fill(selector, text, timeout=min(self.default_timeout_ms, 1500))
            return
        except Exception:
            pass
        locator = page.locator(selector).first
        try:
            locator.fill(text, timeout=min(self.default_timeout_ms, 1500))
            return
        except Exception:
            pass
        locator.type(text, timeout=min(self.default_timeout_ms, 1500))

    def _press_key(self, page: Any, key: str) -> None:
        keyboard = getattr(page, "keyboard", None)
        if keyboard is not None and hasattr(keyboard, "press"):
            keyboard.press(key)
            return
        try:
            page.press("body", key, timeout=min(self.default_timeout_ms, 1500))
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
        total_timeout = timeout_ms if timeout_ms is not None else min(self.default_timeout_ms, 1500)
        total_timeout = max(0, int(total_timeout))
        deadline = time.monotonic() + (total_timeout / 1000)
        probe_timeout = min(150, max(25, total_timeout or 25))
        while True:
            for selector in selector_list:
                remaining_ms = int((deadline - time.monotonic()) * 1000)
                if remaining_ms < 0:
                    return None
                try:
                    locator = page.locator(selector).first
                    locator.wait_for(state="visible", timeout=min(probe_timeout, max(1, remaining_ms)))
                    return selector
                except Exception:
                    continue
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.05)
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
            "logged_in": error_code not in {"not_logged_in", "login_state_unknown"},
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

_CHAT_LIST_SELECTORS = [
    '[aria-label="dialog-item"]',
    "[data-testid='chat-list']",
    "[data-testid='conversation-list']",
    "[role='list']",
    "div[role='list']",
    "[class*='chat-list']",
    "[class*='ChatList']",
    "[class*='conversation']",
]

_BALE_CHAT_TAB_SELECTORS = [
    "text=\u0647\u0645\u0647",
    "text=\u0634\u062e\u0635\u06cc",
    "text=\u06af\u0631\u0648\u0647",
    "text=\u062a\u0645\u0627\u0633",
]

_BALE_SIDE_MENU_SELECTORS = [
    "text=\u06af\u0641\u062a\u06af\u0648",
    "text=\u0645\u062e\u0627\u0637\u0628\u06cc\u0646",
    "text=\u062e\u062f\u0645\u0627\u062a",
    '[aria-label="Contacts-icon"]',
    '[aria-label="Chat-icon"]',
    '[aria-label="BoldContacts-icon"]',
    '[aria-label="BoldChat-icon"]',
]

_CLEAR_LOGIN_FORM_SELECTORS = [
    "input[type='tel']",
    "input[name*='phone']",
    "input[autocomplete='tel']",
    "[data-testid='login-form']",
    "[data-testid='phone-login']",
    "[data-testid='phone-input']",
    "[data-testid*='qr']",
    "[aria-label*='phone']",
    "[aria-label*='\u0634\u0645\u0627\u0631\u0647']",
    "text=\u0634\u0645\u0627\u0631\u0647 \u0645\u0648\u0628\u0627\u06cc\u0644",
    "text=\u0634\u0645\u0627\u0631\u0647 \u062a\u0644\u0641\u0646",
    "text=Login with phone",
    "text=Enter phone",
]

_LOGIN_FORM_SELECTORS = [
    "input[type='tel']",
    "input[name*='phone']",
    "input[autocomplete='tel']",
    "[data-testid*='login']",
    "[class*='login']",
    "text=ÙˆØ±ÙˆØ¯",
    "text=Ø´Ù…Ø§Ø±Ù‡ Ù…ÙˆØ¨Ø§ÛŒÙ„",
]

_INSTALL_PROMPT_SELECTORS = [
    "text=متوجه شدم",
    "text=نصب",
    "text=Ù…ØªÙˆØ¬Ù‡ Ø´Ø¯Ù…",
    "text=Ù†ØµØ¨",
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


def _safe_wait_for_timeout(page: Any, timeout_ms: int) -> None:
    if timeout_ms <= 0:
        return
    try:
        wait_for_timeout = getattr(page, "wait_for_timeout", None)
        if callable(wait_for_timeout):
            wait_for_timeout(timeout_ms)
    except Exception:
        return


def _valid_bale_channel_uid(value: str) -> bool:
    if not value or len(value) > 64:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9_-]+", value))


def _visible_text_sample(page: Any, limit: int = 500) -> str:
    try:
        body_text = page.locator("body").inner_text(timeout=500)
        if body_text:
            return " ".join(str(body_text).split())[:limit]
    except Exception:
        pass
    try:
        selector_text = getattr(page, "selector_text", {})
        if isinstance(selector_text, dict):
            combined = " ".join(str(value) for value in selector_text.values() if value)
            return " ".join(combined.split())[:limit]
    except Exception:
        pass
    return ""


def _is_broad_chat_text(text: str) -> bool:
    if "Ø¨Ø±Ø§ÛŒ Ø´Ø±ÙˆØ¹ ÛŒÚ©ÛŒ Ø§Ø² Ú¯ÙØªÚ¯ÙˆÙ‡Ø§ Ø±Ø§ Ø§Ù†ØªØ®Ø§Ø¨ Ú©Ù†ÛŒØ¯" in text:
        return True
    markers = ["Ù‡Ù…Ù‡ Ù¾ÛŒØ§Ù…â€ŒÙ‡Ø§", "Ú¯ÙØªÚ¯ÙˆÙ‡Ø§", "Ù…Ø®Ø§Ø·Ø¨ÛŒÙ†", "Ø®Ø¯Ù…Ø§Øª", "Ù…Ø¬Ù„Ù‡"]
    marker_count = sum(1 for marker in markers if marker in text)
    line_count = len([line for line in text.splitlines() if line.strip()])
    return bool((len(text) > 220 or line_count >= 8) and marker_count >= 2)


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
