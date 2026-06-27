from pathlib import Path


class SessionManager:
    def __init__(self, base_path: str | Path | None = None):
        backend_root = Path(__file__).resolve().parents[3]
        self.base_path = Path(base_path or backend_root / "runtime" / "sessions")
        self.base_path.mkdir(parents=True, exist_ok=True)

    def save_session(self, account_id: str | int, platform: str, context):
        path = self._session_path(platform, account_id)
        context.storage_state(path=str(path))
        return path

    def load_session(self, account_id: str | int, platform: str):
        path = self._session_path(platform, account_id)
        if not path.exists():
            return None
        return str(path)

    def create_context(self, client, account_id: str | int, platform: str):
        storage_state = self.load_session(account_id, platform)
        return client.new_context(storage_state=storage_state)

    def _session_path(self, platform: str, account_id: str | int):
        return self.base_path / f"{self._key(platform, account_id)}.json"

    @staticmethod
    def _key(platform: str, account_id: str | int):
        clean_platform = str(platform).strip().lower().replace(" ", "_")
        clean_account = str(account_id).strip().replace(" ", "_")
        return f"{clean_platform}_{clean_account}"
