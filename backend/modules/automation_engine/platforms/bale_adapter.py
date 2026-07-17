from __future__ import annotations

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
        self.records["source_url"] = url
        return {"ok": True, "url": url, "timeout_ms": timeout_ms}

    def call_platform_primitive(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        if name == "validate_operation_mode":
            mode = str(params.get("operation_mode") or "")
            if mode not in {"inspect_only", "selection_only", "no_send", "live_send"}:
                return {"ok": False, "error_code": "unsupported_operation_mode", "message": "Unsupported Bale scenario operation_mode"}
            return {"ok": True}
        if name == "verify_source_chat":
            result = self.plugin.open_bale_source_channel(
                account_id=self.plan.account_id,
                source_channel_uid=str(params.get("source_uid") or self.plan.source_channel_uid),
                provider_mode="native_chrome",
            )
            self.source_verified = bool(result.get("success"))
            self.records.update({
                "source_resolved": self.source_verified,
                "source_timeline_detected": bool(result.get("message_stream_visible")),
                "source_diagnostics": result,
            })
            return {"ok": self.source_verified, "error_code": result.get("error_code"), "message": result.get("error_message"), **result}
        if name == "select_source_message":
            return {"ok": True}
        if name == "open_forward_picker":
            self.forward_picker_open = True
            return {"ok": True}
        return {"ok": False, "error_code": "platform_primitive_not_implemented", "primitive": name}

    def fill(self, element_name: str, element: dict[str, Any], value: str, timeout_ms: int | None = None) -> dict[str, Any]:
        if element_name == "recipient_search" and self.forward_boundary_result is None:
            result = self.plugin.forward_message_to_contact(
                account_id=self.plan.account_id,
                source_channel_uid=self.plan.source_channel_uid,
                display_name=self.plan.display_name,
                dry_run=False,
                selection_only=True,
                provider_mode="native_chrome",
                runtime_session=self.runtime_session,
                close_session_when_done=self.runtime_session is None,
            )
            self.forward_boundary_result = result
            self.records.update({
                "recipient_resolved": bool(result.get("success")),
                "confirmation_visible": bool(result.get("confirm_button_selector")),
                "stopped_before_send": True,
                "forward_boundary_diagnostics": result,
            })
            return {"ok": bool(result.get("success")), "error_code": result.get("error_code"), "message": result.get("error_message"), **result}
        return super().fill(element_name, element, value, timeout_ms)

    def click(self, element_name: str, element: dict[str, Any], timeout_ms: int | None = None) -> dict[str, Any]:
        if element_name == "final_forward_confirmation":
            self.final_send_invoked = True
            result = self.plugin.forward_message_to_contact(
                account_id=self.plan.account_id,
                source_channel_uid=self.plan.source_channel_uid,
                display_name=self.plan.display_name,
                dry_run=False,
                selection_only=False,
                provider_mode="native_chrome",
                runtime_session=self.runtime_session,
                close_session_when_done=self.runtime_session is None,
            )
            return {"ok": bool(result.get("success")), "error_code": result.get("error_code"), "message": result.get("error_message"), **result}
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
        runner = ScenarioRunner(BaleScenarioActionExecutor(self.plugin, plan, runtime_session=runtime_session))
        scenario_result = runner.run(scenario, context).to_dict()
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
