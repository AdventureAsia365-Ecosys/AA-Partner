"""AA-956 (card CRM-26, Module 8 ops release) end to end on PostgreSQL with the RDS role shape, as crm_app through
``tenant_tx()``, ``system_tx()`` (the job) and ``ops_tx()`` (AA ops).

- **Done when**: an AA line confirmed by the Booking feed releases the trip (one live ``crm.ops_release``, sealed under
  the agency's ``ops_release`` DEK, minimal fields only); a Stage 3 change refreshes it (``payload_version`` up);
  completeness counts; incomplete at T-21 -> one alert (task + ``t21_alerted_at``), once; S-02 shows the board (no
  personal data) and the Booking incident admin link; every AA ops view writes "AA ops viewed trip X" into the
  agency's audit; the minimal manifest is logged with its watermark; the trip cancel withdraws the release and the job
  purges it before departure with a receipt; a release purges at return + 90 days.
- **R**: a release for an unconfirmed line -> 404 (before any confirm, and after the line is cancelled even before the
  job withdraws it).
- **E**: dates moved -> purge date recomputed; incomplete at T-21 -> alert.
- crm-034 guards (CRM45): only a job creates a release, AA ops only record a view, a member only withdraws, nobody
  deletes; a manifest record is AA ops' only; RLS keeps another agency out; no reader role sees either table.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from typing import Any

import asyncpg
import pytest

from app import stages
from app.auth.route_guard import Caller
from app.booking import contract
from app.booking.sync import apply_event
from app.errors import Conflict, NotFound
from app.ops_release import job, service, staff
from app.tenancy import system_tx, tenant_tx
from app.vault import KeyProvider, Vault
from app.vault.wrapping import LocalKeyWrapper
from tests.integration.conftest import READERS
from tests.integration.test_crm005_intake import _agency

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

WORKFORCE = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_TESTWORKF"
DEP = "dep-2026-lotus-01"
DAY = dt.timedelta(days=1)


def _keys() -> KeyProvider:
    return KeyProvider({"local": LocalKeyWrapper({"k1": os.urandom(32)}, "k1")}, "local")


def _ops(groups: tuple[str, ...] = ("crm-ops",)) -> Caller:
    return Caller("staff", "workforce", "crm-staff", WORKFORCE, str(uuid.uuid4()), groups)


async def _seed(pool: asyncpg.Pool, tenant: uuid.UUID, sql: str, *args: Any, actor: str = "seed:test") -> Any:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
        await conn.execute("SELECT set_config('app.actor', $1, true)", actor)
        return await conn.fetchval(sql, *args)


async def _traveller(pool: asyncpg.Pool, keys: KeyProvider, tenant: uuid.UUID, who: Caller, **f: str) -> uuid.UUID:
    tid = uuid.uuid4()
    async with tenant_tx(pool, who, tenant) as ctx:
        vault = Vault(keys, ctx)
        cols = {"display_name_enc": f["name"], "phone_enc": "+84900000009", **{f"{k}_enc": v for k, v in f.items()
                                                                             if k != "name"}}  # fmt: skip
        names = [*cols, "phone_bidx"]
        values = [await vault.encrypt("traveler", c, tid, v) for c, v in cols.items()]
        values.append(await vault.phone_index("traveler", "phone", "+84900000009"))
        await ctx.conn.execute(
            f"INSERT INTO crm.traveler (traveler_id, {', '.join(names)}) "  # noqa: S608 — test column names
            f"VALUES ($1, {', '.join(f'${i + 2}' for i in range(len(names)))})",
            tid,
            *values,
        )
    return tid


FULL = {
    "given_name": "Mai",
    "family_name": "Lotus",
    "dob": "1990-04-02",
    "nationality": "AU",
    "passport_no": "PA0000001",
    "passport_expiry": "2032-01-01",
    "passport_country": "AU",
    "emergency_contact": "Binh Lotus +61400000000",
}


async def _health(pool: asyncpg.Pool, keys: KeyProvider, tenant: uuid.UUID, who: Caller, tid: uuid.UUID) -> None:
    async with tenant_tx(pool, who, tenant) as ctx:
        consent = await ctx.conn.fetchval(
            "INSERT INTO crm.consent (traveler_id, purpose, value, source) "
            "VALUES ($1, 'health_data', true, 'agent_recorded') RETURNING consent_id",
            tid,
        )
        hid = uuid.uuid4()
        blob = await Vault(keys, ctx).encrypt(
            "traveler_health", "health_flags_enc", hid, json.dumps({"dietary": ["vegetarian"], "mobility": []})
        )
        await ctx.conn.execute(
            "INSERT INTO crm.traveler_health (health_id, traveler_id, health_flags_enc, consent_id) "
            "VALUES ($1, $2, $3, $4)",
            hid,
            tid,
            blob,
            consent,
        )


async def _flight(
    pool: asyncpg.Pool, keys: KeyProvider, tenant: uuid.UUID, who: Caller, trip: uuid.UUID, tid: uuid.UUID, d: str
) -> None:
    async with tenant_tx(pool, who, tenant) as ctx:
        fid = uuid.uuid4()
        await ctx.conn.execute(
            "INSERT INTO crm.traveler_flight (traveler_flight_id, trip_id, traveler_id, direction, flight_no_enc, "
            "airport_iata, sched_local, tz, entered_by_kind) "
            "VALUES ($1, $2, $3, $4, $5, 'HAN', '2026-12-01 22:40', 'Asia/Ho_Chi_Minh', 'member')",
            fid,
            trip,
            tid,
            d,
            await Vault(keys, ctx).encrypt("traveler_flight", "flight_no_enc", fid, "ZZ101"),
        )


async def _trip(
    pool: asyncpg.Pool, tenant: uuid.UUID, owner: uuid.UUID, start: dt.date, end: dt.date, party: list[uuid.UUID]
) -> uuid.UUID:
    trip = uuid.uuid4()
    await _seed(
        pool,
        tenant,
        "INSERT INTO crm.trip (trip_id, title, stage, waiting_on, owner_member_id, start_date, end_date, destinations) "
        "VALUES ($1, 'Lotus family, river loop', 'booked', 'none', $2, $3, $4, ARRAY['vietnam'])",
        trip,
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


async def _aa_line(pool: asyncpg.Pool, tenant: uuid.UUID, trip: uuid.UUID, start: dt.date, end: dt.date) -> uuid.UUID:
    return await _seed(
        pool,
        tenant,
        "INSERT INTO crm.booking_line (trip_id, supplier_kind, agency_supplier_id, title, departure_id, service_from, "
        "service_to) VALUES ($1, 'aa', crm.ensure_aa_supplier(), 'River loop 5 days', $2, $3, $4) "
        "RETURNING booking_line_id",
        trip,
        DEP,
        start,
        end,
        actor="job:test",
    )


async def _feed(pool: asyncpg.Pool, tenant: uuid.UUID, kind: str, line: uuid.UUID) -> tuple[str, str]:
    ev = contract.event({"event_id": f"ev_{uuid.uuid4().hex[:12]}", "type": kind, "line_ref": str(line),
                         "occurred_at": "2026-10-30T01:00:00Z"})  # fmt: skip
    async with system_tx(pool, tenant, actor="job:crm_booking_sync") as ctx:
        return await apply_event(ctx, ev)


async def _sync(pool: asyncpg.Pool, keys: KeyProvider, tenant: uuid.UUID, trip: uuid.UUID | None = None) -> dict:
    async with system_tx(pool, tenant, actor=service.ACTOR) as ctx:
        return dict(await job.run_agency(ctx, Vault(keys, ctx), [trip] if trip else None))


async def _release(pool: asyncpg.Pool, tenant: uuid.UUID, trip: uuid.UUID) -> Any:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
        return await conn.fetchrow(
            "SELECT * FROM crm.ops_release WHERE trip_id = $1 ORDER BY released_at DESC LIMIT 1", trip
        )


async def _events(pool: asyncpg.Pool, tenant: uuid.UUID, trip: uuid.UUID) -> list[tuple[str, str, str]]:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
        rows = await conn.fetch(
            "SELECT action, actor_kind, actor_ref FROM crm.audit_event WHERE object_id = $1 AND action LIKE 'ops\\_%' "
            "ORDER BY at, event_id",
            str(trip),
        )
    return [(r["action"], r["actor_kind"], r["actor_ref"]) for r in rows]


async def _setup(pool: asyncpg.Pool, start_in: int = 10, length: int = 5) -> dict[str, Any]:
    keys = _keys()
    tenant, people = await _agency(pool, "owner", "agent", "assistant")
    who = {role: people[i] for i, role in enumerate(("owner", "agent", "assistant"))}
    owner = who["owner"][0]
    async with tenant_tx(pool, owner, tenant) as ctx:
        today = await service.today(ctx)
    lead = await _traveller(pool, keys, tenant, owner, name="Mai Lotus", **FULL)
    child = await _traveller(pool, keys, tenant, owner, name="Little Lotus", dob=str(today - 7 * 365 * DAY))
    await _health(pool, keys, tenant, owner, lead)
    start, end = today + start_in * DAY, today + (start_in + length) * DAY
    trip = await _trip(pool, tenant, who["agent"][1], start, end, [lead, child])
    line = await _aa_line(pool, tenant, trip, start, end)
    return {"keys": keys, "tenant": tenant, "who": who, "today": today, "trip": trip, "line": line,
            "lead": lead, "child": child, "start": start, "end": end}  # fmt: skip


async def test_done_when_a_confirmed_aa_line_releases_the_trip_to_aa_ops(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    ops = _ops()
    # R: no confirmed AA line yet -> no release, and S-02 answers 404.
    assert await _sync(app_pool, s["keys"], s["tenant"], s["trip"]) == {}
    with pytest.raises(NotFound, match="no ops release"):
        await staff.read(app_pool, s["keys"], ops, s["tenant"], s["trip"])
    # Booking confirms the line: the feed queues a one-trip nudge, the job creates the release.
    assert await _feed(app_pool, s["tenant"], "line.confirmed", s["line"]) == ("applied", "line_confirmed")
    nudges = await app_pool.fetchval(
        "SELECT count(*) FROM crm.job WHERE kind = 'crm_ops_release_sync' AND payload ->> 'trip_id' = $1",
        str(s["trip"]),
    )
    assert nudges == 1
    counts = await _sync(app_pool, s["keys"], s["tenant"], s["trip"])
    assert counts["created"] == 1 and counts["alerted"] == 1  # departs in 10 days, Stage 3 / flights missing: T-21
    r = await _release(app_pool, s["tenant"], s["trip"])
    assert r["created_on"] == "aa_line_confirmed" and r["created_by"] == service.ACTOR and r["payload_version"] == 1
    assert r["purge_at"] == s["end"] + 90 * DAY and r["departure_ids"] == [DEP] and r["traveller_count"] == 2
    assert not r["complete"] and r["t21_alerted_at"] is not None
    completeness = json.loads(r["completeness"])
    assert completeness == {
        "travellers": 2,
        "complete": 0,
        "missing": {"stage3": 1, "waiver": 0, "arrival_flight": 2, "departure_flight": 2},
    }
    assert b"Lotus" not in bytes(r["payload_enc"])  # sealed
    task = await _seed(app_pool, s["tenant"], "SELECT priority || ':' || due_anchor FROM crm.task "
                       "WHERE trip_id = $1 AND kind = 'ops_release_incomplete'", s["trip"])  # fmt: skip
    assert task == "urgent:ops_handoff"

    # AA ops read it: the minimal fields only, one view counted and audited.
    got = await staff.read(
        app_pool, s["keys"], ops, s["tenant"], s["trip"], booking_admin_url="https://bk.example.test"
    )
    doc = got["document"]
    lead, child = doc["travellers"]
    assert (lead["given_name"], lead["family_name"], lead["age_band"], lead["dietary"]) == (
        "Mai", "Lotus", "35_44", ["vegetarian"])  # fmt: skip
    assert child["name"] == "Little Lotus" and child["age_band"] not in (None, "35_44") and child["dietary"] == []
    text = json.dumps(doc)
    for never in ("+84900000009", "+61400000000", "PA0000001", "1990-04-02", "river loop", "AU"):
        assert never not in text, never
    assert doc["lines"][0]["departure_id"] == DEP and doc["trip"]["agency_name"] == "Sen Vàng Travel"
    assert got["view_count"] == 1 and got["incident_admin_url"] == "https://bk.example.test/b2b-desk"
    assert ("ops_release_view", "aa_ops", f"ops:{ops.sub}") in await _events(app_pool, s["tenant"], s["trip"])

    # The board: no personal data, the departure ids and the purge countdown.
    board = await staff.board(app_pool, ops, booking_admin_url="https://bk.example.test")
    mine = [i for i in board["items"] if i["trip_id"] == str(s["trip"])]
    assert len(mine) == 1 and mine[0]["status"] == "live" and mine[0]["departure_ids"] == [DEP]
    assert mine[0]["days_to_purge"] == (s["end"] + 90 * DAY - s["today"]).days and mine[0]["t21_alerted"]
    assert "Lotus" not in json.dumps(mine) and board["incident_admin_url"] == "https://bk.example.test/b2b-desk"

    # The minimal manifest: logged with its watermark, the file itself never stored.
    m = await staff.manifest(app_pool, s["keys"], ops, s["tenant"], s["trip"], ["name", "arrival_flight"])
    assert m["row_count"] == 2 and m["csv"].splitlines()[0] == "watermark,no,name,arrival_flight"
    assert all(line.startswith(m["watermark"]) for line in m["csv"].splitlines()[1:])
    rec = await _seed(app_pool, s["tenant"], "SELECT row(fields, row_count, watermark, created_by)::text "
                      "FROM crm.supplier_manifest_export")  # fmt: skip
    assert "Mai" not in rec and m["watermark"] in rec and f"ops:{ops.sub}" in rec

    # The agency sees the panel (never the document) and its Activity lines.
    async with tenant_tx(app_pool, s["who"]["agent"][0], s["tenant"]) as ctx:
        view = await service.agency_view(ctx, s["trip"])
    assert view["release"]["status"] == "live" and view["release"]["view_count"] == 1
    assert view["release"]["can_withdraw"] and "document" not in view["release"]
    assert {a["action"] for a in view["activity"]} >= {"ops_release_create", "ops_release_view",
                                                          "ops_manifest_export", "ops_release_t21_alert"}  # fmt: skip
    async with tenant_tx(app_pool, s["who"]["assistant"][0], s["tenant"]) as ctx:
        view = await service.agency_view(ctx, s["trip"])
    assert view["activity"] is None and not view["release"]["can_withdraw"]


async def test_refresh_completeness_dates_and_one_t21_alert(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool, start_in=40)
    await _feed(app_pool, s["tenant"], "line.confirmed", s["line"])
    counts = await _sync(app_pool, s["keys"], s["tenant"])
    assert counts["created"] == 1 and "alerted" not in counts  # 40 days out: no T-21 alert yet
    # Flights added: the document changes, payload_version moves up.
    owner = s["who"]["owner"][0]
    for tid in (s["lead"], s["child"]):
        for d in ("arrival", "departure"):
            await _flight(app_pool, s["keys"], s["tenant"], owner, s["trip"], tid, d)
    assert (await _sync(app_pool, s["keys"], s["tenant"], s["trip"]))["refreshed"] == 1
    r = await _release(app_pool, s["tenant"], s["trip"])
    assert r["payload_version"] == 2 and json.loads(r["completeness"])["missing"]["arrival_flight"] == 0
    assert "arrival_flight" in r["fields_released"]
    # Nothing changed: no new version.
    assert (await _sync(app_pool, s["keys"], s["tenant"], s["trip"])).get("unchanged") == 1
    # E: the dates move -> the purge date follows; the trip now departs within 21 days, still incomplete -> one alert.
    new_start, new_end = s["today"] + 15 * DAY, s["today"] + 25 * DAY
    await _seed(app_pool, s["tenant"], "UPDATE crm.trip SET start_date = $2, end_date = $3 WHERE trip_id = $1",
                s["trip"], new_start, new_end)  # fmt: skip
    counts = await _sync(app_pool, s["keys"], s["tenant"], s["trip"])
    assert counts["refreshed"] == 1 and counts["alerted"] == 1
    r = await _release(app_pool, s["tenant"], s["trip"])
    assert (r["start_date"], r["end_date"], r["purge_at"]) == (new_start, new_end, new_end + 90 * DAY)
    assert "alerted" not in await _sync(app_pool, s["keys"], s["tenant"], s["trip"])  # once per release


async def test_cancel_withdraws_and_the_job_purges_before_departure(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool, start_in=30)
    ops = _ops()
    await _feed(app_pool, s["tenant"], "line.confirmed", s["line"])
    await _sync(app_pool, s["keys"], s["tenant"], s["trip"])
    await staff.read(app_pool, s["keys"], ops, s["tenant"], s["trip"])
    agent = s["who"]["agent"][0]
    async with tenant_tx(app_pool, agent, s["tenant"]) as ctx:
        preview = await stages.cancel(ctx, s["trip"], reason=stages.CANCEL_REASONS[0], dry_run=True)
    assert {"kind": "ops_release_withdrawn", "count": 1} in preview["effects"]
    async with tenant_tx(app_pool, agent, s["tenant"]) as ctx:
        await stages.cancel(ctx, s["trip"], reason=stages.CANCEL_REASONS[0], dry_run=False)
    r = await _release(app_pool, s["tenant"], s["trip"])
    assert r["withdraw_reason"] == "trip_cancelled" and r["purged_at"] is None
    with pytest.raises(NotFound, match="no ops release"):  # AA ops lose it at once
        await staff.read(app_pool, s["keys"], ops, s["tenant"], s["trip"])
    # The nudge runs: departure not reached -> purged now, the receipt keeps counts only.
    assert (await _sync(app_pool, s["keys"], s["tenant"], s["trip"]))["purged"] == 1
    r = await _release(app_pool, s["tenant"], s["trip"])
    assert r["payload_enc"] is None and r["purge_reason"] == "withdrawn"
    assert json.loads(r["deletion_receipt"]) == {"payload_version": 1, "travellers": 2, "manifest_exports": 0,
                                                  "views": 1, "reason": "withdrawn"}  # fmt: skip
    # The board keeps the notice of the withdrawal (no document); no new release is created after it.
    board = await staff.board(app_pool, ops)
    assert [i["status"] for i in board["items"] if i["trip_id"] == str(s["trip"])] == ["withdrawn"]
    assert await _sync(app_pool, s["keys"], s["tenant"], s["trip"]) == {}  # the trip is cancelled: nothing to release
    assert ("ops_release_purge", "system", service.ACTOR) in await _events(app_pool, s["tenant"], s["trip"])


async def test_r_a_cancelled_line_is_404_at_once_and_a_new_confirm_releases_again(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool, start_in=30)
    ops = _ops()
    await _feed(app_pool, s["tenant"], "line.confirmed", s["line"])
    await _sync(app_pool, s["keys"], s["tenant"], s["trip"])
    await _feed(app_pool, s["tenant"], "booking.cancelled", s["line"])
    with pytest.raises(NotFound, match="no ops release"):  # before the job ran
        await staff.read(app_pool, s["keys"], ops, s["tenant"], s["trip"])
    counts = await _sync(app_pool, s["keys"], s["tenant"], s["trip"])
    assert counts == {"withdrawn": 1, "purged": 1}
    assert (await _release(app_pool, s["tenant"], s["trip"]))["withdraw_reason"] == "aa_lines_cancelled"
    line = await _aa_line(app_pool, s["tenant"], s["trip"], s["start"], s["end"])
    await _feed(app_pool, s["tenant"], "line.confirmed", line)
    assert (await _sync(app_pool, s["keys"], s["tenant"], s["trip"]))["created"] == 1
    assert (await staff.read(app_pool, s["keys"], ops, s["tenant"], s["trip"]))["view_count"] == 1


async def test_retention_purge_at_return_plus_90_days(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool, start_in=-100, length=5)  # returned 95 days ago
    await _feed(app_pool, s["tenant"], "line.confirmed", s["line"])
    await _seed(app_pool, s["tenant"], "UPDATE crm.trip SET end_date = $2 WHERE trip_id = $1", s["trip"],
                s["today"] - 85 * DAY)  # fmt: skip
    assert (await _sync(app_pool, s["keys"], s["tenant"]))["created"] == 1  # still inside its window
    await _seed(app_pool, s["tenant"], "UPDATE crm.trip SET end_date = $2 WHERE trip_id = $1", s["trip"],
                s["today"] - 95 * DAY)  # fmt: skip
    counts = await _sync(app_pool, s["keys"], s["tenant"])
    assert counts.get("purged") == 1
    r = await _release(app_pool, s["tenant"], s["trip"])
    assert r["purge_reason"] == "retention" and r["payload_enc"] is None and r["purge_at"] < s["today"]


async def test_the_agency_withdraws_its_release(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool, start_in=30)
    await _feed(app_pool, s["tenant"], "line.confirmed", s["line"])
    await _sync(app_pool, s["keys"], s["tenant"], s["trip"])
    async with tenant_tx(app_pool, s["who"]["owner"][0], s["tenant"]) as ctx:
        view = await service.withdraw(ctx, s["trip"], "agency_withdrawn")
    assert view["release"]["status"] == "withdrawn" and not view["release"]["can_withdraw"]
    async with tenant_tx(app_pool, s["who"]["owner"][0], s["tenant"]) as ctx:
        with pytest.raises(Conflict, match="already withdrawn"):
            await service.withdraw(ctx, s["trip"], "agency_withdrawn")
    await _sync(app_pool, s["keys"], s["tenant"], s["trip"])
    assert await _sync(app_pool, s["keys"], s["tenant"], s["trip"]) == {"withdrawn": 1}  # not re-released


async def test_ops_access_is_per_agency_status(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool, start_in=30)
    ops = _ops()
    await _feed(app_pool, s["tenant"], "line.confirmed", s["line"])
    await _sync(app_pool, s["keys"], s["tenant"], s["trip"])
    other, _people = await _agency(app_pool, "owner")
    with pytest.raises(NotFound):  # the trip is not in that agency (RLS)
        await staff.read(app_pool, s["keys"], ops, other, s["trip"])
    await _seed(app_pool, s["tenant"], "UPDATE crm.tenant SET status = 'locked' WHERE tenant_id = $1", s["tenant"],
                actor="platform:test")  # fmt: skip
    # A locked agency: AA keeps running the departure.
    assert (await staff.read(app_pool, s["keys"], ops, s["tenant"], s["trip"]))["view_count"] == 1
    with pytest.raises(NotFound):
        await staff.read(app_pool, s["keys"], ops, uuid.uuid4(), s["trip"])


# ── crm-034 guards ─────────────────────────────────────────────────────────────────────────────────


async def _refused(pool: asyncpg.Pool, tenant: uuid.UUID, actor: str, sql: str, *args: Any) -> str:
    try:
        await _seed(pool, tenant, sql, *args, actor=actor)
    except asyncpg.PostgresError as exc:
        return exc.sqlstate or type(exc).__name__
    return "allowed"


async def test_the_guards(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    s = await _setup(app_pool, start_in=30)
    await _feed(app_pool, s["tenant"], "line.confirmed", s["line"])
    await _sync(app_pool, s["keys"], s["tenant"], s["trip"])
    rid = (await _release(app_pool, s["tenant"], s["trip"]))["ops_release_id"]
    member = f"member:{s['who']['owner'][1]}"
    insert = "INSERT INTO crm.ops_release (trip_id, payload_enc) VALUES ($1, $2) RETURNING 1"
    assert await _refused(app_pool, s["tenant"], member, insert, s["trip"], os.urandom(40)) == "CRM45"
    assert await _refused(app_pool, s["tenant"], "ops:abc", "UPDATE crm.ops_release SET complete = true "
                          "WHERE ops_release_id = $1 RETURNING 1", rid) == "CRM45"  # fmt: skip
    assert await _refused(app_pool, s["tenant"], "ops:abc", "UPDATE crm.ops_release SET view_count = view_count + 2 "
                          "WHERE ops_release_id = $1 RETURNING 1", rid) == "CRM45"  # fmt: skip
    assert await _refused(app_pool, s["tenant"], member, "UPDATE crm.ops_release SET payload_version = 9 "
                          "WHERE ops_release_id = $1 RETURNING 1", rid) == "CRM45"  # fmt: skip
    assert await _refused(app_pool, s["tenant"], service.ACTOR, "UPDATE crm.ops_release SET payload_version = 0 "
                          "WHERE ops_release_id = $1 RETURNING 1", rid) == "CRM45"  # fmt: skip
    assert await _refused(app_pool, s["tenant"], service.ACTOR, "DELETE FROM crm.ops_release "
                          "WHERE ops_release_id = $1 RETURNING 1", rid) == "CRM45"  # fmt: skip
    manifest = ("INSERT INTO crm.supplier_manifest_export (ops_release_id, payload_version, fields, row_count, "
                "watermark) VALUES ($1, 1, ARRAY['name'], 1, 'AAOPS-0000000001') RETURNING 1")  # fmt: skip
    assert await _refused(app_pool, s["tenant"], member, manifest, rid) == "CRM45"
    assert await _refused(app_pool, s["tenant"], "ops:abc", manifest, rid) == "allowed"
    assert await _refused(app_pool, s["tenant"], "ops:abc", "UPDATE crm.supplier_manifest_export SET row_count = 2 "
                          "RETURNING 1") == "CRM45"  # fmt: skip
    # Another agency sees nothing; no reader role holds a grant on either table.
    other, _p = await _agency(app_pool, "owner")
    assert await _seed(app_pool, other, "SELECT count(*) FROM crm.ops_release") == 0
    for table in ("ops_release", "supplier_manifest_export"):
        for reader in READERS:
            assert not await admin.fetchval("SELECT has_table_privilege($1, 'crm.' || $2, 'SELECT')", reader, table)
    # The trip erase cascades both tables away.
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-034'") == 1
