from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ScenarioLoader:
    def __init__(self, scenario_root: str | Path | None = None) -> None:
        backend_dir = Path(__file__).resolve().parents[3]
        self.scenario_root = Path(scenario_root or backend_dir / "scenarios")

    def load(self, platform_id: str, scenario_id: str) -> dict[str, Any]:
        path = self.scenario_root / platform_id / f"{scenario_id}.json"
        with path.open("r", encoding="utf-8") as scenario_file:
            return json.load(scenario_file)

    def list_platform_scenarios(self, platform_id: str) -> list[dict[str, Any]]:
        platform_dir = self.scenario_root / platform_id
        if not platform_dir.exists():
            return []
        scenarios = []
        for path in sorted(platform_dir.glob("*.json")):
            with path.open("r", encoding="utf-8") as scenario_file:
                scenarios.append(json.load(scenario_file))
        return scenarios
