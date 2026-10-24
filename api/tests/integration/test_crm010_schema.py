"""crm-010 (AA-942, card CRM-14) on PostgreSQL 15 with the RDS role shape: the lifecycle rules in the database.

- **R** (card): a trip with a paid invoice is not deletable (``has_money``; ``crm.erase_subject`` refuses with CRM11).
- **E** (card): a trip whose AA lines are all released / expired is deletable; its children go with it, including the
  append-only stage history.
- Erasure reaches the append-only ``consent`` / ``trip_stage_history`` / ``llm_call`` only inside the definer
  functions (crm_owner + ``app.erasing``); ``crm_app`` and a plain owner session are refused; the audit never.
- Legal holds suspend every deletion that reaches the subject; a platform hold is released by a platform actor only.
- The agency erase needs status ``offboarding`` and empties every tenant table outside the keep list.
- ``crm_eraser`` is the only role that executes the deletion functions.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import asyncpg
import pytest

from tests.integration.conftest import OWNER

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ISS = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_test"
KEEP = {
    "tenant",
    "tenant_key",
    "audit_event",
    "audit_export_tenant",
    "audit_export_manifest",
    "erasure_request",
    "lifecycle_run",
    "legal_hold",
}


def _blob() -> bytes:
    return os.urandom(40)  # shaped like a sealed value (>= 30 bytes); never opened here


async def _ctx(conn: asyncpg.Connection, tenant_id: uuid.UUID, actor: str = "job:crm_lifecycle") -> None:
    await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
    await conn.execute("SELECT set_config('app.actor', $1, true)", actor)


async def _agency(pool: asyncpg.Pool) -> dict[str, Any]:
    """One agency with an owner, a lead traveller (profile, consent, health-free), a trip with history, a task, a note,
    a companion, an LLM call and an audit event. Written as crm_app."""
    ids: dict[str, Any] = {
        "tenant": uuid.uuid4(),
        "lead": uuid.uuid4(),
        "companion": uuid.uuid4(),
        "trip": uuid.uuid4(),
        "token": uuid.uuid4(),
        "token2": uuid.uuid4(),
    }
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"], "seed:test")
        await conn.execute("INSERT INTO crm.tenant (tenant_id) VALUES ($1)", ids["tenant"])
        ids["member"] = await conn.fetchval(
            "INSERT INTO crm.member (cognito_iss, cognito_sub, role, status) VALUES ($1, $2, 'owner', 'active') "
            "RETURNING member_id",
            ISS,
            f"sub-{uuid.uuid4()}",
        )
        for key, token in (("lead", "token"), ("companion", "token2")):
            await conn.execute("INSERT INTO crm.traveler_profile (traveler_token) VALUES ($1)", ids[token])
            await conn.execute(
                "INSERT INTO crm.traveler (traveler_id, display_name_enc, profile_token_enc) VALUES ($1, $2, $3)",
                ids[key],
                _blob(),
                _blob(),
            )
        await conn.execute("INSERT INTO crm.trip (trip_id, title) VALUES ($1, 'Vietnam 10 nights')", ids["trip"])
        for key, token, role in (("lead", "token", "lead"), ("companion", "token2", "companion")):
            await conn.execute(
                "INSERT INTO crm.trip_traveler (trip_id, traveler_id, role) VALUES ($1, $2, $3)",
                ids["trip"],
                ids[key],
                role,
            )
            await conn.execute(
                "INSERT INTO crm.trip_party (trip_id, traveler_token, role) VALUES ($1, $2, $3)",
                ids["trip"],
                ids[token],
                role,
            )
        await conn.execute(
            "INSERT INTO crm.trip_stage_history (trip_id, from_stage, to_stage, reason_code) "
            "VALUES ($1, NULL, 'inquiry', 'intake')",
            ids["trip"],
        )
        await conn.execute("INSERT INTO crm.task (trip_id, kind) VALUES ($1, 'reply_within_2h')", ids["trip"])
        await conn.execute(
            "INSERT INTO crm.note (trip_id, kind, body_enc) VALUES ($1, 'internal', $2)", ids["trip"], _blob()
        )
        await conn.execute(
            "INSERT INTO crm.consent (traveler_id, purpose, value, source) VALUES ($1, 'contact', true, 'form')",
            ids["lead"],
        )
        await conn.execute(
            "INSERT INTO crm.llm_call (stage, role, provider, model, outcome) VALUES ('crm_test', 'writer', 'stub', "
            "'stub-1', 'ok')"
        )
        await conn.execute(
            "INSERT INTO crm.audit_event (actor_kind, actor_ref, action, outcome, object_kind) "
            "VALUES ('user', 'member:m', 'view', 'allowed', 'trip')"
        )
    return ids


async def _erase(
    pool: asyncpg.Pool,
    tenant_id: uuid.UUID,
    scope: str,
    subject: uuid.UUID,
    token: uuid.UUID | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant_id)
        await conn.execute("SET LOCAL ROLE crm_eraser")
        import json

        return json.loads(
            await conn.fetchval("SELECT crm.erase_subject($1, $2, $3, $4)", scope, subject, token, dry_run)
        )


async def _count(pool: asyncpg.Pool, tenant_id: uuid.UUID, table: str) -> int:
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant_id)
        return int(await conn.fetchval(f"SELECT count(*) FROM crm.{table}"))  # noqa: S608 — test, fixed names


async def _commit(pool: asyncpg.Pool, tenant_id: uuid.UUID, trip: uuid.UUID, kind: str, status: str) -> None:
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant_id)
        await conn.execute(
            "INSERT INTO crm.trip_commitment (trip_id, kind, line_status, source_ref) VALUES ($1, $2, $3, $4)",
            trip,
            kind,
            status,
            f"ref-{uuid.uuid4()}",
        )


async def _blockers(pool: asyncpg.Pool, tenant_id: uuid.UUID, trip: uuid.UUID) -> list[str]:
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant_id)
        return list(await conn.fetchval("SELECT crm.trip_delete_blockers($1)", trip))


async def test_crm010_is_registered(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-010'") == 1


async def test_r_a_paid_invoice_blocks_the_trip_delete(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    await _commit(app_pool, ids["tenant"], ids["trip"], "invoice", "paid")
    assert await _blockers(app_pool, ids["tenant"], ids["trip"]) == ["has_money"]
    with pytest.raises(asyncpg.PostgresError, match="delete_blocked has_money") as err:
        await _erase(app_pool, ids["tenant"], "trip", ids["trip"])
    assert err.value.sqlstate == "CRM11"
    assert await _count(app_pool, ids["tenant"], "trip") == 1


@pytest.mark.parametrize(
    ("kind", "status", "code"),
    [
        ("payment", "succeeded", "has_money"),
        ("payment", "refunded", "has_money"),
        ("invoice", "payment_claimed", "has_money"),
        ("signature", "signed", "has_signature"),
        ("commission", "received", "has_commission"),
        ("commission", "claimed", "has_commission"),
        ("aa_line", "held", "has_aa_line"),
        ("aa_line", "confirmed", "has_aa_line"),
    ],
)
async def test_every_blocking_commitment(app_pool: asyncpg.Pool, kind: str, status: str, code: str) -> None:
    ids = await _agency(app_pool)
    await _commit(app_pool, ids["tenant"], ids["trip"], kind, status)
    assert await _blockers(app_pool, ids["tenant"], ids["trip"]) == [code]


async def test_e_a_trip_whose_aa_lines_all_expired_is_deletable(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    for status in ("expired", "released", "cancelled", "declined"):
        await _commit(app_pool, ids["tenant"], ids["trip"], "aa_line", status)
    await _commit(app_pool, ids["tenant"], ids["trip"], "invoice", "void")
    assert await _blockers(app_pool, ids["tenant"], ids["trip"]) == []
    dry = await _erase(app_pool, ids["tenant"], "trip", ids["trip"], dry_run=True)
    assert dry["dry_run"] is True and dry["counts"]["trip"] == 1 and dry["counts"]["trip_commitment"] == 5
    assert await _count(app_pool, ids["tenant"], "trip") == 1  # a dry run deletes nothing
    done = await _erase(app_pool, ids["tenant"], "trip", ids["trip"])
    assert done["counts"] | {} == dry["counts"]
    assert done["counts"]["trip_stage_history"] == 1 and done["counts"]["trip_party"] == 2
    for table in ("trip", "trip_stage_history", "trip_traveler", "trip_party", "task", "note", "trip_commitment"):
        assert await _count(app_pool, ids["tenant"], table) == 0, table
    assert await _count(app_pool, ids["tenant"], "traveler") == 2  # the people stay; only the trip went
    assert await _count(app_pool, ids["tenant"], "audit_event") == 1


async def test_append_only_tables_refuse_deletes_outside_the_definer_functions(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    ids = await _agency(app_pool)
    async with app_pool.acquire() as conn:
        for table in ("consent", "trip_stage_history", "llm_call", "lifecycle_run"):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                async with conn.transaction():
                    await _ctx(conn, ids["tenant"])
                    await conn.execute("SELECT set_config('app.erasing', 'on', true)")
                    await conn.execute(f"DELETE FROM crm.{table}")  # noqa: S608 — test, fixed names
    # The owner itself (a migration, an admin session) is refused without the definer function's flag ...
    for table in ("consent", "trip_stage_history", "llm_call"):
        with pytest.raises(asyncpg.InsufficientPrivilegeError, match="append-only"):
            async with admin.transaction():
                await admin.execute(f"SET LOCAL ROLE {OWNER}")
                await _ctx(admin, ids["tenant"])
                await admin.execute(f"DELETE FROM crm.{table}")  # noqa: S608 — test, fixed names
        with pytest.raises(asyncpg.InsufficientPrivilegeError, match="append-only"):
            async with admin.transaction():
                await admin.execute(f"SET LOCAL ROLE {OWNER}")
                await _ctx(admin, ids["tenant"])
                await admin.execute("SELECT set_config('app.erasing', 'on', true)")
                await admin.execute(f"UPDATE crm.{table} SET created_by = 'x'")  # noqa: S608 — UPDATE never
    # ... and the audit is never erasable, even with the flag.
    with pytest.raises(asyncpg.InsufficientPrivilegeError, match="append-only"):
        async with admin.transaction():
            await admin.execute(f"SET LOCAL ROLE {OWNER}")
            await _ctx(admin, ids["tenant"])
            await admin.execute("SELECT set_config('app.erasing', 'on', true)")
            await admin.execute("DELETE FROM crm.audit_event")


async def test_only_crm_eraser_executes_the_deletion_functions(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    async with app_pool.acquire() as conn:
        for sql in (
            "SELECT crm.erase_subject('trip', $1::uuid, NULL, true)",
            "SELECT crm.lifecycle_purge_expired(true)",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                async with conn.transaction():
                    await _ctx(conn, ids["tenant"])
                    await conn.execute(sql, ids["trip"]) if "$1" in sql else await conn.execute(sql)
        with pytest.raises(asyncpg.InsufficientPrivilegeError):  # the step helper is the owner's alone
            async with conn.transaction():
                await _ctx(conn, ids["tenant"])
                await conn.execute("SET LOCAL ROLE crm_eraser")
                await conn.execute("SELECT crm.erase_rows('trip', 'true', NULL, NULL, false)")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):  # the eraser reads no table itself
            async with conn.transaction():
                await _ctx(conn, ids["tenant"])
                await conn.execute("SET LOCAL ROLE crm_eraser")
                await conn.fetch("SELECT * FROM crm.traveler")


async def test_erasing_a_traveller_reaches_consent_and_the_profile(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    with pytest.raises(asyncpg.PostgresError, match="traveler_on_trip") as err:
        await _erase(app_pool, ids["tenant"], "traveler", ids["lead"], ids["token"])
    assert err.value.sqlstate == "CRM12"
    async with app_pool.acquire() as conn, conn.transaction():  # the trip goes to the trash first
        await _ctx(conn, ids["tenant"])
        await conn.execute("UPDATE crm.trip SET deleted_at = now(), deleted_by = 'member:x', purge_after = now()")
    with pytest.raises(asyncpg.PostgresError, match="profile_token_required"):
        await _erase(app_pool, ids["tenant"], "traveler", ids["lead"])
    with pytest.raises(asyncpg.PostgresError, match="profile_token_mismatch") as err:
        await _erase(app_pool, ids["tenant"], "traveler", ids["lead"], ids["token2"])  # the companion's token
    assert err.value.sqlstate == "CRM15"
    done = await _erase(app_pool, ids["tenant"], "traveler", ids["lead"], ids["token"])
    assert done["counts"]["consent"] == 1 and done["counts"]["traveler"] == 1
    assert done["counts"]["traveler_profile"] == 1 and done["counts"]["trip_party"] == 1
    assert await _count(app_pool, ids["tenant"], "consent") == 0
    assert await _count(app_pool, ids["tenant"], "traveler") == 1  # the companion stays


async def test_legal_holds_suspend_deletion(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"], f"member:{ids['member']}")
        hold = await conn.fetchval(
            "INSERT INTO crm.legal_hold (scope, subject_id, reason_code) "
            "VALUES ('trip', $1, 'dispute') RETURNING hold_id",
            ids["trip"],
        )
        await conn.execute("UPDATE crm.trip SET deleted_at = now(), deleted_by = 'member:x', purge_after = now()")
    for scope, subject, token in (("trip", ids["trip"], None), ("traveler", ids["lead"], ids["token"])):
        with pytest.raises(asyncpg.PostgresError, match="legal_hold") as err:
            await _erase(app_pool, ids["tenant"], scope, subject, token)
        assert err.value.sqlstate == "CRM10"
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"], f"member:{ids['member']}")
        await conn.execute(
            "UPDATE crm.legal_hold SET status = 'released', released_at = now(), "
            "released_by = current_setting('app.actor') WHERE hold_id = $1",
            hold,
        )
    assert (await _erase(app_pool, ids["tenant"], "trip", ids["trip"]))["counts"]["trip"] == 1
    # An agency hold placed by AA: the agency cannot release it; the LLM-call purge reports it held.
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"], "platform:l1")
        hold = await conn.fetchval(
            "INSERT INTO crm.legal_hold (scope, reason_code) VALUES ('tenant', 'regulator_request') RETURNING hold_id"
        )
    with pytest.raises(asyncpg.PostgresError, match="hold_platform_only"):
        async with app_pool.acquire() as conn, conn.transaction():
            await _ctx(conn, ids["tenant"], f"member:{ids['member']}")
            await conn.execute(
                "UPDATE crm.legal_hold SET status = 'released', released_at = now(), "
                "released_by = 'member:x' WHERE hold_id = $1",
                hold,
            )
    with pytest.raises(asyncpg.InsufficientPrivilegeError):  # nor delete it
        async with app_pool.acquire() as conn, conn.transaction():
            await _ctx(conn, ids["tenant"], f"member:{ids['member']}")
            await conn.execute("DELETE FROM crm.legal_hold")
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"])
        await conn.execute("SET LOCAL ROLE crm_eraser")
        assert await conn.fetchval("SELECT crm.lifecycle_purge_expired(false)") == '{"held": 1, "llm_call": 0}'


async def test_the_llm_call_purge_keeps_180_days(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"])
        await conn.execute(
            "INSERT INTO crm.llm_call (at, stage, role, provider, model, outcome) VALUES "
            "(now() - interval '181 days', 'crm_test', 'writer', 'stub', 'stub-1', 'ok'), "
            "(now() - interval '179 days', 'crm_test', 'writer', 'stub', 'stub-1', 'ok')"
        )
        await conn.execute("SET LOCAL ROLE crm_eraser")
        assert await conn.fetchval("SELECT crm.lifecycle_purge_expired(true)") == '{"held": 0, "llm_call": 1}'
        assert await conn.fetchval("SELECT crm.lifecycle_purge_expired(false)") == '{"held": 0, "llm_call": 1}'
    assert await _count(app_pool, ids["tenant"], "llm_call") == 2


async def test_the_agency_erase_needs_offboarding_and_empties_every_tenant_table(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    ids = await _agency(app_pool)
    other = await _agency(app_pool)
    await _commit(app_pool, ids["tenant"], ids["trip"], "aa_line", "expired")
    with pytest.raises(asyncpg.PostgresError, match="tenant_not_offboarding") as err:
        await _erase(app_pool, ids["tenant"], "tenant", ids["tenant"])
    assert err.value.sqlstate == "CRM14"
    with pytest.raises(asyncpg.PostgresError, match="subject_not_found"):
        await _erase(app_pool, ids["tenant"], "tenant", other["tenant"])
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"])
        await conn.execute("UPDATE crm.tenant SET status = 'offboarding'")
    dry = await _erase(app_pool, ids["tenant"], "tenant", ids["tenant"], dry_run=True)
    assert dry["counts"]["member"] == 1 and dry["counts"]["traveler"] == 2
    done = await _erase(app_pool, ids["tenant"], "tenant", ids["tenant"])
    assert done["counts"]["consent"] == 1 and done["counts"]["llm_call"] == 1
    tables = [
        r["relname"]
        for r in await admin.fetch(
            "SELECT c.relname FROM pg_class c JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'tenant_id' "
            "WHERE c.relnamespace = 'crm'::regnamespace AND c.relkind IN ('r', 'p') AND NOT c.relispartition"
        )
    ]
    for table in tables:
        n = await _count(app_pool, ids["tenant"], table) if table != "audit_export_tenant" else None
        if table in KEEP:
            continue
        assert n == 0, table
    assert await _count(app_pool, ids["tenant"], "audit_event") == 1  # the audit stays (crm-007 partition path)
    assert await _count(app_pool, other["tenant"], "traveler") == 2  # another agency is untouched


async def test_the_agency_erase_knows_every_tenant_table(admin: asyncpg.Connection) -> None:
    """A tenant table the erase does not name (nor keep) would fail every offboarding (CRM16): name it in
    crm.erase_subject in the same migration that creates it."""
    source = await admin.fetchval(
        "SELECT prosrc FROM pg_proc WHERE oid = 'crm.erase_subject(text,uuid,uuid,boolean)'::regprocedure"
    )
    tables = [
        r["relname"]
        for r in await admin.fetch(
            "SELECT c.relname FROM pg_class c JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'tenant_id' "
            "WHERE c.relnamespace = 'crm'::regnamespace AND c.relkind IN ('r', 'p') AND NOT c.relispartition"
        )
    ]
    missing = [t for t in tables if f"'{t}'" not in source and t not in KEEP]
    assert missing == []


async def test_dispatch_rows_follow_their_gate(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    async with app_pool.acquire() as conn:
        async with conn.transaction():
            await _ctx(conn, ids["tenant"])
            dispatch = await conn.fetchval(
                "INSERT INTO crm.dispatch_queue (traveler_id, channel, purpose, origin) "
                "VALUES ($1, 'email', 'transactional', 'system') RETURNING dispatch_id",
                ids["lead"],
            )
        with pytest.raises(asyncpg.CheckViolationError):  # a decision needs its gate result
            async with conn.transaction():
                await _ctx(conn, ids["tenant"])
                await conn.execute("UPDATE crm.dispatch_queue SET status = 'dropped', decided_at = now()")
        async with conn.transaction():
            await _ctx(conn, ids["tenant"])
            await conn.execute(
                "UPDATE crm.dispatch_queue SET status = 'dropped', gate_result = 'archived', "
                "decided_at = now() WHERE dispatch_id = $1",
                dispatch,
            )
        with pytest.raises(asyncpg.CheckViolationError, match="not an allowed transition"):
            async with conn.transaction():
                await _ctx(conn, ids["tenant"])
                await conn.execute("UPDATE crm.dispatch_queue SET status = 'sent', gate_result = 'sent'")
