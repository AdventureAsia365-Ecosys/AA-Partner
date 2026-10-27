"""crm-022 (AA-951, card CRM-22) on PostgreSQL with the RDS role shape: the AA content cache, the day 0 packs of a
proposal version and the agency's message templates.

- ``crm.aa_content`` (platform cache): crm_app reads, inserts and updates, never deletes; the contract's shapes.
- ``crm.prep_pack_selection``: changes only while its proposal is a draft (CRM35: a published or sent version keeps
  its packs); FORCE RLS (another agency sees nothing, cannot hang a pack on a foreign version); the trip and agency
  erase take it; a trip hold guards it (CRM10).
- ``crm.message_template``: one AA starter copy per group and locale; the agency erase takes it.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from tests.integration.conftest import OWNER, READERS
from tests.integration.test_crm010_schema import _agency, _count, _ctx, _erase, _hold
from tests.integration.test_crm019_schema import _builder, _publish, _run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "022_crm_text_proposal_prep_packs.sql"
PACK_SQL = (
    "INSERT INTO crm.prep_pack_selection (proposal_id, trip_id, group_key, locale, content_id, content_version, title, "
    "body_markdown, applies_to, content_hash) VALUES ($1, $2, $3, 'en', $4, 1, 'Before you go: Nepal', "
    "'## Entry and visa', '{\"country_codes\": [\"NP\"]}', 'sha256:x') RETURNING prep_pack_selection_id"
)


async def test_crm022_is_registered_and_runs_again_as_a_no_op(admin: asyncpg.Connection) -> None:
    # The crm-021 tests re-apply their own erase_subject text: put this key's back first.
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-022'") == 1
    await admin.execute(f"SET ROLE {OWNER}")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")
    src = await admin.fetchval(
        "SELECT prosrc FROM pg_proc WHERE oid = 'crm.erase_subject(text,uuid,uuid,boolean)'::regprocedure"
    )
    assert "'prep_pack_selection'" in src and "'message_template'" in src and "'metric_event'" in src


async def test_privileges(admin: asyncpg.Connection) -> None:
    for privilege, want in (("SELECT", True), ("INSERT", True), ("UPDATE", True), ("DELETE", False),
                            ("TRUNCATE", False)):  # fmt: skip
        assert await admin.fetchval("SELECT has_table_privilege('crm_app', 'crm.aa_content', $1)", privilege) is want
    for table in ("crm.prep_pack_selection", "crm.message_template"):
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert await admin.fetchval("SELECT has_table_privilege('crm_app', $1, $2)", table, privilege)
        assert not await admin.fetchval("SELECT has_table_privilege('crm_app', $1, 'TRUNCATE')", table)
        for reader in READERS:
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                assert not await admin.fetchval("SELECT has_table_privilege($1, $2, $3)", reader, table, privilege)


async def test_the_content_cache_takes_the_contract_shapes(app_pool: asyncpg.Pool) -> None:
    cid = uuid.uuid4()
    sql = (
        "INSERT INTO crm.aa_content (content_id, kind, locale, group_key, version, title, body_markdown, applies_to, "
        "content_hash, published_at) VALUES ($1, $2, $3, $4, 1, 'T', 'B', $5::jsonb, 'sha256:a', now())"
    )
    async with app_pool.acquire() as conn:
        await conn.execute(sql, cid, "prep_pack", "en", "prep-pack-nepal", json.dumps({"country_codes": ["NP"]}))
        await conn.execute("UPDATE crm.aa_content SET listed = false WHERE content_id = $1", cid)
        for kind, locale, group, applies in (
            ("other", "en", "g-1", "{}"),
            ("prep_pack", "fr", "g-1", "{}"),
            ("prep_pack", "en", "Bad Key", "{}"),
            ("prep_pack", "en", "g-1", "[]"),
        ):
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(sql, uuid.uuid4(), kind, locale, group, applies)
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("DELETE FROM crm.aa_content WHERE content_id = $1", cid)


async def test_packs_change_only_in_a_draft(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    b = await _builder(app_pool, ids)
    p = b["proposal"]
    pack = await _run(app_pool, ids["tenant"], PACK_SQL, p, ids["trip"], "prep-pack-nepal", uuid.uuid4())
    await _run(app_pool, ids["tenant"], "UPDATE crm.prep_pack_selection SET sort_order = 1 WHERE "
               "prep_pack_selection_id = $1", pack)  # fmt: skip
    with pytest.raises(asyncpg.UniqueViolationError):  # one copy per group and locale
        await _run(app_pool, ids["tenant"], PACK_SQL, p, ids["trip"], "prep-pack-nepal", uuid.uuid4())
    await _publish(app_pool, ids, p)
    for sql, args in (
        (PACK_SQL, (p, ids["trip"], "prep-pack-bhutan", uuid.uuid4())),
        ("UPDATE crm.prep_pack_selection SET sort_order = 2 WHERE prep_pack_selection_id = $1", (pack,)),
        ("DELETE FROM crm.prep_pack_selection WHERE prep_pack_selection_id = $1", (pack,)),
    ):
        with pytest.raises(asyncpg.PostgresError) as err:
            await _run(app_pool, ids["tenant"], sql, *args)
        assert err.value.sqlstate == "CRM35"


async def test_another_agency_sees_no_pack_and_cannot_attach_one(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    other = await _agency(app_pool)
    b = await _builder(app_pool, ids)
    await _run(app_pool, ids["tenant"], PACK_SQL, b["proposal"], ids["trip"], "prep-pack-nepal", uuid.uuid4())
    assert await _count(app_pool, ids["tenant"], "prep_pack_selection") == 1
    assert await _count(app_pool, other["tenant"], "prep_pack_selection") == 0
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await _run(app_pool, other["tenant"], PACK_SQL, b["proposal"], ids["trip"], "prep-pack-nepal", uuid.uuid4())


async def _template(pool: asyncpg.Pool, tenant: uuid.UUID, group: str | None, locale: str = "en") -> Any:
    return await _run(
        pool, tenant,
        "INSERT INTO crm.message_template (locale, title, body_enc, source, aa_group_key, aa_content_id, aa_version) "
        "VALUES ($1, 'Welcome', $2, $3, $4, $5, $6) "
        "ON CONFLICT (tenant_id, aa_group_key, locale) WHERE aa_group_key IS NOT NULL DO NOTHING "
        "RETURNING message_template_id",
        locale, os.urandom(40), "aa_starter" if group else "agency", group, uuid.uuid4() if group else None,
        1 if group else None,
    )  # fmt: skip


async def test_templates_one_aa_copy_per_group_and_locale(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    t = ids["tenant"]
    assert await _template(app_pool, t, "template-welcome-message") is not None
    assert await _template(app_pool, t, "template-welcome-message") is None  # the import converges
    assert await _template(app_pool, t, "template-welcome-message", "vi") is not None
    assert await _template(app_pool, t, None) is not None and await _template(app_pool, t, None) is not None
    with pytest.raises(asyncpg.CheckViolationError):  # an agency template names no AA source
        await _run(app_pool, t, "INSERT INTO crm.message_template (locale, title, body_enc, source, aa_group_key) "
                   "VALUES ('en', 'x', $1, 'agency', 'g-1')", os.urandom(40))  # fmt: skip
    with pytest.raises(asyncpg.CheckViolationError):  # an AA copy names its source
        await _run(app_pool, t, "INSERT INTO crm.message_template (locale, title, body_enc, source) "
                   "VALUES ('en', 'x', $1, 'aa_starter')", os.urandom(40))  # fmt: skip
    other = await _agency(app_pool)
    assert await _count(app_pool, other["tenant"], "message_template") == 0
    assert await _count(app_pool, t, "message_template") == 4


async def test_the_trip_and_agency_erase_take_packs_and_templates(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    b = await _builder(app_pool, ids)
    await _run(app_pool, ids["tenant"], PACK_SQL, b["proposal"], ids["trip"], "prep-pack-nepal", uuid.uuid4())
    await _publish(app_pool, ids, b["proposal"])  # a published version's packs are erased too (app.erasing)
    done = await _erase(app_pool, ids["tenant"], "trip", ids["trip"])
    assert done["counts"]["prep_pack_selection"] == 1
    assert await _count(app_pool, ids["tenant"], "prep_pack_selection") == 0
    again = await _agency(app_pool)
    b = await _builder(app_pool, again)
    await _run(app_pool, again["tenant"], PACK_SQL, b["proposal"], again["trip"], "prep-pack-nepal", uuid.uuid4())
    await _template(app_pool, again["tenant"], "template-welcome-message")
    await _run(app_pool, again["tenant"], "UPDATE crm.tenant SET status = 'offboarding'", actor="seed:test")
    done = await _erase(app_pool, again["tenant"], "tenant", again["tenant"])
    assert done["counts"]["prep_pack_selection"] == 1 and done["counts"]["message_template"] == 1


async def test_a_trip_hold_guards_the_packs(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    b = await _builder(app_pool, ids)
    pack = await _run(app_pool, ids["tenant"], PACK_SQL, b["proposal"], ids["trip"], "prep-pack-nepal", uuid.uuid4())
    await _hold(app_pool, ids["tenant"], "trip", ids["trip"], "member:m")
    with pytest.raises(asyncpg.PostgresError) as err:
        await _run(app_pool, ids["tenant"], "DELETE FROM crm.prep_pack_selection WHERE prep_pack_selection_id = $1",
                   pack)  # fmt: skip
    assert err.value.sqlstate == "CRM10"
    with pytest.raises(asyncpg.PostgresError) as err:
        await _erase(app_pool, ids["tenant"], "trip", ids["trip"])
    assert err.value.sqlstate == "CRM10"
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"])
        assert await conn.fetchval("SELECT count(*) FROM crm.prep_pack_selection") == 1
