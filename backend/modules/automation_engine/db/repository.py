from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .database import get_connection


def _serialize_datetime(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _row_to_dict(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


class AutomationRepository:
    def create_task(self, task: dict[str, Any]) -> None:
        with get_connection() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO tasks (
                    task_id,
                    scenario_path,
                    status,
                    created_at,
                    run_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    task["task_id"],
                    task["scenario_path"],
                    task["status"],
                    _serialize_datetime(task["created_at"]),
                    _serialize_datetime(task.get("run_at")),
                ),
            )
            connection.execute(
                """
                INSERT OR REPLACE INTO task_state (task_id, current_step, status)
                VALUES (?, ?, ?)
                """,
                (task["task_id"], -1, task["status"]),
            )
            connection.commit()

    def update_task_status(self, task_id: str, status: str) -> None:
        with get_connection() as connection:
            connection.execute(
                "UPDATE tasks SET status = ? WHERE task_id = ?",
                (status, task_id),
            )
            connection.execute(
                """
                INSERT INTO task_state (task_id, current_step, status)
                VALUES (?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET status = excluded.status
                """,
                (task_id, -1, status),
            )
            connection.commit()

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with get_connection() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            return _row_to_dict(row)

    def get_all_tasks(self) -> list[dict[str, Any]]:
        with get_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks ORDER BY created_at ASC"
            ).fetchall()
            return [dict(row) for row in rows]

    def insert_log(
        self,
        task_id: str,
        step_index: int | None,
        action: str | None,
        message: str,
    ) -> None:
        with get_connection() as connection:
            connection.execute(
                """
                INSERT INTO task_logs (
                    task_id,
                    step_index,
                    action,
                    message,
                    timestamp
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    step_index,
                    action,
                    message,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            connection.commit()

    def update_task_state(self, task_id: str, current_step: int, status: str) -> None:
        with get_connection() as connection:
            connection.execute(
                """
                INSERT INTO task_state (task_id, current_step, status)
                VALUES (?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    current_step = excluded.current_step,
                    status = excluded.status
                """,
                (task_id, current_step, status),
            )
            connection.commit()

    def get_task_logs(self, task_id: str) -> list[dict[str, Any]]:
        with get_connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM task_logs
                WHERE task_id = ?
                ORDER BY id ASC
                """,
                (task_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_task_state(self, task_id: str) -> dict[str, Any] | None:
        with get_connection() as connection:
            row = connection.execute(
                "SELECT * FROM task_state WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            return _row_to_dict(row)
