from __future__ import annotations

from .account_context import AccountContext


class AccountManager:
    def __init__(self) -> None:
        self._accounts: dict[str, AccountContext] = {}

    def create_account(self, account_id: str, platform: str) -> AccountContext:
        context = AccountContext(account_id=account_id, platform=platform)
        self._accounts[account_id] = context
        return context

    def get_account(self, account_id: str) -> AccountContext | None:
        return self._accounts.get(account_id)

    def get_or_create_account(self, account_id: str, platform: str = "default") -> AccountContext:
        account = self.get_account(account_id)
        if account is not None:
            return account
        return self.create_account(account_id, platform)

    def update_account_state(self, account_id: str, runtime_state: dict) -> AccountContext:
        account = self.get_or_create_account(account_id)
        account.update_runtime_state(runtime_state)
        return account

    def list_accounts(self) -> list[AccountContext]:
        return list(self._accounts.values())

