#!/usr/bin/env python3
"""AA-1036 (R3 slice 1, root ADR 0019 d6) — the base of the web's ESLint suppressions growth check while the Content
studio moves from ``portal/`` into ``web/``.

Web Build's "Suppressions do not grow" step compares ``web/eslint-suppressions.json`` with the base branch's file
(``check-kit-copy.mjs --suppressions``, root ADR 0016 decision 7): a new entry may only carry what a deleted or renamed
file of ``web/`` carried. The studio's files arrive in ``web/`` from another folder, so their frozen violations would
read as growth. This script adds the base branch's ``portal/`` entries to the base file under the studio's new paths in
``web/`` (:data:`MOVES`), so a moved file may keep its count and never grow it; everything else of the check stays.

Only files that are NOT already in the base branch's ``web/`` tree get a portal entry: once the move is merged, the
base ``web/`` file holds the moved entries itself and this script adds nothing (and after ``portal/`` is deleted, in
R3 slice 2, there is no portal file to read). Stdlib only.

Usage: studio_suppressions_base.py <base web suppressions> <base portal suppressions | -> <base web file list> <out>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# portal/<prefix> -> web/<prefix>, first match wins (paths relative to each app's folder, as ESLint writes them).
MOVES: tuple[tuple[str, str], ...] = (
    ("app/portal/", "app/(studio)/portal/"),
    ("app/portal-access/", "app/(studio)/portal-access/"),
    ("app/_legal/", "app/(studio)/_legal/"),
    ("app/terms/page.tsx", "app/(studio)/legal/partner-terms/page.tsx"),
    ("app/privacy/page.tsx", "app/(studio)/legal/privacy/page.tsx"),
    ("app/_kit/local/", "app/_kit/local/studio/"),
    ("app/api/", "app/api/"),
)


def moved_path(portal_path: str) -> str | None:
    """The web path a portal file moved to, or None for a file that was not moved (e.g. the API-key sign-in page)."""
    for old, new in MOVES:
        if old.endswith("/") and portal_path.startswith(old):
            return new + portal_path[len(old) :]
        if portal_path == old:
            return new
    return None


def merged_base(
    web: dict[str, dict[str, dict[str, int]]],
    portal: dict[str, dict[str, dict[str, int]]],
    web_files: set[str],
) -> dict[str, dict[str, dict[str, int]]]:
    """The base web suppressions plus the portal entries of moved files the base web tree does not have."""
    out = {f: {r: dict(v) for r, v in rules.items()} for f, rules in web.items()}
    for path, rules in portal.items():
        target = moved_path(path)
        if target is None or target in web_files or target in out:
            continue
        out[target] = {r: dict(v) for r, v in rules.items()}
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 5:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    web = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    portal = {} if argv[2] == "-" or not Path(argv[2]).exists() else json.loads(Path(argv[2]).read_text("utf-8"))
    files = {line.strip() for line in Path(argv[3]).read_text(encoding="utf-8").splitlines() if line.strip()}
    out = merged_base(web, portal, files)
    Path(argv[4]).write_text(json.dumps(out, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    added = len(out) - len(web)
    print(f"studio_suppressions_base: {added} moved studio file(s) added to the base ({len(out)} in all)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
