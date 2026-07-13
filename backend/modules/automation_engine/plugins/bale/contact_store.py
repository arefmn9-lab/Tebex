from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


CONTACT_STATUSES = {"active", "inactive"}


def _runtime_dir() -> Path:
    backend_dir = Path(__file__).resolve().parents[4]
    path = backend_dir / "runtime" / "platforms" / "bale"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BaleContactError(ValueError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def normalize_bale_phone(phone: str) -> str:
    raw = str(phone or "").strip()
    if not raw:
        raise BaleContactError("invalid_phone", "Phone is required")
    value = raw.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    value = re.sub(r"[\s\-\(\)]", "", value)
    if value.startswith("+"):
        value = value[1:]
    if not value.isdigit():
        raise BaleContactError("invalid_phone", "Phone contains unsupported characters")
    if value.startswith("0098"):
        value = value[2:]
    if value.startswith("0") and len(value) == 11:
        value = f"98{value[1:]}"
    elif len(value) == 10 and value.startswith("9"):
        value = f"98{value}"
    if not (value.startswith("989") and len(value) == 12):
        raise BaleContactError("invalid_phone", "Phone must be a valid Iranian mobile number")
    return value


class BaleContactStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (_runtime_dir() / "contacts.json")

    def list_bale_contacts(self, account_id: str, status: str | None = None) -> list[dict[str, Any]]:
        records = [
            self._normalize_record(item)
            for item in self._read_json([])
            if str(item.get("account_id") or "") == str(account_id)
        ]
        if status:
            records = [item for item in records if item["status"] == status]
        return sorted(records, key=lambda item: int(item["sequence_number"]))

    def get_or_create_bale_contact(self, account_id: str, phone: str) -> tuple[dict[str, Any], bool]:
        account_id = str(account_id or "").strip()
        if not account_id:
            raise BaleContactError("missing_account_id", "account_id is required")
        phone_normalized = normalize_bale_phone(phone)
        records = [self._normalize_record(item) for item in self._read_json([])]
        for record in records:
            if record["account_id"] == account_id and record["phone_normalized"] == phone_normalized:
                return record, False

        self._assert_unique(records, account_id)
        sequence_number = self._next_sequence(records, account_id)
        now = _now()
        record = {
            "id": f"bale_contact_{uuid4().hex[:12]}",
            "account_id": account_id,
            "phone_normalized": phone_normalized,
            "display_name": f"Bale-{sequence_number:06d}",
            "sequence_number": sequence_number,
            "status": "active",
            "created_at": now,
            "updated_at": now,
        }
        records.append(record)
        self._assert_unique(records, account_id)
        self._write_json(records)
        return record, True

    def bulk_add_bale_contacts(self, account_id: str, phones: list[str]) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        created_count = 0
        existing_count = 0
        invalid_count = 0
        for phone in phones:
            try:
                record, created = self.get_or_create_bale_contact(account_id, phone)
                status = "created" if created else "existing"
                created_count += 1 if created else 0
                existing_count += 0 if created else 1
                results.append(
                    {
                        "input": phone,
                        "phone_normalized": record["phone_normalized"],
                        "display_name": record["display_name"],
                        "status": status,
                    }
                )
            except BaleContactError as exc:
                invalid_count += 1
                results.append(
                    {
                        "input": phone,
                        "phone_normalized": None,
                        "display_name": None,
                        "status": "invalid",
                        "error_code": exc.error_code,
                    }
                )
        return {
            "total": len(phones),
            "created_count": created_count,
            "existing_count": existing_count,
            "invalid_count": invalid_count,
            "results": results,
        }

    def update_contact_metadata(self, account_id: str, phone_normalized: str, metadata: dict[str, Any]) -> dict[str, Any] | None:
        records = [self._normalize_record(item) for item in self._read_json([])]
        updated: dict[str, Any] | None = None
        for index, record in enumerate(records):
            if record["account_id"] == str(account_id) and record["phone_normalized"] == str(phone_normalized):
                records[index] = {**record, **metadata, "updated_at": _now()}
                updated = records[index]
                break
        if updated is not None:
            self._assert_unique(records, str(account_id))
            self._write_json(records)
        return updated

    def assert_unique_mapping(self, account_id: str) -> dict[str, Any]:
        records = [self._normalize_record(item) for item in self._read_json([])]
        self._assert_unique(records, str(account_id))
        return {"ok": True, "account_id": str(account_id)}

    def _assert_unique(self, records: list[dict[str, Any]], account_id: str) -> None:
        by_phone: dict[str, str] = {}
        by_name: dict[str, str] = {}
        for record in records:
            if str(record.get("account_id") or "") != account_id:
                continue
            phone = str(record.get("phone_normalized") or "")
            name = str(record.get("display_name") or "")
            if phone in by_phone and by_phone[phone] != name:
                raise BaleContactError("stable_name_phone_collision", "A phone has more than one stable Bale display name")
            if name in by_name and by_name[name] != phone:
                raise BaleContactError("stable_name_collision", "A stable Bale display name belongs to more than one phone")
            by_phone[phone] = name
            by_name[name] = phone

    def _next_sequence(self, records: list[dict[str, Any]], account_id: str) -> int:
        used = {
            int(record.get("sequence_number") or 0)
            for record in records
            if str(record.get("account_id") or "") == account_id
        }
        sequence_number = 1
        while sequence_number in used:
            sequence_number += 1
        return sequence_number

    def _normalize_record(self, record: dict[str, Any]) -> dict[str, Any]:
        created_at = str(record.get("created_at") or _now())
        sequence_number = int(record.get("sequence_number") or 0)
        status = str(record.get("status") or "active")
        if status not in CONTACT_STATUSES:
            status = "active"
        normalized = {
            "id": str(record.get("id") or f"bale_contact_{uuid4().hex[:12]}"),
            "account_id": str(record.get("account_id") or ""),
            "phone_normalized": str(record.get("phone_normalized") or ""),
            "display_name": str(record.get("display_name") or f"Bale-{sequence_number:06d}"),
            "sequence_number": sequence_number,
            "status": status,
            "created_at": created_at,
            "updated_at": str(record.get("updated_at") or created_at),
        }
        for key in [
            "recipient_origin", "synthetic_test_data", "live_execution_authorized",
            "authorization_status", "authorization_source", "authorization_note",
            "should_not_retry", "bale_contact_preexisting", "bale_contact_verified",
            "contact_creation_expected", "local_record_found_before",
            "bale_contact_found_before", "contact_creation_attempted",
            "bale_contact_created", "bale_verified_at", "bale_verification_status",
            "bale_verification_error", "last_verified_account_id",
            "input_manifest_id", "input_manifest_hash", "input_sequence",
            "input_provenance_status", "contact_preparation_allowed",
            "live_execution_blocked", "block_reason", "manual_review_required",
            "creation_trace",
        ]:
            if key in record:
                normalized[key] = record.get(key)
        return normalized

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


bale_contact_store = BaleContactStore()
