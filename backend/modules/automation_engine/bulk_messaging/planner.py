from __future__ import annotations

from typing import Any

from modules.automation_engine.plugins.bale.account_store import bale_account_store
from modules.automation_engine.scheduling.account_groups import account_group_store

from .campaign_store import BulkCampaignStore, bulk_campaign_store
from .contact_list_store import ContactListStore, contact_list_store
from .contact_store import ContactStore, contact_store
from .message_source_store import MessageSourceStore, message_source_store


class BulkCampaignPlanner:
    def __init__(
        self,
        campaign_store: BulkCampaignStore | None = None,
        source_store: MessageSourceStore | None = None,
        contacts_store: ContactListStore | None = None,
        imported_contacts_store: ContactStore | None = None,
        group_store: Any | None = None,
        account_store: Any | None = None,
    ) -> None:
        self.campaign_store = campaign_store or bulk_campaign_store
        self.source_store = source_store or message_source_store
        self.contacts_store = contacts_store or contact_list_store
        self.imported_contacts_store = imported_contacts_store or contact_store
        self.group_store = group_store or account_group_store
        self.account_store = account_store or bale_account_store

    def build_plan(self, campaign_id: str) -> dict[str, Any]:
        campaign = self.campaign_store.get_campaign(campaign_id)
        if campaign is None:
            return {"ok": False, "dry_run": True, "campaign_id": campaign_id, "error": "campaign_not_found"}

        route_summaries = []
        total_planned = 0
        for route in campaign.get("routes", []):
            summary = self._route_summary(route)
            route_summaries.append(summary)
            total_planned += summary["planned_count"]

        return {
            "ok": True,
            "dry_run": True,
            "campaign_id": campaign["campaign_id"],
            "campaign_name": campaign["name"],
            "total_planned": total_planned,
            "route_summaries": route_summaries,
        }

    def _route_summary(self, route: dict[str, Any]) -> dict[str, Any]:
        warnings: list[str] = []
        group = self.group_store.get_group(str(route.get("account_group_id") or ""))
        source = self.source_store.get_source(str(route.get("message_source_id") or ""))
        contact_list = self.contacts_store.get_contact_list(str(route.get("contact_list_id") or ""))

        if not route.get("enabled", True):
            warnings.append("route_disabled")
        if group is None:
            warnings.append("account_group_missing")
        elif not group.get("enabled", True):
            warnings.append("account_group_disabled")
        if source is None:
            warnings.append("message_source_missing")
        elif not source.get("enabled", True):
            warnings.append("message_source_disabled")
        if contact_list is None:
            warnings.append("contact_list_missing")
        elif contact_list.get("status") != "ready":
            warnings.append("contact_list_not_ready")
        for field in ["platform_id", "account_group_id", "message_source_id", "contact_list_id", "contact_naming_pattern"]:
            if not route.get(field):
                warnings.append(f"{field}_missing")

        available_accounts = self._available_account_count(route, group)
        daily_limit = max(0, int(route.get("daily_limit_per_account", 0)))
        route_capacity = available_accounts * daily_limit
        imported_valid_count = self.imported_contacts_store.valid_contact_count(str(route.get("contact_list_id") or ""))
        valid_contacts = imported_valid_count if imported_valid_count is not None else int((contact_list or {}).get("valid_contacts", 0))
        if not route.get("enabled", True) or group is None or not group.get("enabled", True):
            planned_count = 0
        else:
            planned_count = min(route_capacity, valid_contacts)

        return {
            "route_id": route.get("route_id", ""),
            "platform_id": route.get("platform_id", ""),
            "account_group_id": route.get("account_group_id", ""),
            "message_source_id": route.get("message_source_id", ""),
            "contact_list_id": route.get("contact_list_id", ""),
            "available_accounts": available_accounts,
            "daily_limit_per_account": daily_limit,
            "route_capacity": route_capacity,
            "valid_contacts": valid_contacts,
            "planned_count": planned_count,
            "remaining_contacts": max(0, valid_contacts - planned_count),
            "contact_naming_pattern": route.get("contact_naming_pattern", ""),
            "warnings": warnings,
        }

    def _available_account_count(self, route: dict[str, Any], group: dict[str, Any] | None) -> int:
        if group is None or not group.get("enabled", True):
            return 0
        platform_id = str(route.get("platform_id") or "")
        group_id = str(route.get("account_group_id") or "")
        if platform_id != "bale":
            return 0
        accounts = [
            account
            for account in self.account_store.list_accounts()
            if account.get("account_group_id") == group_id
            and account.get("enabled_for_scheduling", True)
            and account.get("status") == "active"
            and int(account.get("health_score", 0)) >= 50
            and account.get("block_status") not in {"blocked", "limited"}
        ]
        return len(accounts)
