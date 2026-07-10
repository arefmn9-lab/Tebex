from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


class ScenarioRunner:
    def __init__(self, page: Any, scenario_path: str | Path, context: dict[str, Any]) -> None:
        self.page = page
        self.scenario_path = Path(scenario_path)
        self.context = dict(context)
        self.records: dict[str, Any] = {}
        self.step_results: list[dict[str, Any]] = []
        self.scenario = self._load()

    def run(self) -> dict[str, Any]:
        started = time.perf_counter()
        for step in self.scenario.get("steps", []):
            result = self._run_step(step)
            self.step_results.append(result)
            if result["status"] == "failed":
                return {
                    "ok": False,
                    "success": False,
                    "step_results": self.step_results,
                    "failed_step": result["step"],
                    "error_code": result.get("error_code") or "scenario_step_failed",
                    "error_message": result.get("error_message") or "",
                    "screenshot_path": result.get("screenshot_path") or "",
                    "current_url": _safe_url(self.page),
                    "records": self.records,
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                }
        return {
            "ok": True,
            "success": True,
            "step_results": self.step_results,
            "failed_step": None,
            "error_code": None,
            "error_message": "",
            "screenshot_path": "",
            "current_url": _safe_url(self.page),
            "records": self.records,
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }

    def interpolate(self, value: Any) -> Any:
        if isinstance(value, str):
            exact = re.fullmatch(r"\{\{\s*([^}]+?)\s*\}\}", value)
            if exact:
                resolved = self._value(exact.group(1))
                return "" if resolved is None else resolved
            return re.sub(r"\{\{\s*([^}]+?)\s*\}\}", lambda match: str(self._value(match.group(1)) or ""), value)
        if isinstance(value, list):
            return [self.interpolate(item) for item in value]
        if isinstance(value, dict):
            return {key: self.interpolate(item) for key, item in value.items()}
        return value

    def _load(self) -> dict[str, Any]:
        with self.scenario_path.open("r", encoding="utf-8") as scenario_file:
            scenario = json.load(scenario_file)
        if not isinstance(scenario.get("context", {}), dict):
            raise ValueError("Scenario context must be an object")
        if not isinstance(scenario.get("elements", {}), dict):
            raise ValueError("Scenario elements must be an object")
        if not isinstance(scenario.get("steps", []), list):
            raise ValueError("Scenario steps must be a list")
        return scenario

    def _run_step(self, raw_step: dict[str, Any]) -> dict[str, Any]:
        step = self.interpolate(raw_step)
        started = time.perf_counter()
        step_id = str(step.get("id") or step.get("step") or step.get("type") or step.get("action") or "step")
        action = str(step.get("type") or step.get("action") or "")
        try:
            if action == "navigate":
                self._navigate(step)
            elif action == "wait":
                self._wait(step)
            elif action == "click":
                self._click(step)
            elif action == "type":
                self._type(step)
            elif action == "hover":
                self._hover(step)
            elif action == "condition":
                self._condition(step)
            elif action == "foreach":
                self._foreach(step)
            elif action == "loop":
                self._loop(step)
            elif action == "scroll_to_element":
                self._scroll_to_element(step)
            else:
                raise ScenarioStepError(str(step.get("error_code") or "unsupported_step_type"), f"Unsupported scenario step type: {action}")
            return self._step_result(step_id, "success", started, action=action, selector=self._selector_label(step))
        except ScenarioStepError as exc:
            return self._failed_step(step_id, started, action, step, exc.error_code, str(exc))
        except Exception as exc:
            return self._failed_step(step_id, started, action, step, str(step.get("error_code") or "scenario_step_failed"), str(exc))

    def _navigate(self, step: dict[str, Any]) -> None:
        url = str(step.get("url") or "")
        if not url:
            raise ScenarioStepError("missing_url", "Navigate step requires url")
        try:
            self.page.goto(url, wait_until=str(step.get("wait_until") or "load"), timeout=int(step.get("timeout_ms") or 5000))
        except TypeError:
            self.page.goto(url, wait_until=str(step.get("wait_until") or "load"))

    def _wait(self, step: dict[str, Any]) -> None:
        timeout_ms = int(step.get("timeout_ms") or 1500)
        selector = self._find_selector(step)
        condition = str(step.get("condition") or "")
        if selector:
            self._locator(selector).wait_for(state="visible", timeout=timeout_ms)
            return
        if self._has_selector(step):
            raise ScenarioStepError(str(step.get("error_code") or "missing_selector"), "Wait selector was not visible")
        if condition:
            deadline = time.monotonic() + timeout_ms / 1000
            while time.monotonic() <= deadline:
                if self._eval_condition(condition):
                    return
                self._wait_timeout(100)
            raise ScenarioStepError(str(step.get("error_code") or "condition_not_met"), f"Condition not met: {condition}")
            return
        self._wait_timeout(timeout_ms)

    def _click(self, step: dict[str, Any]) -> None:
        selector = self._find_selector(step)
        if not selector:
            raise ScenarioStepError(str(step.get("error_code") or "missing_selector"), "Click selector was not found")
        self._locator(selector).click(timeout=int(step.get("timeout_ms") or 1000))

    def _type(self, step: dict[str, Any]) -> None:
        selector = self._find_selector(step)
        if not selector:
            raise ScenarioStepError(str(step.get("error_code") or "missing_selector"), "Type selector was not found")
        locator = self._locator(selector)
        text = str(step.get("text") or "")
        try:
            locator.fill(text, timeout=int(step.get("timeout_ms") or 1000))
        except Exception:
            locator.type(text, timeout=int(step.get("timeout_ms") or 1000))

    def _hover(self, step: dict[str, Any]) -> None:
        selector = self._find_selector(step)
        if not selector:
            raise ScenarioStepError(str(step.get("error_code") or "missing_selector"), "Hover selector was not found")
        hover = getattr(self._locator(selector), "hover", None)
        if callable(hover):
            hover(timeout=int(step.get("timeout_ms") or 1000))

    def _condition(self, step: dict[str, Any]) -> None:
        condition = str(step.get("condition") or "")
        if not self._eval_condition(condition):
            raise ScenarioStepError(str(step.get("error_code") or "condition_not_met"), f"Condition failed: {condition}")

    def _foreach(self, step: dict[str, Any]) -> None:
        items = self._value(str(step.get("items") or ""))
        if not isinstance(items, list):
            return
        var_name = str(step.get("as") or "item")
        original = self.context.get(var_name)
        for index, item in enumerate(items):
            self.context[var_name] = item
            self.context["loop"] = {"index": index}
            for child in step.get("steps") or []:
                result = self._run_step(child)
                self.step_results.append(result)
                if result["status"] == "failed":
                    raise ScenarioStepError(str(result.get("error_code") or "scenario_step_failed"), str(result.get("error_message") or "foreach step failed"))
        if original is None:
            self.context.pop(var_name, None)
        else:
            self.context[var_name] = original
        self.context.pop("loop", None)

    def _loop(self, step: dict[str, Any]) -> None:
        count = int(step.get("count") or 1)
        for index in range(max(0, count)):
            self.context["loop"] = {"index": index}
            for child in step.get("steps") or []:
                result = self._run_step(child)
                self.step_results.append(result)
                if result["status"] == "failed":
                    raise ScenarioStepError(str(result.get("error_code") or "scenario_step_failed"), str(result.get("error_message") or "loop step failed"))
        self.context.pop("loop", None)

    def _scroll_to_element(self, step: dict[str, Any]) -> None:
        selector = self._find_selector(step)
        if not selector:
            raise ScenarioStepError(str(step.get("error_code") or "missing_selector"), "Scroll selector was not found")
        locator = self._locator(selector)
        scroll = getattr(locator, "scroll_into_view_if_needed", None)
        if callable(scroll):
            scroll(timeout=int(step.get("timeout_ms") or 1000))
        else:
            locator.evaluate("(el) => el.scrollIntoView({block: 'center', inline: 'nearest'})")

    def _find_selector(self, step: dict[str, Any]) -> str:
        for selector in self._selectors(step):
            try:
                self._locator(selector).wait_for(state="visible", timeout=int(step.get("probe_timeout_ms") or 250))
                return selector
            except Exception:
                continue
        return ""

    def _selectors(self, step: dict[str, Any]) -> list[str]:
        value = step.get("selectors") if "selectors" in step else step.get("selector")
        if isinstance(value, str) and value.startswith("$"):
            value = self.scenario.get("elements", {}).get(value[1:], value)
        selectors: list[str] = []
        for item in _as_list(value):
            if isinstance(item, str) and item.startswith("$"):
                item = self.scenario.get("elements", {}).get(item[1:], item)
            if isinstance(item, dict):
                selector_type = str(item.get("type") or "css")
                selector_value = str(item.get("value") or "")
                if selector_type == "xpath" and selector_value and not selector_value.startswith("xpath="):
                    selectors.append(f"xpath={selector_value}")
                elif selector_type == "css":
                    selectors.append(selector_value)
                elif selector_value:
                    selectors.append(selector_value)
            elif item:
                selectors.append(str(item))
        return [selector for selector in selectors if selector]

    def _has_selector(self, step: dict[str, Any]) -> bool:
        return bool(self._selectors(step))

    def _selector_label(self, step: dict[str, Any]) -> str:
        selectors = self._selectors(step)
        return selectors[0] if selectors else ""

    def _eval_condition(self, condition: str) -> bool:
        if condition.startswith("exists(") and condition.endswith(")"):
            selector = condition[7:-1].strip().strip("\"'")
            return bool(self._find_selector({"selector": selector, "probe_timeout_ms": 100}))
        if condition.startswith("not_exists(") and condition.endswith(")"):
            selector = condition[11:-1].strip().strip("\"'")
            return not bool(self._find_selector({"selector": selector, "probe_timeout_ms": 100}))
        return bool(condition)

    def _locator(self, selector: str) -> Any:
        return self.page.locator(selector).first

    def _wait_timeout(self, timeout_ms: int) -> None:
        wait = getattr(self.page, "wait_for_timeout", None)
        if callable(wait):
            wait(timeout_ms)
        elif timeout_ms > 0:
            time.sleep(min(timeout_ms, 1000) / 1000)

    def _value(self, path: str) -> Any:
        value: Any = self.context
        for part in str(path or "").split("."):
            if not part:
                continue
            if isinstance(value, dict):
                value = value.get(part)
            elif isinstance(value, list) and part.isdigit():
                value = value[int(part)]
            else:
                return None
        return value

    def _failed_step(self, step: str, started: float, action: str, raw_step: dict[str, Any], error_code: str, error_message: str) -> dict[str, Any]:
        return self._step_result(
            step,
            "failed",
            started,
            action=action,
            selector=self._selector_label(raw_step),
            error_code=error_code,
            error_message=error_message,
            screenshot_path=self._screenshot(step),
        )

    def _screenshot(self, step: str) -> str:
        try:
            screenshot = getattr(self.page, "screenshot", None)
            if not callable(screenshot):
                return ""
            debug_dir = Path(__file__).resolve().parents[2] / "runtime" / "debug"
            debug_dir.mkdir(parents=True, exist_ok=True)
            path = debug_dir / f"scenario_{self.scenario_path.stem}_{step}_{uuid4().hex[:8]}.png"
            screenshot(path=str(path), full_page=True)
            return str(path)
        except Exception:
            return ""

    def _step_result(self, step: str, status: str, started: float, **details: Any) -> dict[str, Any]:
        return {
            "step": step,
            "status": status,
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "page_url": _safe_url(self.page),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **details,
        }


class ScenarioStepError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _safe_url(page: Any) -> str:
    try:
        return str(getattr(page, "url", "") or "")
    except Exception:
        return ""
