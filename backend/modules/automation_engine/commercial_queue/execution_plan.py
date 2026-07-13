from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class ExecutionPlan:
    correlation_id: str
    scheduler_tick_id: str | None
    worker_round_id: str
    job_id: str
    campaign_id: str
    recipient_id: str
    account_id: str
    platform: str
    phone: str
    display_name: str | None
    source_channel_uid: str
    send_method: str
    operation_order: list[str]
    delay_between_deliveries_seconds: int
    link_open_delay_seconds: int
    job_timeout_seconds: int
    max_job_duration_seconds: int
    dry_run: bool
    effective_policy: dict[str, Any]
    policy_resolution_source: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_execution_plan(
    *,
    context: Any,
    job: dict[str, Any],
    job_details: dict[str, Any],
    policy_result: dict[str, Any],
    dry_run: bool,
) -> ExecutionPlan:
    policy = dict(policy_result["effective_policy"])
    source = dict(policy_result.get("policy_resolution_source") or {})
    return ExecutionPlan(
        correlation_id=str(context.correlation_id),
        scheduler_tick_id=context.scheduler_tick_id,
        worker_round_id=str(context.worker_round_id),
        job_id=str(job["id"]),
        campaign_id=str(job["campaign_id"]),
        recipient_id=str(job["recipient_id"]),
        account_id=str(context.account_id),
        platform=str(policy.get("platform") or "bale"),
        phone=str(job_details.get("recipient_phone_normalized") or job.get("phone_normalized") or ""),
        display_name=job.get("display_name") or job_details.get("recipient_display_name"),
        source_channel_uid=str(job.get("source_channel_uid") or policy.get("source_channel_uid") or ""),
        send_method=str(policy.get("send_method") or "forward_latest_channel_message"),
        operation_order=list(policy.get("operation_order") or []),
        delay_between_deliveries_seconds=int(policy.get("delay_between_deliveries_seconds") or 0),
        link_open_delay_seconds=int(policy.get("link_open_delay_seconds") or 0),
        job_timeout_seconds=int(policy.get("job_timeout_seconds") or 0),
        max_job_duration_seconds=int(policy.get("max_job_duration_seconds") or 0),
        dry_run=bool(dry_run),
        effective_policy=policy,
        policy_resolution_source=source,
    )
