#!/usr/bin/env python3
"""Generate app/platform_settings/compiled_defaults.json from the settings seed of the crm migrations (AA-1096 L4).

The seed of crm.setting_definition lives in the migrations, between the lines ``-- settings-seed:begin`` and
``-- settings-seed:end``, as one JSON array inside ``$seed$ … $seed$`` (crm-064 is the first; the AA-CIS-App CIS 407
format). Migrations are read in number order and a later row of a key replaces an earlier one. The reader
(app.platform_settings) serves these defaults when the database cannot be read, so they must equal the seed:
``--check`` exits 1 when the committed file differs (tests/unit/test_aa1096_settings_seed_parity.py runs it in CI).

AA-1096 l5 (crm-070): a later file changes some fields of existing keys between ``-- settings-edit:begin`` and
``-- settings-edit:end``, one JSON array of ``{"key", <field>: <value>, ...}`` inside ``$edit$ … $edit$`` (an UPDATE of
the existing rows; crm-070 retires the one-release env overrides). Applied in file order after the seed rows of the
same file; a field outside the definition's fields (or ``key`` / ``app``), or a key no earlier file seeds, fails.

    python3 scripts/gen_settings_defaults.py           # rewrite the file (from api/)
    python3 scripts/gen_settings_defaults.py --check   # CI parity check
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

API = Path(__file__).resolve().parents[1]
MIGRATIONS = API / "migrations"
OUT = API / "app" / "platform_settings" / "compiled_defaults.json"
BLOCK_RE = re.compile(r"--\s*settings-seed:begin.*?\$seed\$(.*?)\$seed\$.*?--\s*settings-seed:end", re.S)
EDIT_RE = re.compile(r"--\s*settings-edit:begin.*?\$edit\$(.*?)\$edit\$.*?--\s*settings-edit:end", re.S)
FILE_RE = re.compile(r"^(\d{3})_.*\.sql$")
FIELDS = (
    "key",
    "app",
    "area",
    "label",
    "description",
    "value_type",
    "unit",
    "min_value",
    "max_value",
    "allowed_values",
    "default_value",
    "scope_kind",
    "impact",
    "approval",
    "evidence_required",
    "env_override",
    "cache_s",
    "enforced",
)


def seed_rows(migrations: Path = MIGRATIONS) -> tuple[list[dict], list[str]]:
    rows: dict[str, dict] = {}
    files = sorted((p for p in migrations.glob("*.sql") if FILE_RE.match(p.name)), key=lambda p: p.name)
    sources = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        blocks, edits = BLOCK_RE.findall(text), EDIT_RE.findall(text)
        if not blocks and not edits:
            continue
        sources.append(path.name)
        for block in blocks:
            for row in json.loads(block):
                missing = [f for f in FIELDS if f not in row]
                if missing:
                    raise SystemExit(f"{path.name}: seed row {row.get('key')} misses {missing}")
                rows[row["key"]] = {f: row[f] for f in FIELDS}
        for block in edits:
            for row in json.loads(block):
                _edit(rows, row, path.name)
    return [rows[k] for k in sorted(rows)], sources


def _edit(rows: dict[str, dict], row: dict, source: str) -> None:
    key = row.get("key")
    if key not in rows:
        raise SystemExit(f"{source}: edit row {key} names no seeded key")
    fields = {f: v for f, v in row.items() if f != "key"}
    bad = sorted(f for f in fields if f not in FIELDS or f == "app")
    if not fields or bad:
        raise SystemExit(f"{source}: edit row {key} changes {bad or 'nothing'} (definition fields only, not key / app)")
    rows[key] = {**rows[key], **fields}


def render(migrations: Path = MIGRATIONS) -> str:
    rows, sources = seed_rows(migrations)
    doc = {
        "_generated": "scripts/gen_settings_defaults.py from the settings seed of "
        + ", ".join(sources)
        + "; do not edit by hand",
        "definitions": rows,
    }
    return json.dumps(doc, indent=1, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--check", action="store_true", help="exit 1 when the committed file differs from the seed")
    args = ap.parse_args(argv)
    text = render()
    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print(
                f"{OUT.relative_to(API)} differs from the migration seed: run "
                "python3 scripts/gen_settings_defaults.py and commit the file",
                file=sys.stderr,
            )
            return 1
        print("compiled settings defaults match the migration seed")
        return 0
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT.relative_to(API)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
