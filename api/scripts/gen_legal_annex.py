#!/usr/bin/env python3
"""Copy the public summary of the root DPA Annex 3 into the agency app (AA-970, design v3 card CRM-37b).

The agency app's public pages "How we handle your data" (``/legal/data``) and "Sub-processors"
(``/legal/subprocessors``) are rendered from AA-Ecosys ``docs/legal/dpa-annex-3-subprocessors.md``, section 5: the
block between ``<!-- public-summary:begin -->`` and ``<!-- public-summary:end -->``. This script writes that block, with
the annex version (the ``(draft vX.Y)`` of its title) and its ``last_reviewed`` date, to
``web/lib/legal/annex3.generated.json``; the web parses the Markdown block (``web/lib/legal/annexMarkdown.ts``). The
annex is the one source: never edit the JSON by hand.

    python3 scripts/gen_legal_annex.py ../../../docs/legal/dpa-annex-3-subprocessors.md   # rewrite (from api/)
    python3 scripts/gen_legal_annex.py --check <annex>                                     # exit 1 on drift

The CI job "Vendored CIS copies" (``scripts/check_vendored_copies.py``) reads the annex on AA-Ecosys ``master`` and
fails when the JSON differs from what this script would write. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

API = Path(__file__).resolve().parents[1]
OUT = API.parent / "web" / "lib" / "legal" / "annex3.generated.json"
SOURCE = {
    "repo": "AdventureAsia365-Ecosys/AA-Ecosys",
    "branch": "master",
    "path": "docs/legal/dpa-annex-3-subprocessors.md",
}
BEGIN = "<!-- public-summary:begin -->"
END = "<!-- public-summary:end -->"
TITLE_RE = re.compile(r'^title:\s*"[^"]*\((draft v\d+\.\d+)\)"\s*$', re.M)
REVIEWED_RE = re.compile(r"^last_reviewed:\s*(\d{4}-\d{2}-\d{2})\s*$", re.M)
# The public block must never carry an AWS account number, an ARN or an internal ticket id (it is shown to anyone).
FORBIDDEN = (
    (re.compile(r"\b\d{12}\b"), "a 12-digit number (an AWS account id?)"),
    (re.compile(r"\barn:aws"), "an ARN"),
    (re.compile(r"\bAA-\d+\b"), "an internal issue id"),
    (re.compile(r"\bacc[1-4]\b", re.I), "an internal account label"),
)


class AnnexError(ValueError):
    """The annex has no usable public summary (missing markers, version, date, or a forbidden token)."""


def extract(text: str) -> dict[str, Any]:
    """The JSON object the web reads, from the annex text. Raises :class:`AnnexError`."""
    if text.count(BEGIN) != 1 or text.count(END) != 1:
        raise AnnexError("the annex needs exactly one public-summary:begin and one public-summary:end marker")
    start, stop = text.index(BEGIN) + len(BEGIN), text.index(END)
    if stop <= start:
        raise AnnexError("public-summary:end comes before public-summary:begin")
    block = text[start:stop].strip("\n")
    if not block.strip():
        raise AnnexError("the public summary is empty")
    for pattern, what in FORBIDDEN:
        if pattern.search(block):
            raise AnnexError(f"the public summary contains {what}")
    title = TITLE_RE.search(text)
    reviewed = REVIEWED_RE.search(text)
    if not title:
        raise AnnexError('the front-matter title has no "(draft vX.Y)" version')
    if not reviewed:
        raise AnnexError("the front matter has no last_reviewed date")
    return {"source": dict(SOURCE), "version": title.group(1), "updated": reviewed.group(1), "markdown": block + "\n"}


def render(data: dict[str, Any]) -> str:
    """The exact file text (stable: the drift check compares strings)."""
    return json.dumps(data, ensure_ascii=False, indent=1) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("annex", type=Path, help="path to AA-Ecosys docs/legal/dpa-annex-3-subprocessors.md")
    parser.add_argument("--check", action="store_true", help="exit 1 when the committed JSON differs")
    args = parser.parse_args(argv)
    try:
        want = render(extract(args.annex.read_text(encoding="utf-8")))
    except AnnexError as exc:
        print(f"FAIL  {args.annex}: {exc}", file=sys.stderr)
        return 1
    if args.check:
        have = OUT.read_text(encoding="utf-8") if OUT.is_file() else ""
        if have != want:
            print(f"FAIL  {OUT.relative_to(API.parent)} differs from the annex: re-run this script", file=sys.stderr)
            return 1
        print(f"ok    {OUT.relative_to(API.parent)}")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(want, encoding="utf-8")
    print(f"wrote {OUT.relative_to(API.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
