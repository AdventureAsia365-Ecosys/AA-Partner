"""crm-010 (AA-942, card CRM-14) on PostgreSQL 15 with the RDS role shape: the lifecycle rules in the database.

- **R** (card): a trip with a paid invoice is not deletable (``has_money``; ``crm.erase_subject`` refuses with CRM11).
- **E** (card): a trip whose AA lines are all released / expired is deletable; its children go with it, including the
  append-only stage history.
- Erasure reaches the append-only ``consent`` / ``trip_stage_history`` / ``llm_call`` only inside the definer
  functions (crm_owner + ``app.erasing``); ``crm_app`` and a plain owner session are refused; the audit never.
- Legal holds suspend every deletion that reaches the subject (a client hold reaches their trips and both ends of a
  relation; a trip hold reaches every client whose erase would delete rows of the trip, a client hold every client
  related to them; any hold of the agency suspends its erase); a platform hold is released by a platform actor only,
  and no hold can be trashed.
- The agency erase needs status ``offboarding`` and empties every tenant table outside the keep list.
- ``crm_eraser`` is the only role that executes the deletion functions.
"""

from __future__ import annotations

import json
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
        held = '{"held": 1, "llm_call": 0, "placeholder_map": 0}'
        assert await conn.fetchval("SELECT crm.lifecycle_purge_expired(false)") == held


async def _hold(
    pool: asyncpg.Pool, tenant_id: uuid.UUID, scope: str, subject: uuid.UUID | None, actor: str
) -> uuid.UUID:
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant_id, actor)
        return await conn.fetchval(
            "INSERT INTO crm.legal_hold (scope, subject_id, reason_code) VALUES ($1, $2, 'litigation') "
            "RETURNING hold_id",
            scope,
            subject,
        )


async def _held(pool: asyncpg.Pool, tenant_id: uuid.UUID, scope: str, subject: uuid.UUID | None) -> bool:
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant_id)
        return bool(await conn.fetchval("SELECT crm.legal_hold_blocks($1, $2)", scope, subject))


@pytest.mark.parametrize("scope", ["trip", "traveler"])
async def test_any_hold_in_the_agency_suspends_its_offboarding_erase(app_pool: asyncpg.Pool, scope: str) -> None:
    """Review S249: a trip or client hold (not only an agency hold) stops the agency erase, dry run included."""
    ids = await _agency(app_pool)
    other = await _agency(app_pool)
    await _hold(app_pool, ids["tenant"], scope, ids["trip"] if scope == "trip" else ids["companion"], "platform:l1")
    await _hold(app_pool, other["tenant"], "trip", other["trip"], "platform:l1")
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"])
        await conn.execute("UPDATE crm.tenant SET status = 'offboarding'")
    assert await _held(app_pool, ids["tenant"], "tenant", None)
    for dry_run in (True, False):
        with pytest.raises(asyncpg.PostgresError, match="legal_hold") as err:
            await _erase(app_pool, ids["tenant"], "tenant", ids["tenant"], dry_run=dry_run)
        assert err.value.sqlstate == "CRM10"
    assert await _count(app_pool, ids["tenant"], "trip") == 1
    assert await _count(app_pool, ids["tenant"], "traveler") == 2
    # Subject-less data (llm_call) still follows the agency hold only: another subject's hold does not stop it.
    assert not await _held(app_pool, ids["tenant"], "llm_call", None)
    # A third agency without a hold is not reached by the others' holds (RLS).
    third = await _agency(app_pool)
    assert not await _held(app_pool, third["tenant"], "tenant", None)


async def test_a_hold_cannot_be_hidden_through_the_trash_columns(app_pool: asyncpg.Pool) -> None:
    """Review S249: a member session may not trash a hold (AA's or its own) to make legal_hold_blocks ignore it."""
    ids = await _agency(app_pool)
    platform_hold = await _hold(app_pool, ids["tenant"], "tenant", None, "platform:l1")
    own_hold = await _hold(app_pool, ids["tenant"], "trip", ids["trip"], f"member:{ids['member']}")
    for hold in (platform_hold, own_hold):
        with pytest.raises(asyncpg.PostgresError, match="only moves to released") as err:
            async with app_pool.acquire() as conn, conn.transaction():
                await _ctx(conn, ids["tenant"], f"member:{ids['member']}")
                await conn.execute(
                    "UPDATE crm.legal_hold SET deleted_at = now(), deleted_by = 'member:x', purge_after = now() "
                    "WHERE hold_id = $1",
                    hold,
                )
        assert err.value.sqlstate == "CRM17"
    with pytest.raises(asyncpg.PostgresError, match="hold_platform_only"):  # nor rewrite its release fields
        async with app_pool.acquire() as conn, conn.transaction():
            await _ctx(conn, ids["tenant"], f"member:{ids['member']}")
            await conn.execute("UPDATE crm.legal_hold SET released_by = 'member:x' WHERE hold_id = $1", platform_hold)
    assert await _held(app_pool, ids["tenant"], "tenant", None)
    # Nor can a hold be stored trashed: the CHECK refuses it.
    with pytest.raises(asyncpg.CheckViolationError):
        async with app_pool.acquire() as conn, conn.transaction():
            await _ctx(conn, ids["tenant"], "platform:l1")
            await conn.execute(
                "INSERT INTO crm.legal_hold (scope, subject_id, reason_code, deleted_at, deleted_by, purge_after) "
                "VALUES ('traveler', $1, 'other', now(), 'platform:l1', now())",
                ids["lead"],
            )
    # A released hold stays released: its release fields do not change any more.
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"], f"member:{ids['member']}")
        await conn.execute(
            "UPDATE crm.legal_hold SET status = 'released', released_at = now(), released_by = 'member:x' "
            "WHERE hold_id = $1",
            own_hold,
        )
    with pytest.raises(asyncpg.PostgresError, match="released legal hold does not change"):
        async with app_pool.acquire() as conn, conn.transaction():
            await _ctx(conn, ids["tenant"], f"member:{ids['member']}")
            await conn.execute("UPDATE crm.legal_hold SET released_by = 'member:y' WHERE hold_id = $1", own_hold)


async def test_a_client_hold_reaches_their_trips_and_both_ends_of_a_relation(app_pool: asyncpg.Pool) -> None:
    """Review S249: a hold on one client stops the erase of a trip they are on (their F2 party rows go with it), of a
    trip whose notes name them, and of a relation from or to them."""
    ids = await _agency(app_pool)
    trip2 = uuid.uuid4()
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"], f"member:{ids['member']}")
        await conn.execute("INSERT INTO crm.trip (trip_id, title) VALUES ($1, 'Laos 5 nights')", trip2)
        await conn.execute(
            "INSERT INTO crm.note (trip_id, traveler_id, kind, body_enc) VALUES ($1, $2, 'internal', $3)",
            trip2,
            ids["companion"],
            _blob(),
        )
        rel_from = await conn.fetchval(
            "INSERT INTO crm.traveler_relation (from_traveler_id, to_traveler_id, kind) VALUES ($1, $2, 'friend') "
            "RETURNING relation_id",
            ids["companion"],
            ids["lead"],
        )
        third = uuid.uuid4()
        await conn.execute("INSERT INTO crm.traveler (traveler_id, display_name_enc) VALUES ($1, $2)", third, _blob())
        rel_to = await conn.fetchval(
            "INSERT INTO crm.traveler_relation (from_traveler_id, to_traveler_id, kind) VALUES ($1, $2, 'friend') "
            "RETURNING relation_id",
            third,
            ids["companion"],
        )
    hold = await _hold(app_pool, ids["tenant"], "traveler", ids["companion"], f"member:{ids['member']}")
    for scope, subject in (("trip", ids["trip"]), ("trip", trip2), ("relation", rel_from), ("relation", rel_to)):
        with pytest.raises(asyncpg.PostgresError, match="legal_hold") as err:
            await _erase(app_pool, ids["tenant"], scope, subject)
        assert err.value.sqlstate == "CRM10", (scope, subject)
    assert await _count(app_pool, ids["tenant"], "trip_party") == 2
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"], f"member:{ids['member']}")
        await conn.execute(
            "UPDATE crm.legal_hold SET status = 'released', released_at = now(), released_by = 'member:x' "
            "WHERE hold_id = $1",
            hold,
        )
    assert (await _erase(app_pool, ids["tenant"], "relation", rel_to))["counts"]["traveler_relation"] == 1
    assert (await _erase(app_pool, ids["tenant"], "trip", trip2))["counts"]["trip"] == 1


async def _client(pool: asyncpg.Pool, tenant_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """A client on no trip, with an F2 profile: (traveler_id, profile token)."""
    traveler, token = uuid.uuid4(), uuid.uuid4()
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant_id, "seed:test")
        await conn.execute("INSERT INTO crm.traveler_profile (traveler_token) VALUES ($1)", token)
        await conn.execute(
            "INSERT INTO crm.traveler (traveler_id, display_name_enc, profile_token_enc) VALUES ($1, $2, $3)",
            traveler,
            _blob(),
            _blob(),
        )
    return traveler, token


async def _name_on_trip(
    pool: asyncpg.Pool, tenant_id: uuid.UUID, trip: uuid.UUID, traveler: uuid.UUID, where: str
) -> dict[str, int]:
    """Rows of ``trip`` that name ``traveler`` without putting them on its party list; returns the per-table count."""
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant_id, "seed:test")
        if where == "note_and_document":
            await conn.execute(
                "INSERT INTO crm.note (trip_id, traveler_id, kind, body_enc) VALUES ($1, $2, 'internal', $3)",
                trip,
                traveler,
                _blob(),
            )
            document = uuid.uuid4()
            await conn.execute(
                "INSERT INTO crm.document (document_id, trip_id, traveler_id, kind, s3_key, sha256, size_bytes, "
                "content_type, filename_enc) VALUES ($1, $2, $3, 'ticket', $4, $5, 10, 'application/pdf', $6)",
                document,
                trip,
                traveler,
                f"tenants/{tenant_id}/documents/{document}",
                os.urandom(32),
                _blob(),
            )
            return {"note": 1, "document": 1}
        if where == "submission":
            definition = await conn.fetchval(
                "INSERT INTO crm.form_definition (version, schema_key, schema) VALUES (1, 'inquiry', '{}') "
                "RETURNING form_definition_id"
            )
            form = await conn.fetchval(
                "INSERT INTO crm.intake_form (public_key, label, form_definition_id, honeypot_field) "
                "VALUES ($1, 'Website', $2, 'website') RETURNING intake_form_id",
                uuid.uuid4().hex,
                definition,
            )
            await conn.execute(
                "INSERT INTO crm.form_submission (intake_form_id, schema_key, payload_enc, idempotency_key, trip_id, "
                "traveler_id) VALUES ($1, 'inquiry', $2, $3, $4, $5)",
                form,
                _blob(),
                f"sub-{uuid.uuid4().hex}",
                trip,
                traveler,
            )
            return {"form_submission": 1}
        await conn.execute(
            "INSERT INTO crm.dispatch_queue (traveler_id, trip_id, channel, purpose, origin) "
            "VALUES ($1, $2, 'email', 'transactional', 'system')",
            traveler,
            trip,
        )
        return {"dispatch_queue": 1}


async def _rows_of_trip(
    pool: asyncpg.Pool, tenant_id: uuid.UUID, trip: uuid.UUID, traveler: uuid.UUID, tables: dict[str, int]
) -> dict[str, int]:
    """Rows of ``trip`` naming ``traveler``, per table."""
    sql = "SELECT count(*) FROM crm.{} WHERE trip_id = $1 AND traveler_id = $2"
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant_id)
        return {t: int(await conn.fetchval(sql.format(t), trip, traveler)) for t in tables}


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("where", ["note_and_document", "submission", "dispatch"])
async def test_a_trip_hold_reaches_a_client_named_on_its_rows_off_the_party_list(
    app_pool: asyncpg.Pool, where: str, dry_run: bool
) -> None:
    """Re-review S249 B1 (probe p1): trip H is on hold; client T is not on H's party list, only named on a note and a
    document (or a submission, a queued message) of H. The traveller erase deletes those rows whatever their trip, so
    the hold on H reaches T: CRM10, dry run included, and H keeps every row."""
    ids = await _agency(app_pool)
    traveler, token = await _client(app_pool, ids["tenant"])
    expected = await _name_on_trip(app_pool, ids["tenant"], ids["trip"], traveler, where)
    hold = await _hold(app_pool, ids["tenant"], "trip", ids["trip"], f"member:{ids['member']}")
    with pytest.raises(asyncpg.PostgresError, match="legal_hold") as err:
        await _erase(app_pool, ids["tenant"], "traveler", traveler, token, dry_run=dry_run)
    assert err.value.sqlstate == "CRM10"
    assert await _rows_of_trip(app_pool, ids["tenant"], ids["trip"], traveler, expected) == expected
    assert await _count(app_pool, ids["tenant"], "traveler") == 3
    # Released, the same erase goes through and takes T's rows of H with it.
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"], f"member:{ids['member']}")
        await conn.execute(
            "UPDATE crm.legal_hold SET status = 'released', released_at = now(), released_by = 'member:x' "
            "WHERE hold_id = $1",
            hold,
        )
    done = await _erase(app_pool, ids["tenant"], "traveler", traveler, token, dry_run=dry_run)
    assert all(done["counts"][t] == n for t, n in expected.items()), done["counts"]
    left = await _rows_of_trip(app_pool, ids["tenant"], ids["trip"], traveler, expected)
    assert left == (expected if dry_run else dict.fromkeys(expected, 0))


@pytest.mark.parametrize("dry_run", [False, True])
async def test_a_client_hold_reaches_the_erase_of_a_related_client(app_pool: asyncpg.Pool, dry_run: bool) -> None:
    """Re-review S249 B1 (probe p2): client B is on hold, A and B are related. Erasing A would delete both relation
    rows (the inverse too), so the hold on B reaches A: CRM10, dry run included; nothing is deleted."""
    ids = await _agency(app_pool)
    a, a_token = await _client(app_pool, ids["tenant"])
    b, _ = await _client(app_pool, ids["tenant"])
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"], "seed:test")
        await conn.execute(
            "INSERT INTO crm.traveler_relation (from_traveler_id, to_traveler_id, kind) VALUES ($1, $2, 'spouse')", a, b
        )
    relations = await _count(app_pool, ids["tenant"], "traveler_relation")
    assert relations == 2  # the inverse row (crm-006)
    hold = await _hold(app_pool, ids["tenant"], "traveler", b, f"member:{ids['member']}")
    with pytest.raises(asyncpg.PostgresError, match="legal_hold") as err:
        await _erase(app_pool, ids["tenant"], "traveler", a, a_token, dry_run=dry_run)
    assert err.value.sqlstate == "CRM10"
    assert await _count(app_pool, ids["tenant"], "traveler_relation") == relations
    assert await _count(app_pool, ids["tenant"], "traveler") == 4
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"], f"member:{ids['member']}")
        await conn.execute(
            "UPDATE crm.legal_hold SET status = 'released', released_at = now(), released_by = 'member:x' "
            "WHERE hold_id = $1",
            hold,
        )
    done = await _erase(app_pool, ids["tenant"], "traveler", a, a_token, dry_run=dry_run)
    assert done["counts"]["traveler_relation"] == 2
    assert await _count(app_pool, ids["tenant"], "traveler_relation") == (relations if dry_run else 0)


@pytest.mark.parametrize("dry_run", [False, True])
async def test_a_client_hold_reaches_a_trip_that_queues_a_message_to_them(
    app_pool: asyncpg.Pool, dry_run: bool
) -> None:
    """Re-review S249 (minor): a trip erase deletes the trip's queued messages; one addressed to a held client (who is
    not on the party list) makes the trip held too (crm.legal_hold_blocks), so the erase refuses and the row stays."""
    ids = await _agency(app_pool)
    client, _ = await _client(app_pool, ids["tenant"])
    await _name_on_trip(app_pool, ids["tenant"], ids["trip"], client, "dispatch")
    await _hold(app_pool, ids["tenant"], "traveler", client, f"member:{ids['member']}")
    assert await _held(app_pool, ids["tenant"], "trip", ids["trip"])
    with pytest.raises(asyncpg.PostgresError, match="legal_hold") as err:
        await _erase(app_pool, ids["tenant"], "trip", ids["trip"], dry_run=dry_run)
    assert err.value.sqlstate == "CRM10"
    queued = {"dispatch_queue": 1}
    assert await _rows_of_trip(app_pool, ids["tenant"], ids["trip"], client, queued) == queued
    assert await _count(app_pool, ids["tenant"], "trip") == 1


async def test_the_deletion_functions_pin_their_search_path(admin: asyncpg.Connection) -> None:
    """Re-review S249: every crm-010 function runs with search_path = pg_catalog, pg_temp (pg_temp last)."""
    rows = await admin.fetch(
        "SELECT p.oid::regprocedure::text AS fn, p.proconfig FROM pg_proc p "
        "WHERE p.pronamespace = 'crm'::regnamespace AND p.proname = ANY($1::text[])",
        [
            "guard_legal_hold",
            "legal_hold_blocks",
            "trip_delete_blockers",
            "erasable_append_only",
            "erase_rows",
            "erase_subject",
            "lifecycle_purge_expired",
        ],
    )
    assert len(rows) == 7
    assert {r["fn"]: list(r["proconfig"] or []) for r in rows} == {
        r["fn"]: ["search_path=pg_catalog, pg_temp"] for r in rows
    }


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
        result = '{"held": 0, "llm_call": 1, "placeholder_map": 0}'
        assert await conn.fetchval("SELECT crm.lifecycle_purge_expired(true)") == result
        assert await conn.fetchval("SELECT crm.lifecycle_purge_expired(false)") == result
    assert await _count(app_pool, ids["tenant"], "llm_call") == 2


async def test_the_llm_call_purge_takes_the_placeholder_maps_of_its_calls(app_pool: asyncpg.Pool) -> None:
    """Re-review S249: an old call that still has its crm-009 placeholder map (placeholder_map_call_fk) does not stop
    the 180-day purge: the map goes first; a map of a recent call stays. Before crm-009 the step reports 0."""
    ids = await _agency(app_pool)
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"])
        calls = []
        for days in (200, 10):
            calls.append(
                await conn.fetchval(
                    "INSERT INTO crm.llm_call (at, stage, role, provider, model, outcome) "
                    "VALUES (now() - make_interval(days => $1), 'crm_test', 'writer', 'stub', 'stub-1', 'ok') "
                    "RETURNING llm_call_id",
                    days,
                )
            )
        recent = calls[1]
        has_maps = await conn.fetchval("SELECT to_regclass('crm.placeholder_map') IS NOT NULL")
        if has_maps:
            for call in calls:
                await conn.execute(
                    "INSERT INTO crm.placeholder_map (placeholder_map_id, llm_call_id, map_enc, placeholder_count, "
                    "scrubber_version) VALUES ($1, $2, $3, 1, 'f3-1')",
                    uuid.uuid4(),
                    call,
                    _blob(),
                )
        maps = 1 if has_maps else 0
        await conn.execute("SET LOCAL ROLE crm_eraser")
        assert json.loads(await conn.fetchval("SELECT crm.lifecycle_purge_expired(true)")) == {
            "held": 0,
            "llm_call": 1,
            "placeholder_map": maps,
        }
        assert json.loads(await conn.fetchval("SELECT crm.lifecycle_purge_expired(false)")) == {
            "held": 0,
            "llm_call": 1,
            "placeholder_map": maps,
        }
    assert await _count(app_pool, ids["tenant"], "llm_call") == 2  # the seed call and the recent one
    if has_maps:
        async with app_pool.acquire() as conn, conn.transaction():
            await _ctx(conn, ids["tenant"])
            assert await conn.fetchval("SELECT array_agg(llm_call_id) FROM crm.placeholder_map") == [recent]


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
