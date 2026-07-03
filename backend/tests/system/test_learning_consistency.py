from __future__ import annotations

import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_DIR))

from modules.automation_engine.learning.decision_logger import DecisionLogger
from modules.automation_engine.learning.learning_engine import LearningEngine
from modules.automation_engine.learning.outcome_tracker import OutcomeTracker
from modules.automation_engine.learning.prompt_optimizer import PromptOptimizer


class LearningConsistencyTest(unittest.TestCase):
    def test_decisions_outcomes_and_learning_links_are_consistent(self) -> None:
        decisions = DecisionLogger()
        outcomes = OutcomeTracker()
        learning = LearningEngine(outcomes)
        optimizer = PromptOptimizer(learning)

        decision_a = decisions.log_decision(
            account_id="learn_a",
            task_id="task_a",
            scenario="scenario_a",
            ai_decision={"intent": "greeting", "action": "send_message", "confidence": 0.9},
        )
        decision_b = decisions.log_decision(
            account_id="learn_a",
            task_id="task_b",
            scenario="scenario_b",
            ai_decision={"intent": "support", "action": "collect_details", "confidence": 0.4},
        )

        outcome_a = outcomes.track_outcome(
            decision_a,
            success=True,
            logs=[
                "2026-01-01T00:00:00+00:00 Step 0 started: log",
                "2026-01-01T00:00:01+00:00 Step 0 completed: ok",
            ],
        )
        outcome_b = outcomes.track_outcome(
            decision_b,
            success=False,
            logs=["2026-01-01T00:00:02+00:00 Step 0 failed: no route"],
            error_reason="no route",
        )

        self.assertEqual(outcome_a["decision_id"], decision_a["decision_id"])
        self.assertEqual(outcome_b["decision_id"], decision_b["decision_id"])
        self.assertEqual(len(decisions.list_decisions("learn_a")), 2)
        self.assertEqual(len(outcomes.list_outcomes("learn_a")), 2)

        rates = learning.success_rate_by_decision_type("learn_a")
        self.assertEqual(rates["send_message"], 1.0)
        self.assertEqual(rates["collect_details"], 0.0)

        confidence = learning.confidence_vs_outcome("learn_a")
        self.assertEqual(confidence["average_success_confidence"], 0.9)
        self.assertEqual(confidence["average_failure_confidence"], 0.4)

        suggestions = optimizer.suggest_improvements("learn_a")
        self.assertEqual(suggestions["suggested_action"], "send_message")
        self.assertTrue(suggestions["suggestions"])


if __name__ == "__main__":
    unittest.main()

