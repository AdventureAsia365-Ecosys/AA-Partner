"""Static rules over app/ (card CRM-05 checklist):

- **DB access lint** (design v3 §4.6 "a lint forbids raw queries outside tenant_tx()"): outside the modules in
  :data:`DB_MODULES`, the only allowed asyncpg call is one on ``<ctx>.conn`` written **inside** the body of
  ``async with tenant_tx(...) as <ctx>`` (the RLS context is set there), or of ``provisioning_tx(...)`` in the platform
  provisioning modules :data:`PROVISIONING_MODULES` only (AA-935: creating an agency, before any member exists). Any
  other receiver (a pool, ``request_pool()``, a connection from ``acquire()``, an alias of ``ctx.conn``, ``ctx.conn``
  after the block) fails, and so does rebinding ``<ctx>`` inside the block. Routers reach platform tables through
  the job queue only. AA-936: the platform contexts ``system_tx()`` (job handlers, :data:`SYSTEM_MODULES`) and
  ``intake_tx()`` (the public intake form, :data:`INTAKE_MODULES`) count as context factories too, each allowed only
  in its modules; and a function whose parameter is annotated with a context type (:data:`CONTEXT_TYPES`) may call
  ``<param>.conn`` (it can only have been handed a context that one of the factories opened).
- **Import boundary** (ADR 0010 decision 1): app/ imports only the standard library, the pinned third-party packages
  and ``app`` itself — never a CIS or TripPlanner backend module.
"""

from __future__ import annotations

import ast
import functools
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
        "app/llm/usage.py",  # crm.llm_usage_daily: platform rollup, no tenant rows (AA-936)
        "app/audit_export/registry.py",  # crm-007 platform functions: agency list, partition maker, drop (AA-940)
        "app/privacy/purge.py",  # expired placeholder maps, crm-009 DELETE-only purge policy, no reads (AA-938)
        "app/fx/fetch.py",  # crm.fx_rate_platform: platform reference rates, no tenant rows, insert only (AA-944)
        "app/cis/feed_state.py",  # crm.membership_feed_state: platform singleton, no tenant rows (AA-978)
        "app/catalog/store.py",  # crm.aa_catalog_tour / aa_catalog_state: platform cache, no tenant rows (AA-947)
    }
)
# The only modules that may open the platform context provisioning_tx() (no membership check): agency creation and the
# Dev seeder. Never a router: /crm/v1 request code reaches tenant data through tenant_tx() only.
PROVISIONING_MODULES = frozenset({"app/agencies.py", "app/seed/__init__.py"})
# AA-939: AA staff in one agency (actor platform:<staff sub>; may remove the last owner, complete a recovery).
# AA-941: the L1 console's agency operations (app/ops/console.py) open it too.
PLATFORM_MODULES = frozenset({"app/members/recovery.py", "app/ops/console.py"})
# ... and the only module that calls the platform side of app.members.recovery and app.ops.console: the /crm/ops/v1
# router, whose write routes require the WORKFORCE group crm-platform-admin.
PLATFORM_CALLERS = frozenset({"app/routers/ops.py"})
PLATFORM_FUNCTIONS = (
    "open_request",
    "complete_request",
    "deactivate_member",
    # AA-941 (app.ops.console)
    "list_agencies",
    "agency_summary",
    "agency_usage",
    "agency_entitlements",
    "agency_data_requests",
    "agency_failed_jobs",
    "create_agency",
    "lock_agency",
    "unlock_agency",
    "invite_first_owner",
    "allow_features",
    "set_insights_opt_out",
    "raise_data_request",
    "retry_agency_job",
)
# AA-941: helpers a platform context hands its context to, each called only from its own module and the console.
PLATFORM_HELPERS = {
    "raise_request": frozenset({"app/lifecycle/offboarding.py", "app/ops/console.py"}),
    "create_invitation": frozenset({"app/members/team.py", "app/ops/console.py"}),
    "retry_failed": frozenset({"app/jobs/queue.py", "app/ops/console.py"}),
}
# AA-936: the job context in one agency (no member) and the public intake form's context (AA-942: the dispatcher).
SYSTEM_MODULES = frozenset(
    {
        "app/intake/process.py",
        "app/seed/__init__.py",
        "app/dispatch/worker.py",
        "app/stages_job.py",
        # AA-945: the mailbox health check and the history import (one agency per transaction, actor job:<kind>).
        "app/mail/health.py",
        "app/mail/history_import.py",
        # AA-947: the AA outage queue and the Booking feed (one agency per transaction, actor job:crm_booking_sync).
        "app/booking/sync.py",
    }
)
INTAKE_MODULES = frozenset({"app/intake/public.py"})
# AA-940: the audit export's read context (a locked or trashed agency too): the export, verify and retention jobs only.
AUDIT_EXPORT_MODULES = frozenset(
    {"app/audit_export/export.py", "app/audit_export/verify.py", "app/audit_export/retention.py"}
)
# AA-939: the invitation claim / accept context (token hash lookup, actor invitation:<id>).
INVITATION_MODULES = frozenset({"app/members/invitations.py"})
# AA-943: the signed client link context (token hash lookup, actor client_link:<id>): the public Stage 2 form only.
# AA-946: the proposal accept page and the traveller portal (each opens only its own link purpose).
CLIENT_LINK_MODULES = frozenset({"app/intake/stage2_public.py", "app/proposals/public.py", "app/portal/public.py"})
# AA-945: the forward-in mail ingest context (the recipient's local part picks the agency): the ingest job only.
INGEST_MODULES = frozenset({"app/mail/ingest.py"})
# AA-942: the lifecycle jobs' context (the daily run, the offboarding steps).
LIFECYCLE_MODULES = frozenset(
    {"app/lifecycle/job.py", "app/lifecycle/offboarding.py", "app/portability/exports.py"}  # + AA-967 export job
)
# AA-978: one CIS membership event in one agency (actor platform:feed-tenant-membership, any agency status).
MEMBERSHIP_FEED_MODULES = frozenset({"app/cis/membership_feed.py"})
# AA-979 P2b: Content studio invitations (crm-020 platform table): the platform / job context and the token context.
STUDIO_MODULES = frozenset({"app/members/studio.py"})
CONTEXT_TYPES = frozenset(
    {
        "TenantContext",
        "SystemContext",
        "ProvisioningContext",
        "AnyContext",
        "ExportContext",
        "LifecycleContext",
        "StudioContext",
    }
)
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


@functools.cache
def _modules() -> tuple[tuple[str, ast.Module], ...]:
    # AA-993 (P1): parsed once per test process. The trees are shared by every test here and must stay read-only
    # (walk and inspect them, never edit a node).
    out = []
    for path in sorted(APP.rglob("*.py")):
        rel = str(path.relative_to(APP.parent))
        out.append((rel, ast.parse(path.read_text(encoding="utf-8"))))
    return tuple(out)


CONTEXT_FACTORIES = frozenset(
    {
        "tenant_tx",
        "provisioning_tx",
        "system_tx",
        "platform_tx",
        "intake_tx",
        "audit_export_tx",
        "invitation_tx",
        "lifecycle_tx",
        "client_link_tx",
        "membership_feed_tx",
        "ingest_tx",
        "studio_platform_tx",
        "studio_invitation_tx",
    }
)


def _context_names(target: ast.expr | None) -> list[str]:
    """``as ctx`` or ``as (ctx, form)``: the first name is the context."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Tuple) and target.elts and isinstance(target.elts[0], ast.Name):
        return [target.elts[0].id]
    return []


def _annotation_names(node: ast.expr | None) -> set[str]:
    if node is None:
        return set()
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} | {
        n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }


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
            name
            for item in node.items
            if _is_tenant_tx(item.context_expr)
            for name in _context_names(item.optional_vars)
        ]
        for item in node.items:
            self.visit(item)
        self.ctx_names.extend(bound)
        for stmt in node.body:
            self.visit(stmt)
        del self.ctx_names[len(self.ctx_names) - len(bound) :]

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
        bound = [a.arg for a in args if _annotation_names(a.annotation) & CONTEXT_TYPES]
        self.ctx_names.extend(bound)
        self.generic_visit(node)
        del self.ctx_names[len(self.ctx_names) - len(bound) :]

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

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


def _users_of(name: str, allowed: frozenset[str]) -> list[str]:
    users = []
    for rel, tree in _modules():
        if rel in DB_MODULES or rel in allowed:
            continue
        for node in ast.walk(tree):
            found = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None
            if found == name:
                users.append(f"{rel}:{node.lineno}")
            if isinstance(node, ast.ImportFrom) and any(a.name == name for a in node.names):
                users.append(f"{rel}:{node.lineno} imports {name}")
    return users


def test_system_tx_is_used_only_by_the_job_modules() -> None:
    assert _users_of("system_tx", SYSTEM_MODULES) == []


def test_platform_tx_is_used_only_by_the_recovery_and_console_modules() -> None:
    assert _users_of("platform_tx", PLATFORM_MODULES) == []


def test_the_platform_helpers_are_called_only_by_their_module_and_the_console() -> None:
    """AA-941: raising an AA data request, the shared invitation writer and the job retry run with whatever context
    they are handed; only the console hands them a platform one."""
    for name, allowed in PLATFORM_HELPERS.items():
        assert _users_of(name, allowed) == [], name


def test_the_platform_side_of_recovery_is_called_only_by_the_ops_router() -> None:
    """AA-939 review: a platform: actor bypasses CRM01 / CRM04, so no job, seeder or /crm/v1 module reaches it
    (AA-941: the console's operations too)."""
    users = []
    for name in PLATFORM_FUNCTIONS:
        for found in _users_of(name, PLATFORM_CALLERS | PLATFORM_MODULES):
            users.append(found)
    assert users == [], "platform recovery functions outside the ops router: " + ", ".join(users)
    ops = dict(_modules())["app/routers/ops.py"]
    called = {n.attr for n in ast.walk(ops) if isinstance(n, ast.Attribute)}
    assert set(PLATFORM_FUNCTIONS) <= called  # the walk really sees the router (an empty walk proves nothing)


def test_intake_tx_is_used_only_by_the_public_intake_module() -> None:
    assert _users_of("intake_tx", INTAKE_MODULES) == []


def test_audit_export_tx_is_used_only_by_the_audit_export_jobs() -> None:
    assert _users_of("audit_export_tx", AUDIT_EXPORT_MODULES) == []


def test_invitation_tx_is_used_only_by_the_invitations_module() -> None:
    assert _users_of("invitation_tx", INVITATION_MODULES) == []


def test_client_link_tx_is_used_only_by_the_public_stage2_module() -> None:
    assert _users_of("client_link_tx", CLIENT_LINK_MODULES) == []


def test_membership_feed_tx_is_used_only_by_the_feed_module() -> None:
    """AA-978: the feed's context is a platform actor that may disable the last owner and lock an agency."""
    assert _users_of("membership_feed_tx", MEMBERSHIP_FEED_MODULES) == []


def test_the_feed_state_is_reached_only_by_the_feed() -> None:
    """Plus the readiness probe in ``app/db.py`` (a DB module, so skipped here): ``feed_state.health``, and the L1
    console's agency summary (AA-978 follow-up, crm.ops.v1 1.2), which reads the row in its own SQL and only calls the
    pure ``feed_state.health_word``."""
    allowed = MEMBERSHIP_FEED_MODULES | {"app/cis/feed_state.py", "app/ops/console.py"}
    assert _users_of("feed_state", allowed) == []


def test_studio_contexts_are_used_only_by_the_studio_module() -> None:
    assert _users_of("studio_platform_tx", STUDIO_MODULES) == []
    assert _users_of("studio_invitation_tx", STUDIO_MODULES) == []


def test_ingest_tx_is_used_only_by_the_mail_ingest_job() -> None:
    assert _users_of("ingest_tx", INGEST_MODULES) == []


def test_every_router_tenant_tx_names_its_permission_action() -> None:
    """AA-939 (design §4.6): each /crm/v1 route passes ``action=`` to tenant_tx(), so the permission matrix refuses a
    role before any work. A route without one would only be checked by whatever its helper remembers to do."""
    missing = []
    for rel, tree in _modules():
        if not rel.startswith("app/routers/"):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_tenant_tx(node) and getattr(node.func, "id", None) == "tenant_tx":
                if not any(k.arg == "action" for k in node.keywords):
                    missing.append(f"{rel}:{node.lineno}")
    assert missing == [], "tenant_tx() without action=: " + ", ".join(missing)


def test_lifecycle_tx_is_used_only_by_the_lifecycle_jobs() -> None:
    assert _users_of("lifecycle_tx", LIFECYCLE_MODULES) == []


def test_only_the_erase_module_calls_the_deletion_functions() -> None:
    """AA-942: every hard delete of agency data goes through app/lifecycle/erase.py (crm.erase_subject /
    crm.lifecycle_purge_expired, as crm_eraser)."""
    calls = ("crm.erase_subject($", "crm.lifecycle_purge_expired($")
    users = [
        str(path.relative_to(APP.parent))
        for path in sorted(APP.rglob("*.py"))
        if any(c in path.read_text(encoding="utf-8") for c in calls)
    ]
    assert users == ["app/lifecycle/erase.py"]


CONTEXT_PARAM_ALLOWED = """
async def helper(ctx: TenantContext | SystemContext, x):
    return await ctx.conn.fetchrow("SELECT 1")

async def job(pool, t):
    async with system_tx(pool, t, actor="job:x") as ctx:
        await ctx.conn.execute("SELECT 1")
    async with intake_tx(pool, "k" * 22) as (ictx, form):
        await ictx.conn.execute("SELECT 1")
"""

CONTEXT_PARAM_FORBIDDEN = {
    "untyped parameter": "async def helper(ctx):\n    await ctx.conn.fetch('x')\n",
    "other type": "async def helper(ctx: Pool):\n    await ctx.conn.fetch('x')\n",
    "after the function": (
        "async def helper(ctx: TenantContext):\n    pass\n\nasync def g(ctx):\n    await ctx.conn.fetch('x')\n"
    ),
    "the form, not the context": (
        "async def f(pool):\n    async with intake_tx(pool, 'k') as (ctx, form):\n        await form.conn.fetch('x')\n"
    ),
}


def test_the_lint_allows_context_typed_parameters() -> None:
    assert db_access_offenders(ast.parse(CONTEXT_PARAM_ALLOWED)) == []


def test_the_lint_refuses_untyped_context_parameters() -> None:
    for name, source in CONTEXT_PARAM_FORBIDDEN.items():
        assert db_access_offenders(ast.parse(source)), name


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


def test_only_the_fx_fetch_job_writes_platform_rates() -> None:
    """AA-944: ``crm.fx_rate_platform`` is reference data the daily ``crm_fx_fetch`` job fills (app/fx/fetch.py); no
    request path writes a platform rate (the owner's own rates go to ``crm.fx_rate_override``)."""
    writers = [
        rel
        for rel, tree in _modules()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "fx_rate_platform" in node.value
        and any(word in node.value.upper() for word in ("INSERT", "UPDATE", "DELETE"))
    ]
    assert sorted(set(writers)) == ["app/fx/fetch.py"]
