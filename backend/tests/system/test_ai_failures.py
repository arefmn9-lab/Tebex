from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_DIR))

from modules.automation_engine.ai_integration import AIExecutionMiddleware
from modules.automation_engine.dispatcher import create_default_dispatcher
from modules.automation_engine.queue import TaskQueue
from modules.automation_engine.scenario_engine import ScenarioEngine
from modules.automation_engine.scheduler import Scheduler
from modules.automation_engine.worker import Worker


def write_safe_scenario() -> str:
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    with handle:
        json.dump(
            {
                "name": "ai_failure_safe",
                "version": "1.0",
                "platform": "system",
                "steps": [{"action": "log", "params": {"text": "should not always run"}}],
            },
            handle,
        )
    return handle.name


class DenyMiddleware(AIExecutionMiddleware):
    def before_execution(self, task, account_id, scenario):
        return {
            "decision": {"intent": "test_deny", "action": "deny", "approve": False},
            "scenario": scenario,
        }


class MalformedMiddleware(AIExecutionMiddleware):
    def before_execution(self, task, account_id, scenario):
        return {"decision": "malformed", "scenario": scenario}


class TimeoutMiddleware(AIExecutionMiddleware):
    def before_execution(self, task, account_id, scenario):
        raise TimeoutError("AI decision timed out")


class AIFailureTest(unittest.TestCase):
    def run_worker_with(self, middleware: AIExecutionMiddleware) -> list[dict]:
        queue = TaskQueue()
        scheduler = Scheduler(queue)
        worker = Worker(
            queue,
            scheduler,
            ScenarioEngine(create_default_dispatcher()),
            ai_middleware=middleware,
        )
        scheduler.schedule_task({"scenario_path": write_safe_scenario()})
        worker.run_once()
        return queue.get_all_tasks()

    def test_approve_false_skips_task_without_crashing(self) -> None:
        tasks = self.run_worker_with(DenyMiddleware())
        self.assertEqual(tasks[0]["status"], "failed")
        self.assertTrue(any("AI did not approve" in log for log in tasks[0]["logs"]))

    def test_malformed_decision_fails_safely(self) -> None:
        tasks = self.run_worker_with(MalformedMiddleware())
        self.assertEqual(tasks[0]["status"], "failed")
        self.assertTrue(any("Task execution error" in log for log in tasks[0]["logs"]))

    def test_ai_timeout_fails_safely(self) -> None:
        tasks = self.run_worker_with(TimeoutMiddleware())
        self.assertEqual(tasks[0]["status"], "failed")
        self.assertTrue(any("AI decision timed out" in log for log in tasks[0]["logs"]))


if __name__ == "__main__":
    unittest.main()

