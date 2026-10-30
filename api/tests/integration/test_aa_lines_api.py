"""AA-947 (card CRM-19) end to end on PostgreSQL 15 with the RDS role shape, as crm_app through ``tenant_tx()`` and
``system_tx()``, against an in-memory Booking and CIS (``tests/aa_fakes.py``).

- **R**: editing an AA line -> 403 ``aa_line_read_only`` (title, money, cancel, confirm, trash), and the database
  refuses a member's direct write (crm-014 CRM24).
- **E**: Booking down -> the line reads "AA pending" (the request stays pending with a backoff and its error class);
  Booking back -> one line, one quote; a hold whose answer was lost is retried with the same Idempotency-Key and
  Booking's one hold is the line's (no duplicates).
- Lines only from quote / hold / feed: the title comes from the catalog cache, the money from the quote with the
  quote's FX lock (``aa_quote_lock``), the "from"-price warning, confirm -> on request -> the feed confirms it; a
  replayed feed page applies nothing twice.
- AA-aware Lost: a declined line pre-selects ``aa_sold_out`` and opens the owner task; moving to Lost queues the
  release after the Undo window, Undo cancels it, and a later Lost releases the hold in Booking. A request still
  waiting for Booking survives an Undo (it runs as asked) and is dropped (``trip_closed``) when the Lost stays.
- Races and outages: a hold that lands after another run released its line is given back to Booking; Booking down
  costs one timeout per agency and run (the rest of the batch and the feed wait).
- The catalog sync: served, changed, retired (lines read "no longer offered"), paused.
"""

from __future__ import annotations

import uuid
from collections import Counter
from typing import Any

import asyncpg
import pytest

from app.booking import aa_lines
from app.booking.sync import BookingSync
from app.catalog.sync import CatalogSync
from app.errors import Conflict, Forbidden
from app.jobs.queue import Job
from app.ledger import lines
from app.lifecycle import trash
from app.stages import move, undo
from app.tenancy import tenant_tx
from tests.aa_fakes import FakeBooking, FakeCis, booking_client, catalog_config, tour_body
from tests.integration.test_crm005_intake import _agency

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

JOB = Job("00000000-0000-4000-8000-000000000001", "crm_booking_sync", {}, 1, 3)


async def _setup(pool: asyncpg.Pool, stage: str = "booked") -> dict[str, Any]:
    tenant_id, people = await _agency(pool, "owner", "agent", "assistant")
    trip = uuid.uuid4()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        await conn.execute("SELECT set_config('app.actor', 'seed:test', true)")
        await conn.execute(
            "INSERT INTO crm.trip (trip_id, title, stage, waiting_on, owner_member_id) "
            "VALUES ($1, 'Mekong for two', $2, 'none', $3)",
            trip,
            stage,
            people[0][1],
        )
    who = {role: people[i][0] for i, role in enumerate(("owner", "agent", "assistant"))}
    cis = FakeCis(tours={"T-100": tour_body()})
    await CatalogSync(catalog_config(cis))(pool, JOB)
    return {"tenant": tenant_id, "trip": trip, "who": who}


def _body(**over: Any) -> dict[str, Any]:
    body = {
        "tour_source_id": "T-100",
        "component_id": "tp:1",
        "component_kind": "trip_plan",
        "service_date": "2026-12-01",
        "pax": {"adult": 2, "child": 0, "infant": 0},
        "currency": "USD",
        "sell_amount": "1500",
    }
    return {**body, **over}


async def _sync(pool: asyncpg.Pool, fake: FakeBooking, tenant: uuid.UUID, *, feed: bool = True) -> dict[str, Any]:
    payload = {} if feed else {"tenant_id": str(tenant)}
    return await BookingSync(booking_client(fake))(pool, Job(JOB.id, JOB.kind, payload, 1, 3))


async def _line(pool: asyncpg.Pool, s: dict[str, Any], line_id: uuid.UUID, role: str = "owner") -> dict[str, Any]:
    async with tenant_tx(pool, s["who"][role], s["tenant"]) as ctx:
        return await lines.line(ctx, line_id)


async def _due_now(pool: asyncpg.Pool, tenant: uuid.UUID) -> None:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
        await conn.execute("SELECT set_config('app.actor', 'job:test', true)")
        await conn.execute("UPDATE crm.aa_request SET next_attempt_at = now() WHERE status = 'pending'")


async def _scalar(pool: asyncpg.Pool, tenant: uuid.UUID, sql: str, *args: Any) -> Any:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
        await conn.execute("SELECT set_config('app.actor', 'job:test', true)")
        return await conn.fetchval(sql, *args)


async def test_an_aa_line_from_quote_hold_confirm_and_feed(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    fake = FakeBooking()
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        made = await aa_lines.request_line(ctx, s["trip"], _body())
    line_id = uuid.UUID(made["booking_line_id"])
    assert made["supplier_kind"] == "aa" and made["read_only"] and made["title"] == "Mekong Delta 3 days"
    assert made["aa"]["sub_status"] == "pending" and made["aa"]["pending"]["kind"] == "quote"
    assert made["service_from"] == "2026-12-01" and made["service_to"] == "2026-12-03"

    await _sync(app_pool, fake, s["tenant"])
    quoted = await _line(app_pool, s, line_id)
    assert quoted["aa"]["sub_status"] == "quoted" and quoted["aa"]["pending"] is None
    assert quoted["money"]["cost"]["amount"] == "1000.00" and quoted["money"]["cost"]["fx"]["source"] == "aa_quote_lock"
    assert quoted["money"]["sell"]["amount"] == "1500.00" and quoted["money"]["margin_base"] == "500.00"
    # "From" price 800 per person; the sell is 750 per person: a warning, not a block.
    assert quoted["aa"]["from_price"] == {"amount": "800.00", "currency": "USD"} and quoted["aa"]["below_from_price"]
    assert quoted["aa"]["actions"] == ["hold", "release", "requote"]
    assert set(fake.tenants) == {str(s["tenant"])}

    # The assistant sees the line but no money and no margin.
    assistant_view = await _line(app_pool, s, line_id, "assistant")
    assert "money" not in assistant_view and assistant_view["aa"]["sub_status"] == "quoted"

    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        await aa_lines.ask(ctx, line_id, "hold")
        with pytest.raises(Conflict) as err:
            await aa_lines.ask(ctx, line_id, "hold")
    assert err.value.code == "aa_request_pending"
    await _sync(app_pool, fake, s["tenant"])
    held = await _line(app_pool, s, line_id)
    assert held["aa"]["sub_status"] == "held" and held["aa"]["hold_id"] == "h1"
    assert await _scalar(
        app_pool, s["tenant"], "SELECT line_status FROM crm.trip_commitment WHERE source_ref = 'h1'"
    ) == ("held")
    assert await _scalar(app_pool, s["tenant"], "SELECT crm.trip_delete_blockers($1)", s["trip"]) == ["has_aa_line"]

    # A confirm books with AA: the assistant is refused (403, audited, nothing queued); the agent may.
    with pytest.raises(Forbidden) as denied:
        async with tenant_tx(app_pool, s["who"]["assistant"], s["tenant"]) as ctx:
            await aa_lines.ask(ctx, line_id, "confirm")
    assert denied.value.code == "role_forbidden" and denied.value.status_code == 403
    assert await _scalar(
        app_pool, s["tenant"], "SELECT count(*) FROM crm.aa_request WHERE booking_line_id = $1 AND kind = 'confirm'",
        line_id,
    ) == 0  # fmt: skip
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        await aa_lines.ask(ctx, line_id, "confirm")
    await _sync(app_pool, fake, s["tenant"])
    on_request = await _line(app_pool, s, line_id)
    assert on_request["status"] == "planning" and on_request["aa"]["sub_status"] == "on_request"
    assert on_request["confirmation_no"] == "TC-0001" and on_request["confirm_class"] == "aa_on_request"

    # The feed confirms it; the same page again changes nothing.
    fake.add_event("line.confirmed", hold_id="h1", booking_id="b-h1", line_ref=str(line_id))
    fake.add_event("departure.incident", departure_id="d1", incident_code="weather")
    fake.add_event("x.future_type")
    await _sync(app_pool, fake, s["tenant"])
    confirmed = await _line(app_pool, s, line_id)
    assert confirmed["status"] == "confirmed" and confirmed["confirmed_at"] is not None
    outcomes = await _scalar(
        app_pool, s["tenant"], "SELECT array_agg(reason_code ORDER BY idempotency_key) FROM crm.inbox_events"
    )
    assert outcomes == ["line_confirmed", "departure_incident", "unknown_type"]  # AA-961: the incident is a flag now
    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(s["tenant"]))
        await conn.execute("SELECT set_config('app.actor', 'job:test', true)")
        await conn.execute("UPDATE crm.booking_feed_state SET feed_cursor = NULL")
    result = await _sync(app_pool, fake, s["tenant"])
    assert result["replayed_count"] == 3
    assert await _scalar(app_pool, s["tenant"], "SELECT count(*) FROM crm.inbox_events") == 3
    assert fake.calls["POST holds"] == 2  # the hold and the confirm (POST /holds/h1/confirm)


async def test_r_editing_an_aa_line_is_refused(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    fake = FakeBooking()
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"]) as ctx:
        line_id = uuid.UUID((await aa_lines.request_line(ctx, s["trip"], _body()))["booking_line_id"])
    await _sync(app_pool, fake, s["tenant"])
    for call in (
        lambda ctx: lines.update_line(ctx, line_id, {"title": "My own title"}),
        lambda ctx: lines.put_money(ctx, line_id, {"sell": {"amount": "1", "currency": "USD"}}),
        lambda ctx: lines.cancel(ctx, line_id),
        lambda ctx: lines.confirm(ctx, line_id),
        lambda ctx: lines.trash_check(ctx, line_id),
    ):
        with pytest.raises(Forbidden) as err:
            async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"]) as ctx:
                await call(ctx)
        assert err.value.code == "aa_line_read_only" and err.value.status_code == 403
    # The database refuses it as well (crm-014 guard 3a), whatever the API does.
    with pytest.raises(asyncpg.PostgresError) as db_err:
        async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"]) as ctx:
            await ctx.conn.execute("UPDATE crm.booking_line SET title = 'typed' WHERE booking_line_id = $1", line_id)
    assert db_err.value.sqlstate == "CRM24"
    with pytest.raises(asyncpg.PostgresError) as db_err:  # the trash route runs lines.trash_check first (403)
        async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"]) as ctx:
            await trash.move_to_trash(ctx, "booking_line", line_id)
    assert db_err.value.sqlstate == "CRM24"
    # A sell amount is margin: the assistant may ask for an AA line, not set its sell.
    with pytest.raises(Forbidden) as err:
        async with tenant_tx(app_pool, s["who"]["assistant"], s["tenant"]) as ctx:
            await aa_lines.request_line(ctx, s["trip"], _body())
    assert err.value.code == "role_forbidden"
    async with tenant_tx(app_pool, s["who"]["assistant"], s["tenant"]) as ctx:
        assert (await aa_lines.request_line(ctx, s["trip"], _body(sell_amount=None)))["aa"]["sub_status"] == "pending"


async def test_e_booking_down_is_aa_pending_and_retried_without_duplicates(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    fake = FakeBooking(down=True)
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        line_id = uuid.UUID((await aa_lines.request_line(ctx, s["trip"], _body()))["booking_line_id"])
    await _sync(app_pool, fake, s["tenant"])
    pending = await _line(app_pool, s, line_id)
    assert pending["aa"]["sub_status"] == "pending"
    assert pending["aa"]["pending"]["attempts"] == 1
    assert pending["aa"]["pending"]["last_error_class"] == "booking_timeout"
    # Not due yet: the next run sends nothing.
    await _sync(app_pool, fake, s["tenant"])
    assert (await _line(app_pool, s, line_id))["aa"]["pending"]["attempts"] == 1

    fake.down = False
    await _due_now(app_pool, s["tenant"])
    await _sync(app_pool, fake, s["tenant"])
    assert (await _line(app_pool, s, line_id))["aa"]["sub_status"] == "quoted"
    assert fake.quotes == 1

    # The hold is made in Booking but its answer is lost: retried with the same key, one hold, the line has it.
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        await aa_lines.ask(ctx, line_id, "hold")
    fake.lose_next_answer = True
    await _sync(app_pool, fake, s["tenant"])
    lost = await _line(app_pool, s, line_id)
    assert lost["aa"]["sub_status"] == "quoted" and lost["aa"]["pending"]["kind"] == "hold"
    await _due_now(app_pool, s["tenant"])
    await _sync(app_pool, fake, s["tenant"])
    held = await _line(app_pool, s, line_id)
    assert held["aa"]["sub_status"] == "held" and held["aa"]["hold_id"] == "h1"
    assert len(fake.holds) == 1 and len(fake.hold_keys) == 1
    assert (
        await _scalar(app_pool, s["tenant"], "SELECT count(*) FROM crm.booking_line WHERE trip_id = $1", s["trip"]) == 1
    )
    assert await _scalar(app_pool, s["tenant"], "SELECT count(*) FROM crm.trip_commitment") == 1
    # A repeated identical quote writes no new money head.
    heads = await _scalar(app_pool, s["tenant"], "SELECT count(*) FROM crm.booking_line_money")
    assert heads == 2


async def test_price_changed_requotes_once_then_holds(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    fake = FakeBooking()
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        line_id = uuid.UUID((await aa_lines.request_line(ctx, s["trip"], _body()))["booking_line_id"])
    await _sync(app_pool, fake, s["tenant"])
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        await aa_lines.ask(ctx, line_id, "hold")
    fake.hold_refusal = "price_changed"
    fake.b2b_price = "1100.00"
    await _sync(app_pool, fake, s["tenant"])
    # Re-quoted at the new price, the hold refused again (Booking still says price_changed): failed with the code.
    line = await _line(app_pool, s, line_id)
    assert line["money"]["cost"]["amount"] == "1100.00" and line["aa"]["result_code"] == "price_changed"
    assert fake.quotes == 2
    fake.hold_refusal = "sold_out"
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        await aa_lines.ask(ctx, line_id, "hold")
    await _sync(app_pool, fake, s["tenant"])
    line = await _line(app_pool, s, line_id)
    assert line["aa"]["sub_status"] == "sold_out"
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        assert (await lines.trip_lines(ctx, s["trip"]))["lost_reason_hint"] == "aa_sold_out"


async def test_lost_releases_the_hold_after_the_undo_window(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool, stage="proposal")
    fake = FakeBooking()
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        line_id = uuid.UUID((await aa_lines.request_line(ctx, s["trip"], _body()))["booking_line_id"])
    await _sync(app_pool, fake, s["tenant"])
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        await aa_lines.ask(ctx, line_id, "hold")
    await _sync(app_pool, fake, s["tenant"])

    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        moved = await move(ctx, s["trip"], "lost", lost_reason="went_elsewhere")
    assert moved["aa_releases_queued"] == 1
    await _sync(app_pool, fake, s["tenant"])  # not due yet (the Undo window)
    assert fake.holds["h1"]["status"] == "active"
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        await undo(ctx, s["trip"], uuid.UUID(moved["undo"]["history_id"]))
    assert await _scalar(app_pool, s["tenant"], "SELECT count(*) FROM crm.aa_request WHERE status = 'pending'") == 0

    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        await move(ctx, s["trip"], "lost", lost_reason="went_elsewhere")
    await _due_now(app_pool, s["tenant"])
    await _sync(app_pool, fake, s["tenant"])
    released = await _line(app_pool, s, line_id)
    assert released["status"] == "cancelled" and released["aa"]["sub_status"] == "released"
    assert fake.holds["h1"]["status"] == "released"
    assert await _scalar(
        app_pool, s["tenant"], "SELECT line_status FROM crm.trip_commitment WHERE source_ref = 'h1'"
    ) == ("released")
    assert await _scalar(app_pool, s["tenant"], "SELECT crm.trip_delete_blockers($1)", s["trip"]) == []
    with pytest.raises(Conflict) as err:
        async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
            await aa_lines.request_line(ctx, s["trip"], _body())
    assert err.value.code == "trip_closed"


async def test_a_declined_line_preselects_the_lost_reason(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    fake = FakeBooking()
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        line_id = uuid.UUID((await aa_lines.request_line(ctx, s["trip"], _body()))["booking_line_id"])
    await _sync(app_pool, fake, s["tenant"])
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        await aa_lines.ask(ctx, line_id, "hold")
    await _sync(app_pool, fake, s["tenant"])
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        await aa_lines.ask(ctx, line_id, "confirm")
    await _sync(app_pool, fake, s["tenant"])
    fake.add_event("line.declined", hold_id="h1", line_ref=str(line_id), decline_code="supplier_unavailable")
    await _sync(app_pool, fake, s["tenant"])
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        view = await lines.trip_lines(ctx, s["trip"])
    (line,) = view["lines"]
    assert line["status"] == "cancelled" and line["aa"]["sub_status"] == "declined"
    assert line["aa"]["decline_code"] == "supplier_unavailable"
    assert view["lost_reason_hint"] == "aa_on_request_declined"
    assert await _scalar(app_pool, s["tenant"], "SELECT kind FROM crm.task WHERE trip_id = $1", s["trip"]) == (
        "aa_line_declined"
    )


async def test_undo_of_lost_leaves_a_waiting_request_to_run(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool, stage="proposal")
    fake = FakeBooking(down=True)
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        line_id = uuid.UUID((await aa_lines.request_line(ctx, s["trip"], _body()))["booking_line_id"])
    await _sync(app_pool, fake, s["tenant"])  # Booking down: "AA pending"
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        moved = await move(ctx, s["trip"], "lost", lost_reason="went_elsewhere")
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        await undo(ctx, s["trip"], uuid.UUID(moved["undo"]["history_id"]))
    fake.down = False
    await _due_now(app_pool, s["tenant"])
    await _sync(app_pool, fake, s["tenant"])
    line = await _line(app_pool, s, line_id)
    assert line["status"] == "planning" and line["aa"]["sub_status"] == "quoted" and line["aa"]["pending"] is None
    assert fake.quotes == 1 and "hold" in line["aa"]["actions"]
    assert (
        await _scalar(app_pool, s["tenant"], "SELECT count(*) FROM crm.aa_request WHERE result_code = 'trip_closed'")
        == 0
    )

    # A requote still waits when the trip goes Lost for good: dropped without calling Booking, the line released.
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        await aa_lines.ask(ctx, line_id, "requote")
        await move(ctx, s["trip"], "lost", lost_reason="went_elsewhere")
    # Past the Undo window (only the requote made due here, so it runs before the release).
    await _scalar(app_pool, s["tenant"], "UPDATE crm.aa_request SET next_attempt_at = now() WHERE kind = 'requote'")
    await _sync(app_pool, fake, s["tenant"], feed=False)
    assert await _scalar(app_pool, s["tenant"], "SELECT result_code FROM crm.aa_request WHERE kind = 'requote'") == (
        "trip_closed"
    )
    assert fake.quotes == 1
    await _due_now(app_pool, s["tenant"])
    await _sync(app_pool, fake, s["tenant"], feed=False)
    closed = await _line(app_pool, s, line_id)
    assert closed["status"] == "cancelled" and closed["aa"]["sub_status"] == "released"


async def test_a_hold_that_lands_after_its_line_was_released_is_given_back(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool, stage="proposal")
    fake = FakeBooking()
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        line_id = uuid.UUID((await aa_lines.request_line(ctx, s["trip"], _body()))["booking_line_id"])
    await _sync(app_pool, fake, s["tenant"])
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        await aa_lines.ask(ctx, line_id, "hold")

    async def release_meanwhile() -> None:
        # Another run sends the line's release while the hold call is on its way (the line has no hold id yet): it
        # releases the line and cancels the leased hold request.
        await _scalar(
            app_pool,
            s["tenant"],
            "INSERT INTO crm.aa_request (trip_id, booking_line_id, kind) VALUES ($1, $2, 'release') "
            "RETURNING aa_request_id",
            s["trip"],
            line_id,
        )
        await _sync(app_pool, fake, s["tenant"], feed=False)

    fake.on_hold = release_meanwhile
    await _sync(app_pool, fake, s["tenant"], feed=False)
    line = await _line(app_pool, s, line_id)
    assert line["status"] == "cancelled" and line["aa"]["sub_status"] == "released" and line["aa"]["hold_id"] == "h1"
    assert await _scalar(app_pool, s["tenant"], "SELECT status FROM crm.aa_request WHERE kind = 'hold'") == (
        "cancelled"
    )
    assert (
        await _scalar(
            app_pool, s["tenant"], "SELECT count(*) FROM crm.aa_request WHERE kind = 'release' AND status = 'pending'"
        )
        == 1
    )
    assert fake.holds["h1"]["status"] == "active"

    await _sync(app_pool, fake, s["tenant"], feed=False)
    assert fake.holds["h1"]["status"] == "released"
    assert await _scalar(
        app_pool, s["tenant"], "SELECT line_status FROM crm.trip_commitment WHERE source_ref = 'h1'"
    ) == ("released")
    assert await _scalar(app_pool, s["tenant"], "SELECT crm.trip_delete_blockers($1)", s["trip"]) == []
    assert (await _line(app_pool, s, line_id))["status"] == "cancelled"


async def test_an_outage_costs_one_timeout_per_agency_and_run(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    fake = FakeBooking(down=True)
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        first = uuid.UUID((await aa_lines.request_line(ctx, s["trip"], _body()))["booking_line_id"])
        second = uuid.UUID((await aa_lines.request_line(ctx, s["trip"], _body()))["booking_line_id"])
    counts: Counter[str] = Counter()
    await BookingSync(booking_client(fake)).run_agency(app_pool, s["tenant"], counts, feed=True)
    assert fake.calls["down"] == 1  # one quote tried; the other request and the feed were not sent
    assert counts["outage"] == 1 and counts["requests_retry"] == 2 and "feed_failed" not in counts
    for line_id in (first, second):
        pending = (await _line(app_pool, s, line_id))["aa"]["pending"]
        assert pending["attempts"] == 1 and pending["last_error_class"] == "booking_timeout"
    fake.down = False
    await _due_now(app_pool, s["tenant"])
    await _sync(app_pool, fake, s["tenant"])
    assert fake.quotes == 2


async def test_the_catalog_cache_follows_cis(app_pool: asyncpg.Pool) -> None:
    # Keep this test last in the file: it retires T-100 and leaves the shared catalog state in backoff.
    s = await _setup(app_pool)
    cis = FakeCis(tours={"T-100": tour_body(), "T-200": tour_body("T-200", "Sapa trek")})
    sync = CatalogSync(catalog_config(cis))
    assert (await sync(app_pool, JOB))["status"] == "pass_done"
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        line_id = uuid.UUID((await aa_lines.request_line(ctx, s["trip"], _body()))["booking_line_id"])
        from app.catalog import store

        assert [t["source_id"] for t in await store.search(ctx, "sapa", None, 10)] == ["T-200"]
        assert await store.search(ctx, "%", None, 10) == []  # LIKE wildcards are plain text
    # T-100 retired, T-200 changed: the next pass re-fetches one and re-checks the other.
    cis.tours["T-200"] = tour_body("T-200", "Sapa trek 2 days", "sha256:2")
    del cis.tours["T-100"]
    cis.retired.add("T-100")
    gets = cis.calls["get"]
    assert (await sync(app_pool, JOB))["status"] == "pass_done"
    assert cis.calls["get"] == gets + 2
    line = await _line(app_pool, s, line_id)
    assert line["aa"]["no_longer_offered"] is True
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        from app.catalog import store

        assert await store.tour(ctx, "T-100") == {
            "source_id": "T-100", "title": "Mekong Delta 3 days", "country_code": "VN", "duration_days": 3,
            "served_state": "retired", "reason": "source_deleted",
        }  # fmt: skip
        assert (await store.tour(ctx, "T-200"))["title"] == "Sapa trek 2 days"
        with pytest.raises(Conflict) as err:
            await aa_lines.request_line(ctx, s["trip"], _body())
    assert err.value.code == "aa_tour_not_served"
    cis.down = True
    assert (await sync(app_pool, JOB))["reason_code"] == "cis_unavailable"
    assert (await sync(app_pool, JOB))["status"] == "backoff"
