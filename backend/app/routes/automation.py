from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from modules.automation_engine.dispatcher import create_default_dispatcher
from modules.automation_engine.queue import TaskQueue, TaskRecord
from modules.automation_engine.scenario_engine import ScenarioEngine
from modules.automation_engine.scheduler import Scheduler
from modules.automation_engine.worker import Worker


router = APIRouter(prefix="/automation", tags=["automation"])

queue = TaskQueue()
scheduler = Scheduler(queue)
dispatcher = create_default_dispatcher()
scenario_engine = ScenarioEngine(dispatcher)
worker = Worker(queue, scheduler, scenario_engine)

_worker_thread: threading.Thread | None = None
_worker_lock = threading.Lock()


class CreateTaskRequest(BaseModel):
    scenario_path: str
    run_at: datetime | None = None


class RunTaskRequest(BaseModel):
    task_id: str


def _serialize_task(task: TaskRecord) -> dict[str, Any]:
    return {
        "task_id": task["task_id"],
        "scenario_path": task["scenario_path"],
        "status": task["status"],
        "created_at": _serialize_datetime(task.get("created_at")),
        "run_at": _serialize_datetime(task.get("run_at")),
        "logs": task.get("logs", []),
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


@router.get("/tasks")
def list_tasks() -> list[dict[str, Any]]:
    return [_serialize_task(task) for task in queue.get_all_tasks()]


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
