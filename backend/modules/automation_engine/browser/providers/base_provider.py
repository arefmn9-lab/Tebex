from __future__ import annotations

from typing import Any, Protocol


class BrowserProvider(Protocol):
    provider_id: str

    def health_check(self) -> dict[str, Any]:
        ...

    def open_profile(self, account_id: str, profile_id: str, url: str | None = None) -> dict[str, Any]:
        ...

    def close_profile(self, account_id: str, profile_id: str) -> dict[str, Any]:
        ...

    def get_profile_status(self, account_id: str, profile_id: str) -> dict[str, Any]:
        ...
