from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from .profile_provider import BROWSER_PROVIDERS


DEFAULT_GROUP = {
    "profile_group_id": "group_001",
    "device_group_id": "group_001",
    "name": "گروه ۱",
    "max_accounts": 0,
    "max_concurrent_accounts": 0,
    "browser_provider": "adspower",
    "adspower_group_id": "",
    "account_ids": [],
    "notes": "",
}


def browser_profiles_root() -> Path:
    backend_dir = Path(__file__).resolve().parents[3]
    path = backend_dir / "runtime" / "browser_profiles"
    path.mkdir(parents=True, exist_ok=True)
    return path


def account_user_data_dir(platform_id: str, account_id: str) -> Path:
    safe_account_id = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in account_id)
    path = browser_profiles_root() / platform_id / safe_account_id
    path.mkdir(parents=True, exist_ok=True)
    return path


class ProfileGroupStore:
    def __init__(self, runtime_dir: str | Path | None = None) -> None:
        backend_dir = Path(__file__).resolve().parents[3]
        self.runtime_dir = Path(runtime_dir or backend_dir / "runtime" / "platforms" / "bale")
        self.groups_path = self.runtime_dir / "profile_groups.json"
        self._ensure_seed_data()

    def list_groups(self) -> list[dict[str, Any]]:
        groups = self._read_json([])
        return [self._normalize_group(group) for group in groups]

    def get_group(self, device_group_id: str) -> dict[str, Any] | None:
        for group in self.list_groups():
            if group["profile_group_id"] == device_group_id or group["device_group_id"] == device_group_id:
                return group
        return None

    def create_group(self, payload: dict[str, Any]) -> dict[str, Any]:
        groups = self.list_groups()
        device_group_id = str(payload.get("profile_group_id") or payload.get("device_group_id") or f"group_{len(groups) + 1:03d}")
        if any(group["profile_group_id"] == device_group_id for group in groups):
            raise ValueError(f"Profile group already exists: {device_group_id}")
        group = self._normalize_group({**payload, "profile_group_id": device_group_id, "device_group_id": device_group_id})
        groups.append(group)
        self._write_json(groups)
        return group

    def update_group(self, device_group_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        groups = self.list_groups()
        for index, group in enumerate(groups):
            if group["profile_group_id"] == device_group_id or group["device_group_id"] == device_group_id:
                updated = self._normalize_group({**group, **payload, "profile_group_id": device_group_id, "device_group_id": device_group_id})
                groups[index] = updated
                self._write_json(groups)
                return updated
        raise KeyError(device_group_id)

    def delete_group(self, device_group_id: str) -> dict[str, Any]:
        groups = self.list_groups()
        remaining = [group for group in groups if group["profile_group_id"] != device_group_id and group["device_group_id"] != device_group_id]
        if len(remaining) == len(groups):
            raise KeyError(device_group_id)
        self._write_json(remaining)
        return {"ok": True, "profile_group_id": device_group_id, "device_group_id": device_group_id}

    def assign_account(self, account_id: str, device_group_id: str) -> dict[str, Any]:
        groups = self.list_groups()
        target_found = False
        updated_groups = []
        for group in groups:
            account_ids = [item for item in group.get("account_ids", []) if item != account_id]
            if group["profile_group_id"] == device_group_id or group["device_group_id"] == device_group_id:
                target_found = True
                account_ids.append(account_id)
            group["account_ids"] = account_ids
            updated_groups.append(group)
        if not target_found:
            raise KeyError(device_group_id)
        self._write_json(updated_groups)
        return self.get_group(device_group_id) or {}

    def _normalize_group(self, group: dict[str, Any]) -> dict[str, Any]:
        browser_provider = str(group.get("browser_provider") or "native_chrome")
        if browser_provider not in BROWSER_PROVIDERS:
            browser_provider = "native_chrome"
        account_ids = list(dict.fromkeys(str(item) for item in group.get("account_ids", [])))
        profile_group_id = str(group.get("profile_group_id") or group.get("device_group_id") or "group_001")
        return {
            "profile_group_id": profile_group_id,
            "device_group_id": profile_group_id,
            "name": str(group.get("name") or "گروه ۱"),
            "max_accounts": 0,
            "group_size_limit": 0,
            "max_concurrent_accounts": int(group.get("max_concurrent_accounts") or 0),
            "browser_provider": browser_provider,
            "adspower_group_id": str(group.get("adspower_group_id") or ""),
            "account_ids": account_ids,
            "notes": str(group.get("notes") or ""),
        }

    def _ensure_seed_data(self) -> None:
        if not self.groups_path.exists():
            self._write_json([deepcopy(DEFAULT_GROUP)])

    def _read_json(self, default: Any) -> Any:
        try:
            with self.groups_path.open("r", encoding="utf-8") as json_file:
                return json.load(json_file)
        except Exception:
            return deepcopy(default)

    def _write_json(self, data: Any) -> None:
        self.groups_path.parent.mkdir(parents=True, exist_ok=True)
        with self.groups_path.open("w", encoding="utf-8") as json_file:
            json.dump(data, json_file, ensure_ascii=False, indent=2)


profile_group_store = ProfileGroupStore()
