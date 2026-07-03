from __future__ import annotations

import sqlite3


CREATE_TASKS_TABLE = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    scenario_path TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    run_at TEXT
)
"""

CREATE_TASK_LOGS_TABLE = """
CREATE TABLE IF NOT EXISTS task_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    step_index INTEGER,
    action TEXT,
    message TEXT NOT NULL,
    timestamp TEXT NOT NULL
)
"""

CREATE_TASK_STATE_TABLE = """
CREATE TABLE IF NOT EXISTS task_state (
    task_id TEXT PRIMARY KEY,
    current_step INTEGER,
    status TEXT NOT NULL
)
"""


def initialize_schema(connection: sqlite3.Connection) -> None:
    connection.execute(CREATE_TASKS_TABLE)
    connection.execute(CREATE_TASK_LOGS_TABLE)
    connection.execute(CREATE_TASK_STATE_TABLE)
    connection.commit()

