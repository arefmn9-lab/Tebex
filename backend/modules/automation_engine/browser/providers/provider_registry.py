from __future__ import annotations

from typing import Any

from .adspower_provider import AdsPowerProvider
from .native_chrome_provider import NativeChromeProvider


BROWSER_PROVIDERS = {"native_chrome", "adspower", "remote_worker_placeholder"}

_PROVIDERS = {
    "native_chrome": NativeChromeProvider(),
    "adspower": AdsPowerProvider(),
}


class RemoteWorkerPlaceholderProvider:
    provider_id = "remote_worker_placeholder"
    display_name = "Remote Worker"

    def health_check(self) -> dict[str, Any]:
        return {"ok": False, "browser_provider": self.provider_id, "message": "Remote worker integration is not implemented yet"}

    def open_profile(self, account_id: str, profile_id: str, url: str | None = None) -> dict[str, Any]:
        return {"ok": False, "browser_provider": self.provider_id, "account_id": account_id, "profile_id": profile_id, "message": "Remote worker integration is not implemented yet"}

    def close_profile(self, account_id: str, profile_id: str) -> dict[str, Any]:
        return {"ok": False, "browser_provider": self.provider_id, "account_id": account_id, "profile_id": profile_id, "message": "Remote worker integration is not implemented yet"}

    def get_profile_status(self, account_id: str, profile_id: str) -> dict[str, Any]:
        return {"ok": False, "browser_provider": self.provider_id, "account_id": account_id, "profile_id": profile_id, "status": "placeholder"}


_PROVIDERS["remote_worker_placeholder"] = RemoteWorkerPlaceholderProvider()


def get_provider(provider_id: str):
    return _PROVIDERS.get(provider_id, _PROVIDERS["native_chrome"])


def list_providers() -> list[dict[str, Any]]:
    return [
        {
            "id": provider_id,
            "name": getattr(provider, "display_name", provider_id),
            "enabled": provider_id != "remote_worker_placeholder",
        }
        for provider_id, provider in _PROVIDERS.items()
    ]
