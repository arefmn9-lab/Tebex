from typing import Any

from modules.ai.orchestrator.decision_engine import DecisionEngine
from modules.ai.orchestrator.job_scoring import JobScoring
from modules.ai.orchestrator.schemas import ExecutionPlan
from modules.platform.services.adapter_registry import AdapterRegistry


class AIOrchestratorService:
    @staticmethod
    def create_execution_plan(job_input: Any):
        available_platforms = AdapterRegistry.list_adapters()
        requested_default = AIOrchestratorService._requested_platform(job_input)
        platform_name, reason = DecisionEngine.select_platform(
            job_input=job_input,
            available_platforms=available_platforms,
            default_platform=requested_default,
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
                "decision_engine": "rule_based_mvp",
            },
        )

    @staticmethod
    def _requested_platform(job_input: Any):
        if isinstance(job_input, dict):
            return job_input.get("platform_name") or job_input.get("platform")

        payload = getattr(job_input, "payload", None) or {}
        if isinstance(payload, dict):
            return payload.get("platform_name") or payload.get("platform")

        return None
