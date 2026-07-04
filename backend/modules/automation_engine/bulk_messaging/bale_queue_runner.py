from __future__ import annotations

import time
from typing import Any

from modules.automation_engine.plugins.bale import bale_plugin

from .execution_queue import BulkExecutionQueueStore, execution_queue_store
from .message_source_store import MessageSourceStore, message_source_store
from .models import utc_now


REAL_RUN_LIMIT_CAP = 3


class BaleQueueRunner:
    def __init__(
        self,
        queue_store: BulkExecutionQueueStore | None = None,
        plugin: Any | None = None,
        source_store: MessageSourceStore | None = None,
    ) -> None:
        self.queue_store = queue_store or execution_queue_store
        self.plugin = plugin or bale_plugin
        self.source_store = source_store or message_source_store

    def run(self, campaign_id: str, request: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = request or {}
        if payload.get("dry_run", True) is not False:
            return {
                "ok": False,
                "dry_run": True,
                "campaign_id": campaign_id,
                "error_code": "dry_run_required_for_safe_endpoint",
                "error_message": "Real Bale queue execution requires dry_run=false. Use /queue/dry-run for dry-run execution.",
                "processed_jobs": 0,
                "status_summary": self.queue_store.status_summary(campaign_id),
            }

        requested_limit = _positive_int(payload.get("limit"), 1)
        limit = min(requested_limit, REAL_RUN_LIMIT_CAP)
        account_id = str(payload.get("account_id") or "").strip()
        provider_mode = _provider_mode(payload.get("provider_mode"))
        retry_failed = bool(payload.get("retry_failed", False))
        jobs = self.queue_store.list_jobs()
        selected_indexes = self._select_job_indexes(jobs, campaign_id, limit, account_id, retry_failed)
        if not selected_indexes:
            return {
                "ok": True,
                "dry_run": False,
                "campaign_id": campaign_id,
                "requested_limit": requested_limit,
                "limit": limit,
                "processed_jobs": 0,
                "completed_jobs": 0,
                "failed_jobs": 0,
                "provider_mode": provider_mode,
                "retry_failed": retry_failed,
                "status_summary": self.queue_store.status_summary(campaign_id),
                "sample_results": [],
            }

        for index in selected_indexes:
            now = utc_now()
            jobs[index]["status"] = "running"
            jobs[index]["dry_run"] = False
            jobs[index]["updated_at"] = now
            jobs[index]["error_code"] = None
            jobs[index]["error_message"] = None
        self.queue_store.save_jobs(jobs)

        sample_results: list[dict[str, Any]] = []
        completed = 0
        failed = 0
        for index in selected_indexes:
            job = jobs[index]
            result = self._run_job(job, provider_mode)
            if result["success"]:
                completed += 1
                job["status"] = "completed"
                job["error_code"] = None
                job["error_message"] = None
            else:
                failed += 1
                job["status"] = "failed"
                job["error_code"] = result.get("error_code") or "unknown_error"
                job["error_message"] = result.get("error_message") or "Bale queue job failed"
            job["dry_run"] = False
            job["updated_at"] = result["executed_at"]
            job["execution_result"] = result
            sample_results.append(_sample_result(job))
            self.queue_store.save_jobs(jobs)

        return {
            "ok": True,
            "dry_run": False,
            "campaign_id": campaign_id,
            "requested_limit": requested_limit,
            "limit": limit,
            "processed_jobs": len(selected_indexes),
            "completed_jobs": completed,
            "failed_jobs": failed,
            "provider_mode": provider_mode,
            "retry_failed": retry_failed,
            "status_summary": self.queue_store.status_summary(campaign_id),
            "sample_results": sample_results,
        }

    def _select_job_indexes(
        self,
        jobs: list[dict[str, Any]],
        campaign_id: str,
        limit: int,
        account_id: str,
        retry_failed: bool = False,
    ) -> list[int]:
        selected: list[int] = []
        eligible_statuses = {"pending", "failed"} if retry_failed else {"pending"}
        for index, job in enumerate(jobs):
            if len(selected) >= limit:
                break
            if job.get("campaign_id") != campaign_id:
                continue
            if job.get("status") not in eligible_statuses:
                continue
            if job.get("platform_id") != "bale":
                continue
            if account_id and job.get("account_id") != account_id:
                continue
            selected.append(index)
        return selected

    def _run_job(self, job: dict[str, Any], provider_mode: str = "native_chrome") -> dict[str, Any]:
        started_at = utc_now()
        started_monotonic = time.perf_counter()
        action = "send_text_message"
        try:
            message = self._message_text_for_job(job)
            if hasattr(self.plugin, "send_text_message"):
                plugin_result = self.plugin.send_text_message(
                    account_id=str(job.get("account_id") or ""),
                    normalized_phone=str(job.get("normalized_phone") or ""),
                    contact_naming_value=str(job.get("contact_naming_value") or ""),
                    message_text=message,
                    provider_mode=provider_mode,
                )
            else:
                plugin_result = self.plugin.send_test_message(
                    account_id=str(job.get("account_id") or ""),
                    target=str(job.get("normalized_phone") or ""),
                    message=message,
                    provider_mode=provider_mode,
                )
            success = bool(plugin_result.get("ok"))
            finished_at = utc_now()
            result = {
                "started_at": plugin_result.get("started_at") or started_at,
                "finished_at": plugin_result.get("finished_at") or finished_at,
                "executed_at": plugin_result.get("finished_at") or finished_at,
                "duration_ms": int(plugin_result.get("duration_ms") or ((time.perf_counter() - started_monotonic) * 1000)),
                "runner": "bale_queue_runner",
                "action": action,
                "provider_mode": provider_mode,
                "browser_reused": bool(plugin_result.get("browser_reused", False)),
                "profile_dir": str(plugin_result.get("profile_dir") or ""),
                "success": success,
                "plugin_result": plugin_result,
            }
            if not success:
                result["error_code"] = str(plugin_result.get("error_code") or "plugin_error")
                result["error_message"] = str(plugin_result.get("error") or plugin_result.get("message") or "Bale plugin failed")
            return result
        except Exception as exc:
            finished_at = utc_now()
            return {
                "started_at": started_at,
                "finished_at": finished_at,
                "executed_at": finished_at,
                "duration_ms": int((time.perf_counter() - started_monotonic) * 1000),
                "runner": "bale_queue_runner",
                "action": action,
                "provider_mode": provider_mode,
                "success": False,
                "error_code": _runner_error_code(exc),
                "error_message": str(exc),
            }

    def _message_text_for_job(self, job: dict[str, Any]) -> str:
        source_id = str(job.get("message_source_id") or "")
        source = self.source_store.get_source(source_id) if source_id else None
        if source and str(source.get("source_type") or "") == "text_message":
            return str(source.get("message_ref_value") or source.get("source_ref") or "")
        if source:
            return str(source.get("message_ref_value") or source.get("source_ref") or "")
        return _test_message(job)


def _test_message(job: dict[str, Any]) -> str:
    contact_name = str(job.get("contact_naming_value") or "customer")
    source_id = str(job.get("message_source_id") or "")
    scenario_id = str(job.get("scenario_id") or "")
    parts = [f"ClinicOS controlled Bale queue test for {contact_name}."]
    if source_id:
        parts.append(f"source={source_id}")
    if scenario_id:
        parts.append(f"scenario={scenario_id}")
    return " ".join(parts)


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        return default
    return parsed if parsed > 0 else default


def _provider_mode(value: Any) -> str:
    provider = str(value or "native_chrome").strip()
    return provider if provider in {"native_chrome", "adspower"} else "native_chrome"


def _runner_error_code(exc: Exception) -> str:
    message = str(exc).lower()
    if "cannot switch to a different thread" in message or "greenlet" in message:
        return "browser_thread_error"
    if "timeout" in message and "browser" in message:
        return "browser_start_timeout"
    return "unknown_error"


def _sample_result(job: dict[str, Any]) -> dict[str, Any]:
    execution_result = job.get("execution_result") or {}
    plugin_result = execution_result.get("plugin_result") or {}
    return {
        "job_id": job["job_id"],
        "account_id": job["account_id"],
        "normalized_phone": job["normalized_phone"],
        "contact_naming_value": job["contact_naming_value"],
        "status": job["status"],
        "error_code": job.get("error_code"),
        "error_message": job.get("error_message"),
        "failed_step": plugin_result.get("failed_step") or execution_result.get("failed_step"),
        "contact_save_status": plugin_result.get("contact_save_status") or execution_result.get("contact_save_status"),
        "search_attempts": plugin_result.get("search_attempts") or execution_result.get("search_attempts") or [],
        "screenshot_path": plugin_result.get("screenshot_path") or execution_result.get("screenshot_path"),
        "step_results": plugin_result.get("step_results") or execution_result.get("step_results") or [],
    }


bale_queue_runner = BaleQueueRunner()
