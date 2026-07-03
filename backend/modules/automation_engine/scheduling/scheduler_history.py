from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any


DEFAULT_HISTORY_PATH = Path(__file__).resolve().parents[3] / "runtime" / "scheduler_history.json"


@dataclass
class SchedulerHistoryStore:
    path: Path = DEFAULT_HISTORY_PATH

    def load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict) and isinstance(data.get("days"), list):
            return [item for item in data["days"] if isinstance(item, dict)]
        return []

    def previous_day(self, day: date, platform_id: str, scenario_id: str) -> dict[str, Any] | None:
        previous_date = (day - timedelta(days=1)).isoformat()
        for item in reversed(self.load()):
            if (
                item.get("date") == previous_date
                and item.get("platform_id") == platform_id
                and item.get("scenario_id") == scenario_id
            ):
                return item
        return None

    def save_plan(
        self,
        day: date,
        platform_id: str,
        scenario_id: str,
        planned_jobs: list[dict[str, Any]],
    ) -> None:
        account_map: dict[str, dict[str, Any]] = {}
        for item in planned_jobs:
            account_id = str(item.get("account_id", ""))
            if not account_id:
                continue
            account_plan = account_map.setdefault(
                account_id,
                {
                    "account_id": account_id,
                    "planned_times": [],
                    "batch_index": int(item.get("batch_index", len(account_map))),
                },
            )
            planned_at = str(item.get("planned_at", ""))
            if "T" in planned_at:
                account_plan["planned_times"].append(planned_at.split("T", 1)[1][:5])

        next_entry = {
            "date": day.isoformat(),
            "platform_id": platform_id,
            "scenario_id": scenario_id,
            "account_plans": list(account_map.values()),
        }
        entries = [
            item
            for item in self.load()
            if not (
                item.get("date") == next_entry["date"]
                and item.get("platform_id") == platform_id
                and item.get("scenario_id") == scenario_id
            )
        ]
        entries.append(next_entry)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
