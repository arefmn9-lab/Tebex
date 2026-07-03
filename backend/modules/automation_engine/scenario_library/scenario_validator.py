from __future__ import annotations

from typing import Any


class ScenarioValidator:
    required_fields = {
        "scenario_id",
        "platform_id",
        "name",
        "type",
        "enabled",
        "dry_run",
        "context",
        "elements",
        "steps",
        "limits",
        "safety",
    }

    def validate(self, scenario: dict[str, Any]) -> dict[str, Any]:
        missing = sorted(self.required_fields - set(scenario))
        if missing:
            return {"ok": False, "errors": [f"Missing field: {field}" for field in missing]}

        safety = scenario.get("safety") or {}
        errors = []
        if safety.get("allow_bulk_execution") is True:
            errors.append("Bulk execution is disabled for this scenario system")
        if safety.get("require_manual_login") is not True:
            errors.append("Manual login must be required")
        if not isinstance(scenario.get("steps"), list):
            errors.append("steps must be a list")

        return {"ok": not errors, "errors": errors}
