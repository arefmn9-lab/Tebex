from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class SchedulePlanItem:
    account_id: str
    scenario_id: str
    planned_at: str
    reason: str = "within_limits"

    def to_dict(self) -> dict[str, str]:
        return asdict(self)
