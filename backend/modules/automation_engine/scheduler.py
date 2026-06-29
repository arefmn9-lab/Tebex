from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .queue import TaskQueue, TaskRecord


class Scheduler:
    def __init__(self, queue: TaskQueue) -> None:
        self.queue = queue

    def schedule_task(self, task: TaskRecord, run_at: datetime | None = None) -> TaskRecord:
        task["run_at"] = run_at
        return self.queue.add_task(task)

    def get_ready_tasks(self) -> list[TaskRecord]:
        now = datetime.now(timezone.utc)
        ready_tasks: list[TaskRecord] = []
        for task in self.queue.get_all_tasks():
            if task["status"] != "pending":
                continue

            run_at = task.get("run_at")
            if run_at is None or run_at <= now:
                ready_tasks.append(task)

        return ready_tasks

