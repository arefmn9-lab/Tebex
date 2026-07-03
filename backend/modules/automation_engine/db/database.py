from __future__ import annotations

import sqlite3
from pathlib import Path

from .models import initialize_schema


DATABASE_PATH = Path(__file__).resolve().parents[3] / "clinicos.db"


def get_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    initialize_schema(connection)
    return connection

