"""AA-1070 item crm-13 end to end on PostgreSQL with the RDS role shape (crm-060), as crm_app through ``tenant_tx()`` /
``system_tx()`` / the client link with real field encryption: Adventure Asia cancels a departure.

- **Feed**: ``departure.cancelled`` opens one case per affected trip (a booked trip with an AA line on it, a trip whose
  sent builder version has the tour on it), one urgent owner task and the workflow event; the same event replayed or
  another event for the same departure opens nothing new. The pipeline flag and filter, the trip page card.
- **(b) Cancel**: the reason ``aa_cancelled`` on a flagged trip forces the traveller charge to 0 (no owner needed even
  above the refund threshold, whatever the dialog sent), prefills a full refund of every payment as a ``proposed``
  refund request, records a ``waived`` charge with reason ``aa_departure_cancelled`` and closes the case. No audit
  reason carries an amount.
- **(a) Re-quote**: a trip before booking gets a new builder version (the AA option moved to the picked departure,
  its quote cleared, day 1 moved); a trip with an accepted version gets a new trip in Proposal with the party and the
  version. Idempotent.
- Another agency sees none of it.

Invented names and places only.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from decimal import Decimal
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from app import pipeline, stages
from app.booking import contract
from app.booking.sync import apply_event
from app.errors import Invalid
from app.permissions import Action
from app.proposals import aa_options, builder, service
from app.tenancy import system_tx, tenant_tx
from app.travel_ops import aa_cancel
from app.vault import Vault
from app.workflows import service as workflows
from tests.aa_fakes import FakeBooking, booking_client
from tests.integration.conftest import OWNER
from tests.integration.test_aa1067_retail_payments_api import _accepted_text, _paid_deposit, _policy, _rows
from tests.integration.test_builder_api import _as, _block, _pid, _publishable
from tests.integration.test_builder_api import _setup as _builder_setup
from tests.integration.test_client_proposal_api import _accept, _hold
from tests.integration.test_lifecycle_api import _setup as _lc_setup

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

D = Decimal
DEP_A = "dep-2027-orchid-a"
DEP_B = "dep-2027-orchid-b"
DEP_C = "dep-2027-orchid-c"
TOUR = "T-1070C"


FEED_TENANTS: list[uuid.UUID] = []


@pytest_asyncio.fixture(autouse=True)
async def _nothing_left_for_later_files(admin: asyncpg.Connection) -> Any:
    """The AA tour row (a platform table) and the Booking work of the agencies this file makes must not outlive it:
    the catalog cache test of test_aa_lines_api.py counts every cached tour CIS no longer lists, and crm_booking_sync
    calls Booking for every agency with a feed row, a pending request or an accepted hold set to write."""
    yield
    async with admin.transaction():
        await admin.execute(f"SET LOCAL ROLE {OWNER}")
        await admin.execute("DELETE FROM crm.aa_catalog_tour WHERE source_system = 'cis' AND source_id = $1", TOUR)
        for tenant in FEED_TENANTS:
            await admin.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
            await admin.execute("DELETE FROM crm.booking_feed_state")
            await admin.execute("UPDATE crm.aa_request SET status = 'cancelled', finished_at = now(), "
                                "result_code = 'test_done' WHERE status = 'pending'")  # fmt: skip
            await admin.execute("UPDATE crm.trip SET deleted_at = now(), deleted_by = 'seed:test', purge_after = now() "
                                "WHERE deleted_at IS NULL AND trip_id IN (SELECT trip_id FROM crm.proposal "
                                "WHERE accepted_hold_id IS NOT NULL)")  # fmt: skip
    FEED_TENANTS.clear()


async def _feed_row(pool: asyncpg.Pool, s: dict[str, Any]) -> None:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(s["tenant"]))
        await conn.execute("SELECT set_config('app.actor', 'job:test', true)")
        await conn.execute("INSERT INTO crm.booking_feed_state DEFAULT VALUES ON CONFLICT DO NOTHING")
    FEED_TENANTS.append(s["tenant"])


async def _cancelled(pool: asyncpg.Pool, s: dict[str, Any], departure: str, event: str | None = None) -> Any:
    ev = contract.event({"event_id": event or f"ev_{uuid.uuid4().hex[:12]}", "type": "departure.cancelled",
                         "departure_id": departure, "occurred_at": "2026-11-12T03:00:00Z"})  # fmt: skip
    async with system_tx(pool, s["tenant"], actor="job:crm_booking_sync") as ctx:
        return await apply_event(ctx, ev)


async def _aa_line(pool: asyncpg.Pool, s: dict[str, Any], departure: str, booking: str) -> uuid.UUID:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(s["tenant"]))
        await conn.execute("SELECT set_config('app.actor', 'job:test', true)")
        line = await conn.fetchval(
            "INSERT INTO crm.booking_line (trip_id, supplier_kind, agency_supplier_id, title, departure_id, "
            "service_from, service_to) VALUES ($1, 'aa', crm.ensure_aa_supplier(), 'Orchid river 4 days', $2, "
            "current_date + 10, current_date + 13) RETURNING booking_line_id",
            s["trip"],
            departure,
        )
        await conn.execute("UPDATE crm.booking_line SET status = 'confirmed', confirmed_at = now(), bk_booking_id = $2 "
                           "WHERE booking_line_id = $1", line, booking)  # fmt: skip
        return line


async def test_the_feed_opens_one_case_per_trip_once_and_flags_it(app_pool: asyncpg.Pool) -> None:
    s = await _lc_setup(app_pool)
    await _accepted_text(app_pool, s)
    await _paid_deposit(app_pool, s)  # Booked
    await _aa_line(app_pool, s, DEP_C, "BKG-ORC-1")
    await _feed_row(app_pool, s)
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"], action=Action.WORKFLOW_EDIT) as ctx:
        rule = await workflows.install(ctx, "aa_departure_cancelled_notice", vault=Vault(s["keys"], ctx),
                                       starter_mail=True)  # fmt: skip
        assert rule["status"] == "paused"  # opt-in: the owner switches it on
        await workflows.set_status(ctx, uuid.UUID(rule["workflow_rule_id"]), "active")
    assert await _cancelled(app_pool, s, DEP_C, "ev_cancel_c1") == ("applied", "departure_cancelled")
    # The same departure again (another event id) and an older-than-stored event: nothing new.
    await _cancelled(app_pool, s, DEP_C)
    await _cancelled(app_pool, s, DEP_C, "ev_cancel_c1")
    cases = await _rows(app_pool, s, "SELECT trip_id, departure_id, resolution, opened_event_id "
                        "FROM crm.aa_cancel_case")  # fmt: skip
    assert [(r["trip_id"], r["departure_id"], r["resolution"], r["opened_event_id"]) for r in cases] == [
        (s["trip"], DEP_C, None, "ev_cancel_c1")]  # fmt: skip
    tasks = await _rows(app_pool, s, "SELECT priority, status FROM crm.task WHERE kind = 'aa_departure_cancelled'")
    assert [(t["priority"], t["status"]) for t in tasks] == [("urgent", "open")]
    runs = await _rows(app_pool, s, "SELECT trigger_key FROM crm.workflow_run")
    assert len(runs) == 1 and runs[0]["trigger_key"].startswith("event:")
    async with tenant_tx(app_pool, s["who"]["assistant"], s["tenant"]) as ctx:
        vault = Vault(s["keys"], ctx)
        board = await pipeline.board(ctx, vault, pipeline.BoardFilter(issue="aa_cancelled"))
        detail = await pipeline.trip_detail(ctx, vault, s["trip"])
    shown = [c for st in board["stages"] for c in st["cards"]]
    assert [c["trip_id"] for c in shown] == [str(s["trip"])] and shown[0]["aa_cancelled"] is True
    (card,) = detail["aa_cancel"]["cases"]
    assert card["active"] and card["can_cancel"] and card["requote_mode"] == "new_trip"
    assert card["tour"]["title"] == "Orchid river 4 days" and card["departure_id"] == DEP_C
    # Another agency sees none of it.
    other = await _lc_setup(app_pool)
    async with tenant_tx(app_pool, other["who"]["owner"], other["tenant"]) as ctx:
        assert await ctx.conn.fetchval("SELECT count(*) FROM crm.aa_cancel_case") == 0
        assert await aa_cancel.flagged(ctx, [s["trip"]]) == set()


async def test_cancel_after_aa_cancelled_forces_no_charge_and_full_refunds(app_pool: asyncpg.Pool) -> None:
    s = await _lc_setup(app_pool)
    await _policy(app_pool, s)
    await _accepted_text(app_pool, s)
    await _paid_deposit(app_pool, s)
    # Ten days before departure: the policy would charge 50 % (500), and the refund threshold is lowered to 50.
    await _rows(app_pool, s, "UPDATE crm.trip SET start_date = current_date + 10")
    await _rows(app_pool, s, "UPDATE crm.tenant_settings SET refund_owner_threshold_base = 50")
    await _aa_line(app_pool, s, DEP_C, "BKG-ORC-2")
    await _feed_row(app_pool, s)
    await _cancelled(app_pool, s, DEP_C)

    async def cancel(dry_run: bool, reason: str = "aa_cancelled", **kw: Any) -> dict[str, Any]:
        async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"], action=Action.TRIP_CANCEL) as ctx:
            out = await stages.cancel(ctx, s["trip"], reason=reason, dry_run=dry_run, **kw)
        json.dumps(out, default=str)
        return out

    other_reason = (await cancel(True, "client_request"))["charge"]
    assert other_reason["charge_amount"] == "500.00" and not other_reason["forced"]  # only the AA reason forces it
    preview = await cancel(True, charge={"mode": "override", "amount": "400.00", "reason": "Ignored"})
    c = preview["charge"]
    assert (c["decision"], c["charge_amount"], c["forced"], c["needs_owner"], c["reason"]) == (
        "waived", "0.00", True, False, "aa_departure_cancelled")  # fmt: skip
    assert [p["refund_amount"] for p in preview["payments"]] == ["300.00"]  # prefilled in full
    out = await cancel(False)
    assert out["charge"]["charge_amount"] == "0.00"
    refunds = await _rows(app_pool, s, "SELECT amount, status, reason FROM crm.refund_request")
    assert [(r["amount"], r["status"]) for r in refunds] == [(D("300.00"), "proposed")]  # a draft, never sent
    assert "aa_departure_cancelled" in refunds[0]["reason"]
    charge = await _rows(app_pool, s, "SELECT decision, charge_amount, reason, invoice_id FROM crm.cancellation_charge")
    assert [(r["decision"], r["charge_amount"], r["reason"], r["invoice_id"]) for r in charge] == [
        ("waived", D("0.00"), "aa_departure_cancelled", None)]  # fmt: skip
    cases = await _rows(app_pool, s, "SELECT resolution, resolved_by FROM crm.aa_cancel_case")
    assert cases[0]["resolution"] == "cancelled" and cases[0]["resolved_by"].startswith("member:")
    asks = await _rows(app_pool, s, "SELECT kind FROM crm.aa_request WHERE status = 'pending'")
    assert [a["kind"] for a in asks] == ["cancel"]  # Booking's reason departure_cancelled: no fee
    reasons = await _rows(app_pool, s, "SELECT reason FROM crm.audit_event WHERE reason IS NOT NULL")
    assert not any(re.search(r"\b(300|500|400)(\.00)?\b", r["reason"]) for r in reasons)
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        assert await aa_cancel.flagged(ctx, [s["trip"]]) == set()


async def _aa_option(pool: asyncpg.Pool, s: dict[str, Any], fake: FakeBooking, v: dict[str, Any]) -> dict[str, Any]:
    day2 = next(d for d in v["days"] if d["day_no"] == 2)
    v = await _as(pool, s, "agent", builder.add_block, _pid(v), v["edit_seq"], uuid.UUID(day2["proposal_day_id"]),
                  {"title": "River tour", "type": "activity"})  # fmt: skip
    body = {"tour_source_id": TOUR, "component_id": "tp:1", "component_kind": "trip_plan", "departure_id": DEP_A,
            "sell_amount": D("500")}  # fmt: skip
    async with tenant_tx(pool, s["who"]["agent"], s["tenant"]) as ctx:
        plan = await builder.aa_plan(ctx, _pid(v), v["edit_seq"], uuid.UUID(_block(v, 2, 1)["option_block_id"]), body)
    quote = await aa_options.quote(booking_client(fake), s["tenant"], plan)
    return await _as(pool, s, "agent", builder.add_aa_option, plan, quote, body)


async def _sent_with_tour(pool: asyncpg.Pool) -> dict[str, Any]:
    s = await _builder_setup(pool)
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO crm.aa_catalog_tour (source_id, title, country_code, duration_days, served_state) "
            "VALUES ($1, 'Orchid river 4 days', 'LA', 4, 'served') "
            "ON CONFLICT (source_system, source_id) DO UPDATE SET served_state = 'served'",
            TOUR,
        )
    fake = FakeBooking(b2b_price="300.00", availability="available")
    v = await _aa_option(pool, s, fake, await _publishable(pool, s))
    v = await _as(pool, s, "agent", builder.publish, _pid(v), v["edit_seq"], acknowledge_selection_seq=None)
    async with tenant_tx(pool, s["who"]["agent"], s["tenant"]) as ctx:
        sent = await service.send(ctx, Vault(s["keys"], ctx), _pid(v))
    s.update(fake=fake, v=v, token=sent["link_path"].rsplit("/", 1)[1])
    await _feed_row(pool, s)
    return s


async def _requote(pool: asyncpg.Pool, s: dict[str, Any], case: str, start: dt.date, dep: str = DEP_B) -> Any:
    async with tenant_tx(pool, s["who"]["agent"], s["tenant"], action=Action.PROPOSAL_DRAFT) as ctx:
        return await aa_cancel.requote(ctx, Vault(s["keys"], ctx), s["trip"], uuid.UUID(case), departure_id=dep,
                                       start_date=start)  # fmt: skip


async def _case_id(pool: asyncpg.Pool, s: dict[str, Any]) -> str:
    rows = await _rows(pool, s, "SELECT aa_cancel_case_id FROM crm.aa_cancel_case WHERE trip_id = $1", s["trip"])
    return str(rows[0]["aa_cancel_case_id"])


async def test_requote_before_booking_makes_a_new_version_on_the_picked_departure(app_pool: asyncpg.Pool) -> None:
    s = await _sent_with_tour(app_pool)
    await _cancelled(app_pool, s, DEP_A)  # the sent version has the tour on it
    case = await _case_id(app_pool, s)
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        view = await aa_cancel.trip_view(ctx, s["trip"], "proposal")
    assert view["cases"][0]["requote_mode"] == "version" and view["cases"][0]["can_requote"]
    assert not view["cases"][0]["can_cancel"]  # not booked: nothing to cancel at AA
    with pytest.raises(Invalid) as err:
        await _requote(app_pool, s, case, s["start"], DEP_A)
    assert err.value.code == "same_departure"
    new_start = s["start"] + dt.timedelta(days=14)
    out = await _requote(app_pool, s, case, new_start)
    assert out["mode"] == "version" and out["trip_id"] == str(s["trip"])
    assert await _requote(app_pool, s, case, new_start) == out  # idempotent
    rows = await _rows(app_pool, s, "SELECT version, status, starts_on, based_on_proposal_id FROM crm.proposal "
                       "WHERE proposal_id = $1", uuid.UUID(out["proposal_id"]))  # fmt: skip
    # The tour is on day 2: day 1 is the day before the picked departure.
    assert (rows[0]["version"], rows[0]["status"], rows[0]["starts_on"], rows[0]["based_on_proposal_id"]) == (
        2, "draft", new_start - dt.timedelta(days=1), _pid(s["v"]))  # fmt: skip
    opts = await _rows(app_pool, s, "SELECT aa_departure_id, aa_quote_id FROM crm.option WHERE proposal_id = $1 "
                       "AND source = 'aa'", uuid.UUID(out["proposal_id"]))  # fmt: skip
    assert [(o["aa_departure_id"], o["aa_quote_id"]) for o in opts] == [(DEP_B, None)]
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        assert await aa_cancel.flagged(ctx, [s["trip"]]) == set()  # settled: the new version goes out as usual


async def test_requote_after_acceptance_opens_a_new_trip(app_pool: asyncpg.Pool) -> None:
    s = await _sent_with_tour(app_pool)
    _status, held = await _hold(app_pool, s)
    assert (await _accept(app_pool, s, held["page"]))["status"] == "accepted"
    await _cancelled(app_pool, s, DEP_A)
    case = await _case_id(app_pool, s)
    new_start = s["start"] + dt.timedelta(days=21)
    out = await _requote(app_pool, s, case, new_start)
    assert out["mode"] == "new_trip" and out["trip_id"] != str(s["trip"])
    new_trip = uuid.UUID(out["trip_id"])
    trips = await _rows(app_pool, s, "SELECT stage, start_date, end_date, title FROM crm.trip WHERE trip_id = $1",
                        new_trip)  # fmt: skip
    assert (trips[0]["stage"], trips[0]["start_date"], trips[0]["end_date"]) == (
        "proposal", new_start, new_start + dt.timedelta(days=4))  # fmt: skip
    party = await _rows(app_pool, s, "SELECT role FROM crm.trip_traveler WHERE trip_id = $1", new_trip)
    assert [p["role"] for p in party] == ["lead"]
    history = await _rows(app_pool, s, "SELECT reason_code FROM crm.trip_stage_history WHERE trip_id = $1", new_trip)
    assert [h["reason_code"] for h in history] == ["aa_rebook"]
    props = await _rows(app_pool, s, "SELECT version, status, based_on_proposal_id FROM crm.proposal "
                        "WHERE trip_id = $1", new_trip)  # fmt: skip
    assert [(p["version"], p["status"], p["based_on_proposal_id"]) for p in props] == [(1, "draft", None)]
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        # The old trip stays flagged until it is cancelled (step b); the new one names where it came from.
        assert await aa_cancel.flagged(ctx, [s["trip"]]) == {s["trip"]}
        assert (await aa_cancel.trip_view(ctx, new_trip, "proposal"))["rebooked_from"] == str(s["trip"])
        view = await builder.view(ctx, Vault(s["keys"], ctx), uuid.UUID(out["proposal_id"]))
    aa = [o for d in view["days"] for b in d["blocks"] for o in b["options"] if o["source"] == "aa"]
    assert len(aa) == 1 and aa[0]["aa"]["departure_id"] == DEP_B
    # The case is settled: a second re-quote answers what it made, never a second trip.
    assert await _requote(app_pool, s, case, new_start, "dep-2027-orchid-z") == out
