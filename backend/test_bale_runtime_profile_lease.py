from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from modules.automation_engine.plugins.bale import plugin as bale_plugin_module


# ============================================================
# BLOCK: BALE_RUNTIME_PROFILE_LEASE_REGRESSION
# PURPOSE:
# Verifies failed Playwright startup cannot leave an account profile leased.
# ACCOUNT_SCOPE:
# One isolated test account and temporary profile directory.
# DEPENDENCIES:
# BalePlugin.create_reusable_runtime_session
# LAYER:
# TEST
# ============================================================


def test_playwright_startup_failure_releases_profile_lease(monkeypatch, tmp_path: Path) -> None:
    account_id = "bale_09392609017"
    browser_path = tmp_path / "chrome.exe"
    browser_path.touch()
    profile_path = tmp_path / account_id
    profile_path.mkdir()
    lease_path = profile_path / ".clinicos_profile_lease.json"
    profile_record = SimpleNamespace(
        account_id=account_id,
        chrome_executable=str(browser_path),
        user_data_dir=str(profile_path),
        profile_directory="Default",
    )
    release_calls: list[dict[str, object]] = []

    def acquire_profile_lease(record, *, run_id):
        lease = {"run_id": run_id, "account_id": record.account_id}
        lease_path.write_text("leased", encoding="utf-8")
        return {"lease": lease, "lock_path": str(lease_path)}

    def release_profile_lease(record, lease, *, abnormal=False, reason=None):
        release_calls.append({"lease": lease, "abnormal": abnormal, "reason": reason})
        lease_path.unlink(missing_ok=True)
        return {"ok": True, "released": True}

    class FailingPlaywrightContext:
        def start(self):
            raise RuntimeError("forced Playwright startup failure")

    monkeypatch.setattr(bale_plugin_module, "resolve_system_browser_executable", lambda: str(browser_path))
    monkeypatch.setattr(bale_plugin_module.bale_account_store, "get_account", lambda requested: {"account_id": requested})
    monkeypatch.setattr(bale_plugin_module, "resolve_profile_record", lambda *args, **kwargs: profile_record)
    monkeypatch.setattr(bale_plugin_module, "acquire_profile_lease", acquire_profile_lease)
    monkeypatch.setattr(bale_plugin_module, "release_profile_lease", release_profile_lease)
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: FailingPlaywrightContext())

    with pytest.raises(RuntimeError, match="forced Playwright startup failure"):
        bale_plugin_module.bale_plugin.create_reusable_runtime_session(
            account_id,
            provider_mode="native_chrome",
            profile_path=str(profile_path),
        )

    assert not lease_path.exists()
    assert release_calls == [
        {
            "lease": {"run_id": f"bale_runtime_{account_id}", "account_id": account_id},
            "abnormal": True,
            "reason": "runtime_session_launch_failed",
        }
    ]


def test_same_process_orphan_profile_lease_is_recovered(monkeypatch, tmp_path: Path) -> None:
    account_id = "bale_09392609017"
    profile_path = tmp_path / account_id
    profile_path.mkdir()
    lease_path = profile_path / ".clinicos_profile_lease.json"
    lease = {"run_id": f"bale_runtime_{account_id}", "owner_process_id": os.getpid()}
    lease_path.write_text(json.dumps(lease), encoding="utf-8")
    profile_record = SimpleNamespace(
        account_id=account_id,
        chrome_executable=str(tmp_path / "chrome.exe"),
        user_data_dir=str(profile_path),
        profile_directory="Default",
    )

    monkeypatch.setattr(bale_plugin_module.bale_account_store, "get_account", lambda requested: {"account_id": requested})
    monkeypatch.setattr(bale_plugin_module, "resolve_profile_record", lambda *args, **kwargs: profile_record)
    monkeypatch.setattr(bale_plugin_module, "chrome_processes_for_profile", lambda _record: [])

    recovered = bale_plugin_module.bale_plugin.recover_stale_reusable_runtime_lease(account_id)

    assert recovered["recovered"] is True
    assert not lease_path.exists()


# ============================================================
# END BLOCK: BALE_RUNTIME_PROFILE_LEASE_REGRESSION
# ============================================================
