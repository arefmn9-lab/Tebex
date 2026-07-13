from __future__ import annotations

import tempfile
import time
import zipfile
from pathlib import Path

from modules.automation_engine.commercial_queue.import_pipeline import parse_csv_bytes, parse_paste_content, parse_xlsx_bytes
from modules.automation_engine.commercial_queue.repository import CommercialQueueRepository
from modules.automation_engine.commercial_queue.service import CommercialQueueService


def _service(path: Path) -> CommercialQueueService:
    return CommercialQueueService(
        repository=CommercialQueueRepository(path),
        orchestrator=lambda **payload: {"success": True, "dry_run": True},
        account_auth_checker=lambda account_id: True,
        sleeper=lambda seconds: None,
    )


def _campaign(service: CommercialQueueService) -> dict:
    return service.create_campaign({"name": "Import Test", "platform": "bale", "source_channel_uid": "5613544284"})


def _xlsx_bytes(rows: list[list[str]]) -> bytes:
    def cell_ref(column: int, row: int) -> str:
        letters = ""
        column += 1
        while column:
            column, rem = divmod(column - 1, 26)
            letters = chr(65 + rem) + letters
        return f"{letters}{row}"

    sheet_rows = []
    for row_index, row in enumerate(rows, start=1):
        cells = []
        for column_index, value in enumerate(row):
            ref = cell_ref(column_index, row_index)
            cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>')
        sheet_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    sheet = f'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{"".join(sheet_rows)}</sheetData></worksheet>'
    workbook = '<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Contacts" sheetId="1" r:id="rId1"/></sheets></workbook>'
    rels = '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>'
    content_types = '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>'
    path = Path(tempfile.mkdtemp()) / "book.xlsx"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("xl/workbook.xml", workbook)
        zf.writestr("xl/_rels/workbook.xml.rels", rels)
        zf.writestr("xl/worksheets/sheet1.xml", sheet)
    return path.read_bytes()


def test_paste_parser_separators_and_digits() -> None:
    rows = parse_paste_content("09120000001, ۰۹۱۲۰۰۰۰۰۰۲؛09120000003\t09120000004")
    assert [row.phone_raw for row in rows] == ["09120000001", "۰۹۱۲۰۰۰۰۰۰۲", "09120000003", "09120000004"]


def test_preview_validation_and_duplicates() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "imports.db")
        campaign = _campaign(service)
        service.import_recipients(campaign["id"], ["09120000001"], "seed")
        preview = service.preview_paste_import(campaign["id"], "09120000001\n09120000002\nbad\n09120000002")
    assert preview["submitted_count"] == 4
    assert preview["valid_count"] == 1
    assert preview["invalid_count"] == 1
    assert preview["duplicate_count"] == 2
    statuses = [item["validation_status"] for item in preview["preview_items"]]
    assert statuses == ["duplicate", "valid", "invalid", "duplicate"]


def test_csv_bom_delimiters_and_column_detection() -> None:
    rows, metadata = parse_csv_bytes("\ufeffname;phone\nAli;09120000001\nSara;09120000002\n".encode("utf-8"), "contacts.csv")
    assert len(rows) == 2
    assert rows[0].display_name == "Ali"
    assert metadata["phone_column"] == "phone"
    tab_rows, _ = parse_csv_bytes("phone\tname\n09120000003\tReza\n".encode("utf-8"), "contacts.csv")
    assert tab_rows[0].phone_raw == "09120000003"


def test_csv_missing_column_and_malformed_utf8() -> None:
    try:
        parse_csv_bytes("name\nAli\n".encode("utf-8"), "contacts.csv")
    except Exception as exc:
        assert getattr(exc, "error_code", "") == "missing_phone_column"
    else:
        raise AssertionError("missing phone column was accepted")
    try:
        parse_csv_bytes(b"\xff\xfe\x00", "contacts.csv")
    except Exception as exc:
        assert getattr(exc, "error_code", "") == "malformed_csv"
    else:
        raise AssertionError("malformed CSV was accepted")


def test_xlsx_sheet_and_column_selection() -> None:
    rows, metadata = parse_xlsx_bytes(_xlsx_bytes([["name", "phone"], ["Ali", "09120000001"]]), "contacts.xlsx")
    assert len(rows) == 1
    assert rows[0].phone_raw == "09120000001"
    assert rows[0].display_name == "Ali"
    assert metadata["selected_sheet"] == "Contacts"
    selected_rows, selected_metadata = parse_xlsx_bytes(_xlsx_bytes([["mobile", "name"], ["09120000002", "Sara"]]), "contacts.xlsx", phone_column="mobile", display_name_column="name")
    assert selected_rows[0].display_name == "Sara"
    assert selected_metadata["phone_column"] == "mobile"


def test_malformed_xlsx_rejected() -> None:
    try:
        parse_xlsx_bytes(b"not-a-zip", "bad.xlsx")
    except Exception as exc:
        assert getattr(exc, "error_code", "") == "malformed_excel"
    else:
        raise AssertionError("malformed workbook was accepted")


def test_confirm_import_idempotency_persistence_and_delete_rules() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "imports.db"
        service = _service(db_path)
        campaign = _campaign(service)
        preview = service.preview_paste_import(campaign["id"], "09120000001\n09120000002")
        reloaded = _service(db_path)
        batch = reloaded.get_import_batch(preview["batch_id"])
        items = reloaded.list_import_items(preview["batch_id"])["items"]
        confirm = reloaded.confirm_import_batch(preview["batch_id"], default_priority=7)
        retry = reloaded.confirm_import_batch(preview["batch_id"], default_priority=7)
        campaign_after = reloaded.get_campaign(campaign["id"])
        delete_confirmed = reloaded.delete_import_batch(preview["batch_id"])

        second = reloaded.preview_paste_import(campaign["id"], "09120000003")
        delete_unconfirmed = reloaded.delete_import_batch(second["batch_id"])

    assert batch is not None and batch["status"] == "preview_ready"
    assert len(items) == 2
    assert confirm["created_recipient_count"] == 2
    assert confirm["created_job_count"] == 2
    assert retry["created_recipient_count"] == 2
    assert retry["idempotent_replay"] is True
    assert campaign_after["queued_count"] == 2
    assert delete_confirmed["deleted"] is False
    assert delete_confirmed["reason"] == "batch_already_confirmed"
    assert delete_unconfirmed["deleted"] is True


def test_confirm_selected_items_only() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "imports.db")
        campaign = _campaign(service)
        preview = service.preview_paste_import(campaign["id"], "09120000001\n09120000002")
        items = service.list_import_items(preview["batch_id"])["items"]
        result = service.confirm_import_batch(preview["batch_id"], selected_item_ids=[items[0]["id"]])
    assert result["created_recipient_count"] == 1
    assert result["created_job_count"] == 1


def test_6000_row_preview_pagination_duration() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "imports.db")
        campaign = _campaign(service)
        phones = "\n".join(f"0912{index:07d}" for index in range(6000))
        started = time.perf_counter()
        preview = service.preview_paste_import(campaign["id"], phones)
        duration = time.perf_counter() - started
        page = service.list_import_items(preview["batch_id"], limit=100, offset=5900)
    assert preview["submitted_count"] == 6000
    assert preview["valid_count"] == 6000
    assert len(preview["preview_items"]) == 100
    assert preview["has_more"] is True
    assert len(page["items"]) == 100
    assert duration < 10


if __name__ == "__main__":
    test_paste_parser_separators_and_digits()
    test_preview_validation_and_duplicates()
    test_csv_bom_delimiters_and_column_detection()
    test_csv_missing_column_and_malformed_utf8()
    test_xlsx_sheet_and_column_selection()
    test_malformed_xlsx_rejected()
    test_confirm_import_idempotency_persistence_and_delete_rules()
    test_confirm_selected_items_only()
    test_6000_row_preview_pagination_duration()
    print("Commercial import tests passed")
