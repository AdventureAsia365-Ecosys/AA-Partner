"""crm-002 tenancy base on PostgreSQL 15: the base table template on every crm table, FORCE RLS, tenant_tx().

Card CRM-05 R test: "a cross-tenant SELECT returns 0 rows" (as crm_app inside tenant_tx() of agency A, rows of agency B
are invisible, even when asked for by id) and inserts into another agency are refused.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest
import pytest_asyncio

from app.auth.route_guard import Caller
from app.errors import Forbidden
from app.tenancy import provisioning_tx, tenant_tx
from tests.integration.conftest import APP_ROLE, OWNER, READERS

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

# The roles of the one tenant policy: never PUBLIC (ecosystem catalogue rule b, AA-CIS-Infra db/rls_catalogue_rules.py).
POLICY_ROLES = {OWNER, APP_ROLE, *READERS}

# Every policy on a crm table other than tenant_isolation, by (table, name): its command and roles. Anything else is
# refused, so an extra permissive policy (which would widen tenant_isolation, policies are OR-ed) needs a line here.
#   crm-004: the SELECT-only membership lookups of app.tenancy.caller_memberships() (keyed on app.caller_iss/_sub).
EXTRA_POLICIES = {
    ("member", "member_self_lookup"): ("r", {APP_ROLE}),
    ("tenant", "tenant_member_lookup"): ("r", {APP_ROLE}),
    #   crm-005: the public intake form lookup of app.tenancy.intake_tx() (keyed on app.intake_key).
    ("intake_form", "intake_form_public_lookup"): ("r", {APP_ROLE}),
    #   crm-008: the invitation claim / accept lookup of app.tenancy.invitation_tx() (keyed on app.invite_token_hash).
    ("member_invitation", "invitation_token_lookup"): ("r", {APP_ROLE}),
    #   crm-009: the hourly purge of expired F3 placeholder maps (DELETE only, keyed on app.purge_scope + expires_at).
    ("placeholder_map", "placeholder_map_expired_purge"): ("d", {APP_ROLE}),
    #   crm-012: the signed client link lookup of app.tenancy.client_link_tx() (keyed on app.client_link_hash).
    ("client_link", "client_link_token_lookup"): ("r", {APP_ROLE}),
    #   crm-014: the RESTRICTIVE margin gate on line money (owner / agent / job only; narrows tenant_isolation).
    ("booking_line_money", "booking_line_money_margin_roles"): ("r", {APP_ROLE}),
    #   crm-015: the forward-in address lookup of app.tenancy.ingest_tx() (keyed on app.ingest_local_part).
    ("ingest_address", "ingest_address_lookup"): ("r", {APP_ROLE}),
}
# Extra policies that must be RESTRICTIVE (ANDed with tenant_isolation); every other extra policy is permissive.
RESTRICTIVE_POLICIES = {("booking_line_money", "booking_line_money_margin_roles")}

ISS = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_TESTPARTN"

# Tables in schema crm that are not tenant tables, with the reason. Everything else must follow
# api/db/templates/tenant_table.sql.
PLATFORM_TABLES = {
    "job": "ADR 0003a job table template (crm-001); ids-only payloads, no tenant rows",
    "status_transition": "reference data for the status trigger (crm-002), no tenant column",
    "llm_usage_daily": "LLM usage rollup over every agency (crm-005), no tenant column, no PII",
    "audit_export_tenant": "audit export registry, one row per agency, ids and times only (crm-007, AA-940)",
    "membership_feed_state": "the CIS membership feed cursor, one platform row, no tenant column (crm-013, AA-978)",
    "fx_rate_platform": "reference FX rates per day, no tenant column, no PII (crm-014, AA-944)",
    "aa_catalog_tour": "published AA tour content cached from CIS catalog.http.v1, no tenant column (crm-017, AA-947)",
    "aa_catalog_state": "the catalog sync's one platform row, no tenant column (crm-017, AA-947)",
    "studio_invitation": "Content studio invitations of CIS tenants (maybe no CRM agency), RLS on actor / token "
    "(crm-020, AA-979)",
    "aa_content": "AA-authored content (prep packs, starter templates) cached from CIS catalog.http.v1, no tenant "
    "column, no PII (crm-022, AA-951)",
}
# Append-only tenant tables (crm-003): tenant column, RLS and policy as the template, but no updated_at, no trash
# columns; instead the append-only trigger, and crm_app may only INSERT and SELECT.
APPEND_ONLY_TABLES = {
    "audit_event": "F5 agency audit (crm-003, AA-934; CRM-12 extends it)",
    "consent": "consent evidence, DM-6.5 (crm-005, AA-936)",
    "trip_stage_history": "every stage move of a trip (crm-005, AA-936)",
    "llm_call": "LLM gateway call log, no text (crm-005, AA-936)",
    "audit_export_manifest": "the hourly audit export hash chain (crm-007, AA-940)",
    "lifecycle_run": "counts of each lifecycle job run per agency (crm-010, AA-942)",
    "membership_inbox": "one row per CIS membership feed event, ids and codes only (crm-013, AA-978)",
    "booking_line_money": "money of a booking line, a frozen FX block, corrections supersede (crm-014, AA-944)",
    "fx_rate_override": "the owner's FX rate of a day, the latest row wins (crm-014, AA-944)",
    "payment": "a payment the agency marked received against a schedule row (crm-016, AA-946)",
    "inbox_events": "one row per consumed Booking feed event, ids and codes only (crm-017, AA-947)",
}
# AA-942 (crm-010): append-only tables that erasure must reach. Their trigger is crm.erasable_append_only(): UPDATE and
# TRUNCATE always refused, DELETE only as crm_owner inside crm.erase_subject / crm.lifecycle_purge_expired. Every other
# append-only table (the audit above all) keeps crm.reject_append_only_change().
ERASABLE_APPEND_ONLY = {"consent", "trip_stage_history", "llm_call"}
# AA-978 (crm-013): membership_inbox is erased with its agency (tenant scope) through its own copy of that rule
# (crm-010's Guard 1 pins crm.erasable_append_only() to the three tables above).
OWN_APPEND_ONLY_TRIGGER = {
    "membership_inbox": "membership_inbox_append_only",
    # AA-944 (crm-014): the ledger's append-only tables, erased with their trip / agency.
    "booking_line_money": "ledger_append_only",
    "fx_rate_override": "ledger_append_only",
    # AA-946 (crm-016): the manual payments, the same ledger rule.
    "payment": "ledger_append_only",
    # AA-947 (crm-017): the Booking feed inbox, erased with its agency.
    "inbox_events": "ledger_append_only",
}
APPEND_ONLY_COLUMNS = {
    "tenant_id": ("uuid", True),
    "created_at": ("timestamp with time zone", True),
    "created_by": ("text", True),
}
BASE_COLUMNS = {
    "tenant_id": ("uuid", True),
    "created_at": ("timestamp with time zone", True),
    "created_by": ("text", True),
    "updated_at": ("timestamp with time zone", True),
    "deleted_at": ("timestamp with time zone", False),
    "deleted_by": ("text", False),
    "purge_after": ("timestamp with time zone", False),
}


def caller(sub: str) -> Caller:
    return Caller("agency", "partner", "partner-web", ISS, sub, ())


async def _seed(pool: asyncpg.Pool, *, role: str = "owner", member_status: str = "active") -> tuple[uuid.UUID, str]:
    """A synthetic agency with one member, written as crm_app the way the API will (tenant set first)."""
    tenant_id, sub = uuid.uuid4(), f"sub-{uuid.uuid4()}"
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        await conn.execute("SELECT set_config('app.actor', 'seed:test', true)")  # crm-008: crm_app names its actor
        await conn.execute("INSERT INTO crm.tenant (tenant_id) VALUES ($1)", tenant_id)
        await conn.execute(
            "INSERT INTO crm.member (cognito_iss, cognito_sub, role, status) VALUES ($1, $2, $3, 'invited')",
            ISS,
            sub,
            role,
        )
        if member_status != "invited":
            await conn.execute("UPDATE crm.member SET status = $1 WHERE cognito_sub = $2", member_status, sub)
    return tenant_id, sub


@pytest_asyncio.fixture
async def two_agencies(app_pool: asyncpg.Pool) -> AsyncIterator[tuple[tuple[uuid.UUID, str], tuple[uuid.UUID, str]]]:
    yield await _seed(app_pool), await _seed(app_pool, role="agent")


async def test_every_crm_table_follows_the_base_template(admin: asyncpg.Connection) -> None:
    tables = await admin.fetch(
        "SELECT c.oid, c.relname, c.relrowsecurity, c.relforcerowsecurity FROM pg_class c "
        "WHERE c.relnamespace = 'crm'::regnamespace AND c.relkind IN ('r', 'p') AND NOT c.relispartition "
        "ORDER BY c.relname"
    )
    names = {t["relname"] for t in tables}
    assert {"tenant", "member"} <= names and set(PLATFORM_TABLES) <= names
    problems = []
    for t in tables:
        name = t["relname"]
        if name in PLATFORM_TABLES:
            continue
        cols = {
            r["attname"]: (r["typ"], r["attnotnull"])
            for r in await admin.fetch(
                "SELECT attname, format_type(atttypid, atttypmod) AS typ, attnotnull FROM pg_attribute "
                "WHERE attrelid = $1 AND attnum > 0 AND NOT attisdropped",
                t["oid"],
            )
        }
        append_only = name in APPEND_ONLY_TABLES
        for col, want in (APPEND_ONLY_COLUMNS if append_only else BASE_COLUMNS).items():
            if cols.get(col) != want:
                problems.append(f"{name}.{col}: {cols.get(col)} != {want}")
        if not (t["relrowsecurity"] and t["relforcerowsecurity"]):
            problems.append(f"{name}: ENABLE + FORCE row level security")
        policies = await admin.fetch(
            "SELECT polname, polcmd::text AS cmd, polpermissive, "
            "pg_get_expr(polqual, polrelid) AS q, pg_get_expr(polwithcheck, polrelid) AS c, "
            "ARRAY(SELECT CASE r WHEN 0 THEN 'public' ELSE r::regrole::text END FROM unnest(polroles) r) AS roles "
            "FROM pg_policy WHERE polrelid = $1",
            t["oid"],
        )
        tenant_policy = [
            p
            for p in policies
            if "current_setting('app.tenant_id'" in (p["q"] or "") and "app.tenant_id" in (p["c"] or "")
        ]
        if not tenant_policy or set(tenant_policy[0]["roles"]) != POLICY_ROLES:
            problems.append(f"{name}: one tenant_isolation policy TO the CRM roles (USING + WITH CHECK)")
        problems += [f"{name}.{p['polname']}: TO PUBLIC (catalogue rule b)" for p in policies if "public" in p["roles"]]
        for p in policies:
            if p["polname"] == "tenant_isolation":
                continue
            want = EXTRA_POLICIES.get((name, p["polname"]))
            if want is None:
                problems.append(f"{name}.{p['polname']}: policy not listed in EXTRA_POLICIES")
            elif (p["cmd"], set(p["roles"])) != want:
                problems.append(f"{name}.{p['polname']}: {p['cmd']} TO {sorted(p['roles'])} != {want}")
            if want is not None and p["polpermissive"] == ((name, p["polname"]) in RESTRICTIVE_POLICIES):
                problems.append(f"{name}.{p['polname']}: permissive must be {not p['polpermissive']}")
        triggers = await admin.fetch(
            "SELECT p.proname FROM pg_trigger g JOIN pg_proc p ON p.oid = g.tgfoid "
            "WHERE g.tgrelid = $1 AND NOT g.tgisinternal",
            t["oid"],
        )
        if append_only:
            want_fn = OWN_APPEND_ONLY_TRIGGER.get(
                name, "erasable_append_only" if name in ERASABLE_APPEND_ONLY else "reject_append_only_change"
            )
            if want_fn not in {r["proname"] for r in triggers}:
                problems.append(f"{name}: append-only trigger ({want_fn})")
            for privilege in ("UPDATE", "DELETE", "TRUNCATE"):
                if await admin.fetchval("SELECT has_table_privilege($1, $2::oid, $3)", APP_ROLE, t["oid"], privilege):
                    problems.append(f"{name}: crm_app may {privilege} an append-only table")
            continue
        if "set_updated_at" not in {r["proname"] for r in triggers}:
            problems.append(f"{name}: updated_at trigger")
        if "status" in cols and "enforce_status_transition" not in {r["proname"] for r in triggers}:
            problems.append(f"{name}: status column without the transition trigger")
        if "stage" in cols and name == "trip" and "enforce_stage_transition" not in {r["proname"] for r in triggers}:
            problems.append(f"{name}: stage column without the stage transition trigger")
        trash = await admin.fetchval(
            "SELECT count(*) FROM pg_constraint WHERE conrelid = $1 AND contype = 'c' AND conname = $2",
            t["oid"],
            f"{name}_trash_chk",
        )
        if trash != 1:
            problems.append(f"{name}: {name}_trash_chk")
    assert problems == []


async def test_every_partition_is_reached_through_its_parent(admin: asyncpg.Connection) -> None:
    """AA-940 (crm-007): a partition (crm.audit_event's months) is skipped above and checked here instead: ENABLE +
    FORCE RLS with the tenant policy to the CRM roles, no crm_app privilege (the parent's grants and policy are what a
    query through the parent uses), both append-only triggers."""
    parts = await admin.fetch(
        "SELECT c.oid, c.relname, c.relrowsecurity, c.relforcerowsecurity, i.inhparent::regclass::text AS parent "
        "FROM pg_class c JOIN pg_inherits i ON i.inhrelid = c.oid "
        "WHERE c.relnamespace = 'crm'::regnamespace AND c.relispartition AND c.relkind = 'r' ORDER BY c.relname"
    )
    assert {p["parent"] for p in parts} == {"crm.audit_event"}
    assert len(parts) >= 3
    problems = []
    for p in parts:
        name = p["relname"]
        if not (p["relrowsecurity"] and p["relforcerowsecurity"]):
            problems.append(f"{name}: ENABLE + FORCE row level security")
        roles = await admin.fetchval(
            "SELECT ARRAY(SELECT r::regrole::text FROM unnest(polroles) r) FROM pg_policy "
            "WHERE polrelid = $1 AND polname = 'tenant_isolation'",
            p["oid"],
        )
        if roles is None or set(roles) != POLICY_ROLES:
            problems.append(f"{name}: tenant_isolation TO the CRM roles")
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"):
            if await admin.fetchval("SELECT has_table_privilege($1, $2::oid, $3)", APP_ROLE, p["oid"], privilege):
                problems.append(f"{name}: crm_app may {privilege} a partition")
        triggers = await admin.fetchval(
            "SELECT count(*) FROM pg_trigger g JOIN pg_proc f ON f.oid = g.tgfoid "
            "WHERE g.tgrelid = $1 AND NOT g.tgisinternal AND f.proname = 'reject_append_only_change'",
            p["oid"],
        )
        if triggers != 2:
            problems.append(f"{name}: append-only row + TRUNCATE triggers")
    assert problems == []


async def test_no_tenant_set_means_no_rows(app_pool: asyncpg.Pool, two_agencies) -> None:  # noqa: ANN001
    async with app_pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM crm.tenant") == 0
        assert await conn.fetchval("SELECT count(*) FROM crm.member") == 0


async def test_cross_tenant_select_returns_zero_rows(app_pool: asyncpg.Pool, two_agencies) -> None:  # noqa: ANN001
    (a, a_sub), (b, _b_sub) = two_agencies
    async with tenant_tx(app_pool, caller(a_sub), a) as ctx:
        assert ctx.member_role == "owner"
        assert await ctx.conn.fetchval("SELECT count(*) FROM crm.member WHERE tenant_id = $1", b) == 0
        assert await ctx.conn.fetchval("SELECT count(*) FROM crm.tenant WHERE tenant_id = $1", b) == 0
        assert {r["tenant_id"] for r in await ctx.conn.fetch("SELECT tenant_id FROM crm.member")} == {a}


async def test_writing_into_another_agency_is_refused(app_pool: asyncpg.Pool, two_agencies) -> None:  # noqa: ANN001
    (a, a_sub), (b, _b_sub) = two_agencies
    with pytest.raises(asyncpg.InsufficientPrivilegeError, match="row-level security"):
        async with tenant_tx(app_pool, caller(a_sub), a) as ctx:
            await ctx.conn.execute(
                "INSERT INTO crm.member (tenant_id, cognito_iss, cognito_sub, role) VALUES ($1, $2, 'x', 'agent')",
                b,
                ISS,
            )
    async with tenant_tx(app_pool, caller(a_sub), a) as ctx:
        moved = await ctx.conn.execute("UPDATE crm.member SET role = 'assistant' WHERE tenant_id = $1", b)
        assert moved == "UPDATE 0"


async def test_member_of_a_is_not_a_member_of_b(app_pool: asyncpg.Pool, two_agencies) -> None:  # noqa: ANN001
    (_a, a_sub), (b, _b_sub) = two_agencies
    with pytest.raises(Forbidden, match="not_a_member"):
        async with tenant_tx(app_pool, caller(a_sub), b):
            pass


async def test_inactive_member_and_locked_agency_are_refused(app_pool: asyncpg.Pool) -> None:
    tenant, sub = await _seed(app_pool, member_status="disabled")
    with pytest.raises(Forbidden, match="not_a_member"):
        async with tenant_tx(app_pool, caller(sub), tenant):
            pass
    tenant, sub = await _seed(app_pool)
    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
        await conn.execute("UPDATE crm.tenant SET status = 'locked'")
    with pytest.raises(Forbidden) as info:
        async with tenant_tx(app_pool, caller(sub), tenant):
            pass
    assert info.value.code == "tenant_locked"


async def test_context_is_local_to_the_transaction(app_pool: asyncpg.Pool, two_agencies) -> None:  # noqa: ANN001
    (a, a_sub), _ = two_agencies
    async with tenant_tx(app_pool, caller(a_sub), a) as ctx:
        conn = ctx.conn
        assert await conn.fetchval("SELECT current_setting('app.member_role')") == "owner"
        actor = await conn.fetchval("SELECT current_setting('app.actor')")
        assert actor == f"member:{ctx.member_id}"
        await conn.execute("UPDATE crm.member SET role = role WHERE member_id = $1", ctx.member_id)
    async with app_pool.acquire() as conn:
        assert await conn.fetchval("SELECT current_setting('app.tenant_id', true)") in (None, "")
        assert await conn.fetchval("SELECT count(*) FROM crm.member") == 0


async def test_created_by_and_updated_at_defaults(app_pool: asyncpg.Pool, two_agencies) -> None:  # noqa: ANN001
    (a, a_sub), _ = two_agencies
    async with tenant_tx(app_pool, caller(a_sub), a) as ctx:
        new_id = await ctx.conn.fetchval(
            "INSERT INTO crm.member (cognito_iss, cognito_sub, role) VALUES ($1, 'invitee', 'assistant') "
            "RETURNING member_id",
            ISS,
        )
        row = await ctx.conn.fetchrow(
            "SELECT tenant_id, created_by, updated_at FROM crm.member WHERE member_id = $1", new_id
        )
        assert row["tenant_id"] == a and row["created_by"] == f"member:{ctx.member_id}"
    async with tenant_tx(app_pool, caller(a_sub), a) as ctx:
        await ctx.conn.execute("UPDATE crm.member SET status = 'active' WHERE member_id = $1", new_id)
        after = await ctx.conn.fetchval("SELECT updated_at FROM crm.member WHERE member_id = $1", new_id)
    assert after > row["updated_at"]


async def test_status_moves_only_along_the_transition_table(app_pool: asyncpg.Pool, two_agencies) -> None:  # noqa: ANN001
    (a, a_sub), _ = two_agencies
    # A platform context (crm-008: inside a member request the last owner cannot change their own status at all).
    with pytest.raises(asyncpg.CheckViolationError, match="not an allowed transition"):
        async with provisioning_tx(app_pool, a, actor="seed:test") as ctx:
            await ctx.conn.execute("UPDATE crm.member SET status = 'invited' WHERE cognito_sub = $1", a_sub)
    with pytest.raises(asyncpg.CheckViolationError):
        async with provisioning_tx(app_pool, a, actor="seed:test") as ctx:
            await ctx.conn.execute("UPDATE crm.member SET status = 'gone' WHERE cognito_sub = $1", a_sub)


async def test_trash_columns_move_together(app_pool: asyncpg.Pool, two_agencies) -> None:  # noqa: ANN001
    (a, a_sub), _ = two_agencies
    with pytest.raises(asyncpg.CheckViolationError, match="member_trash_chk"):
        async with provisioning_tx(app_pool, a, actor="seed:test") as ctx:
            await ctx.conn.execute("UPDATE crm.member SET deleted_at = now() WHERE cognito_sub = $1", a_sub)
    async with tenant_tx(app_pool, caller(a_sub), a) as ctx:
        await ctx.conn.execute(
            "UPDATE crm.member SET deleted_at = now(), deleted_by = 'x', purge_after = now() + interval '7 days' "
            "WHERE cognito_sub = 'nobody'"
        )


async def test_reader_role_has_nothing_until_a_card_grants_it(app_pool: asyncpg.Pool, two_agencies) -> None:  # noqa: ANN001
    (a, a_sub), _ = two_agencies
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with tenant_tx(app_pool, caller(a_sub), a, reader_role="crm_ai_reader") as ctx:
            assert await ctx.conn.fetchval("SELECT current_user") == "crm_ai_reader"
            await ctx.conn.fetchval("SELECT count(*) FROM crm.member")


async def test_force_rls_binds_the_owner_too(admin: asyncpg.Connection, two_agencies) -> None:  # noqa: ANN001
    await admin.execute("SET ROLE crm_owner")
    try:
        assert await admin.fetchval("SELECT count(*) FROM crm.member") == 0
    finally:
        await admin.execute("RESET ROLE")
