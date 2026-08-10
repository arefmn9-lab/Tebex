from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
import os


@dataclass(frozen=True)
class ResourceSnapshot:
    captured_at: str
    cpu_percent: float
    memory_percent: float
    available_memory_mb: float
    active_worker_count: int
    active_browser_count: int
    browser_starting_count: int
    queued_job_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CapacityDecision:
    allow_new_worker: bool
    allow_new_browser: bool
    available_worker_slots: int
    available_browser_start_slots: int
    reason_codes: list[str] = field(default_factory=list)
    retry_after_seconds: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ResourceCapacityProvider:
    def __init__(self, repository: Any, cpu_percent: float = 0.0, memory_percent: float = 0.0, available_memory_mb: float = 0.0) -> None:
        self.repository = repository
        self.cpu_percent = cpu_percent
        self.memory_percent = memory_percent
        self.available_memory_mb = available_memory_mb
        # Account, browser, and worker concurrency are one operator policy.
        # Older deployments exposed environment overrides for the latter two;
        # treating those values as hidden ceilings recreated the single-account
        # defect.  Keep the attributes for read-only compatibility, but never
        # use them to reduce the canonical runtime capacity.
        self.browser_slot_capacity = None
        self.worker_slot_capacity = None

    def snapshot(self) -> ResourceSnapshot:
        return ResourceSnapshot(
            captured_at=datetime.now(timezone.utc).isoformat(),
            cpu_percent=float(self.cpu_percent),
            memory_percent=float(self.memory_percent),
            available_memory_mb=float(self.available_memory_mb),
            active_worker_count=len(self.repository.list_active_worker_locks()),
            active_browser_count=0,
            browser_starting_count=0,
            queued_job_count=int(self.repository.count_jobs_by_status().get("queued", 0)),
        )

    def decide(self, policy: dict[str, Any], snapshot: ResourceSnapshot | None = None, scheduler_status: str = "running") -> CapacityDecision:
        snap = snapshot or self.snapshot()
        unrestricted = str(policy.get("concurrency_mode") or "operator_defined") == "unrestricted"
        if unrestricted:
            max_workers = max(0, int(snap.queued_job_count) + int(snap.active_worker_count))
            runtime_capacity = max_workers
        else:
            runtime_capacity = max(
                0,
                int(
                    policy.get("max_concurrent_accounts")
                    if policy.get("max_concurrent_accounts") is not None
                    else policy.get("operator_defined_max_concurrent_accounts") or 0
                ),
            )
        worker_slots = max(0, runtime_capacity - int(snap.active_worker_count))
        browser_slots = max(0, runtime_capacity - int(snap.active_browser_count) - int(snap.browser_starting_count))
        reasons: list[str] = []
        if scheduler_status in {"paused", "stopped"}:
            reasons.append(f"scheduler_{scheduler_status}")
        if worker_slots <= 0:
            reasons.append("concurrency_limit_reached")
        if browser_slots <= 0:
            reasons.append("browser_start_capacity_reached")
        if bool(policy.get("resource_guard_enabled")):
            if snap.memory_percent >= float(policy.get("max_system_memory_percent") or 100):
                reasons.append("memory_threshold_reached")
            if snap.cpu_percent >= float(policy.get("max_system_cpu_percent") or 100):
                reasons.append("cpu_threshold_reached")
        return CapacityDecision(
            allow_new_worker=not reasons and worker_slots > 0,
            allow_new_browser=not reasons and browser_slots > 0,
            available_worker_slots=worker_slots,
            available_browser_start_slots=browser_slots,
            reason_codes=reasons,
            retry_after_seconds=30 if reasons else 0,
        )

    def configuration_inputs(self, configured_max: int, eligible_count: int, host_resource_capacity: int | None = None, *, mode: str = "operator_defined", browser_capacity: int | None = None, worker_capacity: int | None = None) -> dict[str, Any]:
        unrestricted = mode == "unrestricted"
        canonical = max(0, int(configured_max))
        # The optional arguments are retained for callers that still submit the
        # old shape, but account/browser/worker lanes are deliberately derived
        # from the one canonical runtime setting.
        browser = None if unrestricted else canonical
        worker = None if unrestricted else canonical
        host_capacity = None if unrestricted else canonical
        effective = max(0, int(eligible_count)) if unrestricted else canonical
        return {
            "concurrency_mode": mode,
            "configured_max_concurrent_accounts": int(configured_max),
            "eligible_account_count": int(eligible_count),
            "browser_slot_capacity": None if unrestricted else browser,
            "worker_slot_capacity": None if unrestricted else worker,
            "host_resource_capacity": host_capacity,
            "effective_concurrency": effective,
        }
