from __future__ import annotations

from pathlib import Path

from modules.automation_engine.db.repository import AutomationRepository
from modules.automation_engine.dispatcher import create_default_dispatcher
from modules.automation_engine.queue import TaskQueue
from modules.automation_engine.scenario_engine import ScenarioEngine
from modules.automation_engine.scheduler import Scheduler
from modules.automation_engine.worker import Worker


def main() -> None:
    backend_dir = Path(__file__).resolve().parent
    scenario_path = backend_dir / "scenarios" / "example.json"

    repository = AutomationRepository()
    queue = TaskQueue(repository=repository)
    scheduler = Scheduler(queue)
    dispatcher = create_default_dispatcher()
    scenario_engine = ScenarioEngine(dispatcher)
    worker = Worker(queue, scheduler, scenario_engine)

    task = scheduler.schedule_task({"scenario_path": str(scenario_path)})
    print(f"Created task: {task['task_id']}")

    worker.run_once()

    stored_task = repository.get_task(task["task_id"])
    stored_logs = repository.get_task_logs(task["task_id"])
    stored_state = repository.get_task_state(task["task_id"])

    print("\nStored Task")
    print("-----------")
    print(stored_task)

    print("\nStored State")
    print("------------")
    print(stored_state)

    print("\nStored Logs")
    print("-----------")
    for log in stored_logs:
        print(
            f"{log['id']} | step={log['step_index']} | "
            f"action={log['action']} | {log['message']}"
        )

    if not stored_task:
        raise RuntimeError("Task was not stored in SQLite")
    if stored_task["status"] != "done":
        raise RuntimeError(f"Expected stored task status done, got {stored_task['status']}")
    if not stored_logs:
        raise RuntimeError("No task logs were stored in SQLite")
    if not stored_state:
        raise RuntimeError("Task state was not stored in SQLite")
    if stored_state["status"] != "done":
        raise RuntimeError(f"Expected stored state status done, got {stored_state['status']}")

    print("\nDB integration verified")


if __name__ == "__main__":
    main()
