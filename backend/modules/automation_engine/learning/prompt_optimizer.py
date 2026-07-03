from __future__ import annotations

from typing import Any

from ..ai.agent_context import AccountAgentContext
from .learning_engine import LearningEngine


class PromptOptimizer:
    def __init__(self, learning_engine: LearningEngine) -> None:
        self.learning_engine = learning_engine

    def suggest_improvements(self, account_id: str) -> dict[str, Any]:
        patterns = self.learning_engine.account_performance_patterns(account_id)
        decision_rates = patterns.get("decision_success_rates", {})
        best_action = None
        if decision_rates:
            best_action = max(decision_rates, key=decision_rates.get)

        suggestions = []
        success_rate = patterns.get("success_rate")
        if success_rate is not None and success_rate < 0.5:
            suggestions.append("Use a more cautious prompt and request more context before action.")
        if best_action:
            suggestions.append(f"Prefer action '{best_action}' when context is similar.")

        return {
            "account_id": account_id,
            "suggested_action": best_action,
            "suggestions": suggestions,
            "patterns": patterns,
        }

    def apply_prompt_update(self, agent: AccountAgentContext) -> str:
        improvement = self.suggest_improvements(agent.account_id)
        suggestions = improvement["suggestions"]
        if not suggestions:
            return agent.system_prompt

        base_prompt = agent.system_prompt or (
            f"You are a {agent.behavior_profile} ClinicOS automation agent."
        )
        learning_note = " Learning guidance: " + " ".join(suggestions)
        agent.system_prompt = base_prompt + learning_note
        return agent.system_prompt

