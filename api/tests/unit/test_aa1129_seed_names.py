"""AA-1129 D1 (tech lead decision A7, 19/11/2026): new seed and fixture rows never carry an issue ID in a name.

The S283 UI audit found Dev agencies and records named after Linear issues ("AA-1086 rehearsal …"): an agency, a
client or a trip shows its name on every page, so an internal issue ID in it is an ID in the UI. The data already on
Dev stays as it is (decision A7); this guard keeps new rows clean:

- a migration ``INSERT`` (seed / config rows) holds no issue ID in a value; the migration registry row
  (``shared.schema_versions``, its description names the issue on purpose) is the one exception;
- a name-like field (name, title, display name, agency name, subject, label, …) in the E2E specs and helpers, the load
  test, the API and web test fixtures and the API seeder is never set to text with an issue ID (``AA-123``, or a
  ``aa-123`` slug). Put the issue in a comment instead.

The API code's own strings are checked by ``test_no_issue_ids.py`` (every string under ``app/``).
"""

from __future__ import annotations

import re
from pathlib import Path

API = Path(__file__).resolve().parents[2]
REPO = API.parent
ISSUE_ID = re.compile(r"\baa-\d+\b", re.IGNORECASE)
REGISTRY = "shared.schema_versions"
INSERT = re.compile(r"\bINSERT\s+INTO\s+([\w.\"]+)(.*?);", re.IGNORECASE | re.DOTALL)
SQL_LITERAL = re.compile(r"'((?:[^']|'')*)'")
NAME_KEYS = (
    "name",
    "title",
    "display_name",
    "displayName",
    "given_name",
    "family_name",
    "first_name",
    "last_name",
    "full_name",
    "agency_name",
    "tenant_name",
    "company",
    "company_name",
    "label",
    "subject",
    "slug",
)
# A name-like key (bare, quoted or a keyword argument) set to a string, template or f-string literal.
NAME_VALUE = re.compile(
    r"""(?<![\w.])["']?(?:"""
    + "|".join(NAME_KEYS)
    + r""")["']?\s*[:=]\s*(?:[frbFRB]{1,2})?(`[^`]*`|"[^"\n]*"|'[^'\n]*')"""
)
FIXTURE_ROOTS = ("e2e", "loadtest", "api/tests", "api/app/seed", "web/tests")
FIXTURE_SUFFIXES = {".ts", ".js", ".mjs", ".py", ".json", ".sql"}
# The scanners' own samples.
SELF = {Path(__file__).resolve(), (API / "tests" / "unit" / "test_no_issue_ids.py").resolve()}


def _strip_sql_comments(sql: str) -> str:
    return re.sub(r"--[^\n]*", "", sql)


def insert_hits(sql: str) -> list[str]:
    """The values of the INSERTs (outside the migration registry) that carry an issue ID."""
    hits = []
    for m in INSERT.finditer(_strip_sql_comments(sql)):
        if m.group(1).replace('"', "").lower() == REGISTRY:
            continue
        hits.extend(lit for lit in SQL_LITERAL.findall(m.group(2)) if ISSUE_ID.search(lit))
    return hits


def name_hits(text: str) -> list[str]:
    """Name-like fields set to text with an issue ID."""
    return [m.group(0) for m in NAME_VALUE.finditer(text) if ISSUE_ID.search(m.group(1))]


def _fixture_files() -> list[Path]:
    out = []
    for root in FIXTURE_ROOTS:
        base = REPO / root
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if (
                p.suffix in FIXTURE_SUFFIXES
                and "node_modules" not in p.parts
                and p.resolve() not in SELF
                and p.is_file()
            ):
                out.append(p)
    return sorted(out)


def test_migration_inserts_hold_no_issue_id_outside_the_registry() -> None:
    files = sorted((API / "migrations").glob("*.sql"))
    assert len(files) > 50, f"expected the migrations, found {len(files)}"
    hits = [f"{p.name}: {lit[:100]!r}" for p in files for lit in insert_hits(p.read_text(encoding="utf-8"))]
    assert hits == [], "an inserted row names an issue: move the issue into a SQL comment\n" + "\n".join(hits)


def test_no_seed_or_fixture_name_carries_an_issue_id() -> None:
    files = _fixture_files()
    assert any(p.parts[-2:] == ("seed", "catalogue.py") for p in files), "the seeder must be scanned"
    hits = [
        f"{p.relative_to(REPO)}: {hit[:120]}"
        for p in files
        for hit in name_hits(p.read_text(encoding="utf-8", errors="replace"))
    ]
    assert hits == [], (
        "a seed or fixture name carries an issue ID: use an invented name, put the issue in a comment\n"
        + ("\n".join(hits))
    )


def test_the_scanners_catch_names_and_rows_but_not_comments_or_the_registry() -> None:
    sql = """
-- AA-1 comment
INSERT INTO shared.schema_versions (version, applied_at, description) VALUES ('crm-090', now(), 'AA-2: registry');
INSERT INTO crm.setting_definition (key, label) VALUES ('crm.x.y', 'Limit for AA-3');
INSERT INTO crm.tenant (display_name) VALUES ('Harbour Lights');  -- AA-4 note
"""
    assert insert_hits(sql) == ["Limit for AA-3"]
    ts = """
const a = { name: "E2E AA-1086 agency", title: `Trip ${n}` };
test.describe("AA-1040 — studio downloads", () => {});  // a spec title is not data
await api.post("/clients", { display_name: 'Lan (AA-947)' });
const slug = "aa-1086-rehearsal";
"""
    assert name_hits(ts) == ['name: "E2E AA-1086 agency"', "display_name: 'Lan (AA-947)'", 'slug = "aa-1086-rehearsal"']
    py = """
SyntheticAgency(key="harbour", display_name=f"Agency AA-5 {i}")
row = {"title": "Plain trip", "subject": "AA-6 inquiry"}
"""
    assert name_hits(py) == ['display_name=f"Agency AA-5 {i}"', '"subject": "AA-6 inquiry"']
