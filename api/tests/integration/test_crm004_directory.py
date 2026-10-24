"""crm-004 on PostgreSQL 15 (AA-935, card CRM-07): agency label + locale, the caller-scoped membership lookup, agency
provisioning (R test: a non-synthetic agency on Dev -> 422, no row) and the seeder end to end (E test: a Vietnamese
name round-trips byte for byte; a second run changes nothing).
"""

from __future__ import annotations

import io
import json
import unicodedata
import uuid

import asyncpg
import pytest

from app import seed
from app.agencies import AgencySpec, MemberSpec, provision_agency
from app.auth.route_guard import Caller
from app.errors import Invalid
from app.seed.catalogue import AGENCIES, BY_KEY
from app.tenancy import caller_memberships, provisioning_tx, tenant_tx
from tests.integration.conftest import APP_ROLE
from tests.tokens import LocalPools

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ISS = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_TESTEXTRN"
SEN_VANG = "Lữ hành Sen Vàng"


def caller(sub: str) -> Caller:
    return Caller("agency", "external", "cis-portal", ISS, sub, ())


async def _agency(pool: asyncpg.Pool, name: str, *members: tuple[str, str], locale: str = "en-AU") -> uuid.UUID:
    tenant_id = uuid.uuid4()
    await provision_agency(
        pool,
        AgencySpec(tenant_id, name, locale),
        tuple(MemberSpec(ISS, sub, role) for sub, role in members),
        environment="test",
        actor="seed:it",
    )
    return tenant_id


async def test_crm004_is_registered_with_its_columns_and_policies(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-004'") == 1
    cols = {
        r["attname"]: (r["typ"], r["attnotnull"])
        for r in await admin.fetch(
            "SELECT attname, format_type(atttypid, atttypmod) AS typ, attnotnull FROM pg_attribute "
            "WHERE attrelid = 'crm.tenant'::regclass AND attnum > 0 AND NOT attisdropped"
        )
    }
    assert cols["display_name"] == ("text", True) and cols["default_locale"] == ("text", True)
    policies = {
        r["polname"]: (r["polcmd"], r["roles"])
        for r in await admin.fetch(
            "SELECT polname, polcmd::text AS polcmd, "
            "ARRAY(SELECT r::regrole::text FROM unnest(polroles) r) AS roles FROM pg_policy "
            "WHERE polname IN ('member_self_lookup', 'tenant_member_lookup')"
        )
    }
    assert policies == {"member_self_lookup": ("r", [APP_ROLE]), "tenant_member_lookup": ("r", [APP_ROLE])}


async def test_constraints_refuse_bad_labels_and_locales(app_pool: asyncpg.Pool) -> None:
    for name, locale in [(" padded", "en-AU"), ("", "en-AU"), ("ok", "fr-FR")]:
        tid = uuid.uuid4()
        with pytest.raises(asyncpg.CheckViolationError):
            async with provisioning_tx(app_pool, tid, actor="seed:it") as ctx:
                await ctx.conn.execute(
                    "INSERT INTO crm.tenant (tenant_id, display_name, default_locale) VALUES ($1, $2, $3)",
                    tid,
                    name,
                    locale,
                )


async def test_caller_sees_only_their_own_active_memberships(app_pool: asyncpg.Pool) -> None:
    me, other = f"me-{uuid.uuid4()}", f"other-{uuid.uuid4()}"
    a = await _agency(app_pool, "Agency A", (me, "owner"), (other, "agent"))
    b = await _agency(app_pool, SEN_VANG, (me, "assistant"), locale="vi-VN")
    c = await _agency(app_pool, "Agency C", (other, "owner"))
    d = await _agency(app_pool, "Agency D", (me, "agent"))
    async with tenant_tx(app_pool, caller(me), d) as ctx:
        await ctx.conn.execute("UPDATE crm.member SET status = 'disabled' WHERE cognito_sub = $1", me)
    found = await caller_memberships(app_pool, caller(me))
    assert {(m.tenant_id, m.member_role) for m in found} == {(a, "owner"), (b, "assistant")}
    assert {m.display_name for m in found} == {"Agency A", SEN_VANG}
    assert next(m for m in found if m.tenant_id == b).default_locale == "vi-VN"
    assert {m.tenant_id for m in await caller_memberships(app_pool, caller(other))} == {a, c}
    assert await caller_memberships(app_pool, caller(f"nobody-{uuid.uuid4()}")) == []


async def test_a_locked_agency_is_listed_with_its_status(app_pool: asyncpg.Pool) -> None:
    me = f"me-{uuid.uuid4()}"
    tid = await _agency(app_pool, "Locked Agency", (me, "owner"))
    async with provisioning_tx(app_pool, tid, actor="seed:it") as ctx:
        await ctx.conn.execute("UPDATE crm.tenant SET status = 'locked' WHERE tenant_id = $1", tid)
    [m] = await caller_memberships(app_pool, caller(me))
    assert (m.tenant_id, m.tenant_status) == (tid, "locked")


async def test_the_lookup_context_grants_no_write_and_no_foreign_rows(app_pool: asyncpg.Pool) -> None:
    me, other = f"me-{uuid.uuid4()}", f"other-{uuid.uuid4()}"
    a = await _agency(app_pool, "Agency A", (me, "owner"), (other, "agent"))
    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.caller_iss', $1, true)", ISS)
        await conn.execute("SELECT set_config('app.caller_sub', $1, true)", me)
        # Only the caller's own row of agency A, never the colleague's.
        subs = {
            r["cognito_sub"] for r in await conn.fetch("SELECT cognito_sub FROM crm.member WHERE tenant_id = $1", a)
        }
        assert subs == {me}
        assert await conn.execute("UPDATE crm.member SET role = 'assistant' WHERE tenant_id = $1", a) == "UPDATE 0"
        assert await conn.execute("UPDATE crm.tenant SET display_name = 'x' WHERE tenant_id = $1", a) == "UPDATE 0"
        assert await conn.execute("DELETE FROM crm.member WHERE tenant_id = $1", a) == "DELETE 0"
    with pytest.raises(asyncpg.InsufficientPrivilegeError, match="row-level security"):
        async with app_pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT set_config('app.caller_iss', $1, true)", ISS)
            await conn.execute("SELECT set_config('app.caller_sub', $1, true)", me)
            await conn.execute(
                "INSERT INTO crm.member (tenant_id, cognito_iss, cognito_sub, role) VALUES ($1, $2, 'x', 'owner')",
                a,
                ISS,
            )


async def test_inside_tenant_tx_the_lookup_policies_stay_off(app_pool: asyncpg.Pool) -> None:
    me = f"me-{uuid.uuid4()}"
    a = await _agency(app_pool, "Agency A", (me, "owner"))
    b = await _agency(app_pool, "Agency B", (me, "owner"))
    async with tenant_tx(app_pool, caller(me), a) as ctx:
        assert {r["tenant_id"] for r in await ctx.conn.fetch("SELECT tenant_id FROM crm.member")} == {a}
        assert await ctx.conn.fetchval("SELECT count(*) FROM crm.tenant WHERE tenant_id = $1", b) == 0


async def test_dev_refuses_a_non_synthetic_agency_and_writes_nothing(app_pool: asyncpg.Pool) -> None:
    tid = uuid.uuid4()
    with pytest.raises(Invalid) as info:
        await provision_agency(
            app_pool, AgencySpec(tid, "Real Agency", is_synthetic=False), environment="dev", actor="seed:it"
        )
    assert (info.value.status_code, info.value.code) == (422, "synthetic_required")
    async with provisioning_tx(app_pool, tid, actor="seed:it") as ctx:
        assert await ctx.conn.fetchval("SELECT count(*) FROM crm.tenant WHERE tenant_id = $1", tid) == 0


async def test_vietnamese_label_round_trips_and_provisioning_converges(app_pool: asyncpg.Pool) -> None:
    tid, me = uuid.uuid4(), f"me-{uuid.uuid4()}"
    decomposed = unicodedata.normalize("NFD", SEN_VANG)
    first = await provision_agency(
        app_pool,
        AgencySpec(tid, decomposed, "vi-VN"),
        (MemberSpec(ISS, me, "agent"),),
        environment="dev",
        actor="seed:it",
    )
    again = await provision_agency(
        app_pool,
        AgencySpec(tid, SEN_VANG, "vi-VN"),
        (MemberSpec(ISS, me, "owner"),),
        environment="dev",
        actor="seed:it",
    )
    assert (first.created, first.members_added) == (True, 1)
    assert (again.created, again.updated, again.members_added, again.members_updated) == (False, False, 0, 1)
    async with tenant_tx(app_pool, caller(me), tid) as ctx:
        stored = await ctx.conn.fetchval("SELECT display_name FROM crm.tenant WHERE tenant_id = $1", tid)
        assert ctx.member_role == "owner"
    assert stored == SEN_VANG and stored.encode("utf-8") == SEN_VANG.encode("utf-8")


async def test_seeder_end_to_end_is_idempotent(migrated_app_dsn: str) -> None:
    settings = LocalPools().settings(
        database_url=migrated_app_dsn, cognito_external_issuer=ISS, cognito_partner_issuer=""
    )
    owner = f"seed-owner-{uuid.uuid4()}"
    argv = ["--apply", "--member", f"sen-vang:owner:{owner}"]
    out = io.StringIO()
    assert await seed.main(argv, out, settings) == 0
    rows = [json.loads(x) for x in out.getvalue().splitlines()]
    by_key = {r["agency"]: r for r in rows[:-1]}
    assert set(by_key) == {a.key for a in AGENCIES}
    assert all(r["name_round_trip"] for r in by_key.values())
    out2 = io.StringIO()
    assert await seed.main(argv, out2, settings) == 0
    rows2 = [json.loads(x) for x in out2.getvalue().splitlines()]
    assert {r["action"] for r in rows2[:-1]} == {"unchanged"}
    assert rows2[-1]["members_added"] == 0
    dry = io.StringIO()
    assert await seed.main([], dry, settings) == 0
    assert {json.loads(x).get("action") for x in dry.getvalue().splitlines()[:-1]} == {"unchanged"}
    pool = await asyncpg.create_pool(migrated_app_dsn, min_size=0, max_size=1)
    try:
        found = await caller_memberships(pool, caller(owner))
    finally:
        await pool.close()
    assert [(m.tenant_id, m.member_role, m.display_name) for m in found] == [
        (BY_KEY["sen-vang"].tenant_id, "owner", SEN_VANG)
    ]
