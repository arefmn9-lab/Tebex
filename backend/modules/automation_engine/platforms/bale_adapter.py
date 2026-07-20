from __future__ import annotations

import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from modules.automation_engine.plugins.bale import bale_plugin
from modules.automation_engine.plugins.bale.plugin import _native_profile_dir
from modules.automation_engine.scenario_runner import ScenarioActionExecutor, ScenarioRunner


class BaleScenarioActionExecutor(ScenarioActionExecutor):
    def __init__(self, plugin: Any, plan: Any, runtime_session: Any | None = None) -> None:
        super().__init__()
        self.plugin = plugin
        self.plan = plan
        self.runtime_session = runtime_session
        self.source_verified = False
        self.forward_picker_open = False
        self.forward_boundary_result: dict[str, Any] | None = None
        self._stack = ExitStack()
        self.page: Any | None = None
        self.latest_message_selector = ""
        self.search_selector = ""
        self.first_recipient_result: dict[str, Any] | None = None

    def element_exists(self, name: str, element: dict[str, Any] | None = None) -> bool:
        selector = str((element or {}).get("selector") or "")
        if selector and self.page is not None:
            return bool(self.plugin._first_visible_selector(self.page, [selector], timeout_ms=int((element or {}).get("timeout_ms") or 500)) or "")
        if name == "source_message_items":
            return self.source_verified
        if name == "recipient_search_input":
            return bool(self.search_selector)
        if name == "recipient_result_rows":
            return bool(self.first_recipient_result)
        if name == "final_forward_button":
            return bool(self.forward_boundary_result and self.forward_boundary_result.get("confirm_button_selector"))
        return False

    def wait_for(self, element_name: str, element: dict[str, Any], timeout_ms: int | None = None) -> dict[str, Any]:
        selector = str(element.get("selector") or "")
        page = self._ensure_page()
        if not selector:
            return {"ok": False, "error_code": "element_selector_missing", "element": element_name}
        found = self.plugin._first_visible_selector(page, [selector], timeout_ms=timeout_ms or int(element.get("timeout_ms") or 5000))
        if not found:
            return {"ok": False, "error_code": "element_not_found", "element": element_name, "selector": selector}
        if element_name == "source_message_items":
            self.source_verified = True
        if element_name == "recipient_search_input":
            self.search_selector = selector
            search_diagnostics = self._recipient_search_diagnostics(page, selector)
            self.records.update({
                "recipient_picker_visible": True,
                "recipient_search_selector": selector,
                **search_diagnostics,
            })
        if element_name == "recipient_result_rows":
            result = self._recipient_result_state(page, selector, timeout_ms or int(element.get("timeout_ms") or 4000))
            if not result.get("ok"):
                return {"ok": False, "error_code": str(result.get("error_code") or "recipient_result_not_found"), "element": element_name, "selector": selector, **result}
            self.first_recipient_result = result
            visible_row_texts = result.get("visible_result_texts") if isinstance(result.get("visible_result_texts"), list) else []
            if not visible_row_texts and result.get("first_result_text"):
                visible_row_texts = [str(result.get("first_result_text") or "")]
            self.records.update({
                "recipient_resolved": int(result.get("visible_result_count") or 0) > 0,
                "visible_result_count": int(result.get("visible_result_count") or 0),
                "first_result_text": str(result.get("first_result_text") or ""),
                "recipient_row_count": int(result.get("visible_result_count") or 0),
                "visible_recipient_row_texts": visible_row_texts,
                "first_result_clicked": False,
                "recipient_selected": False,
                "forward_boundary_diagnostics": {"first_result": result},
            })
        if element_name == "final_forward_button":
            self.forward_boundary_result = {**(self.forward_boundary_result or {}), "confirm_button_selector": selector}
            confirm_state = self.plugin._forward_confirm_button_state(page) if hasattr(self.plugin, "_forward_confirm_button_state") else {}
            self.records.update({
                "confirmation_visible": True,
                "final_forward_selector": selector,
                "final_forward_dom_count": int(confirm_state.get("final_forward_dom_count") or 0),
                "final_forward_visible_count": int(confirm_state.get("final_forward_visible_count") or 0),
                "final_forward_enabled_count": int(confirm_state.get("final_forward_enabled_count") or 0),
                "final_forward_hit_testable_count": int(confirm_state.get("final_forward_hit_testable_count") or 0),
                "final_forward_wait_confirm_state": confirm_state,
            })
        return {"ok": True, "element": element_name, "selector": selector}

    def _recipient_result_state(self, page: Any, selector: str, timeout_ms: int = 4000) -> dict[str, Any]:
        display_name = str(getattr(self.plan, "display_name", "") or "")
        if hasattr(self.plugin, "_forward_recipient_results_stability"):
            started = time.perf_counter()
            state: dict[str, Any] = {}
            while (time.perf_counter() - started) * 1000 <= max(0, timeout_ms):
                state = self.plugin._forward_recipient_results_stability(page, display_name)
                candidates = state.get("recipient_candidates") if isinstance(state.get("recipient_candidates"), list) else []
                exact_candidates = [
                    item for item in candidates
                    if isinstance(item, dict) and str(item.get("row_name") or item.get("normalized_name") or item.get("exact_text") or "") == display_name
                ]
                if len(exact_candidates) > 1:
                    return {
                        "ok": False,
                        "error_code": "recipient_match_ambiguous",
                        "selector": selector,
                        "visible_result_count": len(exact_candidates),
                        "first_result_text": str(exact_candidates[0].get("text") or exact_candidates[0].get("row_name") or ""),
                        "visible_result_texts": [str(item.get("text") or item.get("row_name") or "") for item in exact_candidates],
                        "recipient_candidates": exact_candidates,
                        "result_set_stable": bool(state.get("result_set_stable")),
                    }
                if exact_candidates:
                    first = exact_candidates[0]
                    return {
                        "ok": True,
                        "selector": str(first.get("click_selector") or first.get("selector") or ""),
                        "visible_result_count": len(exact_candidates),
                        "first_result_text": str(first.get("text") or first.get("row_name") or ""),
                        "visible_result_texts": [str(item.get("text") or item.get("row_name") or "") for item in exact_candidates],
                        "recipient_candidates": exact_candidates,
                        "result_set_stable": bool(state.get("result_set_stable")),
                    }
                _safe_wait = getattr(self.plugin, "_safe_wait_for_timeout", None)
                if callable(_safe_wait):
                    _safe_wait(page, 150)
                else:
                    try:
                        page.wait_for_timeout(150)
                    except Exception:
                        pass
            return {"ok": False, "error_code": "recipient_lookup_unverified", "selector": selector, "visible_result_count": 0, "first_result_text": "", **state}
        return self.plugin._first_visible_fixed_result(page, selector, timeout_ms=timeout_ms)

    def _recipient_search_diagnostics(self, page: Any, selector: str) -> dict[str, Any]:
        diagnostics: dict[str, Any] = {
            "overlay_visible": False,
            "old_english_search_selector_count": 0,
            "proven_persian_search_selector_count": 0,
            "recipient_search_hit_testable": False,
        }
        try:
            overlay = page.locator(".ReactModal__Overlay")
            diagnostics["overlay_visible"] = bool(overlay.count() and overlay.first.is_visible())
        except Exception:
            pass
        for key, candidate in {
            "old_english_search_selector_count": 'input[type="search"][placeholder="Search..."]',
            "proven_persian_search_selector_count": selector,
        }.items():
            try:
                locator = page.locator(candidate)
                diagnostics[key] = sum(1 for index in range(locator.count()) if locator.nth(index).is_visible())
            except Exception:
                diagnostics[key] = 0
        try:
            page.locator(selector).first.click(timeout=1000, trial=True)
            diagnostics["recipient_search_hit_testable"] = True
        except Exception as exc:
            diagnostics["recipient_search_hit_test_error"] = str(exc)
        return diagnostics

    def navigate(self, url: str, timeout_ms: int | None = None) -> dict[str, Any]:
        try:
            self._ensure_page()
            self.plugin._goto_with_timeout(self.page, url, timeout_ms=timeout_ms or 30000, wait_until="load")
        except Exception as exc:
            return {"ok": False, "error_code": "navigation_failed", "message": str(exc)}
        self.records["source_url"] = url
        return {"ok": True, "url": url, "timeout_ms": timeout_ms}

    def close(self) -> None:
        self._stack.close()

    def _ensure_page(self) -> Any:
        if self.page is None:
            context = self.plugin._runtime_or_page_session(self.plan.account_id, provider_mode="native_chrome", runtime_session=self.runtime_session)
            self.page, session_meta = self._stack.enter_context(context)
            self.records["browser_session"] = session_meta
        return self.page

    def call_platform_primitive(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        if name == "validate_operation_mode":
            mode = str(params.get("operation_mode") or "")
            if mode not in {"inspect_only", "selection_only", "no_send", "live_send"}:
                return {"ok": False, "error_code": "unsupported_operation_mode", "message": "Unsupported Bale scenario operation_mode"}
            return {"ok": True}
        if name == "select_latest_message":
            page = self._ensure_page()
            result = self.plugin._select_last_visible_fixed(page, '[aria-label="message-item"]')
            self.latest_message_selector = str(result.get("selector") or "")
            if not result.get("ok"):
                return {"ok": False, "error_code": "source_message_not_found", **result}
            self.records.update({
                "source_message_selected": True,
                "selected_message_preview": str(result.get("text") or ""),
            })
            return {"ok": True}
        if name == "hover_latest_message":
            page = self._ensure_page()
            if not self.latest_message_selector:
                return {"ok": False, "error_code": "source_message_not_found"}
            try:
                page.locator(self.latest_message_selector).first.hover(timeout=5000)
            except Exception as exc:
                return {"ok": False, "error_code": "message_hover_failed", "message": str(exc)}
            return {"ok": True}
        return {"ok": False, "error_code": "platform_primitive_not_implemented", "primitive": name}

    def fill(self, element_name: str, element: dict[str, Any], value: str, timeout_ms: int | None = None) -> dict[str, Any]:
        if element_name == "recipient_search_input":
            page = self._ensure_page()
            self.search_selector = str(element.get("selector") or "")
            if not self.search_selector:
                return {"ok": False, "error_code": "recipient_search_input_not_found"}
            try:
                self._type_recipient_search_value(page, self.search_selector, value)
            except Exception as exc:
                return {"ok": False, "error_code": "recipient_search_input_failed", "message": str(exc)}
            search_value = self.plugin._forward_search_input_value(page, self.search_selector)
            self.records.update({
                "recipient_search_value": search_value,
                "searched_value": value,
            })
            return {"ok": search_value == value, "error_code": None if search_value == value else "recipient_search_input_failed", "search_input_value": search_value}
        return super().fill(element_name, element, value, timeout_ms)

    def _type_recipient_search_value(self, page: Any, selector: str, value: str) -> None:
        locator = page.locator(selector).first
        keyboard = getattr(page, "keyboard", None)
        if keyboard is None:
            self.plugin._fill_or_type(page, selector, value)
            return
        try:
            locator.click(timeout=1000)
            keyboard.press("Control+A")
            keyboard.press("Backspace")
            locator.type(value, timeout=3000)
            return
        except Exception:
            self.plugin._fill_or_type(page, selector, value)

    def choose_from_list(self, element_name: str, element: dict[str, Any], exact_text: str, timeout_ms: int | None = None) -> dict[str, Any]:
        if element_name == "recipient_result_rows":
            page = self._ensure_page()
            result = self.first_recipient_result or self._recipient_result_state(page, str(element.get("selector") or ""), timeout_ms=timeout_ms or 4000)
            if not result.get("ok"):
                return {"ok": False, "error_code": str(result.get("error_code") or "recipient_result_not_found"), **result}
            self.first_recipient_result = result
            recipient_selector = str(result.get("selector") or "")
            if not recipient_selector:
                return {"ok": False, "error_code": "recipient_result_selector_missing", **result}
            select_click = self.plugin._click_selector_short(
                page,
                recipient_selector,
                timeout_ms=timeout_ms or 4000,
                postcondition_selector='.ReactModal__Overlay [role="button"][aria-label="send-button-forward-messages"][data-testid="bold-send2-icon"]',
                postcondition_timeout_ms=4000,
            )
            if select_click.get("status") != "success":
                return {"ok": False, "error_code": "recipient_select_failed", "click_result": select_click}
            try:
                page.wait_for_timeout(500)
            except Exception:
                pass
            selected_state = self.plugin._forward_selected_recipients_state(page)
            selected_names = selected_state.get("selected_names") if isinstance(selected_state.get("selected_names"), list) else []
            confirm_state = self.plugin._forward_confirm_button_state(page) if hasattr(self.plugin, "_forward_confirm_button_state") else {}
            confirm_selector = str(confirm_state.get("confirm_button_selector") or "")
            resolved = selected_names == [exact_text] and bool(confirm_selector)
            self.forward_boundary_result = {
                "success": resolved,
                "confirm_button_selector": confirm_selector,
                "selected_names": selected_names,
            }
            self.records.update({
                "recipient_resolved": resolved,
                "visible_result_count": int(result.get("visible_result_count") or 0),
                "first_result_text": str(result.get("first_result_text") or ""),
                "first_result_clicked": select_click.get("status") == "success",
                "recipient_result_text": str(result.get("first_result_text") or ""),
                "recipient_selected": resolved,
                "recipient_selection_verified": resolved,
                "selected_recipient_display_name": selected_names[0] if len(selected_names) == 1 else "",
                "recipient_select_click_result": select_click,
                "recipient_selected_state": selected_state,
                "confirmation_visible": bool(confirm_selector),
                "final_forward_enabled": bool(confirm_selector),
                "final_forward_selector": confirm_selector,
                "final_forward_dom_count": int(confirm_state.get("final_forward_dom_count") or 0),
                "final_forward_visible_count": int(confirm_state.get("final_forward_visible_count") or 0),
                "final_forward_enabled_count": int(confirm_state.get("final_forward_enabled_count") or 0),
                "final_forward_hit_testable_count": int(confirm_state.get("final_forward_hit_testable_count") or 0),
                "stopped_before_send": True,
                "forward_boundary_diagnostics": {"first_result": result, "selected_state": selected_state, "confirm_state": confirm_state},
            })
            return {"ok": resolved, "error_code": None if resolved else "recipient_selection_unverified", **self.forward_boundary_result}
        return super().choose_from_list(element_name, element, exact_text, timeout_ms)

    def click(self, element_name: str, element: dict[str, Any], timeout_ms: int | None = None) -> dict[str, Any]:
        if element_name == "message_forward_control":
            page = self._ensure_page()
            selector = str(element.get("selector") or "")
            if self.latest_message_selector:
                selector = f"{self.latest_message_selector} {selector}"
            click_result = self.plugin._click_selector_short(
                page,
                selector,
                timeout_ms=timeout_ms or 3000,
                postcondition_selector='.ReactModal__Overlay input[placeholder="جستجوی مخاطب، گروه، کانال و نام‌کاربری..."]',
                postcondition_timeout_ms=4000,
            )
            if click_result.get("status") != "success":
                return {"ok": False, "error_code": "forward_option_not_found", "click_result": click_result}
            self.forward_picker_open = True
            self.records["recipient_picker_visible"] = True
            return {"ok": True, "click_result": click_result}
        if element_name == "final_forward_button":
            self.final_send_invoked = True
            confirm_selector = str(element.get("selector") or (self.forward_boundary_result or {}).get("confirm_button_selector") or "")
            if not confirm_selector:
                return {"ok": False, "error_code": "forward_confirm_button_not_found"}
            page = self._ensure_page()
            click_result = self.plugin._click_selector_short(page, confirm_selector, timeout_ms=timeout_ms or 4000)
            self.records.update({
                "send_confirmation_click_count": 1 if click_result.get("status") == "success" else 0,
                "confirm_click_count": 1 if click_result.get("status") == "success" else 0,
            })
            if click_result.get("status") != "success":
                return {"ok": False, "error_code": "forward_confirm_failed", "click_result": click_result}
            verify_state = self.plugin._wait_forward_success_state(page, expected_recipient_name=str(self.plan.display_name), timeout_ms=2000)
            self.records.update({
                "send_success_verified": bool(verify_state.get("send_success_verified")),
                "verification_method": str(verify_state.get("verification_method") or ""),
                "verification_evidence": str(verify_state.get("verification_evidence") or ""),
                "remote_message_id": verify_state.get("remote_message_id") or None,
                "post_send_verification": verify_state,
            })
            if not verify_state.get("send_success_verified"):
                return {"ok": False, "error_code": "send_result_unverified", "failed_step": "verify_send_result", "click_result": click_result, **verify_state}
            return {"ok": True, "click_result": click_result, **verify_state}
        return super().click(element_name, element, timeout_ms)


class BaleDeliveryAdapter:
    platform_name = "bale"

    def __init__(self, plugin: Any | None = None, scenario_root: Path | None = None) -> None:
        self.plugin = plugin or bale_plugin
        automation_engine_dir = Path(__file__).resolve().parents[1]
        self.scenario_root = scenario_root or automation_engine_dir / "scenarios"

    def validate_execution_plan(self, plan: Any) -> dict[str, Any]:
        from modules.automation_engine.commercial_queue.operations import operation_registry

        validation = operation_registry.validate(list(plan.operation_order))
        return validation.to_dict()

    def create_runtime_session(self, plan: Any) -> dict[str, Any]:
        return self.create_runtime_session_for_account(plan.account_id, "", plan.worker_round_id, plan.effective_policy)

    def create_runtime_session_for_account(self, account_id: str, owner_token: str, worker_round_id: str, policy: dict[str, Any]) -> dict[str, Any]:
        if not bool(policy.get("session_reuse_enabled")):
            return {"platform": self.platform_name, "account_id": account_id, "profile_path": str(policy.get("profile_path") or _native_profile_dir(account_id)), "session_reuse_enabled": False}
        payload = self.plugin.create_reusable_runtime_session(account_id, provider_mode="native_chrome", profile_path=policy.get("profile_path"))
        return {"platform": self.platform_name, "account_id": account_id, "session_reuse_enabled": True, **payload}

    def health_check_session(self, session: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "session": {key: session.get(key) for key in ["platform", "account_id"]}}

    def prepare_session_for_job(self, session: dict[str, Any], plan: Any) -> dict[str, Any]:
        return {"ok": True, "job_id": plan.job_id}

    def prepare_recipient(self, session: dict[str, Any], plan: Any) -> dict[str, Any]:
        return {"ok": True, "recipient_id": plan.recipient_id}

    def deliver(self, session: dict[str, Any], plan: Any) -> dict[str, Any]:
        runtime_session = session if hasattr(session, "session_id") else None
        return self.execute_plan(plan, runtime_session=runtime_session)

    def execute_plan(self, plan: Any, runtime_session: Any | None = None) -> dict[str, Any]:
        policy = getattr(plan, "effective_policy", {}) or {}
        allow_final_send = bool(getattr(plan, "allow_final_send", False) or policy.get("allow_final_send"))
        operation_mode = "no_send" if bool(getattr(plan, "dry_run", False)) else "live_send"
        return self.execute_standalone_scenario(plan, operation_mode=operation_mode, allow_final_send=allow_final_send, runtime_session=runtime_session)

    def controlled_live_no_send(self, plan: Any, runtime_session: Any | None = None) -> dict[str, Any]:
        return self.execute_standalone_scenario(plan, operation_mode="no_send", allow_final_send=False, runtime_session=runtime_session)

    def execute_standalone_scenario(self, plan: Any, operation_mode: str, allow_final_send: bool = False, runtime_session: Any | None = None) -> dict[str, Any]:
        scenario_path = self.scenario_root / "bale" / "forward_channel_messages.json"
        scenario = ScenarioRunner.load(scenario_path)
        source_uid = str(getattr(plan, "source_channel_uid", "") or "")
        context = {
            "sender_account_id": plan.account_id,
            "source_url": str(getattr(plan, "source_channel_url", "") or f"https://web.bale.ai/chat?uid={source_uid}"),
            "source_uid": source_uid,
            "recipient_phone": plan.phone,
            "recipient_display_name": plan.display_name,
            "operation": "forward_channel_messages",
            "operation_mode": operation_mode,
            "allow_final_send": bool(allow_final_send),
            "message_text": str(getattr(plan, "message_text", "") or ""),
            "source_message_selector": str(getattr(plan, "source_message_selector", "latest") or "latest"),
            "job_id": plan.job_id,
            "campaign_id": plan.campaign_id,
            "recipient_id": plan.recipient_id,
        }
        executor = BaleScenarioActionExecutor(self.plugin, plan, runtime_session=runtime_session)
        try:
            scenario_result = ScenarioRunner(executor).run(scenario, context).to_dict()
        finally:
            executor.close()
        records = scenario_result.get("records") or {}
        final_send_invoked = bool(scenario_result.get("final_send_invoked"))
        if final_send_invoked and not allow_final_send:
            return {"success": False, "ok": False, "error_code": "final_send_guard_failed", "failed_step": "confirm_forward_send", "stopped_before_send": False, "scenario_result": scenario_result}
        failed_step = scenario_result.get("failed_step")
        if scenario_result.get("error_code") == "send_result_unverified":
            failed_step = "verify_send_result"
        return {
            "success": bool(scenario_result.get("ok")),
            "ok": bool(scenario_result.get("ok")),
            "action": "bale_standalone_scenario",
            "scenario_id": scenario_result.get("scenario_id"),
            "job_id": plan.job_id,
            "campaign_id": plan.campaign_id,
            "account_id": plan.account_id,
            "recipient_id": plan.recipient_id,
            "phone": plan.phone,
            "display_name": plan.display_name,
            "source_channel_uid": source_uid,
            "source_resolved": bool(records.get("source_resolved")),
            "source_timeline_detected": bool(records.get("source_timeline_detected")),
            "recipient_resolved": bool(records.get("recipient_resolved")),
            "composer_visible": bool(records.get("confirmation_visible")),
            "final_send_control_visible": bool(records.get("confirmation_visible")),
            "stopped_before_send": bool(scenario_result.get("stopped_before_send") or records.get("stopped_before_send")),
            "confirm_click_count": int(records.get("confirm_click_count") or (1 if final_send_invoked else 0)),
            "send_confirmation_click_count": int(records.get("send_confirmation_click_count") or (1 if final_send_invoked else 0)),
            "send_success_verified": bool(records.get("send_success_verified")),
            "verification_method": str(records.get("verification_method") or ""),
            "verification_evidence": str(records.get("verification_evidence") or ""),
            "remote_message_id": records.get("remote_message_id") or None,
            "outcome": "sent" if final_send_invoked and scenario_result.get("ok") and records.get("send_success_verified") else "cancelled",
            "failed_step": failed_step,
            "error_code": scenario_result.get("error_code"),
            "error_message": scenario_result.get("error_message"),
            "scenario_result": scenario_result,
            "diagnostics": records,
        }

    def verify_delivery(self, session: dict[str, Any], plan: Any, result: dict[str, Any]) -> dict[str, Any]:
        return result

    def reset_session_after_job(self, session: Any, plan: Any, result: dict[str, Any]) -> dict[str, Any]:
        if result.get("diagnostics_consistent") is False:
            return {"ok": False, "error_code": "previous_delivery_state_uncertain", "message": "Diagnostics were inconsistent after delivery"}
        if result.get("error_code") in {"multiple_recipients_selected", "unexpected_forward_recipient_count", "forward_confirm_failed", "selector_regression", "browser_profile_corruption"}:
            return {"ok": False, "error_code": str(result.get("error_code")), "message": "Unsafe delivery state for session reuse"}
        page = getattr(session, "page", None)
        started_ok = True
        try:
            if page is not None and hasattr(page, "keyboard"):
                page.keyboard.press("Escape")
        except Exception:
            started_ok = False
        return {"ok": started_ok, "error_code": None if started_ok else "session_reset_failed"}

    def reset_session(self, session: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}

    def close_runtime_session(self, session: dict[str, Any]) -> dict[str, Any]:
        if hasattr(session, "session_id"):
            return self.plugin.close_reusable_runtime_session(session)
        return {"ok": True}

    def classify_error(self, result: dict[str, Any]) -> dict[str, Any]:
        from modules.automation_engine.commercial_queue.errors import classify_error

        return classify_error(result, component="bale_adapter").to_dict()
