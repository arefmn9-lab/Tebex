from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


TaskRecord = dict[str, Any]


class TaskQueue:
    def __init__(self) -> None:
        self._tasks: dict[str, TaskRecord] = {}
        self._order: list[str] = []

    def add_task(self, task: TaskRecord) -> TaskRecord:
        task_id = str(task.get("task_id") or uuid4())
        task_record: TaskRecord = {
            "task_id": task_id,
            "scenario_path": str(task["scenario_path"]),
            "status": task.get("status", "pending"),
            "created_at": task.get("created_at") or datetime.now(timezone.utc),
            "run_at": task.get("run_at"),
            "logs": list(task.get("logs", [])),
        }
        self._tasks[task_id] = task_record
        self._order.append(task_id)
        return task_record

    def get_next_task(self) -> TaskRecord | None:
        for task_id in self._order:
            task = self._tasks[task_id]
            if task["status"] == "pending":
                return task
        return None

    def mark_task_running(self, task_id: str) -> None:
        self._update_status(task_id, "running")

    def mark_task_done(self, task_id: str) -> None:
        self._update_status(task_id, "done")

    def mark_task_failed(self, task_id: str) -> None:
        self._update_status(task_id, "failed")

    def get_all_tasks(self) -> list[TaskRecord]:
        return [self._tasks[task_id].copy() for task_id in self._order]

    def append_log(self, task_id: str, message: str, add_timestamp: bool = True) -> None:
        task = self._get_task(task_id)
        if add_timestamp:
            timestamp = datetime.now(timezone.utc).isoformat()
            message = f"{timestamp} {message}"
        task.setdefault("logs", []).append(message)

    def _update_status(self, task_id: str, status: str) -> None:
        task = self._get_task(task_id)
        task["status"] = status
        self.append_log(task_id, f"Task status changed to {status}")

    def _get_task(self, task_id: str) -> TaskRecord:
        try:
            return self._tasks[task_id]
        except KeyError as exc:
            raise KeyError(f"Task not found: {task_id}") from exc
