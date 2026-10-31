"""AA-966 (card CRM-34) on PostgreSQL 15 with the RDS role shape, as crm_app through ``tenant_tx()`` and
``platform_tx()``: the numbers of the Reports page reconcile with the rows under them.

- **Reconcile**: inquiries, the funnel, won, lead sources and Lost reasons equal direct counts over ``crm.trip`` /
  ``crm.trip_stage_history``; a trashed trip and an undone move to Booked never count; revenue, cost and margin equal
  the confirmed lines' money (a planning line never counts).
- **RLS**: another agency's trips and money never show; the assistant gets no money section and its money CSV is 403.
- **CSV**: a product title that starts with ``=`` is written as text (formula guard); the export is audited.
- **R28 E**: an agency whose first trip is more than a year old compares with the same period last year; a new
  agency gets the 3-month trend.
- **Outstanding** follows A-10 Receivables: an invoiced schedule row is not counted again, a partly paid invoice
  counts its balance once, a refund never reopens it; the open-invoice total equals the Receivables balances.
- **Module 17**: the platform read as ``crm_insights`` publishes a cell only with >= 10 travellers from >= 3
  agencies; an opted-out agency never contributes.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import uuid
from decimal import Decimal
from typing import Any

import asyncpg
import pytest

from app.errors import Forbidden
from app.invoices import service as invoices
from app.ledger import lines, suppliers
from app.ops import console
from app.payments import service as payments
from app.reports import export, service
from app.tenancy import tenant_tx
from app.vault import Vault
from tests.integration.test_crm005_intake import _agency, _keys
from tests.integration.test_invoices_api import _accepted, _as
from tests.integration.test_ledger_api import _fx_today

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _seed(pool: asyncpg.Pool, tenant: uuid.UUID, sql: str, *args: Any) -> Any:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
        await conn.execute("SELECT set_config('app.actor', 'seed:test', true)")
        return await conn.fetchval(sql, *args)


async def _trip(
    pool: asyncpg.Pool,
    tenant: uuid.UUID,
    stage: str,
    *,
    owner: uuid.UUID | None = None,
    source: str | None = None,
    budget: str | None = None,
    lost: str | None = None,
    cancel: str | None = None,
    created: dt.datetime | None = None,
    destinations: tuple[str, ...] = ("vietnam",),
    adults: int = 2,
    start: dt.date | None = None,
) -> uuid.UUID:
    trip = uuid.uuid4()
    await _seed(
        pool,
        tenant,
        "INSERT INTO crm.trip (trip_id, title, stage, waiting_on, owner_member_id, lead_source, budget_band, "
        "lost_reason_code, cancel_reason_code, created_at, destinations, party_adults, start_date) "
        "VALUES ($1, 'Lotus loop', $2, 'none', $3, $4, $5, $6, $7, coalesce($8, now()), $9, $10, $11)",
        trip, stage, owner, source, budget, lost, cancel, created, list(destinations), adults, start,
    )  # fmt: skip
    return trip


async def _move(pool: asyncpg.Pool, tenant: uuid.UUID, trip: uuid.UUID, to: str) -> uuid.UUID:
    return await _seed(
        pool,
        tenant,
        "INSERT INTO crm.trip_stage_history (trip_id, from_stage, to_stage, reason_code, override) "
        "VALUES ($1, 'inquiry', $2, 'manual', false) RETURNING history_id",
        trip,
        to,
    )


async def _undo(pool: asyncpg.Pool, tenant: uuid.UUID, trip: uuid.UUID, history: uuid.UUID) -> None:
    await _seed(
        pool,
        tenant,
        "INSERT INTO crm.trip_stage_history (trip_id, from_stage, to_stage, reason_code, override, undo_of) "
        "VALUES ($1, 'booked', 'proposal', 'undo', false, $2)",
        trip,
        history,
    )


async def _confirmed_line(
    pool: asyncpg.Pool, tenant: uuid.UUID, who: Any, trip: uuid.UUID, supplier: uuid.UUID, title: str,
    cost: str, sell: str, *, confirm: bool = True,
) -> None:  # fmt: skip
    async with tenant_tx(pool, who, tenant) as ctx:
        line = await lines.create_line(ctx, trip, {"agency_supplier_id": supplier, "title": title})
        line_id = uuid.UUID(line["booking_line_id"])
        await lines.update_line(ctx, line_id, {"confirmation_no": f"LP-{uuid.uuid4().hex[:6]}"})
        await lines.put_money(
            ctx, line_id, {"cost": {"amount": cost, "currency": "USD"}, "sell": {"amount": sell, "currency": "USD"}}
        )
        if confirm:
            await lines.confirm(ctx, line_id)


async def _setup(pool: asyncpg.Pool) -> dict[str, Any]:
    tenant, people = await _agency(pool, "owner", "agent", "assistant")  # synthetic by default (crm-002)
    who = {role: people[i][0] for i, role in enumerate(("owner", "agent", "assistant"))}
    ids = {role: people[i][1] for i, role in enumerate(("owner", "agent", "assistant"))}
    agent = ids["agent"]
    t = {
        "inquiry": await _trip(pool, tenant, "inquiry", owner=agent, source="website_form", budget="not_sure"),
        "qualifying": await _trip(pool, tenant, "qualifying", owner=agent, source="website_form", budget="1000_2000"),
        "booked": await _trip(pool, tenant, "booked", owner=ids["owner"], source="referral", budget="over_5000"),
        "lost": await _trip(pool, tenant, "lost", source="referral", lost="price"),
        "cancelled": await _trip(pool, tenant, "cancelled", source="website_form", cancel="client_request"),
        "undone": await _trip(pool, tenant, "proposal", owner=agent, source="website_form"),
        "trashed": await _trip(pool, tenant, "booked", source="website_form"),
    }
    await _move(pool, tenant, t["lost"], "proposal")
    await _move(pool, tenant, t["cancelled"], "booked")
    await _undo(pool, tenant, t["undone"], await _move(pool, tenant, t["undone"], "booked"))
    await _seed(
        pool,
        tenant,
        "UPDATE crm.trip SET deleted_at = now(), deleted_by = 'seed:test', purge_after = now() + interval '7 days' "
        "WHERE trip_id = $1",
        t["trashed"],
    )
    await _fx_today(pool)
    keys = _keys()
    async with tenant_tx(pool, who["agent"], tenant) as ctx:
        own = await suppliers.create_supplier(
            ctx, Vault(keys, ctx), {"name": "Lotus Lodge", "category": "accommodation"}
        )
    supplier = uuid.UUID(own["agency_supplier_id"])
    await _confirmed_line(pool, tenant, who["agent"], t["booked"], supplier, "=HYPERLINK(1)", "100", "130")
    await _confirmed_line(pool, tenant, who["agent"], t["booked"], supplier, "River dinner", "40", "50")
    await _confirmed_line(pool, tenant, who["agent"], t["booked"], supplier, "Not yet", "10", "99", confirm=False)

    # Another agency: everything here must stay invisible to the first one.
    other, other_people = await _agency(pool, "owner")
    for _ in range(3):
        await _trip(pool, other, "booked", source="other_agency_source")
    return {"tenant": tenant, "who": who, "ids": ids, "trips": t, "other": other, "other_owner": other_people[0][0]}


async def test_the_reports_reconcile_with_the_rows(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"]) as ctx:
        report = await service.summary(ctx, "30d", None)
        direct_created = await ctx.conn.fetchval("SELECT count(*) FROM crm.trip WHERE deleted_at IS NULL")
        direct_by_source = {
            r["lead_source"]: r["n"]
            for r in await ctx.conn.fetch(
                "SELECT lead_source, count(*) AS n FROM crm.trip WHERE deleted_at IS NULL GROUP BY 1"
            )
        }
        direct_sell = await ctx.conn.fetchval(
            "SELECT sum(m.amount_base) FROM crm.v_booking_line_money_current m "
            "JOIN crm.booking_line bl USING (booking_line_id) WHERE bl.status = 'confirmed' AND m.kind = 'sell'"
        )

    f = report["funnel"]
    assert f["created"] == direct_created == 6  # the trashed trip is not counted
    # Won: booked (current stage) and cancelled after booked; the undone move to booked does not count.
    assert f["won"] == 2 and f["win_rate_pct"] == pytest.approx(33.3)
    stages = {x["stage"]: x["count"] for x in f["stages"]}
    assert stages["inquiry"] == 6 and stages["qualifying"] == 5  # every cohort trip but the plain inquiry
    assert stages["proposal"] == 4 and stages["booked"] == 2 and stages["completed"] == 0
    assert f["lost"] == 1 and f["cancelled"] == 1
    sources = {x["source"]: x["trips"] for x in report["lead_sources"]}
    assert sources == direct_by_source == {"website_form": 4, "referral": 2}
    assert "other_agency_source" not in sources  # RLS
    assert report["outcomes"]["lost"] == [{"reason": "price", "count": 1}]
    assert report["outcomes"]["cancelled"] == [{"reason": "client_request", "count": 1}]

    money = report["money"]
    assert money["lines"] == 2  # the planning line never counts (R18)
    assert Decimal(money["sell_base"]) == direct_sell == Decimal("180.00")
    assert money["cost_base"] == "140.00" and money["margin_base"] == "40.00" and money["margin_pct"] == 22.2
    assert {p["title"] for p in money["top_products"]} == {"=HYPERLINK(1)", "River dinner"}
    tiles = {x["key"]: x for x in report["tiles"]}
    assert tiles["inquiries"]["value"] == 6 and tiles["revenue"]["value"] == "180.00"
    assert tiles["margin"]["value"] == "40.00" and tiles["aa_share"]["value"] == 0.0
    assert report["period"]["compare"] == "trend_3m" and len(tiles["inquiries"]["trend"]) == 3
    assert tiles["inquiries"]["trend"][-1]["value"] == 6  # this month holds every trip
    assert report["insights_included"] is True
    team = {m["member_id"]: m for m in report["team"]["members"]}
    assert team[str(s["ids"]["agent"])]["open_trips"] == 3 and team[str(s["ids"]["agent"])]["new_trips"] == 3
    assert report["team"]["unassigned_open_trips"] == 0
    metrics = {m["key"]: m for m in report["success"]}
    assert metrics["budget_band_share"]["value"] == pytest.approx(33.3)  # 2 of 6 gave a real band
    assert metrics["margin_known_share"]["value"] == 100.0 and metrics["commission_unclaimed"]["status"] == "not_built"
    assert isinstance(metrics["ops_release_errors"]["value"], int)

    # The owner filter keeps the agent's trips only.
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"]) as ctx:
        mine = await service.summary(ctx, "30d", str(s["ids"]["agent"]))
    assert mine["funnel"]["created"] == 3 and mine["money"]["lines"] == 0

    # The assistant: no money anywhere, AA share by lines, the money CSV refused (and audited).
    async with tenant_tx(app_pool, s["who"]["assistant"], s["tenant"]) as ctx:
        seen = await service.summary(ctx, "30d", None)
    assert seen["money"] is None and seen["money_visible"] is False
    assert {x["key"] for x in seen["tiles"]} == {"inquiries", "win_rate", "aa_share"}
    assert {p["title"] for p in seen["top_products"]} == {"=HYPERLINK(1)", "River dinner"}
    assert "margin_known_share" not in {m["key"] for m in seen["success"]}
    with pytest.raises(Forbidden):
        async with tenant_tx(app_pool, s["who"]["assistant"], s["tenant"]) as ctx:
            await export.export(ctx, "30d", None, "money_by_month")

    # CSV: the formula guard, and the download leaves a report_export event.
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        body, count = await export.export(ctx, "30d", None, "top_products")
    rows = list(csv.reader(io.StringIO(body.decode("utf-8-sig"))))
    assert count == 2 and rows[0] == ["supplier", "product", "lines", "trips", "currency", "sell"]
    assert ["agency", "'=HYPERLINK(1)", "1", "1", "USD", "130.00"] in rows
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"]) as ctx:
        body, _ = await export.export(ctx, "30d", None, "funnel")
        audited = await ctx.conn.fetchval("SELECT count(*) FROM crm.audit_event WHERE action = 'report_export'")
        again = await service.summary(ctx, "30d", None)
    assert audited == 1  # the agent's download (the owner's own event is written when its block ends)
    assert {m["key"]: m["value"] for m in again["success"]}["report_exports"] == 1
    assert ["booked", "2"] in list(csv.reader(io.StringIO(body.decode("utf-8-sig"))))


async def test_an_agency_with_a_year_of_history_compares_with_last_year(app_pool: asyncpg.Pool) -> None:
    tenant, people = await _agency(app_pool, "owner")
    now = dt.datetime.now(dt.UTC)
    await _trip(app_pool, tenant, "inquiry", created=now - dt.timedelta(days=760))
    await _trip(app_pool, tenant, "inquiry", created=now - dt.timedelta(days=365))
    await _trip(app_pool, tenant, "inquiry")
    await _trip(app_pool, tenant, "inquiry")
    async with tenant_tx(app_pool, people[0][0], tenant) as ctx:
        report = await service.summary(ctx, "30d", None)
    tile = {x["key"]: x for x in report["tiles"]}["inquiries"]
    assert report["period"]["compare"] == "previous_year"
    assert tile["value"] == 2 and tile["previous"] == 1 and tile["delta"] == 100.0 and tile["trend"] == []


async def test_outstanding_counts_each_amount_once_and_matches_receivables(app_pool: asyncpg.Pool) -> None:
    s = await _accepted(app_pool)  # proposal 1000 USD: deposit row 300, balance row 700, both due
    draft = await _as(app_pool, s, "agent", lambda ctx: invoices.create_from_schedule(ctx, s["trip"], s["deposit"]))
    deposit = uuid.UUID(draft["invoice_id"])
    await _as(app_pool, s, "agent", lambda ctx: invoices.issue(ctx, deposit))
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        paid = await payments.record_payment(ctx, Vault(s["keys"], ctx), deposit, amount=Decimal("100"),
                                             method="bank_transfer", received_on=None, reference="TT 0007")  # fmt: skip
    payment_id = uuid.UUID(paid["invoice"]["payments"][0]["payment_id"])
    # A partial refund of that payment: the Receivables balance stays 200 (a refund never reopens an invoice).
    asked = await _as(app_pool, s, "agent", lambda ctx: payments.request_refund(ctx, payment_id, amount=Decimal("40"),
                                                                               reason="Goodwill"))  # fmt: skip
    refund_id = uuid.UUID(asked["refund_id"])
    await _as(app_pool, s, "owner", lambda ctx: payments.decide_refund(ctx, refund_id, approve=True, note=None))
    await _as(app_pool, s, "agent", lambda ctx: payments.execute_refund(ctx, refund_id, method="bank_transfer",
                                                                       refunded_on=None))  # fmt: skip

    report = await _as(app_pool, s, "owner", lambda ctx: service.summary(ctx, "30d", None))
    listed = await _as(app_pool, s, "owner", lambda ctx: invoices.receivables(ctx, None))
    owed = report["payments"]
    assert [(i["currency"], i["count"], i["paid"], i["outstanding"]) for i in owed["open_invoices"]] == [
        ("USD", 1, "100.00", "200.00")
    ]
    receivable = sum(Decimal(r["balance_amount"]) for r in listed["invoices"] if r["currency"] == "USD")
    assert Decimal(owed["open_invoices"][0]["outstanding"]) == receivable == Decimal("200")
    # The invoiced deposit row is not listed again: only the balance row, not invoiced yet.
    assert [(r["kind"], r["total"]) for r in owed["schedule_due"]] == [("balance", "700.00")]
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"]) as ctx:
        body, count = await export.export(ctx, "30d", None, "outstanding")
    rows = list(csv.reader(io.StringIO(body.decode("utf-8-sig"))))
    assert count == 2 and sum(Decimal(r[6]) for r in rows[1:]) == Decimal("900")  # 200 owed + 700 not invoiced


async def test_insights_publish_only_big_enough_cells_and_skip_opted_out_agencies(app_pool: asyncpg.Pool) -> None:
    month = dt.date.today().replace(day=1) + dt.timedelta(days=400)
    month = month.replace(day=1)
    agencies = []
    for _ in range(4):
        tenant, _people = await _agency(app_pool, "owner")
        # 4 travellers per agency to a common destination, 2 to an agency-only one.
        await _trip(app_pool, tenant, "inquiry", destinations=("khmer_coast",), adults=4, start=month)
        await _trip(app_pool, tenant, "inquiry", destinations=(f"only_{'abcd'[len(agencies)]}",), adults=2, start=month)
        agencies.append(tenant)
    # The fourth agency opted out: three agencies x 4 travellers = 12 >= 10, still published.
    await console.set_insights_opt_out(
        app_pool, agencies[3], staff_sub="staff-test", opt_out=True, owner_request_ref="SUP-1234"
    )
    result = await console.platform_insights(
        app_pool, staff_sub="staff-test", dimension="destination", month_from=month, month_to=month,
        include_synthetic=True,
    )  # fmt: skip
    cells = {c["bucket"]: c for c in result["cells"]}
    assert cells["khmer_coast"]["agency_count"] == 3 and cells["khmer_coast"]["traveller_count"] == 12
    assert not any(b.startswith("only_") for b in cells)  # one agency each: suppressed
    assert result["suppressed_cells"] >= 3 and result["agencies_opted_out"] >= 1
    assert "tenant_id" not in str(result)
    # Without synthetic agencies (prod) nothing of these counts.
    result = await console.platform_insights(
        app_pool, staff_sub="staff-test", dimension="destination", month_from=month, month_to=month,
        include_synthetic=False,
    )  # fmt: skip
    assert "khmer_coast" not in {c["bucket"] for c in result["cells"]}


async def test_insights_count_a_trip_once_outside_the_destination_dimension(app_pool: asyncpg.Pool) -> None:
    """Review S261: three agencies, each one 1-traveller trip to four places. Per destination that is four rows a trip;
    counted per row, the budget band cell would read 12 travellers and be published. It holds 3: suppressed."""
    month = dt.date(2032, 8, 1)
    places = ("amber_reach", "salt_point", "fern_hollow", "cloud_steps")
    for _ in range(3):
        tenant, _people = await _agency(app_pool, "owner")
        await _trip(app_pool, tenant, "inquiry", budget="over_5000", destinations=places, adults=1, start=month)
    out = await console.platform_insights(
        app_pool, staff_sub="staff-test", dimension="budget_band", month_from=month, month_to=month,
        include_synthetic=True,
    )  # fmt: skip
    assert "over_5000" not in {c["bucket"] for c in out["cells"]} and out["suppressed_cells"] >= 1
    # A fourth and fifth agency with 4 travellers each: 3 + 8 = 11 >= 10 from 5 agencies, published with the real count.
    for _ in range(2):
        tenant, _people = await _agency(app_pool, "owner")
        await _trip(app_pool, tenant, "inquiry", budget="over_5000", destinations=places, adults=4, start=month)
    out = await console.platform_insights(
        app_pool, staff_sub="staff-test", dimension="budget_band", month_from=month, month_to=month,
        include_synthetic=True,
    )  # fmt: skip
    cells = {c["bucket"]: c for c in out["cells"]}
    assert cells["over_5000"] == {"bucket": "over_5000", "traveller_count": 11, "agency_count": 5}
    # Per destination each trip still counts in each of its places: 11 travellers in every one of the four.
    out = await console.platform_insights(
        app_pool, staff_sub="staff-test", dimension="destination", month_from=month, month_to=month,
        include_synthetic=True,
    )  # fmt: skip
    assert {c["bucket"]: c["traveller_count"] for c in out["cells"] if c["bucket"] in places} == dict.fromkeys(
        places, 11
    )
