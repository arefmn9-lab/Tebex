from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_DIR))

from modules.automation_engine.accounts.account_manager import AccountManager
from modules.automation_engine.ai_integration import AIExecutionMiddleware
from modules.automation_engine.dispatcher import create_default_dispatcher
from modules.automation_engine.queue import TaskQueue
from modules.automation_engine.scenario_engine import ScenarioEngine
from modules.automation_engine.scheduler import Scheduler
from modules.automation_engine.worker import Worker


def write_scenario(account_id: str) -> str:
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    with handle:
        json.dump(
            {
                "name": f"load_{account_id}",
                "version": "1.0",
                "platform": "system",
                "steps": [
                    {
                        "action": "log",
                        "params": {"text": f"account scoped task for {account_id}"},
                    }
                ],
            },
            handle,
        )
    return handle.name


class MultiAccountLoadTest(unittest.TestCase):
    def test_multiple_accounts_remain_isolated_under_load(self) -> None:
        account_ids = [f"acct_{index}" for index in range(5)]
        account_manager = AccountManager()
        queue = TaskQueue()
        scheduler = Scheduler(queue)
        middleware = AIExecutionMiddleware()
        worker = Worker(
            queue,
            scheduler,
            ScenarioEngine(create_default_dispatcher()),
            account_manager=account_manager,
            ai_middleware=middleware,
        )

        for account_id in account_ids:
            account_manager.create_account(account_id, "demo")
            scheduler.schedule_task(
                {"account_id": account_id, "scenario_path": write_scenario(account_id)}
            )

        worker.run_once()

        self.assertEqual(set(queue.list_account_ids()), set(account_ids))
        self.assertIsNone(worker.current_account_context)
        for account_id in account_ids:
            account_tasks = queue.get_tasks_for_account(account_id)
            self.assertEqual(len(account_tasks), 1)
            self.assertEqual(account_tasks[0]["account_id"], account_id)
            self.assertEqual(account_tasks[0]["status"], "done")
            account = account_manager.get_account(account_id)
            self.assertIsNotNone(account)
            self.assertFalse(account.runtime_state.get("active", False))
            self.assertEqual(len(middleware.decision_logger.list_decisions(account_id)), 1)


if __name__ == "__main__":
    unittest.main()

