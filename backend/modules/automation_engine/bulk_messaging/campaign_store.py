from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import BulkCampaign, CampaignRoute


CAMPAIGN_STATUSES = {"draft", "planned", "running", "paused", "completed"}


def runtime_path() -> Path:
    return Path(__file__).resolve().parents[3] / "runtime" / "bulk_campaigns.json"


class BulkCampaignStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or runtime_path()

    def list_campaigns(self) -> list[dict[str, Any]]:
        return [self._normalize_campaign(item).to_dict() for item in self._read_json([])]

    def list_platform_campaigns(self, platform_id: str) -> list[dict[str, Any]]:
        return [
            campaign
            for campaign in self.list_campaigns()
            if any(route.get("platform_id") == platform_id for route in campaign.get("routes", []))
        ]

    def get_campaign(self, campaign_id: str) -> dict[str, Any] | None:
        for campaign in self.list_campaigns():
            if campaign["campaign_id"] == campaign_id:
                return campaign
        return None

    def create_campaign(self, payload: dict[str, Any]) -> dict[str, Any]:
        campaigns = self.list_campaigns()
        campaign_id = str(payload.get("campaign_id") or "").strip()
        if not campaign_id:
            campaign_id = _slug(str(payload.get("campaign_tag") or payload.get("name") or "campaign"))
        if any(item["campaign_id"] == campaign_id for item in campaigns):
            raise ValueError(f"Campaign already exists: {campaign_id}")
        campaign = self._normalize_campaign({**payload, "campaign_id": campaign_id}).to_dict()
        campaigns.append(campaign)
        self._write_json(campaigns)
        return campaign

    def update_campaign(self, campaign_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        campaigns = self.list_campaigns()
        for index, campaign in enumerate(campaigns):
            if campaign["campaign_id"] == campaign_id:
                updated = self._normalize_campaign({**campaign, **payload, "campaign_id": campaign_id}).to_dict()
                campaigns[index] = updated
                self._write_json(campaigns)
                return updated
        raise KeyError(campaign_id)

    def create_route(self, campaign_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        campaign = self.get_campaign(campaign_id)
        if campaign is None:
            raise KeyError(campaign_id)
        routes = list(campaign.get("routes", []))
        route_id = str(payload.get("route_id") or f"route_{uuid4().hex[:10]}")
        if any(route["route_id"] == route_id for route in routes):
            raise ValueError(f"Route already exists: {route_id}")
        route = self._normalize_route({**payload, "route_id": route_id, "campaign_id": campaign_id}).to_dict()
        routes.append(route)
        return self.update_campaign(campaign_id, {"routes": routes})

    def update_route(self, campaign_id: str, route_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        campaign = self.get_campaign(campaign_id)
        if campaign is None:
            raise KeyError(campaign_id)
        routes = []
        found = False
        for route in campaign.get("routes", []):
            if route["route_id"] == route_id:
                routes.append(self._normalize_route({**route, **payload, "route_id": route_id, "campaign_id": campaign_id}).to_dict())
                found = True
            else:
                routes.append(route)
        if not found:
            raise KeyError(route_id)
        return self.update_campaign(campaign_id, {"routes": routes})

    def _normalize_campaign(self, payload: dict[str, Any]) -> BulkCampaign:
        status = str(payload.get("status") or "draft")
        if status not in CAMPAIGN_STATUSES:
            status = "draft"
        campaign_id = str(payload.get("campaign_id") or "")
        routes = [self._normalize_route({**route, "campaign_id": campaign_id}).to_dict() for route in payload.get("routes", [])]
        return BulkCampaign(
            campaign_id=campaign_id,
            name=str(payload.get("name") or "Bulk Campaign"),
            campaign_tag=str(payload.get("campaign_tag") or ""),
            status=status,
            dry_run=bool(payload.get("dry_run", True)),
            notes=str(payload.get("notes") or ""),
            created_at=str(payload.get("created_at") or BulkCampaign(campaign_id="", name="").created_at),
            routes=routes,
        )

    def _normalize_route(self, payload: dict[str, Any]) -> CampaignRoute:
        return CampaignRoute(
            route_id=str(payload.get("route_id") or ""),
            campaign_id=str(payload.get("campaign_id") or ""),
            platform_id=str(payload.get("platform_id") or "bale"),
            account_group_id=str(payload.get("account_group_id") or ""),
            message_source_id=str(payload.get("message_source_id") or ""),
            contact_list_id=str(payload.get("contact_list_id") or ""),
            scenario_id=str(payload.get("scenario_id") or "save_contact_and_forward_from_source"),
            contact_naming_pattern=str(payload.get("contact_naming_pattern") or "Bale-GHAB-{seq:06d}"),
            daily_limit_per_account=max(0, int(payload.get("daily_limit_per_account", 50))),
            hourly_limit_per_account=max(0, int(payload.get("hourly_limit_per_account", 5))),
            enabled=bool(payload.get("enabled", True)),
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


def _slug(value: str) -> str:
    safe = "".join(char.lower() if char.isalnum() else "_" for char in value).strip("_")
    return f"{safe or 'bulk'}_campaign"


bulk_campaign_store = BulkCampaignStore()
