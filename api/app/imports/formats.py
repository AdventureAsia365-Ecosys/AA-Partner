"""Reading an import file (AA-962, card CRM-31 "phased CSV import"): a CSV (UTF-8, comma, semicolon or tab) or the
first sheet of an Excel ``.xlsx`` workbook, at most :data:`MAX_BYTES` and :data:`MAX_ROWS` data rows.

The result is a :class:`Table`: the normalised header names (lower case, spaces and dashes to ``_``, aliases resolved
by the entity spec, :mod:`app.imports.spec`) and the data rows as ``{column: text}`` with their row number in the file
(the header is row 1). Rows whose cells are all empty are left out. Nothing here touches the database.

The ``.xlsx`` reader is a small standard-library one (``zipfile`` + ``xml.etree``), so the API needs no spreadsheet
library: shared strings, inline strings, numbers and booleans of the first worksheet; formulas give their cached
value; styles are not read, so a date cell arrives as its Excel serial number (the date parser of the spec accepts
it). Every zip member is size-checked before it is read (a zip bomb is refused with ``unreadable_file``), and
``xml.etree`` resolves no external entity.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass
from xml.etree import ElementTree  # noqa: S405 — standard-library parser, no external entities; sizes capped below

from app.errors import Invalid, PayloadTooLarge

MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 5000
MAX_COLUMNS = 40
MAX_CELL_CHARS = 10_000
# The largest uncompressed zip member read (sheet XML, shared strings): a 5 MB xlsx rarely inflates beyond 40 MB.
MAX_MEMBER_BYTES = 40 * 1024 * 1024
XLSX_MAGIC = b"PK\x03\x04"
_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_CELL_REF = re.compile(r"^([A-Z]{1,3})\d+$")
_HEADER_SPLIT = re.compile(r"[\s\-./]+")


@dataclass(frozen=True)
class Row:
    number: int  # the line / sheet row in the file (header = 1)
    cells: dict[str, str]


@dataclass(frozen=True)
class Table:
    format: str  # csv | xlsx
    headers: tuple[str, ...]
    rows: tuple[Row, ...]


def normalise_header(name: str) -> str:
    text = _HEADER_SPLIT.sub("_", name.strip().lstrip("﻿").lower())
    return re.sub(r"[^a-z0-9_]", "", text).strip("_")


def read_table(data: bytes) -> Table:
    """The table of an upload, or 422 ``empty_upload`` / ``unreadable_file`` / ``file_encoding`` /
    ``no_header`` / ``duplicate_column`` / ``too_many_columns`` / ``too_many_rows`` (413 above :data:`MAX_BYTES`)."""
    if not data:
        raise Invalid("empty_upload")
    if len(data) > MAX_BYTES:
        raise PayloadTooLarge("import_too_large", f"at most {MAX_BYTES // (1024 * 1024)} MB")
    if data[:4] == XLSX_MAGIC:
        fmt, grid = "xlsx", _xlsx_rows(data)
    else:
        fmt, grid = "csv", _csv_rows(data)
    header_index = next((i for i, (_, cells) in enumerate(grid) if any(c.strip() for c in cells)), None)
    if header_index is None:
        raise Invalid("no_header", "the first row must name the columns")
    _, raw_headers = grid[header_index]
    headers = [normalise_header(h) for h in raw_headers]
    while headers and not headers[-1]:
        headers.pop()
    if not headers or not any(headers):
        raise Invalid("no_header", "the first row must name the columns")
    if len(headers) > MAX_COLUMNS:
        raise Invalid("too_many_columns", f"at most {MAX_COLUMNS} columns")
    named = [h for h in headers if h]
    if len(set(named)) != len(named):
        raise Invalid("duplicate_column", "a column name appears twice")
    rows: list[Row] = []
    for number, cells in grid[header_index + 1 :]:
        values = {h: (cells[i].strip() if i < len(cells) else "") for i, h in enumerate(headers) if h}
        if not any(values.values()):
            continue
        if len(rows) >= MAX_ROWS:
            raise Invalid("too_many_rows", f"at most {MAX_ROWS} rows per file; split it")
        rows.append(Row(number, {k: v[:MAX_CELL_CHARS] for k, v in values.items()}))
    return Table(fmt, tuple(named), tuple(rows))


# ── CSV ────────────────────────────────────────────────────────────────────────────────────────────
def _csv_rows(data: bytes) -> list[tuple[int, list[str]]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise Invalid("file_encoding", "save the file as CSV UTF-8") from None
    if "\x00" in text:
        raise Invalid("unreadable_file", "not a CSV or .xlsx file")
    first = text.split("\n", 1)[0]
    delimiter = max((",", ";", "\t"), key=first.count)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    out: list[tuple[int, list[str]]] = []
    try:
        for cells in reader:
            out.append((reader.line_num, cells))
            if len(out) > MAX_ROWS + 50:  # header and blank lines; read_table names the exact limit
                break
    except csv.Error:
        raise Invalid("unreadable_file", "the CSV has a broken quote") from None
    # line_num counts physical lines; the number shown is the record's position (header = 1).
    return [(i + 1, cells) for i, (_, cells) in enumerate(out)]


# ── XLSX (first worksheet) ────────────────────────────────────────────────────────────────────────
def _xlsx_rows(data: bytes) -> list[tuple[int, list[str]]]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
            sheet = _first_sheet(zf, names)
            shared = _shared_strings(zf) if "xl/sharedStrings.xml" in names else []
            root = _parse(zf, sheet)
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError, ValueError, EOFError):
        raise Invalid("unreadable_file", "not a CSV or .xlsx file") from None
    out: list[tuple[int, list[str]]] = []
    data_el = root.find(f"{_NS}sheetData")
    if data_el is None:
        return out
    for position, row in enumerate(data_el.iter(f"{_NS}row"), start=1):
        number = int(row.get("r") or position)
        cells: dict[int, str] = {}
        for i, cell in enumerate(row.iter(f"{_NS}c")):
            col = _column(cell.get("r"), i)
            if col >= MAX_COLUMNS + 10:
                continue
            cells[col] = _cell_text(cell, shared)
        width = max(cells) + 1 if cells else 0
        out.append((number, [cells.get(c, "") for c in range(width)]))
        if len(out) > MAX_ROWS + 50:
            break
    return out


def _first_sheet(zf: zipfile.ZipFile, names: set[str]) -> str:
    if "xl/worksheets/sheet1.xml" in names:
        return "xl/worksheets/sheet1.xml"
    sheets = sorted(n for n in names if n.startswith("xl/worksheets/") and n.endswith(".xml"))
    if not sheets:
        raise KeyError("no worksheet")
    return sheets[0]


def _parse(zf: zipfile.ZipFile, name: str) -> ElementTree.Element:
    if zf.getinfo(name).file_size > MAX_MEMBER_BYTES:
        raise ValueError("member too large")
    with zf.open(name) as handle:
        raw = handle.read(MAX_MEMBER_BYTES + 1)
    if len(raw) > MAX_MEMBER_BYTES:
        raise ValueError("member too large")
    return ElementTree.fromstring(raw)  # noqa: S314 — no external entities in xml.etree; size capped above


def _shared_strings(zf: zipfile.ZipFile) -> list[str]:
    root = _parse(zf, "xl/sharedStrings.xml")
    return ["".join(t.text or "" for t in si.iter(f"{_NS}t")) for si in root.iter(f"{_NS}si")]


def _column(ref: str | None, fallback: int) -> int:
    match = _CELL_REF.match(ref or "")
    if not match:
        return fallback
    col = 0
    for ch in match.group(1):
        col = col * 26 + (ord(ch) - 64)
    return col - 1


def _cell_text(cell: ElementTree.Element, shared: list[str]) -> str:
    kind = cell.get("t")
    if kind == "inlineStr":
        return "".join(t.text or "" for t in cell.iter(f"{_NS}t"))
    value = cell.find(f"{_NS}v")
    text = value.text if value is not None and value.text is not None else ""
    if kind == "s":
        try:
            return shared[int(text)]
        except (ValueError, IndexError):
            return ""
    if kind == "b":
        return "true" if text == "1" else "false"
    if kind in (None, "n") and text.endswith(".0") and text[:-2].lstrip("-").isdigit():
        return text[:-2]  # 42.0 -> 42 (a whole number Excel stored as a float)
    return text
