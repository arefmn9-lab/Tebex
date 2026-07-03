from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .db.repository import AutomationRepository


TaskRecord = dict[str, Any]
DEFAULT_ACCOUNT_ID = "default"


class TaskQueue:
    def __init__(self, repository: AutomationRepository | None = None) -> None:
        self._tasks: dict[str, TaskRecord] = {}
        self._order: list[str] = []
        self._tasks_by_account: dict[str, list[str]] = {}
        self.repository = repository or AutomationRepository()

    def add_task(self, task: TaskRecord) -> TaskRecord:
        task_id = str(task.get("task_id") or uuid4())
        task_record: TaskRecord = {
            "task_id": task_id,
            "account_id": str(task.get("account_id") or DEFAULT_ACCOUNT_ID),
            "scenario_path": str(task["scenario_path"]),
            "status": task.get("status", "pending"),
            "created_at": task.get("created_at") or datetime.now(timezone.utc),
            "run_at": task.get("run_at"),
            "logs": list(task.get("logs", [])),
        }
        self._tasks[task_id] = task_record
        self._order.append(task_id)
        self._tasks_by_account.setdefault(task_record["account_id"], []).append(task_id)
        self._persist(lambda: self.repository.create_task(task_record))
        return task_record

    def get_next_task(self, account_id: str | None = None) -> TaskRecord | None:
        task_ids = self._tasks_by_account.get(account_id, []) if account_id else self._order
        for task_id in task_ids:
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

    def get_tasks_for_account(self, account_id: str) -> list[TaskRecord]:
        return [
            self._tasks[task_id].copy()
            for task_id in self._tasks_by_account.get(account_id, [])
        ]

    def list_account_ids(self) -> list[str]:
        return list(self._tasks_by_account.keys())

    def append_log(self, task_id: str, message: str, add_timestamp: bool = True) -> None:
        task = self._get_task(task_id)
        if add_timestamp:
            timestamp = datetime.now(timezone.utc).isoformat()
            message = f"{timestamp} {message}"
        task.setdefault("logs", []).append(message)
        if add_timestamp:
            self._persist(lambda: self.repository.insert_log(task_id, -1, "queue", message))

    def _update_status(self, task_id: str, status: str) -> None:
        task = self._get_task(task_id)
        task["status"] = status
        self._persist(lambda: self.repository.update_task_status(task_id, status))
        self.append_log(task_id, f"Task status changed to {status}")

    def _get_task(self, task_id: str) -> TaskRecord:
        try:
            return self._tasks[task_id]
        except KeyError as exc:
            raise KeyError(f"Task not found: {task_id}") from exc

    def _persist(self, operation: Any) -> None:
        try:
            operation()
        except Exception as exc:
            print(f"[QUEUE] Persistence skipped: {exc}")
