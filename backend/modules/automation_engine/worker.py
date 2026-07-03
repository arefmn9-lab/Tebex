from __future__ import annotations

import time
from pathlib import Path
from re import search

from .accounts.account_context import AccountContext
from .accounts.account_manager import AccountManager
from .ai_integration import AIExecutionMiddleware
from .events import TASK_COMPLETED, TASK_FAILED, TASK_STARTED, global_event_bus
from .queue import DEFAULT_ACCOUNT_ID, TaskQueue, TaskRecord
from .schemas import ScenarioDefinition
from .scenario_engine import ScenarioEngine
from .scheduler import Scheduler


class Worker:
    def __init__(
        self,
        queue: TaskQueue,
        scheduler: Scheduler,
        scenario_engine: ScenarioEngine,
        account_manager: AccountManager | None = None,
        ai_middleware: AIExecutionMiddleware | None = None,
    ) -> None:
        self.queue = queue
        self.scheduler = scheduler
        self.scenario_engine = scenario_engine
        self.account_manager = account_manager or AccountManager()
        self.ai_middleware = ai_middleware or AIExecutionMiddleware()
        self.current_account_context: AccountContext | None = None

    def run_once(self) -> list[TaskRecord]:
        executed_tasks: list[TaskRecord] = []
        for task in self.scheduler.get_ready_tasks():
            task_id = task["task_id"]
            account_id = task.get("account_id", DEFAULT_ACCOUNT_ID)
            account_context = self._select_account_context(account_id)
            scenario_path = Path(task["scenario_path"])
            print(
                f"[WORKER] Starting task {task_id} "
                f"for account {account_context.account_id}: {scenario_path}"
            )
            self.queue.mark_task_running(task_id)
            global_event_bus.emit(
                TASK_STARTED,
                {
                    "task_id": task_id,
                    "account_id": account_context.account_id,
                    "platform": account_context.platform,
                    "scenario_path": str(scenario_path),
                },
            )

            try:
                scenario = self.scenario_engine.load_scenario(scenario_path)
                ai_execution = self.ai_middleware.before_execution(
                    task,
                    account_context.account_id,
                    scenario,
                )
                ai_decision = ai_execution["decision"]
                if not ai_decision.get("approve", True):
                    message = f"AI did not approve task execution: {ai_decision}"
                    self.queue.append_log(task_id, message)
                    self.queue.mark_task_failed(task_id)
                    self.ai_middleware.after_execution(
                        account_context.account_id,
                        task,
                        error=message,
                    )
                    global_event_bus.emit(
                        TASK_FAILED,
                        {
                            "task_id": task_id,
                            "account_id": account_context.account_id,
                            "scenario_path": str(scenario_path),
                            "message": message,
                        },
                    )
                    print(f"[WORKER] Task {task_id} skipped: {message}")
                    executed = self._find_task(task_id)
                    if executed is not None:
                        executed_tasks.append(executed)
                    self._clear_account_context()
                    continue

                scenario = ai_execution["scenario"]
                result = self.scenario_engine.execute(scenario, task_id=task_id)
                for log_entry in result.logs:
                    self.queue.append_log(task_id, log_entry, add_timestamp=False)
                self._persist_result_logs(task_id, scenario, result.logs)
                self.ai_middleware.after_execution(
                    account_context.account_id,
                    task,
                    result=result,
                )

                if result.ok:
                    self.queue.mark_task_done(task_id)
                    global_event_bus.emit(
                        TASK_COMPLETED,
                        {
                            "task_id": task_id,
                            "account_id": account_context.account_id,
                            "scenario_path": str(scenario_path),
                            "message": result.message,
                        },
                    )
                    print(f"[WORKER] Task {task_id} done")
                else:
                    self.queue.mark_task_failed(task_id)
                    global_event_bus.emit(
                        TASK_FAILED,
                        {
                            "task_id": task_id,
                            "account_id": account_context.account_id,
                            "scenario_path": str(scenario_path),
                            "message": result.message,
                        },
                    )
                    print(f"[WORKER] Task {task_id} failed: {result.message}")
            except Exception as exc:
                self.queue.append_log(task_id, f"Task execution error: {exc}")
                self.queue.mark_task_failed(task_id)
                self.ai_middleware.after_execution(
                    account_context.account_id,
                    task,
                    error=str(exc),
                )
                global_event_bus.emit(
                    TASK_FAILED,
                    {
                        "task_id": task_id,
                        "account_id": account_context.account_id,
                        "scenario_path": str(scenario_path),
                        "message": str(exc),
                    },
                )
                print(f"[WORKER] Task {task_id} failed: {exc}")

            self._clear_account_context()
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

    def _select_account_context(self, account_id: str) -> AccountContext:
        account_context = self.account_manager.get_or_create_account(account_id)
        if self.current_account_context is not None:
            self.current_account_context.deactivate()
        account_context.activate()
        self.current_account_context = account_context
        return account_context

    def _clear_account_context(self) -> None:
        if self.current_account_context is not None:
            self.current_account_context.deactivate()
        self.current_account_context = None

    def _persist_result_logs(
        self,
        task_id: str,
        scenario: ScenarioDefinition,
        logs: list[str],
    ) -> None:
        for log_entry in logs:
            step_index = -1
            action = "scenario"
            match = search(r"Step (\d+) (started|completed):", log_entry)
            if match:
                step_index = int(match.group(1))
                if step_index < len(scenario.steps):
                    action = scenario.steps[step_index].action

            try:
                self.queue.repository.insert_log(task_id, step_index, action, log_entry)
                if step_index >= 0:
                    self.queue.repository.update_task_state(task_id, step_index, "running")
            except Exception as exc:
                print(f"[WORKER] Persistence skipped: {exc}")
