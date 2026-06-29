from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .dispatcher import ActionDispatcher, ActionNotFoundError
from .schemas import ScenarioDefinition, ScenarioStep, TaskResult, TaskStatus
from .state import TaskState


class ScenarioValidationError(ValueError):
    pass


class ScenarioEngine:
    def __init__(self, dispatcher: ActionDispatcher) -> None:
        self.dispatcher = dispatcher

    def load_scenario(self, path: str | Path) -> ScenarioDefinition:
        scenario_path = Path(path)
        with scenario_path.open("r", encoding="utf-8") as scenario_file:
            raw_data = json.load(scenario_file)
        return self.validate_scenario(raw_data)

    def validate_scenario(self, data: dict[str, Any]) -> ScenarioDefinition:
        required_fields = ("name", "version", "platform", "steps")
        for field_name in required_fields:
            if field_name not in data:
                raise ScenarioValidationError(f"Missing required scenario field: {field_name}")

        if not isinstance(data["steps"], list) or not data["steps"]:
            raise ScenarioValidationError("Scenario steps must be a non-empty list")

        steps: list[ScenarioStep] = []
        for index, raw_step in enumerate(data["steps"]):
            if not isinstance(raw_step, dict):
                raise ScenarioValidationError(f"Step {index} must be an object")
            action = raw_step.get("action")
            params = raw_step.get("params", {})
            if not isinstance(action, str) or not action:
                raise ScenarioValidationError(f"Step {index} must include a non-empty action")
            if not isinstance(params, dict):
                raise ScenarioValidationError(f"Step {index} params must be an object")
            steps.append(ScenarioStep(action=action, params=params))

        return ScenarioDefinition(
            name=str(data["name"]),
            version=str(data["version"]),
            platform=str(data["platform"]),
            steps=steps,
        )

    def execute(self, scenario: ScenarioDefinition, task_id: str = "manual") -> TaskResult:
        state = TaskState(task_id=task_id)
        state.mark_running()
        state.add_log(
            f"Scenario loaded: {scenario.name} v{scenario.version} on {scenario.platform}"
        )

        for index, step in enumerate(scenario.steps):
            state.current_step_index = index
            state.add_log(f"Step {index} started: {step.action}")
            try:
                result = self.dispatcher.execute(step.action, step.params)
            except ActionNotFoundError as exc:
                state.mark_failed(str(exc))
                return TaskResult(
                    task_id=task_id,
                    status=TaskStatus.FAILED,
                    ok=False,
                    message=str(exc),
                    logs=state.logs,
                )
            except Exception as exc:
                message = f"Step {index} failed: {exc}"
                state.mark_failed(message)
                return TaskResult(
                    task_id=task_id,
                    status=TaskStatus.FAILED,
                    ok=False,
                    message=message,
                    logs=state.logs,
                )

            if not result.get("ok", False):
                message = str(result.get("message", f"Step {index} failed"))
                state.mark_failed(message)
                return TaskResult(
                    task_id=task_id,
                    status=TaskStatus.FAILED,
                    ok=False,
                    message=message,
                    logs=state.logs,
                )

            state.add_log(f"Step {index} completed: {result.get('message', '')}")

        state.mark_success()
        return TaskResult(
            task_id=task_id,
            status=TaskStatus.SUCCESS,
            ok=True,
            message="Scenario executed successfully",
            logs=state.logs,
        )

