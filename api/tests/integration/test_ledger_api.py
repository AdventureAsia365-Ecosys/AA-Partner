"""AA-944 (card CRM-16) end to end on PostgreSQL 15 with the RDS role shape, as crm_app through ``tenant_tx()``, with
real field encryption and the synthetic FX job.

- **R**: under Strict a second confirmed line of one supplier with one confirmation number -> 409
  ``duplicate_confirmation_no``.
- **E**: an override for the confirmation day after the confirm leaves the line's base amounts and margin unchanged.
- The assistant: no money in lines or the ledger, no totals; a confirm without commission under Strict succeeds and
  opens the owner task "Add commission" (R18).
- The supplier directory: Adventure Asia exactly once, contact fields encrypted and revealed with a reason.
- ``waiting_on``: ``supplier`` while a booked trip has a planning agency line, then back to the base rule.
- The daily ``crm_fx_fetch`` (synthetic) fills ``crm.fx_rate_platform``, idempotently.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import asyncpg
import pytest

from app.errors import Conflict, Invalid
from app.fx import fetch
from app.jobs.queue import Job
from app.ledger import fx, lines, suppliers
from app.lifecycle import trash
from app.tenancy import tenant_tx
from app.vault import Vault
from tests.integration.test_crm005_intake import _agency, _keys

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _setup(pool: asyncpg.Pool) -> dict[str, Any]:
    tenant_id, people = await _agency(pool, "owner", "agent", "assistant")
    trip = uuid.uuid4()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        await conn.execute("SELECT set_config('app.actor', 'seed:test', true)")
        await conn.execute(
            "INSERT INTO crm.trip (trip_id, title, stage, waiting_on) "
            "VALUES ($1, 'Vietnam 9 nights', 'booked', 'none')",
            trip,
        )
    who = {role: people[i][0] for i, role in enumerate(("owner", "agent", "assistant"))}
    return {"tenant": tenant_id, "trip": trip, "who": who, "keys": _keys()}


async def _fx_today(pool: asyncpg.Pool) -> None:
    """The synthetic rates for today and yesterday (UTC): the agency's day in its timezone may be ahead of UTC."""
    for day_offset in (0, 1):
        day = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=day_offset)
        await fetch.store(pool, fetch.synthetic_rows(day))


async def test_the_ledger_end_to_end(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    owner, agent, assistant = s["who"]["owner"], s["who"]["agent"], s["who"]["assistant"]
    await _fx_today(app_pool)

    # Suppliers: AA exactly once, an own one with an encrypted contact, revealed with a reason.
    async with tenant_tx(app_pool, agent, s["tenant"]) as ctx:
        listed = await suppliers.list_suppliers(ctx)
        assert [x["kind"] for x in listed["suppliers"]] == ["aa"]
        own = await suppliers.create_supplier(
            ctx,
            Vault(s["keys"], ctx),
            {"name": "Hanoi Pearl", "category": "accommodation", "contact_name": "Lan", "default_commission_pct": 8},
        )
        assert own["has_contact_name"] and not own["has_contact_email"]
        shown = await suppliers.reveal_contact(
            ctx, Vault(s["keys"], ctx), uuid.UUID(own["agency_supplier_id"]), "contact_name", "prepare_booking"
        )
        assert shown["value"] == "Lan"
    async with tenant_tx(app_pool, agent, s["tenant"]) as ctx:
        assert len((await suppliers.list_suppliers(ctx))["suppliers"]) == 2
        enc = await ctx.conn.fetchval("SELECT contact_name_enc FROM crm.agency_supplier WHERE kind = 'own'")
        assert b"Lan" not in bytes(enc)
    supplier = uuid.UUID(own["agency_supplier_id"])

    # A planning line: the booked trip now waits on the supplier.
    async with tenant_tx(app_pool, agent, s["tenant"]) as ctx:
        line = await lines.create_line(ctx, s["trip"], {"agency_supplier_id": supplier, "title": "Hanoi 2 nights"})
        assert await ctx.conn.fetchval("SELECT waiting_on FROM crm.trip") == "supplier"
    line_id = uuid.UUID(line["booking_line_id"])

    # Basic: a confirm without number, cost and sell is 409 with the codes.
    with pytest.raises(Conflict) as err:
        async with tenant_tx(app_pool, agent, s["tenant"]) as ctx:
            await lines.confirm(ctx, line_id)
    assert err.value.code == "line_incomplete" and err.value.extra["missing"] == ["confirmation_no", "cost", "sell"]

    async with tenant_tx(app_pool, agent, s["tenant"]) as ctx:
        await lines.update_line(ctx, line_id, {"confirmation_no": "HP-1001"})
        money = await lines.put_money(
            ctx,
            line_id,
            {"cost": {"amount": "2540000", "currency": "VND"}, "sell": {"amount": "3048000", "currency": "VND"}},
        )
        assert money["money"]["cost"] == {"amount": "2540000.00", "currency": "VND"}  # planning: not frozen
        confirmed = await lines.confirm(ctx, line_id)
    assert confirmed["status"] == "confirmed" and confirmed["frozen"] == 2
    cost, sell = confirmed["money"]["cost"], confirmed["money"]["sell"]
    assert cost["amount_base"] == "100.00" and sell["amount_base"] == "120.00"
    assert cost["fx"]["source"] == "platform_daily" and confirmed["money"]["margin_base"] == "20.00"
    async with tenant_tx(app_pool, agent, s["tenant"]) as ctx:
        assert await ctx.conn.fetchval("SELECT waiting_on FROM crm.trip") == "none"

    # E: an override for the confirmation day later changes nothing stored.
    async with tenant_tx(app_pool, owner, s["tenant"]) as ctx:
        today = await fx.agency_today(ctx)
        await fx.add_override(ctx, base="USD", quote="VND", rate="20000", rate_date=today)
        again = await lines.line(ctx, line_id)
    assert again["money"]["margin_base"] == "20.00" and again["money"]["cost"]["amount_base"] == "100.00"

    # R: Strict refuses the same number twice for one supplier.
    async with tenant_tx(app_pool, owner, s["tenant"]) as ctx:
        await fx.update_payment_settings(ctx, {"booking_validation_level": "strict"})
        second = await lines.create_line(
            ctx, s["trip"], {"agency_supplier_id": supplier, "title": "Hanoi 1 night", "confirmation_no": "hp 1001"}
        )
        for kind in ("cost", "sell", "commission_expected"):
            await lines.put_money(
                ctx, uuid.UUID(second["booking_line_id"]), {kind: {"amount": "10", "currency": "USD"}}
            )
    with pytest.raises(Conflict) as err:
        async with tenant_tx(app_pool, owner, s["tenant"]) as ctx:
            await lines.confirm(ctx, uuid.UUID(second["booking_line_id"]))
    assert err.value.code == "duplicate_confirmation_no"

    # The assistant: no money, no totals; never blocked by a missing commission (owner task).
    async with tenant_tx(app_pool, agent, s["tenant"]) as ctx:
        third = await lines.create_line(
            ctx, s["trip"], {"agency_supplier_id": supplier, "title": "Boat", "confirmation_no": "B-7"}
        )
        await lines.put_money(
            ctx,
            uuid.UUID(third["booking_line_id"]),
            {"cost": {"amount": "50", "currency": "USD"}, "sell": {"amount": "70", "currency": "USD"}},
        )
    async with tenant_tx(app_pool, assistant, s["tenant"]) as ctx:
        seen = await lines.trip_lines(ctx, s["trip"])
        assert all("money" not in x for x in seen["lines"])
        ledger = await lines.ledger(ctx, status=None, date_from=None, date_to=None)
        assert "totals" not in ledger and len(ledger["lines"]) == 3
        done = await lines.confirm(ctx, uuid.UUID(third["booking_line_id"]))
        assert done["owner_task"]["kind"] == "add_commission" and done["owner_task"]["task_id"]
        assert await ctx.conn.fetchval("SELECT count(*) FROM crm.booking_line_money") == 0  # RLS: none for her
    async with tenant_tx(app_pool, owner, s["tenant"]) as ctx:
        totals = (await lines.ledger(ctx, status=None, date_from=None, date_to=None))["totals"]
        assert totals["confirmed_sell_base"] == "190.00" and totals["confirmed_margin_base"] == "40.00"
        assert totals["pipeline_sell_base"] == "10.00"  # the refused duplicate is still planning (USD)
        task_owner = await ctx.conn.fetchval("SELECT assignee_member_id FROM crm.task WHERE kind = 'add_commission'")
        assert task_owner is not None

    # Trash: a planning line goes, a confirmed one is cancelled first; the AA row never goes.
    async with tenant_tx(app_pool, agent, s["tenant"]) as ctx:
        await lines.trash_check(ctx, uuid.UUID(second["booking_line_id"]))
        await trash.move_to_trash(ctx, "booking_line", uuid.UUID(second["booking_line_id"]))
        with pytest.raises(Conflict):
            await lines.trash_check(ctx, line_id)
        aa = await ctx.conn.fetchval("SELECT agency_supplier_id FROM crm.agency_supplier WHERE kind = 'aa'")
        with pytest.raises(Conflict):
            await suppliers.trash_check(ctx, aa)
        with pytest.raises(Conflict):
            await suppliers.trash_check(ctx, supplier)  # live lines name it
        cancelled = await lines.cancel(ctx, line_id)
        assert cancelled["status"] == "cancelled" and cancelled["visible_to_travelers"] is False


async def test_a_money_write_by_the_assistant_is_refused_by_the_database(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        supplier = await suppliers.create_supplier(ctx, Vault(s["keys"], ctx), {"name": "Pearl"})
        line = await lines.create_line(
            ctx, s["trip"], {"agency_supplier_id": supplier["agency_supplier_id"], "title": "Hanoi"}
        )
    with pytest.raises(Exception) as err:  # noqa: PT011 — the service maps 42501 margin_forbidden to 403
        async with tenant_tx(app_pool, s["who"]["assistant"], s["tenant"]) as ctx:
            await lines.put_money(ctx, uuid.UUID(line["booking_line_id"]), {"cost": {"amount": "1", "currency": "USD"}})
    assert getattr(err.value, "code", None) == "role_forbidden"
    with pytest.raises(Invalid):
        async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
            await lines.create_line(ctx, s["trip"], {"agency_supplier_id": supplier["agency_supplier_id"], "title": ""})


async def test_the_synthetic_fx_job_fills_platform_rates_once(app_pool: asyncpg.Pool) -> None:
    day = dt.date(2026, 1, 15)
    handler = fetch.build_handler("synthetic", today=lambda: day)
    first = await handler(app_pool, Job("j", "crm_fx_fetch", {}, 1, 4))
    again = await handler(app_pool, Job("j", "crm_fx_fetch", {}, 1, 4))
    assert first["inserted_count"] in (first["rate_count"], 0) and again["inserted_count"] == 0
    async with app_pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT rate FROM crm.fx_rate_platform WHERE rate_date = $1 AND base = 'USD' AND quote = 'VND'", day
            )
            == fetch.SYNTHETIC_USD["VND"]
        )
