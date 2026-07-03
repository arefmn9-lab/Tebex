from __future__ import annotations

from typing import Any, Literal, TypedDict


ScenarioType = Literal["forward_from_source", "health_check", "preparation"]


class ScenarioSafety(TypedDict, total=False):
    require_manual_login: bool
    stop_on_error: bool
    allow_bulk_execution: bool


class ScenarioDefinition(TypedDict, total=False):
    scenario_id: str
    platform_id: str
    name: str
    type: ScenarioType
    enabled: bool
    dry_run: bool
    context: dict[str, Any]
    elements: dict[str, Any]
    steps: list[dict[str, Any]]
    limits: dict[str, Any]
    safety: ScenarioSafety
