from __future__ import annotations

import random
from datetime import date
from typing import Any
from uuid import uuid4

from modules.automation_engine.plugins.bale.account_store import bale_account_store
from modules.automation_engine.scheduling.account_groups import account_group_store

from .assignment_store import AssignmentStore, assignment_store
from .campaign_store import BulkCampaignStore, bulk_campaign_store
from .contact_store import ContactStore, contact_store
from .message_source_store import MessageSourceStore, message_source_store


PLATFORM_DEFAULT_LIMITS = {
    "bale": {"daily": 10, "hourly": 2},
    "rubika": {"daily": 10, "hourly": 2},
    "eitaa": {"daily": 10, "hourly": 2},
    "instagram": {"daily": 10, "hourly": 2},
}


class BulkAssignmentPlanner:
    def __init__(
        self,
        campaign_store: BulkCampaignStore | None = None,
        source_store: MessageSourceStore | None = None,
        contacts_store: ContactStore | None = None,
        assignments_store: AssignmentStore | None = None,
        group_store: Any | None = None,
        account_store: Any | None = None,
    ) -> None:
        self.campaign_store = campaign_store or bulk_campaign_store
        self.source_store = source_store or message_source_store
        self.contacts_store = contacts_store or contact_store
        self.assignments_store = assignments_store or assignment_store
        self.group_store = group_store or account_group_store
        self.account_store = account_store or bale_account_store

    def build_assignment_plan(self, campaign_id: str, request: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = request or {}
        campaign = self.campaign_store.get_campaign(campaign_id)
        if campaign is None:
            return {"ok": False, "dry_run": True, "campaign_id": campaign_id, "error": "campaign_not_found"}

        planned_for_date = str(payload.get("planned_for_date") or date.today().isoformat())
        plan_seed = str(payload.get("plan_seed") or uuid4().hex[:12])
        max_contacts_per_account = _optional_positive_int(payload.get("max_contacts_per_account"))
        rng = random.Random(plan_seed)
        existing_contact_ids = self.assignments_store.assigned_contact_ids(campaign_id, exclude_planned_for_date=planned_for_date)

        route_summaries: list[dict[str, Any]] = []
        assignments: list[dict[str, Any]] = []
        total_remaining_contacts = 0
        for route in campaign.get("routes", []):
            summary, route_assignments = self._assign_route(
                campaign_id=campaign_id,
                route=route,
                planned_for_date=planned_for_date,
                max_contacts_per_account=max_contacts_per_account,
                rng=rng,
                existing_contact_ids=existing_contact_ids,
            )
            route_summaries.append(summary)
            assignments.extend(route_assignments)
            total_remaining_contacts += summary["remaining_contacts"]
            existing_contact_ids.update(item["contact_id"] for item in route_assignments if item.get("contact_id"))

        self.assignments_store.replace_campaign_assignments(campaign_id, planned_for_date, assignments)
        return {
            "ok": True,
            "dry_run": bool(payload.get("dry_run", True)),
            "campaign_id": campaign["campaign_id"],
            "campaign_name": campaign["name"],
            "planned_for_date": planned_for_date,
            "plan_seed": plan_seed,
            "total_assignments": len(assignments),
            "total_remaining_contacts": total_remaining_contacts,
            "route_summaries": route_summaries,
        }

    def _assign_route(
        self,
        campaign_id: str,
        route: dict[str, Any],
        planned_for_date: str,
        max_contacts_per_account: int | None,
        rng: random.Random,
        existing_contact_ids: set[str],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        warnings: list[str] = []
        route_id = str(route.get("route_id") or "")
        group = self.group_store.get_group(str(route.get("account_group_id") or ""))
        source = self.source_store.get_source(str(route.get("message_source_id") or ""))

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

        accounts = self._available_accounts(route, group)
        contacts = self._assignable_contacts(str(route.get("contact_list_id") or ""), existing_contact_ids)
        rng.shuffle(accounts)
        rng.shuffle(contacts)

        effective_limits = [self._effective_limits(account, group, route, max_contacts_per_account) for account in accounts]
        route_capacity = sum(item["effective_daily_limit_per_account"] for item in effective_limits)
        active = route.get("enabled", True) and group is not None and group.get("enabled", True) and source is not None and source.get("enabled", True)
        assignment_limit = min(route_capacity, len(contacts)) if active else 0
        route_assignments: list[dict[str, Any]] = []

        contact_index = 0
        sequence = 1
        for account, limits in zip(accounts, effective_limits):
            daily_limit = limits["effective_daily_limit_per_account"]
            for _ in range(daily_limit):
                if contact_index >= assignment_limit:
                    break
                contact = contacts[contact_index]
                route_assignments.append(
                    {
                        "assignment_id": f"assign_{uuid4().hex[:12]}",
                        "campaign_id": campaign_id,
                        "route_id": route_id,
                        "platform_id": str(route.get("platform_id") or ""),
                        "account_group_id": str(route.get("account_group_id") or ""),
                        "account_id": account["account_id"],
                        "contact_id": contact["contact_id"],
                        "contact_list_id": contact["contact_list_id"],
                        "normalized_phone": contact["normalized_phone"],
                        "message_source_id": str(route.get("message_source_id") or ""),
                        "scenario_id": str(route.get("scenario_id") or "save_contact_and_forward_from_source"),
                        "contact_naming_value": _format_contact_name(str(route.get("contact_naming_pattern") or ""), sequence),
                        "planned_status": "planned",
                        "skip_reason": "",
                        "planned_for_date": planned_for_date,
                    }
                )
                contact_index += 1
                sequence += 1
            if contact_index >= assignment_limit:
                break

        sample_assignments = [
            {
                "account_id": item["account_id"],
                "contact_naming_value": item["contact_naming_value"],
                "normalized_phone": item["normalized_phone"],
                "route_id": item["route_id"],
                "platform_id": item["platform_id"],
            }
            for item in route_assignments[:10]
        ]
        first_limits = effective_limits[0] if effective_limits else {"effective_daily_limit_per_account": 0, "effective_hourly_limit_per_account": 0}
        return (
            {
                "route_id": route_id,
                "platform_id": str(route.get("platform_id") or ""),
                "account_group_id": str(route.get("account_group_id") or ""),
                "available_accounts": len(accounts),
                "valid_contacts": len(contacts),
                "effective_daily_limit_per_account": first_limits["effective_daily_limit_per_account"],
                "effective_hourly_limit_per_account": first_limits["effective_hourly_limit_per_account"],
                "route_capacity": route_capacity if active else 0,
                "assigned_contacts": len(route_assignments),
                "remaining_contacts": max(0, len(contacts) - len(route_assignments)),
                "sample_assignments": sample_assignments,
                "warnings": warnings,
            },
            route_assignments,
        )

    def _available_accounts(self, route: dict[str, Any], group: dict[str, Any] | None) -> list[dict[str, Any]]:
        if group is None or not group.get("enabled", True):
            return []
        platform_id = str(route.get("platform_id") or "")
        group_id = str(route.get("account_group_id") or "")
        if platform_id != "bale":
            return []
        return [
            account
            for account in self.account_store.list_accounts()
            if account.get("account_group_id") == group_id
            and account.get("enabled_for_scheduling", True)
            and account.get("status") == "active"
            and int(account.get("health_score", 0)) >= 50
            and account.get("block_status") not in {"blocked", "limited"}
        ]

    def _assignable_contacts(self, contact_list_id: str, excluded_contact_ids: set[str]) -> list[dict[str, Any]]:
        return [
            contact
            for contact in self.contacts_store.list_contacts(contact_list_id)
            if contact.get("status") == "new"
            and contact.get("normalized_phone")
            and contact.get("contact_id") not in excluded_contact_ids
        ]

    def _effective_limits(
        self,
        account: dict[str, Any],
        group: dict[str, Any] | None,
        route: dict[str, Any],
        max_contacts_per_account: int | None,
    ) -> dict[str, int]:
        platform_defaults = PLATFORM_DEFAULT_LIMITS.get(str(route.get("platform_id") or ""), {"daily": 1, "hourly": 1})
        account_daily = int(account.get("daily_limit", platform_defaults["daily"]))
        account_hourly = int(account.get("hourly_limit", platform_defaults["hourly"]))
        group_daily = _first_positive_int(group or {}, ["daily_limit_per_account", "daily_limit", "daily_capacity"])
        group_hourly = _first_positive_int(group or {}, ["hourly_limit_per_account", "hourly_limit"])
        route_daily = _optional_positive_int(route.get("daily_limit_per_account"))
        route_hourly = _optional_positive_int(route.get("hourly_limit_per_account"))

        effective_daily = route_daily or group_daily or account_daily or platform_defaults["daily"]
        effective_hourly = route_hourly or group_hourly or account_hourly or platform_defaults["hourly"]
        if max_contacts_per_account is not None:
            effective_daily = min(effective_daily, max_contacts_per_account)
        return {
            "effective_daily_limit_per_account": max(0, int(effective_daily)),
            "effective_hourly_limit_per_account": max(0, int(effective_hourly)),
        }


def _optional_positive_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except Exception:
        return None
    return parsed if parsed > 0 else None


def _first_positive_int(payload: dict[str, Any], keys: list[str]) -> int | None:
    for key in keys:
        value = _optional_positive_int(payload.get(key))
        if value is not None:
            return value
    return None


def _format_contact_name(pattern: str, sequence: int) -> str:
    template = pattern or "Contact-{seq:06d}"
    try:
        return template.format(seq=sequence)
    except Exception:
        return f"{template}-{sequence:06d}"


assignment_planner = BulkAssignmentPlanner()
