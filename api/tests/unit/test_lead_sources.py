"""AA-957 S262 round 3: the trip card printed the raw lead source "synthetic_seed". ``app.lead_sources`` lists every
code the API writes; this test reads app/ and keeps the list complete in both directions (the web test
``web/tests/closedTripRound3.test.ts`` then checks each listed code has an EN and a VI label).

What counts as "written": a ``lead_source=`` keyword literal (or a parameter default), a ``"lead_source": "..."`` dict
literal, the fallback of ``.get("lead_source") or "..."``, and a literal in the ``lead_source`` column of an
``INSERT INTO crm.trip`` / ``crm.traveler`` / ``crm.intake_form`` SQL text.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from app.lead_sources import LEAD_SOURCES

APP = Path(__file__).resolve().parents[2] / "app"
CODE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
INSERT = re.compile(r"INSERT INTO crm\.(?:trip|traveler|intake_form)\s*\(([^)]*)\)\s*VALUES\s*\(([^)]*)\)", re.S)


def _split(text: str) -> list[str]:
    return [part.strip() for part in text.split(",")]


def _literal(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def written() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}

    def add(code: str | None, where: str) -> None:
        if code is not None:
            found.setdefault(code, []).append(where)

    for path in sorted(APP.rglob("*.py")):
        where = str(path.relative_to(APP))
        source = path.read_text(encoding="utf-8")
        for match in INSERT.finditer(source):
            columns, values = _split(match.group(1)), _split(match.group(2))
            if "lead_source" in columns and len(values) == len(columns):
                value = values[columns.index("lead_source")]
                if value.startswith("'"):
                    add(value.strip("'"), where)
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.keyword) and node.arg == "lead_source":
                add(_literal(node.value), where)
            elif isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values, strict=True):
                    if _literal(key) == "lead_source":
                        add(_literal(value), where)
            elif isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or) and len(node.values) == 2:
                call = node.values[0]
                if isinstance(call, ast.Call) and call.args and _literal(call.args[0]) == "lead_source":
                    add(_literal(node.values[1]), where)
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                args = node.args.kwonlyargs
                for arg, default in zip(args, node.args.kw_defaults, strict=True):
                    if arg.arg == "lead_source":
                        add(_literal(default), where)
                positional = node.args.posonlyargs + node.args.args
                for arg, default in zip(positional[len(positional) - len(node.args.defaults) :], node.args.defaults,
                                        strict=True):  # fmt: skip
                    if arg.arg == "lead_source":
                        add(_literal(default), where)
    return found


def test_every_written_lead_source_is_listed_and_every_listed_one_is_written() -> None:
    found = written()
    assert set(found) - set(LEAD_SOURCES) == set(), {c: found[c] for c in set(found) - set(LEAD_SOURCES)}
    assert set(LEAD_SOURCES) - set(found) == set(), "listed but no longer written"
    # The live evidence: the seed's form code, and the codes of the import and the agent's own clients.
    assert {"synthetic_seed", "import", "agent_created", "website_form"} <= set(found)


def test_the_listed_codes_are_unique_database_codes() -> None:
    assert len(set(LEAD_SOURCES)) == len(LEAD_SOURCES)
    assert all(CODE.match(c) for c in LEAD_SOURCES)
