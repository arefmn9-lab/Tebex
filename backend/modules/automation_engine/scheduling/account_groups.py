from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


ACCOUNT_GROUP_PROVIDERS = {"native_chrome", "adspower", "remote_worker_placeholder"}


DEFAULT_ACCOUNT_GROUPS = [
    {
        "group_id": "bale_test_group",
        "name": "Bale Test Group",
        "platform_id": "bale",
        "browser_provider": "native_chrome",
        "device_group_id": "device_group_001",
        "profile_group_id": "default",
        "max_concurrent": 5,
        "batch_capacity": 30,
        "daily_capacity": 100,
        "enabled": True,
        "notes": "Default Bale scheduler group",
    },
    {
        "group_id": "telegram_test_group",
        "name": "Telegram Test Group",
        "platform_id": "telegram",
        "browser_provider": "native_chrome",
        "device_group_id": "device_group_001",
        "profile_group_id": "default",
        "max_concurrent": 5,
        "batch_capacity": 30,
        "daily_capacity": 100,
        "enabled": True,
        "notes": "Default Telegram scheduler group",
    },
    {
        "group_id": "rubika_test_group",
        "name": "Rubika Test Group",
        "platform_id": "rubika",
        "browser_provider": "native_chrome",
        "device_group_id": "device_group_001",
        "profile_group_id": "default",
        "max_concurrent": 5,
        "batch_capacity": 30,
        "daily_capacity": 100,
        "enabled": True,
        "notes": "Default Rubika scheduler group",
    },
]


@dataclass
class AccountGroup:
    group_id: str
    name: str
    platform_id: str
    browser_provider: str
    device_group_id: str
    profile_group_id: str
    max_concurrent: int
    batch_capacity: int
    daily_capacity: int
    enabled: bool = True
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def backend_runtime_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "runtime"


class AccountGroupStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or backend_runtime_dir() / "account_groups.json"
        self._ensure_seed_data()

    def list_groups(self) -> list[dict[str, Any]]:
        return [self._normalize_group(group).to_dict() for group in self._read_json([])]

    def list_platform_groups(self, platform_id: str) -> list[dict[str, Any]]:
        return [group for group in self.list_groups() if group["platform_id"] == platform_id]

    def get_group(self, group_id: str) -> dict[str, Any] | None:
        for group in self.list_groups():
            if group["group_id"] == group_id:
                return group
        return None

    def default_group_for_platform(self, platform_id: str) -> dict[str, Any] | None:
        platform_groups = self.list_platform_groups(platform_id)
        if not platform_groups:
            return None
        for group in platform_groups:
            if group.get("enabled"):
                return group
        return platform_groups[0]

    def create_group(self, payload: dict[str, Any]) -> dict[str, Any]:
        groups = self.list_groups()
        group_id = str(payload.get("group_id") or "").strip()
        if not group_id:
            base = str(payload.get("name") or "account_group").strip().lower().replace(" ", "_")
            group_id = base or f"account_group_{len(groups) + 1}"
        if any(group["group_id"] == group_id for group in groups):
            raise ValueError(f"Account group already exists: {group_id}")
        group = self._normalize_group({**payload, "group_id": group_id}).to_dict()
        groups.append(group)
        self._write_json(groups)
        return group

    def update_group(self, group_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        groups = self.list_groups()
        for index, group in enumerate(groups):
            if group["group_id"] == group_id:
                updated = self._normalize_group({**group, **payload, "group_id": group_id}).to_dict()
                groups[index] = updated
                self._write_json(groups)
                return updated
        raise KeyError(group_id)

    def _normalize_group(self, group: dict[str, Any]) -> AccountGroup:
        browser_provider = str(group.get("browser_provider") or "native_chrome")
        if browser_provider not in ACCOUNT_GROUP_PROVIDERS:
            browser_provider = "native_chrome"
        max_concurrent = max(1, int(group.get("max_concurrent", group.get("max_concurrent_per_group", 5))))
        batch_capacity = max(1, int(group.get("batch_capacity", 30)))
        daily_capacity = max(1, int(group.get("daily_capacity", batch_capacity)))
        group_id = str(group.get("group_id") or "bale_test_group")
        platform_id = str(group.get("platform_id") or "bale")
        return AccountGroup(
            group_id=group_id,
            name=str(group.get("name") or group.get("account_group_name") or group_id),
            platform_id=platform_id,
            browser_provider=browser_provider,
            device_group_id=str(group.get("device_group_id") or "device_group_001"),
            profile_group_id=str(group.get("profile_group_id") or "default"),
            max_concurrent=max_concurrent,
            batch_capacity=batch_capacity,
            daily_capacity=daily_capacity,
            enabled=bool(group.get("enabled", True)),
            notes=str(group.get("notes") or ""),
        )

    def _ensure_seed_data(self) -> None:
        existing = self._read_json(None)
        if existing is None:
            self._write_json(deepcopy(DEFAULT_ACCOUNT_GROUPS))
            return
        groups = [self._normalize_group(group).to_dict() for group in existing if isinstance(group, dict)]
        group_ids = {group["group_id"] for group in groups}
        changed = False
        for default_group in DEFAULT_ACCOUNT_GROUPS:
            if default_group["group_id"] not in group_ids:
                groups.append(self._normalize_group(default_group).to_dict())
                changed = True
        if changed:
            self._write_json(groups)

    def _read_json(self, default: Any) -> Any:
        try:
            with self.path.open("r", encoding="utf-8") as json_file:
                return json.load(json_file)
        except Exception:
            return deepcopy(default)

    def _write_json(self, data: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as json_file:
            json.dump(data, json_file, ensure_ascii=False, indent=2)


account_group_store = AccountGroupStore()
