from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

from .assignment_store import AssignmentStore, assignment_store
from .execution_models import EXECUTION_JOB_STATUSES, BulkExecutionJob
from .models import utc_now


STATUS_ORDER = ["pending", "running", "completed", "failed", "skipped", "cancelled"]


def runtime_path() -> Path:
    return Path(__file__).resolve().parents[3] / "runtime" / "bulk_execution_queue.json"


class BulkExecutionQueueStore:
    def __init__(self, path: Path | None = None, assignments_store: AssignmentStore | None = None) -> None:
        self.path = path or runtime_path()
        self.assignments_store = assignments_store or assignment_store

    def list_jobs(self, campaign_id: str | None = None) -> list[dict[str, Any]]:
        jobs = [self._normalize_job(item).to_dict() for item in self._read_json([])]
        if campaign_id is None:
            return jobs
        return [item for item in jobs if item["campaign_id"] == campaign_id]

    def create_from_assignments(
        self,
        campaign_id: str,
        dry_run: bool = True,
        planned_for_date: str = "",
    ) -> dict[str, Any]:
        dry_run = True
        jobs = self.list_jobs()
        existing_assignment_ids = {
            item["assignment_id"]
            for item in jobs
            if item["campaign_id"] == campaign_id and item.get("assignment_id")
        }
        assignments = [
            item
            for item in self.assignments_store.list_assignments(campaign_id)
            if item.get("planned_status") == "planned"
        ]
        if planned_for_date:
            assignments = [item for item in assignments if item.get("planned_for_date") == planned_for_date]

        created_jobs: list[dict[str, Any]] = []
        now = utc_now()
        for assignment in assignments:
            assignment_id = str(assignment.get("assignment_id") or "")
            if not assignment_id or assignment_id in existing_assignment_ids:
                continue
            job = BulkExecutionJob(
                job_id=f"job_{uuid4().hex[:12]}",
                campaign_id=campaign_id,
                route_id=str(assignment.get("route_id") or ""),
                assignment_id=assignment_id,
                platform_id=str(assignment.get("platform_id") or ""),
                account_group_id=str(assignment.get("account_group_id") or ""),
                account_id=str(assignment.get("account_id") or ""),
                contact_id=str(assignment.get("contact_id") or ""),
                normalized_phone=str(assignment.get("normalized_phone") or ""),
                contact_naming_value=str(assignment.get("contact_naming_value") or ""),
                message_source_id=str(assignment.get("message_source_id") or ""),
                scenario_id=str(assignment.get("scenario_id") or ""),
                status="pending",
                dry_run=True,
                planned_for_date=str(assignment.get("planned_for_date") or planned_for_date),
                created_at=now,
                updated_at=now,
            ).to_dict()
            created_jobs.append(job)
            existing_assignment_ids.add(assignment_id)

        if created_jobs:
            jobs.extend(created_jobs)
            self._write_json(jobs)

        total_jobs = self.list_jobs(campaign_id)
        return {
            "ok": True,
            "dry_run": True,
            "campaign_id": campaign_id,
            "created_jobs": len(created_jobs),
            "existing_jobs": len(assignments) - len(created_jobs),
            "total_jobs": len(total_jobs),
            "status_summary": self.status_summary(campaign_id),
            "sample_jobs": self.sample_jobs(campaign_id),
        }

    def summary(self, campaign_id: str) -> dict[str, Any]:
        jobs = self.list_jobs(campaign_id)
        return {
            "ok": True,
            "dry_run": True,
            "campaign_id": campaign_id,
            "total_jobs": len(jobs),
            "status_summary": self.status_summary(campaign_id),
            "sample_jobs": self.sample_jobs(campaign_id),
        }

    def status_summary(self, campaign_id: str) -> dict[str, int]:
        summary = {status: 0 for status in STATUS_ORDER}
        for job in self.list_jobs(campaign_id):
            status = job.get("status", "pending")
            if status in summary:
                summary[status] += 1
        return summary

    def sample_jobs(self, campaign_id: str, limit: int = 10) -> list[dict[str, Any]]:
        return [
            {
                "job_id": job["job_id"],
                "account_id": job["account_id"],
                "normalized_phone": job["normalized_phone"],
                "contact_naming_value": job["contact_naming_value"],
                "status": job["status"],
            }
            for job in self.list_jobs(campaign_id)[:limit]
        ]

    def run_dry_run(self, campaign_id: str, limit: int = 10) -> dict[str, Any]:
        limit = max(0, int(limit))
        jobs = self.list_jobs()
        processed = 0
        now = utc_now()
        for job in jobs:
            if processed >= limit:
                break
            if job["campaign_id"] != campaign_id or job["status"] != "pending":
                continue
            job["status"] = "completed"
            job["dry_run"] = True
            job["updated_at"] = now
            job["dry_run_result"] = {
                "ok": True,
                "dry_run": True,
                "message": "Simulated bulk assignment execution; no browser opened and no message sent.",
                "completed_at": now,
            }
            processed += 1

        if processed:
            self._write_json(jobs)

        return {
            "ok": True,
            "dry_run": True,
            "campaign_id": campaign_id,
            "processed_jobs": processed,
            "status_summary": self.status_summary(campaign_id),
        }

    def _normalize_job(self, payload: dict[str, Any]) -> BulkExecutionJob:
        status = str(payload.get("status") or "pending")
        if status not in EXECUTION_JOB_STATUSES:
            status = "pending"
        created_at = str(payload.get("created_at") or utc_now())
        return BulkExecutionJob(
            job_id=str(payload.get("job_id") or ""),
            campaign_id=str(payload.get("campaign_id") or ""),
            route_id=str(payload.get("route_id") or ""),
            assignment_id=str(payload.get("assignment_id") or ""),
            platform_id=str(payload.get("platform_id") or ""),
            account_group_id=str(payload.get("account_group_id") or ""),
            account_id=str(payload.get("account_id") or ""),
            contact_id=str(payload.get("contact_id") or ""),
            normalized_phone=str(payload.get("normalized_phone") or ""),
            contact_naming_value=str(payload.get("contact_naming_value") or ""),
            message_source_id=str(payload.get("message_source_id") or ""),
            scenario_id=str(payload.get("scenario_id") or ""),
            status=status,
            dry_run=bool(payload.get("dry_run", True)),
            planned_for_date=str(payload.get("planned_for_date") or ""),
            created_at=created_at,
            updated_at=str(payload.get("updated_at") or created_at),
            error_code=payload.get("error_code"),
            error_message=payload.get("error_message"),
            dry_run_result=payload.get("dry_run_result"),
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


execution_queue_store = BulkExecutionQueueStore()
