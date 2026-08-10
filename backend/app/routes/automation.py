from __future__ import annotations

import threading
import asyncio
import logging
import os
import time
import sqlite3
import json
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from modules.automation_engine.bulk_messaging import (
    assignment_planner,
    assignment_store,
    bale_queue_runner,
    BulkCampaignPlanner,
    bulk_campaign_store,
    contact_importer,
    contact_list_store,
    contact_store,
    execution_queue_store,
    message_source_store,
)
from modules.automation_engine.commercial_queue import commercial_queue_service
from modules.automation_engine.bale_bulk_orchestrator import (
    BaleBulkCampaignConfig,
    BaleBulkOrchestratorError,
    bale_bulk_orchestrator,
)
from modules.automation_engine.db.database import DATABASE_PATH
from modules.automation_engine.dispatcher import create_default_dispatcher
from modules.automation_engine.browser.profile_groups import profile_group_store
from modules.automation_engine.browser.profile_provider import BROWSER_PROVIDERS
from modules.automation_engine.browser.providers import get_provider, list_providers
from modules.automation_engine.platforms.platform_registry import (
    get_platform,
    has_platform,
    list_platforms as registry_list_platforms,
)
from modules.automation_engine.account_registry import account_registry_service
from modules.automation_engine.account_registry.bale_onboarding import BaleOnboardingError, bale_onboarding_service
from modules.automation_engine.plugins.bale import bale_plugin
from modules.automation_engine.plugins.bale.account_store import bale_account_store, canonical_source_channel_url, normalize_source_channel_uid
from modules.automation_engine.plugins.bale.contact_store import bale_contact_store
from modules.automation_engine.plugins.bale.governance import can_account_run_scenario
from modules.automation_engine.runtime_sessions.bale_account_executor import bale_account_executor_registry
from modules.automation_engine.queue import TaskQueue, TaskRecord
from modules.automation_engine.scenario_library import (
    ScenarioExecutorStub,
    ScenarioLoader,
    ScenarioValidator,
)
from modules.automation_engine.scenario_engine import ScenarioEngine
from modules.automation_engine.scheduling.account_groups import account_group_store
from modules.automation_engine.scheduling import ScenarioScheduler as DryRunScenarioScheduler
from modules.automation_engine.scheduling import CompliancePolicy
from modules.automation_engine.scheduler import Scheduler
from modules.automation_engine.worker import Worker


def _set_dashboard_cors_headers(response: Response) -> None:
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"


router = APIRouter(prefix="/automation", tags=["automation"])
logger = logging.getLogger(__name__)

_BACKEND_ROOT = Path(__file__).resolve().parents[2]
DIAGNOSTICS_ROOT = _BACKEND_ROOT / "runtime" / "diagnostics"
_DIAGNOSTIC_TEXT_EXTENSIONS = {".json", ".jsonl", ".txt", ".log"}
_DIAGNOSTIC_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
_DIAGNOSTIC_PREVIEW_EXTENSIONS = _DIAGNOSTIC_TEXT_EXTENSIONS | _DIAGNOSTIC_IMAGE_EXTENSIONS
_DIAGNOSTIC_BLOCKED_NAME_PARTS = {
    ".env",
    "cookie",
    "cookies",
    "token",
    "tokens",
    "localstorage",
    "sessionstorage",
    "credential",
    "credentials",
    "browser_profiles",
    "browser_profile",
}
_DIAGNOSTIC_BLOCKED_EXTENSIONS = {".db", ".sqlite", ".sqlite3", ".pem", ".key", ".pfx"}
_DIAGNOSTIC_MAX_PREVIEW_BYTES = 512_000
_DIAGNOSTIC_MAX_RUNS = 250
_DIAGNOSTIC_MAX_FILES = 600
_client_diagnostic_lock = threading.RLock()

queue = TaskQueue()
scheduler = Scheduler(queue)
dispatcher = create_default_dispatcher()
scenario_engine = ScenarioEngine(dispatcher)
worker = Worker(queue, scheduler, scenario_engine)
scenario_loader = ScenarioLoader()
scenario_validator = ScenarioValidator()
scenario_executor_stub = ScenarioExecutorStub()
scenario_dry_run_scheduler = DryRunScenarioScheduler()
bulk_campaign_planner = BulkCampaignPlanner()

_worker_thread: threading.Thread | None = None
_worker_lock = threading.Lock()


def _diagnostics_root() -> Path:
    DIAGNOSTICS_ROOT.mkdir(parents=True, exist_ok=True)
    return DIAGNOSTICS_ROOT.resolve()


def _is_blocked_diagnostic_path(path: Path) -> bool:
    lowered_parts = [part.lower() for part in path.parts]
    if any(part in _DIAGNOSTIC_BLOCKED_NAME_PARTS for part in lowered_parts):
        return True
    name = path.name.lower()
    if any(part in name for part in _DIAGNOSTIC_BLOCKED_NAME_PARTS):
        return True
    return path.suffix.lower() in _DIAGNOSTIC_BLOCKED_EXTENSIONS


def _safe_diagnostics_path(relative_path: str | None = None, *, require_exists: bool = True) -> Path:
    raw = (relative_path or "").strip().replace("\\", "/")
    candidate = Path(raw)
    if raw.startswith("/") or candidate.is_absolute() or any(part == ".." for part in candidate.parts):
        raise HTTPException(status_code=403, detail="Diagnostics path is not allowed")
    root = _diagnostics_root()
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Diagnostics path is not allowed") from exc
    if require_exists and not resolved.exists():
        raise HTTPException(status_code=404, detail="Diagnostics path was not found")
    if _is_blocked_diagnostic_path(resolved.relative_to(root)):
        raise HTTPException(status_code=403, detail="Diagnostics file is not allowed")
    return resolved


def _diagnostic_file_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".json":
        return "json"
    if suffix == ".jsonl":
        return "jsonl"
    if suffix in {".txt", ".log"}:
        return "text"
    if suffix in _DIAGNOSTIC_IMAGE_EXTENSIONS:
        return "image"
    return "unsupported"


def _diagnostic_file_info(path: Path, root: Path) -> dict[str, Any]:
    stat = path.stat()
    relative = path.relative_to(root).as_posix()
    kind = _diagnostic_file_kind(path)
    return {
        "name": path.name,
        "relative_path": relative,
        "kind": kind,
        "extension": path.suffix.lower(),
        "size_bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        "preview_supported": kind in {"json", "jsonl", "text", "image"},
        "download_supported": kind in {"json", "jsonl", "text", "image"},
    }


def _diagnostic_run_summary(run_dir: Path, root: Path) -> dict[str, Any]:
    files = [path for path in run_dir.rglob("*") if path.is_file() and not _is_blocked_diagnostic_path(path.relative_to(root))]
    latest_mtime = max([run_dir.stat().st_mtime, *[path.stat().st_mtime for path in files]], default=run_dir.stat().st_mtime)
    status_file = next((name for name in ("result.json", "batch_summary.json", "readiness_summary.json") if (run_dir / name).is_file()), "")
    return {
        "name": run_dir.name,
        "modified_at": datetime.fromtimestamp(latest_mtime, timezone.utc).isoformat(),
        "file_count": len(files),
        "screenshot_count": sum(1 for path in files if path.suffix.lower() in _DIAGNOSTIC_IMAGE_EXTENSIONS),
        "status_file": status_file,
    }


class CreateTaskRequest(BaseModel):
    scenario_path: str
    run_at: datetime | None = None


class BaleBulkSourceRequest(BaseModel):
    uid: str = ""
    url: str = ""


class BaleBulkRecipientRequest(BaseModel):
    recipient_id: str = ""
    display_name: str = ""
    enabled: bool = True


class BaleBulkCampaignRequest(BaseModel):
    campaign_id: str
    platform: str = "bale"
    account_id: str
    source: BaleBulkSourceRequest
    recipients: list[BaleBulkRecipientRequest] = Field(default_factory=list, max_length=10)
    mode: str = "dry_run"
    concurrency: int = 1
    max_recipients: int = 10
    max_final_clicks: int = 0
    continue_on_not_found: bool = True
    stop_on_ambiguous: bool = True
    stop_on_selection_mismatch: bool = True
    stop_on_structural_failure: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)
    explicit_live_authorized: bool = False


class BaleBulkRunRequest(BaseModel):
    run_id: str | None = None


def _bale_bulk_error(exc: Exception) -> HTTPException:
    code = getattr(exc, "error_code", None) or "bale_bulk_orchestrator_error"
    status = 404 if code == "campaign_not_found" else 409
    return HTTPException(status_code=status, detail={"error_code": code, "message": str(exc), "details": getattr(exc, "details", {})})


class RunTaskRequest(BaseModel):
    task_id: str


@router.post("/diagnostics/client-events", status_code=202)
def persist_client_diagnostic_event(event: dict[str, Any], response: Response) -> dict[str, Any]:
    """Persist scrubbed frontend action evidence so exports survive navigation/restart."""
    _set_dashboard_cors_headers(response)
    allowed = {
        "timestamp", "event_id", "action", "module", "account_id", "campaign_id", "success", "status",
        "error_code", "error_message", "endpoint", "duration_ms", "related_files", "source_revision",
        "clicked_action", "handler_reached", "button_disabled", "disabled_reason", "pending_state",
        "operation_id", "http_method", "stage", "result",
    }
    payload = {key: event.get(key) for key in allowed if key in event}
    payload["received_at"] = datetime.now(timezone.utc).isoformat()
    target = _diagnostics_root() / "frontend_client_events.jsonl"
    with _client_diagnostic_lock:
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    return {"accepted": True, "event_id": payload.get("event_id"), "source_revision": payload.get("source_revision")}


@router.get("/diagnostics/client-events")
def list_client_diagnostic_events(response: Response, limit: int = Query(250, ge=1, le=1000)) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    target = _diagnostics_root() / "frontend_client_events.jsonl"
    if not target.exists():
        return {"items": [], "persisted": True}
    with _client_diagnostic_lock:
        lines = target.read_text(encoding="utf-8").splitlines()[-limit:]
    items = []
    for line in reversed(lines):
        try:
            items.append(json.loads(line))
        except (TypeError, ValueError):
            continue
    return {"items": items, "persisted": True}


@router.get("/diagnostics/runs")
def list_diagnostic_runs(response: Response, limit: int = Query(100, ge=1, le=_DIAGNOSTIC_MAX_RUNS)) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    root = _diagnostics_root()
    runs = [
        _diagnostic_run_summary(path, root)
        for path in root.iterdir()
        if path.is_dir() and not _is_blocked_diagnostic_path(path.relative_to(root))
    ]
    runs.sort(key=lambda item: item["modified_at"], reverse=True)
    return {
        "diagnostics_root": "backend/runtime/diagnostics",
        "runs": runs[:limit],
        "total": len(runs),
        "read_only": True,
    }


@router.get("/diagnostics/runs/{run_name}/files")
def list_diagnostic_run_files(run_name: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    run_dir = _safe_diagnostics_path(run_name)
    if not run_dir.is_dir():
        raise HTTPException(status_code=404, detail="Diagnostics run was not found")
    root = _diagnostics_root()
    files = [
        _diagnostic_file_info(path, root)
        for path in run_dir.rglob("*")
        if path.is_file() and not _is_blocked_diagnostic_path(path.relative_to(root))
    ]
    files.sort(key=lambda item: (item["relative_path"].count("/"), item["relative_path"]))
    return {
        "run_name": run_dir.name,
        "files": files[:_DIAGNOSTIC_MAX_FILES],
        "total": len(files),
        "read_only": True,
    }


@router.get("/diagnostics/preview")
def preview_diagnostic_file(path: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    file_path = _safe_diagnostics_path(path)
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Diagnostics file was not found")
    kind = _diagnostic_file_kind(file_path)
    info = _diagnostic_file_info(file_path, _diagnostics_root())
    if kind == "unsupported":
        return {"file": info, "preview": None, "truncated": False, "parse_error": "", "read_only": True}
    if kind == "image":
        return {"file": info, "preview": None, "image_url": f"/automation/diagnostics/download?path={path}", "truncated": False, "parse_error": "", "read_only": True}
    size = file_path.stat().st_size
    with file_path.open("rb") as handle:
        raw = handle.read(_DIAGNOSTIC_MAX_PREVIEW_BYTES + 1)
    truncated = len(raw) > _DIAGNOSTIC_MAX_PREVIEW_BYTES
    text = raw[:_DIAGNOSTIC_MAX_PREVIEW_BYTES].decode("utf-8", errors="replace")
    parse_error = ""
    parsed: Any = None
    if kind == "json":
        try:
            parsed = json.loads(text) if not truncated else None
        except json.JSONDecodeError as exc:
            parse_error = f"Invalid JSON at line {exc.lineno}, column {exc.colno}"
    return {
        "file": info,
        "preview": text,
        "parsed_json": parsed,
        "truncated": truncated or size > _DIAGNOSTIC_MAX_PREVIEW_BYTES,
        "parse_error": parse_error,
        "max_preview_bytes": _DIAGNOSTIC_MAX_PREVIEW_BYTES,
        "read_only": True,
    }


@router.get("/diagnostics/download")
def download_diagnostic_file(path: str) -> FileResponse:
    file_path = _safe_diagnostics_path(path)
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Diagnostics file was not found")
    if file_path.suffix.lower() not in _DIAGNOSTIC_PREVIEW_EXTENSIONS:
        raise HTTPException(status_code=415, detail="Diagnostics file type is not downloadable")
    return FileResponse(file_path, filename=file_path.name)


@router.get("/platforms/bale/bulk-campaigns")
def list_bale_bulk_campaigns(response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return {"items": bale_bulk_orchestrator.store.list()}


@router.post("/platforms/bale/bulk-campaigns")
def save_bale_bulk_campaign(payload: BaleBulkCampaignRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return bale_bulk_orchestrator.save_draft(payload.model_dump())
    except BaleBulkOrchestratorError as exc:
        raise _bale_bulk_error(exc) from exc


@router.get("/platforms/bale/bulk-campaigns/{campaign_id}")
def get_bale_bulk_campaign(campaign_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return bale_bulk_orchestrator.get_state(campaign_id)
    except BaleBulkOrchestratorError as exc:
        raise _bale_bulk_error(exc) from exc


@router.post("/platforms/bale/bulk-campaigns/{campaign_id}/validate")
def validate_bale_bulk_campaign(campaign_id: str, payload: BaleBulkCampaignRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        config = BaleBulkCampaignConfig.from_payload({**payload.model_dump(), "campaign_id": campaign_id})
        return bale_bulk_orchestrator.validate_config(
            config,
            explicit_live_authorized=payload.explicit_live_authorized,
        )
    except BaleBulkOrchestratorError as exc:
        raise _bale_bulk_error(exc) from exc


@router.post("/platforms/bale/bulk-campaigns/{campaign_id}/dry-preflight")
def run_bale_bulk_dry_preflight(campaign_id: str, payload: BaleBulkRunRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return bale_bulk_orchestrator.run_dry_preflight(campaign_id, run_id=payload.run_id)
    except BaleBulkOrchestratorError as exc:
        raise _bale_bulk_error(exc) from exc


@router.get("/platforms/bale/bulk-campaigns/{campaign_id}/results")
def get_bale_bulk_results(campaign_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        state = bale_bulk_orchestrator.get_state(campaign_id)
        return {
            "campaign_id": campaign_id,
            "recipient_results": state.get("resume_state", {}).get("items", []),
            "stored_campaign": state,
        }
    except BaleBulkOrchestratorError as exc:
        raise _bale_bulk_error(exc) from exc


@router.get("/platforms/bale/bulk-campaigns/{campaign_id}/resume-state")
def get_bale_bulk_resume_state(campaign_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        stored = bale_bulk_orchestrator.store.get(campaign_id)
        if stored is None:
            raise BaleBulkOrchestratorError("campaign_not_found", "Bale bulk campaign was not found")
        config = BaleBulkCampaignConfig.from_payload(stored["config"])
        return bale_bulk_orchestrator.resume_state(config)
    except BaleBulkOrchestratorError as exc:
        raise _bale_bulk_error(exc) from exc


@router.post("/platforms/bale/bulk-campaigns/{campaign_id}/live-run")
def prepare_bale_bulk_live_run(campaign_id: str, payload: BaleBulkCampaignRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    config_payload = {**payload.model_dump(), "campaign_id": campaign_id, "mode": "live"}
    config = BaleBulkCampaignConfig.from_payload(config_payload)
    validation = bale_bulk_orchestrator.validate_config(config, explicit_live_authorized=payload.explicit_live_authorized)
    enabled_nonterminal = bale_bulk_orchestrator.resume_state(config)["items"]
    enabled_nonterminal_count = sum(1 for item in enabled_nonterminal if item["enabled"] and item["state"] == "pending")
    if config.max_final_clicks > enabled_nonterminal_count:
        validation = {
            **validation,
            "ok": False,
            "errors": [
                *validation.get("errors", []),
                {"field": "max_final_clicks", "error_code": "click_budget_exceeds_enabled_nonterminal_recipients"},
            ],
        }
    if not payload.explicit_live_authorized or not validation["ok"]:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "live_run_not_authorized",
                "message": "Controlled live Bale bulk execution requires explicit authorization and valid campaign readiness.",
                "validation": validation,
            },
        )
    return {
        "campaign_id": campaign_id,
        "live_run_ready": True,
        "execution_started": False,
        "controlled_live_endpoint": True,
        "enabled_nonterminal_recipient_count": enabled_nonterminal_count,
        "maximum_possible_final_clicks": config.max_final_clicks,
    }


class CreatePlatformAccountRequest(BaseModel):
    phone: str
    status: str = "active"
    daily_limit: int = 50


class BaleAccountRequest(BaseModel):
    account_id: str | None = None
    username_or_number: str = ""
    phone: str = ""
    status: str = "new"
    section: str = "new_accounts"
    daily_limit: int = 10
    hourly_limit: int = 2
    min_delay_seconds: int = 300
    max_actions_per_session: int = 5
    health_score: int = 100
    login_status: str = "unknown"
    block_status: str = "unknown"
    consecutive_failures: int = 0
    notes: str = ""
    account_group_id: str = "bale_test_group"
    account_group_name: str = "Bale Test Group"
    browser_provider: str = "adspower"
    profile_id: str | None = None
    adspower_profile_id: str | None = None
    profile_group_id: str = "group_001"
    device_group_id: str | None = None
    batch_capacity: int = 30
    max_concurrent_per_group: int = 5
    priority: int = 100
    enabled_for_scheduling: bool = True
    worker_id: str = "local_windows_1"
    user_data_dir: str | None = None


class BaleAccountUpdateRequest(BaseModel):
    username_or_number: str | None = None
    phone: str | None = None
    status: str | None = None
    section: str | None = None
    daily_limit: int | None = None
    hourly_limit: int | None = None
    min_delay_seconds: int | None = None
    max_actions_per_session: int | None = None
    health_score: int | None = None
    login_status: str | None = None
    block_status: str | None = None
    consecutive_failures: int | None = None
    notes: str | None = None
    account_group_id: str | None = None
    account_group_name: str | None = None
    browser_provider: str | None = None
    profile_id: str | None = None
    adspower_profile_id: str | None = None
    profile_group_id: str | None = None
    device_group_id: str | None = None
    batch_capacity: int | None = None
    max_concurrent_per_group: int | None = None
    priority: int | None = None
    enabled_for_scheduling: bool | None = None
    worker_id: str | None = None
    user_data_dir: str | None = None


class AccountGroupRequest(BaseModel):
    group_id: str | None = None
    name: str
    platform_id: str = "bale"
    browser_provider: str = "native_chrome"
    device_group_id: str = "device_group_001"
    profile_group_id: str = "default"
    max_concurrent: int = 5
    batch_capacity: int = 30
    daily_capacity: int = 100
    enabled: bool = True
    notes: str = ""


class MessageSourceRequest(BaseModel):
    message_source_id: str | None = None
    platform_id: str = "bale"
    name: str
    campaign_tag: str = ""
    source_type: str = "channel"
    source_ref: str = ""
    message_ref_type: str = "latest"
    message_ref_value: str = ""
    enabled: bool = True
    notes: str = ""


class ContactListRequest(BaseModel):
    contact_list_id: str | None = None
    name: str
    platform_id: str = ""
    campaign_tag: str = ""
    source_filename: str = ""
    total_contacts: int = 0
    valid_contacts: int = 0
    duplicate_contacts: int = 0
    status: str = "draft"
    notes: str = ""


class ManualContactImportRequest(BaseModel):
    name: str
    platform_id: str = "bale"
    campaign_tag: str = ""
    phones_text: str
    notes: str = ""


class BulkCampaignRequest(BaseModel):
    campaign_id: str | None = None
    name: str
    campaign_tag: str = ""
    status: str = "draft"
    dry_run: bool = True
    notes: str = ""


class CampaignRouteRequest(BaseModel):
    route_id: str | None = None
    platform_id: str = "bale"
    account_group_id: str
    message_source_id: str
    contact_list_id: str
    scenario_id: str = "save_contact_and_forward_from_source"
    contact_naming_pattern: str = "Bale-GHAB-{seq:06d}"
    daily_limit_per_account: int = 50
    hourly_limit_per_account: int = 5
    enabled: bool = True


class BulkAssignmentRequest(BaseModel):
    dry_run: bool = True
    planned_for_date: str | None = None
    max_contacts_per_account: int | None = None
    plan_seed: str | None = None


class BulkQueueRequest(BaseModel):
    dry_run: bool = True
    planned_for_date: str | None = None


class BulkQueueDryRunRequest(BaseModel):
    limit: int = 10


class BaleQueueRunRequest(BaseModel):
    dry_run: bool = True
    limit: int = 1
    account_id: str | None = None
    provider_mode: str = "native_chrome"
    retry_failed: bool = False


class BaleProfileGroupRequest(BaseModel):
    profile_group_id: str | None = None
    device_group_id: str | None = None
    name: str = "گروه ۱"
    max_accounts: int = 0
    group_size_limit: int | None = None
    max_concurrent_accounts: int = 0
    browser_provider: str = "adspower"
    adspower_group_id: str = ""
    account_ids: list[str] = []
    notes: str = ""


class BaleAssignProfileGroupRequest(BaseModel):
    profile_group_id: str | None = None
    device_group_id: str | None = None
    browser_provider: str = "adspower"
    profile_id: str | None = None
    adspower_profile_id: str | None = None


class AdsPowerConfigRequest(BaseModel):
    enabled: bool = True
    api_base_url: str = "http://127.0.0.1:50325"
    api_token: str = ""
    open_timeout_seconds: int = 60


class BaleMessageConfigRequest(BaseModel):
    source_type: str = "message_link"
    source_value: str = ""
    source_message_hint: str = ""
    description: str = ""
    dry_run: bool = True


class BaleTestForwardRequest(BaseModel):
    account_id: str
    target: str
    dry_run: bool = True
    source_type: str | None = None
    source_value: str | None = None
    source_message_hint: str | None = None
    description: str | None = None
    compliance_policy: dict[str, Any] = Field(default_factory=dict)


class BaleScheduleDryRunRequest(BaseModel):
    selected_accounts: list[str] = []
    scenario_id: str = "forward_from_source"
    work_start: str = "10:00"
    work_end: str = "18:00"
    daily_limit: int = 50
    hourly_limit: int = 6
    min_delay_seconds: int = 300
    max_actions_per_session: int = 5
    max_concurrent_browsers: int = 10
    close_browser_after_task: bool = True
    compliance_policy: dict[str, Any] = Field(default_factory=dict)
    plan_seed: str | None = None


class BalePreparationRequest(BaseModel):
    enabled: bool = False
    mode: str = "qa_only"
    selected_accounts: list[str] = []
    rules: dict[str, Any] = {}
    account_sections: list[str] = []


class BaleOpenAccountRequest(BaseModel):
    account_id: str


class BaleSendTestRequest(BaseModel):
    account_id: str
    target: str | dict[str, Any]
    message: str = ""
    action: str = "send_text_message"
    source: dict[str, Any] | None = None
    normalized_phone: str | None = None
    contact_naming_value: str | None = None


class BaleContactsBulkRequest(BaseModel):
    account_id: str
    phones: list[str]


class BaleSourceChannelRequest(BaseModel):
    account_id: str
    source_channel_url: str


class BalePreviewLatestRequest(BaseModel):
    account_id: str


class BaleSaveContactRequest(BaseModel):
    account_id: str
    phone: str


class BaleOpenSourceChannelRequest(BaseModel):
    account_id: str
    source_channel_uid: str


class BaleLocateLatestChannelMessageRequest(BaseModel):
    account_id: str
    source_channel_uid: str


class BaleOpenMessageForwardRequest(BaseModel):
    account_id: str
    source_channel_uid: str


class BaleForwardMessageToContactRequest(BaseModel):
    account_id: str
    source_channel_uid: str
    display_name: str
    dry_run: bool = False
    selection_only: bool = False


class BaleForwardLatestChannelMessageRequest(BaseModel):
    job_id: str | None = None
    campaign_id: str | None = None
    account_id: str
    source_channel_uid: str | None = None
    phone: str
    display_name: str | None = None
    recipient_id: str | None = None
    idempotency_key: str | None = None
    dry_run: bool = False


class BalePrepareAuthorizedContactsRequest(BaseModel):
    account_id: str
    recipient_ids: list[str] = Field(min_length=1, max_length=2)


class BaleAuthenticationOpenRequest(BaseModel):
    account_id: str
    purpose: str = "login"


class BaleOnboardingPreflightRequest(BaseModel):
    identifier: str
    account_id: str | None = None


class BaleOnboardingProvisionRequest(BaleOnboardingPreflightRequest):
    idempotency_key: str
    onboarding_batch_id: str | None = None
    created_by: str = "operator"


class BaleOnboardingBatchRequest(BaseModel):
    name: str
    target_count: int = Field(ge=1)
    account_ids: list[str] = Field(default_factory=list)
    created_by: str = "operator"


class BaleSchedulingRequest(BaseModel):
    enabled: bool


class BaleRetireRequest(BaseModel):
    reason: str = "retired_by_operator"

class BaleProfileResetRequest(BaseModel):
    confirmation: str

class BaleOperationalConfigurationRequest(BaseModel):
    configuration: dict[str, Any]
    actor: str = "operator-ui"


class GlobalSettingsRequest(BaseModel):
    concurrency_mode: str | None = None
    operator_defined_max_concurrent_accounts: int | None = None
    browser_concurrency: int | None = None
    worker_concurrency: int | None = None
    max_concurrent_accounts: int | None = None
    deliveries_per_account_round: int | None = None
    delay_between_deliveries_seconds: int | None = None
    round_cooldown_seconds: int | None = None
    default_daily_limit_per_account: int | None = None
    default_source_channel_uid: str | None = None
    account_assignment_strategy: str | None = None
    max_job_duration_seconds: int | None = None
    job_timeout_seconds: int | None = None
    auto_pause_on_auth_error: bool | None = None
    auto_pause_on_selector_error: bool | None = None
    send_method: str | None = None
    operation_order_json: str | None = None
    link_open_delay_seconds: int | None = None
    browser_start_batch_size: int | None = None
    browser_start_stagger_ms: int | None = None
    max_system_memory_percent: int | None = None
    max_system_cpu_percent: int | None = None
    session_reuse_enabled: bool | None = None
    resource_guard_enabled: bool | None = None
    campaign_overrides_enabled: bool | None = None
    automatic_retry_enabled: bool | None = None
    live_campaign_execution_enabled: bool | None = None


class AccountSettingsRequest(BaseModel):
    enabled: bool | None = None
    priority: int | None = None
    daily_limit_override: int | None = None
    deliveries_per_round_override: int | None = None
    delay_between_deliveries_override: int | None = None
    round_cooldown_override: int | None = None
    source_channel_uid_override: str | None = None
    current_daily_sent_count: int | None = None
    current_round_sent_count: int | None = None
    worker_status: str | None = None
    cooldown_until: str | None = None
    last_job_started_at: str | None = None
    last_job_completed_at: str | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None


# ============================================================
# BLOCK: CAMPAIGN_CAPACITY_RESERVATION_API_CONTRACT
# PURPOSE:
# Accepts one shared-pool capacity reservation in the existing campaign API.
# ACCOUNT_SCOPE:
# No account is selected or owned by this contract.
# DEPENDENCIES:
# CommercialQueueService
# LAYER:
# API
# ============================================================


class CommercialCampaignRequest(BaseModel):
    name: str
    platform: str = "bale"
    status: str = "draft"
    source_channel_uid: str | None = None
    policy_overrides: dict[str, Any] | None = None
    capacity_reservation: int | None = Field(default=None, ge=0)


class CommercialCampaignPatchRequest(BaseModel):
    name: str | None = None
    platform: str | None = None
    status: str | None = None
    source_channel_uid: str | None = None
    policy_overrides: dict[str, Any] | None = None
    policy_overrides_json: str | None = None
    capacity_reservation: int | None = Field(default=None, ge=0)
    started_at: str | None = None
    paused_at: str | None = None
    completed_at: str | None = None


class CampaignCapacityRequest(BaseModel):
    requested_account_count: int = Field(ge=1)


# ============================================================
# END BLOCK: CAMPAIGN_CAPACITY_RESERVATION_API_CONTRACT
# ============================================================


class CampaignConfigurationDraftRequest(BaseModel):
    configuration: dict[str, Any] = Field(default_factory=dict)
    change_summary: str | None = None
    created_by: str = "user"


class CampaignConfigurationRevisionRequest(BaseModel):
    configuration: dict[str, Any] = Field(default_factory=dict)
    change_summary: str | None = None
    created_by: str = "user"


class CampaignConfigurationApproveRequest(BaseModel):
    approved_by: str = "user"
    approval_id: str | None = None


class CampaignConfigurationSnapshotRequest(BaseModel):
    revision_id: str | None = None
    approval_id: str | None = None
    created_by: str = "system"


class CampaignSendApprovalRequest(BaseModel):
    final_review_hash: str | None = None
    requested_by: str = "user"
    approval_scope: str = "campaign_send"
    explicit_confirmation: bool = False


class CampaignQueueRequest(BaseModel):
    validation_hash: str | None = None
    validation_id: str | None = None
    final_review_hash: str | None = None
    review_token: str | None = None
    manifest_hash: str | None = None
    approval_id: str | None = None
    idempotency_key: str | None = None
    explicit_operator_confirmation: bool = False
    expected_campaign_status: str = "draft"


class CampaignFinalReviewRequest(BaseModel):
    explicit_operator_confirmation: bool = False
    approved_by: str = "campaign_operator"


class CampaignLivePreflightRequest(BaseModel):
    approval_id: str | None = None


class CampaignExecuteRequest(BaseModel):
    approval_id: str
    final_review_hash: str
    execution_snapshot_id: str
    idempotency_key: str
    requested_by: str = "test"
    mode: str = "disabled"
    platforms: list[str] | None = None


class CampaignScenarioRetryRequest(BaseModel):
    approval_id: str
    final_review_hash: str
    execution_snapshot_id: str
    idempotency_key: str
    requested_by: str = "test"
    mode: str = "disabled"
    platforms: list[str] | None = None


class ExecutionBatchCancelRequest(BaseModel):
    reason: str = "cancelled_by_request"


class RecipientImportRequest(BaseModel):
    phones: list[str]
    import_source: str = "manual"


class RecipientPreviewRequest(BaseModel):
    phones: list[str]
    source_type: str = "manual"


class RecipientConfirmRequest(BaseModel):
    phones: list[str]
    source_type: str = "manual"
    submitted_by: str = "user"
    confirmation_checked: bool = False


class CampaignMaterializeRequest(BaseModel):
    platforms: list[str]
    authorize_for_live_execution: bool = False
    authorized_by: str | None = None
    authorization_note: str | None = None
    create_platform_identities: bool = False


class CampaignPlatformSettingsRequest(BaseModel):
    platforms: list[str]
    platform_settings: dict[str, Any]
    created_by: str = "user"


class RecipientScenarioStartRequest(BaseModel):
    recipient_id: str
    platforms: list[str]
    global_contact_id: str | None = None
    correlation_id: str | None = None


class CampaignScenarioStartRequest(BaseModel):
    platforms: list[str]


class PlatformRunOutcomeRequest(BaseModel):
    outcome: str
    stable_display_name: str | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None


class RecipientImportPreviewRequest(BaseModel):
    import_source: str = "paste"
    content: str


class RecipientImportConfirmRequest(BaseModel):
    include_valid: bool = True
    selected_item_ids: list[str] | None = None
    default_priority: int = 0
    scheduled_at: str | None = None
    authorize_for_live_execution: bool = False
    authorized_by: str | None = None
    authorization_note: str | None = None


class RecipientLiveAuthorizeRequest(BaseModel):
    authorization_note: str
    authorized_by: str | None = None


class RecipientLiveRevokeRequest(BaseModel):
    reason: str


class RecipientAuthorizationMigrationRequest(BaseModel):
    dry_run: bool = True


class LiveReadinessRequest(BaseModel):
    account_ids: list[str] | None = None
    max_jobs: int | None = None


class LiveApprovalCreateRequest(BaseModel):
    requested_by: str
    approval_note: str
    account_ids: list[str] | None = None
    max_jobs: int | None = None
    expires_in_minutes: int = 30


class LiveApprovalApproveRequest(BaseModel):
    approved_by: str


class LiveApprovalRevokeRequest(BaseModel):
    reason: str


class WorkerAssignRequest(BaseModel):
    campaign_id: str | None = None
    limit: int | None = None


class WorkerRunRoundRequest(BaseModel):
    campaign_id: str | None = None
    max_jobs: int | None = None


class SchedulerRunOnceRequest(BaseModel):
    campaign_id: str | None = None


class TestWorkerFaultRequest(BaseModel):
    fault_mode: str | None = None
    account_id: str | None = None
    run_replacement_tick: bool = True


class TestUncertainDeliveryReconciliationRequest(BaseModel):
    job_id: str


class BrowserIdentityUpdateRequest(BaseModel):
    profile_path: str | None = None
    locale: str | None = None
    timezone_id: str | None = None
    viewport_width: int | None = None
    viewport_height: int | None = None
    device_scale_factor: float | None = None
    chrome_channel: str | None = None
    network_route_id: str | None = None
    worker_node_id: str | None = None
    enabled: bool | None = None


class BrowserIdentityMigrationRequest(BaseModel):
    dry_run: bool = True


def _serialize_task(task: TaskRecord) -> dict[str, Any]:
    return {
        "task_id": task["task_id"],
        "account_id": task.get("account_id"),
        "platform": task.get("platform"),
        "operation": task.get("operation"),
        "trigger": task.get("trigger"),
        "scenario_path": task["scenario_path"],
        "status": task["status"],
        "created_at": _serialize_datetime(task.get("created_at")),
        "run_at": _serialize_datetime(task.get("run_at")),
        "logs": task.get("logs", []),
    }


def _serialize_log(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "task_id": row["task_id"],
        "level": "info",
        "message": row["message"],
        "timestamp": _serialize_datetime(row["timestamp"]),
    }


def _serialize_account(account: Any) -> dict[str, Any]:
    runtime_state = getattr(account, "runtime_state", {}) or {}
    active = bool(runtime_state.get("active", False))
    return {
        "account_id": str(getattr(account, "account_id", "")),
        "platform": str(getattr(account, "platform", "default")),
        "status": str(runtime_state.get("status") or ("active" if active else "inactive")),
        "active": active,
    }


def _serialize_datetime(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _normalize_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.astimezone()
    return value


def _find_task(task_id: str) -> TaskRecord:
    for task in queue.get_all_tasks():
        if task["task_id"] == task_id:
            return task
    raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")


def _set_task_run_at(task_id: str, run_at: datetime | None) -> None:
    # The current in-memory queue exposes task reads and status transitions.
    # This API-only helper makes an existing scheduled task eligible for run_once.
    if task_id not in queue._tasks:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
    queue._tasks[task_id]["run_at"] = run_at


def _ensure_platform(platform_id: str) -> None:
    if not has_platform(platform_id):
        raise HTTPException(status_code=404, detail=f"Platform not found: {platform_id}")


def _filter_items_by_platform(items: list[dict[str, Any]], platform_id: str) -> list[dict[str, Any]]:
    return [
        item
        for item in items
        if str(item.get("platform") or item.get("platform_id") or "").lower() == platform_id
    ]


@router.post("/task/create")
def create_task(request: CreateTaskRequest) -> dict[str, Any]:
    task = scheduler.schedule_task(
        {"scenario_path": request.scenario_path},
        run_at=_normalize_datetime(request.run_at),
    )
    return {
        "task_id": task["task_id"],
        "status": task["status"],
    }


@router.post("/task/run")
def run_task(request: RunTaskRequest) -> dict[str, Any]:
    task = _find_task(request.task_id)
    if task["status"] not in {"pending", "failed"}:
        raise HTTPException(
            status_code=409,
            detail=f"Task {request.task_id} cannot be run from status {task['status']}",
        )

    _set_task_run_at(request.task_id, None)
    worker.run_once()
    updated_task = _find_task(request.task_id)
    return _serialize_task(updated_task)


@router.get("/task/status/{task_id}")
def task_status(task_id: str) -> dict[str, Any]:
    return _serialize_task(_find_task(task_id))


@router.options("/{path:path}")
def automation_options(path: str, response: Response) -> Response:
    _set_dashboard_cors_headers(response)
    response.status_code = 204
    return response


@router.get("/tasks")
def list_tasks(response: Response) -> list[dict[str, Any]]:
    _set_dashboard_cors_headers(response)
    return [_serialize_task(task) for task in queue.get_all_tasks()]


@router.get("/logs")
def list_logs(response: Response) -> list[dict[str, Any]]:
    _set_dashboard_cors_headers(response)
    if not DATABASE_PATH.exists():
        return []

    try:
        uri = f"file:{DATABASE_PATH.as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            connection.row_factory = sqlite3.Row
            table = connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name = 'task_logs'
                """
            ).fetchone()
            if table is None:
                return []

            rows = connection.execute(
                """
                SELECT id, task_id, message, timestamp
                FROM task_logs
                ORDER BY id DESC
                LIMIT 100
                """
            ).fetchall()
            return [_serialize_log(row) for row in reversed(rows)]
    except Exception:
        return []


@router.get("/settings/global")
def get_global_settings(response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return commercial_queue_service.get_global_settings()


@router.put("/settings/global")
def update_global_settings(request: GlobalSettingsRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return commercial_queue_service.update_global_settings(request.model_dump(exclude_unset=True))


@router.get("/policy/effective")
def get_effective_policy(response: Response, account_id: str | None = None, campaign_id: str | None = None, platform: str = "bale") -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return commercial_queue_service.resolve_effective_policy(account_id=account_id, campaign_id=campaign_id, platform=platform)


@router.get("/architecture/capabilities")
def get_architecture_capabilities(response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return commercial_queue_service.architecture_capabilities()


@router.get("/resources/status")
def get_resources_status(response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return commercial_queue_service.resource_status()


@router.get("/runtime-sessions")
def list_runtime_sessions(response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return {"items": commercial_queue_service.runtime_session_manager.list_active_sessions()}


@router.get("/runtime-sessions/{account_id}")
def get_runtime_session(account_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    diagnostics = commercial_queue_service.runtime_session_manager.get_session_diagnostics(account_id)
    if diagnostics is None:
        raise HTTPException(status_code=404, detail={"error_code": "session_not_found", "error_message": "Runtime session not found"})
    return diagnostics


@router.post("/runtime-sessions/{account_id}/close")
def close_runtime_session(account_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    result = commercial_queue_service.runtime_session_manager.close_session_for_account(account_id)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result)
    return result


@router.get("/browser-identities")
def list_browser_identities(response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return commercial_queue_service.list_browser_identities()


@router.get("/browser-identities/{account_id}")
def get_browser_identity(account_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return commercial_queue_service.get_browser_identity(account_id)


@router.put("/browser-identities/{account_id}")
def update_browser_identity(account_id: str, request: BrowserIdentityUpdateRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return commercial_queue_service.update_browser_identity(account_id, request.model_dump(exclude_unset=True))
    except Exception as exc:
        raise HTTPException(status_code=409, detail={"error_code": getattr(exc, "error_code", "identity_validation_failed"), "error_message": str(exc), "details": getattr(exc, "details", {})}) from exc


@router.post("/browser-identities/{account_id}/validate")
def validate_browser_identity(account_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return commercial_queue_service.validate_browser_identity(account_id)


@router.post("/browser-identities/migrate-existing")
def migrate_browser_identities(request: BrowserIdentityMigrationRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return commercial_queue_service.migrate_browser_identities(dry_run=request.dry_run)


@router.get("/accounts/health")
def list_account_health(response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return commercial_queue_service.list_account_health()


@router.get("/accounts/{account_id}/health")
def get_account_health(account_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return commercial_queue_service.get_account_health(account_id)


@router.post("/accounts/{account_id}/health/require-review")
def require_account_review(account_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return commercial_queue_service.set_account_health(account_id, "manual_review", "manual_review_requested")


@router.post("/accounts/{account_id}/health/reset-warning")
def reset_account_warning(account_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return commercial_queue_service.set_account_health(account_id, "healthy", None)


@router.post("/accounts/{account_id}/health/enable")
def enable_account_health(account_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return commercial_queue_service.set_account_health(account_id, "healthy", None)


@router.post("/accounts/{account_id}/health/disable")
def disable_account_health(account_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return commercial_queue_service.set_account_health(account_id, "disabled", "disabled_by_user")


@router.get("/accounts/settings")
def list_account_settings(response: Response, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return commercial_queue_service.list_account_settings(limit=limit, offset=offset)


@router.get("/accounts/{account_id}/settings")
def get_account_settings(account_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    settings = commercial_queue_service.get_account_settings(account_id)
    return {**settings, "effective": commercial_queue_service.resolve_account_settings(account_id)}


@router.put("/accounts/{account_id}/settings")
def update_account_settings(account_id: str, request: AccountSettingsRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    settings = commercial_queue_service.update_account_settings(account_id, request.model_dump(exclude_unset=True))
    return {**settings, "effective": commercial_queue_service.resolve_account_settings(account_id)}


@router.post("/accounts/settings/apply-global")
def apply_global_account_settings(response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return commercial_queue_service.apply_global_defaults()


@router.post("/campaigns")
def create_commercial_campaign(request: CommercialCampaignRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return commercial_queue_service.create_campaign(request.model_dump(exclude_none=True))


@router.get("/campaigns")
def list_commercial_campaigns(
    response: Response,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    include_internal: bool = False,
) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return commercial_queue_service.list_campaigns(
        status=status,
        limit=limit,
        offset=offset,
        include_internal=include_internal,
    )


@router.get("/campaigns/{campaign_id}")
def get_commercial_campaign(campaign_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    campaign = commercial_queue_service.get_campaign(campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail=f"Campaign not found: {campaign_id}")
    return campaign


@router.get("/campaigns/{campaign_id}/capacity")
def get_commercial_campaign_capacity(campaign_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return commercial_queue_service.campaign_capacity_state(campaign_id)
    except Exception as exc:
        raise _campaign_lifecycle_error(exc) from exc


@router.put("/campaigns/{campaign_id}/capacity")
def allocate_commercial_campaign_capacity(campaign_id: str, request: CampaignCapacityRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return commercial_queue_service.allocate_campaign_capacity(campaign_id, request.model_dump())
    except Exception as exc:
        raise _campaign_lifecycle_error(exc) from exc


@router.patch("/campaigns/{campaign_id}")
def update_commercial_campaign(campaign_id: str, request: CommercialCampaignPatchRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    campaign = commercial_queue_service.update_campaign(campaign_id, request.model_dump(exclude_unset=True))
    if campaign is None:
        raise HTTPException(status_code=404, detail=f"Campaign not found: {campaign_id}")
    return campaign


@router.delete("/campaigns/{campaign_id}")
def delete_commercial_campaign(campaign_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return commercial_queue_service.delete_campaign(campaign_id)
    except KeyError:
        raise HTTPException(status_code=404, detail={"error_code": "campaign_not_found", "error_message": f"Campaign not found: {campaign_id}"}) from None
    except Exception as exc:
        raise _campaign_lifecycle_error(exc) from exc


@router.get("/campaigns/{campaign_id}/configuration")
def get_campaign_configuration(campaign_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        revision = commercial_queue_service.repository.get_latest_configuration_revision(campaign_id)
        effective = commercial_queue_service.resolve_campaign_configuration(campaign_id)
        return {"latest_revision": revision, "effective": effective}
    except Exception as exc:
        raise _campaign_lifecycle_error(exc) from exc


@router.put("/campaigns/{campaign_id}/configuration/draft")
def put_campaign_configuration_draft(campaign_id: str, request: CampaignConfigurationDraftRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        revision = commercial_queue_service.create_or_update_campaign_configuration_draft(
            campaign_id,
            request.configuration,
            created_by=request.created_by,
            change_summary=request.change_summary,
        )
        return {"revision": revision, "effective": commercial_queue_service.resolve_campaign_configuration(campaign_id)}
    except Exception as exc:
        raise _campaign_lifecycle_error(exc) from exc


@router.post("/campaigns/{campaign_id}/configuration/validate")
def validate_campaign_configuration(campaign_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return commercial_queue_service.validate_campaign_configuration(campaign_id)
    except Exception as exc:
        raise _campaign_lifecycle_error(exc) from exc


@router.get("/campaigns/{campaign_id}/configuration/effective")
def get_campaign_effective_configuration(campaign_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return commercial_queue_service.resolve_campaign_configuration(campaign_id)
    except Exception as exc:
        raise _campaign_lifecycle_error(exc) from exc


@router.get("/campaigns/{campaign_id}/configuration/origin-trace")
def get_campaign_configuration_origin_trace(campaign_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        effective = commercial_queue_service.resolve_campaign_configuration(campaign_id)
        return {"campaign_id": campaign_id, "origin_trace": effective["origin_trace"]}
    except Exception as exc:
        raise _campaign_lifecycle_error(exc) from exc


@router.post("/campaigns/{campaign_id}/configuration/revisions")
def create_campaign_configuration_revision(campaign_id: str, request: CampaignConfigurationRevisionRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        revision = commercial_queue_service.create_or_update_campaign_configuration_draft(
            campaign_id,
            request.configuration,
            created_by=request.created_by,
            change_summary=request.change_summary,
        )
        return {"revision": revision}
    except Exception as exc:
        raise _campaign_lifecycle_error(exc) from exc


@router.get("/campaigns/{campaign_id}/configuration/revisions")
def list_campaign_configuration_revisions(campaign_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return {"items": commercial_queue_service.repository.list_configuration_revisions(campaign_id)}


@router.get("/campaigns/{campaign_id}/configuration/revisions/{revision_id}")
def get_campaign_configuration_revision(campaign_id: str, revision_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    revision = commercial_queue_service.repository.get_configuration_revision(revision_id)
    if revision is None or revision.get("campaign_id") != campaign_id:
        raise HTTPException(status_code=404, detail=f"Configuration revision not found: {revision_id}")
    return revision


@router.post("/campaigns/{campaign_id}/configuration/revisions/{revision_id}/approve")
def approve_campaign_configuration_revision(campaign_id: str, revision_id: str, request: CampaignConfigurationApproveRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        approved = commercial_queue_service.approve_campaign_configuration_revision(campaign_id, revision_id, approved_by=request.approved_by)
        snapshot = commercial_queue_service.create_execution_configuration_snapshot(
            campaign_id,
            revision_id=revision_id,
            approval_id=request.approval_id,
            created_by=request.approved_by,
        )
        return {**approved, **snapshot}
    except Exception as exc:
        raise _campaign_lifecycle_error(exc) from exc


@router.post("/campaigns/{campaign_id}/configuration/snapshot")
def create_campaign_configuration_snapshot(campaign_id: str, request: CampaignConfigurationSnapshotRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return commercial_queue_service.create_execution_configuration_snapshot(
            campaign_id,
            revision_id=request.revision_id,
            approval_id=request.approval_id,
            created_by=request.created_by,
        )
    except Exception as exc:
        raise _campaign_lifecycle_error(exc) from exc


@router.post("/campaigns/{campaign_id}/configuration/check-drift")
def check_campaign_configuration_drift(campaign_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return commercial_queue_service.check_campaign_configuration_drift(campaign_id)
    except Exception as exc:
        raise _campaign_lifecycle_error(exc) from exc


def _campaign_lifecycle_error(exc: Exception) -> HTTPException:
    error_code = getattr(exc, "error_code", None) or "invalid_campaign_transition"
    status = 404 if error_code == "campaign_not_found" else 409
    validation = getattr(exc, "summary", {})
    return HTTPException(
        status_code=status,
        detail={
            "error_code": error_code,
            "error_message": str(exc),
            "validation": validation,
            "accounts": validation.get("accounts", []),
        },
        headers={"Access-Control-Allow-Origin": "*"},
    )


@router.post("/campaigns/{campaign_id}/validate-start")
def validate_commercial_campaign_start(campaign_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return commercial_queue_service.validate_campaign_start(campaign_id)
    except Exception as exc:
        raise _campaign_lifecycle_error(exc) from exc


@router.post("/campaigns/{campaign_id}/queue")
def queue_commercial_campaign(campaign_id: str, request: CampaignQueueRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return commercial_queue_service.queue_campaign(campaign_id, request.model_dump())
    except Exception as exc:
        raise _campaign_lifecycle_error(exc) from exc


@router.post("/campaigns/{campaign_id}/start")
def start_commercial_campaign(campaign_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return commercial_queue_service.start_campaign(campaign_id)
    except Exception as exc:
        raise _campaign_lifecycle_error(exc) from exc


@router.post("/campaigns/{campaign_id}/pause")
def pause_commercial_campaign(campaign_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return commercial_queue_service.pause_campaign(campaign_id)
    except Exception as exc:
        raise _campaign_lifecycle_error(exc) from exc


@router.post("/campaigns/{campaign_id}/resume")
def resume_commercial_campaign(campaign_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return commercial_queue_service.resume_campaign(campaign_id)
    except Exception as exc:
        raise _campaign_lifecycle_error(exc) from exc


@router.post("/campaigns/{campaign_id}/cancel")
def cancel_commercial_campaign(campaign_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return commercial_queue_service.cancel_campaign(campaign_id)
    except Exception as exc:
        raise _campaign_lifecycle_error(exc) from exc


@router.post("/campaigns/{campaign_id}/recipients/preview")
def preview_campaign_recipients(campaign_id: str, request: RecipientPreviewRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return commercial_queue_service.preview_campaign_recipients(campaign_id, request.phones, source_type=request.source_type)
    except Exception as exc:
        raise _campaign_lifecycle_error(exc) from exc


@router.post("/campaigns/{campaign_id}/recipients/confirm")
def confirm_campaign_recipients(campaign_id: str, request: RecipientConfirmRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return commercial_queue_service.confirm_recipient_manifest(
            campaign_id,
            request.phones,
            submitted_by=request.submitted_by,
            source_type=request.source_type,
            confirmation_checked=request.confirmation_checked,
        )
    except Exception as exc:
        raise _campaign_lifecycle_error(exc) from exc


@router.post("/campaigns/{campaign_id}/materialize")
def materialize_campaign_recipients(campaign_id: str, request: CampaignMaterializeRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return commercial_queue_service.materialize_campaign_recipients(
            campaign_id,
            request.platforms,
            authorize_for_live_execution=request.authorize_for_live_execution,
            authorized_by=request.authorized_by,
            authorization_note=request.authorization_note,
            create_platform_identities=request.create_platform_identities,
        )
    except Exception as exc:
        raise _campaign_lifecycle_error(exc) from exc


@router.post("/campaigns/{campaign_id}/platform-settings")
def configure_campaign_platform_settings(campaign_id: str, request: CampaignPlatformSettingsRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return commercial_queue_service.configure_campaign_platform_settings(
            campaign_id,
            request.platforms,
            request.platform_settings,
            created_by=request.created_by,
        )
    except Exception as exc:
        raise _campaign_lifecycle_error(exc) from exc


@router.post("/campaigns/{campaign_id}/recipient-scenarios")
def start_recipient_scenario(campaign_id: str, request: RecipientScenarioStartRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return commercial_queue_service.start_recipient_scenario(
            campaign_id=campaign_id,
            recipient_id=request.recipient_id,
            platforms=request.platforms,
            global_contact_id=request.global_contact_id,
            correlation_id=request.correlation_id,
            create_delivery_jobs=False,
        )
    except Exception as exc:
        raise _campaign_lifecycle_error(exc) from exc


@router.post("/campaigns/{campaign_id}/recipient-scenarios/start")
def start_campaign_recipient_scenarios(campaign_id: str, request: CampaignScenarioStartRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return commercial_queue_service.start_campaign_recipient_scenarios(
            campaign_id=campaign_id,
            platforms=request.platforms,
            create_delivery_jobs=False,
        )
    except Exception as exc:
        raise _campaign_lifecycle_error(exc) from exc


@router.get("/campaigns/{campaign_id}/recipient-scenarios/report")
def list_recipient_scenario_report(campaign_id: str, response: Response, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return commercial_queue_service.list_recipient_scenario_report(campaign_id, limit=limit, offset=offset)
    except Exception as exc:
        raise _campaign_lifecycle_error(exc) from exc


@router.get("/campaigns/{campaign_id}/recipient-scenarios")
def list_campaign_recipient_scenarios(campaign_id: str, response: Response, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return commercial_queue_service.list_recipient_scenario_report(campaign_id, limit=limit, offset=offset)
    except Exception as exc:
        raise _campaign_lifecycle_error(exc) from exc


@router.get("/campaigns/{campaign_id}/recipient-scenarios/{campaign_recipient_run_id}")
def get_campaign_recipient_scenario(campaign_id: str, campaign_recipient_run_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        scenario = commercial_queue_service.get_recipient_scenario(campaign_recipient_run_id)
        if scenario.get("campaign_id") != campaign_id:
            raise KeyError(campaign_recipient_run_id)
        return scenario
    except Exception as exc:
        raise _campaign_lifecycle_error(exc) from exc


@router.get("/recipient-scenarios/{campaign_recipient_run_id}")
def get_recipient_scenario(campaign_recipient_run_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return commercial_queue_service.get_recipient_scenario(campaign_recipient_run_id)
    except Exception as exc:
        raise _campaign_lifecycle_error(exc) from exc


@router.post("/recipient-scenarios/{campaign_recipient_run_id}/retry")
def retry_recipient_scenario(campaign_recipient_run_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return commercial_queue_service.retry_recipient_scenario(campaign_recipient_run_id)
    except Exception as exc:
        raise _campaign_lifecycle_error(exc) from exc


@router.post("/campaigns/{campaign_id}/recipient-scenarios/{campaign_recipient_run_id}/retry")
def retry_campaign_recipient_scenario_controlled(campaign_id: str, campaign_recipient_run_id: str, request: CampaignScenarioRetryRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return commercial_queue_service.retry_controlled_recipient_scenario(
            campaign_id=campaign_id,
            campaign_recipient_run_id=campaign_recipient_run_id,
            approval_id=request.approval_id,
            final_review_hash=request.final_review_hash,
            execution_snapshot_id=request.execution_snapshot_id,
            idempotency_key=request.idempotency_key,
            requested_by=request.requested_by,
            mode=request.mode,
            selected_platforms=request.platforms,
        )
    except Exception as exc:
        raise _campaign_lifecycle_error(exc) from exc


@router.post("/execution-batches/{batch_id}/cancel")
def cancel_execution_batch_controlled(batch_id: str, request: ExecutionBatchCancelRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return commercial_queue_service.cancel_execution_batch(batch_id, request.reason)
    except Exception as exc:
        raise _campaign_lifecycle_error(exc) from exc


@router.get("/execution-batches/{batch_id}")
def get_execution_batch_report(batch_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return commercial_queue_service.get_execution_batch_report(batch_id)
    except Exception as exc:
        raise _campaign_lifecycle_error(exc) from exc


@router.patch("/platform-runs/{platform_run_id}/outcome")
def update_platform_run_outcome(platform_run_id: str, request: PlatformRunOutcomeRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    raise HTTPException(
        status_code=403,
        detail={
            "error_code": "internal_platform_outcome_update_required",
            "error_message": "Platform outcomes may only be updated by trusted worker/internal service paths.",
        },
    )


@router.post("/campaigns/{campaign_id}/contacts/prepare")
def prepare_campaign_contacts(campaign_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return commercial_queue_service.prepare_campaign_contacts_explicit(campaign_id)


@router.post("/campaigns/{campaign_id}/final-review")
def final_review_campaign(
    campaign_id: str,
    response: Response,
    request: CampaignFinalReviewRequest = CampaignFinalReviewRequest(),
) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return commercial_queue_service.final_review(
            campaign_id,
            explicit_operator_confirmation=request.explicit_operator_confirmation,
            approved_by=request.approved_by,
        )
    except Exception as exc:
        raise _campaign_lifecycle_error(exc) from exc


@router.post("/campaigns/{campaign_id}/request-send-approval")
def request_campaign_send_approval(campaign_id: str, request: CampaignSendApprovalRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return commercial_queue_service.request_send_approval(
            campaign_id,
            final_review_hash=request.final_review_hash,
            requested_by=request.requested_by,
            approval_scope=request.approval_scope,
            explicit_confirmation=request.explicit_confirmation,
        )
    except Exception as exc:
        raise _campaign_lifecycle_error(exc) from exc


@router.post("/campaigns/{campaign_id}/live-preflight")
def campaign_live_preflight(campaign_id: str, request: CampaignLivePreflightRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return commercial_queue_service.live_preflight(campaign_id, approval_id=request.approval_id)
    except Exception as exc:
        raise _campaign_lifecycle_error(exc) from exc


@router.post("/campaigns/{campaign_id}/execute")
def execute_campaign_controlled(campaign_id: str, request: CampaignExecuteRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return commercial_queue_service.request_controlled_execution(
            campaign_id=campaign_id,
            approval_id=request.approval_id,
            final_review_hash=request.final_review_hash,
            execution_snapshot_id=request.execution_snapshot_id,
            idempotency_key=request.idempotency_key,
            requested_by=request.requested_by,
            mode=request.mode,
            selected_platforms=request.platforms,
        )
    except Exception as exc:
        raise _campaign_lifecycle_error(exc) from exc


@router.post("/campaigns/{campaign_id}/validate-live-readiness")
def validate_commercial_live_readiness(campaign_id: str, request: LiveReadinessRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return commercial_queue_service.validate_live_execution_readiness(
            campaign_id,
            requested_account_ids=request.account_ids,
            requested_max_jobs=request.max_jobs,
        )
    except Exception as exc:
        raise _campaign_lifecycle_error(exc) from exc


@router.post("/campaigns/{campaign_id}/live-approvals")
def create_commercial_live_approval(campaign_id: str, request: LiveApprovalCreateRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return commercial_queue_service.create_live_execution_approval(
            campaign_id,
            requested_by=request.requested_by,
            approval_note=request.approval_note,
            requested_account_ids=request.account_ids,
            requested_max_jobs=request.max_jobs,
            expires_in_minutes=request.expires_in_minutes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error_code": str(exc), "error_message": str(exc)}) from exc
    except Exception as exc:
        raise _campaign_lifecycle_error(exc) from exc


@router.get("/live-approvals")
def list_commercial_live_approvals(response: Response, campaign_id: str | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return commercial_queue_service.list_live_execution_approvals(campaign_id=campaign_id, limit=limit, offset=offset)


@router.get("/live-approvals/{approval_id}")
def get_commercial_live_approval(approval_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    approval = commercial_queue_service.get_live_execution_approval(approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail=f"Approval not found: {approval_id}")
    return approval


@router.post("/live-approvals/{approval_id}/approve")
def approve_commercial_live_approval(approval_id: str, request: LiveApprovalApproveRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return commercial_queue_service.approve_live_execution_approval(approval_id, request.approved_by)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Approval not found: {approval_id}") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"error_code": str(exc), "error_message": str(exc)}) from exc


@router.post("/live-approvals/{approval_id}/revoke")
def revoke_commercial_live_approval(approval_id: str, request: LiveApprovalRevokeRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return commercial_queue_service.revoke_live_execution_approval(approval_id, request.reason)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Approval not found: {approval_id}") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"error_code": str(exc), "error_message": str(exc)}) from exc


@router.post("/live-approvals/{approval_id}/execute")
def execute_commercial_live_approval(approval_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        result = commercial_queue_service.execute_approved_live_campaign(approval_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Approval not found: {approval_id}") from None
    if not result.get("executed"):
        validation = result.get("validation") or {}
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "live_execution_gate_blocked",
                "error_message": "Approved live execution did not pass the current safety gate.",
                "validation": validation,
            },
        )
    return result


def _commercial_import_error(exc: Exception) -> HTTPException:
    error_code = getattr(exc, "error_code", None)
    if error_code:
        return HTTPException(status_code=400, detail={"error_code": error_code, "error_message": str(exc), "details": getattr(exc, "details", {})})
    if isinstance(exc, ValueError):
        return HTTPException(status_code=409, detail={"error_code": str(exc), "error_message": str(exc)})
    return HTTPException(status_code=400, detail=str(exc))


@router.post("/campaigns/{campaign_id}/recipient-imports/preview")
def preview_commercial_recipient_paste_import(campaign_id: str, request: RecipientImportPreviewRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return commercial_queue_service.preview_paste_import(campaign_id, request.content, request.import_source)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Campaign not found: {campaign_id}") from None
    except Exception as exc:
        raise _commercial_import_error(exc) from exc


@router.post("/campaigns/{campaign_id}/recipient-imports/upload")
async def upload_commercial_recipient_import(
    campaign_id: str,
    response: Response,
    file: UploadFile = File(...),
    phone_column: str | None = Form(default=None),
    display_name_column: str | None = Form(default=None),
    sheet_name: str | None = Form(default=None),
) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    filename = str(file.filename or "")
    data = await file.read()
    try:
        if filename.lower().endswith(".csv"):
            return commercial_queue_service.preview_csv_import(campaign_id, data, filename, phone_column, display_name_column)
        if filename.lower().endswith(".xlsx"):
            return commercial_queue_service.preview_excel_import(campaign_id, data, filename, sheet_name, phone_column, display_name_column)
        raise ValueError("unsupported_file_type")
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Campaign not found: {campaign_id}") from None
    except Exception as exc:
        raise _commercial_import_error(exc) from exc


@router.get("/recipient-imports/{batch_id}")
def get_commercial_recipient_import(batch_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    batch = commercial_queue_service.get_import_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"Import batch not found: {batch_id}")
    return batch


@router.get("/recipient-imports/{batch_id}/items")
def list_commercial_recipient_import_items(
    batch_id: str,
    response: Response,
    validation_status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return commercial_queue_service.list_import_items(batch_id, validation_status=validation_status, limit=limit, offset=offset)


@router.post("/recipient-imports/{batch_id}/confirm")
def confirm_commercial_recipient_import(batch_id: str, request: RecipientImportConfirmRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return commercial_queue_service.confirm_import_batch(
            batch_id=batch_id,
            include_valid=request.include_valid,
            selected_item_ids=request.selected_item_ids,
            default_priority=request.default_priority,
            scheduled_at=request.scheduled_at,
            authorize_for_live_execution=request.authorize_for_live_execution,
            authorized_by=request.authorized_by,
            authorization_note=request.authorization_note,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Import batch not found: {batch_id}") from None
    except Exception as exc:
        raise _commercial_import_error(exc) from exc


@router.delete("/recipient-imports/{batch_id}")
def delete_commercial_recipient_import(batch_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    result = commercial_queue_service.delete_import_batch(batch_id)
    if not result.get("deleted") and result.get("reason") == "not_found":
        raise HTTPException(status_code=404, detail=f"Import batch not found: {batch_id}")
    if not result.get("deleted"):
        raise HTTPException(status_code=409, detail=result)
    return result


@router.post("/campaigns/{campaign_id}/recipients")
def import_commercial_recipients(campaign_id: str, request: RecipientImportRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return commercial_queue_service.import_recipients(campaign_id, request.phones, request.import_source)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Campaign not found: {campaign_id}") from None


@router.get("/campaigns/{campaign_id}/recipients")
def list_commercial_recipients(
    campaign_id: str,
    response: Response,
    validation_status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return commercial_queue_service.list_recipients(campaign_id, validation_status=validation_status, limit=limit, offset=offset)


@router.get("/recipients/{recipient_id}/authorization")
def get_recipient_authorization(recipient_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return commercial_queue_service.get_recipient_authorization(recipient_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Recipient not found: {recipient_id}") from None


@router.post("/recipients/{recipient_id}/authorize-live")
def authorize_recipient_live(recipient_id: str, request: RecipientLiveAuthorizeRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return commercial_queue_service.authorize_recipient_live(
            recipient_id,
            authorization_note=request.authorization_note,
            authorized_by=request.authorized_by,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Recipient not found: {recipient_id}") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error_code": str(exc), "message": str(exc)}) from exc


@router.post("/recipients/{recipient_id}/revoke-live")
def revoke_recipient_live(recipient_id: str, request: RecipientLiveRevokeRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return commercial_queue_service.revoke_recipient_live(recipient_id, request.reason)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Recipient not found: {recipient_id}") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error_code": str(exc), "message": str(exc)}) from exc


@router.post("/recipient-authorizations/migrate-test-data")
def migrate_recipient_authorizations(request: RecipientAuthorizationMigrationRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return commercial_queue_service.migrate_phase5d_recipient_authorizations(dry_run=request.dry_run)


@router.get("/jobs")
def list_commercial_jobs(
    response: Response,
    status: str | None = None,
    account_id: str | None = None,
    campaign_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return commercial_queue_service.list_jobs(status=status, account_id=account_id, campaign_id=campaign_id, limit=limit, offset=offset)


@router.get("/jobs/{job_id}/events")
def list_commercial_job_events(job_id: str, response: Response, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return commercial_queue_service.list_events(job_id=job_id, limit=limit, offset=offset)


@router.get("/jobs/{job_id}")
def get_commercial_job(job_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    job = commercial_queue_service.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    return job


@router.get("/events")
def list_commercial_events(
    response: Response,
    job_id: str | None = None,
    campaign_id: str | None = None,
    account_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return commercial_queue_service.list_events(job_id=job_id, campaign_id=campaign_id, account_id=account_id, limit=limit, offset=offset)


@router.post("/workers/{account_id}/assign")
def assign_commercial_worker_jobs(account_id: str, request: WorkerAssignRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return commercial_queue_service.assign_jobs(account_id=account_id, campaign_id=request.campaign_id, limit=request.limit)


@router.post("/workers/{account_id}/run-round")
def run_commercial_worker_round(account_id: str, request: WorkerRunRoundRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return commercial_queue_service.run_account_round(
        account_id=account_id,
        campaign_id=request.campaign_id,
        max_jobs=request.max_jobs,
    )


@router.get("/workers/{account_id}/status")
def get_commercial_worker_status(account_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return commercial_queue_service.worker_status(account_id)


@router.post("/workers/{account_id}/release-stale-lock")
def release_commercial_worker_stale_lock(account_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return commercial_queue_service.release_stale_lock(account_id)


@router.post("/jobs/recover-stale")
def recover_commercial_stale_jobs(response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return commercial_queue_service.recover_stale_jobs()


@router.post("/scheduler/start")
def start_commercial_scheduler(response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return commercial_queue_service.scheduler_start()


@router.post("/scheduler/stop")
def stop_commercial_scheduler(response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return commercial_queue_service.scheduler_stop()


@router.post("/scheduler/pause")
def pause_commercial_scheduler(response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return commercial_queue_service.scheduler_pause()


@router.post("/scheduler/resume")
def resume_commercial_scheduler(response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return commercial_queue_service.scheduler_resume()


@router.post("/scheduler/run-once")
def run_commercial_scheduler_once(request: SchedulerRunOnceRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return commercial_queue_service.scheduler_run_once(campaign_id=request.campaign_id)


def _require_isolated_test_worker_faults() -> None:
    """Reject synthetic worker outcomes outside the disposable UI runtime."""
    enabled = (
        os.environ.get("CLINICOS_TEST_MODE") == "1"
        and os.environ.get("CLINICOS_ENABLE_TEST_EXECUTION_MODES") == "1"
        and os.environ.get("CLINICOS_SAFE_TEST_WORKER_BOUNDARY") == "1"
        and os.environ.get("CLINICOS_TEST_FAKE_WORKER_FAULTS") == "1"
    )
    production = (_BACKEND_ROOT / "clinicos.db").resolve()
    resolved = commercial_queue_service.repository.database_path.resolve()
    if not enabled or resolved == production or resolved.name.casefold() == "clinicos.db":
        raise HTTPException(
            status_code=404,
            detail={"error_code": "isolated_test_worker_faults_unavailable"},
        )


@router.post("/test-resilience/campaigns/{campaign_id}/inject-worker-fault")
def inject_isolated_worker_fault(
    campaign_id: str,
    request: TestWorkerFaultRequest,
    response: Response,
) -> dict[str, Any]:
    """Exercise real scheduler/worker recovery without Bale I/O.

    This endpoint has no normal-UI consumer and is deliberately absent from
    production operation.  The rendered UI creates, allocates, queues, and
    starts the campaign; this hook supplies only a deterministic account-local
    worker outcome after that operator-visible transition.
    """
    _set_dashboard_cors_headers(response)
    _require_isolated_test_worker_faults()
    campaign = commercial_queue_service.get_campaign(campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail={"error_code": "campaign_not_found"})
    fault_mode = str(request.fault_mode or "").strip()
    target_account_id = str(request.account_id or "").strip()
    if fault_mode:
        eligible, _ = commercial_queue_service._ordered_eligible_accounts(campaign_id)
        eligible_ids = [str(item.get("account_id") or "") for item in eligible]
        if not target_account_id:
            target_account_id = next((value for value in eligible_ids if value), "")
        if not target_account_id or target_account_id not in eligible_ids:
            raise HTTPException(
                status_code=409,
                detail={
                    "error_code": "test_fault_target_not_eligible",
                    "eligible_account_count": len(eligible_ids),
                },
            )
        try:
            commercial_queue_service.configure_test_worker_fault(target_account_id, fault_mode)
        except Exception as exc:
            raise HTTPException(status_code=409, detail={"error_code": getattr(exc, "error_code", str(exc))}) from exc
    primary_tick = commercial_queue_service.scheduler_run_once(campaign_id=campaign_id)
    replacement_tick = (
        commercial_queue_service.scheduler_run_once(campaign_id=campaign_id)
        if request.run_replacement_tick
        else None
    )
    reconciliation = None
    if fault_mode == "uncertain_after_send":
        failed_jobs = commercial_queue_service.list_jobs(campaign_id=campaign_id, limit=500).get("items", [])
        uncertain_ids = [
            str(job["id"])
            for job in failed_jobs
            if str(job.get("last_error_code") or "") == "confirm_uncertain"
        ]
        # This is the same durable-evidence reconciliation path used by
        # normal recovery.  It must retain an ambiguous post-confirmation job
        # for manual review instead of assigning it to a replacement account.
        reconciliation = commercial_queue_service.repository.recover_failed_jobs_without_send(
            campaign_id,
            uncertain_ids,
        )
    refreshed_campaign = commercial_queue_service.get_campaign(campaign_id)
    jobs = commercial_queue_service.list_jobs(campaign_id=campaign_id, limit=500).get("items", [])
    return {
        "test_only": True,
        "fault_mode": fault_mode or None,
        "fault_account_id": target_account_id or None,
        "primary_tick": primary_tick,
        "replacement_tick": replacement_tick,
        "reconciliation": reconciliation,
        "campaign": refreshed_campaign,
        "scheduler": commercial_queue_service.scheduler_status_snapshot(),
        "account_health": commercial_queue_service.get_account_health(target_account_id) if target_account_id else None,
        "jobs": jobs,
        "worker_lock": commercial_queue_service.repository.get_worker_lock(target_account_id) if target_account_id else None,
    }


@router.post("/test-resilience/campaigns/{campaign_id}/reconcile-uncertain-delivery")
def reconcile_isolated_uncertain_delivery(
    campaign_id: str,
    request: TestUncertainDeliveryReconciliationRequest,
    response: Response,
) -> dict[str, Any]:
    """Inject a fake *verified* provider reconciliation into the real durable path.

    The surrounding endpoint is isolated-test-only.  The service method is
    production-safe reconciliation logic: it requires a durable proof key and
    can transition only `confirm_uncertain` manual-review work to verified
    success, never to a blind retry.
    """
    _set_dashboard_cors_headers(response)
    _require_isolated_test_worker_faults()
    campaign = commercial_queue_service.get_campaign(campaign_id)
    job = commercial_queue_service.repository.get_job(request.job_id)
    if campaign is None or job is None or str(job.get("campaign_id") or "") != campaign_id:
        raise HTTPException(status_code=404, detail={"error_code": "campaign_or_job_not_found"})
    applied = commercial_queue_service.reconcile_uncertain_delivery_result(
        str(job["id"]),
        {
            "outcome": "sent",
            "success": True,
            "delivery_verified": True,
            "verified_forwarded_recipient_count": 1,
            "remote_message_id": f"isolated-fake-reconciliation:{job['id']}",
            "trusted_result_key": f"isolated-fake-reconciliation:{job['id']}",
            "reconciliation_source": "isolated_fake_delivery_boundary",
            "failed_step": "fake_external_delivery_reconciliation",
            "diagnostics": {"fake_delivery_boundary": True, "no_provider_send": True},
        },
    )
    refreshed = commercial_queue_service.get_campaign(campaign_id)
    return {
        "test_only": True,
        "reconciliation": applied,
        "campaign": refreshed,
        "scheduler": commercial_queue_service.scheduler_status_snapshot(),
        "jobs": commercial_queue_service.list_jobs(campaign_id=campaign_id, limit=500).get("items", []),
    }


@router.get("/scheduler/status")
def get_commercial_scheduler_status(response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return commercial_queue_service.scheduler_status_snapshot()


@router.get("/accounts/runtime-status")
def list_commercial_account_runtime_status(
    response: Response,
    worker_status: str | None = None,
    enabled: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return commercial_queue_service.list_account_runtime_status(worker_status=worker_status, enabled=enabled, limit=limit, offset=offset)


@router.get("/dashboard/summary")
def get_commercial_dashboard_summary(response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return commercial_queue_service.dashboard_summary()


@router.get("/platforms")
def list_automation_platforms(response: Response) -> list[dict[str, str | bool]]:
    _set_dashboard_cors_headers(response)
    return registry_list_platforms()


@router.get("/account-registry/summary")
def get_account_registry_summary(response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return account_registry_service.summarize_all_platforms([str(platform["id"]) for platform in registry_list_platforms()])


@router.get("/browser/providers")
def list_browser_providers(response: Response) -> list[dict[str, Any]]:
    _set_dashboard_cors_headers(response)
    return list_providers()


@router.get("/browser/providers/adspower/config")
def get_adspower_config(response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    provider = get_provider("adspower")
    return provider.load_config(masked=True)


@router.put("/browser/providers/adspower/config")
def update_adspower_config(
    request: AdsPowerConfigRequest,
    response: Response,
) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    provider = get_provider("adspower")
    return provider.save_config(request.model_dump())


@router.get("/browser/providers/adspower/health")
def adspower_health(response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return get_provider("adspower").health_check()


@router.get("/account-groups")
def list_account_groups(response: Response) -> list[dict[str, Any]]:
    _set_dashboard_cors_headers(response)
    return account_group_store.list_groups()


@router.post("/account-groups")
def create_account_group(request: AccountGroupRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    if request.max_concurrent < 1:
        raise HTTPException(status_code=422, detail="max_concurrent must be >= 1")
    if request.batch_capacity < 1:
        raise HTTPException(status_code=422, detail="batch_capacity must be >= 1")
    try:
        return account_group_store.create_group(request.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.put("/account-groups/{group_id}")
def update_account_group(group_id: str, request: AccountGroupRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    if request.max_concurrent < 1:
        raise HTTPException(status_code=422, detail="max_concurrent must be >= 1")
    if request.batch_capacity < 1:
        raise HTTPException(status_code=422, detail="batch_capacity must be >= 1")
    try:
        return account_group_store.update_group(group_id, request.model_dump())
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Account group not found: {group_id}") from None


@router.get("/platforms/{platform_id}/account-groups")
def list_platform_account_groups(platform_id: str, response: Response) -> list[dict[str, Any]]:
    _set_dashboard_cors_headers(response)
    _ensure_platform(platform_id)
    return account_group_store.list_platform_groups(platform_id)


@router.get("/bulk/message-sources")
def list_bulk_message_sources(response: Response) -> list[dict[str, Any]]:
    _set_dashboard_cors_headers(response)
    return message_source_store.list_sources()


@router.post("/bulk/message-sources")
def create_bulk_message_source(request: MessageSourceRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return message_source_store.create_source(request.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.put("/bulk/message-sources/{message_source_id}")
def update_bulk_message_source(message_source_id: str, request: MessageSourceRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return message_source_store.update_source(message_source_id, request.model_dump())
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Message source not found: {message_source_id}") from None


@router.get("/bulk/contact-lists")
def list_bulk_contact_lists(response: Response) -> list[dict[str, Any]]:
    _set_dashboard_cors_headers(response)
    return contact_list_store.list_contact_lists()


@router.post("/bulk/contact-lists")
def create_bulk_contact_list(request: ContactListRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return contact_list_store.create_contact_list(request.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.put("/bulk/contact-lists/{contact_list_id}")
def update_bulk_contact_list(contact_list_id: str, request: ContactListRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return contact_list_store.update_contact_list(contact_list_id, request.model_dump())
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Contact list not found: {contact_list_id}") from None


@router.post("/bulk/contact-lists/import")
async def import_bulk_contact_list(
    response: Response,
    file: UploadFile = File(...),
    name: str = Form(...),
    platform_id: str = Form(""),
    campaign_tag: str = Form(""),
    notes: str = Form(""),
) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    content = await file.read()
    try:
        return contact_importer.import_file(
            content=content,
            filename=file.filename or "",
            name=name,
            platform_id=platform_id,
            campaign_tag=campaign_tag,
            notes=notes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/bulk/contact-lists/manual")
def import_manual_bulk_contact_list(request: ManualContactImportRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return contact_importer.import_manual(
        phones_text=request.phones_text,
        name=request.name,
        platform_id=request.platform_id,
        campaign_tag=request.campaign_tag,
        notes=request.notes,
    )


@router.get("/bulk/contact-lists/{contact_list_id}/contacts")
def list_bulk_contacts(contact_list_id: str, response: Response) -> list[dict[str, Any]]:
    _set_dashboard_cors_headers(response)
    return contact_store.list_contacts(contact_list_id)


@router.get("/bulk/contact-lists/{contact_list_id}/summary")
def bulk_contact_list_summary(contact_list_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    metadata = contact_list_store.get_contact_list(contact_list_id)
    summary = contact_store.summary(contact_list_id)
    return {**summary, "metadata": metadata}


@router.get("/bulk/campaigns")
def list_bulk_campaigns(response: Response) -> list[dict[str, Any]]:
    _set_dashboard_cors_headers(response)
    return bulk_campaign_store.list_campaigns()


@router.post("/bulk/campaigns")
def create_bulk_campaign(request: BulkCampaignRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return bulk_campaign_store.create_campaign(request.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.put("/bulk/campaigns/{campaign_id}")
def update_bulk_campaign(campaign_id: str, request: BulkCampaignRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return bulk_campaign_store.update_campaign(campaign_id, request.model_dump())
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Campaign not found: {campaign_id}") from None


@router.post("/bulk/campaigns/{campaign_id}/routes")
def create_bulk_campaign_route(campaign_id: str, request: CampaignRouteRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return bulk_campaign_store.create_route(campaign_id, request.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Campaign not found: {campaign_id}") from None


@router.put("/bulk/campaigns/{campaign_id}/routes/{route_id}")
def update_bulk_campaign_route(campaign_id: str, route_id: str, request: CampaignRouteRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return bulk_campaign_store.update_route(campaign_id, route_id, request.model_dump())
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Campaign or route not found: {campaign_id}/{route_id}") from None


@router.post("/bulk/campaigns/{campaign_id}/plan")
def plan_bulk_campaign(campaign_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return bulk_campaign_planner.build_plan(campaign_id)


@router.post("/bulk/campaigns/{campaign_id}/assign")
def assign_bulk_campaign(campaign_id: str, request: BulkAssignmentRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return assignment_planner.build_assignment_plan(campaign_id, request.model_dump())


@router.get("/bulk/campaigns/{campaign_id}/assignments")
def list_bulk_campaign_assignments(campaign_id: str, response: Response) -> list[dict[str, Any]]:
    _set_dashboard_cors_headers(response)
    return assignment_store.list_assignments(campaign_id)


@router.get("/bulk/campaigns/{campaign_id}/assignments/summary")
def bulk_campaign_assignment_summary(campaign_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return assignment_store.summary(campaign_id)


@router.post("/bulk/campaigns/{campaign_id}/queue")
def create_bulk_campaign_queue(campaign_id: str, request: BulkQueueRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return execution_queue_store.create_from_assignments(
        campaign_id=campaign_id,
        dry_run=request.dry_run,
        planned_for_date=request.planned_for_date or "",
    )


@router.get("/bulk/campaigns/{campaign_id}/queue")
def list_bulk_campaign_queue(campaign_id: str, response: Response) -> list[dict[str, Any]]:
    _set_dashboard_cors_headers(response)
    return execution_queue_store.list_jobs(campaign_id)


@router.get("/bulk/campaigns/{campaign_id}/queue/summary")
def bulk_campaign_queue_summary(campaign_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return execution_queue_store.summary(campaign_id)


@router.post("/bulk/campaigns/{campaign_id}/queue/dry-run")
def dry_run_bulk_campaign_queue(campaign_id: str, request: BulkQueueDryRunRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return execution_queue_store.run_dry_run(campaign_id, request.limit)


@router.post("/bulk/campaigns/{campaign_id}/queue/bale/run")
def run_bale_bulk_campaign_queue(campaign_id: str, request: BaleQueueRunRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return bale_queue_runner.run(campaign_id, request.model_dump())


@router.get("/platforms/{platform_id}/bulk/campaigns")
def list_platform_bulk_campaigns(platform_id: str, response: Response) -> list[dict[str, Any]]:
    _set_dashboard_cors_headers(response)
    _ensure_platform(platform_id)
    return bulk_campaign_store.list_platform_campaigns(platform_id)


@router.get("/platforms/{platform_id}/bulk/message-sources")
def list_platform_bulk_message_sources(platform_id: str, response: Response) -> list[dict[str, Any]]:
    _set_dashboard_cors_headers(response)
    _ensure_platform(platform_id)
    return message_source_store.list_platform_sources(platform_id)


@router.get("/platforms/{platform_id}/bulk/contact-lists")
def list_platform_bulk_contact_lists(platform_id: str, response: Response) -> list[dict[str, Any]]:
    _set_dashboard_cors_headers(response)
    _ensure_platform(platform_id)
    return contact_list_store.list_platform_contact_lists(platform_id)


@router.get("/platforms/{platform_id}/accounts")
def list_platform_accounts(platform_id: str, response: Response) -> list[dict[str, Any]]:
    _set_dashboard_cors_headers(response)
    _ensure_platform(platform_id)
    return account_registry_service.list_platform_accounts(platform_id)


@router.post("/platforms/{platform_id}/accounts")
def create_platform_account(
    platform_id: str,
    request: BaleAccountRequest,
    response: Response,
) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    _ensure_platform(platform_id)
    try:
        return account_registry_service.create_platform_account(platform_id, request.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.put("/platforms/{platform_id}/accounts/{account_id}")
def update_platform_account(
    platform_id: str,
    account_id: str,
    request: BaleAccountUpdateRequest,
    response: Response,
) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    _ensure_platform(platform_id)
    payload = {key: value for key, value in request.model_dump().items() if value is not None}
    try:
        return account_registry_service.update_platform_account(platform_id, account_id, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Account not found: {account_id}") from None


@router.delete("/platforms/{platform_id}/accounts/{account_id}")
async def delete_platform_account(platform_id: str, account_id: str, response: Response, delete_profile: bool = Query(True)) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    _ensure_platform(platform_id)
    # This generic route is registered before the Bale-specific delete route
    # below, so FastAPI resolves Bale deletes here.  Delegate to the durable
    # Bale operation rather than deleting only the legacy registry mirror.
    if platform_id == "bale":
        return await delete_bale_account(account_id, response, delete_profile=delete_profile)
    try:
        return account_registry_service.delete_platform_account(platform_id, account_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Account not found: {account_id}") from None


@router.get("/platforms/{platform_id}/tasks")
def list_platform_tasks(platform_id: str, response: Response) -> list[dict[str, Any]]:
    _set_dashboard_cors_headers(response)
    _ensure_platform(platform_id)
    tasks = [_serialize_task(task) for task in queue.get_all_tasks()]
    return _filter_items_by_platform(tasks, platform_id)


@router.get("/platforms/{platform_id}/logs")
def list_platform_logs(platform_id: str, response: Response) -> list[dict[str, Any]]:
    _set_dashboard_cors_headers(response)
    _ensure_platform(platform_id)
    logs = _filter_items_by_platform(list_logs(response), platform_id)
    if platform_id == "bale":
        logs.extend(bale_plugin.list_logs())
    return logs


@router.get("/platforms/bale/latest-job")
def get_latest_bale_job(response: Response) -> dict[str, Any] | None:
    _set_dashboard_cors_headers(response)
    jobs = [job for job in execution_queue_store.list_jobs() if str(job.get("platform_id") or "").lower() == "bale"]
    if not jobs:
        return None
    latest_job = sorted(jobs, key=lambda job: str(job.get("updated_at") or job.get("created_at") or ""), reverse=True)[0]
    return _bale_job_diagnostics_shape(latest_job)


@router.get("/platforms/bale/jobs")
def list_bale_jobs(response: Response, limit: int = 10) -> list[dict[str, Any]]:
    _set_dashboard_cors_headers(response)
    limit = max(1, min(int(limit or 10), 100))
    jobs = [
        _bale_job_diagnostics_shape(job)
        for job in execution_queue_store.list_jobs()
        if str(job.get("platform_id") or "").lower() == "bale"
    ]
    jobs.sort(key=lambda job: str(job.get("updated_at") or job.get("created_at") or ""), reverse=True)
    return jobs[:limit]


@router.get("/platforms/bale/contacts")
def list_bale_contacts(response: Response, account_id: str, status: str | None = None) -> list[dict[str, Any]]:
    _set_dashboard_cors_headers(response)
    return bale_contact_store.list_bale_contacts(account_id, status=status)


@router.post("/platforms/bale/contacts/bulk")
def bulk_add_bale_contacts(request: BaleContactsBulkRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return bale_contact_store.bulk_add_bale_contacts(request.account_id, request.phones)


@router.post("/platforms/bale/contacts/prepare-authorized")
def prepare_authorized_bale_contacts(request: BalePrepareAuthorizedContactsRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return commercial_queue_service.prepare_authorized_bale_contacts(request.account_id, request.recipient_ids)
    except Exception as exc:
        raise _campaign_lifecycle_error(exc) from exc


@router.get("/platforms/bale/authentication/audit/{account_id}")
def audit_bale_authentication_profile(account_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return commercial_queue_service.audit_bale_authentication_profile(account_id)
    except Exception as exc:
        raise _campaign_lifecycle_error(exc) from exc


# ============================================================
# BLOCK: BALE_AUTHENTICATION_ACCOUNT_EXECUTOR_BOUNDARY
# PURPOSE:
# Dispatches persistent authentication lifecycle calls to account-owned threads.
# ACCOUNT_SCOPE:
# One account executor per active Bale authentication session.
# DEPENDENCIES:
# BaleAccountExecutorRegistry, CommercialQueueService
# LAYER:
# API
# ============================================================

_bale_action_operations: dict[str, dict[str, Any]] = {}
_bale_action_tasks: set[asyncio.Task[Any]] = set()
_bale_action_guard = threading.RLock()
_ASYNC_BALE_ACCOUNT_ACTIONS = {"open_login", "session_recheck", "delete_account", "reset_profile"}
_BALE_ACCOUNT_OPERATION_TERMINAL_STATES = {"succeeded", "failed", "cancelled", "interrupted", "timed_out"}
# Hot-reload marker: account-operation watchdog/recovery code is restart-safe (final acceptance).
# The deadline covers navigation, DOM classification, identity extraction,
# persistence, and cleanup as one operation.  Heartbeats never extend it.
BALE_ACCOUNT_OPERATION_DEADLINE_SECONDS = max(
    15,
    int(os.environ.get("CLINICOS_BALE_ACCOUNT_OPERATION_DEADLINE_SECONDS", "90")),
)


class BaleAccountOperationTimeout(BaleOnboardingError):
    """Absolute operation deadline elapsed before decisive auth evidence."""

    def __init__(self, stage: str, message: str = "Bale account authentication exceeded its absolute deadline") -> None:
        super().__init__("authentication_operation_timeout", message, {
            "terminal_reason": "bale_bootstrap_unresolved",
            "failed_step": stage,
        })
        self.stage = stage


def _persist_bale_action_operation(operation: dict[str, Any]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    operation.setdefault("owner_runtime_id", getattr(bale_onboarding_service, "backend_instance_id", "unknown_runtime"))
    operation.setdefault("owner_pid", os.getpid())
    operation["heartbeat_at"] = now
    with bale_onboarding_service.connection() as connection:
        generation = connection.execute(
            "SELECT profile_generation_id FROM bale_operational_accounts WHERE account_id=?",
            (operation["account_id"],),
        ).fetchone()
        if not operation.get("profile_generation_id"):
            operation["profile_generation_id"] = str(generation["profile_generation_id"] or "") if generation else None
        connection.execute(
            """INSERT INTO bale_onboarding_operations
            (operation_id,idempotency_key,request_hash,operation_type,account_id,onboarding_batch_id,status,current_step,progress_json,error_code,safe_error_message,created_at,updated_at,
             owner_runtime_id,owner_pid,profile_generation_id,heartbeat_at,lease_released_at,deadline_at,terminal_reason)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(operation_id) DO UPDATE SET status=excluded.status,current_step=excluded.current_step,
            progress_json=excluded.progress_json,error_code=excluded.error_code,safe_error_message=excluded.safe_error_message,updated_at=excluded.updated_at,
            owner_runtime_id=excluded.owner_runtime_id,owner_pid=excluded.owner_pid,
            profile_generation_id=excluded.profile_generation_id,heartbeat_at=excluded.heartbeat_at,
            lease_released_at=excluded.lease_released_at,deadline_at=excluded.deadline_at,
            terminal_reason=excluded.terminal_reason""",
            (
                operation["operation_id"], operation["operation_id"], operation["correlation_id"], operation["action"],
                operation["account_id"], None, operation["status"], operation["stage"], json.dumps(operation, ensure_ascii=False, default=str),
                operation.get("error_code"), operation.get("error_message"), operation["started_at"], now,
                operation.get("owner_runtime_id"), operation.get("owner_pid"), operation.get("profile_generation_id"),
                operation.get("heartbeat_at"), operation.get("lease_released_at"), operation.get("deadline_at"),
                operation.get("terminal_reason"),
            ),
        )
        connection.commit()


def _load_persisted_bale_action_operation(operation_id: str) -> dict[str, Any] | None:
    with bale_onboarding_service.connection() as connection:
        row = connection.execute("SELECT progress_json FROM bale_onboarding_operations WHERE operation_id=?", (operation_id,)).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row["progress_json"] or "{}")
        return payload if isinstance(payload, dict) else None
    except (TypeError, ValueError):
        return None


def reconcile_orphaned_bale_account_action_operations() -> dict[str, Any]:
    """Recover queued/running account actions at backend startup.

    Recovery is deterministic and metadata-only: an expired operation is
    timed out even when its owner still heartbeats, while an operation whose
    owner process disappeared is cancelled.  Durable account identity/session
    evidence is intentionally untouched.
    """
    now = datetime.now(timezone.utc).isoformat()
    terminalized: list[str] = []
    timed_out: list[str] = []
    interrupted: list[str] = []
    leases_released: list[str] = []
    with _bale_action_guard:
        active_operation_ids = {
            operation_id
            for operation_id, operation in _bale_action_operations.items()
            if operation.get("status") in {"queued", "running"}
        }
        with bale_onboarding_service.connection() as connection:
            placeholders = ",".join("?" for _ in _ASYNC_BALE_ACCOUNT_ACTIONS)
            rows = connection.execute(
                f"""SELECT operation_id, operation_type, account_id, current_step, progress_json,
                    status, created_at, updated_at, deadline_at, owner_pid
                FROM bale_onboarding_operations
                WHERE operation_id LIKE 'baleop_%'
                  AND operation_type IN ({placeholders})
                  AND status IN ('queued', 'running')""",
                tuple(sorted(_ASYNC_BALE_ACCOUNT_ACTIONS)),
            ).fetchall()
            for row in rows:
                operation_id = str(row["operation_id"])
                try:
                    operation = json.loads(row["progress_json"] or "{}")
                except (TypeError, ValueError):
                    operation = {}
                if not isinstance(operation, dict):
                    operation = {}
                previous_stage = str(operation.get("stage") or row["current_step"] or "queued")
                deadline_at = str(operation.get("deadline_at") or row["deadline_at"] or "").strip()
                try:
                    deadline = datetime.fromisoformat(deadline_at.replace("Z", "+00:00"))
                    if deadline.tzinfo is None:
                        deadline = deadline.replace(tzinfo=timezone.utc)
                except (TypeError, ValueError):
                    try:
                        started = datetime.fromisoformat(str(operation.get("started_at") or row["created_at"]).replace("Z", "+00:00"))
                        if started.tzinfo is None:
                            started = started.replace(tzinfo=timezone.utc)
                    except (TypeError, ValueError):
                        started = datetime.now(timezone.utc)
                    deadline = started + timedelta(seconds=BALE_ACCOUNT_OPERATION_DEADLINE_SECONDS)
                    deadline_at = deadline.isoformat()
                owner_alive = False
                try:
                    owner_alive = bool(bale_onboarding_service._owner_process_alive(row["owner_pid"]))
                except Exception:
                    owner_alive = False
                # The process-local task registry is the authoritative proof
                # that this backend is actively executing the operation.  A
                # live PID alone is insufficient (it may be a reused PID or a
                # task that already died), so an operation absent from the
                # registry is recoverable on startup.
                if deadline > datetime.now(timezone.utc) and operation_id in active_operation_ids:
                    # Persist a deadline for legacy rows, but do not interrupt a
                    # genuinely live operation merely because the server is
                    # starting.  Its absolute deadline remains authoritative.
                    if not row["deadline_at"]:
                        operation["deadline_at"] = deadline_at
                        connection.execute(
                            "UPDATE bale_onboarding_operations SET deadline_at=?, progress_json=?, updated_at=? WHERE operation_id=?",
                            (deadline_at, json.dumps(operation, ensure_ascii=False, default=str), now, operation_id),
                        )
                    continue
                terminal_status = "timed_out" if deadline <= datetime.now(timezone.utc) else "cancelled"
                terminal_reason = "bale_bootstrap_unresolved" if terminal_status == "timed_out" else "account_operation_interrupted_by_restart"
                if terminal_status == "timed_out":
                    timed_out.append(operation_id)
                else:
                    interrupted.append(operation_id)
                operation.update({
                    "operation_id": operation_id,
                    "action": str(operation.get("action") or row["operation_type"]),
                    "account_id": str(operation.get("account_id") or row["account_id"] or ""),
                    "status": terminal_status,
                    "stage": terminal_status,
                    "previous_stage": previous_stage,
                    "success": False,
                    "completed_at": now,
                    "error_code": "authentication_operation_timeout" if terminal_status == "timed_out" else "account_operation_interrupted_by_restart",
                    "error_message": "Bale bootstrap/authentication exceeded its absolute deadline." if terminal_status == "timed_out" else "The previous server process stopped before this account operation completed.",
                    "terminal_reason": terminal_reason,
                    "deadline_at": deadline_at,
                    "failed_step": previous_stage,
                    "cleanup_result": {
                        "restart_reconciliation": "operation_terminalized_without_resuming_browser_or_delete",
                        "resources_released": False,
                    },
                    "lock_release_result": {
                        "release_attempted": False,
                        "reason": "startup_releases_only_proven_stale_resources",
                    },
                    "restart_reconciliation": {
                        "reconciled_at": now,
                        "reason": "in_memory_operation_owner_missing" if terminal_status == "cancelled" else terminal_reason,
                    },
                })
                connection.execute(
                    """UPDATE bale_onboarding_operations
                    SET status=?, current_step=?, progress_json=?, error_code=?,
                        safe_error_message=?, updated_at=?, deadline_at=?, terminal_reason=?, lease_released_at=? WHERE operation_id=?""",
                    (
                        terminal_status,
                        terminal_status,
                        json.dumps(operation, ensure_ascii=False, default=str),
                        operation["error_code"],
                        operation["error_message"],
                        now,
                        deadline_at,
                        terminal_reason,
                        now,
                        operation_id,
                    ),
                )
                if operation_id in _bale_action_operations:
                    _bale_action_operations[operation_id].update(operation)
                terminalized.append(operation_id)
            connection.commit()
        for operation_id in terminalized:
            operation = _bale_action_operations.get(operation_id) or {}
            account_id = str(operation.get("account_id") or "")
            if not account_id:
                with bale_onboarding_service.connection() as lookup:
                    row = lookup.execute("SELECT account_id FROM bale_onboarding_operations WHERE operation_id=?", (operation_id,)).fetchone()
                    account_id = str(row["account_id"] or "") if row else ""
            release = getattr(bale_onboarding_service, "release_profile_launch_lock_for_operation", None)
            if account_id and callable(release):
                try:
                    if release(account_id, operation_id):
                        leases_released.append(operation_id)
                except Exception:
                    logger.exception("[BALE_AUTH_RECOVERY] exact lease release failed operation_id=%s", operation_id)
    return {
        "checked_count": len(rows),
        "cancelled_count": len(interrupted),
        "cancelled_operation_ids": interrupted,
        "timed_out_count": len(timed_out),
        "timed_out_operation_ids": timed_out,
        "terminalized_operation_ids": terminalized,
        "resumed_operations": 0,
        "resources_released": len(leases_released),
        "released_operation_ids": leases_released,
    }


def _new_bale_action_operation(account_id: str, action: str, endpoint: str, method: str) -> tuple[dict[str, Any], bool]:
    with _bale_action_guard:
        # A browser profile is account-scoped.  Coalesce *all* live account
        # actions, rather than just repeated copies of the same action, so a
        # Session Recheck/Delete click cannot race an in-flight Open/Login on
        # the same profile generation.  Returning the active operation lets
        # the caller poll one deterministic terminal result.
        existing = next((item for item in _bale_action_operations.values() if item["account_id"] == account_id and item["status"] in {"queued", "running"}), None)
        if existing:
            coalesced_request = {
                "action": action,
                "endpoint": endpoint,
                "http_method": method,
                "requested_at": datetime.now(timezone.utc).isoformat(),
            }
            existing["coalesced_requests"] = [
                *(existing.get("coalesced_requests") or []),
                coalesced_request,
            ]
            existing["last_coalesced_request"] = coalesced_request
            _persist_bale_action_operation(existing)
            return dict(existing), False
        now = datetime.now(timezone.utc).isoformat()
        deadline_at = (datetime.now(timezone.utc) + timedelta(seconds=BALE_ACCOUNT_OPERATION_DEADLINE_SECONDS)).isoformat()
        operation = {
            "operation_id": f"baleop_{uuid4().hex}", "correlation_id": f"corr_{uuid4().hex}",
            "entity_type": "bale_account", "entity_id": account_id, "account_id": account_id,
            "campaign_id": None, "action": action, "endpoint": endpoint, "http_method": method,
            "stage": "queued", "previous_stage": None, "started_at": now, "completed_at": None,
            "duration_ms": None, "http_status": 202, "success": None, "status": "queued",
            "error_code": None, "error_message": None, "last_successful_step": "operation_created",
            "failed_step": None, "nested_error": None, "cleanup_result": None,
            "lock_release_result": None, "result": None,
            "coalesced_requests": [], "last_coalesced_request": None,
            "owner_runtime_id": getattr(bale_onboarding_service, "backend_instance_id", "unknown_runtime"),
            "owner_pid": os.getpid(), "profile_generation_id": None, "heartbeat_at": now,
            "lease_released_at": None, "deadline_at": deadline_at, "terminal_reason": None,
        }
        _bale_action_operations[operation["operation_id"]] = operation
        _persist_bale_action_operation(operation)
        return dict(operation), True


def _update_bale_action_operation(operation_id: str, **changes: Any) -> dict[str, Any]:
    with _bale_action_guard:
        operation = _bale_action_operations[operation_id]
        # A watchdog may terminalize an operation while its browser thread is
        # unwinding.  Late progress/success from that stale worker must never
        # overwrite the durable terminal state.
        if operation.get("status") in _BALE_ACCOUNT_OPERATION_TERMINAL_STATES:
            requested_status = changes.get("status")
            if requested_status is None or requested_status != operation.get("status"):
                return dict(operation)
            if requested_status in _BALE_ACCOUNT_OPERATION_TERMINAL_STATES:
                return dict(operation)
        if "stage" in changes:
            changes.setdefault("previous_stage", operation.get("stage"))
        operation.update(changes)
        _persist_bale_action_operation(operation)
        return dict(operation)


def _account_operation_exception_payload(exc: Exception) -> dict[str, Any]:
    """Return a diagnostic-safe account-operation exception description.

    Account authentication is an onboarding concern.  It must not depend on the
    campaign lifecycle exception type (and, importantly, must not lose the
    originating error when terminal state is persisted).
    """
    details = getattr(exc, "details", None)
    if not isinstance(details, dict):
        details = getattr(exc, "summary", None)
    if not isinstance(details, dict):
        details = {}
    frames = traceback.extract_tb(exc.__traceback__)[-8:] if exc.__traceback__ else []
    return {
        "exception_type": type(exc).__name__,
        "details": details,
        "traceback": [
            {"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
            for frame in frames
        ],
    }


def _operation_deadline(operation: dict[str, Any]) -> tuple[str, float]:
    """Return the durable ISO deadline and its monotonic remaining clock."""
    raw = str(operation.get("deadline_at") or "").strip()
    try:
        deadline = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        deadline = datetime.now(timezone.utc) + timedelta(seconds=BALE_ACCOUNT_OPERATION_DEADLINE_SECONDS)
        raw = deadline.isoformat()
    return raw, max(0.0, (deadline - datetime.now(timezone.utc)).total_seconds())


def _consume_background_task(task: asyncio.Task[Any]) -> None:
    """Consume late browser-task exceptions after watchdog terminalization."""
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("[BALE_AUTH_WATCHDOG] late account browser task failed")


def _terminalize_bale_auth_timeout(
    operation_id: str,
    account_id: str,
    *,
    started: float,
    failed_step: str,
    result: dict[str, Any] | None = None,
    reason: str = "bale_bootstrap_unresolved",
) -> None:
    """Persist a deterministic timeout and fence only this operation's lease."""
    now = datetime.now(timezone.utc).isoformat()
    operation = _bale_action_operations.get(operation_id)
    if operation is None:
        operation = _load_persisted_bale_action_operation(operation_id)
        if operation is None:
            return
        with _bale_action_guard:
            _bale_action_operations[operation_id] = operation
    if operation.get("status") in _BALE_ACCOUNT_OPERATION_TERMINAL_STATES:
        return
    lease_released = False
    try:
        release = getattr(bale_onboarding_service, "release_profile_launch_lock_for_operation", None)
        if callable(release):
            lease_released = bool(release(account_id, operation_id))
    except Exception:
        logger.exception("[BALE_AUTH_WATCHDOG] exact lease release failed operation_id=%s", operation_id)
    _update_bale_action_operation(
        operation_id,
        status="timed_out",
        stage="timed_out",
        success=False,
        http_status=409,
        completed_at=now,
        duration_ms=round((time.perf_counter() - started) * 1000, 3),
        error_code="authentication_operation_timeout",
        error_message="Bale bootstrap/authentication did not reach decisive evidence before the absolute deadline.",
        terminal_reason=reason,
        failed_step=failed_step,
        result={"last_authentication_result": result or {}},
        cleanup_result={"browser_close_attempted": False, "timeout_watchdog": True},
        lock_release_result={"released": lease_released, "operation_id_fenced": True},
        lease_released_at=now if lease_released else None,
    )


async def _await_bale_browser_call(awaitable: Any, deadline_at: str, stage: str) -> Any:
    """Await account-thread work without allowing a stuck Playwright call to
    consume the entire event loop operation forever.

    The underlying thread future is deliberately shielded: Python cannot
    safely kill a thread running Playwright.  The caller receives a timeout,
    terminalizes the durable operation, and the late task is fenced from
    overwriting that terminal result.
    """
    _, remaining = _operation_deadline({"deadline_at": deadline_at})
    if remaining <= 0:
        raise BaleAccountOperationTimeout(stage)
    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
    except asyncio.TimeoutError as exc:
        task.add_done_callback(_consume_background_task)
        raise BaleAccountOperationTimeout(stage) from exc


async def _run_auth_action_body(operation_id: str, account_id: str, purpose: str, deadline_at: str, started: float) -> None:
    session_id = ""
    lease_owner_id = ""
    before_state: dict[str, Any] | None = None
    result: dict[str, Any] = {}
    failed_step = "load_before_persisted_account_state"
    try:
        before_state = next((row for row in commercial_queue_service.canonical_bale_account_states(include_soft_deleted=True) if row["account_id"] == account_id), None)
        _update_bale_action_operation(operation_id, status="running", stage="opening_profile", before=before_state)
        failed_step = "open_canonical_profile"
        _, remaining = _operation_deadline({"deadline_at": deadline_at})
        if remaining <= 0:
            raise BaleAccountOperationTimeout(failed_step)
        opened = await _await_bale_browser_call(
            bale_account_executor_registry.execute_open(
                account_id,
                _open_bale_authentication_sync,
                account_id,
                purpose,
                operation_id,
            ),
            deadline_at,
            failed_step,
        )
        session_id = str(opened.get("maintenance_session_id") or "")
        lease_owner_id = str(opened.get("profile_lease_owner_id") or "")
        result = opened
        failed_step = "wait_for_authenticated_identity"
        while not result.get("verified"):
            _, remaining = _operation_deadline({"deadline_at": deadline_at})
            if remaining <= 0:
                raise BaleAccountOperationTimeout(failed_step)
            auth = result.get("auth") or {}
            auth_state = str(auth.get("auth_state") or result.get("state") or "")
            if purpose == "session_recheck" and auth_state in {"unauthenticated", "login_required", "verification_code_required", "otp_required"}:
                raise BaleOnboardingError("authentication_required", "Current Bale Login/OTP screen is visible", {"auth_state": auth_state})
            if result.get("terminal") and not result.get("verified"):
                raise BaleOnboardingError(str(result.get("error_code") or auth_state or "authentication_failed"), str(result.get("error_message") or "Authentication operation failed"), result)
            await asyncio.sleep(min(1.0, remaining))
            _, remaining = _operation_deadline({"deadline_at": deadline_at})
            if remaining <= 0:
                raise BaleAccountOperationTimeout(failed_step)
            if lease_owner_id:
                renew_lease = getattr(bale_onboarding_service, "renew_profile_launch_lock", None)
                if callable(renew_lease):
                    renew_lease(account_id, lease_owner_id, operation_id=operation_id)
            result = await _await_bale_browser_call(
                bale_account_executor_registry.execute_session_operation(session_id, "status", _get_bale_authentication_status_sync, session_id),
                deadline_at,
                failed_step,
            )
            _update_bale_action_operation(operation_id, stage="waiting_for_authenticated_identity", last_successful_step="authentication_status_checked", result=result)
        if not result.get("verified"):
            raise BaleOnboardingError("authentication_operation_timeout", "Authentication did not reach a terminal verified state", result)
        if session_id and not result.get("maintenance_released"):
            closed = await _await_bale_browser_call(
                bale_account_executor_registry.execute_session_operation(
                    session_id,
                    "close",
                    _close_bale_authentication_sync,
                    session_id,
                ),
                deadline_at,
                "close_authentication_session",
            )
            result = {**result, "maintenance_released": bool(closed.get("closed") or closed.get("already_closed"))}
        failed_step = "load_after_persisted_account_state"
        final_state = next((row for row in commercial_queue_service.canonical_bale_account_states(include_soft_deleted=True) if row["account_id"] == account_id), None)
        _update_bale_action_operation(
            operation_id, status="succeeded", stage="succeeded", success=True, http_status=200,
            completed_at=datetime.now(timezone.utc).isoformat(), duration_ms=round((time.perf_counter() - started) * 1000, 3),
            last_successful_step="final_persisted_account_loaded", result={**result, "before_persisted_account_state": before_state, "after_persisted_account_state": final_state, "final_persisted_account_state": final_state},
            cleanup_result={"browser_closed": bool(result.get("maintenance_released"))}, lock_release_result={"released": True},
            lease_released_at=datetime.now(timezone.utc).isoformat(),
        )
    except BaleAccountOperationTimeout as exc:
        _terminalize_bale_auth_timeout(
            operation_id,
            account_id,
            started=started,
            failed_step=getattr(exc, "stage", failed_step),
            result=result,
        )
    except Exception as exc:
        exception_payload = _account_operation_exception_payload(exc)
        if session_id:
            try:
                await bale_account_executor_registry.execute_session_operation(session_id, "close", _close_bale_authentication_sync, session_id)
            except Exception as cleanup_exc:
                exception_payload["cleanup_error"] = _account_operation_exception_payload(cleanup_exc)
        _update_bale_action_operation(
            operation_id, status="failed", stage="failed", success=False, http_status=409,
            completed_at=datetime.now(timezone.utc).isoformat(), duration_ms=round((time.perf_counter() - started) * 1000, 3),
            error_code=str(getattr(exc, "error_code", type(exc).__name__)), error_message=str(exc),
            failed_step=failed_step, nested_error=exception_payload,
            result={"last_authentication_result": result, "before_persisted_account_state": before_state},
            cleanup_result={"browser_close_attempted": bool(session_id), "cleanup_error": exception_payload.get("cleanup_error")},
            lock_release_result={"release_attempted": True},
            lease_released_at=datetime.now(timezone.utc).isoformat() if session_id else None,
        )


async def _run_auth_action(operation_id: str, account_id: str, purpose: str) -> None:
    """Run one account action with an independent absolute watchdog."""
    started = time.perf_counter()
    operation = _bale_action_operations.get(operation_id) or _load_persisted_bale_action_operation(operation_id) or {}
    deadline_at, remaining = _operation_deadline(operation)
    if not operation.get("deadline_at"):
        _update_bale_action_operation(operation_id, deadline_at=deadline_at)
    browser_task = asyncio.create_task(
        _run_auth_action_body(operation_id, account_id, purpose, deadline_at, started),
        name=f"bale-auth-body-{operation_id}",
    )
    # Let the body enter its bounded executor call before arming the watchdog;
    # otherwise a coroutine object could be created and cancelled before it is
    # scheduled, producing an un-awaited warning during rapid timeout tests.
    await asyncio.sleep(0)
    watchdog_task = asyncio.create_task(asyncio.sleep(remaining), name=f"bale-auth-watchdog-{operation_id}")
    done, _ = await asyncio.wait({browser_task, watchdog_task}, return_when=asyncio.FIRST_COMPLETED)
    if watchdog_task in done and not browser_task.done():
        _terminalize_bale_auth_timeout(
            operation_id,
            account_id,
            started=started,
            failed_step="absolute_operation_deadline",
        )
        browser_task.add_done_callback(_consume_background_task)
        return
    watchdog_task.cancel()
    try:
        await browser_task
    except asyncio.CancelledError:
        raise


async def _run_non_browser_account_action(operation_id: str, account_id: str, action: str, confirmation: str | None = None) -> None:
    started = time.perf_counter()
    try:
        _update_bale_action_operation(operation_id, status="running", stage="validating")
        if action == "reset_profile" and confirmation != account_id:
            raise BaleOnboardingError("account_confirmation_required", "Exact account_id confirmation is required")
        if action == "delete_account":
            result = await asyncio.to_thread(commercial_queue_service.delete_bale_account_and_profile, account_id, operation_id)
        elif action == "reset_profile":
            result = await asyncio.to_thread(bale_onboarding_service.reset_profile, account_id, confirmation)
        else:
            raise BaleOnboardingError("unknown_account_action", action)
        _update_bale_action_operation(operation_id, status="succeeded", stage="succeeded", success=True, http_status=200, result=result,
            completed_at=datetime.now(timezone.utc).isoformat(), duration_ms=round((time.perf_counter() - started) * 1000, 3), last_successful_step="final_persisted_account_loaded")
    except Exception as exc:
        exception_payload = _account_operation_exception_payload(exc)
        _update_bale_action_operation(operation_id, status="failed", stage="failed", success=False, http_status=409,
            completed_at=datetime.now(timezone.utc).isoformat(), duration_ms=round((time.perf_counter() - started) * 1000, 3),
            error_code=str(getattr(exc, "error_code", type(exc).__name__)), error_message=str(exc), failed_step="account_action",
            nested_error=exception_payload)


def _schedule_bale_action(coroutine: Any) -> None:
    task = asyncio.create_task(coroutine)
    _bale_action_tasks.add(task)
    task.add_done_callback(_bale_action_tasks.discard)


# FUNCTION:
# open_bale_authentication
# RESPONSIBILITY:
# Dispatches authentication open to the requested account executor.
# INPUT:
# BaleAuthenticationOpenRequest
# OUTPUT:
# Authentication maintenance session payload
# SIDE EFFECTS:
# Creates an account executor and persistent browser session.
@router.post("/platforms/bale/authentication/open")
async def open_bale_authentication(request: BaleAuthenticationOpenRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    action = "session_recheck" if request.purpose == "session_recheck" else "open_login"
    operation, created = _new_bale_action_operation(
        request.account_id, action, "/automation/platforms/bale/authentication/open", "POST"
    )
    if created:
        _schedule_bale_action(_run_auth_action(operation["operation_id"], request.account_id, request.purpose))
    response.status_code = 202
    return operation


@router.post("/platforms/bale/authentication/session-recheck")
async def recheck_bale_authentication(request: BaleAuthenticationOpenRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    operation, created = _new_bale_action_operation(
        request.account_id, "session_recheck", "/automation/platforms/bale/authentication/session-recheck", "POST"
    )
    if created:
        _schedule_bale_action(_run_auth_action(operation["operation_id"], request.account_id, "session_recheck"))
    response.status_code = 202
    return operation


@router.get("/platforms/bale/account-operations/{operation_id}")
def get_bale_account_action(operation_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    with _bale_action_guard:
        operation = _bale_action_operations.get(operation_id)
        if operation is None:
            operation = _load_persisted_bale_action_operation(operation_id)
        if operation is None:
            raise HTTPException(status_code=404, detail={"error_code": "account_operation_not_found", "operation_id": operation_id})
        return dict(operation)


@router.get("/platforms/bale/authentication/status/{maintenance_session_id}")
async def get_bale_authentication_status(maintenance_session_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return await bale_account_executor_registry.execute_session_operation(
            maintenance_session_id,
            "status",
            _get_bale_authentication_status_sync,
            maintenance_session_id,
        )
    except Exception as exc:
        persisted = bale_onboarding_service.persisted_authentication_status(maintenance_session_id)
        if persisted is not None:
            return persisted
        raise _campaign_lifecycle_error(exc) from exc


@router.post("/platforms/bale/authentication/verify/{maintenance_session_id}")
async def verify_bale_authentication(maintenance_session_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return await bale_account_executor_registry.execute_session_operation(
            maintenance_session_id,
            "verify",
            _verify_bale_authentication_sync,
            maintenance_session_id,
        )
    except Exception as exc:
        raise _campaign_lifecycle_error(exc) from exc


@router.post("/platforms/bale/authentication/close/{maintenance_session_id}")
async def close_bale_authentication(maintenance_session_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return await bale_account_executor_registry.execute_session_operation(
            maintenance_session_id,
            "close",
            _close_bale_authentication_sync,
            maintenance_session_id,
        )
    except Exception as exc:
        raise _campaign_lifecycle_error(exc) from exc


def _open_bale_authentication_sync(account_id: str, purpose: str, operation_id: str | None = None) -> dict[str, Any]:
    launch_lock = None
    try:
        prepare = getattr(commercial_queue_service, "prepare_bale_authentication_open", None)
        prepared = prepare(account_id) if callable(prepare) else {"action": "new"}
        if prepared.get("action") == "reuse":
            return dict(prepared["session"])

        recover = getattr(bale_onboarding_service, "recover_stale_authentication_open", None)
        if callable(recover):
            recover(account_id, force=prepared.get("action") == "recovered")

        try:
            launch_lock = bale_onboarding_service.acquire_profile_launch_lock(account_id, operation_id=operation_id)
        except TypeError as exc:
            # Keep third-party/test onboarding implementations compatible while
            # the canonical store receives operation ownership metadata.
            if "operation_id" not in str(exc):
                raise
            launch_lock = bale_onboarding_service.acquire_profile_launch_lock(account_id)
        result = commercial_queue_service.open_bale_authentication(account_id)
        result = {
            **result,
            "operation_id": operation_id,
            "profile_generation_id": launch_lock.get("profile_generation_id"),
            "owner_pid": launch_lock.get("owner_pid"),
            "profile_lease_owner_id": launch_lock.get("owner_id"),
        }
        account = bale_onboarding_service.record_authentication_open(account_id, result, purpose=purpose)
        auth = result.get("auth") or {}
        if auth.get("auth_state") == "authenticated" and bool(auth.get("authenticated")):
            # Login completion always flows directly into the same own-profile
            # identity probe used by polling. Shell visibility alone is insufficient.
            try:
                verified = _verify_bale_authentication_sync(str(result["maintenance_session_id"]), close_after=True)
                return {**result, **verified, "automatic_post_login_verification": True}
            except Exception as exc:
                code = str(getattr(exc, "error_code", "auth_probe_inconclusive"))
                bale_onboarding_service.record_session_health_probe(account_id, result="temporarily_inconclusive")
                return {
                    **result,
                    "state": "temporarily_inconclusive",
                    "terminal": False,
                    "retryable": True,
                    "error_code": code,
                    "error_message": str(exc),
                    "automatic_post_login_verification": True,
                }
        return result
    except Exception:
        if launch_lock:
            bale_onboarding_service.release_profile_launch_lock(account_id, launch_lock["owner_id"])
        raise


def _bale_authentication_open_error(exc: Exception) -> HTTPException:
    error_code = getattr(exc, "error_code", None) or "bale_authentication_open_failed"
    return HTTPException(
        status_code=409,
        detail={
            "error_code": error_code,
            "error_message": str(exc),
            "validation": getattr(exc, "details", None) or getattr(exc, "summary", {}),
        },
        headers={"Access-Control-Allow-Origin": "*"},
    )


def _get_bale_authentication_status_sync(maintenance_session_id: str) -> dict[str, Any]:
    result = commercial_queue_service.get_bale_authentication_status(maintenance_session_id)
    recorded = bale_onboarding_service.record_authentication_status(maintenance_session_id, result)
    auth = recorded.get("auth") or {}
    if auth.get("auth_state") == "authenticated" and auth.get("authenticated") and auth.get("chat_shell_visible"):
        return _verify_bale_authentication_sync(maintenance_session_id, close_after=True)
    return recorded


def _verify_bale_authentication_sync(maintenance_session_id: str, close_after: bool = True) -> dict[str, Any]:
    result = commercial_queue_service.verify_bale_authentication(maintenance_session_id)
    result["account"] = bale_onboarding_service.record_authentication_verified(maintenance_session_id, result)
    if result.get("verified"):
        activate = getattr(commercial_queue_service, "activate_verified_account_eligibility", None)
        if callable(activate):
            activation = activate(str(result.get("account_id") or ""), previously_eligible=False)
            result["worker_eligibility"] = activation
            result["readiness"] = activation.get("readiness")
            result["scheduler_woken"] = activation.get("scheduler_woken", False)
        else:
            refresh = getattr(commercial_queue_service, "refresh_authenticated_bale_worker_eligibility", None)
            if callable(refresh):
                result["worker_eligibility"] = refresh()
        result["state"] = "ready"
        result["terminal"] = True
        result["retryable"] = False
        if close_after:
            close_result = commercial_queue_service.close_bale_authentication(maintenance_session_id)
            bale_onboarding_service.record_authentication_closed(maintenance_session_id, close_result)
            result["maintenance_released"] = bool(close_result.get("closed") or close_result.get("already_closed"))
        else:
            result["maintenance_released"] = False
    return result


def _close_bale_authentication_sync(maintenance_session_id: str) -> dict[str, Any]:
    result = commercial_queue_service.close_bale_authentication(maintenance_session_id)
    result["account"] = bale_onboarding_service.record_authentication_closed(maintenance_session_id, result)
    return result


# ============================================================
# END BLOCK: BALE_AUTHENTICATION_ACCOUNT_EXECUTOR_BOUNDARY
# ============================================================


def _onboarding_error(exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "error_code": getattr(exc, "error_code", "bale_onboarding_error"),
            "message": str(exc),
            "details": getattr(exc, "details", {}),
        },
    )


@router.get("/platforms/bale/onboarding/accounts")
def list_bale_onboarding_accounts(response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    from app.performance import span
    with span("onboarding_bulk_database"):
        items = commercial_queue_service.canonical_bale_account_states()
        configuration = bale_onboarding_service.configuration()
    return {"items": items, "configuration": configuration, "summary": {
        "registered_accounts": sum(bool(item.get("registered")) for item in items),
        "authenticated_accounts": sum(item.get("session_state") == "authenticated" for item in items),
        "login_required_accounts": sum(item.get("session_state") == "login_required" for item in items),
        "verification_fresh": sum(bool(item.get("session_healthy_recent")) for item in items),
        "verification_expired": sum(bool(item.get("verification_expired")) for item in items),
        "identity_verified": sum(bool(item.get("durable_identity_verified")) for item in items),
        "session_healthy": sum(bool(item.get("session_health_acceptable")) for item in items),
        "probe_due": sum("session_revalidation_required" in item.get("eligibility_reasons", []) for item in items),
        "in_grace": sum(bool(item.get("session_in_grace")) for item in items),
        "otp_required": sum(item.get("session_state") == "otp_required" for item in items),
        "disabled": sum(not item.get("enabled") for item in items),
        "cooling_down": 0,
        "eligible": sum(bool(item.get("worker_eligible")) for item in items),
        "active": sum(item.get("worker_status") != "idle" for item in items),
        "daily_limited": 0,
        "error": sum(bool(item.get("last_error")) for item in items),
    }}


@router.get("/platforms/bale/onboarding/accounts/{account_id}")
def get_bale_onboarding_account(account_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    account = next((item for item in commercial_queue_service.canonical_bale_account_states(include_soft_deleted=True) if item["account_id"] == account_id), None)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    return account


@router.get("/platforms/bale/onboarding/accounts/{account_id}/reconcile")
def reconcile_bale_onboarding_account(account_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return bale_onboarding_service.reconcile(account_id)

@router.delete("/platforms/bale/accounts/{account_id}")
async def delete_bale_account(account_id: str, response: Response, delete_profile: bool = Query(True)) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    if not delete_profile:
        raise HTTPException(status_code=400, detail={"error_code": "delete_profile_confirmation_required"})
    operation, created = _new_bale_action_operation(
        account_id, "delete_account", f"/automation/platforms/bale/accounts/{account_id}", "DELETE"
    )
    if created:
        _schedule_bale_action(_run_non_browser_account_action(operation["operation_id"], account_id, "delete_account"))
    response.status_code = 202
    return operation


@router.post("/platforms/bale/onboarding/accounts/{account_id}/reset-profile")
async def reset_bale_account_profile(account_id: str, request: BaleProfileResetRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    operation, created = _new_bale_action_operation(
        account_id, "reset_profile", f"/automation/platforms/bale/onboarding/accounts/{account_id}/reset-profile", "POST"
    )
    if created:
        _schedule_bale_action(_run_non_browser_account_action(operation["operation_id"], account_id, "reset_profile", request.confirmation))
    response.status_code = 202
    return operation


@router.post("/platforms/bale/onboarding/preflight")
def preflight_bale_onboarding(request: BaleOnboardingPreflightRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return bale_onboarding_service.preflight(request.identifier, request.account_id)


@router.post("/platforms/bale/onboarding/provision")
def provision_bale_onboarding_account(request: BaleOnboardingProvisionRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return bale_onboarding_service.provision(request.model_dump())
    except BaleOnboardingError as exc:
        raise _onboarding_error(exc) from exc


@router.put("/platforms/bale/onboarding/accounts/{account_id}/scheduling")
def set_bale_onboarding_scheduling(account_id: str, request: BaleSchedulingRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return bale_onboarding_service.set_scheduling(account_id, request.enabled)
    except BaleOnboardingError as exc:
        raise _onboarding_error(exc) from exc


@router.post("/platforms/bale/onboarding/accounts/{account_id}/disable")
def disable_bale_onboarding_account(account_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return bale_onboarding_service.disable_account(account_id)
    except Exception as exc:
        raise _onboarding_error(exc) from exc


@router.post("/platforms/bale/onboarding/accounts/{account_id}/retire")
def retire_bale_onboarding_account(account_id: str, request: BaleRetireRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return bale_onboarding_service.transition(account_id, "retired", reason=request.reason)
    except BaleOnboardingError as exc:
        raise _onboarding_error(exc) from exc


@router.get("/platforms/bale/onboarding/batches")
def list_bale_onboarding_batches(response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return bale_onboarding_service.list_batches()


@router.post("/platforms/bale/onboarding/batches")
def create_bale_onboarding_batch(request: BaleOnboardingBatchRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return bale_onboarding_service.create_batch(request.model_dump())
    except BaleOnboardingError as exc:
        raise _onboarding_error(exc) from exc


@router.get("/platforms/bale/onboarding/batches/{batch_id}")
def get_bale_onboarding_batch(batch_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return bale_onboarding_service.get_batch(batch_id)
    except BaleOnboardingError as exc:
        raise _onboarding_error(exc) from exc


@router.get("/platforms/bale/onboarding/operations/{operation_id}")
def get_bale_onboarding_operation(operation_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    operation = bale_onboarding_service.get_operation(operation_id)
    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")
    return operation


@router.get("/platforms/bale/onboarding/audit-events")
def list_bale_onboarding_audit_events(response: Response, account_id: str | None = None, batch_id: str | None = None) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return bale_onboarding_service.audit_events(account_id, batch_id)


@router.post("/platforms/bale/onboarding/recover-stale-sessions")
def recover_bale_onboarding_sessions(response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return bale_onboarding_service.recover_stale_sessions()


@router.get("/platforms/bale/onboarding/configuration")
def get_bale_onboarding_configuration(response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return bale_onboarding_service.configuration()


@router.put("/platforms/bale/onboarding/configuration")
def update_bale_onboarding_configuration(request: BaleOperationalConfigurationRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return bale_onboarding_service.update_configuration(request.configuration, request.actor)
    except Exception as exc:
        raise _onboarding_error(exc) from exc


@router.get("/platforms/bale/source-channel")
def get_bale_source_channel(response: Response, account_id: str) -> dict[str, Any] | None:
    _set_dashboard_cors_headers(response)
    return bale_account_store.get_source_channel(account_id)


@router.put("/platforms/bale/source-channel")
def save_bale_source_channel(request: BaleSourceChannelRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        saved = bale_account_store.save_source_channel(request.account_id, request.source_channel_url)
        reloaded = bale_account_store.get_source_channel(request.account_id)
        if not reloaded or reloaded.get("source_channel_uid") != saved.get("source_channel_uid"):
            raise HTTPException(status_code=500, detail="source_channel_save_not_persisted")
        return reloaded
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/platforms/bale/forward-latest/preview")
def preview_latest_bale_channel_message(request: BalePreviewLatestRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    source_channel = bale_account_store.get_source_channel(request.account_id)
    if source_channel is None or not source_channel.get("source_channel_url"):
        result = {
            "success": False,
            "ok": False,
            "account_id": request.account_id,
            "action": "preview_latest_channel_message",
            "source_channel_url": "",
            "message_found": False,
            "error_code": "source_channel_not_configured",
            "error_message": "Bale source channel URL is not configured",
            "failed_step": "load_source_channel",
            "last_successful_step": None,
            "diagnostics": {},
        }
        _record_bale_preview_job(request.account_id, result)
        return result

    result = bale_plugin.preview_latest_channel_message(
        account_id=request.account_id,
        source_channel_url=str(source_channel.get("source_channel_url") or ""),
        provider_mode="native_chrome",
    )
    _record_bale_preview_job(request.account_id, result)
    return result


@router.post("/platforms/bale/save-contact")
def save_bale_contact(request: BaleSaveContactRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    result = bale_plugin.save_bale_contact(
        account_id=request.account_id,
        phone=request.phone,
        provider_mode="native_chrome",
    )
    _record_bale_action_job(request.account_id, "save_bale_contact", result)
    return result


@router.post("/platforms/bale/open-source-channel")
def open_bale_source_channel(request: BaleOpenSourceChannelRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    configured = bale_account_store.get_source_channel(request.account_id) or {}
    effective_uid = normalize_source_channel_uid(request.source_channel_uid or configured.get("source_channel_uid") or configured.get("source_channel_url") or "")
    result = bale_plugin.open_bale_source_channel(
        account_id=request.account_id,
        source_channel_uid=effective_uid,
        provider_mode="native_chrome",
    )
    result.update(_source_channel_route_meta(request.source_channel_uid, configured, effective_uid))
    _record_bale_action_job(request.account_id, "open_bale_source_channel", result)
    return result


@router.post("/platforms/bale/locate-latest-channel-message")
def locate_latest_channel_message(request: BaleLocateLatestChannelMessageRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    configured = bale_account_store.get_source_channel(request.account_id) or {}
    effective_uid = normalize_source_channel_uid(request.source_channel_uid or configured.get("source_channel_uid") or configured.get("source_channel_url") or "")
    result = bale_plugin.locate_latest_channel_message(
        account_id=request.account_id,
        source_channel_uid=effective_uid,
        provider_mode="native_chrome",
    )
    result.update(_source_channel_route_meta(request.source_channel_uid, configured, effective_uid))
    _record_bale_action_job(request.account_id, "locate_latest_channel_message", result)
    return result


@router.post("/platforms/bale/open-message-forward")
def open_message_forward(request: BaleOpenMessageForwardRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    configured = bale_account_store.get_source_channel(request.account_id) or {}
    effective_uid = normalize_source_channel_uid(request.source_channel_uid or configured.get("source_channel_uid") or configured.get("source_channel_url") or "")
    result = bale_plugin.open_message_forward(
        account_id=request.account_id,
        source_channel_uid=effective_uid,
        provider_mode="native_chrome",
    )
    result.update(_source_channel_route_meta(request.source_channel_uid, configured, effective_uid))
    _record_bale_action_job(request.account_id, "open_message_forward", result)
    return result


@router.post("/platforms/bale/forward-message-to-contact")
def forward_message_to_contact(request: BaleForwardMessageToContactRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    configured = bale_account_store.get_source_channel(request.account_id) or {}
    effective_uid = normalize_source_channel_uid(request.source_channel_uid or configured.get("source_channel_uid") or configured.get("source_channel_url") or "")
    result = bale_plugin.forward_message_to_contact(
        account_id=request.account_id,
        source_channel_uid=effective_uid,
        display_name=request.display_name,
        dry_run=request.dry_run,
        selection_only=request.selection_only,
        provider_mode="native_chrome",
    )
    result.update(_source_channel_route_meta(request.source_channel_uid, configured, effective_uid))
    _record_bale_action_job(request.account_id, "forward_message_to_contact", result)
    return result


@router.post("/platforms/bale/forward-latest-channel-message")
def forward_latest_channel_message(request: BaleForwardLatestChannelMessageRequest, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    result = bale_plugin.forward_latest_channel_message(
        account_id=request.account_id,
        source_channel_uid=request.source_channel_uid,
        phone=request.phone,
        display_name=request.display_name,
        recipient_id=request.recipient_id,
        job_id=request.job_id,
        campaign_id=request.campaign_id,
        idempotency_key=request.idempotency_key,
        dry_run=request.dry_run,
        provider_mode="native_chrome",
    )
    _record_bale_action_job(request.account_id, "forward_latest_channel_message", result)
    return result


def _source_channel_route_meta(requested_value: str, configured: dict[str, Any], effective_uid: str) -> dict[str, Any]:
    requested_uid = normalize_source_channel_uid(requested_value) if str(requested_value or "").strip() else ""
    configured_uid = str(configured.get("source_channel_uid") or "")
    return {
        "requested_source_channel_uid": requested_uid,
        "configured_source_channel_uid": configured_uid,
        "effective_source_channel_uid": effective_uid,
        "source_channel_value_origin": "request" if requested_uid else "stored_config",
        "requested_channel_url": canonical_source_channel_url(effective_uid),
        "final_channel_url": canonical_source_channel_url(effective_uid),
        "channel_uid_verified": True,
    }


def _bale_job_diagnostics_shape(job: dict[str, Any]) -> dict[str, Any]:
    execution_result = job.get("execution_result") if isinstance(job.get("execution_result"), dict) else {}
    plugin_result = job.get("plugin_result") if isinstance(job.get("plugin_result"), dict) else None
    if plugin_result is None:
        plugin_result = execution_result.get("plugin_result") if isinstance(execution_result.get("plugin_result"), dict) else None
    diagnostics = plugin_result or execution_result
    return {
        "job_id": job.get("job_id"),
        "status": job.get("status"),
        "action": job.get("action") or diagnostics.get("action") or execution_result.get("action") or job.get("scenario_id"),
        "scenario_id": job.get("scenario_id"),
        "account_id": job.get("account_id"),
        "platform_id": job.get("platform_id"),
        "normalized_phone": job.get("normalized_phone"),
        "contact_naming_value": job.get("contact_naming_value"),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "error_code": job.get("error_code") or diagnostics.get("error_code") or execution_result.get("error_code"),
        "error_message": job.get("error_message") or diagnostics.get("error_message") or diagnostics.get("error") or execution_result.get("error_message"),
        "execution_result": execution_result,
        "plugin_result": plugin_result,
        "failed_step": diagnostics.get("failed_step") or execution_result.get("failed_step"),
        "last_successful_step": diagnostics.get("last_successful_step") or execution_result.get("last_successful_step"),
        "screenshot_path": diagnostics.get("screenshot_path") or execution_result.get("screenshot_path"),
        "step_results": diagnostics.get("step_results") or execution_result.get("step_results") or [],
    }


def _record_bale_preview_job(account_id: str, result: dict[str, Any]) -> None:
    _record_bale_action_job(account_id, "preview_latest_channel_message", result)


def _record_bale_action_job(account_id: str, action: str, result: dict[str, Any]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    execution_result = {
        "success": bool(result.get("success") or result.get("ok")),
        "action": action,
        "provider_mode": result.get("provider_mode") or "native_chrome",
        "plugin_result": result,
        "failed_step": result.get("failed_step"),
        "last_successful_step": result.get("last_successful_step"),
        "error_code": result.get("error_code"),
        "error_message": result.get("error_message"),
    }
    job = {
        "job_id": result.get("job_id") or f"bale_{action}_{uuid4().hex[:12]}",
        "campaign_id": result.get("campaign_id") or f"bale_{action}",
        "route_id": "",
        "assignment_id": "",
        "platform_id": "bale",
        "account_group_id": "",
        "account_id": account_id,
        "contact_id": "",
        "normalized_phone": result.get("phone_normalized") or result.get("normalized_phone") or "",
        "contact_naming_value": result.get("display_name") or result.get("contact_naming_value") or "",
        "message_source_id": "",
        "scenario_id": None,
        "action": action,
        "status": "completed" if execution_result["success"] else "failed",
        "dry_run": False,
        "planned_for_date": "",
        "created_at": now,
        "updated_at": now,
        "error_code": result.get("error_code"),
        "error_message": result.get("error_message"),
        "execution_result": execution_result,
        "plugin_result": result,
    }
    jobs = execution_queue_store.list_jobs()
    jobs.append(job)
    execution_queue_store.save_jobs(jobs)


@router.put("/platforms/bale/accounts/{account_id}")
def update_bale_account(
    account_id: str,
    request: BaleAccountUpdateRequest,
    response: Response,
) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    payload = {key: value for key, value in request.model_dump().items() if value is not None}
    try:
        return bale_account_store.update_account(account_id, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Account not found: {account_id}") from None


@router.get("/platforms/bale/profile-groups")
def list_bale_profile_groups(response: Response) -> list[dict[str, Any]]:
    _set_dashboard_cors_headers(response)
    return profile_group_store.list_groups()


@router.post("/platforms/bale/profile-groups")
def create_bale_profile_group(
    request: BaleProfileGroupRequest,
    response: Response,
) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    if request.browser_provider not in BROWSER_PROVIDERS:
        raise HTTPException(status_code=400, detail="Unsupported browser provider")
    try:
        return profile_group_store.create_group(request.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.put("/platforms/bale/profile-groups/{profile_group_id}")
def update_bale_profile_group(
    profile_group_id: str,
    request: BaleProfileGroupRequest,
    response: Response,
) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    if request.browser_provider not in BROWSER_PROVIDERS:
        raise HTTPException(status_code=400, detail="Unsupported browser provider")
    payload = request.model_dump()
    payload.pop("device_group_id", None)
    payload.pop("profile_group_id", None)
    try:
        return profile_group_store.update_group(profile_group_id, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Profile group not found: {profile_group_id}") from None


@router.delete("/platforms/bale/profile-groups/{profile_group_id}")
def delete_bale_profile_group(profile_group_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return profile_group_store.delete_group(profile_group_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Profile group not found: {profile_group_id}") from None


@router.post("/platforms/bale/accounts/{account_id}/assign-profile-group")
def assign_bale_profile_group(
    account_id: str,
    request: BaleAssignProfileGroupRequest,
    response: Response,
) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    if request.browser_provider not in BROWSER_PROVIDERS:
        raise HTTPException(status_code=400, detail="Unsupported browser provider")
    profile_group_id = request.profile_group_id or request.device_group_id
    if not profile_group_id:
        raise HTTPException(status_code=400, detail="profile_group_id is required")
    try:
        profile_group_store.assign_account(account_id, profile_group_id)
        return bale_account_store.assign_profile_group(
            account_id=account_id,
            device_group_id=profile_group_id,
            browser_provider=request.browser_provider,
            profile_id=request.profile_id,
            adspower_profile_id=request.adspower_profile_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Profile group or account not found: {exc}") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/platforms/bale/message-config")
def get_bale_message_config(response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return bale_account_store.get_message_config()


@router.put("/platforms/bale/message-config")
def update_bale_message_config(
    request: BaleMessageConfigRequest,
    response: Response,
) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return bale_account_store.save_message_config(request.model_dump())


@router.get("/platforms/bale/scenarios")
def list_bale_scenarios(response: Response) -> list[dict[str, Any]]:
    _set_dashboard_cors_headers(response)
    return scenario_loader.list_platform_scenarios("bale")


@router.post("/platforms/bale/test-forward")
def test_bale_forward(
    request: BaleTestForwardRequest,
    response: Response,
) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    scenario_id = "forward_from_source"
    policy = CompliancePolicy.from_dict(request.compliance_policy)
    account = bale_account_store.get_account(request.account_id)
    if account is None:
        return {
            "ok": False,
            "dry_run": request.dry_run,
            "compliance_policy": policy.to_dict(),
            "planned_steps": [],
            "skipped_accounts": [{"account_id": request.account_id, "reason": "account_not_found"}],
            "warnings": [],
        }
    allowed_by_policy, policy_reason, _limits = policy.evaluate_account(
        account,
        account.get("daily_limit", policy.max_actions_per_account_per_day),
        account.get("hourly_limit", policy.max_actions_per_account_per_hour),
        account.get("min_delay_seconds", policy.min_delay_between_actions_seconds),
    )
    if not allowed_by_policy:
        return {
            "ok": False,
            "dry_run": request.dry_run,
            "compliance_policy": policy.to_dict(),
            "planned_steps": [],
            "skipped_accounts": [{"account_id": request.account_id, "reason": policy_reason}],
            "warnings": [],
        }
    governance = can_account_run_scenario(request.account_id, scenario_id)
    if not governance["allowed"]:
        bale_plugin.log_scenario_step(
            request.account_id,
            scenario_id,
            "governance_check",
            "skipped",
            "Account cannot run scenario",
            reason=governance["reason"],
            dry_run=request.dry_run,
        )
        return {
            "ok": False,
            "dry_run": request.dry_run,
            "compliance_policy": policy.to_dict(),
            "governance": governance,
            "planned_steps": [],
            "skipped_accounts": [{"account_id": request.account_id, "reason": governance["reason"]}],
            "warnings": [],
        }

    scenario = scenario_loader.load("bale", scenario_id)
    validation = scenario_validator.validate(scenario)
    if not validation["ok"]:
        return {"ok": False, "dry_run": request.dry_run, "errors": validation["errors"], "planned_steps": []}

    config = bale_account_store.get_message_config()
    request_data = request.model_dump()
    for key in ["source_type", "source_value", "source_message_hint", "description"]:
        if request_data.get(key) is not None:
            config[key] = request_data[key]

    dry_run_plan = scenario_executor_stub.dry_run(
        scenario,
        {
            "account_id": request.account_id,
            "target": request.target,
            "source": config,
        },
    )
    for step in dry_run_plan["planned_steps"]:
        bale_plugin.log_scenario_step(
            request.account_id,
            scenario_id,
            step["step_id"],
            "planned" if request.dry_run else "pending_test_action",
            step.get("description", ""),
            reason=governance["reason"],
            dry_run=request.dry_run,
        )

    if request.dry_run:
        return {**dry_run_plan, "compliance_policy": policy.to_dict(), "governance": governance, "warnings": []}

    bale_plugin.log_scenario_step(
        request.account_id,
        scenario_id,
        "forward_once",
        "blocked",
        "Controlled forward execution is not implemented yet; dry-run plan returned",
        reason="implementation_pending",
        dry_run=False,
    )
    return {
        **dry_run_plan,
        "ok": False,
        "dry_run": False,
        "compliance_policy": policy.to_dict(),
        "governance": governance,
        "warnings": ["controlled_forward_execution_not_implemented"],
        "message": "Controlled forward execution is not implemented yet; no message was sent.",
    }


@router.post("/platforms/bale/schedule/dry-run")
def bale_schedule_dry_run(
    request: BaleScheduleDryRunRequest,
    response: Response,
) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    accounts = bale_account_store.list_accounts()
    if request.selected_accounts:
        selected = set(request.selected_accounts)
        accounts = [account for account in accounts if account["account_id"] in selected]

    eligible = []
    skipped = []
    for account in accounts:
        governance = can_account_run_scenario(account["account_id"], request.scenario_id)
        if governance["allowed"]:
            eligible.append(account)
        else:
            skipped.append({"account_id": account["account_id"], "reason": governance["reason"]})

    result = scenario_dry_run_scheduler.build_dry_run_plan(
        eligible,
        request.scenario_id,
        request.work_start,
        request.work_end,
        request.daily_limit,
        request.hourly_limit,
        request.min_delay_seconds,
        request.max_actions_per_session,
        profile_group_store.list_groups(),
        request.max_concurrent_browsers,
        request.close_browser_after_task,
        CompliancePolicy.from_dict(request.compliance_policy),
        "bale",
        request.plan_seed,
        account_group_store.list_platform_groups("bale"),
    )
    result["skipped"] = skipped + result.get("skipped", [])
    result["skipped_accounts"] = skipped + result.get("skipped_accounts", [])
    return result


@router.get("/platforms/bale/preparation")
def get_bale_preparation(response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return bale_account_store.get_preparation()


@router.put("/platforms/bale/preparation")
def update_bale_preparation(
    request: BalePreparationRequest,
    response: Response,
) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return bale_account_store.save_preparation(request.model_dump())


@router.post("/platforms/bale/preparation/dry-run")
def bale_preparation_dry_run(response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    config = bale_account_store.get_preparation()
    selected = set(config.get("selected_accounts") or [])
    sections = set(config.get("account_sections") or [])
    planned_checks = [
        "ownership_rule",
        "manual_approval_required",
        "login_status_review",
        "health_score_review",
        "block_status_review",
    ]
    selected_accounts = []
    skipped = []
    for account in bale_account_store.list_accounts():
        if selected and account["account_id"] not in selected:
            skipped.append({"account_id": account["account_id"], "reason": "not_selected"})
            continue
        if sections and account["section"] not in sections:
            skipped.append({"account_id": account["account_id"], "reason": "section_not_selected"})
            continue
        governance = can_account_run_scenario(account["account_id"], "account_preparation_check")
        selected_accounts.append(
            {
                "account_id": account["account_id"],
                "section": account["section"],
                "readiness": "ready" if governance["allowed"] else "needs_review",
                "reason": governance["reason"],
            }
        )
    return {
        "ok": True,
        "dry_run": True,
        "mode": config.get("mode", "qa_only"),
        "selected_accounts": selected_accounts,
        "account_sections": config.get("account_sections", []),
        "planned_checks": planned_checks,
        "skipped": skipped,
    }


@router.post("/platforms/bale/open-account")
def open_bale_account(
    request: BaleOpenAccountRequest,
    response: Response,
) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return bale_plugin.open_account(request.account_id)


@router.post("/platforms/bale/accounts/{account_id}/open-login")
def open_bale_login(account_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    raise HTTPException(
        status_code=410,
        detail={"error_code": "legacy_login_route_deprecated", "message": "Use the controlled Bale authentication workflow."},
    )


@router.post("/platforms/bale/accounts/{account_id}/check-login")
def check_bale_login(account_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    raise HTTPException(
        status_code=410,
        detail={"error_code": "legacy_login_route_deprecated", "message": "Use the controlled Bale authentication workflow."},
    )


@router.post("/platforms/bale/send-test")
def send_bale_test(
    request: BaleSendTestRequest,
    response: Response,
) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    if request.action == "forward_message":
        target = request.target if isinstance(request.target, dict) else {"phone": request.target}
        return bale_plugin.forward_message(
            account_id=request.account_id,
            source=request.source,
            target=target,
            normalized_phone=request.normalized_phone or str(target.get("phone") or ""),
            contact_naming_value=request.contact_naming_value or str(target.get("name") or ""),
        )
    return bale_plugin.send_test_message(
        account_id=request.account_id,
        target=str(request.target),
        message=request.message,
    )


@router.get("/platforms/{platform_id}")
def get_automation_platform(platform_id: str, response: Response) -> dict[str, str | bool]:
    _set_dashboard_cors_headers(response)
    platform = get_platform(platform_id)
    if platform is None:
        raise HTTPException(status_code=404, detail=f"Platform not found: {platform_id}")
    return platform


@router.post("/accounts/{account_id}/open-browser")
def open_account_browser(account_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return {
        "ok": False,
        "message": "Open browser endpoint is not implemented yet",
        "account_id": account_id,
    }


@router.get("/accounts")
def list_accounts(response: Response) -> list[dict[str, Any]]:
    _set_dashboard_cors_headers(response)
    return account_registry_service.list_platform_accounts("bale")


@router.get("/health")
def automation_health(response: Response) -> dict[str, str]:
    _set_dashboard_cors_headers(response)
    return {
        "status": "ok",
        "backend": "running",
    }


@router.post("/worker/start")
def start_worker() -> dict[str, Any]:
    global _worker_thread

    with _worker_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return {"status": "already_running"}

        _worker_thread = threading.Thread(
            target=worker.run_continuous,
            name="automation-engine-worker",
            daemon=True,
        )
        _worker_thread.start()
        return {"status": "started"}
