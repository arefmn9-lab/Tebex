from __future__ import annotations

import time
from pathlib import Path

from .queue import TaskQueue, TaskRecord
from .scenario_engine import ScenarioEngine
from .scheduler import Scheduler


class Worker:
    def __init__(
        self,
        queue: TaskQueue,
        scheduler: Scheduler,
        scenario_engine: ScenarioEngine,
    ) -> None:
        self.queue = queue
        self.scheduler = scheduler
        self.scenario_engine = scenario_engine

    def run_once(self) -> list[TaskRecord]:
        executed_tasks: list[TaskRecord] = []
        for task in self.scheduler.get_ready_tasks():
            task_id = task["task_id"]
            scenario_path = Path(task["scenario_path"])
            print(f"[WORKER] Starting task {task_id}: {scenario_path}")
            self.queue.mark_task_running(task_id)

            try:
                scenario = self.scenario_engine.load_scenario(scenario_path)
                result = self.scenario_engine.execute(scenario, task_id=task_id)
                for log_entry in result.logs:
                    self.queue.append_log(task_id, log_entry, add_timestamp=False)

                if result.ok:
                    self.queue.mark_task_done(task_id)
                    print(f"[WORKER] Task {task_id} done")
                else:
                    self.queue.mark_task_failed(task_id)
                    print(f"[WORKER] Task {task_id} failed: {result.message}")
            except Exception as exc:
                self.queue.append_log(task_id, f"Task execution error: {exc}")
                self.queue.mark_task_failed(task_id)
                print(f"[WORKER] Task {task_id} failed: {exc}")

            executed = self._find_task(task_id)
            if executed is not None:
                executed_tasks.append(executed)

        if not executed_tasks:
            print("[WORKER] No due tasks")

        return executed_tasks

    def run_continuous(self) -> None:
        while True:
            self.run_once()
            time.sleep(1)

    def _find_task(self, task_id: str) -> TaskRecord | None:
        for task in self.queue.get_all_tasks():
            if task["task_id"] == task_id:
                return task
        return None
