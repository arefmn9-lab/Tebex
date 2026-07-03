from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_DIR))

from modules.automation_engine.ai_integration import AIExecutionMiddleware
from modules.automation_engine.db.repository import AutomationRepository
from modules.automation_engine.dispatcher import create_default_dispatcher
from modules.automation_engine.queue import TaskQueue
from modules.automation_engine.scenario_engine import ScenarioEngine
from modules.automation_engine.scheduler import Scheduler
from modules.automation_engine.worker import Worker


def write_scenario(data: dict) -> str:
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    with handle:
        json.dump(data, handle)
    return handle.name


class EndToEndFlowTest(unittest.TestCase):
    def test_full_pipeline_records_execution_and_learning(self) -> None:
        scenario_path = write_scenario(
            {
                "name": "system_e2e_browser_safe",
                "version": "1.0",
                "platform": "system",
                "steps": [
                    {"action": "browser_wait", "params": {"account_id": "e2e_acct", "seconds": 0}},
                    {"action": "log", "params": {"text": "e2e complete"}},
                ],
            }
        )

        middleware = AIExecutionMiddleware()
        queue = TaskQueue()
        scheduler = Scheduler(queue)
        worker = Worker(
            queue,
            scheduler,
            ScenarioEngine(create_default_dispatcher()),
            ai_middleware=middleware,
        )

        task = scheduler.schedule_task(
            {"account_id": "e2e_acct", "scenario_path": scenario_path}
        )
        worker.run_once()

        stored_task = AutomationRepository().get_task(task["task_id"])
        stored_logs = AutomationRepository().get_task_logs(task["task_id"])
        decisions = middleware.decision_logger.list_decisions("e2e_acct")
        outcomes = middleware.outcome_tracker.list_outcomes("e2e_acct")
        patterns = middleware.learning_engine.account_performance_patterns("e2e_acct")

        self.assertEqual(queue.get_all_tasks()[0]["status"], "done")
        self.assertIsNotNone(stored_task)
        self.assertEqual(stored_task["status"], "done")
        self.assertGreater(len(stored_logs), 0)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["decision_id"], decisions[0]["decision_id"])
        self.assertEqual(patterns["success_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()

