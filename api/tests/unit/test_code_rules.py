"""Static rules over app/ (card CRM-05 checklist):

- **DB access lint** (design v3 §4.6 "a lint forbids raw queries outside tenant_tx()"): outside the modules in
  :data:`DB_MODULES`, the only allowed asyncpg call is one on ``<ctx>.conn`` written **inside** the body of
  ``async with tenant_tx(...) as <ctx>`` (the RLS context is set there), or of ``provisioning_tx(...)`` in the platform
  provisioning modules :data:`PROVISIONING_MODULES` only (AA-935: creating an agency, before any member exists). Any
  other receiver (a pool, ``request_pool()``, a connection from ``acquire()``, an alias of ``ctx.conn``, ``ctx.conn``
  after the block) fails, and so does rebinding ``<ctx>`` inside the block. Routers reach platform tables through
  the job queue only.
- **Import boundary** (ADR 0010 decision 1): app/ imports only the standard library, the pinned third-party packages
  and ``app`` itself — never a CIS or TripPlanner backend module.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"

# asyncpg Pool / Connection methods that run SQL or hand out a connection (asyncpg 0.30 API).
QUERY_METHODS = frozenset(
    {
        "acquire",
        "copy_from_query",
        "copy_from_table",
        "copy_records_to_table",
        "copy_to_table",
        "cursor",
        "execute",
        "executemany",
        "fetch",
        "fetchmany",
        "fetchrow",
        "fetchval",
        "prepare",
    }
)
DB_MODULES = frozenset(
    {
        "app/db.py",  # readiness probe
        "app/tenancy.py",  # tenant_tx() itself
        "app/jobs/queue.py",  # crm.job state machine (platform table, no tenant rows)
        "app/jobs/metrics.py",  # crm.job backlog sampler
        "app/jobs/handlers.py",  # crm.job retention (platform table)
        "app/vault/store.py",  # DEK store: crm.tenant_key, only on the connection of a TenantContext (AA-934)
    }
)
# The only modules that may open the platform context provisioning_tx() (no membership check): agency creation and the
# Dev seeder. Never a router: /crm/v1 request code reaches tenant data through tenant_tx() only.
PROVISIONING_MODULES = frozenset({"app/agencies.py", "app/seed/__init__.py"})
THIRD_PARTY = frozenset(
    {
        "asyncpg",
        "boto3",
        "botocore",
        "cryptography",
        "faker",
        "fastapi",
        "jwt",
        "phonenumbers",
        "pydantic",
        "sentry_sdk",
        "starlette",
        "structlog",
    }
)
FORBIDDEN_TOP_LEVEL = frozenset({"api", "shared", "backend", "core", "cis", "tripplanner", "lambdas"})


def _modules() -> list[tuple[str, ast.Module]]:
    out = []
    for path in sorted(APP.rglob("*.py")):
        rel = str(path.relative_to(APP.parent))
        out.append((rel, ast.parse(path.read_text(encoding="utf-8"))))
    return out


CONTEXT_FACTORIES = frozenset({"tenant_tx", "provisioning_tx"})


def _is_tenant_tx(call: ast.expr) -> bool:
    if not isinstance(call, ast.Call):
        return False
    func = call.func
    return (isinstance(func, ast.Name) and func.id in CONTEXT_FACTORIES) or (
        isinstance(func, ast.Attribute) and func.attr in CONTEXT_FACTORIES
    )


class _DbAccess(ast.NodeVisitor):
    """Walks one module, tracking the names bound by ``async with tenant_tx(...) as <name>`` that are in scope."""

    def __init__(self) -> None:
        self.ctx_names: list[str] = []
        self.offenders: list[str] = []

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        bound = [
            item.optional_vars.id
            for item in node.items
            if _is_tenant_tx(item.context_expr) and isinstance(item.optional_vars, ast.Name)
        ]
        for item in node.items:
            self.visit(item)
        self.ctx_names.extend(bound)
        for stmt in node.body:
            self.visit(stmt)
        del self.ctx_names[len(self.ctx_names) - len(bound) :]

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store | ast.Del) and node.id in self.ctx_names:
            self.offenders.append(f"{node.lineno} rebinds tenant context {node.id!r}")

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in QUERY_METHODS:
            receiver = func.value
            on_ctx_conn = (
                isinstance(receiver, ast.Attribute)
                and receiver.attr == "conn"
                and isinstance(receiver.value, ast.Name)
                and receiver.value.id in self.ctx_names
            )
            if not on_ctx_conn:
                self.offenders.append(f"{node.lineno} .{func.attr}() outside tenant_tx() ctx.conn")
        self.generic_visit(node)


def db_access_offenders(tree: ast.Module) -> list[str]:
    visitor = _DbAccess()
    visitor.visit(tree)
    return visitor.offenders


def test_only_db_modules_run_queries() -> None:
    offenders = []
    for rel, tree in _modules():
        if rel not in DB_MODULES:
            offenders += [f"{rel}:{o}" for o in db_access_offenders(tree)]
    assert offenders == [], "query outside tenant_tx() / the job queue: " + ", ".join(offenders)


ALLOWED = """
async def route(request, caller, tenant_id):
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id) as ctx:
        rows = await ctx.conn.fetch("SELECT 1")
        await ctx.conn.execute("SELECT 2")
        async with ctx.conn.transaction():
            stmt = await ctx.conn.prepare("SELECT 3")
        async for r in ctx.conn.cursor("SELECT 4"):
            pass
    async with app.tenancy.tenant_tx(pool, caller, tenant_id) as other:
        await other.conn.fetchval("SELECT 5")
    return rows
"""

FORBIDDEN = {
    "pool query": "async def f(request):\n    pool = await request_pool(request)\n    await pool.fetch('x')\n",
    "request_pool chain": "async def f(request):\n    await (await request_pool(request)).fetchval('x')\n",
    "pool acquire": "async def f(pool):\n    async with pool.acquire() as conn:\n        await conn.fetch('x')\n",
    "after the block": (
        "async def f(pool, c, t):\n    async with tenant_tx(pool, c, t) as ctx:\n        pass\n"
        "    await ctx.conn.fetch('x')\n"
    ),
    "alias of ctx.conn": (
        "async def f(pool, c, t):\n    async with tenant_tx(pool, c, t) as ctx:\n        conn = ctx.conn\n"
        "        await conn.fetch('x')\n"
    ),
    "rebinding ctx": (
        "async def f(pool, c, t, other):\n    async with tenant_tx(pool, c, t) as ctx:\n        ctx = other\n"
        "        await ctx.conn.fetch('x')\n"
    ),
    "not a tenant_tx block": (
        "async def f(pool):\n    async with something(pool) as ctx:\n        await ctx.conn.fetch('x')\n"
    ),
    "prepare": "async def f(conn):\n    await conn.prepare('x')\n",
    "cursor": "async def f(conn):\n    conn.cursor('x')\n",
    "fetchmany": "async def f(conn):\n    await conn.fetchmany('x', [])\n",
    "copy_from_query": "async def f(conn):\n    await conn.copy_from_query('x', output='y')\n",
    "copy_to_table": "async def f(conn):\n    await conn.copy_to_table('t', source='y')\n",
}


def test_provisioning_tx_is_used_only_by_the_provisioning_modules() -> None:
    users = []
    for rel, tree in _modules():
        if rel in DB_MODULES or rel in PROVISIONING_MODULES:
            continue
        for node in ast.walk(tree):
            name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None
            if name == "provisioning_tx":
                users.append(f"{rel}:{node.lineno}")
            if isinstance(node, ast.ImportFrom) and any(a.name == "provisioning_tx" for a in node.names):
                users.append(f"{rel}:{node.lineno} imports provisioning_tx")
    assert users == [], "provisioning_tx outside agency provisioning: " + ", ".join(users)


def test_the_lint_allows_ctx_conn_inside_tenant_tx() -> None:
    assert db_access_offenders(ast.parse(ALLOWED)) == []


def test_the_lint_flags_every_other_db_access() -> None:
    for name, source in FORBIDDEN.items():
        assert db_access_offenders(ast.parse(source)), name


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
    pins = {"jwt": "pyjwt==", "sentry_sdk": "sentry-sdk", "phonenumbers": "phonenumberslite==", "faker": "faker=="}
    for package in THIRD_PARTY:
        assert pins.get(package, f"{package}==") in requirements, package
