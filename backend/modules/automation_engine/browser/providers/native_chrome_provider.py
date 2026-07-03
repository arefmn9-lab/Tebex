from __future__ import annotations

from typing import Any


class NativeChromeProvider:
    provider_id = "native_chrome"
    display_name = "Native Chrome"

    def health_check(self) -> dict[str, Any]:
        return {"ok": True, "browser_provider": self.provider_id, "status": "available"}

    def open_profile(self, account_id: str, profile_id: str, url: str | None = None) -> dict[str, Any]:
        return {
            "ok": True,
            "browser_provider": self.provider_id,
            "account_id": account_id,
            "profile_id": profile_id,
            "message": "Native Chrome profile is managed by BrowserManager",
            "url": url,
        }

    def close_profile(self, account_id: str, profile_id: str) -> dict[str, Any]:
        return {
            "ok": True,
            "browser_provider": self.provider_id,
            "account_id": account_id,
            "profile_id": profile_id,
            "message": "Native Chrome close requested",
        }

    def get_profile_status(self, account_id: str, profile_id: str) -> dict[str, Any]:
        return {
            "ok": True,
            "browser_provider": self.provider_id,
            "account_id": account_id,
            "profile_id": profile_id,
            "status": "available",
        }
