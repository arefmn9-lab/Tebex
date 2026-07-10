from __future__ import annotations

import threading
import sqlite3
from datetime import datetime
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile
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
from modules.automation_engine.platforms.platform_store import platform_store
from modules.automation_engine.plugins.bale import bale_plugin
from modules.automation_engine.plugins.bale.account_store import bale_account_store
from modules.automation_engine.plugins.bale.governance import can_account_run_scenario
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


class CreateTaskRequest(BaseModel):
    scenario_path: str
    run_at: datetime | None = None


class RunTaskRequest(BaseModel):
    task_id: str


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
    max_accounts: int = 30
    group_size_limit: int | None = None
    max_concurrent_accounts: int = 10
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


@router.get("/platforms")
def list_automation_platforms(response: Response) -> list[dict[str, str | bool]]:
    _set_dashboard_cors_headers(response)
    return registry_list_platforms()


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
    if platform_id == "bale":
        return bale_account_store.list_accounts()
    return platform_store.list_accounts(platform_id)


@router.post("/platforms/{platform_id}/accounts")
def create_platform_account(
    platform_id: str,
    request: BaleAccountRequest,
    response: Response,
) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    _ensure_platform(platform_id)
    if platform_id == "bale":
        try:
            return bale_account_store.create_account(request.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return platform_store.create_account(
        platform_id,
        phone=request.phone,
        status=request.status,
        daily_limit=request.daily_limit,
    )


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
    return sorted(jobs, key=lambda job: str(job.get("updated_at") or job.get("created_at") or ""), reverse=True)[0]


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


@router.delete("/platforms/bale/accounts/{account_id}")
def delete_bale_account(account_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    try:
        return bale_account_store.delete_account(account_id)
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
    return bale_plugin.open_login(account_id)


@router.post("/platforms/bale/accounts/{account_id}/check-login")
def check_bale_login(account_id: str, response: Response) -> dict[str, Any]:
    _set_dashboard_cors_headers(response)
    return bale_plugin.check_login(account_id)


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
    try:
        return [_serialize_account(account) for account in worker.account_manager.list_accounts()]
    except Exception:
        return []


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
