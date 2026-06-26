from typing import Any

from modules.ai.orchestrator.decision_engine import DecisionEngine
from modules.ai.orchestrator.job_scoring import JobScoring
from modules.ai.orchestrator.schemas import ExecutionPlan
from modules.ai.providers.provider_factory import AIProviderFactory
from modules.platform.services.adapter_registry import AdapterRegistry


class AIOrchestratorService:
    @staticmethod
    def create_execution_plan(job_input: Any):
        available_platforms = AdapterRegistry.list_adapters()
        requested_default = AIOrchestratorService._requested_platform(job_input)
        context = AIOrchestratorService._build_context(job_input)

        platform_name, reason, provider_name = AIOrchestratorService._select_platform(
            job_input=job_input,
            context=context,
            available_platforms=available_platforms,
            requested_default=requested_default,
        )
        adapter_available = AdapterRegistry.get_adapter(platform_name) is not None
        priority_score = JobScoring.calculate_priority_score(job_input, platform_name)

        return ExecutionPlan(
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
