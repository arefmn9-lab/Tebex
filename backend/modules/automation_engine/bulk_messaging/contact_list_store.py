from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from .models import ContactListMetadata


CONTACT_STATUSES = {"draft", "imported", "ready"}

DEMO_CONTACT_LISTS = [
    {
        "contact_list_id": "ghab_customers_demo",
        "name": "GHAB Customers Demo",
        "campaign_tag": "GHAB",
        "total_contacts": 12000,
        "valid_contacts": 12000,
        "duplicate_contacts": 0,
        "status": "ready",
        "notes": "Demo metadata only; no contact import implemented.",
    },
    {
        "contact_list_id": "blef_customers_demo",
        "name": "BLEF Customers Demo",
        "campaign_tag": "BLEF",
        "total_contacts": 9000,
        "valid_contacts": 8800,
        "duplicate_contacts": 200,
        "status": "ready",
        "notes": "Demo metadata only; no contact import implemented.",
    },
    {
        "contact_list_id": "lipo_customers_demo",
        "name": "LIPO Customers Demo",
        "campaign_tag": "LIPO",
        "total_contacts": 7500,
        "valid_contacts": 7400,
        "duplicate_contacts": 100,
        "status": "ready",
        "notes": "Demo metadata only; no contact import implemented.",
    },
]


def runtime_path() -> Path:
    return Path(__file__).resolve().parents[3] / "runtime" / "contact_lists.json"


class ContactListStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or runtime_path()
        self._ensure_seed_data()

    def list_contact_lists(self) -> list[dict[str, Any]]:
        return [self._normalize_contact_list(item).to_dict() for item in self._read_json([])]

    def list_platform_contact_lists(self, platform_id: str) -> list[dict[str, Any]]:
        return [
            item
            for item in self.list_contact_lists()
            if not item["platform_id"] or item["platform_id"] == platform_id
        ]

    def get_contact_list(self, contact_list_id: str) -> dict[str, Any] | None:
        for item in self.list_contact_lists():
            if item["contact_list_id"] == contact_list_id:
                return item
        return None

    def create_contact_list(self, payload: dict[str, Any]) -> dict[str, Any]:
        items = self.list_contact_lists()
        contact_list_id = str(payload.get("contact_list_id") or "").strip()
        if not contact_list_id:
            contact_list_id = _slug(str(payload.get("name") or "contacts"))
        if any(item["contact_list_id"] == contact_list_id for item in items):
            raise ValueError(f"Contact list already exists: {contact_list_id}")
        item = self._normalize_contact_list({**payload, "contact_list_id": contact_list_id}).to_dict()
        items.append(item)
        self._write_json(items)
        return item

    def update_contact_list(self, contact_list_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        items = self.list_contact_lists()
        for index, item in enumerate(items):
            if item["contact_list_id"] == contact_list_id:
                updated = self._normalize_contact_list({**item, **payload, "contact_list_id": contact_list_id}).to_dict()
                items[index] = updated
                self._write_json(items)
                return updated
        raise KeyError(contact_list_id)

    def _normalize_contact_list(self, payload: dict[str, Any]) -> ContactListMetadata:
        status = str(payload.get("status") or "draft")
        if status not in CONTACT_STATUSES:
            status = "draft"
        return ContactListMetadata(
            contact_list_id=str(payload.get("contact_list_id") or ""),
            name=str(payload.get("name") or "Contact List"),
            platform_id=str(payload.get("platform_id") or ""),
            campaign_tag=str(payload.get("campaign_tag") or ""),
            source_filename=str(payload.get("source_filename") or ""),
            total_contacts=max(0, int(payload.get("total_contacts", 0))),
            valid_contacts=max(0, int(payload.get("valid_contacts", 0))),
            duplicate_contacts=max(0, int(payload.get("duplicate_contacts", 0))),
            status=status,
            notes=str(payload.get("notes") or ""),
            created_at=str(payload.get("created_at") or ContactListMetadata(contact_list_id="", name="").created_at),
        )

    def _ensure_seed_data(self) -> None:
        existing = self._read_json(None)
        if existing is None:
            self._write_json(deepcopy(DEMO_CONTACT_LISTS))
            return
        items = [self._normalize_contact_list(item).to_dict() for item in existing if isinstance(item, dict)]
        ids = {item["contact_list_id"] for item in items}
        changed = False
        for demo in DEMO_CONTACT_LISTS:
            if demo["contact_list_id"] not in ids:
                items.append(self._normalize_contact_list(demo).to_dict())
                changed = True
        if changed:
            self._write_json(items)

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


def _slug(value: str) -> str:
    safe = "".join(char.lower() if char.isalnum() else "_" for char in value).strip("_")
    return safe or "contact_list"


contact_list_store = ContactListStore()
