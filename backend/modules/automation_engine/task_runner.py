from __future__ import annotations

from collections import OrderedDict
from uuid import uuid4

from .scenario_engine import ScenarioEngine
from .schemas import AutomationTask, ScenarioDefinition, TaskResult, TaskStatus


class InMemoryTaskRunner:
    def __init__(self, scenario_engine: ScenarioEngine) -> None:
        self.scenario_engine = scenario_engine
        self.tasks: OrderedDict[str, AutomationTask] = OrderedDict()
        self.results: dict[str, TaskResult] = {}

    def add_task(self, scenario: ScenarioDefinition) -> AutomationTask:
        task_id = str(uuid4())
        task = AutomationTask(task_id=task_id, scenario=scenario)
        self.tasks[task_id] = task
        return task

    def run_task(self, task_id: str) -> TaskResult:
        task = self.tasks.get(task_id)
        if task is None:
            raise KeyError(f"Task not found: {task_id}")

        task.status = TaskStatus.RUNNING
        result = self.scenario_engine.execute(task.scenario, task_id=task.task_id)
        task.status = result.status
        self.results[task_id] = result
        return result

    def run_all(self) -> list[TaskResult]:
        results: list[TaskResult] = []
        for task_id, task in self.tasks.items():
            if task.status == TaskStatus.PENDING:
                results.append(self.run_task(task_id))
        return results

