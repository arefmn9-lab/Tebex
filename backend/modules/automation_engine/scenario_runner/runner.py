from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from .actions import ScenarioActionExecutor
from .conditions import evaluate_condition, get_path
from .models import ScenarioResult, ScenarioStepResult


_TOKEN_RE = re.compile(r"{{\s*([^}]+)\s*}}")


class ScenarioRunner:
    def __init__(self, action_executor: ScenarioActionExecutor | None = None) -> None:
        self.action_executor = action_executor or ScenarioActionExecutor()

    @staticmethod
    def load(path: str | Path) -> dict[str, Any]:
        with Path(path).open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def run(self, scenario: dict[str, Any], context: dict[str, Any]) -> ScenarioResult:
        scenario_copy = copy.deepcopy(scenario)
        scenario_id = str(scenario_copy.get("id") or scenario_copy.get("scenario_id") or "scenario")
        elements = scenario_copy.get("elements") or {}
        records: dict[str, Any] = {}
        steps: list[ScenarioStepResult] = []

        def exists(element_name: str) -> bool:
            return self.action_executor.element_exists(element_name, elements.get(element_name))

        def fail(step: dict[str, Any], error_code: str, message: str | None = None, details: dict[str, Any] | None = None) -> ScenarioResult:
            step_id = str(step.get("id") or step.get("step_id") or step.get("action") or "unknown")
            action = str(step.get("action") or step.get("type") or "")
            steps.append(ScenarioStepResult(step_id, action, "failed", error_code, message, details or {}))
            return ScenarioResult(False, scenario_id, step_id, error_code, message, bool(records.get("stopped_before_send")), self.action_executor.final_send_invoked, {**records, **self.action_executor.records}, steps)

        required_context = list(scenario_copy.get("required_context") or [])
        for key in required_context:
            if get_path(context, str(key)) in {None, ""}:
                return fail({"id": "validate_context", "action": "validate_context"}, "missing_runtime_context", f"Missing runtime context: {key}", {"context_key": key})

        for index, step in enumerate(scenario_copy.get("steps") or [], start=1):
            step = self._substitute(step, context)
            step_id = str(step.get("id") or step.get("step_id") or f"step_{index}")
            action = str(step.get("action") or step.get("type") or "")
            condition = step.get("when")
            if condition and not evaluate_condition(str(condition), context, exists):
                steps.append(ScenarioStepResult(step_id, action, "skipped"))
                continue

            if action == "stop":
                stopped_before_send = bool(step.get("stopped_before_send"))
                if stopped_before_send:
                    records["stopped_before_send"] = True
                steps.append(ScenarioStepResult(step_id, action, "stopped", details={"reason": step.get("reason")}))
                if step.get("ok") is False:
                    return ScenarioResult(False, scenario_id, step_id, str(step.get("error_code") or "scenario_stopped"), str(step.get("message") or step.get("reason") or "Scenario stopped"), stopped_before_send, self.action_executor.final_send_invoked, {**records, **self.action_executor.records}, steps)
                return ScenarioResult(True, scenario_id, None, None, None, stopped_before_send, self.action_executor.final_send_invoked, {**records, **self.action_executor.records}, steps)

            result = self._execute_action(action, step, elements)
            ok = bool(result.get("ok"))
            steps.append(ScenarioStepResult(step_id, action, "success" if ok else "failed", result.get("error_code"), result.get("message"), result))
            if not ok:
                return ScenarioResult(False, scenario_id, step_id, str(result.get("error_code") or step.get("error_code") or "scenario_step_failed"), result.get("message"), bool(records.get("stopped_before_send")), self.action_executor.final_send_invoked, {**records, **self.action_executor.records}, steps)
            if action == "record_result":
                records[str(step.get("name"))] = step.get("value")

        return ScenarioResult(True, scenario_id, None, None, None, bool(records.get("stopped_before_send")), self.action_executor.final_send_invoked, {**records, **self.action_executor.records}, steps)

    def _execute_action(self, action: str, step: dict[str, Any], elements: dict[str, Any]) -> dict[str, Any]:
        element_name = str(step.get("element") or "")
        element = elements.get(element_name) or {}
        timeout_ms = int(step.get("timeout_ms") or element.get("timeout_ms") or element.get("timeout") or 5000)
        if action == "navigate":
            return self.action_executor.navigate(str(step.get("url") or step.get("value") or ""), timeout_ms)
        if action == "wait_for":
            return self.action_executor.wait_for(element_name, element, timeout_ms)
        if action == "click":
            return self.action_executor.click(element_name, element, timeout_ms)
        if action == "fill":
            return self.action_executor.fill(element_name, element, str(step.get("value") or ""), timeout_ms)
        if action == "press":
            return self.action_executor.press(element_name, element, str(step.get("key") or ""), timeout_ms)
        if action == "assert_visible":
            return self.action_executor.assert_visible(element_name, element, timeout_ms)
        if action == "choose_from_list":
            return self.action_executor.choose_from_list(element_name, element, str(step.get("exact_text") or ""), timeout_ms)
        if action == "call_platform_primitive":
            return self.action_executor.call_platform_primitive(str(step.get("name") or ""), dict(step.get("params") or {}))
        if action == "record_result":
            return self.action_executor.record_result(str(step.get("name") or ""), step.get("value"))
        return {"ok": False, "error_code": "unsupported_scenario_action", "message": f"Unsupported scenario action: {action}"}

    def _substitute(self, value: Any, context: dict[str, Any]) -> Any:
        if isinstance(value, dict):
            return {key: self._substitute(item, context) for key, item in value.items()}
        if isinstance(value, list):
            return [self._substitute(item, context) for item in value]
        if not isinstance(value, str):
            return value
        def replace(match: re.Match[str]) -> str:
            resolved = get_path(context, match.group(1).strip())
            return "" if resolved is None else str(resolved)
        return _TOKEN_RE.sub(replace, value)
