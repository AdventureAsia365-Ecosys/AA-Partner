"""crm-056 (AA-1071, a contract file) on PostgreSQL with the RDS role shape: sales partners and their commission.

- ``crm.sales_partner``: the terms CHECK (percent <= 100, a currency exactly for fixed_per_pax), owner-only writes
  (CRM54), cascades from the agency's settings row.
- ``crm.partner_commission`` + ``crm.partner_commission_sync`` and the trip trigger: a booked trip with a partner
  accrues one row (revenue = the accepted proposal total, margin = the P&L margin before partner commissions, fixed
  per traveller); an accrued row follows the partner and the trip; an approved row is never touched; the guard keeps
  the identity and amounts and gives approve / pay to the owner; the assistant reads no row; the P&L counts it.
- ``crm.sales_partner_link`` (lookup policy, counters only for its own actor) and ``crm.sales_partner_otp`` (no member
  writes it).
- ``crm.dispatch_queue`` / ``crm.conversation``: exactly one subject; a partner row is a transactional e-mail.
- Tenant isolation: another agency sees nothing.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from tests.integration.conftest import OWNER, READERS

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

CRM056 = Path(__file__).resolve().parents[2] / "migrations" / "056_crm_sales_partners.sql"


@pytest_asyncio.fixture(autouse=True)
async def _crm056_view(admin: asyncpg.Connection) -> AsyncIterator[None]:
    """crm-056 again (re-runnable): an earlier test of the session may have re-run crm-051, whose crm.v_trip_pnl has
    no partner commission (a re-run DB Migrate never does on Dev)."""
    await admin.execute(f"SET ROLE {OWNER}")
    try:
        await admin.execute(CRM056.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")
    yield


NEW_TABLES = ("sales_partner", "partner_commission", "sales_partner_link", "sales_partner_otp")


def _sealed() -> bytes:
    return os.urandom(40)  # shaped like a sealed value; never opened here


async def _as_owner(admin: asyncpg.Connection, tenant: uuid.UUID, sql: str, *args: Any) -> Any:
    """One statement as crm_owner inside the agency (FORCE RLS still applies to the owner)."""
    async with admin.transaction():
        await admin.execute(f"SET LOCAL ROLE {OWNER}")
        await admin.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
        await admin.execute("SELECT set_config('app.actor', 'seed:test', true)")
        return await admin.fetchval(sql, *args)


async def _as_app(
    pool: asyncpg.Pool, tenant: uuid.UUID, sql: str, *args: Any, role: str = "owner", actor: str = "member:test"
) -> Any:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
        await conn.execute("SELECT set_config('app.actor', $1, true)", actor)
        await conn.execute("SELECT set_config('app.member_role', $1, true)", role)
        return await conn.fetchval(sql, *args)


async def _agency(admin: asyncpg.Connection) -> uuid.UUID:
    tenant = uuid.uuid4()
    await _as_owner(admin, tenant, "INSERT INTO crm.tenant (tenant_id) VALUES ($1)", tenant)
    await _as_owner(admin, tenant, "INSERT INTO crm.tenant_settings (tenant_id) VALUES ($1)", tenant)
    return tenant


async def _partner(admin: asyncpg.Connection, tenant: uuid.UUID, **kw: Any) -> uuid.UUID:
    return await _as_owner(
        admin, tenant,
        "INSERT INTO crm.sales_partner (kind, name_enc, contact_email_enc, commission_type, rate, currency, basis) "
        "VALUES ($1, $2, $3, $4, $5::numeric, $6, $7) RETURNING sales_partner_id",
        kw.get("kind", "sub_agent"), _sealed(), _sealed(), kw.get("type", "percent"), kw.get("rate", "10"),
        kw.get("currency"), kw.get("basis", "revenue"),
    )  # fmt: skip


async def _trip(admin: asyncpg.Connection, tenant: uuid.UUID, stage: str = "booked", adults: int = 2) -> uuid.UUID:
    trip = uuid.uuid4()
    await _as_owner(
        admin, tenant,
        "INSERT INTO crm.trip (trip_id, title, stage, waiting_on, party_adults, party_child_ages, start_date, "
        "end_date) VALUES ($1, 'Lagoon loop 6 days', $2, 'none', $3, ARRAY[7]::smallint[], DATE '2027-03-01', "
        "DATE '2027-03-06')",
        trip, stage, adults,
    )  # fmt: skip
    return trip


async def _accepted_proposal(admin: asyncpg.Connection, tenant: uuid.UUID, trip: uuid.UUID, total: str) -> None:
    proposal = await _as_owner(
        admin, tenant,
        "INSERT INTO crm.proposal (trip_id, version, body_enc, currency, total_amount, deposit_amount, balance_due_on) "
        "VALUES ($1, 1, $2, 'USD', $3::numeric, 100, current_date + 60) RETURNING proposal_id",
        trip, _sealed(), total,
    )  # fmt: skip
    await _as_owner(
        admin, tenant,
        "UPDATE crm.proposal SET status = 'sent', price_hash = $2, sent_at = now() WHERE proposal_id = $1",
        proposal, hashlib.sha256(os.urandom(8)).digest(),
    )  # fmt: skip
    await _as_owner(
        admin, tenant,
        "UPDATE crm.proposal SET status = 'accepted', accepted_at = now(), accepted_name_enc = $2, "
        "accepted_price_hash = price_hash WHERE proposal_id = $1",
        proposal, _sealed(),
    )  # fmt: skip


async def _rows(admin: asyncpg.Connection, tenant: uuid.UUID, trip: uuid.UUID) -> list[dict[str, Any]]:
    async with admin.transaction():
        await admin.execute(f"SET LOCAL ROLE {OWNER}")
        await admin.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
        rows = await admin.fetch(
            "SELECT * FROM crm.partner_commission WHERE trip_id = $1 ORDER BY created_at, partner_commission_id", trip
        )
        return [dict(r) for r in rows]


async def _set_partner(
    admin: asyncpg.Connection, tenant: uuid.UUID, trip: uuid.UUID, partner: uuid.UUID | None
) -> None:
    await _as_owner(admin, tenant, "UPDATE crm.trip SET sales_partner_id = $2 WHERE trip_id = $1", trip, partner)


@pytest_asyncio.fixture
async def tenant(admin: asyncpg.Connection) -> AsyncIterator[uuid.UUID]:
    yield await _agency(admin)


async def test_crm056_is_registered(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-056'") == 1


async def test_no_reader_role_reads_the_partner_tables(admin: asyncpg.Connection) -> None:
    for table in NEW_TABLES:
        for reader in READERS:
            assert not await admin.fetchval("SELECT has_table_privilege($1, $2, 'SELECT')", reader, f"crm.{table}"), (
                reader,
                table,
            )


async def test_partner_terms_check(admin: asyncpg.Connection, tenant: uuid.UUID) -> None:
    with pytest.raises(asyncpg.CheckViolationError):
        await _partner(admin, tenant, rate="120")  # a percent above 100
    with pytest.raises(asyncpg.CheckViolationError):
        await _partner(admin, tenant, type="fixed_per_pax", rate="25")  # no currency
    with pytest.raises(asyncpg.CheckViolationError):
        await _partner(admin, tenant, rate="10", currency="USD")  # a currency on a percent
    assert await _partner(admin, tenant, type="fixed_per_pax", rate="250", currency="USD")


async def test_only_an_owner_writes_a_partner(
    admin: asyncpg.Connection, app_pool: asyncpg.Pool, tenant: uuid.UUID
) -> None:
    sql = "INSERT INTO crm.sales_partner (kind, name_enc, rate) VALUES ('referrer', $1, 5) RETURNING sales_partner_id"
    for role in ("agent", "assistant"):
        with pytest.raises(asyncpg.PostgresError) as err:
            await _as_app(app_pool, tenant, sql, _sealed(), role=role)
        assert err.value.sqlstate == "CRM54"
    assert await _as_app(app_pool, tenant, sql, _sealed(), role="owner")


async def test_a_booked_trip_accrues_the_revenue_commission(admin: asyncpg.Connection, tenant: uuid.UUID) -> None:
    partner = await _partner(admin, tenant, rate="10")
    trip = await _trip(admin, tenant)
    await _accepted_proposal(admin, tenant, trip, "2450.00")
    await _set_partner(admin, tenant, trip, partner)
    [row] = await _rows(admin, tenant, trip)
    assert (row["status"], row["basis"], row["basis_amount"], row["amount"], row["currency"]) == (
        "accrued", "revenue", D("2450.00"), D("245.00"), "USD",
    )  # fmt: skip
    assert (row["base_currency"], row["fx_rate"], row["amount_base"]) == ("USD", D(1), D("245.00"))
    assert row["due_on"].isoformat() == "2027-04-05"  # trip end + 30 days
    # The P&L counts it as a cost.
    async with admin.transaction():
        await admin.execute(f"SET LOCAL ROLE {OWNER}")
        await admin.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
        pnl = await admin.fetchrow(
            "SELECT sales_partner_id, partner_commission_base, total_cost_base FROM crm.v_trip_pnl WHERE trip_id = $1",
            trip,
        )
    assert (pnl["sales_partner_id"], pnl["partner_commission_base"]) == (partner, D("245.00"))
    assert pnl["total_cost_base"] == D("245.00")


async def test_fixed_per_pax_and_margin_bases(admin: asyncpg.Connection, tenant: uuid.UUID) -> None:
    per_pax = await _partner(admin, tenant, type="fixed_per_pax", rate="40", currency="USD")
    trip = await _trip(admin, tenant, adults=3)
    await _set_partner(admin, tenant, trip, per_pax)
    [row] = await _rows(admin, tenant, trip)
    assert (row["basis"], row["pax"], row["basis_amount"], row["amount"]) == ("pax", 4, D(4), D("160.00"))
    margin = await _partner(admin, tenant, rate="20", basis="margin")
    trip2 = await _trip(admin, tenant)
    await _set_partner(admin, tenant, trip2, margin)
    [row2] = await _rows(admin, tenant, trip2)
    assert (row2["basis"], row2["basis_amount"], row2["amount"]) == ("margin", D(0), D("0.00"))  # nothing invoiced yet


async def test_the_accrued_row_follows_the_partner_and_the_trip(admin: asyncpg.Connection, tenant: uuid.UUID) -> None:
    first = await _partner(admin, tenant, rate="10")
    second = await _partner(admin, tenant, rate="5")
    trip = await _trip(admin, tenant)
    await _accepted_proposal(admin, tenant, trip, "1000")
    await _set_partner(admin, tenant, trip, first)
    await _set_partner(admin, tenant, trip, second)
    rows = await _rows(admin, tenant, trip)
    assert [(r["sales_partner_id"], r["status"], r["cancel_reason"], r["amount"]) for r in rows] == [
        (first, "cancelled", "partner_changed", D("100.00")),
        (second, "accrued", None, D("50.00")),
    ]
    await _set_partner(admin, tenant, trip, None)
    assert [r["cancel_reason"] for r in await _rows(admin, tenant, trip)] == ["partner_changed", "partner_removed"]
    # Not booked yet: nothing accrues; the move to booked does.
    lead = await _trip(admin, tenant, stage="proposal")
    await _set_partner(admin, tenant, lead, first)
    assert await _rows(admin, tenant, lead) == []
    # Trashed: the accrued row goes (unbooked).
    await _set_partner(admin, tenant, trip, first)
    await _as_owner(
        admin, tenant,
        "UPDATE crm.trip SET deleted_at = now(), deleted_by = 'member:test', purge_after = now() + interval '7 days' "
        "WHERE trip_id = $1",
        trip,
    )  # fmt: skip
    assert (await _rows(admin, tenant, trip))[-1]["cancel_reason"] == "unbooked"


async def test_the_guard_keeps_amounts_and_gives_approval_to_the_owner(
    admin: asyncpg.Connection, app_pool: asyncpg.Pool, tenant: uuid.UUID
) -> None:
    partner = await _partner(admin, tenant, rate="10")
    trip = await _trip(admin, tenant)
    await _accepted_proposal(admin, tenant, trip, "800")
    await _set_partner(admin, tenant, trip, partner)
    [row] = await _rows(admin, tenant, trip)
    pc = row["partner_commission_id"]
    approve = (
        "UPDATE crm.partner_commission SET status = 'approved', approved_at = now(), approved_by = $2 "
        "WHERE partner_commission_id = $1 RETURNING status"
    )
    with pytest.raises(asyncpg.PostgresError) as err:
        await _as_app(app_pool, tenant, approve, pc, "member:test", role="agent")
    assert err.value.sqlstate == "CRM54"
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await _as_app(
            app_pool, tenant, "UPDATE crm.partner_commission SET amount = 1 WHERE partner_commission_id = $1", pc
        )
    with pytest.raises(asyncpg.PostgresError) as err:  # an owner approves as themselves
        await _as_app(app_pool, tenant, approve, pc, "member:someone-else")
    assert err.value.sqlstate == "CRM54"
    assert await _as_app(app_pool, tenant, approve, pc, "member:test") == "approved"
    # An approved row is never touched by the sync (the proposal total changes, the partner terms change).
    await _as_owner(admin, tenant, "UPDATE crm.sales_partner SET rate = 50 WHERE sales_partner_id = $1", partner)
    await _as_owner(admin, tenant, "SELECT crm.partner_commission_sync($1)", trip)
    assert (await _rows(admin, tenant, trip))[0]["amount"] == D("80.00")
    pay = (
        "UPDATE crm.partner_commission SET status = 'paid', paid_on = current_date, payout_ref = 'BANK-17' "
        "WHERE partner_commission_id = $1 RETURNING status"
    )
    assert await _as_app(app_pool, tenant, pay, pc) == "paid"
    with pytest.raises(asyncpg.PostgresError) as err:
        await _as_app(
            app_pool, tenant, "UPDATE crm.partner_commission SET payout_ref = 'X' WHERE partner_commission_id = $1", pc
        )
    assert err.value.sqlstate == "CRM54"  # sealed
    # The assistant reads no partner commission (margin field); the agent does.
    count = "SELECT count(*) FROM crm.partner_commission"
    assert await _as_app(app_pool, tenant, count, role="assistant") == 0
    assert await _as_app(app_pool, tenant, count, role="agent") == 1


async def test_a_trip_cancel_is_an_owner_or_agent_cancel(
    admin: asyncpg.Connection, app_pool: asyncpg.Pool, tenant: uuid.UUID
) -> None:
    partner = await _partner(admin, tenant, rate="10")
    trip = await _trip(admin, tenant)
    await _set_partner(admin, tenant, trip, partner)
    [row] = await _rows(admin, tenant, trip)
    cancel = (
        "UPDATE crm.partner_commission SET status = 'cancelled', cancel_reason = $2 "
        "WHERE partner_commission_id = $1 RETURNING status"
    )
    with pytest.raises(asyncpg.PostgresError) as err:
        await _as_app(app_pool, tenant, cancel, row["partner_commission_id"], "by_owner", role="agent")
    assert err.value.sqlstate == "CRM54"
    assert await _as_app(app_pool, tenant, cancel, row["partner_commission_id"], "trip_cancelled", role="agent") == (
        "cancelled"
    )


async def test_a_waived_commission_is_not_brought_back(
    admin: asyncpg.Connection, app_pool: asyncpg.Pool, tenant: uuid.UUID
) -> None:
    """An owner's cancel (by_owner) and a pending trip cancel (trip_cancelled) are waivers: a later stage move, the
    trash and restore or a direct sync never accrue that partner's commission again on the trip, until the trip has
    left the booked stages since."""
    cancel = (
        "UPDATE crm.partner_commission SET status = 'cancelled', cancel_reason = $2 "
        "WHERE partner_commission_id = $1 RETURNING status"
    )
    stage = "UPDATE crm.trip SET stage = $2 WHERE trip_id = $1"
    for reason, role in (("by_owner", "owner"), ("trip_cancelled", "agent")):
        partner = await _partner(admin, tenant, rate="10")
        trip = await _trip(admin, tenant)
        await _accepted_proposal(admin, tenant, trip, "600")
        await _set_partner(admin, tenant, trip, partner)
        [row] = await _rows(admin, tenant, trip)
        assert await _as_app(app_pool, tenant, cancel, row["partner_commission_id"], reason, role=role) == "cancelled"
        await _as_owner(admin, tenant, stage, trip, "travelling")  # the stage-rules job's date moves
        await _as_owner(admin, tenant, stage, trip, "completed")
        await _as_owner(
            admin, tenant,
            "UPDATE crm.trip SET deleted_at = now(), deleted_by = 'member:test', "
            "purge_after = now() + interval '7 days' WHERE trip_id = $1",
            trip,
        )  # fmt: skip
        await _as_owner(
            admin, tenant,
            "UPDATE crm.trip SET deleted_at = NULL, deleted_by = NULL, purge_after = NULL WHERE trip_id = $1",
            trip,
        )  # fmt: skip
        await _as_owner(admin, tenant, "SELECT crm.partner_commission_sync($1)", trip)
        assert [(r["status"], r["cancel_reason"]) for r in await _rows(admin, tenant, trip)] == [
            ("cancelled", reason)
        ], reason
    # A move out of the booked stages after the waiver (a new booking) lets the next sync accrue again.
    await _as_owner(
        admin, tenant,
        "INSERT INTO crm.trip_stage_history (trip_id, from_stage, to_stage, reason_code) "
        "VALUES ($1, 'completed', 'proposal', 'undo_test')",
        trip,
    )  # fmt: skip
    await _as_owner(admin, tenant, "SELECT crm.partner_commission_sync($1)", trip)
    rows = await _rows(admin, tenant, trip)
    assert [(r["status"], r["cancel_reason"]) for r in rows] == [("cancelled", "trip_cancelled"), ("accrued", None)]


async def test_a_partner_delete_never_takes_an_approved_or_paid_commission(
    admin: asyncpg.Connection, app_pool: asyncpg.Pool, tenant: uuid.UUID
) -> None:
    first = await _partner(admin, tenant, rate="10")
    second = await _partner(admin, tenant, rate="5")
    trip = await _trip(admin, tenant)
    await _set_partner(admin, tenant, trip, first)
    [row] = await _rows(admin, tenant, trip)
    approve = (
        "UPDATE crm.partner_commission SET status = 'approved', approved_at = now(), approved_by = 'member:test' "
        "WHERE partner_commission_id = $1 RETURNING status"
    )
    assert await _as_app(app_pool, tenant, approve, row["partner_commission_id"]) == "approved"
    # The trip moves to the second partner; the first partner's approved row stays (the sync never touches it).
    await _set_partner(admin, tenant, trip, second)
    delete = "DELETE FROM crm.sales_partner WHERE sales_partner_id = $1 RETURNING sales_partner_id"
    with pytest.raises(asyncpg.PostgresError) as err:
        await _as_app(app_pool, tenant, delete, first)
    assert err.value.sqlstate == "CRM54" and "partner_commission_kept" in str(err.value)
    with pytest.raises(asyncpg.PostgresError) as err:  # the owner role (a migration, a script) is refused too
        await _as_owner(admin, tenant, delete, first)
    assert err.value.sqlstate == "CRM54"
    assert [r["status"] for r in await _rows(admin, tenant, trip)] == ["approved", "accrued"]
    # The trip's own erase takes it (the trip goes first; no proposal here to erase), then the partner leaves.
    await _as_owner(admin, tenant, "DELETE FROM crm.trip WHERE trip_id = $1", trip)
    assert await _as_app(app_pool, tenant, delete, first) == first


async def test_partner_links_and_codes(admin: asyncpg.Connection, app_pool: asyncpg.Pool, tenant: uuid.UUID) -> None:
    partner = await _partner(admin, tenant)
    token_hash = hashlib.sha256(os.urandom(16)).digest()
    link = await _as_app(
        app_pool, tenant,
        "INSERT INTO crm.sales_partner_link (sales_partner_id, purpose, token_hash, expires_at) "
        "VALUES ($1, 'view', $2, now() + interval '90 days') RETURNING link_id",
        partner, token_hash,
    )  # fmt: skip
    with pytest.raises(asyncpg.CheckViolationError):  # an intake link names its form
        await _as_owner(
            admin, tenant,
            "INSERT INTO crm.sales_partner_link (sales_partner_id, purpose, token_hash, expires_at) "
            "VALUES ($1, 'intake', $2, now() + interval '9 days')",
            partner, hashlib.sha256(os.urandom(16)).digest(),
        )  # fmt: skip
    # Found by its token hash before the agency is known.
    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.partner_link_hash', $1, true)", token_hash.hex())
        assert await conn.fetchval("SELECT tenant_id FROM crm.sales_partner_link") == tenant
    # Its page touches only its counters.
    actor = f"sales_partner_link:{link}"
    assert await _as_app(
        app_pool, tenant,
        "UPDATE crm.sales_partner_link SET use_count = use_count + 1, last_used_at = now() WHERE link_id = $1 "
        "RETURNING use_count",
        link, role="", actor=actor,
    ) == 1  # fmt: skip
    with pytest.raises(asyncpg.PostgresError) as err:
        await _as_app(
            app_pool, tenant,
            "UPDATE crm.sales_partner_link SET status = 'revoked', revoked_at = now() WHERE link_id = $1",
            link, role="", actor=actor,
        )  # fmt: skip
    assert err.value.sqlstate == "CRM54"
    code = (
        "INSERT INTO crm.sales_partner_otp (sales_partner_id, link_id, code_hash, delivery, expires_at) "
        "VALUES ($1, $2, $3, 'dev_echo', now() + interval '10 minutes') RETURNING otp_id"
    )
    assert await _as_app(app_pool, tenant, code, partner, link, os.urandom(32), role="", actor=actor)
    with pytest.raises(asyncpg.PostgresError) as err:  # never a member
        await _as_app(app_pool, tenant, code, partner, link, os.urandom(32), role="owner")
    assert err.value.sqlstate == "CRM54"
    # Revoked by the owner: the lookup no longer finds it.
    await _as_app(
        app_pool,
        tenant,
        "UPDATE crm.sales_partner_link SET status = 'revoked', revoked_at = now() WHERE link_id = $1",
        link,
    )
    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.partner_link_hash', $1, true)", token_hash.hex())
        assert await conn.fetchval("SELECT count(*) FROM crm.sales_partner_link") == 0


async def test_a_dispatch_row_has_exactly_one_subject(admin: asyncpg.Connection, tenant: uuid.UUID) -> None:
    partner = await _partner(admin, tenant)
    conversation = await _as_owner(
        admin, tenant,
        "INSERT INTO crm.conversation (channel, sales_partner_id) VALUES ('email', $1) RETURNING conversation_id",
        partner,
    )  # fmt: skip
    assert conversation
    with pytest.raises(asyncpg.CheckViolationError):  # a partner thread has no trip
        await _as_owner(
            admin, tenant, "INSERT INTO crm.conversation (channel, sales_partner_id) VALUES ('sms', $1)", partner
        )
    row = (
        "INSERT INTO crm.dispatch_queue (traveler_id, sales_partner_id, channel, purpose, origin) "
        "VALUES ($1, $2, 'email', $3, 'system') RETURNING dispatch_id"
    )
    assert await _as_owner(admin, tenant, row, None, partner, "transactional")
    with pytest.raises(asyncpg.CheckViolationError):
        await _as_owner(admin, tenant, row, None, None, "transactional")
    with pytest.raises(asyncpg.CheckViolationError):
        await _as_owner(admin, tenant, row, None, partner, "marketing")
    # Clean-up: older files re-run by test_crm_migrations never see a partner row.
    await _as_owner(admin, tenant, "DELETE FROM crm.dispatch_queue WHERE sales_partner_id = $1", partner)
    await _as_owner(admin, tenant, "DELETE FROM crm.conversation WHERE sales_partner_id = $1", partner)


async def test_the_agency_erase_takes_every_partner_row(admin: asyncpg.Connection, tenant: uuid.UUID) -> None:
    partner = await _partner(admin, tenant)
    trip = await _trip(admin, tenant)
    await _set_partner(admin, tenant, trip, partner)
    # A paid payout record leaves with its trip (crm.erase_subject deletes the trips before the settings row).
    await _as_owner(
        admin, tenant,
        "UPDATE crm.partner_commission SET status = 'approved', approved_at = now(), approved_by = 'member:test'",
    )  # fmt: skip
    await _as_owner(admin, tenant, "UPDATE crm.partner_commission SET status = 'paid', paid_on = current_date")
    await _as_owner(admin, tenant, "DELETE FROM crm.trip WHERE trip_id = $1", trip)
    assert await _as_owner(admin, tenant, "SELECT count(*) FROM crm.partner_commission") == 0
    await _as_owner(admin, tenant, "DELETE FROM crm.tenant_settings WHERE tenant_id = $1", tenant)
    assert await _as_owner(admin, tenant, "SELECT count(*) FROM crm.sales_partner") == 0


async def test_another_agency_sees_nothing(
    admin: asyncpg.Connection, app_pool: asyncpg.Pool, tenant: uuid.UUID
) -> None:
    await _partner(admin, tenant)
    other = await _agency(admin)
    assert await _as_app(app_pool, other, "SELECT count(*) FROM crm.sales_partner") == 0
    # A trip names a partner of its own agency only (the key is on (tenant_id, sales_partner_id)).
    mine = await _as_owner(admin, tenant, "SELECT sales_partner_id FROM crm.sales_partner LIMIT 1")
    trip = await _trip(admin, other)
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await _set_partner(admin, other, trip, mine)
