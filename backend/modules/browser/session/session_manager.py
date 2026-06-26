import json
from pathlib import Path
from typing import Any


class SessionManager:
    def __init__(self, base_path: str | Path | None = None):
        self.base_path = Path(base_path or "runtime/browser_sessions")
        self.base_path.mkdir(parents=True, exist_ok=True)

    def save_session(self, platform: str, account_id: str | int, context):
        path = self._session_path(platform, account_id)
        context.storage_state(path=str(path))
        return path

    def load_session(self, platform: str, account_id: str | int):
        path = self._session_path(platform, account_id)
        if not path.exists():
            return None
        return str(path)

    def save_cookies(
        self,
        platform: str,
        account_id: str | int,
        cookies: list[dict[str, Any]],
    ):
        path = self._cookies_path(platform, account_id)
        path.write_text(json.dumps(cookies, indent=2), encoding="utf-8")
        return path

    def load_cookies(self, platform: str, account_id: str | int):
        path = self._cookies_path(platform, account_id)
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8"))

    def _session_path(self, platform: str, account_id: str | int):
        return self.base_path / f"{self._key(platform, account_id)}.json"

    def _cookies_path(self, platform: str, account_id: str | int):
        return self.base_path / f"{self._key(platform, account_id)}.cookies.json"

    @staticmethod
    def _key(platform: str, account_id: str | int):
        clean_platform = str(platform).strip().lower().replace(" ", "_")
        clean_account = str(account_id).strip().replace(" ", "_")
        return f"{clean_platform}_{clean_account}"
