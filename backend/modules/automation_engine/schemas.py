from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


@dataclass
class ScenarioStep:
    action: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScenarioDefinition:
    name: str
    version: str
    platform: str
    steps: list[ScenarioStep]


@dataclass
class AutomationTask:
    task_id: str
    scenario: ScenarioDefinition
    status: TaskStatus = TaskStatus.PENDING


@dataclass
class TaskResult:
    task_id: str
    status: TaskStatus
    ok: bool
    message: str
    logs: list[str] = field(default_factory=list)

