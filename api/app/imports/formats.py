"""Reading an import file (AA-962, card CRM-31 "phased CSV import"): a CSV (UTF-8, comma, semicolon or tab) or the
first sheet of an Excel ``.xlsx`` workbook, at most :data:`MAX_BYTES` and :data:`MAX_ROWS` data rows.

The result is a :class:`Table`: the normalised header names (lower case, spaces and dashes to ``_``, aliases resolved
by the entity spec, :mod:`app.imports.spec`) and the data rows as ``{column: text}`` with their row number in the file
(the header is row 1). Rows whose cells are all empty are left out. Nothing here touches the database.

The ``.xlsx`` reader is a small standard-library one (``zipfile`` + ``xml.etree``), so the API needs no spreadsheet
library: shared strings, inline strings, numbers and booleans of the first worksheet; formulas give their cached
value; styles are not read, so a date cell arrives as its Excel serial number (the date parser of the spec accepts
it). The sheet and the shared strings are **streamed** (``iterparse``; a cell is dropped once read, a row once kept,
the reading stops after the row cap), every zip member is read through a byte budget (:data:`MAX_MEMBER_BYTES`
inflated bytes, whatever its header claims) and the shared strings are capped in count and size, so a small,
highly compressible file is refused with ``unreadable_file`` instead of inflating in memory. ``xml.etree`` resolves
no external entity. The parse is CPU work: callers run :func:`read_table` in a thread (``asyncio.to_thread``).
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from typing import IO
from xml.etree import ElementTree  # noqa: S405 — standard-library parser, no external entities; sizes capped below

from app.errors import Invalid, PayloadTooLarge

MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 5000
MAX_COLUMNS = 40
MAX_CELL_CHARS = 10_000
# The largest inflated zip member read (sheet XML, shared strings). 5000 rows x 40 short cells of sheet XML stay far
# below it; the member is streamed (iterparse) and its bytes counted, so memory stays near the rows kept, not the XML.
MAX_MEMBER_BYTES = 8 * 1024 * 1024
# Shared strings kept: one per cell of the largest file at most, and a total size bound.
MAX_SHARED_STRINGS = MAX_ROWS * MAX_COLUMNS
MAX_SHARED_CHARS = 4_000_000
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
class _Capped:
    """A zip member read through a byte budget: past :data:`MAX_MEMBER_BYTES` inflated bytes it raises (the size in
    the zip header can lie), so a highly compressible member costs at most the budget, never its claimed size."""

    def __init__(self, handle: IO[bytes]) -> None:
        self._handle = handle
        self._left = MAX_MEMBER_BYTES

    def read(self, size: int = -1) -> bytes:
        want = self._left + 1 if size < 0 else min(size, self._left + 1)
        chunk = self._handle.read(want)
        self._left -= len(chunk)
        if self._left < 0:
            raise ValueError("member too large")
        return chunk


def _events(zf: zipfile.ZipFile, name: str) -> Iterator[tuple[str, ElementTree.Element]]:
    """Start / end events of one member, streamed (``iterparse``): no whole tree is built."""
    if zf.getinfo(name).file_size > MAX_MEMBER_BYTES:
        raise ValueError("member too large")
    with zf.open(name) as handle:
        # no external entities in xml.etree; the bytes are capped by _Capped
        yield from ElementTree.iterparse(_Capped(handle), events=("start", "end"))  # noqa: S314


def _xlsx_rows(data: bytes) -> list[tuple[int, list[str]]]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
            sheet = _first_sheet(zf, names)
            shared = _shared_strings(zf) if "xl/sharedStrings.xml" in names else []
            return _sheet_rows(zf, sheet, shared)
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError, ValueError, EOFError, OSError):
        raise Invalid("unreadable_file", "not a CSV or .xlsx file") from None


def _sheet_rows(zf: zipfile.ZipFile, sheet: str, shared: list[str]) -> list[tuple[int, list[str]]]:
    """The rows of the sheet, one ``<row>`` at a time: each cell is read when it closes and then dropped, each row
    is dropped once read, and the reading stops after :data:`MAX_ROWS` + 50 rows (``read_table`` names the limit)."""
    out: list[tuple[int, list[str]]] = []
    stack: list[ElementTree.Element] = []
    cells: dict[int, str] = {}
    in_cell = 0
    position = 0
    row_number = 0
    cell_index = 0
    for event, elem in _events(zf, sheet):
        if event == "start":
            stack.append(elem)
            if elem.tag == f"{_NS}row":
                position += 1
                row_number = int(elem.get("r") or position)
                cells, cell_index = {}, 0
            elif elem.tag == f"{_NS}c":
                in_cell += 1
            continue
        stack.pop()
        parent = stack[-1] if stack else None
        if elem.tag == f"{_NS}c":
            in_cell -= 1
            col = _column(elem.get("r"), cell_index)
            cell_index += 1
            if col < MAX_COLUMNS + 10:
                cells[col] = _cell_text(elem, shared)
        elif elem.tag == f"{_NS}row":
            width = max(cells) + 1 if cells else 0
            out.append((row_number, [cells.get(c, "") for c in range(width)]))
            if len(out) > MAX_ROWS + 50:
                break
        elif elem.tag == f"{_NS}sheetData":
            break
        elif in_cell:
            continue  # a value / inline string of the open cell: read when the cell closes
        elem.clear()
        if parent is not None:
            parent.remove(elem)  # always the parent's only child here, so this is O(1)
    return out


def _first_sheet(zf: zipfile.ZipFile, names: set[str]) -> str:
    if "xl/worksheets/sheet1.xml" in names:
        return "xl/worksheets/sheet1.xml"
    sheets = sorted(n for n in names if n.startswith("xl/worksheets/") and n.endswith(".xml"))
    if not sheets:
        raise KeyError("no worksheet")
    return sheets[0]


def _shared_strings(zf: zipfile.ZipFile) -> list[str]:
    """The shared strings, streamed; at most :data:`MAX_SHARED_STRINGS` strings and :data:`MAX_SHARED_CHARS`
    characters in all (each string cut at :data:`MAX_CELL_CHARS`), else the file is unreadable."""
    out: list[str] = []
    total = 0
    stack: list[ElementTree.Element] = []
    in_item = 0
    for event, elem in _events(zf, "xl/sharedStrings.xml"):
        if event == "start":
            stack.append(elem)
            if elem.tag == f"{_NS}si":
                in_item += 1
            continue
        stack.pop()
        parent = stack[-1] if stack else None
        if elem.tag == f"{_NS}si":
            in_item -= 1
            text = "".join(t.text or "" for t in elem.iter(f"{_NS}t"))[:MAX_CELL_CHARS]
            total += len(text)
            out.append(text)
            if len(out) > MAX_SHARED_STRINGS or total > MAX_SHARED_CHARS:
                raise ValueError("too many shared strings")
        elif in_item:
            continue
        elem.clear()
        if parent is not None:
            parent.remove(elem)
    return out


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
