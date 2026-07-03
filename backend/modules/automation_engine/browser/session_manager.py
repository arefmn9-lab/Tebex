from __future__ import annotations

from pathlib import Path
from typing import Any


class SessionManager:
    def __init__(self, session_dir: str | Path | None = None) -> None:
        backend_dir = Path(__file__).resolve().parents[3]
        self.session_dir = Path(session_dir or backend_dir / "runtime" / "browser_sessions")
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self._sessions: dict[str, dict[str, Any]] = {}

    def get_session_path(self, account_id: str) -> Path:
        safe_account_id = "".join(
            char if char.isalnum() or char in {"-", "_"} else "_"
            for char in account_id
        )
        return self.session_dir / f"{safe_account_id}.json"

    def assign_session(self, account_id: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        session = self._sessions.setdefault(account_id, {})
        if data:
            session.update(data)
        session["storage_state_path"] = str(self.get_session_path(account_id))
        return session

    def get_session(self, account_id: str) -> dict[str, Any]:
        return self.assign_session(account_id)

    def get_storage_state_path(self, account_id: str) -> Path:
        return self.get_session_path(account_id)

    def has_storage_state(self, account_id: str) -> bool:
        return self.get_session_path(account_id).exists()

    def can_save_storage_state(self, account_id: str) -> bool:
        session = self._sessions.get(account_id, {})
        return not bool(session.get("user_data_dir"))

    def save_storage_state(self, account_id: str, context: Any) -> None:
        path = self.get_storage_state_path(account_id)
        context.storage_state(path=str(path))
        self.assign_session(account_id, {"storage_state_path": str(path)})
