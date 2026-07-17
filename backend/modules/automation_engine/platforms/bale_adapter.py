from __future__ import annotations

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
        self.exact_recipient: dict[str, Any] | None = None

    def element_exists(self, name: str, element: dict[str, Any] | None = None) -> bool:
        if name == "source_timeline":
            return self.source_verified
        if name in {"forward_picker", "recipient_search"}:
            return self.forward_picker_open
        if name == "recipient_result":
            return bool(self.forward_boundary_result and self.forward_boundary_result.get("success"))
        if name == "final_forward_confirmation":
            return bool(self.forward_boundary_result and (self.forward_boundary_result.get("final_send_control_visible") or self.forward_boundary_result.get("confirm_button_selector")))
        return False

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
        if name == "verify_source_chat":
            source_uid = str(params.get("source_uid") or self.plan.source_channel_uid)
            page = self._ensure_page()
            current_url = ""
            try:
                current_url = str(page.url)
            except Exception:
                current_url = ""
            readiness: dict[str, Any] = {}
            for _ in range(10):
                readiness = self.plugin._source_channel_readiness(page)
                if readiness.get("ready"):
                    break
                try:
                    page.wait_for_timeout(500)
                except Exception:
                    pass
            self.source_verified = bool(readiness.get("ready")) and f"uid={source_uid}" in current_url
            self.records.update({
                "source_resolved": self.source_verified,
                "source_timeline_detected": bool(readiness.get("message_stream_visible")),
                "source_header_text": str(readiness.get("target_channel_header_text") or ""),
                "source_diagnostics": {"current_url": current_url, **readiness},
            })
            return {"ok": self.source_verified, "error_code": None if self.source_verified else "source_channel_not_ready", **readiness}
        if name == "select_source_message":
            page = self._ensure_page()
            latest = self.plugin._resolve_latest_forward_message_target(page)
            self.latest_message_selector = str(latest.get("latest_message_selector") or "")
            if not latest.get("message_found") or not self.latest_message_selector:
                return {"ok": False, "error_code": "latest_message_not_found", **latest}
            try:
                locator = page.locator(self.latest_message_selector).first
                locator.scroll_into_view_if_needed(timeout=1000)
                locator.hover(timeout=1000)
            except Exception as exc:
                return {"ok": False, "error_code": "latest_message_hover_failed", "message": str(exc), **latest}
            self.records.update({
                "source_message_selected": True,
                "selected_message_preview": str(latest.get("latest_message_text_preview") or ""),
                "selected_message_signature": str(latest.get("latest_message_signature") or ""),
            })
            return {"ok": True}
        if name == "open_forward_picker":
            page = self._ensure_page()
            menu_state = self.plugin._message_forward_menu_candidates(page, self.latest_message_selector)
            menu_selector = str(menu_state.get("message_menu_selector") or "")
            if not menu_selector:
                return {"ok": False, "error_code": "message_menu_not_found", **menu_state}
            menu_click = self.plugin._click_selector_short(page, menu_selector, timeout_ms=1000)
            if menu_click.get("status") != "success":
                return {"ok": False, "error_code": "message_menu_open_failed", "click_result": menu_click}
            try:
                page.wait_for_timeout(300)
            except Exception:
                pass
            picker_state = self.plugin._forward_picker_state(page)
            if not picker_state.get("forward_picker_visible"):
                forward_state = self.plugin._forward_option_candidates(page)
                forward_selector = str(forward_state.get("forward_option_selector") or "")
                if not forward_selector:
                    return {"ok": False, "error_code": "forward_option_not_found", **forward_state}
                forward_click = self.plugin._click_selector_short(page, forward_selector, timeout_ms=1000)
                if forward_click.get("status") != "success":
                    return {"ok": False, "error_code": "forward_option_click_failed", "click_result": forward_click}
                try:
                    page.wait_for_timeout(500)
                except Exception:
                    pass
                picker_state = self.plugin._forward_picker_state(page)
            self.forward_picker_open = bool(picker_state.get("forward_picker_visible"))
            self.records["recipient_picker_visible"] = self.forward_picker_open
            return {"ok": self.forward_picker_open, "error_code": None if self.forward_picker_open else "recipient_picker_not_found", **picker_state}
        return {"ok": False, "error_code": "platform_primitive_not_implemented", "primitive": name}

    def fill(self, element_name: str, element: dict[str, Any], value: str, timeout_ms: int | None = None) -> dict[str, Any]:
        if element_name == "recipient_search":
            page = self._ensure_page()
            search_state = self.plugin._forward_recipient_search_state(page)
            self.search_selector = str(search_state.get("recipient_search_selector") or "")
            if not self.search_selector:
                return {"ok": False, "error_code": "recipient_search_input_not_found", **search_state}
            try:
                self._type_recipient_search_value(page, self.search_selector, value)
            except Exception as exc:
                return {"ok": False, "error_code": "recipient_search_fill_failed", "message": str(exc), **search_state}
            search_value = self.plugin._forward_search_input_value(page, self.search_selector)
            self.records.update({
                "recipient_search_value": search_value,
            })
            return {"ok": search_value == value, "error_code": None if search_value == value else "search_value_not_verified", "search_input_value": search_value, **search_state}
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
        if element_name == "recipient_result":
            page = self._ensure_page()
            stability = self.plugin._forward_recipient_results_stability(page, exact_text)
            candidates = stability.get("recipient_candidates") if isinstance(stability.get("recipient_candidates"), list) else []
            exact_matches = [item for item in candidates if isinstance(item, dict) and item.get("exact_match")]
            if not stability.get("result_set_stable"):
                return {"ok": False, "error_code": "recipient_results_not_stable", **stability}
            if len(exact_matches) != 1:
                return {"ok": False, "error_code": "recipient_not_found", **stability}
            self.exact_recipient = exact_matches[0]
            recipient_selector = str(self.exact_recipient.get("click_selector") or self.exact_recipient.get("selector") or "")
            click_diagnostic = self.plugin._forward_recipient_click_diagnostic(page, recipient_selector, exact_text)
            if not click_diagnostic.get("click_safe"):
                return {"ok": False, "error_code": "destructive_click_blocked", **click_diagnostic, **stability}
            selected_before = self.plugin._forward_selected_recipients_state(page)
            if int(selected_before.get("selected_count") or 0) != 0:
                return {"ok": False, "error_code": "multiple_recipients_selected", **selected_before}
            select_click = self.plugin._click_selector_short(page, recipient_selector, timeout_ms=1000)
            if select_click.get("status") != "success":
                return {"ok": False, "error_code": "recipient_select_failed", "click_result": select_click}
            try:
                page.wait_for_timeout(500)
            except Exception:
                pass
            selected_after = self.plugin._forward_selected_recipients_state(page)
            confirm_state = self.plugin._forward_confirm_button_state(page)
            selected_names = selected_after.get("selected_names") if isinstance(selected_after.get("selected_names"), list) else []
            resolved = selected_names == [exact_text]
            self.forward_boundary_result = {
                "success": resolved,
                "confirm_button_selector": str(confirm_state.get("confirm_button_selector") or ""),
                "selected_names": selected_names,
                "recipient_candidates": candidates,
            }
            self.records.update({
                "recipient_resolved": resolved,
                "recipient_result_text": str(self.exact_recipient.get("row_name") or self.exact_recipient.get("exact_text") or exact_text),
                "recipient_selected": resolved,
                "confirmation_visible": bool(confirm_state.get("confirm_button_selector")),
                "stopped_before_send": True,
                "forward_boundary_diagnostics": {"selected_after": selected_after, "confirm_state": confirm_state, "candidates": candidates},
            })
            return {"ok": resolved, "error_code": None if resolved else "unexpected_selected_recipient", **self.forward_boundary_result}
        return super().choose_from_list(element_name, element, exact_text, timeout_ms)

    def click(self, element_name: str, element: dict[str, Any], timeout_ms: int | None = None) -> dict[str, Any]:
        if element_name == "final_forward_confirmation":
            self.final_send_invoked = True
            confirm_selector = str((self.forward_boundary_result or {}).get("confirm_button_selector") or "")
            if not confirm_selector:
                return {"ok": False, "error_code": "forward_confirm_button_not_found"}
            click_result = self.plugin._click_selector_short(self._ensure_page(), confirm_selector, timeout_ms=1000)
            return {"ok": click_result.get("status") == "success", "error_code": None if click_result.get("status") == "success" else "forward_confirm_failed", "click_result": click_result}
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
        return self.plugin.forward_latest_channel_message(
            job_id=plan.job_id,
            campaign_id=plan.campaign_id,
            account_id=plan.account_id,
            recipient_id=plan.recipient_id,
            idempotency_key=plan.job_id,
            phone=plan.phone,
            display_name=plan.display_name,
            source_channel_uid=plan.source_channel_uid,
            dry_run=plan.dry_run,
            execution_plan=plan,
            runtime_session=runtime_session,
            close_session_when_done=runtime_session is None,
        )

    def execute_plan(self, plan: Any, runtime_session: Any | None = None) -> dict[str, Any]:
        return self.plugin.forward_latest_channel_message(
            job_id=plan.job_id,
            campaign_id=plan.campaign_id,
            account_id=plan.account_id,
            recipient_id=plan.recipient_id,
            idempotency_key=plan.job_id,
            phone=plan.phone,
            display_name=plan.display_name,
            source_channel_uid=plan.source_channel_uid,
            dry_run=plan.dry_run,
            execution_plan=plan,
            runtime_session=runtime_session,
            close_session_when_done=runtime_session is None,
        )

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
            "confirm_click_count": 1 if final_send_invoked else 0,
            "remote_message_id": None,
            "outcome": "sent" if final_send_invoked and scenario_result.get("ok") else "cancelled",
            "failed_step": scenario_result.get("failed_step"),
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
