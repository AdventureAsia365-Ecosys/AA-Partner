"""AA-1070 end to end on PostgreSQL with the RDS role shape (crm-054 / crm-055), as crm_app through ``tenant_tx()`` /
``system_tx()`` / ``client_link_tx`` paths:

- passport check: a passport ending before the trip's end + 6 months is an issue (the trip party, the pipeline filter,
  the Booked event); the owner's 0 months clears it;
- rooming list: replaced whole, checked, carried into the ops release document (v2) and the departure manifest;
- vouchers: sealed details, released only for a booked trip's visible confirmed line;
- visa tracker: the agency template, the suggestions, an application with its copied checklist, the status moves,
  the masked number, the overdue task (once);
- departure roster: the Booking feed's guaranteed state, the roster and the merged manifest (audited);
- server PDFs: the job stores a sealed ``confirmation_pdf`` / ``voucher`` document, uploader ``system``.

Invented names and places only.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from app import pipeline
from app.booking import contract
from app.booking.sync import apply_event
from app.calendar import events as calendar_events
from app.documents import pdf_job
from app.documents.store import MemoryDocumentStore
from app.errors import Conflict, Invalid
from app.jobs.queue import Job
from app.ops_release import build
from app.tenancy import system_tx, tenant_tx
from app.travel_ops import departures, passport, rooming, visas, vouchers
from app.vault import Vault
from tests.integration.conftest import OWNER
from tests.integration.test_crm054_schema import purge_system_documents, reapply_crm055
from tests.integration.test_ops_release_api import DAY, DEP, FULL, _seed, _setup, _traveller

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest_asyncio.fixture(autouse=True)
async def _crm055_values(admin: asyncpg.Connection) -> AsyncIterator[None]:
    # The PDF job writes crm-055 document values: the same guard as test_crm054_schema (see purge_system_documents).
    await reapply_crm055(admin)
    yield
    await purge_system_documents(admin)


async def _world(pool: asyncpg.Pool) -> dict[str, Any]:
    s = await _setup(pool, start_in=20, length=6)
    # A third traveller whose passport ends one month after the trip: an issue under the 6-month rule.
    short = {**FULL, "given_name": "Tam", "passport_expiry": (s["end"] + 31 * DAY).isoformat(), "nationality": "VN"}
    s["short"] = await _traveller(pool, s["keys"], s["tenant"], s["who"]["owner"][0], name="Tam Orvane", **short)
    await _seed(pool, s["tenant"], "INSERT INTO crm.trip_traveler (trip_id, traveler_id, role) VALUES ($1, $2, "
                "'companion')", s["trip"], s["short"])  # fmt: skip
    await _seed(
        pool,
        s["tenant"],
        "UPDATE crm.booking_line SET status = 'confirmed', confirmed_at = now(), bk_booking_id = 'BKG-ORV-0001', "
        "visible_to_travelers = true WHERE booking_line_id = $1",
        s["line"],
        actor="job:test",
    )
    return s


def _agent(s: dict[str, Any]) -> Any:
    return s["who"]["agent"][0]


async def test_passport_check_party_board_and_booked_event(app_pool: asyncpg.Pool) -> None:
    s = await _world(app_pool)
    async with tenant_tx(app_pool, _agent(s), s["tenant"]) as ctx:
        vault = Vault(s["keys"], ctx)
        view = await passport.trip_view(ctx, vault, s["trip"], s["end"])
        status = {uuid.UUID(t["traveler_id"]): t["status"] for t in view["travellers"]}
        assert status == {s["lead"]: "ok", s["child"]: "missing", s["short"]: "expires_too_soon"}
        assert view["issues"] == 1 and view["valid_months"] == 6
        assert view["valid_until_needed"] == passport.add_months(s["end"], 6).isoformat()
        detail = await pipeline.trip_detail(ctx, vault, s["trip"])
        assert {p["passport"] for p in detail["party"]} == {"ok", "missing", "expires_too_soon"}
        board = await pipeline.board(ctx, vault, pipeline.BoardFilter(issue="passport"))
        booked = next(st for st in board["stages"] if st["stage"] == "booked")
        assert [c["trip_id"] for c in booked["cards"]] == [str(s["trip"])]
        assert booked["cards"][0]["passport_issues"] == 1
    fired: list[tuple[uuid.UUID, str]] = []

    async def fire(_ctx: Any, trip_id: uuid.UUID, name: str, event_id: uuid.UUID) -> int:
        fired.append((trip_id, name))
        return 1

    async with system_tx(app_pool, s["tenant"], actor="job:crm_workflow_tick") as ctx:
        counts = await passport.booked_events(ctx, Vault(s["keys"], ctx), fire)
    assert fired == [(s["trip"], "passport.invalid_for_trip")] and counts["passport_runs_started"] == 1
    owner = s["who"]["owner"][0]
    async with tenant_tx(app_pool, owner, s["tenant"]) as ctx:
        assert (await passport.update_settings(ctx, 0))["passport_valid_months"] == 0
        with pytest.raises(Invalid):
            await passport.update_settings(ctx, 25)
        assert await passport.issue_counts(ctx, Vault(s["keys"], ctx), [s["trip"]]) == {}


async def test_rooming_list_into_the_ops_release_and_the_manifest(app_pool: asyncpg.Pool) -> None:
    s = await _world(app_pool)
    rooms = [
        {"room_no": 1, "room_type": "twin", "travellers": [{"traveler_id": str(s["lead"])},
                                                           {"traveler_id": str(s["child"]), "bed_pref": "extra_bed"}]},
        {"room_no": 2, "room_type": "single", "travellers": [{"traveler_id": str(s["short"])}]},
    ]  # fmt: skip
    async with tenant_tx(app_pool, _agent(s), s["tenant"]) as ctx:
        vault = Vault(s["keys"], ctx)
        view = await rooming.replace(ctx, vault, s["trip"], rooms)
        assert [len(r["travellers"]) for r in view["rooms"]] == [2, 1] and view["unassigned"] == []
        with pytest.raises(Invalid) as err:
            await rooming.replace(ctx, vault, s["trip"], [{**rooms[1], "travellers": rooms[0]["travellers"]}])
        assert err.value.code == "room_over_capacity"
        # Moving the single traveller into room 1 drops room 2; leaving them out unassigns them.
        view = await rooming.replace(ctx, vault, s["trip"], rooms[:1])
        assert [u["traveler_id"] for u in view["unassigned"]] == [str(s["short"])]
        await rooming.replace(ctx, vault, s["trip"], rooms)
        trip = await ctx.conn.fetchrow(
            "SELECT trip_id, start_date, end_date, party_adults, party_child_ages FROM crm.trip WHERE trip_id = $1",
            s["trip"],
        )
        built = await build.document(ctx, vault, trip, [])
    assert '"v":2' in built.text and '"room":{"bed_pref":"extra_bed","room_no":1,"room_type":"twin"}' in built.text
    assert "room" in built.fields
    # The lead's portal save names the travellers only (no bed_pref key): the agent's extra bed stays.
    named_only = [{**r, "travellers": [{"traveler_id": p["traveler_id"]} for p in r["travellers"]]} for r in rooms]
    async with tenant_tx(app_pool, _agent(s), s["tenant"]) as ctx:
        view = await rooming.replace(ctx, Vault(s["keys"], ctx), s["trip"], named_only)
    beds = {p["traveler_id"]: p["bed_pref"] for r in view["rooms"] for p in r["travellers"]}
    assert beds[str(s["child"])] == "extra_bed" and beds[str(s["lead"])] is None
    async with tenant_tx(app_pool, _agent(s), s["tenant"]) as ctx:
        out = await departures.manifest(ctx, Vault(s["keys"], ctx), DEP, ["name", "room", "passport_check"])
        events = list(ctx.audit_events)
    assert out["row_count"] == 3 and "1 twin, extra_bed" in out["csv"] and "expires_too_soon" in out["csv"]
    assert out["csv"].splitlines()[0] == "watermark,no,name,room,passport_check"
    assert [(e.action, e.reason) for e in events][-1] == ("report_export", "departure_manifest")
    reveals = [e for e in events if e.action == "reveal"]
    assert len(reveals) == 3 and {e.reason for e in reveals} == {"duty_of_care"}
    async with tenant_tx(app_pool, _agent(s), s["tenant"]) as ctx:
        with pytest.raises(Invalid) as err:
            await departures.manifest(ctx, Vault(s["keys"], ctx), DEP, ["name", "dietary"])
    assert err.value.code == "reason_required"


async def test_vouchers_sealed_and_released(app_pool: asyncpg.Pool) -> None:
    s = await _world(app_pool)
    async with tenant_tx(app_pool, _agent(s), s["tenant"]) as ctx:
        vault = Vault(s["keys"], ctx)
        saved = await vouchers.save_details(
            ctx, vault, s["line"], {"pickup": "Orvane Riverside Inn lobby", "service_time": "07:30", "pnr": "QX7Z9"}
        )
        assert saved["service_time"] == "07:30" and saved["dropoff"] is None
        raw = await ctx.conn.fetchrow("SELECT pickup_enc, pnr_enc FROM crm.booking_line_voucher WHERE booking_line_id"
                                      " = $1", s["line"])  # fmt: skip
        assert b"Orvane" not in bytes(raw["pickup_enc"]) and b"QX7Z9" not in bytes(raw["pnr_enc"])
        agent_view = await vouchers.trip_vouchers(ctx, vault, s["trip"])
        (v,) = agent_view["vouchers"]
        assert v["released"] and v["pickup"] == "Orvane Riverside Inn lobby"
        assert v["supplier"]["name"] == "Adventure Asia"
        assert v["supplier"]["aa_booking_ref"] == "BKG-ORV-" and agent_view["lead_traveller"] == "Mai Lotus"
    await _seed(app_pool, s["tenant"], "UPDATE crm.booking_line SET visible_to_travelers = false "
                "WHERE booking_line_id = $1", s["line"], actor="job:test")  # fmt: skip
    async with tenant_tx(app_pool, _agent(s), s["tenant"]) as ctx:
        vault = Vault(s["keys"], ctx)
        assert (await vouchers.trip_vouchers(ctx, vault, s["trip"], travellers_only=True))["vouchers"] == []
        assert (await vouchers.trip_vouchers(ctx, vault, s["trip"]))["vouchers"][0]["not_released_reason"] == (
            "line_hidden"
        )


async def test_visa_tracker(app_pool: asyncpg.Pool) -> None:
    s = await _world(app_pool)
    owner = s["who"]["owner"][0]
    async with tenant_tx(app_pool, owner, s["tenant"]) as ctx:
        tpl = await visas.create_template(ctx, {"country": "VN", "visa_type": "evisa", "processing_days": 5,
                                                "fee": "25.00", "fee_currency": "USD",
                                                "documents": ["Passport photo page", "Recent photo"],
                                                "official_url": "https://evisa.example.test"})  # fmt: skip
        with pytest.raises(Conflict):
            await visas.create_template(ctx, {"country": "VN", "visa_type": "evisa"})
    async with tenant_tx(app_pool, _agent(s), s["tenant"]) as ctx:
        vault = Vault(s["keys"], ctx)
        sug = await visas.suggest(ctx, vault, s["trip"])
        # The Vietnamese traveller needs none for Vietnam; the Australian lead and the child (no nationality) do.
        assert {(x["traveler_id"], x["country"]) for x in sug["suggestions"]} == {
            (str(s["lead"]), "VN"), (str(s["child"]), "VN"),
        }  # fmt: skip
        assert sug["suggestions"][0]["template"]["visa_template_id"] == tpl["visa_template_id"]
        app = await visas.create(ctx, vault, s["trip"], {"traveler_id": str(s["lead"]), "country": "VN"})
        assert [i["label"] for i in app["checklist"]] == ["Passport photo page", "Recent photo"]
        assert app["deadline"] == (s["start"] - dt.timedelta(days=5 + visas.DEADLINE_MARGIN_DAYS)).isoformat()
        app_id = uuid.UUID(app["visa_application_id"])
        with pytest.raises(Conflict):
            await visas.create(ctx, vault, s["trip"], {"traveler_id": str(s["lead"]), "country": "VN"})
        with pytest.raises(Conflict):
            await visas.update(ctx, vault, app_id, {"status": "approved"})
        with pytest.raises(Invalid):
            await visas.update(ctx, vault, app_id, {"status": "collecting", "visa_no": "EV1234567"})
        await visas.update(ctx, vault, app_id, {"status": "submitted"})
        done = await visas.update(ctx, vault, app_id, {"status": "approved", "visa_no": "EV1234567",
                                                       "valid_until": s["end"] + 30 * DAY})  # fmt: skip
        assert done["visa_no"] == "EV1234567" and done["submitted_on"] and done["decided_on"]
        listed = await visas.trip_applications(ctx, vault, s["trip"])
        assert listed["applications"][0]["visa_no"] == "•••567"
        await visas.set_item(ctx, uuid.UUID(app["checklist"][0]["visa_checklist_item_id"]), True)
        child = await visas.create(ctx, vault, s["trip"], {"traveler_id": str(s["child"]), "country": "VN",
                                                           "visa_type": "visa_on_arrival",
                                                           "deadline": s["today"] - DAY})  # fmt: skip
        assert child["checklist"] == []
    async with system_tx(app_pool, s["tenant"], actor="job:crm_workflow_tick") as ctx:
        first = await visas.overdue_tasks(ctx, s["today"])
        again = await visas.overdue_tasks(ctx, s["today"])
        tasks = await ctx.conn.fetchval(
            "SELECT count(*) FROM crm.task WHERE trip_id = $1 AND kind = 'visa_deadline_overdue' AND status = 'open'",
            s["trip"],
        )
    assert first["visa_tasks_opened"] == 1 and again["visa_tasks_opened"] == 0 and tasks == 1
    async with tenant_tx(app_pool, _agent(s), s["tenant"]) as ctx:
        await visas.delete(ctx, uuid.UUID(child["visa_application_id"]))
        assert len((await visas.trip_applications(ctx, Vault(s["keys"], ctx), s["trip"]))["applications"]) == 1


async def test_departure_roster_from_the_feed(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    s = await _world(app_pool)
    await _seed(app_pool, s["tenant"], "INSERT INTO crm.booking_feed_state DEFAULT VALUES", actor="job:test")
    ev = contract.event({"event_id": f"ev_{uuid.uuid4().hex[:12]}", "type": "departure.guaranteed",
                         "departure_id": DEP, "occurred_at": "2026-10-30T01:00:00Z"})  # fmt: skip
    async with system_tx(app_pool, s["tenant"], actor="job:crm_booking_sync") as ctx:
        assert await apply_event(ctx, ev) == ("applied", "departure_guaranteed")
    older = contract.event({"event_id": f"ev_{uuid.uuid4().hex[:12]}", "type": "departure.cancelled",
                            "departure_id": DEP, "occurred_at": "2026-10-29T01:00:00Z"})  # fmt: skip
    async with system_tx(app_pool, s["tenant"], actor="job:crm_booking_sync") as ctx:
        await apply_event(ctx, older)  # older than the stored event: nothing changes
    async with tenant_tx(app_pool, s["who"]["assistant"][0], s["tenant"]) as ctx:
        listed = await departures.roster_list(ctx, "upcoming", s["today"])
        (row,) = [d for d in listed["departures"] if d["departure_id"] == DEP]
        assert row["state"]["status"] == "guaranteed" and row["trips"] == 1 and row["pax"]["adults"] >= 1
        roster = await departures.roster(ctx, Vault(s["keys"], ctx), DEP)
        assert len(roster["travellers"]) == 3 and roster["trips"][0]["passport_issues"] == 1
        with pytest.raises(asyncpg.PostgresError):
            async with ctx.conn.transaction():
                await ctx.conn.execute("DELETE FROM crm.aa_departure_state")  # a member never writes it (CRM53)
    async with tenant_tx(app_pool, _agent(s), s["tenant"]) as ctx:
        cal = await calendar_events.agency_calendar(ctx, start=s["start"] - DAY, end=s["start"] + DAY, mine=False)
    (aa,) = [e for e in cal["events"] if e["kind"] == "aa_departure"]
    assert aa["departure_id"] == DEP and aa["departure_status"] == "guaranteed" and aa["trips"] == 1
    # The feed row makes crm_booking_sync read this agency's feed in later tests (they count the agencies called).
    async with admin.transaction():
        await admin.execute(f"SET LOCAL ROLE {OWNER}")
        await admin.execute("SELECT set_config('app.tenant_id', $1, true)", str(s["tenant"]))
        await admin.execute("DELETE FROM crm.booking_feed_state")


async def test_pdf_job_stores_sealed_documents(app_pool: asyncpg.Pool) -> None:
    s = await _world(app_pool)
    store = MemoryDocumentStore()
    handler = pdf_job.build_handler(s["keys"], store)
    async with tenant_tx(app_pool, _agent(s), s["tenant"]) as ctx:
        queued = await pdf_job.request(ctx, s["trip"], "confirmation", None)
        assert queued == {"queued": True, "kind": "confirmation", "document_kind": "confirmation_pdf"}
        with pytest.raises(Invalid):
            await pdf_job.request(ctx, s["trip"], "invoice", None)
    for kind, subject in (("confirmation", None), ("voucher", s["line"])):
        payload = {"tenant_id": str(s["tenant"]), "trip_id": str(s["trip"]), "kind": kind}
        if subject:
            payload["subject_id"] = str(subject)
        out = await handler(app_pool, Job(id="1", kind="crm_pdf_render", payload=payload, attempt=1, max_attempts=3))
        assert out["byte_count"] > 1000
    async with tenant_tx(app_pool, _agent(s), s["tenant"]) as ctx:
        rows = await ctx.conn.fetch(
            "SELECT kind, uploaded_by_kind, visible_to_travelers, s3_key FROM crm.document WHERE trip_id = $1 "
            "ORDER BY kind",
            s["trip"],
        )
    assert [(r["kind"], r["uploaded_by_kind"], r["visible_to_travelers"]) for r in rows] == [
        ("confirmation_pdf", "system", False), ("voucher", "system", False),
    ]  # fmt: skip
    assert not (await store.get(rows[0]["s3_key"])).startswith(b"%PDF")  # sealed at rest
