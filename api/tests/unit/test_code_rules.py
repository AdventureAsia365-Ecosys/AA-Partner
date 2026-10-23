"""Static rules over app/ (card CRM-05 checklist):

- **DB access lint** (design v3 §4.6 "a lint forbids raw queries outside tenant_tx()"): only the modules in
  :data:`DB_MODULES` call asyncpg query methods; routers reach data through ``tenant_tx()`` or the job queue.
- **Import boundary** (ADR 0010 decision 1): app/ imports only the standard library, the pinned third-party packages
  and ``app`` itself — never a CIS or TripPlanner backend module.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"

QUERY_METHODS = frozenset(
    {"fetch", "fetchrow", "fetchval", "execute", "executemany", "acquire", "copy_records_to_table"}
)
DB_MODULES = frozenset(
    {
        "app/db.py",  # readiness probe
        "app/tenancy.py",  # tenant_tx() itself
        "app/jobs/queue.py",  # crm.job state machine (platform table, no tenant rows)
        "app/jobs/metrics.py",  # crm.job backlog sampler
        "app/jobs/handlers.py",  # crm.job retention (platform table)
    }
)
THIRD_PARTY = frozenset(
    {"asyncpg", "boto3", "botocore", "fastapi", "jwt", "pydantic", "sentry_sdk", "starlette", "structlog"}
)
FORBIDDEN_TOP_LEVEL = frozenset({"api", "shared", "backend", "core", "cis", "tripplanner", "lambdas"})


def _modules() -> list[tuple[str, ast.Module]]:
    out = []
    for path in sorted(APP.rglob("*.py")):
        rel = str(path.relative_to(APP.parent))
        out.append((rel, ast.parse(path.read_text(encoding="utf-8"))))
    return out


def test_only_db_modules_run_queries() -> None:
    offenders = []
    for rel, tree in _modules():
        if rel in DB_MODULES:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in QUERY_METHODS:
                offenders.append(f"{rel}:{node.lineno} .{node.func.attr}()")
    assert offenders == [], "query outside tenant_tx() / the job queue: " + ", ".join(offenders)


def test_the_lint_is_not_vacuous() -> None:
    tree = ast.parse("async def f(pool):\n    return await pool.fetch('SELECT 1')\n")
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
    assert any(c.func.attr in QUERY_METHODS for c in calls)


def test_imports_stay_inside_the_boundary() -> None:
    stdlib = set(sys.stdlib_module_names)
    bad = []
    for rel, tree in _modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                if top in FORBIDDEN_TOP_LEVEL or not (top == "app" or top in stdlib or top in THIRD_PARTY):
                    bad.append(f"{rel}:{node.lineno} import {name}")
    assert bad == []


def test_every_third_party_import_is_pinned() -> None:
    requirements = (APP.parent / "requirements.txt").read_text(encoding="utf-8").lower()
    pins = {"jwt": "pyjwt==", "sentry_sdk": "sentry-sdk"}
    for package in THIRD_PARTY:
        assert pins.get(package, f"{package}==") in requirements, package
