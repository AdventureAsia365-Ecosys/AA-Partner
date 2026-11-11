"""AA-1085 lint: every legal-hold guard reads the holds of the row's own agency (an explicit tenant predicate).

A foreign-key action (ON DELETE CASCADE / SET NULL) runs its query, and the BEFORE triggers it fires, as the table
owner without row security, so a hold guard that leans on RLS for the agency reads every agency's holds there. The
rule, checked on the latest definition of each crm trigger function in ``api/migrations/`` (a later file replaces an
earlier one):

- a trigger function that reads holds (``crm.legal_hold`` or the hold rule functions) never calls the one-argument
  ``crm.hold_row`` / ``crm.hold_trip`` / ``crm.hold_traveler`` (they rely on RLS); it calls the ``*_of`` forms with
  the row's tenant;
- every ``FROM crm.legal_hold`` in such a function, and in the ``*_of`` functions, carries ``tenant_id =``; the
  ``*_of`` functions also read ``crm.trip_traveler`` with ``tenant_id = p_tenant``.

The integration test ``tests/integration/test_crm058_schema.py`` checks the same rule on the replayed catalog.
"""

from __future__ import annotations

import re
from pathlib import Path

MIGRATIONS = sorted((Path(__file__).resolve().parents[2] / "migrations").glob("*.sql"))

FUNCTION_RE = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+crm\.(\w+)\s*\(([^)]*)\)\s*RETURNS\s+(\w+).*?AS\s+\$\$(.*?)\$\$\s*;",
    re.IGNORECASE | re.DOTALL,
)
READS_HOLDS_RE = re.compile(r"crm\.legal_hold\b|crm\.hold_(?:row|trip|traveler)(?:_of)?\s*\(", re.IGNORECASE)
RLS_HOLD_CALL_RE = re.compile(r"crm\.hold_(?:row|trip|traveler)\s*\(", re.IGNORECASE)  # not the *_of forms
HOLD_READ_RE = re.compile(r"FROM\s+crm\.legal_hold\b", re.IGNORECASE)
PARTY_READ_RE = re.compile(r"FROM\s+crm\.trip_traveler\b", re.IGNORECASE)
TENANT_OF = ("hold_trip_of", "hold_traveler_of", "hold_row_of")
# Hold guards that exist today (a parse that finds fewer is broken, not clean).
KNOWN_GUARDS = {
    "guard_held_change",
    "guard_message",
    "guard_held_flight",
    "guard_ops_release",
    "guard_supplier_manifest_export",
    "guard_held_travel_doc",
}


def latest_functions(paths: list[Path]) -> dict[str, tuple[str, str, str]]:
    """name -> (returns, body, file) of the last definition across the files in order."""
    found: dict[str, tuple[str, str, str]] = {}
    for path in paths:
        for m in FUNCTION_RE.finditer(path.read_text(encoding="utf-8")):
            found[m.group(1)] = (m.group(3).lower(), m.group(4), path.name)
    return found


def _strip_comments(body: str) -> str:
    return re.sub(r"--[^\n]*", "", body)


def _reads_without_tenant(body: str, read_re: re.Pattern[str], predicate: str) -> list[str]:
    """Each matching read whose WHERE (the next 200 characters) lacks the tenant predicate."""
    out = []
    for m in read_re.finditer(body):
        window = body[m.end() : m.end() + 200]
        if predicate not in re.sub(r"\s+", " ", window):
            out.append(re.sub(r"\s+", " ", body[m.start() : m.end() + 60]).strip())
    return out


def guard_violations(name: str, body: str) -> list[str]:
    body = _strip_comments(body)
    problems = [f"{name}: calls {m.group(0)}...) (RLS-only hold check)" for m in RLS_HOLD_CALL_RE.finditer(body)]
    problems += [f"{name}: {r} without tenant_id =" for r in _reads_without_tenant(body, HOLD_READ_RE, "tenant_id =")]
    return problems


def hold_guards(functions: dict[str, tuple[str, str, str]]) -> dict[str, str]:
    return {
        name: body
        for name, (returns, body, _) in functions.items()
        if returns == "trigger" and READS_HOLDS_RE.search(_strip_comments(body))
    }


def test_every_hold_guard_reads_the_holds_of_its_own_agency() -> None:
    guards = hold_guards(latest_functions(MIGRATIONS))
    assert set(guards) >= KNOWN_GUARDS
    problems = [p for name, body in sorted(guards.items()) for p in guard_violations(name, body)]
    assert problems == []


def test_the_tenant_hold_functions_filter_every_read() -> None:
    functions = latest_functions(MIGRATIONS)
    for name in TENANT_OF:
        returns, body, _ = functions[name]
        assert returns == "boolean"
        body = _strip_comments(body)
        assert not RLS_HOLD_CALL_RE.search(body), name
        assert _reads_without_tenant(body, HOLD_READ_RE, "tenant_id = p_tenant") == [], name
        assert _reads_without_tenant(body, PARTY_READ_RE, "tenant_id = p_tenant") == [], name
    assert HOLD_READ_RE.search(functions["hold_trip_of"][1])
    assert PARTY_READ_RE.search(functions["hold_traveler_of"][1])


def test_the_lint_flags_the_rls_only_guards_it_replaced() -> None:
    """The crm-010 .. crm-054 texts (before crm-058) are exactly what the lint must refuse."""
    before = [p for p in MIGRATIONS if p.name < "058_"]
    guards = hold_guards(latest_functions(before))
    assert set(guards) >= KNOWN_GUARDS
    for name in KNOWN_GUARDS:
        assert guard_violations(name, guards[name]), name
    # The precise findings on two of them.
    assert any("FROM crm.legal_hold h WHERE h.status" in p for p in guard_violations("x", guards["guard_held_change"]))
    assert any("crm.hold_row(" in p for p in guard_violations("x", guards["guard_held_travel_doc"]))


def test_a_comment_naming_a_hold_function_is_not_a_call() -> None:
    body = "BEGIN\n    -- crm.hold_row(trip, traveler) was the RLS form\n    RETURN NEW;\nEND;"
    assert guard_violations("x", body) == []
    assert not READS_HOLDS_RE.search(_strip_comments(body))
