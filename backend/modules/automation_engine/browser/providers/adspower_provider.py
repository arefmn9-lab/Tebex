from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from copy import deepcopy
from pathlib import Path
from typing import Any


DEFAULT_ADSPOWER_CONFIG = {
    "enabled": True,
    "api_base_url": "http://127.0.0.1:50325",
    "api_token": "",
    "open_timeout_seconds": 60,
}


def adspower_config_path() -> Path:
    backend_dir = Path(__file__).resolve().parents[4]
    path = backend_dir / "runtime" / "browser_providers" / "adspower.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class AdsPowerProvider:
    provider_id = "adspower"
    display_name = "AdsPower"

    def __init__(self, config_path: str | Path | None = None) -> None:
        self.config_path = Path(config_path or adspower_config_path())
        self._last_debug_data: dict[str, Any] = {}

    def load_config(self, masked: bool = False) -> dict[str, Any]:
        if not self.config_path.exists():
            config = deepcopy(DEFAULT_ADSPOWER_CONFIG)
            if masked:
                config["configured"] = False
                config["api_token"] = ""
            return config
        try:
            with self.config_path.open("r", encoding="utf-8") as config_file:
                config = {**deepcopy(DEFAULT_ADSPOWER_CONFIG), **json.load(config_file)}
        except Exception:
            config = deepcopy(DEFAULT_ADSPOWER_CONFIG)
            config["enabled"] = False
        if masked and config.get("api_token"):
            config["api_token"] = "********"
        if masked:
            config["configured"] = self.config_path.exists()
        return config

    def save_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        current = self.load_config(masked=False)
        token = payload.get("api_token")
        if token == "********":
            payload = {key: value for key, value in payload.items() if key != "api_token"}
        config = {**current, **payload}
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        with self.config_path.open("w", encoding="utf-8") as config_file:
            json.dump(config, config_file, ensure_ascii=False, indent=2)
        return self.load_config(masked=True)

    def health_check(self) -> dict[str, Any]:
        config = self.load_config(masked=False)
        if not self.config_path.exists():
            return {
                "ok": False,
                "browser_provider": self.provider_id,
                "error_code": "adspower_not_configured",
                "message": "AdsPower provider is not configured",
            }
        if not config.get("enabled", False):
            return {
                "ok": False,
                "browser_provider": self.provider_id,
                "error_code": "adspower_disabled",
                "message": "AdsPower provider is disabled",
            }
        result = self._request("/status", config)
        if result["ok"]:
            return {"ok": True, "browser_provider": self.provider_id, "message": "AdsPower local API is reachable", "data": result.get("data")}
        return {
            "ok": False,
            "browser_provider": self.provider_id,
            "error_code": "adspower_unavailable",
            "message": "AdsPower local API is not reachable",
            "error": result.get("error"),
        }

    def open_profile(self, account_id: str, profile_id: str, url: str | None = None) -> dict[str, Any]:
        if not profile_id:
            return {
                "ok": False,
                "browser_provider": self.provider_id,
                "account_id": account_id,
                "profile_id": profile_id,
                "error_code": "profile_not_configured",
                "message": "AdsPower profile_id is required for this account",
            }
        config = self.load_config(masked=False)
        if not self.config_path.exists():
            return {
                "ok": False,
                "browser_provider": self.provider_id,
                "account_id": account_id,
                "profile_id": profile_id,
                "error_code": "adspower_not_configured",
                "message": "AdsPower provider is not configured",
            }
        if not config.get("enabled", False):
            return {
                "ok": False,
                "browser_provider": self.provider_id,
                "account_id": account_id,
                "profile_id": profile_id,
                "error_code": "adspower_disabled",
                "message": "AdsPower provider is disabled",
            }

        query = {"user_id": profile_id}
        result = self._request("/api/v1/browser/start", config, query)
        if not result["ok"]:
            return {
                "ok": False,
                "browser_provider": self.provider_id,
                "account_id": account_id,
                "profile_id": profile_id,
                "error_code": "adspower_unavailable",
                "message": "AdsPower local API is not reachable",
                "error": result.get("error"),
            }

        data = result.get("data") or {}
        self._last_debug_data[profile_id] = data
        navigate_result = self._navigate_with_cdp(data, url) if url else {"ok": False, "message": "No URL requested"}
        return {
            "ok": True,
            "browser_provider": self.provider_id,
            "account_id": account_id,
            "profile_id": profile_id,
            "message": "AdsPower profile opened",
            "url": url,
            "adspower_data": data,
            "navigation": navigate_result,
        }

    def close_profile(self, account_id: str, profile_id: str) -> dict[str, Any]:
        config = self.load_config(masked=False)
        if not self.config_path.exists():
            return {
                "ok": False,
                "browser_provider": self.provider_id,
                "account_id": account_id,
                "profile_id": profile_id,
                "error_code": "adspower_not_configured",
                "message": "AdsPower provider is not configured",
            }
        result = self._request("/api/v1/browser/stop", config, {"user_id": profile_id})
        if not result["ok"]:
            return {
                "ok": False,
                "browser_provider": self.provider_id,
                "account_id": account_id,
                "profile_id": profile_id,
                "error_code": "adspower_unavailable",
                "message": "AdsPower local API is not reachable",
                "error": result.get("error"),
            }
        return {
            "ok": True,
            "browser_provider": self.provider_id,
            "account_id": account_id,
            "profile_id": profile_id,
            "message": "AdsPower profile close requested",
            "adspower_data": result.get("data"),
        }

    def get_profile_status(self, account_id: str, profile_id: str) -> dict[str, Any]:
        config = self.load_config(masked=False)
        if not self.config_path.exists():
            return {
                "ok": False,
                "browser_provider": self.provider_id,
                "account_id": account_id,
                "profile_id": profile_id,
                "error_code": "adspower_not_configured",
                "message": "AdsPower provider is not configured",
            }
        result = self._request("/api/v1/browser/active", config, {"user_id": profile_id})
        if not result["ok"]:
            return {
                "ok": False,
                "browser_provider": self.provider_id,
                "account_id": account_id,
                "profile_id": profile_id,
                "error_code": "adspower_unavailable",
                "message": "AdsPower local API is not reachable",
                "error": result.get("error"),
            }
        return {
            "ok": True,
            "browser_provider": self.provider_id,
            "account_id": account_id,
            "profile_id": profile_id,
            "status": "available",
            "adspower_data": result.get("data"),
        }

    def _request(self, path: str, config: dict[str, Any], query: dict[str, str] | None = None) -> dict[str, Any]:
        base_url = str(config.get("api_base_url") or "").rstrip("/")
        if not base_url:
            return {"ok": False, "error": "api_base_url is empty"}
        url = f"{base_url}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        headers = {}
        if config.get("api_token"):
            headers["Authorization"] = f"Bearer {config['api_token']}"
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=int(config.get("open_timeout_seconds", 60))) as response:
                raw = response.read().decode("utf-8", errors="replace")
                data = json.loads(raw) if raw else {}
                success = data.get("code") in {0, "0", None} or data.get("status") in {"success", "ok", True}
                return {"ok": bool(success), "data": data, "error": None if success else str(data)}
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _navigate_with_cdp(self, data: dict[str, Any], url: str | None) -> dict[str, Any]:
        if not url:
            return {"ok": False, "message": "No URL requested"}
        cdp_endpoint = self._extract_cdp_endpoint(data)
        if not cdp_endpoint:
            return {"ok": True, "message": "AdsPower opened profile; CDP endpoint was not returned"}
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                browser = playwright.chromium.connect_over_cdp(cdp_endpoint)
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(url, wait_until="load")
                browser.close()
            return {"ok": True, "message": "Navigated via AdsPower CDP", "cdp_endpoint": cdp_endpoint}
        except Exception as exc:
            return {"ok": False, "message": "AdsPower profile opened, but CDP navigation failed", "error": str(exc), "cdp_endpoint": cdp_endpoint}

    def _extract_cdp_endpoint(self, data: dict[str, Any]) -> str | None:
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        for key in ["ws", "ws_endpoint", "websocket", "webSocketDebuggerUrl", "debug_port"]:
            value = payload.get(key) if isinstance(payload, dict) else None
            if not value:
                continue
            value = str(value)
            if value.startswith("ws://") or value.startswith("http://"):
                return value
            if value.isdigit():
                return f"http://127.0.0.1:{value}"
        debugger = payload.get("debugger_address") if isinstance(payload, dict) else None
        if debugger:
            return f"http://{debugger}"
        return None
