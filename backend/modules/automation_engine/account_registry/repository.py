from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any


def backend_runtime_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "runtime"


class JsonAccountRegistryRepository:
    """Persistent non-Bale registry storage.

    Bale remains delegated to BaleAccountStore. This repository stores canonical
    identity and platform account records for platforms that do not already have
    a production account store.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or backend_runtime_dir() / "account_registry" / "registry.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def list_identities(self) -> list[dict[str, Any]]:
        return deepcopy(self._read()["identities"])

    def upsert_identity(self, identity: dict[str, Any]) -> dict[str, Any]:
        data = self._read()
        identities = data["identities"]
        identity_id = str(identity.get("identity_id") or "").strip()
        if not identity_id:
            raise ValueError("identity_id is required")
        for index, existing in enumerate(identities):
            if str(existing.get("identity_id")) == identity_id:
                identities[index] = deepcopy(identity)
                self._write(data)
                return deepcopy(identity)
        identities.append(deepcopy(identity))
        self._write(data)
        return deepcopy(identity)

    def list_platform_accounts(self, platform_id: str) -> list[dict[str, Any]]:
        return [
            deepcopy(account)
            for account in self._read()["platform_accounts"]
            if str(account.get("platform_id") or account.get("platform")) == platform_id
        ]

    def list_all_platform_accounts(self) -> list[dict[str, Any]]:
        return deepcopy(self._read()["platform_accounts"])

    def get_platform_account(self, platform_id: str, account_id: str) -> dict[str, Any] | None:
        for account in self.list_platform_accounts(platform_id):
            if str(account.get("account_id") or "") == account_id:
                return account
        return None

    def create_platform_account(self, account: dict[str, Any]) -> dict[str, Any]:
        data = self._read()
        platform_id = str(account.get("platform_id") or account.get("platform") or "").strip()
        account_id = str(account.get("account_id") or "").strip()
        if not platform_id or not account_id:
            raise ValueError("platform_id and account_id are required")
        if any(
            str(item.get("platform_id") or item.get("platform")) == platform_id
            and str(item.get("account_id") or "") == account_id
            for item in data["platform_accounts"]
        ):
            raise ValueError(f"Account already exists: {platform_id}/{account_id}")
        data["platform_accounts"].append(deepcopy(account))
        self._write(data)
        return deepcopy(account)

    def update_platform_account(self, platform_id: str, account_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        data = self._read()
        for index, account in enumerate(data["platform_accounts"]):
            if str(account.get("platform_id") or account.get("platform")) == platform_id and str(account.get("account_id") or "") == account_id:
                updated = {**account, **deepcopy(updates), "platform_id": platform_id, "platform": platform_id, "account_id": account_id}
                data["platform_accounts"][index] = updated
                self._write(data)
                return deepcopy(updated)
        raise KeyError(account_id)

    def delete_platform_account(self, platform_id: str, account_id: str) -> dict[str, Any]:
        data = self._read()
        before = len(data["platform_accounts"])
        data["platform_accounts"] = [
            account
            for account in data["platform_accounts"]
            if not (str(account.get("platform_id") or account.get("platform")) == platform_id and str(account.get("account_id") or "") == account_id)
        ]
        if len(data["platform_accounts"]) == before:
            raise KeyError(account_id)
        self._write(data)
        return {"ok": True, "platform_id": platform_id, "account_id": account_id}

    def _read(self) -> dict[str, Any]:
        try:
            with self.path.open("r", encoding="utf-8") as json_file:
                data = json.load(json_file)
        except Exception:
            data = {}
        identities = data.get("identities") if isinstance(data.get("identities"), list) else []
        accounts = data.get("platform_accounts") if isinstance(data.get("platform_accounts"), list) else []
        return {"identities": identities, "platform_accounts": accounts}

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        with tmp_path.open("w", encoding="utf-8") as json_file:
            json.dump(data, json_file, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self.path)
