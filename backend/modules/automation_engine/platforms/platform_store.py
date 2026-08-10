from __future__ import annotations

from .platform_models import PlatformAccount


class PlatformStore:
    def __init__(self) -> None:
        self._accounts: dict[str, PlatformAccount] = {}
        self._seed_demo_accounts()

    def list_accounts(self, platform_id: str) -> list[dict[str, str | int | bool]]:
        return [
            account.to_dict()
            for account in self._accounts.values()
            if account.platform == platform_id
        ]

    def create_account(
        self,
        platform_id: str,
        phone: str,
        status: str = "active",
        daily_limit: int = 50,
    ) -> dict[str, str | int | bool]:
        account_id = f"{platform_id}_{phone}"
        active = status == "active"
        account = PlatformAccount(
            account_id=account_id,
            platform=platform_id,
            phone=phone,
            status=status,
            daily_limit=daily_limit,
            active=active,
        )
        self._accounts[account_id] = account
        return account.to_dict()

    def _seed_demo_accounts(self) -> None:
        # Runtime startup must never manufacture account state.
        return None


platform_store = PlatformStore()
