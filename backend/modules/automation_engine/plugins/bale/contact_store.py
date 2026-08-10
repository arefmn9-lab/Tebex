from __future__ import annotations

import json
import re
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


CONTACT_STATUSES = {"active", "inactive", "archived", "deleted"}
PLATFORM_PREFIXES = {"bale": "Bale", "telegram": "Telegram", "whatsapp": "WhatsApp"}


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
        self._lock = threading.RLock()

    def list_bale_contacts(self, account_id: str, status: str | None = None) -> list[dict[str, Any]]:
        records = [
            bound
            for item in self._read_json([])
            for bound in [self._record_for_account(self._normalize_record(item), str(account_id))]
            if bound is not None
        ]
        if status:
            records = [item for item in records if item["status"] == status]
        return sorted(records, key=lambda item: int(item["sequence_number"]))

    def get_or_create_bale_contact(self, account_id: str, phone: str) -> tuple[dict[str, Any], bool]:
        return self.get_or_create_platform_contact("bale", phone, account_id=account_id, prefix="Bale")

    def get_bale_contact(self, account_id: str, phone: str) -> dict[str, Any] | None:
        return self.get_platform_contact("bale", phone, account_id=account_id)

    def get_platform_contact(self, platform: str, phone: str, account_id: str | None = None) -> dict[str, Any] | None:
        platform_id = self._normalize_platform(platform)
        phone_normalized = normalize_bale_phone(phone)
        records = [self._normalize_record(item) for item in self._read_json([])]
        for record in records:
            if record["platform"] == platform_id and record["phone_normalized"] == phone_normalized:
                if account_id is None:
                    return record
                return self._record_for_account(record, str(account_id))
        return None

    def get_or_create_platform_contact(
        self,
        platform: str,
        phone: str,
        account_id: str | None = None,
        prefix: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        platform_id = self._normalize_platform(platform)
        account_id = str(account_id or "").strip()
        phone_normalized = normalize_bale_phone(phone)
        with self._lock:
            records = [self._normalize_record(item) for item in self._read_json([])]
            for index, record in enumerate(records):
                if record["platform"] == platform_id and record["phone_normalized"] == phone_normalized:
                    binding_created = self._ensure_account_binding(record, account_id)
                    if binding_created:
                        records[index] = record
                        self._write_json(records)
                    bound_record = self._record_for_account(record, account_id) if account_id else record
                    return bound_record or record, binding_created

            self._assert_unique(records, platform_id)
            sequence_number = self._next_sequence(records, platform_id)
            display_prefix = self._display_prefix(platform_id, prefix)
            now = _now()
            stable_name = f"{display_prefix}-{sequence_number:06d}"
            record = {
                "id": f"{platform_id}_contact_{uuid4().hex[:12]}",
                "platform": platform_id,
                "account_id": account_id,
                "phone_normalized": phone_normalized,
                "display_name": stable_name,
                "sequence_number": sequence_number,
                "stable_name": stable_name,
                "stable_sequence": sequence_number,
                "status": "active",
                "account_bindings": [self._new_account_binding(account_id, now)] if account_id else [],
                "created_at": now,
                "updated_at": now,
            }
            records.append(record)
            self._assert_unique(records, platform_id)
            self._write_json(records)
            return record, True

    def bulk_add_bale_contacts(self, account_id: str, phones: list[str]) -> dict[str, Any]:
        return self.bulk_add_platform_contacts("bale", phones, account_id=account_id, prefix="Bale")

    def ensure_stable_mapping(self, phone: str, display_name: str, account_id: str | None = None) -> dict[str, Any]:
        """Mirror the database-allocated canonical mapping without renumbering it."""
        normalized = normalize_bale_phone(phone)
        expected = str(display_name or "").strip()
        match = re.fullmatch(r"Bale-(\d{6,})", expected)
        if not match:
            raise BaleContactError("invalid_stable_display_name", "Canonical Bale display name is invalid")
        with self._lock:
            records = [self._normalize_record(item) for item in self._read_json([])]
            # The SQLite global-contact mapping is authoritative. Remove only
            # stale mirror rows that claim its exact canonical name for a
            # different phone; never move or renumber the database mapping.
            records = [
                record for record in records
                if not (record["platform"] == "bale" and record["display_name"] == expected and record["phone_normalized"] != normalized)
            ]
            for index, record in enumerate(records):
                if record["platform"] == "bale" and record["phone_normalized"] == normalized:
                    if record["display_name"] != expected:
                        record["display_name"] = expected
                        record["stable_name"] = expected
                        record["sequence_number"] = int(match.group(1))
                        record["stable_sequence"] = int(match.group(1))
                        record["updated_at"] = _now()
                        records[index] = record
                        self._assert_unique(records, "bale")
                        self._write_json(records)
                    if self._ensure_account_binding(record, str(account_id or "")):
                        records[index] = record
                        self._write_json(records)
                    return self._record_for_account(record, str(account_id)) if account_id else record
            now = _now()
            sequence = int(match.group(1))
            record = {
                "id": f"bale_contact_{uuid4().hex[:12]}", "platform": "bale",
                "account_id": str(account_id or ""), "phone_normalized": normalized,
                "display_name": expected, "sequence_number": sequence,
                "stable_name": expected, "stable_sequence": sequence, "status": "active",
                "account_bindings": [self._new_account_binding(str(account_id), now)] if account_id else [],
                "created_at": now, "updated_at": now,
            }
            records.append(record)
            self._assert_unique(records, "bale")
            self._write_json(records)
            return self._record_for_account(record, str(account_id)) if account_id else record

    def bulk_add_platform_contacts(
        self,
        platform: str,
        phones: list[str],
        account_id: str | None = None,
        prefix: str | None = None,
    ) -> dict[str, Any]:
        platform_id = self._normalize_platform(platform)
        account_id = str(account_id or "").strip()
        results: list[dict[str, Any]] = []
        created_count = 0
        existing_count = 0
        invalid_count = 0
        with self._lock:
            records = [self._normalize_record(item) for item in self._read_json([])]
            by_phone = {
                str(record["phone_normalized"]): record
                for record in records
                if record["platform"] == platform_id
            }
            next_sequence = self._next_sequence(records, platform_id)
            display_prefix = self._display_prefix(platform_id, prefix)
            changed = False
            seen_in_request: set[str] = set()
            for phone in phones:
                try:
                    normalized = normalize_bale_phone(phone)
                    record = by_phone.get(normalized)
                    if record is not None:
                        if self._ensure_account_binding(record, account_id):
                            changed = True
                        created = False
                    elif normalized in seen_in_request:
                        record = by_phone[normalized]
                        created = False
                    else:
                        now = _now()
                        stable_name = f"{display_prefix}-{next_sequence:06d}"
                        record = {
                            "id": f"{platform_id}_contact_{uuid4().hex[:12]}",
                            "platform": platform_id,
                            "account_id": account_id,
                            "phone_normalized": normalized,
                            "display_name": stable_name,
                            "sequence_number": next_sequence,
                            "stable_name": stable_name,
                            "stable_sequence": next_sequence,
                            "status": "active",
                            "account_bindings": [self._new_account_binding(account_id, now)] if account_id else [],
                            "created_at": now,
                            "updated_at": now,
                        }
                        records.append(record)
                        by_phone[normalized] = record
                        next_sequence += 1
                        created = True
                        changed = True
                    seen_in_request.add(normalized)
                    status = "created" if created else "existing"
                    created_count += 1 if created else 0
                    existing_count += 0 if created else 1
                    results.append({
                        "input": phone,
                        "phone_normalized": record["phone_normalized"],
                        "display_name": record["display_name"],
                        "status": status,
                    })
                except BaleContactError as exc:
                    invalid_count += 1
                    results.append({
                        "input": phone,
                        "phone_normalized": None,
                        "display_name": None,
                        "status": "invalid",
                        "error_code": exc.error_code,
                    })
            if changed:
                self._assert_unique(records, platform_id)
                self._write_json(records)
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
            if record["phone_normalized"] == str(phone_normalized):
                binding = self._get_account_binding(record, str(account_id))
                if binding is None:
                    continue
                binding.update(metadata)
                binding["updated_at"] = _now()
                record["updated_at"] = binding["updated_at"]
                records[index] = record
                updated = self._record_for_account(record, str(account_id))
                break
        if updated is not None:
            self._assert_unique(records, "bale")
            self._write_json(records)
        return updated

    def assert_unique_mapping(self, account_id: str) -> dict[str, Any]:
        records = [self._normalize_record(item) for item in self._read_json([])]
        self._assert_unique(records, "bale")
        return {"ok": True, "account_id": str(account_id)}

    def _assert_unique(self, records: list[dict[str, Any]], platform: str) -> None:
        platform_id = self._normalize_platform(platform)
        by_phone: dict[str, str] = {}
        by_name: dict[str, str] = {}
        for record in records:
            if self._normalize_platform(str(record.get("platform") or "bale")) != platform_id:
                continue
            phone = str(record.get("phone_normalized") or "")
            name = str(record.get("display_name") or "")
            if phone in by_phone and by_phone[phone] != name:
                raise BaleContactError("stable_name_phone_collision", "A phone has more than one stable Bale display name")
            if name in by_name and by_name[name] != phone:
                raise BaleContactError("stable_name_collision", "A stable Bale display name belongs to more than one phone")
            by_phone[phone] = name
            by_name[name] = phone

    def _next_sequence(self, records: list[dict[str, Any]], platform: str) -> int:
        platform_id = self._normalize_platform(platform)
        used = {
            int(record.get("sequence_number") or 0)
            for record in records
            if self._normalize_platform(str(record.get("platform") or "bale")) == platform_id
        }
        return max(used or {0}) + 1

    def _normalize_record(self, record: dict[str, Any]) -> dict[str, Any]:
        created_at = str(record.get("created_at") or _now())
        sequence_number = int(record.get("sequence_number") or 0)
        status = str(record.get("status") or "active")
        if status not in CONTACT_STATUSES:
            status = "active"
        bindings = record.get("account_bindings")
        normalized_bindings = [self._normalize_binding(binding) for binding in bindings] if isinstance(bindings, list) else []
        legacy_account_id = str(record.get("account_id") or "")
        if legacy_account_id and not any(binding["account_id"] == legacy_account_id for binding in normalized_bindings):
            legacy_binding = self._new_account_binding(legacy_account_id, created_at)
            for key in self._binding_metadata_keys():
                if key in record:
                    legacy_binding[key] = record.get(key)
            normalized_bindings.append(self._normalize_binding(legacy_binding))
        normalized = {
            "id": str(record.get("id") or f"bale_contact_{uuid4().hex[:12]}"),
            "platform": self._normalize_platform(str(record.get("platform") or "bale")),
            "account_id": legacy_account_id or (normalized_bindings[0]["account_id"] if normalized_bindings else ""),
            "phone_normalized": str(record.get("phone_normalized") or ""),
            "display_name": str(record.get("display_name") or f"Bale-{sequence_number:06d}"),
            "sequence_number": sequence_number,
            "stable_name": str(record.get("stable_name") or record.get("display_name") or f"Bale-{sequence_number:06d}"),
            "stable_sequence": int(record.get("stable_sequence") or sequence_number),
            "status": status,
            "platform_contact_identity_id": str(record.get("platform_contact_identity_id") or record.get("id") or f"bale_contact_{uuid4().hex[:12]}"),
            "account_bindings": normalized_bindings,
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

    def list_platform_contact_identities(self, platform: str = "bale") -> list[dict[str, Any]]:
        platform_id = self._normalize_platform(platform)
        return [
            record for record in [self._normalize_record(item) for item in self._read_json([])]
            if record["platform"] == platform_id
        ]

    def list_account_contact_bindings(self, account_id: str, platform: str = "bale") -> list[dict[str, Any]]:
        platform_id = self._normalize_platform(platform)
        account = str(account_id)
        bindings: list[dict[str, Any]] = []
        for record in [self._normalize_record(item) for item in self._read_json([])]:
            if record["platform"] != platform_id:
                continue
            binding = self._get_account_binding(record, account)
            if binding is not None:
                bindings.append({
                    **binding,
                    "platform": record["platform"],
                    "platform_contact_identity_id": record["id"],
                    "phone_normalized": record["phone_normalized"],
                    "stable_name": record["stable_name"],
                    "stable_sequence": record["stable_sequence"],
                    "display_name": record["display_name"],
                })
        return sorted(bindings, key=lambda item: int(item["stable_sequence"]))

    def _record_for_account(self, record: dict[str, Any], account_id: str) -> dict[str, Any] | None:
        binding = self._get_account_binding(record, account_id)
        if binding is None:
            return None
        return {
            **record,
            **{key: value for key, value in binding.items() if key != "id"},
            "id": record["id"],
            "account_id": binding["account_id"],
            "account_binding_id": binding["id"],
            "platform_contact_identity_id": record["id"],
        }

    def _get_account_binding(self, record: dict[str, Any], account_id: str) -> dict[str, Any] | None:
        account = str(account_id or "")
        for binding in record.get("account_bindings") or []:
            if str(binding.get("account_id") or "") == account:
                return binding
        return None

    def _ensure_account_binding(self, record: dict[str, Any], account_id: str) -> bool:
        account = str(account_id or "").strip()
        if not account:
            return False
        if self._get_account_binding(record, account) is not None:
            return False
        record.setdefault("account_bindings", []).append(self._new_account_binding(account, _now()))
        record["updated_at"] = _now()
        return True

    def _new_account_binding(self, account_id: str, created_at: str) -> dict[str, Any]:
        return {
            "id": f"binding_{uuid4().hex[:12]}",
            "account_id": str(account_id),
            "preparation_status": "not_prepared",
            "verification_status": "unverified",
            "status": "active",
            "created_at": created_at,
            "updated_at": created_at,
        }

    def _normalize_binding(self, binding: dict[str, Any]) -> dict[str, Any]:
        created_at = str(binding.get("created_at") or _now())
        normalized = {
            "id": str(binding.get("id") or f"binding_{uuid4().hex[:12]}"),
            "account_id": str(binding.get("account_id") or ""),
            "preparation_status": str(binding.get("preparation_status") or "not_prepared"),
            "verification_status": str(binding.get("verification_status") or "unverified"),
            "status": str(binding.get("status") or "active"),
            "created_at": created_at,
            "updated_at": str(binding.get("updated_at") or created_at),
        }
        for key in self._binding_metadata_keys():
            if key in binding:
                normalized[key] = binding.get(key)
        return normalized

    def _binding_metadata_keys(self) -> list[str]:
        return [
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
            "creation_trace", "failure_code", "failure_message",
            "verification_method", "prepared_at", "verified_at",
            "profile_identity", "browser_pid", "last_successful_step",
            "failure_evidence",
        ]

    def _normalize_platform(self, platform: str) -> str:
        value = str(platform or "").strip().lower()
        return value or "bale"

    def _display_prefix(self, platform: str, prefix: str | None = None) -> str:
        raw = str(prefix or PLATFORM_PREFIXES.get(self._normalize_platform(platform), platform.title())).strip()
        cleaned = re.sub(r"[^A-Za-z0-9_-]", "", raw)
        return cleaned or self._normalize_platform(platform).title()

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
