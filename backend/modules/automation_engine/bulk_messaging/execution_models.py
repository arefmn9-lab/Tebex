from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .models import utc_now


EXECUTION_JOB_STATUSES = {"pending", "running", "completed", "failed", "skipped", "cancelled"}


@dataclass
class BulkExecutionJob:
    job_id: str
    campaign_id: str
    route_id: str
    assignment_id: str
    platform_id: str
    account_group_id: str
    account_id: str
    contact_id: str
    normalized_phone: str
    contact_naming_value: str
    message_source_id: str
    scenario_id: str
    status: str = "pending"
    dry_run: bool = True
    planned_for_date: str = ""
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    error_code: str | None = None
    error_message: str | None = None
    dry_run_result: dict[str, Any] | None = None
    execution_result: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
