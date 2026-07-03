from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


class DecisionLogger:
    def __init__(self) -> None:
        self._decisions: list[dict[str, Any]] = []

    def log_decision(
        self,
        account_id: str,
        task_id: str,
        scenario: str,
        ai_decision: dict[str, Any],
    ) -> dict[str, Any]:
        record = {
            "decision_id": str(uuid4()),
            "account_id": account_id,
            "task_id": task_id,
            "scenario": scenario,
            "ai_decision": dict(ai_decision),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._decisions.append(record)
        return record

    def get_decision(self, decision_id: str) -> dict[str, Any] | None:
        for record in self._decisions:
            if record["decision_id"] == decision_id:
                return dict(record)
        return None

    def get_decision_for_task(self, task_id: str) -> dict[str, Any] | None:
        for record in reversed(self._decisions):
            if record["task_id"] == task_id:
                return dict(record)
        return None

    def list_decisions(self, account_id: str | None = None) -> list[dict[str, Any]]:
        if account_id is None:
            return [dict(record) for record in self._decisions]
        return [
            dict(record)
            for record in self._decisions
            if record["account_id"] == account_id
        ]

