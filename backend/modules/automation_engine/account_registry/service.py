from __future__ import annotations

from typing import Any

from modules.automation_engine.account_registry.providers import BaleAccountRegistryProvider, GenericPlatformAccountRegistryProvider


class AccountRegistryService:
    def __init__(
        self,
        bale_provider: BaleAccountRegistryProvider | None = None,
        generic_provider: GenericPlatformAccountRegistryProvider | None = None,
    ) -> None:
        self.bale_provider = bale_provider or BaleAccountRegistryProvider()
        self.generic_provider = generic_provider or GenericPlatformAccountRegistryProvider()

    def list_platform_accounts(self, platform_id: str) -> list[dict[str, Any]]:
        platform = self._platform(platform_id)
        if platform == "bale":
            return self.bale_provider.list_accounts()
        return self.generic_provider.list_accounts(platform)

    def get_platform_account(self, platform_id: str, account_id: str) -> dict[str, Any] | None:
        platform = self._platform(platform_id)
        if platform == "bale":
            return self.bale_provider.get_account(account_id)
        return self.generic_provider.get_account(platform, account_id)

    def create_platform_account(self, platform_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        platform = self._platform(platform_id)
        if platform == "bale":
            return self.bale_provider.create_account(payload)
        return self.generic_provider.create_account(platform, payload)

    def update_platform_account(self, platform_id: str, account_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        platform = self._platform(platform_id)
        if platform == "bale":
            return self.bale_provider.update_account(account_id, payload)
        return self.generic_provider.update_account(platform, account_id, payload)

    def delete_platform_account(self, platform_id: str, account_id: str) -> dict[str, Any]:
        platform = self._platform(platform_id)
        if platform == "bale":
            return self.bale_provider.delete_account(account_id)
        return self.generic_provider.delete_account(platform, account_id)

    def summarize_platform_accounts(self, platform_id: str) -> dict[str, Any]:
        platform = self._platform(platform_id)
        accounts = self.list_platform_accounts(platform)
        active = sum(1 for account in accounts if self._active(account))
        inactive = len(accounts) - active
        return {"platform_id": platform, "total": len(accounts), "active": active, "inactive": inactive}

    def summarize_all_platforms(self, platform_ids: list[str] | None = None) -> dict[str, Any]:
        platforms = platform_ids or ["bale", "telegram", "whatsapp", "eitaa", "rubika", "soroush", "instagram"]
        summaries = [self.summarize_platform_accounts(platform_id) for platform_id in platforms]
        total = sum(item["total"] for item in summaries)
        active = sum(item["active"] for item in summaries)
        inactive = sum(item["inactive"] for item in summaries)
        return {
            "total_accounts": total,
            "active_accounts": active,
            "inactive_accounts": inactive,
            "platforms_with_accounts": sum(1 for item in summaries if item["total"] > 0),
            "platforms": summaries,
        }

    def _active(self, account: dict[str, Any]) -> bool:
        if "active" in account:
            return bool(account.get("active"))
        return str(account.get("status") or "").lower() == "active"

    def _platform(self, platform_id: str) -> str:
        return str(platform_id or "").strip().lower()


account_registry_service = AccountRegistryService()
