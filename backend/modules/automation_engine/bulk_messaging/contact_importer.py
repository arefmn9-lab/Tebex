from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Any
from uuid import uuid4

from .contact_list_store import ContactListStore, contact_list_store
from .contact_store import ContactStore, contact_store


class ContactImporter:
    def __init__(
        self,
        contacts_store: ContactStore | None = None,
        lists_store: ContactListStore | None = None,
    ) -> None:
        self.contacts_store = contacts_store or contact_store
        self.lists_store = lists_store or contact_list_store

    def import_csv(
        self,
        content: bytes,
        filename: str,
        name: str,
        platform_id: str = "",
        campaign_tag: str = "",
        notes: str = "",
    ) -> dict[str, Any]:
        text = content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        contacts: list[dict[str, Any]] = []
        seen: set[str] = set()
        total_rows = 0
        valid_contacts = 0
        invalid_contacts = 0
        duplicate_contacts = 0
        contact_list_id = _contact_list_id(name, campaign_tag)

        for row_number, row in enumerate(reader, start=2):
            total_rows += 1
            raw_phone = _first_value(row, ["phone", "mobile"])
            normalized = normalize_iranian_phone(raw_phone)
            status = "new"
            if not normalized:
                status = "invalid"
                invalid_contacts += 1
            elif normalized in seen:
                status = "duplicate"
                duplicate_contacts += 1
            else:
                seen.add(normalized)
                valid_contacts += 1
            contacts.append(
                {
                    "contact_id": f"{contact_list_id}_{uuid4().hex[:10]}",
                    "contact_list_id": contact_list_id,
                    "raw_phone": raw_phone,
                    "normalized_phone": normalized,
                    "full_name": _first_value(row, ["full_name", "name"]),
                    "city": _first_value(row, ["city"]),
                    "service_taken": _first_value(row, ["service_taken", "service"]),
                    "last_visit_date": _first_value(row, ["last_visit_date"]),
                    "campaign_tag": _first_value(row, ["campaign_tag"]) or campaign_tag,
                    "platform_hint": platform_id,
                    "status": status,
                    "import_row_number": row_number,
                    "notes": _first_value(row, ["notes"]),
                }
            )

        payload = {
            "contact_list_id": contact_list_id,
            "name": name,
            "platform_id": platform_id,
            "campaign_tag": campaign_tag,
            "source_filename": filename,
            "total_contacts": total_rows,
            "valid_contacts": valid_contacts,
            "duplicate_contacts": duplicate_contacts,
            "status": "ready" if valid_contacts > 0 else "draft",
            "notes": notes,
        }
        if self.lists_store.get_contact_list(contact_list_id):
            list_record = self.lists_store.update_contact_list(contact_list_id, payload)
        else:
            list_record = self.lists_store.create_contact_list(payload)
        self.contacts_store.replace_contacts_for_list(contact_list_id, contacts)
        return {
            "ok": True,
            "contact_list_id": contact_list_id,
            "name": list_record["name"],
            "total_rows": total_rows,
            "valid_contacts": valid_contacts,
            "invalid_contacts": invalid_contacts,
            "duplicate_contacts": duplicate_contacts,
            "status": list_record["status"],
            "sample_contacts": [
                {
                    "raw_phone": item["raw_phone"],
                    "normalized_phone": item["normalized_phone"],
                    "full_name": item["full_name"],
                    "status": item["status"],
                }
                for item in contacts[:5]
            ],
            "warnings": [],
        }


def normalize_iranian_phone(value: str) -> str:
    digits = "".join(char for char in str(value or "") if char.isdigit())
    if digits.startswith("0098"):
        digits = digits[2:]
    if digits.startswith("98") and len(digits) == 12:
        normalized = digits
    elif digits.startswith("0") and len(digits) == 11:
        normalized = "98" + digits[1:]
    elif digits.startswith("9") and len(digits) == 10:
        normalized = "98" + digits
    else:
        return ""
    if len(normalized) == 12 and normalized.startswith("989"):
        return normalized
    return ""


def _first_value(row: dict[str, Any], keys: list[str]) -> str:
    normalized_row = {str(key).strip().lower(): value for key, value in row.items()}
    for key in keys:
        value = normalized_row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _contact_list_id(name: str, campaign_tag: str) -> str:
    today = datetime.now().strftime("%Y_%m_%d")
    base = campaign_tag or name or "contacts"
    safe = "".join(char.lower() if char.isalnum() else "_" for char in base).strip("_")
    return f"{safe or 'contacts'}_{today}"


contact_importer = ContactImporter()
