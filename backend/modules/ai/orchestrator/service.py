from typing import Any

from modules.ai.orchestrator.decision_engine import DecisionEngine
from modules.ai.orchestrator.job_scoring import JobScoring
from modules.ai.orchestrator.schemas import ExecutionPlan
from modules.ai.providers.provider_factory import AIProviderFactory
from modules.platform.services.adapter_registry import AdapterRegistry


class AIOrchestratorService:
    @staticmethod
    def create_execution_plan(job_input: Any):
        context = AIOrchestratorService._build_context(job_input)
        payload = AIOrchestratorService._normalize_payload(context.get("payload") or {})
        AIOrchestratorService._copy_context_fields_to_payload(context, payload)
        execution_type = AIOrchestratorService._execution_type(context, payload)

        available_platforms = AdapterRegistry.list_adapters()
        requested_default = AIOrchestratorService._requested_platform(job_input)

        if execution_type == "BROWSER":
            platform_name, reason, provider_name = "browser", "browser action selected", "rules"
            adapter_available = True
        elif execution_type == "INSTAGRAM":
            platform_name, reason, provider_name = "instagram", "instagram browser action selected", "rules"
            adapter_available = True
        else:
            platform_name, reason, provider_name = AIOrchestratorService._select_platform(
                job_input=job_input,
                context=context,
                available_platforms=available_platforms,
                requested_default=requested_default,
            )
            adapter_available = AdapterRegistry.get_adapter(platform_name) is not None

        priority_score = JobScoring.calculate_priority_score(job_input, platform_name)

        return ExecutionPlan(
            type=execution_type,
            platform=platform_name,
            account_id=AIOrchestratorService._account_id(context),
            payload=payload,
            db=context.get("db"),
            platform_name=platform_name,
            priority_score=priority_score,
            adapter_available=adapter_available,
            reason=reason,
            metadata={
                "available_platforms": available_platforms,
                "decision_engine": "hybrid_ai_rules",
                "ai_provider": provider_name,
            },
        )

    @staticmethod
    def _select_platform(
        job_input: Any,
        context: dict,
        available_platforms: list[str],
        requested_default: str | None,
    ):
        prompt = (
            "Select the best messaging platform for this job. "
            "Return only the platform name when possible."
        )

        for provider_name in AIProviderFactory.provider_order():
            provider = AIProviderFactory.get_provider(provider_name)
            if provider is None:
                continue

            ai_output = provider.generate(prompt, context)
            platform_name, reason = DecisionEngine.select_platform_from_ai_output(
                ai_output,
                available_platforms,
            )
            if platform_name:
                return platform_name, reason, provider_name

        platform_name, reason = DecisionEngine.select_platform(
            job_input=job_input,
            available_platforms=available_platforms,
            default_platform=requested_default,
        )
        return platform_name, reason, "rules"

    @staticmethod
    def _requested_platform(job_input: Any):
        if isinstance(job_input, dict):
            return job_input.get("platform_name") or job_input.get("platform")

        payload = getattr(job_input, "payload", None) or {}
        if isinstance(payload, dict):
            return payload.get("platform_name") or payload.get("platform")

        return None

    @staticmethod
    def _build_context(job_input: Any):
        if isinstance(job_input, dict):
            return job_input

        payload = getattr(job_input, "payload", None) or {}
        return {
            "job_type": getattr(job_input, "job_type", None),
            "priority": getattr(job_input, "priority", None),
            "payload": payload,
            "metadata": payload.get("metadata") if isinstance(payload, dict) else None,
        }

    @staticmethod
    def _execution_type(context: dict[str, Any], payload: dict[str, Any]):
        explicit_type = context.get("type") or payload.get("type")
        if explicit_type:
            return str(explicit_type).upper()

        action = context.get("action") or payload.get("action")
        job_type = context.get("job_type") or payload.get("job_type")
        platform = (
            context.get("platform")
            or context.get("platform_name")
            or payload.get("platform")
            or payload.get("platform_name")
        )

        normalized_action = str(action or "").lower()
        normalized_job_type = str(job_type or "").lower()
        normalized_platform = str(platform or "").lower()

        if normalized_action == "instagram_send_message" or (
            normalized_platform == "instagram" and payload.get("chat_url")
        ):
            return "INSTAGRAM"

        if normalized_action in {"open_page", "click", "type", "wait"}:
            return "BROWSER"

        if normalized_job_type in {"browser", "browser_action"}:
            return "BROWSER"

        return "MESSAGE"

    @staticmethod
    def _normalize_payload(payload: Any):
        if not isinstance(payload, dict):
            return {}

        normalized = dict(payload)
        if "message" not in normalized and "text" in normalized:
            normalized["message"] = normalized["text"]
        if "text" not in normalized and "message" in normalized:
            normalized["text"] = normalized["message"]
        return normalized

    @staticmethod
    def _account_id(context: dict[str, Any]):
        payload = context.get("payload") if isinstance(context.get("payload"), dict) else {}
        account_id = (
            context.get("account_id")
            or context.get("communication_account_id")
            or payload.get("account_id")
            or payload.get("communication_account_id")
        )
        return account_id

    @staticmethod
    def _copy_context_fields_to_payload(context: dict[str, Any], payload: dict[str, Any]):
        for key in (
            "action",
            "url",
            "selector",
            "text",
            "chat_url",
            "message",
            "target",
            "chat_id",
            "human",
            "timeout",
        ):
            if key not in payload and context.get(key) is not None:
                payload[key] = context[key]
