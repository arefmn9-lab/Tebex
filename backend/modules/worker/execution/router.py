from datetime import datetime, timezone
from typing import Any

from modules.account_control.account_manager import AccountManager
from modules.browser.adapters.instagram_adapter import InstagramAdapter
from modules.browser.engine.browser_engine import BrowserEngine
from modules.platform.schemas.message import UnifiedMessageSchema
from modules.platform.services.adapter_registry import AdapterRegistry
from modules.production.error_handler import ProductionErrorHandler
from modules.production.logger import ProductionLogger
from modules.production.metrics import MetricsEngine


class ExecutionRouter:
    BROWSER_ACTIONS = {"open_page", "click", "type", "wait"}
    _logs: list[dict[str, Any]] = []

    @staticmethod
    def execute(plan: Any):
        try:
            normalized_plan = ExecutionRouter._normalize_plan(plan)
            execution_type = normalized_plan.get("type")

            if execution_type == "BROWSER":
                return ExecutionRouter._record_result(
                    normalized_plan,
                    ExecutionRouter._execute_browser(normalized_plan),
                )

            if execution_type == "MESSAGE":
                return ExecutionRouter._record_result(
                    normalized_plan,
                    ExecutionRouter._execute_message(normalized_plan),
                )

            if execution_type == "INSTAGRAM":
                return ExecutionRouter._record_result(
                    normalized_plan,
                    ExecutionRouter._execute_instagram(normalized_plan),
                )

            return ExecutionRouter._record_result(normalized_plan, {
                "success": False,
                "error": "invalid execution type",
                "type": execution_type,
            })
        except Exception as exc:
            return ExecutionRouter._record_result(
                {},
                ProductionErrorHandler.handle(exc),
            )

    @staticmethod
    def _execute_browser(plan: dict[str, Any]):
        payload = plan.get("payload") or {}
        action = payload.get("action")

        if action not in ExecutionRouter.BROWSER_ACTIONS:
            return {
                "success": False,
                "error": "unsupported browser action",
                "action": action,
            }

        engine = BrowserEngine()
        try:
            if action == "open_page":
                page = engine.open_page(payload["url"])
                return {"success": True, "type": "BROWSER", "action": action, "url": page.url}

            if action == "click":
                engine.click(payload["selector"])
                return {"success": True, "type": "BROWSER", "action": action}

            if action == "type":
                engine.type(
                    payload["selector"],
                    payload["text"],
                    human=payload.get("human", True),
                )
                return {"success": True, "type": "BROWSER", "action": action}

            if action == "wait":
                engine.wait(payload["selector"], timeout=payload.get("timeout", 30000))
                return {"success": True, "type": "BROWSER", "action": action}
        finally:
            engine.close()

    @staticmethod
    def _execute_message(plan: dict[str, Any]):
        account_check = AccountManager.prepare_execution_plan(plan)
        if not account_check["allowed"]:
            return {
                "success": False,
                "error": account_check["error"],
                "type": "MESSAGE",
            }

        plan = ExecutionRouter._normalize_plan(account_check["plan"])
        platform = plan.get("platform")
        adapter = AdapterRegistry.get_adapter(platform)
        if adapter is None:
            return {
                "success": False,
                "error": "adapter not found",
                "platform": platform,
            }

        db = plan.get("db")
        account_id = plan.get("account_id")
        payload = plan.get("payload") or {}
        message = ExecutionRouter._message_from_payload(payload)

        result = adapter.send_message(
            db=db,
            communication_account_id=account_id,
            message=message,
        )
        AccountManager.register_send(account_id, platform)
        account = AccountManager.get_account(account_id, platform)
        if account is not None:
            AccountManager.remember_message(account, payload)
        return {
            "success": True,
            "type": "MESSAGE",
            "platform": platform,
            "result": result,
        }

    @staticmethod
    def _execute_instagram(plan: dict[str, Any]):
        account_check = AccountManager.prepare_execution_plan(plan)
        if not account_check["allowed"]:
            return {
                "success": False,
                "error": account_check["error"],
                "type": "INSTAGRAM",
            }

        plan = ExecutionRouter._normalize_plan(account_check["plan"])
        payload = plan.get("payload") or {}
        account_id = plan.get("account_id")
        platform = plan.get("platform") or "instagram"
        adapter = InstagramAdapter()
        try:
            result = adapter.send_message(
                chat_url=payload["chat_url"],
                text=payload["text"],
            )
            AccountManager.register_send(account_id, platform)
            account = AccountManager.get_account(account_id, platform)
            if account is not None:
                AccountManager.remember_message(account, payload)
            return {
                "success": True,
                "type": "INSTAGRAM",
                "platform": "instagram",
                "result": result,
            }
        finally:
            adapter.close()

    @staticmethod
    def _message_from_payload(payload: dict[str, Any]):
        message = payload.get("message")
        if isinstance(message, UnifiedMessageSchema):
            return message

        return UnifiedMessageSchema(
            chat_id=str(payload.get("chat_id") or payload.get("target") or ""),
            message=str(message or payload.get("text") or ""),
            attachments=payload.get("attachments"),
            metadata=payload.get("metadata"),
        )

    @staticmethod
    def _normalize_plan(plan: Any):
        if hasattr(plan, "model_dump"):
            plan = plan.model_dump()
        elif not isinstance(plan, dict):
            plan = {
                "type": getattr(plan, "type", None),
                "platform": getattr(plan, "platform", None),
                "account_id": getattr(plan, "account_id", None),
                "payload": getattr(plan, "payload", None),
                "db": getattr(plan, "db", None),
            }

        normalized = dict(plan)
        normalized["type"] = str(normalized.get("type") or "").upper()
        normalized["platform"] = normalized.get("platform") or normalized.get("platform_name")
        normalized["payload"] = normalized.get("payload") or {}
        return normalized

    @classmethod
    def logs(cls, limit: int = 100):
        return cls._logs[-limit:]

    @classmethod
    def _record_result(cls, plan: dict[str, Any], result: dict[str, Any]):
        MetricsEngine.record_execution(plan, result)
        ProductionLogger.log(
            "execution",
            "execution_completed",
            account_id=plan.get("account_id"),
            platform=plan.get("platform"),
            execution_type=plan.get("type"),
            level="info" if result.get("success") else "error",
            context={
                "success": result.get("success"),
                "error": result.get("error"),
                "result": result,
            },
        )
        cls._logs.append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "type": plan.get("type"),
                "platform": plan.get("platform"),
                "account_id": plan.get("account_id"),
                "success": result.get("success"),
                "error": result.get("error"),
                "result": result,
            }
        )
        cls._logs = cls._logs[-500:]
        return result
