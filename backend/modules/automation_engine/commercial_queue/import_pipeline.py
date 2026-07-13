from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from modules.automation_engine.plugins.bale.contact_store import BaleContactError, normalize_bale_phone


MAX_IMPORT_ROWS = 10000
PREVIEW_PAGE_SIZE = 100
MAX_UPLOAD_SIZE_MB = 10


class ImportParseError(ValueError):
    def __init__(self, error_code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.details = details or {}


@dataclass
class ParsedRow:
    row_number: int
    phone_raw: str
    display_name: str | None = None


def safe_filename(filename: str | None) -> str | None:
    if not filename:
        return None
    return Path(str(filename)).name[:180]


def parse_paste_content(content: str, max_rows: int = MAX_IMPORT_ROWS) -> list[ParsedRow]:
    text = str(content or "").strip()
    if not text:
        return []
    chunks: list[str] = []
    for line in re.split(r"[\r\n,;\t،؛]+", text):
        line = line.strip()
        if not line:
            continue
        try:
            normalize_bale_phone(line)
            chunks.append(line)
            continue
        except BaleContactError:
            pass
        parts = [part for part in re.split(r"\s+", line) if part]
        chunks.extend(parts if len(parts) > 1 else [line])
    if len(chunks) > max_rows:
        raise ImportParseError("max_rows_exceeded", f"Import row limit exceeded: {max_rows}")
    return [ParsedRow(index + 1, chunk) for index, chunk in enumerate(chunks)]


def _looks_like_phone_header(value: str) -> bool:
    normalized = str(value or "").strip().lower().replace(" ", "_")
    return normalized in {"phone", "mobile", "number", "phone_number", "mobile_number", "شماره", "تلفن", "موبایل"}


def _looks_like_name_header(value: str) -> bool:
    normalized = str(value or "").strip().lower().replace(" ", "_")
    return normalized in {"name", "display_name", "full_name", "نام"}


def _detect_columns(rows: list[list[str]], phone_column: str | None = None, display_name_column: str | None = None) -> tuple[int, int | None, bool, list[str]]:
    if not rows:
        raise ImportParseError("missing_phone_column", "No rows found")
    first = [str(cell or "").strip() for cell in rows[0]]
    has_header = any(_looks_like_phone_header(cell) or _looks_like_name_header(cell) for cell in first)
    headers = first if has_header else [f"column_{index + 1}" for index in range(max(len(row) for row in rows))]
    phone_index: int | None = None
    name_index: int | None = None
    if phone_column:
        if phone_column in headers:
            phone_index = headers.index(phone_column)
        elif str(phone_column).isdigit():
            phone_index = max(0, int(phone_column) - 1)
    if display_name_column:
        if display_name_column in headers:
            name_index = headers.index(display_name_column)
        elif str(display_name_column).isdigit():
            name_index = max(0, int(display_name_column) - 1)
    if phone_index is None:
        header_matches = [index for index, cell in enumerate(headers) if _looks_like_phone_header(cell)]
        if len(header_matches) == 1:
            phone_index = header_matches[0]
    if name_index is None:
        header_matches = [index for index, cell in enumerate(headers) if _looks_like_name_header(cell)]
        if len(header_matches) == 1:
            name_index = header_matches[0]
    if phone_index is None:
        scores: list[tuple[int, int]] = []
        data_rows = rows[1:] if has_header else rows
        for index in range(len(headers)):
            score = 0
            for row in data_rows[:25]:
                if index >= len(row):
                    continue
                try:
                    normalize_bale_phone(str(row[index]))
                    score += 1
                except BaleContactError:
                    pass
            if score:
                scores.append((score, index))
        scores.sort(reverse=True)
        if len(scores) == 1 or (scores and scores[0][0] > scores[1][0]):
            phone_index = scores[0][1]
    if phone_index is None:
        raise ImportParseError("missing_phone_column", "Phone column could not be detected", {"columns": headers, "header_detected": has_header})
    return phone_index, name_index, has_header, headers


def parse_csv_bytes(
    data: bytes,
    filename: str | None = None,
    phone_column: str | None = None,
    display_name_column: str | None = None,
    max_rows: int = MAX_IMPORT_ROWS,
) -> tuple[list[ParsedRow], dict[str, Any]]:
    if len(data) > MAX_UPLOAD_SIZE_MB * 1024 * 1024:
        raise ImportParseError("file_too_large", f"File exceeds {MAX_UPLOAD_SIZE_MB} MB")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ImportParseError("malformed_csv", "CSV must be UTF-8 or UTF-8 BOM") from exc
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    try:
        rows = list(csv.reader(io.StringIO(text), dialect))
    except csv.Error as exc:
        raise ImportParseError("malformed_csv", "Malformed CSV") from exc
    rows = [[str(cell or "").strip() for cell in row] for row in rows if any(str(cell or "").strip() for cell in row)]
    if len(rows) > max_rows + 1:
        raise ImportParseError("max_rows_exceeded", f"Import row limit exceeded: {max_rows}")
    phone_index, name_index, has_header, headers = _detect_columns(rows, phone_column, display_name_column)
    data_rows = rows[1:] if has_header else rows
    parsed = [
        ParsedRow(index + 1, row[phone_index].strip() if phone_index < len(row) else "", row[name_index].strip() if name_index is not None and name_index < len(row) else None)
        for index, row in enumerate(data_rows[:max_rows])
    ]
    return parsed, {"filename": safe_filename(filename), "columns": headers, "phone_column": headers[phone_index], "display_name_column": headers[name_index] if name_index is not None else None, "header_detected": has_header}


def _xlsx_sheet_paths(zf: zipfile.ZipFile) -> list[tuple[str, str]]:
    workbook = ElementTree.fromstring(zf.read("xl/workbook.xml"))
    rels = ElementTree.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    ns = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main", "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
    rel_map = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels}
    sheets: list[tuple[str, str]] = []
    for sheet in workbook.findall("main:sheets/main:sheet", ns):
        name = sheet.attrib.get("name", "Sheet")
        rel_id = sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
        target = rel_map.get(rel_id or "", "")
        path = "xl/" + target.lstrip("/")
        if not path.startswith("xl/worksheets/"):
            path = "xl/worksheets/" + Path(target).name
        sheets.append((name, path))
    return sheets


def _xlsx_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    root = ElementTree.fromstring(zf.read("xl/sharedStrings.xml"))
    ns = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    values: list[str] = []
    for si in root.findall("main:si", ns):
        texts = [node.text or "" for node in si.findall(".//main:t", ns)]
        values.append("".join(texts))
    return values


def _cell_text(cell: ElementTree.Element, shared: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    value = cell.find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}v")
    if value is None or value.text is None:
        inline = cell.find(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")
        return inline.text if inline is not None and inline.text is not None else ""
    if cell_type == "s":
        index = int(value.text)
        return shared[index] if 0 <= index < len(shared) else ""
    return value.text


def parse_xlsx_bytes(
    data: bytes,
    filename: str | None = None,
    sheet_name: str | None = None,
    phone_column: str | None = None,
    display_name_column: str | None = None,
    max_rows: int = MAX_IMPORT_ROWS,
) -> tuple[list[ParsedRow], dict[str, Any]]:
    if len(data) > MAX_UPLOAD_SIZE_MB * 1024 * 1024:
        raise ImportParseError("file_too_large", f"File exceeds {MAX_UPLOAD_SIZE_MB} MB")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ImportParseError("malformed_excel", "Malformed Excel workbook") from exc
    if any(name.lower().endswith((".bin", ".vba")) or "vbaproject" in name.lower() for name in zf.namelist()):
        raise ImportParseError("unsupported_file_type", "Macro-enabled workbooks are not supported")
    try:
        sheets = _xlsx_sheet_paths(zf)
        if not sheets:
            raise ImportParseError("malformed_excel", "Workbook has no sheets")
        selected = next((item for item in sheets if item[0] == sheet_name), sheets[0])
        shared = _xlsx_shared_strings(zf)
        root = ElementTree.fromstring(zf.read(selected[1]))
    except Exception as exc:
        if isinstance(exc, ImportParseError):
            raise
        raise ImportParseError("malformed_excel", "Malformed Excel workbook") from exc
    ns = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    rows: list[list[str]] = []
    for row in root.findall(".//main:sheetData/main:row", ns):
        values: list[str] = []
        for cell in row.findall("main:c", ns):
            ref = cell.attrib.get("r", "")
            column_letters = re.sub(r"\d", "", ref)
            column_index = 0
            for char in column_letters:
                column_index = column_index * 26 + (ord(char.upper()) - ord("A") + 1)
            column_index = max(1, column_index) - 1
            while len(values) <= column_index:
                values.append("")
            values[column_index] = _cell_text(cell, shared).strip()
        if any(values):
            rows.append(values)
        if len(rows) > max_rows + 1:
            raise ImportParseError("max_rows_exceeded", f"Import row limit exceeded: {max_rows}")
    phone_index, name_index, has_header, headers = _detect_columns(rows, phone_column, display_name_column)
    data_rows = rows[1:] if has_header else rows
    parsed = [
        ParsedRow(index + 1, row[phone_index].strip() if phone_index < len(row) else "", row[name_index].strip() if name_index is not None and name_index < len(row) else None)
        for index, row in enumerate(data_rows[:max_rows])
    ]
    return parsed, {"filename": safe_filename(filename), "sheets": [sheet[0] for sheet in sheets], "selected_sheet": selected[0], "columns": headers, "phone_column": headers[phone_index], "display_name_column": headers[name_index] if name_index is not None else None, "header_detected": has_header}


def build_preview_items(rows: list[ParsedRow], existing_by_phone: dict[str, str]) -> list[dict[str, Any]]:
    seen: dict[str, int] = {}
    items: list[dict[str, Any]] = []
    for row in rows:
        raw = str(row.phone_raw or "").strip()
        base = {"row_number": row.row_number, "phone_raw": raw, "display_name": row.display_name or None}
        try:
            normalized = normalize_bale_phone(raw)
        except BaleContactError as exc:
            items.append({**base, "phone_normalized": None, "validation_status": "invalid", "error_code": exc.error_code, "error_message": str(exc), "selected_for_import": False})
            continue
        if normalized in seen:
            items.append({**base, "phone_normalized": normalized, "validation_status": "duplicate", "duplicate_reason": "input", "error_code": "duplicate_in_input", "error_message": "Duplicate phone in uploaded input", "selected_for_import": False})
            continue
        if normalized in existing_by_phone:
            items.append({**base, "phone_normalized": normalized, "validation_status": "duplicate", "duplicate_reason": "campaign", "duplicate_recipient_id": existing_by_phone[normalized], "error_code": "duplicate_in_campaign", "error_message": "Duplicate phone already exists in campaign", "selected_for_import": False})
            seen[normalized] = row.row_number
            continue
        seen[normalized] = row.row_number
        items.append({**base, "phone_normalized": normalized, "validation_status": "valid", "selected_for_import": True})
    return items
