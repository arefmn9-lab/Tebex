from __future__ import annotations


class MemoryStore:
    def __init__(self) -> None:
        self._messages: dict[str, list[dict[str, str]]] = {}

    def update_memory(self, account_id: str, role: str, content: str) -> None:
        self._messages.setdefault(account_id, []).append(
            {"role": role, "content": content}
        )

    def get_history(self, account_id: str) -> list[dict[str, str]]:
        return list(self._messages.get(account_id, []))

    def get_last_messages(self, account_id: str, limit: int = 10) -> list[dict[str, str]]:
        if limit <= 0:
            return []
        return self.get_history(account_id)[-limit:]

    def reset(self, account_id: str) -> None:
        self._messages.pop(account_id, None)

