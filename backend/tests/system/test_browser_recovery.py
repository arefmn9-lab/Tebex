from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_DIR))

from modules.automation_engine.browser import actions_browser
from modules.automation_engine.dispatcher import create_default_dispatcher
from modules.automation_engine.queue import TaskQueue
from modules.automation_engine.scenario_engine import ScenarioEngine
from modules.automation_engine.scheduler import Scheduler
from modules.automation_engine.worker import Worker


def write_browser_scenario() -> str:
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    with handle:
        json.dump(
            {
                "name": "browser_recovery",
                "version": "1.0",
                "platform": "browser",
                "steps": [
                    {
                        "action": "open_url",
                        "params": {
                            "account_id": "browser_acct",
                            "url": "https://example.test",
                        },
                    }
                ],
            },
            handle,
        )
    return handle.name


class CrashingBrowserManager:
    def is_available(self) -> bool:
        return True

    def get_page(self, *args, **kwargs):
        raise RuntimeError("simulated browser crash")


class FakePage:
    def goto(self, url: str, wait_until: str = "load") -> None:
        self.url = url

    def title(self) -> str:
        return "Recovered"


class RecoveringBrowserManager:
    def is_available(self) -> bool:
        return True

    def get_page(self, *args, **kwargs):
        return FakePage()

    def save_session(self, account_id: str) -> None:
        self.saved_account_id = account_id


class BrowserRecoveryTest(unittest.TestCase):
    def run_browser_task(self) -> str:
        queue = TaskQueue()
        scheduler = Scheduler(queue)
        worker = Worker(queue, scheduler, ScenarioEngine(create_default_dispatcher()))
        scheduler.schedule_task(
            {"account_id": "browser_acct", "scenario_path": write_browser_scenario()}
        )
        worker.run_once()
        return queue.get_all_tasks()[0]["status"]

    def test_browser_crash_fails_safely_then_recovers_with_new_session(self) -> None:
        with patch.object(actions_browser, "browser_manager", CrashingBrowserManager()):
            self.assertEqual(self.run_browser_task(), "failed")

        with patch.object(actions_browser, "browser_manager", RecoveringBrowserManager()):
            self.assertEqual(self.run_browser_task(), "done")


if __name__ == "__main__":
    unittest.main()

