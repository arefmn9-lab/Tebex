from __future__ import annotations

from statistics import mean
from typing import Any

from .outcome_tracker import OutcomeTracker


class LearningEngine:
    def __init__(self, outcome_tracker: OutcomeTracker) -> None:
        self.outcome_tracker = outcome_tracker

    def success_rate_by_decision_type(
        self,
        account_id: str | None = None,
    ) -> dict[str, float]:
        grouped: dict[str, list[bool]] = {}
        for outcome in self.outcome_tracker.list_outcomes(account_id):
            key = str(outcome.get("decision_action") or "unknown")
            grouped.setdefault(key, []).append(bool(outcome["success"]))

        return {
            key: sum(values) / len(values)
            for key, values in grouped.items()
            if values
        }

    def confidence_vs_outcome(
        self,
        account_id: str | None = None,
    ) -> dict[str, float | None]:
        successes = []
        failures = []
        for outcome in self.outcome_tracker.list_outcomes(account_id):
            confidence = outcome.get("decision_confidence")
            if confidence is None:
                continue
            if outcome["success"]:
                successes.append(float(confidence))
            else:
                failures.append(float(confidence))

        return {
            "average_success_confidence": mean(successes) if successes else None,
            "average_failure_confidence": mean(failures) if failures else None,
        }

    def account_performance_patterns(self, account_id: str) -> dict[str, Any]:
        outcomes = self.outcome_tracker.list_outcomes(account_id)
        if not outcomes:
            return {
                "account_id": account_id,
                "total_runs": 0,
                "success_rate": None,
                "decision_success_rates": {},
                "confidence_patterns": {},
            }

        successes = [bool(outcome["success"]) for outcome in outcomes]
        return {
            "account_id": account_id,
            "total_runs": len(outcomes),
            "success_rate": sum(successes) / len(successes),
            "decision_success_rates": self.success_rate_by_decision_type(account_id),
            "confidence_patterns": self.confidence_vs_outcome(account_id),
        }

