"""AA-961 (card CRM-30) end to end on PostgreSQL 15 with the RDS role shape, as crm_app through ``tenant_tx()``,
``system_tx()`` and ``calendar_feed_tx()``.

- **Done when**: an AA incident on a departure (the Booking feed event) shows the affected trips only for the agency
  that has a line on it; another agency Booking also told sees nothing; clearing it lifts the banner.
- **E**: a traveller on two overlapping trips appears once, with both trips.
- **R**: a bulk reveal opens names / phones with one audit event per traveller (the unit tests cover the reason
  threshold and the hourly limit).
- **E**: a revoked calendar feed answers 404 (subscribed calendars stop updating); the member sees it revoked.
- The ICS feed of scope ``mine`` has the member's own trips only, and no title or name.
- A travelling trip without an end date stays in Travelling now (open-ended, design F10.1); a trip the stage job
  completed on its return day is still there that day, and its departure / return stay in the calendar and the feed.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from typing import Any

import asyncpg
import pytest

from app import stages_job
from app.booking import contract
from app.booking.sync import apply_event
from app.calendar import events, feeds
from app.calendar import public as calendar_public
from app.duty import service
from app.errors import Forbidden, NotFound
from app.tenancy import system_tx, tenant_tx
from app.vault import KeyProvider, Vault
from app.vault.wrapping import LocalKeyWrapper
from tests.integration.test_crm005_intake import _agency

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

DEP = "dep-2026-hg-01"


def _keys() -> KeyProvider:
    return KeyProvider({"local": LocalKeyWrapper({"k1": os.urandom(32)}, "k1")}, "local")


async def _seed(pool: asyncpg.Pool, tenant: uuid.UUID, sql: str, *args: Any, actor: str = "seed:test") -> Any:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
        await conn.execute("SELECT set_config('app.actor', $1, true)", actor)
        return await conn.fetchval(sql, *args)


async def _traveller(pool: asyncpg.Pool, keys: KeyProvider, tenant: uuid.UUID, who: Any, name: str) -> uuid.UUID:
    tid = uuid.uuid4()
    async with tenant_tx(pool, who, tenant) as ctx:
        vault = Vault(keys, ctx)
        await ctx.conn.execute(
            "INSERT INTO crm.traveler (traveler_id, display_name_enc, phone_enc, phone_bidx, emergency_contact_enc) "
            "VALUES ($1, $2, $3, $4, $5)",
            tid,
            await vault.encrypt("traveler", "display_name_enc", tid, name),
            await vault.encrypt("traveler", "phone_enc", tid, "+84900000001"),
            await vault.phone_index("traveler", "phone", "+84900000001"),
            await vault.encrypt("traveler", "emergency_contact_enc", tid, "Le Van An +84900000002"),
        )
    return tid


async def _trip(
    pool: asyncpg.Pool,
    tenant: uuid.UUID,
    owner: uuid.UUID,
    stage: str,
    start: dt.date,
    end: dt.date | None,
    party: list,
) -> uuid.UUID:
    trip = uuid.uuid4()
    await _seed(
        pool,
        tenant,
        "INSERT INTO crm.trip (trip_id, title, stage, waiting_on, owner_member_id, start_date, end_date, destinations) "
        "VALUES ($1, 'Lotus family, Ha Giang loop', $2, 'none', $3, $4, $5, ARRAY['vietnam'])",
        trip,
        stage,
        owner,
        start,
        end,
    )
    for i, tid in enumerate(party):
        await _seed(
            pool,
            tenant,
            "INSERT INTO crm.trip_traveler (trip_id, traveler_id, role) VALUES ($1, $2, $3)",
            trip,
            tid,
            "lead" if i == 0 else "companion",
        )
    return trip


async def _aa_line(pool: asyncpg.Pool, tenant: uuid.UUID, trip: uuid.UUID, start: dt.date, end: dt.date) -> None:
    await _seed(
        pool,
        tenant,
        "INSERT INTO crm.booking_line (trip_id, supplier_kind, agency_supplier_id, title, departure_id, service_from, "
        "service_to) VALUES ($1, 'aa', crm.ensure_aa_supplier(), 'Ha Giang loop 4 days', $2, $3, $4)",
        trip,
        DEP,
        start,
        end,
        actor="job:test",
    )


async def _event(pool: asyncpg.Pool, tenant: uuid.UUID, kind: str, event_id: str) -> tuple[str, str]:
    ev = contract.event(
        {
            "event_id": event_id,
            "type": kind,
            "departure_id": DEP,
            "incident_code": "road",
            "occurred_at": "2026-10-30T01:00:00Z",
        }  # fmt: skip
    )
    async with system_tx(pool, tenant, actor="job:crm_booking_sync") as ctx:
        return await apply_event(ctx, ev)


async def _setup(pool: asyncpg.Pool) -> dict[str, Any]:
    keys = _keys()
    tenant, people = await _agency(pool, "owner", "agent", "assistant")
    who = {role: people[i] for i, role in enumerate(("owner", "agent", "assistant"))}
    async with tenant_tx(pool, who["owner"][0], tenant) as ctx:
        today, _ = await service.today(ctx)
    t1 = await _traveller(pool, keys, tenant, who["owner"][0], "Lotus Mai")
    t2 = await _traveller(pool, keys, tenant, who["owner"][0], "Lotus Binh")
    t3 = await _traveller(pool, keys, tenant, who["owner"][0], "Lotus Chau")
    day = dt.timedelta(days=1)
    away = await _trip(pool, tenant, who["agent"][1], "travelling", today - 2 * day, today + 5 * day, [t1, t2])
    next_ = await _trip(pool, tenant, who["owner"][1], "booked", today + 3 * day, today + 6 * day, [t1])
    await _trip(pool, tenant, who["agent"][1], "proposal", today + 4 * day, today + 9 * day, [t3])
    await _aa_line(pool, tenant, away, today - day, today + 2 * day)
    return {"keys": keys, "tenant": tenant, "who": who, "today": today, "away": away, "next": next_, "t": (t1, t2, t3)}


async def test_done_when_an_incident_shows_only_for_the_agency_with_the_departure(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    other, people = await _agency(app_pool, "owner")
    assert await _event(app_pool, s["tenant"], "departure.incident", "ev_inc_1") == ("applied", "departure_incident")
    assert await _event(app_pool, other, "departure.incident", "ev_inc_1b") == ("applied", "departure_incident")
    # A replay of the same event, or a second incident while one is open, opens nothing new.
    assert await _event(app_pool, s["tenant"], "departure.incident", "ev_inc_1") == ("ignored", "incident_already_open")
    agent = s["who"]["agent"][0]
    async with tenant_tx(app_pool, agent, s["tenant"]) as ctx:
        view = await service.travelling(ctx)
        banner = await service.trip_incidents(ctx, s["away"])
    assert len(view["incidents"]) == 1 and [t["trip_id"] for t in view["incidents"][0]["trips"]] == [str(s["away"])]
    assert view["incidents"][0]["incident_code"] == "road"
    away = next(t for g in view["regions"] for t in g["trips"] if t["trip_id"] == str(s["away"]))
    assert away["incident_flag_id"] == view["incidents"][0]["incident_flag_id"]
    assert banner["incidents"][0]["status"] == "open"
    # The affected trip has one urgent task for its owner.
    task = await _seed(
        app_pool, s["tenant"], "SELECT priority FROM crm.task WHERE trip_id = $1 AND kind = 'aa_incident'", s["away"]
    )
    assert task == "urgent"
    # The other agency has no line on the departure: nothing to show.
    async with tenant_tx(app_pool, people[0][0], other) as ctx:
        assert (await service.travelling(ctx))["incidents"] == []
    # Cleared: the banner goes, the trip shows it as cleared.
    assert await _event(app_pool, s["tenant"], "departure.incident_cleared", "ev_inc_2") == (
        "applied",
        "departure_incident_cleared",
    )
    async with tenant_tx(app_pool, agent, s["tenant"]) as ctx:
        assert (await service.travelling(ctx))["incidents"] == []
        assert (await service.trip_incidents(ctx, s["away"]))["incidents"][0]["status"] == "cleared"
    assert await _event(app_pool, s["tenant"], "departure.incident_cleared", "ev_inc_3") == (
        "ignored",
        "incident_not_open",
    )


async def test_e_a_traveller_on_two_overlapping_trips_appears_once(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    t1, t2, t3 = s["t"]
    async with tenant_tx(app_pool, s["who"]["assistant"][0], s["tenant"]) as ctx:
        view = await service.travelling(ctx, days=7)
        reach = await service.reach(ctx, region="VN")
    people = {p["traveler_id"]: p for p in view["travellers"]}
    assert set(people) == {str(t1), str(t2)}  # the proposal-stage trip is not travelling
    assert sorted(people[str(t1)]["trip_ids"]) == sorted([str(s["away"]), str(s["next"])])
    assert people[str(t1)]["has_phone"] and people[str(t1)]["has_emergency_contact"]
    assert [g["region"] for g in view["regions"]] == ["VN"] and view["regions"][0]["traveller_count"] == 2
    away = next(t for t in view["regions"][0]["trips"] if t["trip_id"] == str(s["away"]))
    assert away["day_no"] == 3 and away["lines"][0]["departure_id"] == DEP
    # No name anywhere in the view.
    assert "Lotus Mai" not in str(view) and "Lotus Mai" not in str(reach)
    # Today only the trip under way is in Vietnam.
    assert [t["trip_id"] for t in reach["trips"]] == [str(s["away"])]
    assert {p["traveler_id"] for p in reach["travellers"]} == {str(t1), str(t2)}


async def test_r_bulk_reveal_opens_the_names_and_logs_each_traveller(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    t1, t2, _ = s["t"]
    async with tenant_tx(app_pool, s["who"]["assistant"][0], s["tenant"]) as ctx:
        out = await service.bulk_reveal(
            ctx, Vault(s["keys"], ctx), [t1, t2], ["display_name", "emergency_contact"], None
        )
    names = {p["display_name"] for p in out["travellers"]}
    assert names == {"Lotus Mai", "Lotus Binh"} and out["travellers"][0]["emergency_contact"].startswith("Le Van An")
    logged = await _seed(
        app_pool,
        s["tenant"],
        "SELECT count(*) FROM crm.audit_event WHERE action = 'reveal' AND reason = 'duty_of_care' "
        "AND fields = ARRAY['display_name', 'emergency_contact']",
    )
    assert logged == 2


async def test_calendar_events_and_the_feed(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    owner, agent, assistant = (s["who"][r][0] for r in ("owner", "agent", "assistant"))
    today = s["today"]
    task_at = dt.datetime.combine(today + dt.timedelta(days=1), dt.time(9), dt.UTC)
    await _seed(
        app_pool,
        s["tenant"],
        "INSERT INTO crm.task (trip_id, kind, assignee_member_id, due_at) VALUES ($1, 'confirm_flights', $2, $3)",
        s["away"],
        s["who"]["agent"][1],
        task_at,
    )
    await _seed(
        app_pool,
        s["tenant"],
        "INSERT INTO crm.meeting_slot (trip_id, starts_at, duration_min, status, source) "
        "VALUES ($1, $2, 30, 'confirmed', 'agent')",
        s["next"],
        task_at + dt.timedelta(hours=2),
    )
    async with tenant_tx(app_pool, agent, s["tenant"]) as ctx:
        mine = await events.agency_calendar(ctx, start=today - dt.timedelta(days=7), end=today + dt.timedelta(days=30),
                                            mine=True)  # fmt: skip
        everyone = await events.agency_calendar(
            ctx, start=today - dt.timedelta(days=7), end=today + dt.timedelta(days=30), mine=False
        )
    kinds_mine = {(e["kind"], e["trip_id"]) for e in mine["events"]}
    assert ("departure", str(s["away"])) in kinds_mine and ("task", str(s["away"])) in kinds_mine
    assert ("departure", str(s["next"])) not in kinds_mine  # the owner's trip
    kinds_all = {(e["kind"], e["trip_id"]) for e in everyone["events"]}
    assert {("departure", str(s["next"])), ("return", str(s["next"])), ("call", str(s["next"]))} <= kinds_all

    # The agent's feed: the URL once, the agent's own trips only, no title or name.
    async with tenant_tx(app_pool, agent, s["tenant"]) as ctx:
        made = await feeds.create(ctx, scope="mine", origin="https://partner.example")
    token = made["url"].rsplit("/", 1)[1].removesuffix(".ics")
    text = await calendar_public.feed(app_pool, token, web_origin="https://partner.example")
    assert f"trip {str(s['away'])[:8]}" in text and f"trip {str(s['next'])[:8]}" not in text
    assert "Lotus" not in text and "Ha Giang" not in text
    await calendar_public.feed(app_pool, token, web_origin="https://partner.example")  # within 10 minutes
    assert await _seed(app_pool, s["tenant"], "SELECT fetch_count FROM crm.calendar_feed WHERE status = 'active'") == 1

    # The assistant may not revoke the agent's feed; the owner may; then the URL is gone and the agent sees why.
    feed_id = uuid.UUID(made["feed"]["calendar_feed_id"])
    with pytest.raises(Forbidden):
        async with tenant_tx(app_pool, assistant, s["tenant"]) as ctx:
            await feeds.revoke(ctx, feed_id)
    async with tenant_tx(app_pool, owner, s["tenant"]) as ctx:
        assert len((await feeds.team(ctx))["feeds"]) == 1
        await feeds.revoke(ctx, feed_id)
    with pytest.raises(NotFound):
        await calendar_public.feed(app_pool, token, web_origin="https://partner.example")
    async with tenant_tx(app_pool, agent, s["tenant"]) as ctx:
        state = (await feeds.status(ctx))["feed"]
    assert state["status"] == "revoked" and state["revoked_by_owner"] is True
    # A new link replaces nothing live (the old one is revoked) and works at once.
    async with tenant_tx(app_pool, agent, s["tenant"]) as ctx:
        again = await feeds.create(ctx, scope="agency", origin="https://partner.example")
        again2 = await feeds.create(ctx, scope="agency", origin="https://partner.example")
    with pytest.raises(NotFound):  # creating a second link revoked the first
        await calendar_public.feed(
            app_pool, again["url"].rsplit("/", 1)[1].removesuffix(".ics"), web_origin="https://partner.example"
        )
    text = await calendar_public.feed(
        app_pool, again2["url"].rsplit("/", 1)[1].removesuffix(".ics"), web_origin="https://partner.example"
    )
    assert f"trip {str(s['next'])[:8]}" in text  # scope agency
    with pytest.raises(NotFound):
        await calendar_public.feed(app_pool, "x" * 43, web_origin="https://partner.example")


async def test_open_ended_and_return_day_trips_stay_in_duty_of_care_and_the_calendar(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    today, day = s["today"], dt.timedelta(days=1)
    owner = s["who"]["owner"]
    t4 = await _traveller(app_pool, s["keys"], s["tenant"], owner[0], "Lotus Dao")
    t5 = await _traveller(app_pool, s["keys"], s["tenant"], owner[0], "Lotus Em")
    # Travelling for 10 days, no return date set (the stage job only raises return_date_missing).
    no_end = await _trip(app_pool, s["tenant"], owner[1], "travelling", today - 10 * day, None, [t4])
    # Back today: the stage job completed it this morning.
    back = await _trip(app_pool, s["tenant"], owner[1], "travelling", today - 4 * day, today, [t5])
    await _aa_line(app_pool, s["tenant"], back, today - 3 * day, today)
    # Came back yesterday: gone from duty of care.
    gone = await _trip(app_pool, s["tenant"], owner[1], "travelling", today - 6 * day, today - day, [])
    async with system_tx(app_pool, s["tenant"], actor=stages_job.ACTOR) as ctx:
        counts = await stages_job.run_agency(ctx)
    assert counts["travelling_to_completed"] == 2 and counts["return_date_missing"] == 1
    assert await _event(app_pool, s["tenant"], "departure.incident", "ev_inc_back") == ("applied", "departure_incident")

    async with tenant_tx(app_pool, owner[0], s["tenant"]) as ctx:
        week = await service.travelling(ctx, days=7)
        reach = await service.reach(ctx, region="VN")
        tomorrow = await service.reach(ctx, region="VN", on=today + day)
        cal = await events.agency_calendar(ctx, start=today - 30 * day, end=today + 30 * day, mine=True)
        made = await feeds.create(ctx, scope="mine", origin="https://partner.example")
    trips = {t["trip_id"]: t for g in week["regions"] for t in g["trips"]}
    assert str(no_end) in trips and trips[str(no_end)]["day_no"] == 11 and trips[str(no_end)]["end_date"] is None
    assert trips[str(back)]["stage"] == "completed" and trips[str(back)]["day_no"] == 5
    assert str(gone) not in trips
    assert {str(no_end), str(back)} <= {t["trip_id"] for t in reach["trips"]}
    assert {str(t4), str(t5)} <= {p["traveler_id"] for p in reach["travellers"]}
    # The incident on the departure still names the trip coming back today, with its badge.
    assert {t["trip_id"] for t in week["incidents"][0]["trips"]} == {str(s["away"]), str(back)}
    assert trips[str(back)]["incident_flag_id"] == week["incidents"][0]["incident_flag_id"]
    assert (
        await _seed(
            app_pool, s["tenant"], "SELECT count(*) FROM crm.task WHERE trip_id = $1 AND kind = 'aa_incident'", back
        )
        == 1
    )
    # Tomorrow the returned trip is out (a future window shows no completed trip); the open-ended one is still away.
    later = {t["trip_id"] for t in tomorrow["trips"]}
    assert str(no_end) in later and str(back) not in later

    # Calendar and feed: the completed trips keep their departure and return (the UIDs a subscribed calendar holds).
    kinds = {(e["kind"], e["trip_id"]) for e in cal["events"]}
    assert {("departure", str(back)), ("return", str(back)), ("departure", str(gone)), ("return", str(gone))} <= kinds
    assert ("departure", str(no_end)) in kinds
    text = await calendar_public.feed(
        app_pool, made["url"].rsplit("/", 1)[1].removesuffix(".ics"), web_origin="https://partner.example"
    )
    assert f"UID:return-{back}@crm" in text and f"UID:departure-{gone}@crm" in text
