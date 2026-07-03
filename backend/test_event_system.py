from __future__ import annotations

from pathlib import Path

from modules.automation_engine.dispatcher import create_default_dispatcher
from modules.automation_engine.events import (
    ACTION_COMPLETED,
    ACTION_FAILED,
    ACTION_STARTED,
    STEP_COMPLETED,
    STEP_FAILED,
    STEP_STARTED,
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_STARTED,
    global_event_bus,
)
from modules.automation_engine.queue import TaskQueue
from modules.automation_engine.scenario_engine import ScenarioEngine
from modules.automation_engine.scheduler import Scheduler
from modules.automation_engine.worker import Worker


def main() -> None:
    backend_dir = Path(__file__).resolve().parent
    scenario_path = backend_dir / "scenarios" / "example.json"
    captured_events: list[str] = []

    def record_event(payload: dict) -> None:
        event_name = payload["event"]
        action = payload.get("action", "-")
        step = payload.get("step_index", "-")
        message = payload.get("message", "")
        captured_events.append(f"{event_name} | step={step} | action={action} | {message}")

    for event_name in (
        TASK_STARTED,
        TASK_COMPLETED,
        TASK_FAILED,
        STEP_STARTED,
        STEP_COMPLETED,
        STEP_FAILED,
        ACTION_STARTED,
        ACTION_COMPLETED,
        ACTION_FAILED,
    ):
        global_event_bus.subscribe(event_name, record_event)

    queue = TaskQueue()
    scheduler = Scheduler(queue)
    dispatcher = create_default_dispatcher()
    scenario_engine = ScenarioEngine(dispatcher)
    worker = Worker(queue, scheduler, scenario_engine)

    task = scheduler.schedule_task({"scenario_path": str(scenario_path)})
    print(f"Created event test task: {task['task_id']}")

    worker.run_once()

    print("\nCaptured Event Flow")
    print("-------------------")
    for event in captured_events:
        print(event)

    if TASK_STARTED not in [event.split(" | ")[0] for event in captured_events]:
        raise RuntimeError("TASK_STARTED event was not captured")
    if TASK_COMPLETED not in [event.split(" | ")[0] for event in captured_events]:
        raise RuntimeError("TASK_COMPLETED event was not captured")
    if STEP_STARTED not in [event.split(" | ")[0] for event in captured_events]:
        raise RuntimeError("STEP_STARTED event was not captured")
    if STEP_COMPLETED not in [event.split(" | ")[0] for event in captured_events]:
        raise RuntimeError("STEP_COMPLETED event was not captured")

    print("\nEvent system verified")


if __name__ == "__main__":
    main()
