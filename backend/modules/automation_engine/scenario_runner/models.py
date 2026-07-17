from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ScenarioStepResult:
    step_id: str
    action: str
    status: str
    error_code: str | None = None
    message: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScenarioResult:
    ok: bool
    scenario_id: str
    failed_step: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    stopped_before_send: bool = False
    final_send_invoked: bool = False
    records: dict[str, Any] = field(default_factory=dict)
    steps: list[ScenarioStepResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "success": self.ok,
            "scenario_id": self.scenario_id,
            "failed_step": self.failed_step,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "stopped_before_send": self.stopped_before_send,
            "final_send_invoked": self.final_send_invoked,
            "records": self.records,
            "steps": [step.__dict__ for step in self.steps],
        }
