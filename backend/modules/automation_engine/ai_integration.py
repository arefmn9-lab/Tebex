from __future__ import annotations

from typing import Any

from .ai.agent_manager import AgentManager
from .learning.decision_logger import DecisionLogger
from .learning.learning_engine import LearningEngine
from .learning.outcome_tracker import OutcomeTracker
from .learning.prompt_optimizer import PromptOptimizer
from .schemas import ScenarioDefinition, ScenarioStep, TaskResult


class AIExecutionMiddleware:
    def __init__(
        self,
        agent_manager: AgentManager | None = None,
        decision_logger: DecisionLogger | None = None,
        outcome_tracker: OutcomeTracker | None = None,
    ) -> None:
        self.agent_manager = agent_manager or AgentManager()
        self.decision_logger = decision_logger or DecisionLogger()
        self.outcome_tracker = outcome_tracker or OutcomeTracker()
        self.learning_engine = LearningEngine(self.outcome_tracker)
        self.prompt_optimizer = PromptOptimizer(self.learning_engine)
        self._task_decisions: dict[str, dict[str, Any]] = {}

    def before_execution(
        self,
        task: dict[str, Any],
        account_id: str,
        scenario: ScenarioDefinition,
    ) -> dict[str, Any]:
        agent = self.agent_manager.get_agent(account_id)
        task_context = self._build_task_context(task, scenario)
        prompt = self.agent_manager.prompt_engine.build_prompt(agent, task_context)
        decision = self.agent_manager.decide(account_id, prompt)

        approved_decision = {
            "intent": decision.get("intent", "general_message"),
            "action": decision.get("action", "approve_execution"),
            "confidence": decision.get("confidence", 0.5),
            "response": decision.get("response", ""),
            "approve": decision.get("approve", True),
        }

        if "modify_scenario" in decision:
            approved_decision["modify_scenario"] = decision["modify_scenario"]

        selected_scenario = scenario
        if approved_decision.get("modify_scenario"):
            selected_scenario = self._build_modified_scenario(
                approved_decision["modify_scenario"],
                fallback=scenario,
            )

        decision_record = self.decision_logger.log_decision(
            account_id=account_id,
            task_id=str(task["task_id"]),
            scenario=scenario.name,
            ai_decision=approved_decision,
        )
        self._task_decisions[str(task["task_id"])] = decision_record

        return {
            "decision": approved_decision,
            "scenario": selected_scenario,
        }

    def after_execution(
        self,
        account_id: str,
        task: dict[str, Any],
        result: TaskResult | None = None,
        error: str | None = None,
    ) -> None:
        task_id = str(task["task_id"])
        decision_record = self._task_decisions.get(task_id)
        if result is not None:
            status = "success" if result.ok else "failure"
            feedback = (
                f"Task {task['task_id']} completed with {status}. "
                f"Message: {result.message}. Logs: {result.logs}"
            )
            logs = result.logs
            success = result.ok
        else:
            feedback = f"Task {task['task_id']} failed before completion. Error: {error}"
            logs = []
            success = False

        if decision_record is not None:
            outcome = self.outcome_tracker.track_outcome(
                decision_record=decision_record,
                success=success,
                logs=logs,
                error_reason=error,
            )
            patterns = self.learning_engine.account_performance_patterns(account_id)
            suggestions = self.prompt_optimizer.suggest_improvements(account_id)
            feedback = (
                f"{feedback} Learning outcome: {outcome}. "
                f"Account performance: {patterns}. "
                f"Prompt suggestions: {suggestions['suggestions']}"
            )

        self.agent_manager.update_memory(account_id, feedback, role="system")

    def _build_task_context(
        self,
        task: dict[str, Any],
        scenario: ScenarioDefinition,
    ) -> str:
        steps = [
            f"{index}: {step.action} params={step.params}"
            for index, step in enumerate(scenario.steps)
        ]
        return (
            "Automation task pending execution.\n"
            f"task_id: {task['task_id']}\n"
            f"account_id: {task.get('account_id', 'default')}\n"
            f"scenario_path: {task.get('scenario_path')}\n"
            f"scenario: {scenario.name} v{scenario.version} platform={scenario.platform}\n"
            f"steps:\n" + "\n".join(steps) + "\n"
            "Return a decision only. Do not execute actions directly."
        )

    def _build_modified_scenario(
        self,
        value: Any,
        fallback: ScenarioDefinition,
    ) -> ScenarioDefinition:
        if isinstance(value, ScenarioDefinition):
            return value

        if not isinstance(value, dict):
            return fallback

        raw_steps = value.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            return fallback

        steps = []
        for raw_step in raw_steps:
            if not isinstance(raw_step, dict) or not raw_step.get("action"):
                return fallback
            params = raw_step.get("params", {})
            if not isinstance(params, dict):
                return fallback
            steps.append(ScenarioStep(action=str(raw_step["action"]), params=params))

        return ScenarioDefinition(
            name=str(value.get("name", fallback.name)),
            version=str(value.get("version", fallback.version)),
            platform=str(value.get("platform", fallback.platform)),
            steps=steps,
        )
