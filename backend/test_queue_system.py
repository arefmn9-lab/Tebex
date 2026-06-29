from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules.automation_engine.dispatcher import create_default_dispatcher
from modules.automation_engine.queue import TaskQueue
from modules.automation_engine.scenario_engine import ScenarioEngine
from modules.automation_engine.scheduler import Scheduler
from modules.automation_engine.worker import Worker


def print_queue(queue: TaskQueue) -> None:
    print("\nQueue State")
    print("-----------")
    for task in queue.get_all_tasks():
        run_at = task["run_at"].isoformat() if task.get("run_at") else "immediate"
        print(f"{task['task_id']} | {task['status']} | run_at={run_at}")
        for log_entry in task.get("logs", []):
            print(f"  {log_entry}")


def main() -> None:
    backend_dir = Path(__file__).resolve().parent
    scenario_path = backend_dir / "scenarios" / "example.json"

    queue = TaskQueue()
    scheduler = Scheduler(queue)
    dispatcher = create_default_dispatcher()
    scenario_engine = ScenarioEngine(dispatcher)
    worker = Worker(queue, scheduler, scenario_engine)

    immediate_task = scheduler.schedule_task(
        {"scenario_path": str(scenario_path)},
        run_at=None,
    )
    delayed_task = scheduler.schedule_task(
        {"scenario_path": str(scenario_path)},
        run_at=datetime.now(timezone.utc) + timedelta(seconds=5),
    )

    print("Added tasks")
    print("-----------")
    print(f"Immediate task: {immediate_task['task_id']}")
    print(f"Delayed task:   {delayed_task['task_id']}")

    print("\nWorker pass 1")
    print("-------------")
    worker.run_once()
    print_queue(queue)

    print("\nWaiting for delayed task...")
    time.sleep(5)

    print("\nWorker pass 2")
    print("-------------")
    worker.run_once()
    print_queue(queue)


if __name__ == "__main__":
    main()

