"""AA-953 (card CRM-24a) end to end on PostgreSQL 15 with the RDS role shape: the client portal per person, as crm_app
through the portal link context, with real field and document encryption.

The party (invented): the lead with an address of their own, an adult companion with their own address, and a child
companion without one (reached through the lead's address, Fix E4). A second agency for the cross-agency checks.

- **E** (card): two travellers on one address get separate sessions after the "Who are you?" picker; the code of the
  child's own link goes to the lead's address; a pick outside the address's group is 403; a session switches inside
  its group only.
- **R** (card): a companion's session cannot open the lead's passport scan (403 ``document_write_only``); per-person
  document visibility (another traveller's -> 403 ``document_not_yours``, unshared -> 403 ``document_not_shared``);
  another agency's document is not found and its session never signs in here.
- Structured flights (R33): the local time and zone at the airport, an overnight arrival lands the day before the
  trip starts; invalid fields are 422; locked before the deposit.
- Cancelled lines hidden, no prices; the accepted itinerary is not required.
- Companion e-mails asked at the invite: the lead gives the child an address (they leave the lead's group), never
  another client's address, never over one on file; a companion cannot invite.
- A lost trip has no portal.
"""

from __future__ import annotations

import base64
import datetime as dt
import uuid
from typing import Any

import asyncpg
import pytest

from app import stages
from app.documents import service as documents
from app.documents.store import MemoryDocumentStore
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.portal import access
from app.portal import public as portal
from app.settings import Settings
from app.tenancy import tenant_tx
from app.travellers import service as travellers
from app.vault import Vault
from tests.integration.test_crm005_intake import _agency, _keys

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ECHO = Settings(environment="test", db_ssl="disable", portal_otp_dev_echo=True)
PDF = b"%PDF-1.4\n% invented test document\n"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def _token(path: str) -> str:
    return path.rsplit("/", 1)[1]


async def _party(pool: asyncpg.Pool, *, book: bool = True) -> dict[str, Any]:
    tenant_id, people = await _agency(pool, "owner", "agent")
    agent, agent_member = people[1]
    keys = _keys()
    trip = uuid.uuid4()
    async with tenant_tx(pool, agent, tenant_id) as ctx:
        vault = Vault(keys, ctx)
        lead = uuid.UUID(
            (
                await travellers.create_traveler(
                    ctx, vault, {"display_name": "Mira Castell", "email": "mira.castell@example.test"}
                )
            )["traveler_id"]
        )
        await ctx.conn.execute(
            "INSERT INTO crm.trip (trip_id, title, owner_member_id, start_date, end_date, intake_completeness) "
            "VALUES ($1, 'Nordic lakes 8 nights', $2, current_date + 60, current_date + 68, "
            '\'{"s1": 100, "s2": 100, "s3": 0}\')',
            trip,
            agent_member,
        )
        await ctx.conn.execute(
            "INSERT INTO crm.trip_traveler (trip_id, traveler_id, role) VALUES ($1, $2, 'lead')", trip, lead
        )
        adult = uuid.UUID(
            (
                await travellers.add_to_trip(
                    ctx, vault, trip, new={"display_name": "Oskar Lindvall", "email": "oskar.l@example.test"}
                )
            )["traveler_id"]
        )
        child = uuid.UUID((await travellers.add_to_trip(ctx, vault, trip, new={"display_name": "Pip Castell"}))[
            "traveler_id"
        ])  # fmt: skip
        await stages.move(ctx, trip, "qualifying")
        await stages.move(ctx, trip, "proposal")
        if book:  # the deposit was taken outside the CRM: Stage 3 opens (crm.portal_unlocked)
            await stages.move(ctx, trip, "booked", vault=vault, override_reason="manual_booking")
        invited = await access.invite_all(ctx, trip)
    links = {uuid.UUID(i["traveler_id"]): _token(i["link_path"]) for i in invited["invited"]}
    return {
        "tenant": tenant_id,
        "agent": agent,
        "keys": keys,
        "trip": trip,
        "lead": lead,
        "adult": adult,
        "child": child,
        "links": links,
        "store": MemoryDocumentStore(),
    }


async def _code(pool: asyncpg.Pool, s: dict[str, Any], token: str) -> dict[str, Any]:
    return await portal.request_code(pool, s["keys"], ECHO, token, purpose="sign_in", session=None, origin=None)


async def _sign_in(pool: asyncpg.Pool, s: dict[str, Any], token: str, who: uuid.UUID | None = None) -> str:
    sent = await _code(pool, s, token)
    session, answer = await portal.verify_code(
        pool, ECHO, token, sent["dev_code"], origin=None, traveler_id=who, keys=s["keys"]
    )
    assert session is not None and answer["signed_in"], answer
    return session


async def _home(pool: asyncpg.Pool, s: dict[str, Any], token: str, session: str | None) -> dict[str, Any]:
    return await portal.home(pool, token, session, s["keys"])


async def _upload(pool: asyncpg.Pool, s: dict[str, Any], **kw: Any) -> uuid.UUID:
    async with tenant_tx(pool, s["agent"], s["tenant"]) as ctx:
        saved = await documents.upload(
            ctx,
            Vault(s["keys"], ctx),
            s["store"],
            traveler_id=kw.get("traveler_id"),
            trip_id=kw.get("trip_id", s["trip"]),
            kind=kw.get("kind", "ticket"),
            filename=kw.get("filename", "ticket.pdf"),
            content_type="application/pdf",
            content=kw.get("content", PDF),
            visible_to_travelers=kw.get("visible", True),
        )
    return uuid.UUID(saved["document_id"])


async def test_e_two_travellers_on_one_address_get_separate_sessions(app_pool: asyncpg.Pool) -> None:
    s = await _party(app_pool)
    lead_link, child_link, adult_link = s["links"][s["lead"]], s["links"][s["child"]], s["links"][s["adult"]]
    # The child has no address: their own link's code goes to the lead's address (the address itself:
    # tests/unit/test_portal_person.py; this agency has no mailbox, so the Dev echo carries the code).
    sent = await _code(app_pool, s, child_link)
    assert sent["dev_code"]
    session, answer = await portal.verify_code(
        app_pool, ECHO, child_link, sent["dev_code"], origin=None, keys=s["keys"]
    )
    assert session is None and answer["signed_in"] is False
    picks = {p["traveler_id"]: p for p in answer["pick"]}
    assert set(picks) == {str(s["lead"]), str(s["child"])}  # the adult companion has their own address
    assert picks[str(s["lead"])]["name"] == "Mira Castell" and picks[str(s["child"])]["link"] is True
    # The same (still pending) code with a choice outside the group: 403, nothing opened.
    with pytest.raises(Forbidden) as err:
        await portal.verify_code(
            app_pool, ECHO, child_link, sent["dev_code"], origin=None, traveler_id=s["adult"], keys=s["keys"]
        )
    assert err.value.code == "not_your_traveller"
    as_child, _ = await portal.verify_code(
        app_pool, ECHO, child_link, sent["dev_code"], origin=None, traveler_id=s["child"], keys=s["keys"]
    )
    as_lead = await _sign_in(app_pool, s, child_link, s["lead"])
    assert as_child and as_lead and as_child != as_lead
    child_home = await _home(app_pool, s, child_link, as_child)
    lead_home = await _home(app_pool, s, child_link, as_lead)
    assert child_home["me"] == {"traveler_id": str(s["child"]), "name": "Pip Castell", "role": "companion"}
    assert "payments" not in child_home and "party" not in child_home
    assert child_home["switch_to"] == [{"traveler_id": str(s["lead"]), "name": "Mira Castell", "role": "lead"}]
    assert lead_home["me"]["traveler_id"] == str(s["lead"]) and lead_home["role"] == "lead"
    party = {p["traveler_id"]: p for p in lead_home["party"]}
    assert party[str(s["child"])]["via_you"] and not party[str(s["adult"])]["via_you"]
    assert party[str(s["adult"])]["name"] == "Oskar Lindvall" and party[str(s["adult"])]["has_email"]
    # The lead's own link reaches the same two; the adult's link reaches only the adult (no picker).
    assert {p["traveler_id"] for p in (await _verify_pick(app_pool, s, lead_link))} == set(picks)
    adult_session = await _sign_in(app_pool, s, adult_link)
    assert (await _home(app_pool, s, adult_link, adult_session))["me"]["traveler_id"] == str(s["adult"])
    # A session of one address never opens a link of another address.
    assert (await _home(app_pool, s, adult_link, as_child))["signed_in"] is False
    assert (await _home(app_pool, s, lead_link, adult_session))["signed_in"] is False
    # Switching inside the group, never outside it.
    await portal.switch(app_pool, ECHO, child_link, as_lead, {"traveler_id": str(s["child"])}, origin=None)
    assert (await _home(app_pool, s, child_link, as_lead))["me"]["traveler_id"] == str(s["child"])
    with pytest.raises(Forbidden):
        await portal.switch(app_pool, ECHO, child_link, as_lead, {"traveler_id": str(s["adult"])}, origin=None)
    # Signing out ends that session only.
    await portal.sign_out(app_pool, child_link, as_child)
    assert (await _home(app_pool, s, child_link, as_child))["signed_in"] is False
    assert (await _home(app_pool, s, child_link, as_lead))["signed_in"] is True


async def _verify_pick(pool: asyncpg.Pool, s: dict[str, Any], token: str) -> list[dict[str, Any]]:
    sent = await _code(pool, s, token)
    session, answer = await portal.verify_code(pool, ECHO, token, sent["dev_code"], origin=None, keys=s["keys"])
    assert session is None
    return answer["pick"]


async def test_r_a_companion_cannot_open_the_leads_passport(app_pool: asyncpg.Pool) -> None:
    s = await _party(app_pool)
    lead_link, adult_link = s["links"][s["lead"]], s["links"][s["adult"]]
    lead = await _sign_in(app_pool, s, lead_link, s["lead"])
    adult = await _sign_in(app_pool, s, adult_link)
    scan = {"filename": "passport.jpg", "content_type": "image/jpeg", "content_b64": base64.b64encode(JPEG).decode()}
    saved = await portal.upload_passport_scan(app_pool, s["keys"], s["store"], ECHO, lead_link, lead, scan, origin=None)
    assert saved == {"received": True, "kind": "passport_scan", "size_bytes": len(JPEG)}
    assert (await _home(app_pool, s, lead_link, lead))["passport_scan_on_file"] is True
    for _ in range(4):  # five live scans at most
        await portal.upload_passport_scan(app_pool, s["keys"], s["store"], ECHO, lead_link, lead, scan, origin=None)
    with pytest.raises(Conflict) as err:
        await portal.upload_passport_scan(app_pool, s["keys"], s["store"], ECHO, lead_link, lead, scan, origin=None)
    assert err.value.code == "scan_limit"
    async with tenant_tx(app_pool, s["agent"], s["tenant"]) as ctx:
        passport = await ctx.conn.fetchval(
            "SELECT document_id FROM crm.document WHERE traveler_id = $1 AND kind = 'passport_scan' "
            "AND uploaded_by_kind = 'traveler' AND write_only AND NOT visible_to_travelers LIMIT 1",
            s["lead"],
        )
    assert passport is not None
    ticket = await _upload(app_pool, s, traveler_id=s["adult"], filename="oskar-ticket.pdf")
    voucher = await _upload(app_pool, s, traveler_id=s["lead"], kind="voucher", filename="mira-voucher.pdf")
    shared = await _upload(app_pool, s, kind="other", filename="packing-list.pdf")
    private = await _upload(app_pool, s, kind="other", filename="agency-notes.pdf", visible=False)

    listed = {d["filename"]: d for d in (await _home(app_pool, s, adult_link, adult))["documents"]}
    assert set(listed) == {"oskar-ticket.pdf", "packing-list.pdf"}
    assert listed["oskar-ticket.pdf"]["for"] == "me" and listed["packing-list.pdf"]["for"] == "trip"
    opened = await portal.open_document(app_pool, s["keys"], s["store"], adult_link, adult, ticket)
    assert base64.b64decode(opened["content_b64"]) == PDF and opened["filename"] == "oskar-ticket.pdf"
    for document, code in (
        (passport, "document_write_only"),  # card R
        (voucher, "document_not_yours"),
        (private, "document_not_shared"),
    ):
        with pytest.raises(Forbidden) as err:
            await portal.open_document(app_pool, s["keys"], s["store"], adult_link, adult, document)
        assert (err.value.code, err.value.status_code) == (code, 403)
    with pytest.raises(Forbidden) as err:  # never shown back, to its own traveller either
        await portal.open_document(app_pool, s["keys"], s["store"], lead_link, lead, passport)
    assert err.value.code == "document_write_only"
    lead_docs = {d["filename"] for d in (await _home(app_pool, s, lead_link, lead))["documents"]}
    assert lead_docs == {"mira-voucher.pdf", "packing-list.pdf"}
    assert (await portal.open_document(app_pool, s["keys"], s["store"], lead_link, lead, shared))["filename"]
    with pytest.raises(Forbidden):  # no session: nothing opens
        await portal.open_document(app_pool, s["keys"], s["store"], adult_link, None, ticket)

    # Another agency: its documents are not found here, its sessions never sign in here.
    other = await _party(app_pool)
    other_ticket = await _upload(app_pool, other, traveler_id=other["adult"], filename="other.pdf")
    other_session = await _sign_in(app_pool, other, other["links"][other["adult"]])
    with pytest.raises(NotFound):
        await portal.open_document(app_pool, s["keys"], s["store"], adult_link, adult, other_ticket)
    assert (await _home(app_pool, s, adult_link, other_session))["signed_in"] is False


async def test_flights_in_local_time_and_locked_before_the_deposit(app_pool: asyncpg.Pool) -> None:
    s = await _party(app_pool)
    link = s["links"][s["adult"]]
    session = await _sign_in(app_pool, s, link)
    start = dt.date.today() + dt.timedelta(days=60)
    night = (start - dt.timedelta(days=1)).isoformat()
    saved = await portal.save_flights(
        app_pool,
        s["keys"],
        ECHO,
        link,
        session,
        {
            "flights": [
                {
                    "direction": "arrival",
                    "flight_no": "fx 204",
                    "airport": "osl",
                    "local_time": f"{night}T23:50",
                    "tz": "Europe/Oslo",
                },
                {
                    "direction": "departure",
                    "flight_no": "FX205",
                    "airport": "BGO",
                    "local_time": "2031-01-02T07:15",
                    "tz": "Europe/Oslo",
                },
            ]
        },  # fmt: skip
        origin=None,
    )
    arrival = saved["flights"][0]
    assert arrival["direction"] == "arrival" and arrival["flight_no"] == "FX204" and arrival["airport"] == "OSL"
    assert arrival["days_from_start"] == -1 and arrival["local_date"] == night
    assert arrival["utc_offset"] in ("+01:00", "+02:00")
    home = await _home(app_pool, s, link, session)
    assert [f["direction"] for f in home["flights"]] == ["arrival", "departure"]
    async with tenant_tx(app_pool, s["agent"], s["tenant"]) as ctx:
        row = await ctx.conn.fetchrow(
            "SELECT flight_no_enc, entered_by_kind FROM crm.traveler_flight WHERE traveler_id = $1 "
            "AND direction = 'arrival'",
            s["adult"],
        )
    assert b"FX204" not in bytes(row["flight_no_enc"]) and row["entered_by_kind"] == "traveler"
    gone = await portal.save_flights(
        app_pool, s["keys"], ECHO, link, session, {"flights": [{"direction": "departure", "remove": True}]}, origin=None
    )
    assert [f["direction"] for f in gone["flights"]] == ["arrival"]
    with pytest.raises(Invalid) as err:
        await portal.save_flights(
            app_pool,
            s["keys"],
            ECHO,
            link,
            session,
            {
                "flights": [
                    {
                        "direction": "arrival",
                        "flight_no": "?",
                        "airport": "Oslo",
                        "local_time": "soon",
                        "tz": "Mars/Base",
                    }
                ]
            },  # fmt: skip
            origin=None,
        )
    assert err.value.extra["fields"] == ["flight_no", "airport", "local_time", "tz"]
    # The other travellers see none of it.
    lead_session = await _sign_in(app_pool, s, s["links"][s["lead"]], s["lead"])
    assert (await _home(app_pool, s, s["links"][s["lead"]], lead_session))["flights"] == []

    early = await _party(app_pool, book=False)
    early_link = early["links"][early["adult"]]
    early_session = await _sign_in(app_pool, early, early_link)
    with pytest.raises(Forbidden) as err:
        await portal.save_flights(
            app_pool, early["keys"], ECHO, early_link, early_session,
            {"flights": [{"direction": "arrival", "remove": True}]}, origin=None,
        )  # fmt: skip
    assert err.value.code == "stage3_locked"


async def test_cancelled_lines_are_hidden_and_no_price_is_shown(app_pool: asyncpg.Pool) -> None:
    s = await _party(app_pool)
    async with tenant_tx(app_pool, s["agent"], s["tenant"]) as ctx:
        supplier = await ctx.conn.fetchval(
            "INSERT INTO crm.agency_supplier (name, category) VALUES ('Fjord View Lodge', 'accommodation') "
            "RETURNING agency_supplier_id"
        )
        for title, visible, collect in (
            ("Lodge 3 nights", True, "agency"),
            ("Ferry pass", True, "on_arrival"),
            ("Old kayak day", True, "agency"),
            ("Internal hold", False, "agency"),
        ):
            await ctx.conn.execute(
                "INSERT INTO crm.booking_line (trip_id, agency_supplier_id, title, visible_to_travelers, collect, "
                "service_from) VALUES ($1, $2, $3, $4, $5, current_date + 60)",
                s["trip"],
                supplier,
                title,
                visible,
                collect,
            )
        # Cancelling a shown line hides it (R22; the database clears visible_to_travelers too).
        await ctx.conn.execute(
            "UPDATE crm.booking_line SET status = 'cancelled' WHERE trip_id = $1 AND title = 'Old kayak day'", s["trip"]
        )
    link = s["links"][s["adult"]]
    home = await _home(app_pool, s, link, await _sign_in(app_pool, s, link))
    assert [(x["title"], x["status"], x["pay_on_arrival"]) for x in home["services"]] == [
        ("Ferry pass", "planning", True),
        ("Lodge 3 nights", "planning", False),
    ]
    assert all(set(x) == {"title", "from", "to", "status", "confirm_class", "pay_on_arrival"} for x in home["services"])
    assert home["itinerary"] == []  # no accepted day-by-day proposal on this trip


async def test_companion_emails_are_asked_at_the_invite(app_pool: asyncpg.Pool) -> None:
    s = await _party(app_pool)
    lead_link, child_link, adult_link = s["links"][s["lead"]], s["links"][s["child"]], s["links"][s["adult"]]
    lead = await _sign_in(app_pool, s, lead_link, s["lead"])
    as_child = await _sign_in(app_pool, s, lead_link, s["child"])
    adult = await _sign_in(app_pool, s, adult_link)
    with pytest.raises(Forbidden) as err:
        await portal.invite_companion(
            app_pool, s["keys"], ECHO, adult_link, adult, {"traveler_id": str(s["child"]), "email": "x@example.test"},
            origin=None,
        )  # fmt: skip
    assert err.value.code == "lead_only"
    with pytest.raises(Conflict) as err:  # another client's address: refused without saying whose
        await portal.invite_companion(
            app_pool, s["keys"], ECHO, lead_link, lead,
            {"traveler_id": str(s["child"]), "email": "oskar.l@example.test"}, origin=None,
        )  # fmt: skip
    assert err.value.code == "email_in_use" and "traveler_id" not in err.value.extra
    with pytest.raises(Conflict) as err:  # never over an address on file
        await portal.invite_companion(
            app_pool, s["keys"], ECHO, lead_link, lead,
            {"traveler_id": str(s["adult"]), "email": "new@example.test"}, origin=None,
        )  # fmt: skip
    assert err.value.code == "email_on_file"
    invited = await portal.invite_companion(
        app_pool, s["keys"], ECHO, lead_link, lead,
        {"traveler_id": str(s["child"]), "email": "pip.castell@example.test"}, origin=None,
    )  # fmt: skip
    assert invited["reach"] == "own" and invited["link_path"].startswith("/c/t/")
    # The child left the lead's group: the session that acted as them through the lead's link has ended, the old
    # link was re-issued, and the new link's code goes to the child's own address.
    assert (await _home(app_pool, s, lead_link, as_child))["signed_in"] is False
    with pytest.raises(NotFound):
        await _home(app_pool, s, child_link, None)
    new_link = _token(invited["link_path"])
    sent = await _code(app_pool, s, new_link)
    session, answer = await portal.verify_code(app_pool, ECHO, new_link, sent["dev_code"], origin=None, keys=s["keys"])
    assert session is not None and answer["signed_in"]
    assert [p["traveler_id"] for p in await _verify_pick_or_none(app_pool, s, lead_link)] == []


async def _verify_pick_or_none(pool: asyncpg.Pool, s: dict[str, Any], token: str) -> list[dict[str, Any]]:
    sent = await _code(pool, s, token)
    session, answer = await portal.verify_code(pool, ECHO, token, sent["dev_code"], origin=None, keys=s["keys"])
    return [] if session is not None else answer["pick"]


async def test_a_lost_trip_has_no_portal(app_pool: asyncpg.Pool) -> None:
    s = await _party(app_pool, book=False)
    link = s["links"][s["lead"]]
    assert (await _home(app_pool, s, link, None))["signed_in"] is False
    async with tenant_tx(app_pool, s["agent"], s["tenant"]) as ctx:
        await stages.move(ctx, s["trip"], "lost", lost_reason="price")
    with pytest.raises(NotFound) as err:
        await _home(app_pool, s, link, None)
    assert err.value.code == "link_not_found"
