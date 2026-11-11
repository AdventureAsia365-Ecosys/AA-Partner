"""A small PDF writer for the CRM's server-side documents (AA-1070 item crm-21): A4 pages of wrapped text, headings,
key / value rows, simple tables and rules. No browser and no third-party library: the worker image installs no new
system package (the Trivy rule of the Dockerfile) and a headless browser is a large, CVE-heavy dependency.

Text is Unicode (Vietnamese included): one TrueType font (``fonts/LiberationSans-Regular.ttf``, SIL OFL 1.1, see
``fonts/LICENSE.txt``) is embedded whole as a CID font (``Type0`` / ``CIDFontType2``, ``Identity-H``, the glyph id is
the character code), with the widths of the glyphs used and a ``ToUnicode`` map so the text can be searched and copied.
Bold is the same font drawn with fill and stroke. A character the font lacks prints as its "missing" box.

Usage::

    doc = Document(title="Booking confirmation", author="Agency name")
    doc.heading("Booking confirmation")
    doc.text("Dear ...")
    doc.table(["Service", "Dates"], [["Day tour", "1 Mar"]], widths=[0.7, 0.3])
    pdf_bytes = doc.render()

Every page gets the footer ``footer`` (left) and "n / m" (right). Coordinates are PDF points (1/72 inch).
"""

from __future__ import annotations

import datetime as dt
import struct
import unicodedata
import zlib
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

FONT_PATH = Path(__file__).with_name("fonts") / "LiberationSans-Regular.ttf"
FONT_NAME = "LiberationSans"
PAGE_W, PAGE_H = 595.28, 841.89  # A4
MARGIN = 48.0
Rgb = tuple[float, float, float]
TEXT_COLOR = (0.11, 0.14, 0.17)
MUTED = (0.38, 0.42, 0.47)
RULE = (0.82, 0.84, 0.87)
BOLD_STROKE = 0.04  # outline width per point of size that makes the regular face read as bold


class FontError(ValueError):
    """The bundled font cannot be read (a broken install)."""


@dataclass(frozen=True)
class Font:
    """What the writer needs from a TrueType file: its bytes, metrics and the character -> glyph map."""

    data: bytes
    units_per_em: int
    bbox: tuple[int, int, int, int]
    ascent: int
    descent: int
    cap_height: int
    advances: tuple[int, ...]
    cmap: dict[int, int]

    def glyph(self, char: str) -> int:
        return self.cmap.get(ord(char), 0)

    def width(self, gid: int) -> int:
        """The advance of a glyph in 1/1000 em (the PDF text space unit)."""
        adv = self.advances[gid] if gid < len(self.advances) else self.advances[-1]
        return round(adv * 1000 / self.units_per_em)

    def text_width(self, text: str, size: float) -> float:
        return sum(self.width(self.glyph(c)) for c in text) * size / 1000


def _tables(data: bytes) -> dict[str, tuple[int, int]]:
    if len(data) < 12:
        raise FontError("font too short")
    count = struct.unpack(">H", data[4:6])[0]
    out = {}
    for i in range(count):
        tag, _check, offset, length = struct.unpack(">4sIII", data[12 + 16 * i : 28 + 16 * i])
        out[tag.decode("latin-1")] = (offset, length)
    return out


def _cmap(data: bytes, offset: int) -> dict[int, int]:
    """The Unicode subtable (format 12 preferred, else format 4) as code point -> glyph id."""
    count = struct.unpack(">H", data[offset + 2 : offset + 4])[0]
    best: tuple[int, int] | None = None
    for i in range(count):
        platform, encoding, sub = struct.unpack(">HHI", data[offset + 4 + 8 * i : offset + 12 + 8 * i])
        fmt = struct.unpack(">H", data[offset + sub : offset + sub + 2])[0]
        if (platform, encoding) in ((3, 10), (0, 4), (0, 6)) and fmt == 12:
            best = (fmt, offset + sub)
            break
        if (platform, encoding) in ((3, 1), (0, 3), (0, 1), (0, 0)) and fmt == 4 and best is None:
            best = (fmt, offset + sub)
    if best is None:
        raise FontError("no Unicode cmap")
    fmt, at = best
    out: dict[int, int] = {}
    if fmt == 12:
        groups = struct.unpack(">I", data[at + 12 : at + 16])[0]
        for g in range(groups):
            start, end, gid = struct.unpack(">III", data[at + 16 + 12 * g : at + 28 + 12 * g])
            for code in range(start, end + 1):
                out[code] = gid + code - start
        return out
    segs = struct.unpack(">H", data[at + 6 : at + 8])[0] // 2
    ends = struct.unpack(f">{segs}H", data[at + 14 : at + 14 + 2 * segs])
    base = at + 16 + 2 * segs
    starts = struct.unpack(f">{segs}H", data[base : base + 2 * segs])
    deltas = struct.unpack(f">{segs}h", data[base + 2 * segs : base + 4 * segs])
    range_at = base + 4 * segs
    offsets = struct.unpack(f">{segs}H", data[range_at : range_at + 2 * segs])
    for s in range(segs):
        for code in range(starts[s], ends[s] + 1):
            if code == 0xFFFF:
                continue
            if offsets[s] == 0:
                gid = (code + deltas[s]) & 0xFFFF
            else:
                ptr = range_at + 2 * s + offsets[s] + 2 * (code - starts[s])
                gid = struct.unpack(">H", data[ptr : ptr + 2])[0]
                if gid:
                    gid = (gid + deltas[s]) & 0xFFFF
            if gid:
                out[code] = gid
    return out


def read_font(data: bytes) -> Font:
    try:
        t = _tables(data)
        head, hhea, hmtx, maxp = t["head"][0], t["hhea"][0], t["hmtx"][0], t["maxp"][0]
        units = struct.unpack(">H", data[head + 18 : head + 20])[0]
        bbox = struct.unpack(">hhhh", data[head + 36 : head + 44])
        ascent, descent = struct.unpack(">hh", data[hhea + 4 : hhea + 8])
        metrics = struct.unpack(">H", data[hhea + 34 : hhea + 36])[0]
        glyphs = struct.unpack(">H", data[maxp + 4 : maxp + 6])[0]
        advances = [struct.unpack(">H", data[hmtx + 4 * i : hmtx + 4 * i + 2])[0] for i in range(metrics)]
        advances += [advances[-1]] * (glyphs - metrics)
        cap = ascent
        if "OS/2" in t:
            os2 = t["OS/2"][0]
            version = struct.unpack(">H", data[os2 : os2 + 2])[0]
            if version >= 2:
                cap = struct.unpack(">h", data[os2 + 88 : os2 + 90])[0]
        return Font(data, units, tuple(bbox), ascent, descent, cap, tuple(advances), _cmap(data, t["cmap"][0]))
    except (KeyError, struct.error, IndexError) as exc:
        raise FontError(f"unreadable font: {type(exc).__name__}") from None


@cache
def bundled_font() -> Font:
    return read_font(FONT_PATH.read_bytes())


def clean(text: str) -> str:
    """NFC (precomposed Vietnamese letters are in the font), tabs to spaces, other control characters dropped."""
    text = unicodedata.normalize("NFC", text.replace("\t", "    "))
    return "".join(c for c in text if c == "\n" or unicodedata.category(c)[0] != "C")


def wrap(font: Font, text: str, size: float, width: float) -> list[str]:
    """Lines of at most ``width`` points: words kept whole when they fit, a longer word cut by characters."""
    lines: list[str] = []
    for paragraph in clean(text).split("\n"):
        line = ""
        for word in paragraph.split(" "):
            candidate = f"{line} {word}" if line else word
            if font.text_width(candidate, size) <= width:
                line = candidate
                continue
            if line:
                lines.append(line)
            line = ""
            while font.text_width(word, size) > width:
                cut = len(word)
                while cut > 1 and font.text_width(word[:cut], size) > width:
                    cut -= 1
                lines.append(word[:cut])
                word = word[cut:]
            line = word
        lines.append(line)
    return lines


def _num(value: float) -> str:
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def _color(rgb: tuple[float, float, float]) -> str:
    return " ".join(_num(c) for c in rgb)


def _pdf_text(value: str) -> str:
    """A PDF literal string (document info): ASCII as is, anything else as UTF-16BE hex."""
    if value.isascii():
        return "(" + value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ")"
    return "<FEFF" + value.encode("utf-16-be").hex().upper() + ">"


@dataclass
class Document:
    title: str
    author: str | None = None
    footer: str | None = None
    font: Font = field(default_factory=bundled_font)
    pages: list[list[str]] = field(default_factory=list)
    y: float = 0.0
    used: set[int] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.new_page()

    @property
    def width(self) -> float:
        return PAGE_W - 2 * MARGIN

    def new_page(self) -> None:
        self.pages.append([])
        self.y = PAGE_H - MARGIN

    def _room(self, height: float) -> None:
        if self.y - height < MARGIN + 24:
            self.new_page()

    def _show(self, text: str, x: float, y: float, size: float, *, bold: bool = False,
              color: tuple[float, float, float] = TEXT_COLOR) -> None:  # fmt: skip
        gids = [self.font.glyph(c) for c in text]
        self.used.update(gids)
        hexed = "".join(f"{g:04X}" for g in gids)
        mode = f"2 Tr {_num(BOLD_STROKE * size)} w {_color(color)} RG " if bold else "0 Tr "
        self.pages[-1].append(f"BT {mode}{_color(color)} rg /F1 {_num(size)} Tf {_num(x)} {_num(y)} Td <{hexed}> Tj ET")

    def text(self, value: str, *, size: float = 10.5, bold: bool = False, color: Rgb = TEXT_COLOR,
             indent: float = 0.0, gap: float = 4.0) -> None:  # fmt: skip
        """A wrapped paragraph (newlines kept)."""
        lead = size * 1.35
        for line in wrap(self.font, value, size, self.width - indent):
            self._room(lead)
            self.y -= lead
            self._show(line, MARGIN + indent, self.y + (lead - size) / 2, size, bold=bold, color=color)
        self.y -= gap

    def heading(self, value: str, *, size: float = 16.0) -> None:
        self._room(size * 2.2)
        self.y -= 4
        self.text(value, size=size, bold=True, gap=6)

    def small(self, value: str) -> None:
        self.text(value, size=8.5, color=MUTED, gap=2)

    def rule(self, *, gap: float = 8.0) -> None:
        self._room(gap * 2)
        self.y -= gap
        self.pages[-1].append(
            f"{_color(RULE)} RG 0.8 w {_num(MARGIN)} {_num(self.y)} m {_num(PAGE_W - MARGIN)} {_num(self.y)} l S"
        )
        self.y -= gap

    def space(self, height: float = 8.0) -> None:
        self.y -= height

    def pairs(self, rows: list[tuple[str, str | None]], *, size: float = 10.0, label_share: float = 0.32) -> None:
        """Label / value rows; a row without a value is left out."""
        label_w = self.width * label_share
        for label, value in rows:
            if value in (None, ""):
                continue
            lines = wrap(self.font, str(value), size, self.width - label_w - 8)
            lead = size * 1.4
            self._room(lead * len(lines))
            for i, line in enumerate(lines):
                self.y -= lead
                base = self.y + (lead - size) / 2
                if i == 0:
                    self._show(label, MARGIN, base, size, color=MUTED)
                self._show(line, MARGIN + label_w, base, size)
        self.y -= 4

    def table(self, headers: list[str], rows: list[list[str]], *, widths: list[float] | None = None,
              size: float = 9.5, right: tuple[int, ...] = (), show_header: bool = True) -> None:  # fmt: skip
        """A table: ``widths`` are shares of the text width (equal by default); ``right`` = right-aligned columns.
        The header row repeats on each new page (``show_header=False``: no header row, e.g. totals)."""
        shares = widths or [1 / len(headers)] * len(headers)
        cols = [self.width * s for s in shares]
        lead = size * 1.35

        def row(cells: list[str], *, bold: bool, color: tuple[float, float, float]) -> None:
            wrapped = [wrap(self.font, c or "", size, w - 8) for c, w in zip(cells, cols, strict=True)]
            height = lead * max(len(w) for w in wrapped) + 6
            if self.y - height < MARGIN + 24:
                self.new_page()
                if not bold and show_header:
                    row(headers, bold=True, color=MUTED)
            top = self.y
            x = MARGIN
            for i, (lines, w) in enumerate(zip(wrapped, cols, strict=True)):
                for j, line in enumerate(lines):
                    base = top - lead * (j + 1) + (lead - size) / 2
                    lx = x + w - 4 - self.font.text_width(line, size) if i in right else x + 2
                    self._show(line, lx, base, size, bold=bold, color=color)
                x += w
            self.y = top - height
            self.pages[-1].append(
                f"{_color(RULE)} RG 0.5 w {_num(MARGIN)} {_num(self.y + 2)} m {_num(PAGE_W - MARGIN)} "
                f"{_num(self.y + 2)} l S"
            )

        if show_header:
            row(headers, bold=True, color=MUTED)
        for cells in rows:
            row(cells, bold=False, color=TEXT_COLOR)
        self.y -= 6

    # ── output ───────────────────────────────────────────────────────────────────────────────────
    def _footers(self) -> None:
        total = len(self.pages)
        for n, page in enumerate(self.pages, start=1):
            label = f"{n} / {total}"
            size = 8.0
            x = PAGE_W - MARGIN - self.font.text_width(label, size)
            current = page
            gids = [self.font.glyph(c) for c in label]
            self.used.update(gids)
            current.append(
                f"BT 0 Tr {_color(MUTED)} rg /F1 8 Tf {_num(x)} {_num(MARGIN - 18)} Td "
                f"<{''.join(f'{g:04X}' for g in gids)}> Tj ET"
            )
            if self.footer:
                text = wrap(self.font, self.footer, size, self.width * 0.75)[0]
                gids = [self.font.glyph(c) for c in text]
                self.used.update(gids)
                current.append(
                    f"BT 0 Tr {_color(MUTED)} rg /F1 8 Tf {_num(MARGIN)} {_num(MARGIN - 18)} Td "
                    f"<{''.join(f'{g:04X}' for g in gids)}> Tj ET"
                )

    def _to_unicode(self) -> bytes:
        reverse: dict[int, int] = {}
        for code, gid in sorted(self.font.cmap.items(), reverse=True):
            if gid in self.used:
                reverse[gid] = code  # the lowest code point wins for a shared glyph
        pairs = sorted(reverse.items())
        chunks = []
        for i in range(0, len(pairs), 100):
            block = pairs[i : i + 100]
            body = "\n".join(f"<{g:04X}> <{chr(c).encode('utf-16-be').hex().upper()}>" for g, c in block)
            chunks.append(f"{len(block)} beginbfchar\n{body}\nendbfchar")
        return (
            "/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
            "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
            "/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n"
            "1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n" + "\n".join(chunks) +
            "\nendcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n"
        ).encode("ascii")  # fmt: skip

    def _widths(self) -> str:
        return " ".join(f"{g} [{self.font.width(g)}]" for g in sorted(self.used))

    def render(self, *, created: dt.datetime | None = None) -> bytes:
        """The PDF file."""
        self._footers()
        font = self.font
        scale = 1000 / font.units_per_em
        objects: list[bytes] = []

        def add(body: bytes) -> int:
            objects.append(body)
            return len(objects)

        def stream(data: bytes, extra: str = "") -> bytes:
            packed = zlib.compress(data, 9)
            head = f"<< /Length {len(packed)} /Filter /FlateDecode{extra} >>\nstream\n".encode("ascii")
            return head + packed + b"\nendstream"

        font_file = add(stream(font.data, f" /Length1 {len(font.data)}"))
        bbox = " ".join(str(round(v * scale)) for v in font.bbox)
        descriptor = add(
            (f"<< /Type /FontDescriptor /FontName /{FONT_NAME} /Flags 32 /FontBBox [{bbox}] /ItalicAngle 0 "
             f"/Ascent {round(font.ascent * scale)} /Descent {round(font.descent * scale)} "
             f"/CapHeight {round(font.cap_height * scale)} /StemV 80 /FontFile2 {font_file} 0 R >>").encode("ascii")
        )  # fmt: skip
        cid = add(
            (f"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /{FONT_NAME} "
             "/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
             f"/FontDescriptor {descriptor} 0 R /DW 1000 /W [{self._widths()}] "
             "/CIDToGIDMap /Identity >>").encode("ascii")
        )  # fmt: skip
        to_unicode = add(stream(self._to_unicode()))
        type0 = add(
            (f"<< /Type /Font /Subtype /Type0 /BaseFont /{FONT_NAME} /Encoding /Identity-H "
             f"/DescendantFonts [{cid} 0 R] /ToUnicode {to_unicode} 0 R >>").encode("ascii")
        )  # fmt: skip
        pages_id = len(objects) + 1 + 2 * len(self.pages)  # after every page and its content
        kids = []
        for ops in self.pages:
            content = add(stream("\n".join(ops).encode("ascii")))
            kids.append(
                add(
                    (f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 {_num(PAGE_W)} {_num(PAGE_H)}] "
                     f"/Resources << /Font << /F1 {type0} 0 R >> >> /Contents {content} 0 R >>").encode("ascii")
                )
            )  # fmt: skip
        add(f"<< /Type /Pages /Count {len(kids)} /Kids [{' '.join(f'{k} 0 R' for k in kids)}] >>".encode("ascii"))
        catalog = add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode("ascii"))
        when = (created or dt.datetime.now(dt.UTC)).strftime("D:%Y%m%d%H%M%SZ")
        info_parts = [f"/Title {_pdf_text(self.title)}", "/Producer (AA Partner CRM)", f"/CreationDate ({when})"]
        if self.author:
            info_parts.append(f"/Author {_pdf_text(self.author)}")
        info = add(f"<< {' '.join(info_parts)} >>".encode("ascii"))
        out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for i, body in enumerate(objects, start=1):
            offsets.append(len(out))
            out += f"{i} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii")
        out += b"".join(f"{o:010d} 00000 n \n".encode("ascii") for o in offsets)
        out += (f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R /Info {info} 0 R >>\n"
                f"startxref\n{xref}\n%%EOF\n").encode("ascii")  # fmt: skip
        return bytes(out)
