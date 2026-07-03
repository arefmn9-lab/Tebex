from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from modules.automation_engine.browser.profile_groups import account_user_data_dir
from modules.automation_engine.browser.profile_provider import BROWSER_PROVIDERS


ACCOUNT_STATUSES = {"new", "preparing", "active", "limited", "blocked", "paused", "disabled"}
ACCOUNT_SECTIONS = {"new_accounts", "week_1", "week_2", "month_1", "old_accounts"}

DEFAULT_MESSAGE_CONFIG = {
    "source_type": "message_link",
    "source_value": "",
    "source_message_hint": "",
    "description": "",
    "dry_run": True,
}

DEFAULT_PREPARATION = {
    "enabled": False,
    "mode": "qa_only",
    "selected_accounts": [],
    "rules": {
        "only_owned_accounts": True,
        "manual_approval_required": True,
        "max_test_messages_per_account_per_day": 3,
        "min_delay_seconds": 300,
        "stop_on_failure": True,
    },
    "account_sections": ["new_accounts", "week_1", "week_2", "month_1", "old_accounts"],
}


def _runtime_dir() -> Path:
    backend_dir = Path(__file__).resolve().parents[4]
    path = backend_dir / "runtime" / "platforms" / "bale"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _account_age_days(created_at: str) -> int:
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        return max(0, (datetime.now(timezone.utc) - created).days)
    except Exception:
        return 0


class BaleAccountStore:
    def __init__(self, runtime_dir: Path | None = None) -> None:
        self.runtime_dir = runtime_dir or _runtime_dir()
        self.accounts_path = self.runtime_dir / "accounts.json"
        self.message_config_path = self.runtime_dir / "message_config.json"
        self.preparation_path = self.runtime_dir / "preparation.json"
        self._ensure_seed_data()

    def list_accounts(self) -> list[dict[str, Any]]:
        accounts = self._read_json(self.accounts_path, [])
        return [self._normalize_account(account) for account in accounts]

    def get_account(self, account_id: str) -> dict[str, Any] | None:
        for account in self.list_accounts():
            if account["account_id"] == account_id:
                return account
        return None

    def create_account(self, payload: dict[str, Any]) -> dict[str, Any]:
        accounts = self.list_accounts()
        phone = str(payload.get("phone") or payload.get("username_or_number") or "").strip()
        account_id = str(payload.get("account_id") or f"bale_{phone or len(accounts) + 1}").strip()
        if any(account["account_id"] == account_id for account in accounts):
            raise ValueError(f"Account already exists: {account_id}")
        self._validate_provider_payload(payload)
        account = self._normalize_account({"account_id": account_id, **payload})
        accounts.append(account)
        self._write_json(self.accounts_path, accounts)
        return account

    def update_account(self, account_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        accounts = self.list_accounts()
        for index, account in enumerate(accounts):
            if account["account_id"] == account_id:
                merged = {**account, **payload, "account_id": account_id}
                self._validate_provider_payload(merged)
                updated = self._normalize_account(merged)
                accounts[index] = updated
                self._write_json(self.accounts_path, accounts)
                return updated
        raise KeyError(account_id)

    def assign_profile_group(
        self,
        account_id: str,
        device_group_id: str,
        browser_provider: str = "adspower",
        profile_id: str | None = None,
        adspower_profile_id: str | None = None,
    ) -> dict[str, Any]:
        return self.update_account(
            account_id,
            {
                "profile_group_id": device_group_id,
                "device_group_id": device_group_id,
                "browser_provider": browser_provider,
                "profile_id": profile_id or f"profile_{account_id}",
                "adspower_profile_id": adspower_profile_id,
                "user_data_dir": str(account_user_data_dir("bale", account_id)),
            },
        )

    def _validate_provider_payload(self, payload: dict[str, Any]) -> None:
        phone = str(payload.get("phone") or "").strip()
        username = str(payload.get("username_or_number") or "").strip()
        if not phone and not username:
            raise ValueError("phone or username_or_number is required")
        browser_provider = str(payload.get("browser_provider") or "adspower")
        if browser_provider == "adspower" and not str(payload.get("adspower_profile_id") or "").strip():
            raise ValueError("adspower_profile_id is required when browser_provider is adspower")

    def delete_account(self, account_id: str) -> dict[str, Any]:
        accounts = self.list_accounts()
        remaining = [account for account in accounts if account["account_id"] != account_id]
        if len(remaining) == len(accounts):
            raise KeyError(account_id)
        self._write_json(self.accounts_path, remaining)
        return {"ok": True, "account_id": account_id}

    def get_message_config(self) -> dict[str, Any]:
        return self._read_json(self.message_config_path, deepcopy(DEFAULT_MESSAGE_CONFIG))

    def save_message_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = {**deepcopy(DEFAULT_MESSAGE_CONFIG), **payload}
        config["dry_run"] = bool(config.get("dry_run", True))
        self._write_json(self.message_config_path, config)
        return config

    def get_preparation(self) -> dict[str, Any]:
        return self._read_json(self.preparation_path, deepcopy(DEFAULT_PREPARATION))

    def save_preparation(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = {**deepcopy(DEFAULT_PREPARATION), **payload}
        rules = {**DEFAULT_PREPARATION["rules"], **(payload.get("rules") or {})}
        config["rules"] = rules
        self._write_json(self.preparation_path, config)
        return config

    def _normalize_account(self, account: dict[str, Any]) -> dict[str, Any]:
        created_at = str(account.get("created_at") or _now())
        status = account.get("status") if account.get("status") in ACCOUNT_STATUSES else "new"
        section = account.get("section") if account.get("section") in ACCOUNT_SECTIONS else "new_accounts"
        phone = str(account.get("phone") or "")
        username_or_number = str(account.get("username_or_number") or phone)
        account_id = str(account.get("account_id") or f"bale_{phone}")
        browser_provider = str(account.get("browser_provider") or "adspower")
        if browser_provider == "adspower_placeholder":
            browser_provider = "adspower"
        if browser_provider not in BROWSER_PROVIDERS:
            browser_provider = "adspower"
        profile_id = str(account.get("profile_id") or f"profile_{account_id}")
        adspower_profile_id = str(account.get("adspower_profile_id") or "")
        profile_group_id = str(account.get("profile_group_id") or "default")
        device_group_id = str(account.get("device_group_id") or "device_group_001")
        account_group_id = str(account.get("account_group_id") or "bale_test_group")
        worker_id = str(account.get("worker_id") or "local_windows_1")
        user_data_dir = str(account.get("user_data_dir") or account_user_data_dir("bale", account_id))
        return {
            "account_id": account_id,
            "platform_id": "bale",
            "platform": "bale",
            "username_or_number": username_or_number,
            "phone": phone,
            "status": status,
            "section": section,
            "created_at": created_at,
            "account_age_days": _account_age_days(created_at),
            "daily_limit": int(account.get("daily_limit", 10)),
            "hourly_limit": int(account.get("hourly_limit", 2)),
            "min_delay_seconds": int(account.get("min_delay_seconds", 300)),
            "max_actions_per_session": int(account.get("max_actions_per_session", 5)),
            "health_score": int(account.get("health_score", 100)),
            "login_status": str(account.get("login_status") or "unknown"),
            "block_status": str(account.get("block_status") or "unknown"),
            "last_activity_at": account.get("last_activity_at"),
            "last_login_check_at": account.get("last_login_check_at"),
            "consecutive_failures": int(account.get("consecutive_failures", 0)),
            "notes": str(account.get("notes") or ""),
            "browser_provider": browser_provider,
            "profile_id": profile_id,
            "adspower_profile_id": adspower_profile_id,
            "account_group_id": account_group_id,
            "account_group_name": str(account.get("account_group_name") or "Bale Test Group"),
            "profile_group_id": profile_group_id,
            "device_group_id": device_group_id,
            "batch_capacity": int(account.get("batch_capacity", 30)),
            "max_concurrent_per_group": int(account.get("max_concurrent_per_group", 5)),
            "priority": int(account.get("priority", 100)),
            "enabled_for_scheduling": bool(account.get("enabled_for_scheduling", True)),
            "worker_id": worker_id,
            "user_data_dir": user_data_dir,
            "active": status == "active",
        }

    def _ensure_seed_data(self) -> None:
        if not self.accounts_path.exists():
            self._write_json(
                self.accounts_path,
                [
                    self._normalize_account({"account_id": "bale_09214032167", "phone": "09214032167", "status": "active", "daily_limit": 10, "browser_provider": "native_chrome"}),
                    self._normalize_account({"account_id": "bale_09214032168", "phone": "09214032168", "status": "preparing", "daily_limit": 8, "browser_provider": "native_chrome"}),
                ],
            )
        if not self.message_config_path.exists():
            self._write_json(self.message_config_path, deepcopy(DEFAULT_MESSAGE_CONFIG))
        if not self.preparation_path.exists():
            self._write_json(self.preparation_path, deepcopy(DEFAULT_PREPARATION))

    def _read_json(self, path: Path, default: Any) -> Any:
        try:
            with path.open("r", encoding="utf-8") as json_file:
                return json.load(json_file)
        except Exception:
            return deepcopy(default)

    def _write_json(self, path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as json_file:
            json.dump(data, json_file, ensure_ascii=False, indent=2)


bale_account_store = BaleAccountStore()
