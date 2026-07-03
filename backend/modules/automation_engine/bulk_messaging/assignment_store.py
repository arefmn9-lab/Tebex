from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from .models import BulkAssignment


PLANNED_STATUSES = {"planned", "skipped"}


def runtime_path() -> Path:
    return Path(__file__).resolve().parents[3] / "runtime" / "bulk_assignments.json"


class AssignmentStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or runtime_path()

    def list_assignments(self, campaign_id: str | None = None) -> list[dict[str, Any]]:
        assignments = [self._normalize_assignment(item).to_dict() for item in self._read_json([])]
        if campaign_id is None:
            return assignments
        return [item for item in assignments if item["campaign_id"] == campaign_id]

    def replace_campaign_assignments(
        self,
        campaign_id: str,
        planned_for_date: str,
        assignments: list[dict[str, Any]],
    ) -> None:
        existing = [
            item
            for item in self.list_assignments()
            if not (item["campaign_id"] == campaign_id and item["planned_for_date"] == planned_for_date)
        ]
        normalized = [self._normalize_assignment(item).to_dict() for item in assignments]
        self._write_json(existing + normalized)

    def assigned_contact_ids(self, campaign_id: str, exclude_planned_for_date: str | None = None) -> set[str]:
        return {
            item["contact_id"]
            for item in self.list_assignments(campaign_id)
            if item["planned_status"] == "planned" and item.get("contact_id")
            and item.get("planned_for_date") != exclude_planned_for_date
        }

    def summary(self, campaign_id: str) -> dict[str, Any]:
        assignments = self.list_assignments(campaign_id)
        planned = [item for item in assignments if item["planned_status"] == "planned"]
        skipped = [item for item in assignments if item["planned_status"] == "skipped"]
        by_route: dict[str, int] = {}
        for item in planned:
            route_id = item["route_id"]
            by_route[route_id] = by_route.get(route_id, 0) + 1
        return {
            "campaign_id": campaign_id,
            "total_assignments": len(planned),
            "skipped_assignments": len(skipped),
            "routes": [{"route_id": route_id, "planned": count} for route_id, count in sorted(by_route.items())],
        }

    def _normalize_assignment(self, payload: dict[str, Any]) -> BulkAssignment:
        status = str(payload.get("planned_status") or "planned")
        if status not in PLANNED_STATUSES:
            status = "planned"
        return BulkAssignment(
            assignment_id=str(payload.get("assignment_id") or ""),
            campaign_id=str(payload.get("campaign_id") or ""),
            route_id=str(payload.get("route_id") or ""),
            platform_id=str(payload.get("platform_id") or ""),
            account_group_id=str(payload.get("account_group_id") or ""),
            account_id=str(payload.get("account_id") or ""),
            contact_id=str(payload.get("contact_id") or ""),
            contact_list_id=str(payload.get("contact_list_id") or ""),
            normalized_phone=str(payload.get("normalized_phone") or ""),
            message_source_id=str(payload.get("message_source_id") or ""),
            scenario_id=str(payload.get("scenario_id") or ""),
            contact_naming_value=str(payload.get("contact_naming_value") or ""),
            planned_status=status,
            skip_reason=str(payload.get("skip_reason") or ""),
            planned_for_date=str(payload.get("planned_for_date") or ""),
            created_at=str(payload.get("created_at") or BulkAssignment(assignment_id="", campaign_id="", route_id="", platform_id="", account_group_id="", account_id="", contact_id="", contact_list_id="", normalized_phone="", message_source_id="", scenario_id="", contact_naming_value="").created_at),
        )

    def _read_json(self, default: Any) -> Any:
        try:
            with self.path.open("r", encoding="utf-8") as json_file:
                return json.load(json_file)
        except Exception:
            return deepcopy(default)

    def _write_json(self, data: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as json_file:
            json.dump(data, json_file, ensure_ascii=False, indent=2)


assignment_store = AssignmentStore()
