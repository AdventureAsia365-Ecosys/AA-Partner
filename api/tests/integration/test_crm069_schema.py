"""crm-069 (AA-965, CRM-33b, a contract file) on PostgreSQL with the RDS role shape.

- ``crm.voice_profile`` (R41): name / locale / channel / descriptors checks, never trashed, tenant isolation, and the
  agency erase takes it with the settings row (the member key is checked by test_crm010_schema CASCADE_ERASED).
- ``crm.ai_draft``: the two new kinds, ``voice_profile_id`` only on a reply / follow-up draft, ``validation`` an object
  of at most 16 KB; the crm-040 guard still lets only the decision change.
- The two AI stages are copies of the ``crm_draft_reply`` profile and route as it stood then (crm-078, AA-1138, later
  moved only the draft's position 0 to Claude Haiku 5.5 on acc1; the copies keep it as it was).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import asyncpg
import pytest

from tests.integration.test_crm010_schema import _agency, _count, _ctx, _erase

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MEMBER = "member:test"
STAGES = ("crm_supplier_confirm", "crm_notes_options")


async def _run(pool: asyncpg.Pool, tenant: uuid.UUID, sql: str, *args: Any, actor: str = MEMBER) -> Any:
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant, actor)
        return await conn.fetchval(sql, *args)


async def _setup(pool: asyncpg.Pool) -> dict[str, Any]:
    ids = await _agency(pool)
    await _run(pool, ids["tenant"], "INSERT INTO crm.tenant_settings DEFAULT VALUES ON CONFLICT (tenant_id) DO NOTHING")
    return ids


async def _voice(pool: asyncpg.Pool, ids: dict[str, Any], name: str = "Warm and short", **cols: Any) -> Any:
    descriptors = json.dumps(cols.pop("descriptors", {"formality": "neutral", "warmth": "warm"}))
    return await _run(
        pool, ids["tenant"],
        "INSERT INTO crm.voice_profile (member_id, name, locale, channel, descriptors) "
        "VALUES ($1, $2, $3, $4, $5::jsonb) RETURNING voice_profile_id",
        ids["member"], name, cols.get("locale", "en"), cols.get("channel", "email"), descriptors,
    )  # fmt: skip


async def _draft(pool: asyncpg.Pool, ids: dict[str, Any], kind: str, **cols: Any) -> Any:
    return await _run(
        pool, ids["tenant"],
        "INSERT INTO crm.ai_draft (trip_id, kind, llm_call_id, prompt_version, output_enc, voice_profile_id, "
        "validation) VALUES ($1, $2, $3, 'crm-test-1', $4, $5, $6::jsonb) RETURNING ai_draft_id",
        ids["trip"], kind, uuid.uuid4(), b"x" * 40, cols.get("voice"),
        json.dumps(cols["validation"]) if "validation" in cols else None,
    )  # fmt: skip


async def _refused(coro: Any) -> str:
    with pytest.raises(asyncpg.PostgresError) as err:
        await coro
    return f"{err.value.sqlstate} {err.value}"


async def test_crm069_is_registered_with_row_security(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 AS ok FROM shared.schema_versions WHERE version = 'crm-069'") == 1
    assert await admin.fetchval(
        "SELECT relrowsecurity AND relforcerowsecurity AS ok FROM pg_class WHERE oid = 'crm.voice_profile'::regclass"
    )


async def test_voice_profile_checks(app_pool: asyncpg.Pool) -> None:
    ids = await _setup(app_pool)
    assert await _voice(app_pool, ids) is not None
    assert await _voice(app_pool, ids, "Formal Vietnamese", locale="vi", channel="whatsapp") is not None
    for name, cols in (
        ("", {}),
        (" padded ", {}),
        ("x" * 61, {}),
        ("Wrong locale", {"locale": "fr"}),
        ("Wrong channel", {"channel": "sms"}),
        ("A list", {"descriptors": ["formal"]}),
        ("Too long", {"descriptors": {"formality": "x" * 1000}}),
    ):
        assert "23514" in await _refused(_voice(app_pool, ids, name, **cols)), name
    voice = await _voice(app_pool, ids, "To trash")
    refused = await _refused(_run(
        app_pool, ids["tenant"],
        "UPDATE crm.voice_profile SET deleted_at = now(), deleted_by = 'member:test', purge_after = now() "
        "WHERE voice_profile_id = $1", voice,
    ))  # fmt: skip
    assert "voice_profile_never_trashed_chk" in refused


async def test_voice_profile_is_isolated_and_erased_with_the_agency(app_pool: asyncpg.Pool) -> None:
    ids = await _setup(app_pool)
    other = await _setup(app_pool)
    await _voice(app_pool, ids)
    assert await _count(app_pool, ids["tenant"], "voice_profile") == 1
    assert await _count(app_pool, other["tenant"], "voice_profile") == 0
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"])
        await conn.execute("UPDATE crm.tenant SET status = 'offboarding'")
    await _erase(app_pool, ids["tenant"], "tenant", ids["tenant"])
    assert await _count(app_pool, ids["tenant"], "voice_profile") == 0


async def test_ai_draft_new_kinds_voice_and_validation(app_pool: asyncpg.Pool) -> None:
    ids = await _setup(app_pool)
    voice = await _voice(app_pool, ids)
    assert await _draft(app_pool, ids, "reply", voice=voice) is not None
    assert await _draft(app_pool, ids, "supplier_confirmation", validation={"lines": [], "rejected": 0}) is not None
    assert await _draft(app_pool, ids, "notes_to_options", validation={"options": [], "rejected": 2}) is not None
    assert "ai_draft_kind_chk" in await _refused(_draft(app_pool, ids, "summary"))
    refused = await _refused(_draft(app_pool, ids, "notes_to_options", voice=voice))
    assert "ai_draft_validation_chk" in refused  # a voice only on a message draft
    refused = await _refused(_draft(app_pool, ids, "notes_to_options", validation=["x"]))
    assert "ai_draft_validation_chk" in refused
    refused = await _refused(_draft(app_pool, ids, "notes_to_options", validation={"pad": "x" * 17000}))
    assert "ai_draft_validation_chk" in refused


async def test_the_guard_still_lets_only_the_decision_change(app_pool: asyncpg.Pool) -> None:
    ids = await _setup(app_pool)
    draft = await _draft(app_pool, ids, "supplier_confirmation", validation={"lines": []})
    refused = await _refused(_run(
        app_pool, ids["tenant"],
        "UPDATE crm.ai_draft SET validation = '{\"lines\": [1]}'::jsonb WHERE ai_draft_id = $1", draft,
    ))  # fmt: skip
    assert "CRM47" in refused
    decided = await _run(
        app_pool, ids["tenant"],
        "UPDATE crm.ai_draft SET decision = 'accepted', decided_at = now(), decided_by = 'member:test' "
        "WHERE ai_draft_id = $1 RETURNING decision", draft,
    )  # fmt: skip
    assert decided == "accepted"


async def test_the_new_stages_copy_the_draft_profile_and_route(admin: asyncpg.Connection) -> None:
    draft = await admin.fetchrow(
        "SELECT is_active, max_attempts, route_deadline_s FROM crm.llm_stage_profile WHERE stage = 'crm_draft_reply'"
    )
    route = [
        tuple(r)
        for r in await admin.fetch(
            "SELECT position, model, family, is_enabled FROM crm.llm_stage_route WHERE stage = 'crm_draft_reply' "
            "AND position > 0 ORDER BY position"  # crm-078 changed position 0 of the draft stage only
        )
    ]
    for stage in STAGES:
        row = await admin.fetchrow(
            "SELECT role, is_active, max_attempts, route_deadline_s FROM crm.llm_stage_profile WHERE stage = $1", stage
        )
        assert row is not None and row["role"] == "writer", stage
        assert (row["is_active"], row["max_attempts"], row["route_deadline_s"]) == tuple(draft), stage
        got = [
            tuple(r)
            for r in await admin.fetch(
                "SELECT position, model, family, is_enabled FROM crm.llm_stage_route WHERE stage = $1 "
                "ORDER BY position",
                stage,
            )
        ]
        assert got[1:] == route, stage
        assert got[0][0] == 0 and got[0][2] == "anthropic" and got[0][3] is False, stage  # the crm-037 entry, off
