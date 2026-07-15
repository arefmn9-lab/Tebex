from __future__ import annotations

import json
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from modules.automation_engine.plugins.bale.contact_store import BaleContactStore


def _store(tmp_dir: str) -> BaleContactStore:
    return BaleContactStore(Path(tmp_dir) / "contacts.json")


def _phone(index: int) -> str:
    return f"98912{index:07d}"


def test_import_one_new_bale_phone_allocates_first_name() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        contact, created = _store(tmp_dir).get_or_create_platform_contact("bale", _phone(1), prefix="Bale")
    assert created is True
    assert contact["display_name"] == "Bale-000001"
    assert contact["stable_sequence"] == 1


def test_import_ten_new_bale_phones_allocates_sequential_names() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = _store(tmp_dir)
        names = [store.get_or_create_platform_contact("bale", _phone(index), prefix="Bale")[0]["display_name"] for index in range(1, 11)]
    assert names == [f"Bale-{index:06d}" for index in range(1, 11)]


def test_duplicate_and_reimport_return_existing_identity() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = _store(tmp_dir)
        first, first_created = store.get_or_create_platform_contact("bale", "09120000001", prefix="Bale")
        duplicate, duplicate_created = store.get_or_create_platform_contact("bale", "+989120000001", prefix="Bale")
        reimport, reimport_created = store.get_or_create_platform_contact("bale", "00989120000001", prefix="Bale")
    assert first_created is True
    assert duplicate_created is False
    assert reimport_created is False
    assert {first["id"], duplicate["id"], reimport["id"]} == {first["id"]}
    assert duplicate["display_name"] == "Bale-000001"


def test_existing_and_new_mix_only_new_phones_consume_sequence() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = _store(tmp_dir)
        store.get_or_create_platform_contact("bale", _phone(1), prefix="Bale")
        existing, existing_created = store.get_or_create_platform_contact("bale", _phone(1), prefix="Bale")
        new_contact, new_created = store.get_or_create_platform_contact("bale", _phone(2), prefix="Bale")
    assert existing_created is False
    assert existing["display_name"] == "Bale-000001"
    assert new_created is True
    assert new_contact["display_name"] == "Bale-000002"


def test_different_platforms_have_independent_prefixes_and_sequences() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = _store(tmp_dir)
        bale, _ = store.get_or_create_platform_contact("bale", _phone(1), prefix="Bale")
        telegram, _ = store.get_or_create_platform_contact("telegram", _phone(1), prefix="Telegram")
    assert bale["display_name"] == "Bale-000001"
    assert telegram["display_name"] == "Telegram-000001"


def test_archived_sequence_is_not_reused() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = _store(tmp_dir)
        first, _ = store.get_or_create_platform_contact("bale", _phone(1), prefix="Bale")
        store.update_contact_metadata("", first["phone_normalized"], {"status": "archived"})
        second, _ = store.get_or_create_platform_contact("bale", _phone(2), prefix="Bale")
    assert second["display_name"] == "Bale-000002"


def test_concurrent_allocation_attempts_do_not_duplicate_name() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = _store(tmp_dir)
        phones = [_phone(index) for index in range(1, 51)]
        with ThreadPoolExecutor(max_workers=8) as executor:
            contacts = list(executor.map(lambda phone: store.get_or_create_platform_contact("bale", phone, prefix="Bale")[0], phones))
    names = [contact["display_name"] for contact in contacts]
    assert len(names) == len(set(names))
    assert sorted(names) == [f"Bale-{index:06d}" for index in range(1, 51)]


def test_preview_dry_run_does_not_allocate_stable_names() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        path = Path(tmp_dir) / "contacts.json"
        store = BaleContactStore(path)
        assert store.get_platform_contact("bale", _phone(1)) is None
        assert not path.exists()


def test_import_six_thousand_rows_without_sending() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = _store(tmp_dir)
        result = store.bulk_add_platform_contacts("bale", [_phone(index) for index in range(1, 6001)], prefix="Bale")
        records = json.loads((Path(tmp_dir) / "contacts.json").read_text(encoding="utf-8"))
    assert result["created_count"] == 6000
    assert len(records) == 6000
    assert records[0]["display_name"] == "Bale-000001"
    assert records[-1]["display_name"] == "Bale-006000"


if __name__ == "__main__":
    test_import_one_new_bale_phone_allocates_first_name()
    test_import_ten_new_bale_phones_allocates_sequential_names()
    test_duplicate_and_reimport_return_existing_identity()
    test_existing_and_new_mix_only_new_phones_consume_sequence()
    test_different_platforms_have_independent_prefixes_and_sequences()
    test_archived_sequence_is_not_reused()
    test_concurrent_allocation_attempts_do_not_duplicate_name()
    test_preview_dry_run_does_not_allocate_stable_names()
    test_import_six_thousand_rows_without_sending()
    print("Stable contact naming tests passed")
