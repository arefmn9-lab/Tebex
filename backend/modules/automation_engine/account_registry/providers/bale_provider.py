from __future__ import annotations

from typing import Any

from modules.automation_engine.plugins.bale.account_store import bale_account_store


class BaleAccountRegistryProvider:
    """Registry adapter over the production BaleAccountStore."""

    platform_id = "bale"

    def __init__(self, store: Any | None = None) -> None:
        self.store = store or bale_account_store

    def list_accounts(self) -> list[dict[str, Any]]:
        return self.store.list_accounts()

    def get_account(self, account_id: str) -> dict[str, Any] | None:
        return self.store.get_account(account_id)

    def create_account(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.store.create_account(payload)

    def update_account(self, account_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.store.update_account(account_id, payload)

    def delete_account(self, account_id: str) -> dict[str, Any]:
        return self.store.delete_account(account_id)
