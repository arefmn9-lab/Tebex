from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class MessageSource:
    message_source_id: str
    platform_id: str
    name: str
    campaign_tag: str = ""
    source_type: str = "channel"
    source_ref: str = ""
    message_ref_type: str = "latest"
    message_ref_value: str = ""
    enabled: bool = True
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ContactListMetadata:
    contact_list_id: str
    name: str
    platform_id: str = ""
    campaign_tag: str = ""
    source_filename: str = ""
    total_contacts: int = 0
    valid_contacts: int = 0
    duplicate_contacts: int = 0
    status: str = "draft"
    notes: str = ""
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BulkContact:
    contact_id: str
    contact_list_id: str
    raw_phone: str
    normalized_phone: str
    full_name: str = ""
    city: str = ""
    service_taken: str = ""
    last_visit_date: str = ""
    campaign_tag: str = ""
    platform_hint: str = ""
    status: str = "new"
    import_row_number: int = 0
    notes: str = ""
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BulkAssignment:
    assignment_id: str
    campaign_id: str
    route_id: str
    platform_id: str
    account_group_id: str
    account_id: str
    contact_id: str
    contact_list_id: str
    normalized_phone: str
    message_source_id: str
    scenario_id: str
    contact_naming_value: str
    planned_status: str = "planned"
    skip_reason: str = ""
    planned_for_date: str = ""
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CampaignRoute:
    route_id: str
    campaign_id: str
    platform_id: str
    account_group_id: str
    message_source_id: str
    contact_list_id: str
    scenario_id: str = "save_contact_and_forward_from_source"
    contact_naming_pattern: str = "Bale-GHAB-{seq:06d}"
    daily_limit_per_account: int = 50
    hourly_limit_per_account: int = 5
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BulkCampaign:
    campaign_id: str
    name: str
    campaign_tag: str = ""
    status: str = "draft"
    dry_run: bool = True
    notes: str = ""
    created_at: str = field(default_factory=utc_now)
    routes: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
