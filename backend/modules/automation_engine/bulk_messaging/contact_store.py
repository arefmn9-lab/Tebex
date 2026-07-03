from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from .models import BulkContact


CONTACT_STATUSES = {"new", "assigned", "contacted", "replied", "invalid", "duplicate"}


def runtime_path() -> Path:
    return Path(__file__).resolve().parents[3] / "runtime" / "bulk_contacts.json"


class ContactStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or runtime_path()

    def list_contacts(self, contact_list_id: str | None = None) -> list[dict[str, Any]]:
        contacts = [self._normalize_contact(item).to_dict() for item in self._read_json([])]
        if contact_list_id is None:
            return contacts
        return [item for item in contacts if item["contact_list_id"] == contact_list_id]

    def replace_contacts_for_list(self, contact_list_id: str, contacts: list[dict[str, Any]]) -> None:
        existing = [item for item in self.list_contacts() if item["contact_list_id"] != contact_list_id]
        normalized = [self._normalize_contact({**item, "contact_list_id": contact_list_id}).to_dict() for item in contacts]
        self._write_json(existing + normalized)

    def summary(self, contact_list_id: str) -> dict[str, Any]:
        contacts = self.list_contacts(contact_list_id)
        valid = [item for item in contacts if item["status"] in {"new", "assigned", "contacted", "replied"}]
        invalid = [item for item in contacts if item["status"] == "invalid"]
        duplicates = [item for item in contacts if item["status"] == "duplicate"]
        return {
            "contact_list_id": contact_list_id,
            "total_contacts": len(contacts),
            "valid_contacts": len(valid),
            "invalid_contacts": len(invalid),
            "duplicate_contacts": len(duplicates),
        }

    def valid_contact_count(self, contact_list_id: str) -> int | None:
        contacts = self.list_contacts(contact_list_id)
        if not contacts:
            return None
        return self.summary(contact_list_id)["valid_contacts"]

    def _normalize_contact(self, payload: dict[str, Any]) -> BulkContact:
        status = str(payload.get("status") or "new")
        if status not in CONTACT_STATUSES:
            status = "new"
        return BulkContact(
            contact_id=str(payload.get("contact_id") or ""),
            contact_list_id=str(payload.get("contact_list_id") or ""),
            raw_phone=str(payload.get("raw_phone") or ""),
            normalized_phone=str(payload.get("normalized_phone") or ""),
            full_name=str(payload.get("full_name") or ""),
            city=str(payload.get("city") or ""),
            service_taken=str(payload.get("service_taken") or ""),
            last_visit_date=str(payload.get("last_visit_date") or ""),
            campaign_tag=str(payload.get("campaign_tag") or ""),
            platform_hint=str(payload.get("platform_hint") or ""),
            status=status,
            import_row_number=int(payload.get("import_row_number", 0)),
            notes=str(payload.get("notes") or ""),
            created_at=str(payload.get("created_at") or BulkContact(contact_id="", contact_list_id="", raw_phone="", normalized_phone="").created_at),
        )

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


contact_store = ContactStore()
