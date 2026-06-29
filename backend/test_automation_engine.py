from __future__ import annotations

from pathlib import Path

from modules.automation_engine.dispatcher import create_default_dispatcher
from modules.automation_engine.scenario_engine import ScenarioEngine
from modules.automation_engine.task_runner import InMemoryTaskRunner


def main() -> None:
    backend_dir = Path(__file__).resolve().parent
    scenario_path = backend_dir / "scenarios" / "example.json"

    dispatcher = create_default_dispatcher()
    scenario_engine = ScenarioEngine(dispatcher)
    task_runner = InMemoryTaskRunner(scenario_engine)

    scenario = scenario_engine.load_scenario(scenario_path)
    task = task_runner.add_task(scenario)
    result = task_runner.run_task(task.task_id)

    print("\nAutomation Engine Logs")
    print("----------------------")
    for log_entry in result.logs:
        print(log_entry)

    print("\nAutomation Engine Result")
    print("------------------------")
    print(f"task_id: {result.task_id}")
    print(f"status: {result.status.value}")
    print(f"ok: {result.ok}")
    print(f"message: {result.message}")


if __name__ == "__main__":
    main()

