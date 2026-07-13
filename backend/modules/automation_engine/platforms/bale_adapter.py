from __future__ import annotations

from typing import Any

from modules.automation_engine.commercial_queue.errors import classify_error
from modules.automation_engine.commercial_queue.operations import operation_registry
from modules.automation_engine.plugins.bale import bale_plugin
from modules.automation_engine.plugins.bale.plugin import _native_profile_dir


class BaleDeliveryAdapter:
    platform_name = "bale"

    def __init__(self, plugin: Any | None = None) -> None:
        self.plugin = plugin or bale_plugin

    def validate_execution_plan(self, plan: Any) -> dict[str, Any]:
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
        return classify_error(result, component="bale_adapter").to_dict()
