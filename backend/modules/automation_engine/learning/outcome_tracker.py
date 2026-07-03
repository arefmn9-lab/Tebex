from __future__ import annotations

from datetime import datetime
from typing import Any


class OutcomeTracker:
    def __init__(self) -> None:
        self._outcomes: list[dict[str, Any]] = []

    def track_outcome(
        self,
        decision_record: dict[str, Any],
        success: bool,
        logs: list[str] | None = None,
        error_reason: str | None = None,
    ) -> dict[str, Any]:
        logs = logs or []
        outcome = {
            "decision_id": decision_record["decision_id"],
            "account_id": decision_record["account_id"],
            "task_id": decision_record["task_id"],
            "decision_action": decision_record["ai_decision"].get("action"),
            "decision_intent": decision_record["ai_decision"].get("intent"),
            "decision_confidence": decision_record["ai_decision"].get("confidence"),
            "success": success,
            "step_results": self._extract_step_results(logs),
            "execution_time": self._estimate_execution_time(logs),
            "error_reason": error_reason,
            "timestamp": datetime.now().astimezone().isoformat(),
        }
        self._outcomes.append(outcome)
        return outcome

    def list_outcomes(self, account_id: str | None = None) -> list[dict[str, Any]]:
        if account_id is None:
            return [dict(outcome) for outcome in self._outcomes]
        return [
            dict(outcome)
            for outcome in self._outcomes
            if outcome["account_id"] == account_id
        ]

    def _extract_step_results(self, logs: list[str]) -> list[dict[str, Any]]:
        step_results: list[dict[str, Any]] = []
        for log_entry in logs:
            if "Step " not in log_entry:
                continue
            status = "completed" if " completed:" in log_entry else "started"
            if " failed:" in log_entry:
                status = "failed"
            step_results.append({"status": status, "message": log_entry})
        return step_results

    def _estimate_execution_time(self, logs: list[str]) -> float | None:
        timestamps = []
        for log_entry in logs:
            raw_timestamp = log_entry.split(" ", 1)[0]
            try:
                timestamps.append(datetime.fromisoformat(raw_timestamp))
            except ValueError:
                continue

        if len(timestamps) < 2:
            return None
        return (timestamps[-1] - timestamps[0]).total_seconds()

