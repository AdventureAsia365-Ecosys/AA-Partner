"""crm-025 (AA-955 CRM-25 e-sign v1) on PostgreSQL with the RDS role shape: the envelope in the database.

- ``crm.agreement_doc``: the insurance declination kind.
- ``crm.signature_request``: a guardian signs from their own access for a minor (the access key names the signer);
  the signer / envelope CHECKs; the v1 columns are sealed once signed (CRM31).
- ``crm.terms_acceptance`` / ``crm.signature_event``: append-only for crm_app, FORCE RLS, nothing for the readers, no
  foreign key on the trip or traveller; the evidence hash CHECKs.
- The DM-7 claim-period exception: a trip or traveller erase keeps the evidence and counts it in the receipt; the
  agency erase takes it; crm.lifecycle_purge_expired deletes it after keep_until once its trip is gone, never a held
  row.

The API's use of them is in tests/integration/test_esign_api.py.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from tests.integration.conftest import OWNER, READERS
from tests.integration.test_crm010_schema import _count, _ctx, _erase, _hold
from tests.integration.test_crm016_schema import _blob, _hash, _lifecycle, _pay_deposit, _run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "025_crm_contracts_waivers.sql"
NEW_TABLES = ("terms_acceptance", "signature_event")


async def _reapply(admin: asyncpg.Connection) -> None:
    await admin.execute(f"SET ROLE {OWNER}")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")


def _record(**parts: Any) -> tuple[bytes, bytes]:
    raw = json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()
    return raw, hashlib.sha256(raw).digest()


async def test_crm025_is_registered_and_runs_again_as_a_no_op(admin: asyncpg.Connection) -> None:
    # Earlier test modules re-apply their own erase_subject / lifecycle_purge_expired text: put this key's back first.
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-025'") == 1
    await _reapply(admin)
    erase = await admin.fetchval(
        "SELECT prosrc FROM pg_proc WHERE oid = 'crm.erase_subject(text,uuid,uuid,boolean)'::regprocedure"
    )
    assert erase.count("'terms_acceptance'") == 1 and erase.count("'signature_event'") == 1
    assert "signature_event_kept" in erase and "signer_traveler_id = $1" in erase and "'invoice'" in erase
    assert erase.index("['signature_event', '']") < erase.index("['agreement_doc', '']")
    purge = await admin.fetchval(
        "SELECT prosrc FROM pg_proc WHERE oid = 'crm.lifecycle_purge_expired(boolean)'::regprocedure"
    )
    assert "'membership_inbox'" in purge and "'terms_acceptance'" in purge and "'signature_event'" in purge


@pytest.mark.parametrize("table", NEW_TABLES)
async def test_privileges_and_no_trip_keys(admin: asyncpg.Connection, table: str) -> None:
    for privilege, want in (("SELECT", True), ("INSERT", True), ("UPDATE", False), ("DELETE", False),
                            ("TRUNCATE", False)):  # fmt: skip
        got = await admin.fetchval("SELECT has_table_privilege('crm_app', $1, $2)", f"crm.{table}", privilege)
        assert got is want, (table, privilege)
    for reader in READERS:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert not await admin.fetchval("SELECT has_table_privilege($1, $2, $3)", reader, f"crm.{table}", privilege)
    assert await admin.fetchval(
        "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid = $1::regclass", f"crm.{table}"
    )
    refs = await admin.fetch(
        "SELECT confrelid::regclass::text AS ref FROM pg_constraint WHERE conrelid = $1::regclass AND contype = 'f'",
        f"crm.{table}",
    )
    assert {r["ref"] for r in refs} <= {"crm.tenant", "crm.agreement_doc"}


async def _signed_event(conn: asyncpg.Connection, ids: dict[str, Any], *, traveler: uuid.UUID, at: Any = None,
                        keep: Any = None, trip: uuid.UUID | None = None) -> uuid.UUID:  # fmt: skip
    record, digest = _record(kind="signed", traveler=str(traveler))
    return await conn.fetchval(
        "INSERT INTO crm.signature_event (kind, request_id, trip_id, traveler_id, access_id, evidence_record, "
        "evidence_sha256, envelope_enc, envelope_sha256, seal_alg, seal_key_ref, seal_value, occurred_at, keep_until) "
        "VALUES ('signed', $1, $2, $3, $4, $5, $6, $7, $8, 'dev-stub-hmac-sha256', 'dev-stub', $9, "
        "coalesce($10, now()), coalesce($11, current_date + 1095)) RETURNING event_id",
        uuid.uuid4(),
        trip or ids["trip"],
        traveler,
        ids["access_lead"],
        record,
        digest,
        _blob(),
        _hash(),
        _hash(),
        at,
        keep,
    )


async def _acceptance(conn: asyncpg.Connection, ids: dict[str, Any], *, at: Any = None, keep: Any = None,
                      trip: uuid.UUID | None = None, traveler: uuid.UUID | None = None) -> uuid.UUID:  # fmt: skip
    record, digest = _record(kind="terms")
    return await conn.fetchval(
        "INSERT INTO crm.terms_acceptance (trip_id, proposal_id, traveler_id, link_id, source, doc_id, doc_sha256, "
        "price_hash, typed_name_sha256, evidence_record, evidence_sha256, accepted_at, keep_until) "
        "VALUES ($1, $2, $3, $4, 'proposal', $5, $6, $7, $8, $9, $10, coalesce($11, now()), "
        "coalesce($12, current_date + 1095)) RETURNING acceptance_id",
        trip or ids["trip"],
        ids["proposal"],
        traveler or ids["lead"],
        ids["link"],
        ids["terms"],
        _hash(),
        ids["price_hash"],
        _hash(),
        record,
        digest,
        at,
        keep,
    )


async def test_the_evidence_tables_are_append_only_and_checked(app_pool: asyncpg.Pool) -> None:
    ids = await _lifecycle(app_pool)
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"], "member:test")
        event = await _signed_event(conn, ids, traveler=ids["lead"])
        acceptance = await _acceptance(conn, ids)
    for sql, key in (("UPDATE crm.signature_event SET reason = 'x' WHERE event_id = $1", event),
                     ("DELETE FROM crm.terms_acceptance WHERE acceptance_id = $1", acceptance)):  # fmt: skip
        with pytest.raises(asyncpg.PostgresError) as err:
            await _run(app_pool, ids["tenant"], sql, key)
        assert err.value.sqlstate == "42501"
    bad = [
        # The evidence hash must be the record's.
        "INSERT INTO crm.terms_acceptance (trip_id, proposal_id, source, doc_id, doc_sha256, typed_name_sha256, "
        "evidence_record, evidence_sha256, keep_until) VALUES ($1, $2, 'proposal', $3, $4, $4, '{}', $4, "
        "current_date + 1)",
        # A signed event carries its envelope and seal.
        "INSERT INTO crm.signature_event (kind, trip_id, traveler_id, access_id, keep_until) "
        "VALUES ('signed', $1, $2, $3, current_date)",
        # A refusal names its reason.
        "INSERT INTO crm.signature_event (kind, trip_id, traveler_id, access_id, keep_until) "
        "VALUES ('code_refused', $1, $2, $3, current_date)",
        # keep_until is never before the event.
        "INSERT INTO crm.signature_event (kind, trip_id, traveler_id, access_id, keep_until) "
        "VALUES ('code_sent', $1, $2, $3, current_date - 1)",
    ]
    args = [(ids["trip"], ids["proposal"], ids["terms"], _hash())] + [
        (ids["trip"], ids["lead"], ids["access_lead"])
    ] * 3
    for sql, a in zip(bad, args, strict=True):
        with pytest.raises(asyncpg.CheckViolationError):
            await _run(app_pool, ids["tenant"], sql, *a)
    await _run(app_pool, ids["tenant"],
               "INSERT INTO crm.signature_event (kind, trip_id, traveler_id, access_id, reason, keep_until) "
               "VALUES ('code_refused', $1, $2, $3, 'code_expired', current_date + 1)",
               ids["trip"], ids["lead"], ids["access_lead"])  # fmt: skip
    # Another agency sees none of it (FORCE RLS).
    other = uuid.uuid4()
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, other)
        for table in NEW_TABLES:
            assert await conn.fetchval(f"SELECT count(*) FROM crm.{table}") == 0  # noqa: S608


async def test_the_insurance_declination_kind(app_pool: asyncpg.Pool) -> None:
    ids = await _lifecycle(app_pool)
    doc = await _run(app_pool, ids["tenant"],
                     "INSERT INTO crm.agreement_doc (kind, version_label, body_text) "
                     "VALUES ('insurance_declination', 'v1', 'I decline insurance.') RETURNING doc_id")  # fmt: skip
    assert doc is not None
    with pytest.raises(asyncpg.CheckViolationError):
        await _run(app_pool, ids["tenant"], "INSERT INTO crm.agreement_doc (kind, version_label, body_text) "
                   "VALUES ('nda', 'v1', 'x')")  # fmt: skip


async def test_a_guardian_signs_from_their_own_access(app_pool: asyncpg.Pool) -> None:
    ids = await _lifecycle(app_pool)
    await _pay_deposit(app_pool, ids)
    insert = (
        "INSERT INTO crm.signature_request (trip_id, traveler_id, access_id, doc_id, signer_traveler_id, signer_role) "
        "VALUES ($1, $2, $3, $4, $5, $6) RETURNING request_id"
    )
    # The lead's access signs for the companion (a minor) as guardian.
    request = await _run(app_pool, ids["tenant"], insert, ids["trip"], ids["companion"], ids["access_lead"],
                         ids["waiver"], ids["lead"], "guardian")  # fmt: skip
    assert request is not None
    # The lead's access cannot sign for the companion as "self" (the access key names the signer).
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await _run(app_pool, ids["tenant"], insert, ids["trip"], ids["companion"], ids["access_lead"],
                   ids["terms"], None, "self")  # fmt: skip
    for signer, role in ((None, "guardian"), (ids["lead"], "self"), (ids["companion"], "guardian")):
        with pytest.raises(asyncpg.CheckViolationError):
            await _run(app_pool, ids["tenant"], insert, ids["trip"], ids["companion"],
                       ids["access_lead"] if signer != ids["companion"] else ids["access_companion"],
                       ids["terms"], signer, role)  # fmt: skip
    record, digest = _record(kind="v1")
    sign = (
        "UPDATE crm.signature_request SET status = 'signed', signed_at = now(), signed_name_enc = $2, doc_sha256 = $3, "
        "evidence_sha256 = $4, evidence_record = $5, code_delivery = 'dev_echo', insurance_choice = $6, "
        "envelope_sha256 = $7, seal_alg = 'dev-stub-hmac-sha256' WHERE request_id = $1"
    )
    # Declined without a declination version in the envelope: refused (R13).
    with pytest.raises(asyncpg.CheckViolationError):
        await _run(app_pool, ids["tenant"], sign, request, _blob(), _hash(), digest, record, "declined", _hash())
    await _run(app_pool, ids["tenant"], sign, request, _blob(), _hash(), digest, record, "insured", _hash())
    for change in ("envelope_sha256 = $2", "insurance_choice = 'declined', declination_doc_id = NULL",
                   "signer_role = 'self', signer_traveler_id = NULL"):  # fmt: skip
        args = (request, _hash()) if "$2" in change else (request,)
        with pytest.raises(asyncpg.PostgresError) as err:
            await _run(app_pool, ids["tenant"], f"UPDATE crm.signature_request SET {change} WHERE request_id = $1",
                       *args)  # fmt: skip
        assert err.value.sqlstate in {"CRM31", "23514"}, change


async def _evidence(app_pool: asyncpg.Pool, ids: dict[str, Any]) -> None:
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"], "member:test")
        await _signed_event(conn, ids, traveler=ids["lead"])
        await _signed_event(conn, ids, traveler=ids["companion"])
        await _acceptance(conn, ids)


async def test_a_trip_erase_keeps_the_signed_evidence(app_pool: asyncpg.Pool) -> None:
    ids = await _lifecycle(app_pool)
    await _evidence(app_pool, ids)
    done = await _erase(app_pool, ids["tenant"], "trip", ids["trip"])
    assert done["counts"]["signature_event_kept"] == 2 and done["counts"]["terms_acceptance_kept"] == 1
    assert await _count(app_pool, ids["tenant"], "trip") == 0
    assert await _count(app_pool, ids["tenant"], "signature_event") == 2
    assert await _count(app_pool, ids["tenant"], "terms_acceptance") == 1


async def test_a_traveller_erase_takes_their_guardian_signatures_and_keeps_the_evidence(app_pool: asyncpg.Pool) -> None:
    ids = await _lifecycle(app_pool)
    await _pay_deposit(app_pool, ids)
    await _run(app_pool, ids["tenant"],
               "INSERT INTO crm.signature_request (trip_id, traveler_id, access_id, doc_id, signer_traveler_id, "
               "signer_role) VALUES ($1, $2, $3, $4, $5, 'guardian')",
               ids["trip"], ids["companion"], ids["access_lead"], ids["waiver"], ids["lead"])  # fmt: skip
    await _evidence(app_pool, ids)
    await _run(app_pool, ids["tenant"],
               "UPDATE crm.trip SET deleted_at = now(), deleted_by = 'member:test', purge_after = now() "
               "WHERE trip_id = $1", ids["trip"])  # fmt: skip
    done = await _erase(app_pool, ids["tenant"], "traveler", ids["lead"], ids["token"])
    assert done["counts"]["signature_request"] == 1  # the companion's, signed by the lead as guardian
    assert done["counts"]["signature_event_kept"] == 1 and done["counts"]["terms_acceptance_kept"] == 1
    assert await _count(app_pool, ids["tenant"], "signature_event") == 2


async def test_the_agency_erase_takes_the_evidence(app_pool: asyncpg.Pool) -> None:
    ids = await _lifecycle(app_pool)
    await _evidence(app_pool, ids)
    await _run(app_pool, ids["tenant"], "UPDATE crm.tenant SET status = 'offboarding'")
    done = await _erase(app_pool, ids["tenant"], "tenant", ids["tenant"])
    assert done["counts"]["signature_event"] == 2 and done["counts"]["terms_acceptance"] == 1
    for table in NEW_TABLES:
        assert await _count(app_pool, ids["tenant"], table) == 0


async def _purge(pool: asyncpg.Pool, tenant: uuid.UUID, *, dry_run: bool = False) -> dict[str, int]:
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant)
        await conn.execute("SET LOCAL ROLE crm_eraser")
        return json.loads(await conn.fetchval("SELECT crm.lifecycle_purge_expired($1)", dry_run))


async def test_the_purge_after_the_claim_period(app_pool: asyncpg.Pool) -> None:
    ids = await _lifecycle(app_pool)
    long_ago = dt.datetime.now(dt.UTC) - dt.timedelta(days=4 * 365)
    past = (long_ago + dt.timedelta(days=3 * 365)).date()
    gone, held_traveller = uuid.uuid4(), uuid.uuid4()
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"], "member:test")
        # Past keep_until, trip gone: purged.
        await _signed_event(conn, ids, traveler=ids["lead"], at=long_ago, keep=past, trip=gone)
        await _acceptance(conn, ids, at=long_ago, keep=past, trip=gone)
        # Past keep_until but the trip is still there and ends within 3 years (moved later): kept.
        await _signed_event(conn, ids, traveler=ids["lead"], at=long_ago, keep=past)
        # Past keep_until, trip gone, the traveller is under a legal hold: kept.
        await _signed_event(conn, ids, traveler=held_traveller, at=long_ago, keep=past, trip=gone)
    await _hold(app_pool, ids["tenant"], "traveler", held_traveller, "member:m")
    dry = await _purge(app_pool, ids["tenant"], dry_run=True)
    assert (dry["signature_event"], dry["terms_acceptance"]) == (1, 1)
    assert await _count(app_pool, ids["tenant"], "signature_event") == 3
    done = await _purge(app_pool, ids["tenant"])
    assert (done["signature_event"], done["terms_acceptance"], done["held"]) == (1, 1, 0)
    assert await _count(app_pool, ids["tenant"], "signature_event") == 2
    assert await _count(app_pool, ids["tenant"], "terms_acceptance") == 0
