"""AA-1093 part 1 end to end on PostgreSQL with the RDS role shape (crm-063), as crm_app: an AA tour Adventure Asia no
longer serves.

- **Transition -> job row**: the catalog store's state write that retires a tour queues one
  ``crm_tour_retired_fanout`` row in its transaction; writing the same state again queues nothing.
- **Retired**: per agency, one flag per item (an option in a draft builder version, a held, a quoted and a booked AA
  line; a released line is left out), one task per trip (the trip owner, else the agency owner; normal priority when
  only booked lines are hit). Nothing on the lines, the option or the version changes. The same job run twice raises
  nothing new. Another agency sees none of it. The pipeline filter and the trip page card read the flags.
- **Reinstated**: the open flags are cleared, the trip gets one internal note, the task stays open; again = nothing.
- **Stale**: a retirement run after the reinstatement does nothing.

Invented names and places only.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from app import pipeline
from app.catalog import retired, store
from app.catalog.client import summary
from app.jobs.queue import Job
from app.permissions import Action
from app.proposals import aa_options, builder
from app.tenancy import tenant_tx
from app.vault import Vault
from tests.aa_fakes import FakeBooking, booking_client, tour_body
from tests.integration.conftest import OWNER
from tests.integration.test_aa1067_retail_payments_api import _rows
from tests.integration.test_builder_api import _as, _block, _create, _pid
from tests.integration.test_builder_api import _setup as _builder_setup
from tests.integration.test_crm010_schema import _agency as _bare_agency

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

D = Decimal
TOUR = "6c2d8e41-93a7-4b5f-8e20-1d4c7f9a0b13"
OTHER_TOUR = "a91f3b7c-2e64-4d08-b5c9-7e0a6d2f4c85"


@pytest_asyncio.fixture(autouse=True)
async def _clean(admin: asyncpg.Connection) -> Any:
    """The tour rows (platform cache) and the fan-out job rows of this file never outlive it: the catalog cache test
    of test_aa_lines_api.py counts every cached tour CIS no longer lists."""
    yield
    async with admin.transaction():
        await admin.execute(f"SET LOCAL ROLE {OWNER}")
        await admin.execute("DELETE FROM crm.aa_catalog_tour WHERE source_system = 'cis' AND source_id = ANY($1)",
                            [TOUR, OTHER_TOUR])  # fmt: skip
        await admin.execute("DELETE FROM crm.job WHERE kind = $1", retired.KIND)


async def _jobs(admin: asyncpg.Connection) -> list[dict[str, Any]]:
    async with admin.transaction():
        await admin.execute(f"SET LOCAL ROLE {OWNER}")
        rows = await admin.fetch("SELECT payload, idempotency_key, created_by FROM crm.job WHERE kind = $1 "
                                 "ORDER BY created_at, id", retired.KIND)  # fmt: skip
    return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]


async def _seed(pool: asyncpg.Pool, tenant: uuid.UUID, sql: str, *args: Any) -> Any:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
        await conn.execute("SELECT set_config('app.actor', 'job:test', true)")
        return await conn.fetchval(sql, *args)


async def _line(pool: asyncpg.Pool, tenant: uuid.UUID, trip: uuid.UUID, sub: str | None, *,
                booked: bool = False, tour: str = TOUR) -> uuid.UUID:  # fmt: skip
    line = await _seed(
        pool, tenant,
        "INSERT INTO crm.booking_line (trip_id, supplier_kind, agency_supplier_id, title, departure_id, service_from, "
        "service_to, aa_tour_source_id, aa_sub_status) VALUES ($1, 'aa', crm.ensure_aa_supplier(), "
        "'Lotus lake 3 days', 'dep-2027-lotus-a', current_date + 30, current_date + 32, $2, $3) "
        "RETURNING booking_line_id",
        trip, tour, sub,
    )  # fmt: skip
    if booked:
        await _seed(pool, tenant, "UPDATE crm.booking_line SET status = 'confirmed', confirmed_at = now(), "
                    "bk_booking_id = 'BKG-LOTUS-1', aa_sub_status = NULL WHERE booking_line_id = $1", line)  # fmt: skip
    return line


async def _aa_option(pool: asyncpg.Pool, s: dict[str, Any]) -> tuple[uuid.UUID, uuid.UUID]:
    v = await _create(pool, s)
    day2 = next(d for d in v["days"] if d["day_no"] == 2)
    v = await _as(pool, s, "agent", builder.add_block, _pid(v), v["edit_seq"], uuid.UUID(day2["proposal_day_id"]),
                  {"title": "Lake tour", "type": "activity"})  # fmt: skip
    body = {"tour_source_id": TOUR, "component_id": "tp:1", "component_kind": "trip_plan",
            "departure_id": "dep-2027-lotus-a", "sell_amount": D("400")}  # fmt: skip
    async with tenant_tx(pool, s["who"]["agent"], s["tenant"]) as ctx:
        plan = await builder.aa_plan(ctx, _pid(v), v["edit_seq"], uuid.UUID(_block(v, 2, 1)["option_block_id"]), body)
    quote = await aa_options.quote(booking_client(FakeBooking(b2b_price="250.00", availability="available")),
                                   s["tenant"], plan)  # fmt: skip
    v = await _as(pool, s, "agent", builder.add_aa_option, plan, quote, body)
    option = next(o["option_id"] for d in v["days"] for b in d["blocks"] for o in b["options"] if o["source"] == "aa")
    return _pid(v), uuid.UUID(option)


def _job(payload: dict[str, Any]) -> Job:
    return Job("00000000-0000-4000-8000-000000001093", retired.KIND, payload, 1, retired.MAX_ATTEMPTS)


async def _snapshot(pool: asyncpg.Pool, s: dict[str, Any]) -> list[Any]:
    lines = await _rows(pool, s, "SELECT booking_line_id, status, aa_sub_status, updated_at FROM crm.booking_line "
                        "ORDER BY booking_line_id")  # fmt: skip
    options = await _rows(pool, s, "SELECT option_id, aa_departure_id, updated_at FROM crm.option ORDER BY option_id")
    versions = await _rows(pool, s, "SELECT proposal_id, status, updated_at FROM crm.proposal ORDER BY proposal_id")
    return [dict(r) for r in [*lines, *options, *versions]]


async def test_a_retired_tour_flags_every_trip_that_uses_it_once(app_pool: asyncpg.Pool, admin: asyncpg.Connection
                                                                 ) -> None:  # fmt: skip
    body = tour_body(TOUR, "Lotus lake 3 days")
    assert await store.upsert(app_pool, summary(body), '"e1"', body) is None  # first seen: no transition
    s = await _builder_setup(app_pool)
    proposal, option = await _aa_option(app_pool, s)
    held = await _line(app_pool, s["tenant"], s["trip"], "held")
    quoted = await _line(app_pool, s["tenant"], s["trip"], "quoted")
    booked = await _line(app_pool, s["tenant"], s["trip"], None, booked=True)
    await _line(app_pool, s["tenant"], s["trip"], "released")  # nothing stands on it any more
    await _line(app_pool, s["tenant"], s["trip"], "held", tour=OTHER_TOUR)  # another tour
    # Agency C: one booked line only, a trip without an owner (the agency owner gets the task).
    c = await _bare_agency(app_pool)
    await _line(app_pool, c["tenant"], c["trip"], None, booked=True)
    other = await _builder_setup(app_pool)  # uses nothing of the tour
    before = await _snapshot(app_pool, s)

    assert await store.set_state(app_pool, TOUR, "retired", "source_deleted") == "retired"
    assert await store.set_state(app_pool, TOUR, "retired", "source_deleted") is None  # the same state again
    jobs = await _jobs(admin)
    assert len(jobs) == 1 and jobs[0]["payload"]["source_id"] == TOUR and jobs[0]["payload"]["purpose"] == "retired"
    assert jobs[0]["created_by"] == retired.QUEUED_BY
    handler = retired.build_handler(s["keys"])
    out = await handler(app_pool, _job(jobs[0]["payload"]))
    assert out["status"] == "done" and out["flag_count"] == 5 and out["task_count"] == 2 and out["trip_count"] == 2

    flags = await _rows(app_pool, s, "SELECT kind, option_id, booking_line_id, state, raised_key, created_by "
                        "FROM crm.tour_retired_flag ORDER BY kind")  # fmt: skip
    assert [(f["kind"], f["option_id"] or f["booking_line_id"]) for f in flags] == [
        ("confirmed_line", booked), ("held_line", held), ("open_option", option), ("quoted_line", quoted)]  # fmt: skip
    assert {f["state"] for f in flags} == {"open"} and {f["created_by"] for f in flags} == {retired.ACTOR}
    assert flags[0]["raised_key"] == f"retired:{jobs[0]['payload']['changed_at']}"
    tasks = await _rows(app_pool, s, "SELECT priority, status, assignee_member_id FROM crm.task WHERE kind = $1",
                        retired.TASK)  # fmt: skip
    assert [(t["priority"], t["status"], t["assignee_member_id"]) for t in tasks] == [
        ("urgent", "open", s["members"]["agent"])]  # fmt: skip
    c_tasks = await _rows(app_pool, {"tenant": c["tenant"]}, "SELECT priority, assignee_member_id FROM crm.task "
                          "WHERE kind = $1", retired.TASK)  # fmt: skip
    assert [(t["priority"], t["assignee_member_id"]) for t in c_tasks] == [("normal", c["member"])]
    # Nothing the agent or Booking owns moved: lines, the option and the version are as they were.
    assert await _snapshot(app_pool, s) == before
    assert proposal in {r["proposal_id"] for r in before if "proposal_id" in r}
    # Another agency: nothing of its own, nothing of the others.
    assert await _rows(app_pool, other, "SELECT 1 FROM crm.tour_retired_flag") == []
    assert await _rows(app_pool, other, "SELECT 1 FROM crm.task WHERE kind = $1", retired.TASK) == []

    # The same transition run again (a retried job): no new flag, no new task.
    again = await handler(app_pool, _job(jobs[0]["payload"]))
    assert again["status"] == "done" and "flag_count" not in again and "task_count" not in again
    assert len(await _rows(app_pool, s, "SELECT 1 FROM crm.tour_retired_flag")) == 4

    # The pipeline filter and the trip page card.
    async with tenant_tx(app_pool, s["who"]["assistant"], s["tenant"], action=Action.PIPELINE_VIEW) as ctx:
        vault = Vault(s["keys"], ctx)
        board = await pipeline.board(ctx, vault, pipeline.BoardFilter(issue="aa_tour_retired"))
        detail = await pipeline.trip_detail(ctx, vault, s["trip"])
    shown = [c for st in board["stages"] for c in st["cards"]]
    assert [c["trip_id"] for c in shown] == [str(s["trip"])] and shown[0]["aa_tour_retired"] is True
    assert detail["aa_tour_retired"] == [{
        "source_id": TOUR, "title": "Lotus lake 3 days", "raised_at": detail["aa_tour_retired"][0]["raised_at"],
        "items": {"open_option": 1, "held_line": 1, "quoted_line": 1, "confirmed_line": 1},
    }]  # fmt: skip

    # CIS serves the tour again: the flags clear, the trip gets one note, the task stays for the agent.
    notes_before = len(await _rows(app_pool, s, "SELECT 1 FROM crm.note WHERE trip_id = $1", s["trip"]))
    assert await store.upsert(app_pool, summary(body), '"e2"', body) == "reinstated"
    jobs = await _jobs(admin)
    assert [j["payload"]["purpose"] for j in jobs] == ["retired", "reinstated"]
    back = await handler(app_pool, _job(jobs[1]["payload"]))
    assert back["status"] == "done" and back["cleared_trip_count"] == 2 and back["note_count"] == 2
    flags = await _rows(app_pool, s, "SELECT state, cleared_key FROM crm.tour_retired_flag")
    assert {(f["state"], f["cleared_key"]) for f in flags} == {
        ("cleared", f"reinstated:{jobs[1]['payload']['changed_at']}")}  # fmt: skip
    notes = await _rows(app_pool, s, "SELECT kind, created_by FROM crm.note WHERE trip_id = $1", s["trip"])
    assert len(notes) == notes_before + 1 and retired.ACTOR in {n["created_by"] for n in notes}
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        detail = await pipeline.trip_detail(ctx, Vault(s["keys"], ctx), s["trip"])
    assert detail["aa_tour_retired"] == []
    assert any("offers the AA tour" in n["body"] and "Lotus lake 3 days" in n["body"] for n in detail["notes"])
    assert [t["status"] for t in await _rows(app_pool, s, "SELECT status FROM crm.task WHERE kind = $1",
                                             retired.TASK)] == ["open"]  # fmt: skip
    assert "note_count" not in await handler(app_pool, _job(jobs[1]["payload"]))  # again: nothing new

    # The retirement's row run late (after the reinstatement): stale, nothing raised.
    assert await handler(app_pool, _job(jobs[0]["payload"])) == {"status": "stale", "purpose": "retired"}
    assert {f["state"] for f in await _rows(app_pool, s, "SELECT state FROM crm.tour_retired_flag")} == {"cleared"}
