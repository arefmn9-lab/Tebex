from __future__ import annotations

import os
import platform
from typing import Any

from .profile_groups import account_user_data_dir
from .profile_provider import get_profile_provider
from .session_manager import SessionManager
from modules.automation_engine.browser_identity.bale_profile_contract import (
    BaleProfileContractError,
    acquire_profile_lease,
    profile_launch_args,
    release_profile_lease,
    resolve_profile_record,
    verify_runtime_process_identity,
)


SYSTEM_BROWSER_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]
SYSTEM_CHROME_PATH = SYSTEM_BROWSER_CANDIDATES[0]
SYSTEM_EDGE_PATH = SYSTEM_BROWSER_CANDIDATES[3]


def resolve_system_browser_executable() -> str | None:
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


class BrowserManager:
    def __init__(self, session_manager: SessionManager | None = None) -> None:
        self.session_manager = session_manager or SessionManager()
        self.playwright_runtime: Any = None
        self._browser: Any = None
        self._contexts: dict[str, Any] = {}
        self._pages: dict[str, Any] = {}
        self._profile_metadata: dict[str, dict[str, Any]] = {}
        self._profile_leases: dict[str, dict[str, Any]] = {}
        self._headless: bool | None = None
        self.last_browser_path: str | None = None

    def is_available(self) -> bool:
        try:
            import playwright.sync_api  # noqa: F401

            return True
        except Exception:
            return False

    def launch_browser(self, headless: bool = True) -> Any:
        if self._browser is not None:
            return self._browser

        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise RuntimeError("Playwright is not installed or not importable") from exc

        self.playwright_runtime = sync_playwright().start()
        self._headless = headless
        browser_path = resolve_system_browser_executable()
        self.last_browser_path = browser_path
        print("[BrowserManager] os.name =", os.name)
        print("[BrowserManager] platform =", platform.system())
        print("[BrowserManager] executable_path passed to Playwright =", browser_path)

        if not browser_path:
            print("No system Chrome/Edge found")
            raise RuntimeError("No system Chrome/Edge found")

        print("[BrowserManager] FORCED system browser:", browser_path)
        launch_headless = False if platform.system() == "Windows" or os.name == "nt" else headless
        self._browser = self.playwright_runtime.chromium.launch(
            executable_path=browser_path,
            headless=launch_headless,
            args=[],
        )
        return self._browser

    def browser_debug_info(self) -> dict[str, Any]:
        return {
            "platform": platform.system(),
            "os_name": os.name,
            "chrome_exists": os.path.exists(SYSTEM_BROWSER_CANDIDATES[0]),
            "edge_exists": os.path.exists(SYSTEM_BROWSER_CANDIDATES[3]),
            "resolved_browser_path": resolve_system_browser_executable(),
        }

    def get_context(
        self,
        account_id: str,
        headless: bool = True,
        login_required: bool = False,
        profile_metadata: dict[str, Any] | None = None,
    ) -> Any:
        if account_id in self._contexts:
            return self._contexts[account_id]

        profile = self._profile_for_account(account_id, profile_metadata)
        provider_id = str(profile.get("browser_provider") or "native_chrome")
        provider = get_profile_provider(provider_id)
        if provider_id != "native_chrome":
            result = provider.open_profile(
                account_id,
                str(profile.get("adspower_profile_id") or profile.get("profile_id") or account_id),
            )
            if not result.get("ok"):
                raise RuntimeError(str(result.get("message") or result))
            raise RuntimeError(f"Browser provider is not implemented yet: {provider_id}")

        context = self._launch_persistent_context(
            account_id=account_id,
            user_data_dir=str(profile["user_data_dir"]),
            headless=headless and not login_required,
            profile_metadata=profile,
        )
        self._contexts[account_id] = context
        self.session_manager.assign_session(account_id, profile)
        return context

    def get_page(
        self,
        account_id: str,
        headless: bool = True,
        login_required: bool = False,
        profile_metadata: dict[str, Any] | None = None,
    ) -> Any:
        if account_id in self._pages:
            return self._pages[account_id]

        context = self.get_context(
            account_id,
            headless=headless,
            login_required=login_required,
            profile_metadata=profile_metadata,
        )
        page = context.new_page()
        self._pages[account_id] = page
        return page

    def save_session(self, account_id: str) -> None:
        context = self._contexts.get(account_id)
        if context is not None and self.session_manager.can_save_storage_state(account_id):
            self.session_manager.save_storage_state(account_id, context)

    def close_account(self, account_id: str) -> None:
        page = self._pages.pop(account_id, None)
        if page is not None:
            page.close()

        context = self._contexts.pop(account_id, None)
        if context is not None:
            profile = self._profile_metadata.pop(account_id, {})
            if self.session_manager.can_save_storage_state(account_id):
                self.session_manager.save_storage_state(account_id, context)
            context.close()
            lease = self._profile_leases.pop(account_id, None)
            record = profile.get("_bale_profile_record")
            if isinstance(record, dict):
                release_profile_lease(resolve_profile_record(account_id), lease)
            get_profile_provider(str(profile.get("browser_provider") or "native_chrome")).close_profile(
                account_id,
                str(profile.get("adspower_profile_id") or profile.get("profile_id") or account_id),
            )

    def close_browser(self) -> None:
        for account_id in list(self._contexts.keys()):
            self.close_account(account_id)

        if self._browser is not None:
            self._browser.close()
            self._browser = None

        if self.playwright_runtime is not None:
            self.playwright_runtime.stop()
            self.playwright_runtime = None

    def _profile_for_account(
        self,
        account_id: str,
        profile_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        record = resolve_profile_record(account_id)
        profile = {
            "account_id": account_id,
            "platform_id": "bale",
            "browser_provider": "native_chrome",
            "profile_id": f"profile_{account_id}",
            "adspower_profile_id": "",
            "device_group_id": "device_group_001",
            "profile_group_id": "group_001",
            "worker_id": "local_windows_1",
            "user_data_dir": record.user_data_dir,
            "profile_directory": record.profile_directory,
            "_bale_profile_record": record.to_dict(),
        }
        profile.update(profile_metadata or {})
        record = resolve_profile_record(account_id, profile)
        profile["user_data_dir"] = record.user_data_dir
        profile["profile_directory"] = record.profile_directory
        profile["_bale_profile_record"] = record.to_dict()
        self._profile_metadata[account_id] = profile
        return profile

    def _launch_persistent_context(
        self,
        account_id: str,
        user_data_dir: str,
        headless: bool = True,
        profile_metadata: dict[str, Any] | None = None,
    ) -> Any:
        if self.playwright_runtime is None:
            try:
                from playwright.sync_api import sync_playwright
            except Exception as exc:
                raise RuntimeError("Playwright is not installed or not importable") from exc
            self.playwright_runtime = sync_playwright().start()

        record = resolve_profile_record(account_id, profile_metadata or {})
        if str(user_data_dir) and str(user_data_dir) != record.user_data_dir:
            raise RuntimeError("PROFILE_IDENTITY_MISMATCH")
        lease_payload = acquire_profile_lease(record, run_id=f"browser_manager_{account_id}")
        lease = lease_payload["lease"]
        self._profile_leases[account_id] = lease
        browser_path = resolve_system_browser_executable()
        self.last_browser_path = browser_path
        print("[BrowserManager] os.name =", os.name)
        print("[BrowserManager] platform =", platform.system())
        print("[BrowserManager] account_id =", account_id)
        print("[BrowserManager] user_data_dir =", user_data_dir)
        print("[BrowserManager] executable_path passed to Playwright =", browser_path)

        if not browser_path:
            print("No system Chrome/Edge found")
            release_profile_lease(record, lease, abnormal=True, reason="browser_executable_missing")
            raise RuntimeError("No system Chrome/Edge found")
        if str(browser_path).casefold() != record.chrome_executable.casefold():
            release_profile_lease(record, lease, abnormal=True, reason="browser_executable_mismatch")
            raise RuntimeError("PROFILE_IDENTITY_MISMATCH")

        print("[BrowserManager] FORCED system browser:", browser_path)
        launch_headless = False if platform.system() == "Windows" or os.name == "nt" else headless
        try:
            context = self.playwright_runtime.chromium.launch_persistent_context(
                user_data_dir=record.user_data_dir,
                executable_path=browser_path,
                headless=launch_headless,
                args=profile_launch_args(record),
            )
            identity = verify_runtime_process_identity(record)
            self._profile_metadata.setdefault(account_id, {})["runtime_process_identity"] = identity
            return context
        except BaleProfileContractError:
            release_profile_lease(record, lease, abnormal=True, reason="profile_identity_failed")
            self._profile_leases.pop(account_id, None)
            raise
        except Exception:
            release_profile_lease(record, lease, abnormal=True, reason="launch_failed")
            self._profile_leases.pop(account_id, None)
            raise
