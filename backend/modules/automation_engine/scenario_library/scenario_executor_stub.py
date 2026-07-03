from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


class ScenarioExecutorStub:
    def dry_run(self, scenario: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
        runtime_context = dict(scenario.get("context") or {})
        runtime_context.update(context or {})
        planned_steps = []
        for index, step in enumerate(scenario.get("steps") or [], start=1):
            planned_steps.append(
                {
                    "index": index,
                    "step_id": step.get("step_id") or f"step_{index}",
                    "action": step.get("action"),
                    "description": step.get("description", ""),
                    "dry_run": True,
                    "status": "planned",
                }
            )
        return {
            "ok": True,
            "scenario_id": scenario.get("scenario_id"),
            "platform_id": scenario.get("platform_id"),
            "dry_run": True,
            "planned_steps": planned_steps,
            "context": runtime_context,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
