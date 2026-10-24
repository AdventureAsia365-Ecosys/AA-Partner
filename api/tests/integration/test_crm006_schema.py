"""crm-006 (AA-937, card CRM-09) on PostgreSQL 15 with the RDS role shape: the F1 / F2 schema rules.

- **R** (card): ``crm_ai_reader``, ``crm_insights`` and ``crm_support_reader`` selecting ``crm.trip_traveler`` -> 42501
  (Fix A1), and the same for every F1 table; ``crm_ai_reader`` / ``crm_insights`` read their views only.
- Done when (lane draft): F2 queries need no decrypt: the reader views hold bands and codes only and the readers
  cannot reach the DEK store.
- R7: a relationship writes its inverse, trash and delete follow it, the kind is fixed.
- Health codes only with a health_data consent; documents: write-only passports, keys without names.
- ``audit_event.object_ids`` (list views) holds ids only.
"""

from __future__ import annotations

import uuid

import asyncpg
import pytest

from app.auth.route_guard import Caller
from app.tenancy import tenant_tx
from tests.integration.conftest import READERS

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ISS = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_TESTPARTN"
BLOB = b"\x00\x01" + b"\x07" * 40  # shaped like a sealed value (header + nonce + tag); never opened here
F1_TABLES = ("traveler", "traveler_relation", "traveler_health", "document", "trip_traveler", "note", "consent")
AI_VIEWS = ("v_ai_traveler_profile", "v_ai_trip_party", "v_ai_trip_context")


def caller(sub: str) -> Caller:
    return Caller("agency", "partner", "partner-web", ISS, sub, ())


async def _agency(pool: asyncpg.Pool) -> tuple[uuid.UUID, Caller]:
    tenant_id, sub = uuid.uuid4(), f"sub-{uuid.uuid4()}"
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        await conn.execute("INSERT INTO crm.tenant (tenant_id) VALUES ($1)", tenant_id)
        member = await conn.fetchval(
            "INSERT INTO crm.member (cognito_iss, cognito_sub, role, status) VALUES ($1, $2, 'owner', 'invited') "
            "RETURNING member_id",
            ISS,
            sub,
        )
        await conn.execute("UPDATE crm.member SET status = 'active' WHERE member_id = $1", member)
    return tenant_id, caller(sub)


async def _traveler(conn: asyncpg.Connection) -> uuid.UUID:
    return await conn.fetchval("INSERT INTO crm.traveler (display_name_enc) VALUES ($1) RETURNING traveler_id", BLOB)


async def _trip_with_party(conn: asyncpg.Connection) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    traveler = await _traveler(conn)
    token = await conn.fetchval(
        "INSERT INTO crm.traveler_profile (budget_band, fitness_band, dietary_flags) "
        "VALUES ('2000_3500', '4_6h', ARRAY['vegetarian']) RETURNING traveler_token"
    )
    trip = await conn.fetchval(
        "INSERT INTO crm.trip (title, destinations, start_date) VALUES ('Vietnam, 9 nights', ARRAY['vietnam', 'laos'], "
        "DATE '2027-03-12') RETURNING trip_id"
    )
    await conn.execute(
        "INSERT INTO crm.trip_traveler (trip_id, traveler_id, role) VALUES ($1, $2, 'lead')", trip, traveler
    )
    await conn.execute(
        "INSERT INTO crm.trip_party (trip_id, traveler_token, role, age_band_at_departure) "
        "VALUES ($1, $2, 'lead', '35_44')",
        trip,
        token,
    )
    return trip, traveler, token


async def test_crm006_is_registered(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-006'") == 1


async def test_r_readers_selecting_trip_traveler_and_f1_tables_get_42501(app_pool: asyncpg.Pool) -> None:
    tenant, who = await _agency(app_pool)
    async with tenant_tx(app_pool, who, tenant) as ctx:
        await _trip_with_party(ctx.conn)
    for reader in READERS:
        for table in F1_TABLES:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                async with tenant_tx(app_pool, who, tenant, reader_role=reader) as ctx:
                    await ctx.conn.fetchval(f"SELECT count(*) FROM crm.{table}")  # noqa: S608 — fixed names


async def test_ai_and_insight_readers_see_views_only(app_pool: asyncpg.Pool) -> None:
    tenant, who = await _agency(app_pool)
    async with tenant_tx(app_pool, who, tenant) as ctx:
        trip, _, token = await _trip_with_party(ctx.conn)
    async with tenant_tx(app_pool, who, tenant, reader_role="crm_ai_reader") as ctx:
        profile = await ctx.conn.fetchrow("SELECT * FROM crm.v_ai_traveler_profile")
        party = await ctx.conn.fetchrow("SELECT * FROM crm.v_ai_trip_party")
        context = await ctx.conn.fetchrow("SELECT * FROM crm.v_ai_trip_context")
    assert profile["traveler_token"] == token and profile["fitness_band"] == "4_6h"
    assert list(profile["dietary_flags"]) == ["vegetarian"]
    assert dict(party) == {"trip_id": trip, "traveler_token": token, "role": "lead", "age_band_at_departure": "35_44"}
    assert context["trip_id"] == trip and "title" not in context.keys()
    # Base tables (F2 included) and the other reader's view are refused.
    for reader, table in (
        ("crm_ai_reader", "traveler_profile"),
        ("crm_ai_reader", "trip"),
        ("crm_ai_reader", "trip_party"),
        ("crm_ai_reader", "v_insight_fact"),
        ("crm_ai_reader", "tenant_key"),
        ("crm_insights", "v_ai_traveler_profile"),
        ("crm_insights", "trip"),
        ("crm_insights", "tenant_key"),
        ("crm_support_reader", "v_ai_trip_party"),
        ("crm_eraser", "v_insight_fact"),
    ):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with tenant_tx(app_pool, who, tenant, reader_role=reader) as ctx:
                await ctx.conn.fetchval(f"SELECT count(*) FROM crm.{table}")  # noqa: S608 — fixed names
    async with tenant_tx(app_pool, who, tenant, reader_role="crm_insights") as ctx:
        facts = await ctx.conn.fetch("SELECT * FROM crm.v_insight_fact ORDER BY destination")
    assert [(f["destination"], f["age_band"], f["stage_reached"]) for f in facts] == [
        ("laos", "35_44", "inquiry"),
        ("vietnam", "35_44", "inquiry"),
    ]
    assert "trip_id" not in facts[0].keys() and "traveler_token" not in facts[0].keys()


async def test_reader_views_show_one_agency_only(app_pool: asyncpg.Pool) -> None:
    (a, who_a), (b, who_b) = await _agency(app_pool), await _agency(app_pool)
    async with tenant_tx(app_pool, who_a, a) as ctx:
        _, _, token_a = await _trip_with_party(ctx.conn)
    async with tenant_tx(app_pool, who_b, b) as ctx:
        await _trip_with_party(ctx.conn)
    for view in AI_VIEWS:
        async with tenant_tx(app_pool, who_a, a, reader_role="crm_ai_reader") as ctx:
            assert await ctx.conn.fetchval(f"SELECT count(*) FROM crm.{view}") == 1  # noqa: S608
    async with tenant_tx(app_pool, who_a, a, reader_role="crm_ai_reader") as ctx:
        assert await ctx.conn.fetchval("SELECT traveler_token FROM crm.v_ai_traveler_profile") == token_a
    # With no agency in the context a view shows nothing (RLS binds the view owner too).
    async with app_pool.acquire() as conn, conn.transaction():
        assert await conn.fetchval("SELECT count(*) FROM crm.v_ai_traveler_profile") == 0
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with tenant_tx(app_pool, who_a, a) as ctx:
            await ctx.conn.execute("DELETE FROM crm.v_ai_trip_party")


async def test_relationship_writes_its_inverse_and_keeps_it(app_pool: asyncpg.Pool) -> None:
    tenant, who = await _agency(app_pool)
    async with tenant_tx(app_pool, who, tenant) as ctx:
        a, l_, s = await _traveler(ctx.conn), await _traveler(ctx.conn), await _traveler(ctx.conn)
        rel = await ctx.conn.fetchval(
            "INSERT INTO crm.traveler_relation (from_traveler_id, to_traveler_id, kind) VALUES ($1, $2, 'child') "
            "RETURNING relation_id",
            a,
            l_,
        )
        rows = await ctx.conn.fetch(
            "SELECT relation_id, from_traveler_id, to_traveler_id, kind, inverse_id FROM crm.traveler_relation "
            "ORDER BY kind"
        )
        assert [(r["from_traveler_id"], r["to_traveler_id"], r["kind"]) for r in rows] == [
            (a, l_, "child"),
            (l_, a, "parent"),
        ]
        by_id = {r["relation_id"]: r for r in rows}
        assert by_id[rel]["inverse_id"] in by_id and by_id[by_id[rel]["inverse_id"]]["inverse_id"] == rel
        await ctx.conn.execute(
            "INSERT INTO crm.traveler_relation (from_traveler_id, to_traveler_id, kind) VALUES ($1, $2, 'referrer')",
            a,
            s,
        )
        assert (
            await ctx.conn.fetchval(
                "SELECT kind FROM crm.traveler_relation WHERE from_traveler_id = $1 AND to_traveler_id = $2", s, a
            )
            == "referred"
        )
        # Trash and hard delete follow the inverse.
        await ctx.conn.execute(
            "UPDATE crm.traveler_relation SET deleted_at = now(), deleted_by = 'member:x', "
            "purge_after = now() + interval '7 days' WHERE relation_id = $1",
            rel,
        )
        assert (
            await ctx.conn.fetchval(
                "SELECT count(*) FROM crm.traveler_relation "
                "WHERE deleted_at IS NOT NULL AND kind IN ('child', 'parent')"
            )
            == 2
        )
        await ctx.conn.execute("DELETE FROM crm.traveler_relation WHERE relation_id = $1", rel)
        assert (
            await ctx.conn.fetchval("SELECT count(*) FROM crm.traveler_relation WHERE kind IN ('child', 'parent')") == 0
        )
    for sql, args, error in (
        ("UPDATE crm.traveler_relation SET kind = 'friend' WHERE kind = 'referrer'", (), asyncpg.CheckViolationError),
        (
            "INSERT INTO crm.traveler_relation (from_traveler_id, to_traveler_id, kind) VALUES ($1, $2, 'friend')",
            (s, a),
            asyncpg.UniqueViolationError,
        ),
        (
            "INSERT INTO crm.traveler_relation (from_traveler_id, to_traveler_id, kind) VALUES ($1, $1, 'friend')",
            (a,),
            asyncpg.CheckViolationError,
        ),
        (
            "INSERT INTO crm.traveler_relation (from_traveler_id, to_traveler_id, kind) VALUES ($1, $2, 'referrer')",
            (a, l_),
            asyncpg.UniqueViolationError,
        ),
        (
            "INSERT INTO crm.traveler_relation (from_traveler_id, to_traveler_id, kind) VALUES ($1, $2, 'cousin')",
            (l_, s),
            asyncpg.CheckViolationError,
        ),
    ):
        with pytest.raises(error):
            async with tenant_tx(app_pool, who, tenant) as ctx:
                await ctx.conn.execute(sql, *args)


async def test_health_codes_need_a_health_data_consent(app_pool: asyncpg.Pool) -> None:
    tenant, who = await _agency(app_pool)
    async with tenant_tx(app_pool, who, tenant) as ctx:
        traveler, other = await _traveler(ctx.conn), await _traveler(ctx.conn)
        marketing = await ctx.conn.fetchval(
            "INSERT INTO crm.consent (traveler_id, purpose, value, source) "
            "VALUES ($1, 'marketing', true, 'agent_recorded') "
            "RETURNING consent_id",
            traveler,
        )
        refused = await ctx.conn.fetchval(
            "INSERT INTO crm.consent (traveler_id, purpose, value, source) VALUES ($1, 'health_data', false, "
            "'agent_recorded') RETURNING consent_id",
            traveler,
        )
        health = await ctx.conn.fetchval(
            "INSERT INTO crm.consent (traveler_id, purpose, value, source) VALUES ($1, 'health_data', true, "
            "'agent_recorded') RETURNING consent_id",
            traveler,
        )
    insert = "INSERT INTO crm.traveler_health (traveler_id, health_flags_enc, consent_id) VALUES ($1, $2, $3)"
    for who_id, consent in ((traveler, marketing), (traveler, refused), (other, health)):
        with pytest.raises(asyncpg.CheckViolationError):
            async with tenant_tx(app_pool, who, tenant) as ctx:
                await ctx.conn.execute(insert, who_id, BLOB, consent)
    async with tenant_tx(app_pool, who, tenant) as ctx:
        await ctx.conn.execute(insert, traveler, BLOB, health)
        with pytest.raises(asyncpg.UniqueViolationError):
            async with ctx.conn.transaction():
                await ctx.conn.execute(insert, traveler, BLOB, health)


async def test_document_rules(app_pool: asyncpg.Pool) -> None:
    tenant, who = await _agency(app_pool)
    async with tenant_tx(app_pool, who, tenant) as ctx:
        traveler = await _traveler(ctx.conn)

    def key(doc: uuid.UUID) -> str:
        return f"tenants/{tenant}/documents/{doc}"

    sql = (
        "INSERT INTO crm.document (document_id, traveler_id, kind, s3_key, sha256, size_bytes, content_type, "
        "filename_enc, write_only, visible_to_travelers) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)"
    )
    good = uuid.uuid4()
    async with tenant_tx(app_pool, who, tenant) as ctx:
        await ctx.conn.execute(
            sql, good, traveler, "passport_scan", key(good), b"\x01" * 32, 1000, "image/jpeg", BLOB, True, False
        )
    for kind, s3_key, write_only, visible, ctype, size in (
        ("passport_scan", None, False, False, "image/jpeg", 10),  # a passport is always write-only
        ("ticket", None, True, True, "application/pdf", 10),  # write-only is never visible to travellers
        ("ticket", f"tenants/{tenant}/documents/passport-mai.pdf", False, False, "application/pdf", 10),
        ("ticket", f"tenants/{tenant}/documents/{good}", False, False, "application/pdf", 10),  # another doc's key
        ("ticket", f"tenants/{uuid.uuid4()}/documents/{{doc}}", False, False, "application/pdf", 10),  # other agency
        ("ticket", None, False, False, "text/html", 10),
        ("ticket", None, False, False, "application/pdf", 11 * 1024 * 1024),
        ("selfie", None, False, False, "image/png", 10),
    ):
        doc = uuid.uuid4()
        with pytest.raises(asyncpg.CheckViolationError):
            async with tenant_tx(app_pool, who, tenant) as ctx:
                await ctx.conn.execute(
                    sql,
                    doc,
                    traveler,
                    kind,
                    (s3_key or key(doc)).replace("{doc}", str(doc)),
                    b"\x01" * 32,
                    size,
                    ctype,
                    BLOB,
                    write_only,
                    visible,
                )


async def test_new_references_cannot_cross_agencies(app_pool: asyncpg.Pool) -> None:
    """Referential checks bypass FORCE RLS, so a single-column key would let agency B point at agency A's traveller,
    trip or profile. crm-006 keys every new reference on (tenant_id, id): the database refuses it (23503)."""
    (a, who_a), (b, who_b) = await _agency(app_pool), await _agency(app_pool)
    async with tenant_tx(app_pool, who_a, a) as ctx:
        trip_a, traveler_a, token_a = await _trip_with_party(ctx.conn)
    async with tenant_tx(app_pool, who_b, b) as ctx:
        traveler_b = await _traveler(ctx.conn)
        consent_b = await ctx.conn.fetchval(
            "INSERT INTO crm.consent (traveler_id, purpose, value, source) VALUES ($1, 'health_data', true, "
            "'agent_recorded') RETURNING consent_id",
            traveler_b,
        )
    doc = uuid.uuid4()
    attempts = (
        (
            "INSERT INTO crm.traveler_relation (from_traveler_id, to_traveler_id, kind) VALUES ($1, $2, 'friend')",
            (traveler_b, traveler_a),
        ),
        (
            "INSERT INTO crm.traveler_relation (from_traveler_id, to_traveler_id, kind) VALUES ($1, $2, 'friend')",
            (traveler_a, traveler_b),
        ),
        (
            "INSERT INTO crm.document (document_id, traveler_id, kind, s3_key, sha256, size_bytes, content_type, "
            "filename_enc) VALUES ($1, $2, 'ticket', $3, $4, 10, 'application/pdf', $5)",
            (doc, traveler_a, f"tenants/{b}/documents/{doc}", b"\x01" * 32, BLOB),
        ),
        (
            "INSERT INTO crm.document (document_id, trip_id, kind, s3_key, sha256, size_bytes, content_type, "
            "filename_enc) VALUES ($1, $2, 'ticket', $3, $4, 10, 'application/pdf', $5)",
            (doc, trip_a, f"tenants/{b}/documents/{doc}", b"\x01" * 32, BLOB),
        ),
        (
            "INSERT INTO crm.profile_history (traveler_token, stage_reached, year_month) "
            "VALUES ($1, 'booked', DATE '2027-03-01')",
            (token_a,),
        ),
    )
    for sql, args in attempts:
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            async with tenant_tx(app_pool, who_b, b) as ctx:
                await ctx.conn.execute(sql, *args)
    # traveler_health: the consent trigger (RLS-bound) or the composite key refuses agency A's traveller.
    with pytest.raises((asyncpg.ForeignKeyViolationError, asyncpg.CheckViolationError)):
        async with tenant_tx(app_pool, who_b, b) as ctx:
            await ctx.conn.execute(
                "INSERT INTO crm.traveler_health (traveler_id, health_flags_enc, consent_id) VALUES ($1, $2, $3)",
                traveler_a,
                BLOB,
                consent_b,
            )
    # The same rows inside one agency are fine.
    async with tenant_tx(app_pool, who_b, b) as ctx:
        other_b = await _traveler(ctx.conn)
        await ctx.conn.execute(attempts[0][0], traveler_b, other_b)


async def test_age_band_list_and_list_view_ids(app_pool: asyncpg.Pool) -> None:
    tenant, who = await _agency(app_pool)
    async with tenant_tx(app_pool, who, tenant) as ctx:
        trip, _, token = await _trip_with_party(ctx.conn)
    with pytest.raises(asyncpg.CheckViolationError):
        async with tenant_tx(app_pool, who, tenant) as ctx:
            await ctx.conn.execute(
                "UPDATE crm.trip_party SET age_band_at_departure = 'thirties' WHERE trip_id = $1", trip
            )
    insert = (
        "INSERT INTO crm.audit_event (actor_kind, actor_ref, action, outcome, object_kind, fields, object_ids) "
        "VALUES ('user', 'member:x', 'list_view', 'allowed', 'traveler', ARRAY['display_name'], $1)"
    )
    async with tenant_tx(app_pool, who, tenant) as ctx:
        await ctx.conn.execute(insert, [str(token), str(trip)])
    for bad in (["Anna Martin"], [str(uuid.uuid4())] * 201, [None]):
        with pytest.raises(asyncpg.CheckViolationError):
            async with tenant_tx(app_pool, who, tenant) as ctx:
                await ctx.conn.execute(insert, bad)


async def test_profile_codes_are_checked(app_pool: asyncpg.Pool) -> None:
    tenant, who = await _agency(app_pool)
    for column, value in (
        ("fitness_band", "marathon"),
        ("residence_country", "Vietnam"),
        ("dietary_flags", ["vegetarian", "loves pho"]),
        ("mobility_flags", ["rollerblades"]),
        ("interests", ["Food & wine"]),
    ):
        with pytest.raises(asyncpg.CheckViolationError):
            async with tenant_tx(app_pool, who, tenant) as ctx:
                await ctx.conn.execute(f"INSERT INTO crm.traveler_profile ({column}) VALUES ($1)", value)  # noqa: S608
    async with tenant_tx(app_pool, who, tenant) as ctx:
        token = await ctx.conn.fetchval(
            "INSERT INTO crm.traveler_profile (residence_country, interests, occasion) VALUES ('AU', ARRAY['food', "
            "'trekking'], 'anniversary') RETURNING traveler_token"
        )
        await ctx.conn.execute(
            "INSERT INTO crm.profile_history (traveler_token, aa_departure_id, stage_reached, year_month) "
            "VALUES ($1, 'dep-2025-10', 'completed', DATE '2025-10-01')",
            token,
        )
