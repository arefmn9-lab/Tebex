from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .schemas import TaskStatus


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class TaskState:
    task_id: str
    status: TaskStatus = TaskStatus.PENDING
    current_step_index: int = -1
    logs: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def add_log(self, message: str) -> None:
        timestamp = utc_now().isoformat()
        self.logs.append(f"{timestamp} {message}")

    def mark_running(self) -> None:
        self.status = TaskStatus.RUNNING
        self.started_at = utc_now()
        self.add_log("Task started")

    def mark_success(self, message: str = "Task completed successfully") -> None:
        self.status = TaskStatus.SUCCESS
        self.finished_at = utc_now()
        self.add_log(message)

    def mark_failed(self, message: str) -> None:
        self.status = TaskStatus.FAILED
        self.finished_at = utc_now()
        self.add_log(message)

