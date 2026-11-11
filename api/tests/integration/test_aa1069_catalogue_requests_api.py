"""AA-1069 end to end on PostgreSQL with the RDS role shape, as crm_app through ``tenant_tx()`` (crm-052, crm-053).

- The service catalogue: an agent adds a service with group-size tiers; the assistant reads it without the net and
  cannot change it (CRM52 -> 403 ``role_forbidden``); a supplier in the trash or the AA row is refused.
- "Add from catalogue" in the builder: the quantity follows the basis (3 travellers per person, the rooms, the nights),
  the net takes the tier of the party size, the option is a snapshot (deleting the service keeps it); a service in
  another currency needs a rate (409 ``fx_rate_missing``), an inactive or out-of-window one is refused; the assistant
  adds one too and never sees its net.
- Supplier requests: one draft per supplier of the trip's planning agency lines, in the supplier's own thread (never
  the client's conversation); a supplier without an e-mail gets no draft; sending marks the lines requested and the
  dispatcher sends it to the supplier's address from the agency mailbox; the supplier's reply threads on that
  conversation by its headers and the line shows it; the confirmation number confirms the requested line in one call;
  back to planning clears a request; a cancel lists requested lines among the supplier bookings.
"""

from __future__ import annotations

import datetime as dt
import email
import email.policy
import email.utils
import uuid
from typing import Any

import asyncpg
import pytest

from app.catalogue import service as catalogue
from app.dispatch.worker import build_handler as build_dispatch
from app.errors import Conflict, Forbidden, Invalid
from app.jobs.queue import Job
from app.ledger import lines, requests, suppliers
from app.mail import conversations, ingest, threads
from app.mail.sender import MailSender
from app.mail.state import MailRuntime
from app.mail.store import MemoryIngestStore
from app.permissions import Action
from app.proposals import builder
from app.tenancy import tenant_tx
from app.vault import Vault
from tests.integration.test_builder_api import _as, _block, _create, _pid
from tests.integration.test_builder_api import _setup as _builder_setup
from tests.integration.test_mail_db import DOMAIN, JOB
from tests.integration.test_mail_db import _setup as _mail_setup
from tests.unit.mail_fakes import FakeProvider, raw_mail

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

SUPPLIER_MAIL = "bookings@lantern-air.example.test"


async def _supplier(
    pool: asyncpg.Pool, s: dict[str, Any], who: Any, name: str, category: str, email_address: str | None = None
) -> uuid.UUID:
    async with tenant_tx(pool, who, s["tenant"]) as ctx:
        body = {"name": name, "category": category}
        if email_address:
            body["contact_email"] = email_address
        made = await suppliers.create_supplier(ctx, Vault(s["keys"], ctx), body)
    return uuid.UUID(made["agency_supplier_id"])


# ── the catalogue and "Add from catalogue" ───────────────────────────────────────────────────────


async def test_the_catalogue_and_add_from_catalogue(app_pool: asyncpg.Pool) -> None:
    s = await _builder_setup(app_pool)  # 2 adults + a child of 8, five days from day 1
    agent, assistant = s["who"]["agent"], s["who"]["assistant"]
    air = await _supplier(app_pool, s, agent, "Lantern Air", "flight")
    async with tenant_tx(app_pool, agent, s["tenant"]) as ctx:
        flight = await catalogue.create_service(
            ctx,
            {
                "agency_supplier_id": air,
                "category": "flight",
                "name": "Valley hop flight",
                "unit_net": "90",
                "unit_sell": "120",
                "currency": "USD",
                "qty_basis": "per_person",
                "tiers": [
                    {"min_pax": 1, "max_pax": 2, "unit_net": "95"},
                    {"min_pax": 3, "max_pax": None, "unit_net": "85"},
                ],
            },
        )
        fee = await catalogue.create_service(
            ctx,
            {
                "category": "fee",
                "name": "Booking service fee",
                "unit_sell": "25",
                "currency": "USD",
                "qty_basis": "per_booking",
            },
        )
        nightly = await catalogue.create_service(
            ctx,
            {
                "category": "fee",
                "name": "Town tax",
                "unit_net": "2",
                "unit_sell": "3",
                "currency": "USD",
                "qty_basis": "per_person_night",
            },
        )
        foreign = await catalogue.create_service(
            ctx,
            {
                "category": "transfer",
                "name": "Lake shuttle",
                "unit_sell": "40",
                "currency": "QZX",
                "qty_basis": "per_booking",
            },
        )
        with pytest.raises(Invalid):  # never the AA row
            await catalogue.create_service(
                ctx,
                {
                    "agency_supplier_id": await ctx.conn.fetchval("SELECT crm.ensure_aa_supplier()"),
                    "category": "fee",
                    "name": "AA thing",
                    "unit_sell": "1",
                    "currency": "USD",
                    "qty_basis": "per_booking",
                },
            )
        with pytest.raises(Conflict) as err:
            await catalogue.update_service(
                ctx,
                uuid.UUID(flight["service_id"]),
                {
                    "tiers": [
                        {"min_pax": 1, "max_pax": 5, "unit_net": "1"},
                        {"min_pax": 4, "max_pax": None, "unit_net": "1"},
                    ]
                },
            )
        assert err.value.code == "tier_overlap"
    assert flight["unit_net"] == "90.00" and [t["unit_net"] for t in flight["tiers"]] == ["95.00", "85.00"]
    async with tenant_tx(app_pool, assistant, s["tenant"]) as ctx:
        listed = await catalogue.list_services(ctx, category=None, active=None)
        assert {x["name"] for x in listed["services"]} >= {"Valley hop flight", "Booking service fee"}
        assert all("unit_net" not in x and "tiers" not in x for x in listed["services"])
        with pytest.raises(Forbidden) as denied:
            await catalogue.update_service(ctx, uuid.UUID(fee["service_id"]), {"unit_sell": "0"})
        assert denied.value.code == "role_forbidden"

    v = await _create(app_pool, s)
    block = uuid.UUID(_block(v, 1)["option_block_id"])

    async def add(role: str, service: dict[str, Any], **kw: Any) -> dict[str, Any]:
        nonlocal v
        out = await _as(
            app_pool,
            s,
            role,
            builder.add_catalogue_option,
            _pid(v),
            v["edit_seq"],
            block,
            uuid.UUID(service["service_id"]),
            **kw,
        )
        v = out
        return out

    out = await add("agent", flight)
    summary = out["catalogue"]
    assert (summary["quantity"], summary["travellers"], summary["unit_net"]) == (3, 3, "85.00")  # the 3+ tier
    option = next(
        o for d in out["days"] for b in d["blocks"] for o in b["options"] if o["title"] == "Valley hop flight x 3"
    )
    assert option["sell_amount"] == "360.00" and option["net_amount"] == "255.00"
    assert option["agency_supplier_id"] == str(air)
    out = await add("agent", fee)
    assert out["catalogue"]["quantity"] == 1 and out["catalogue"]["net_amount"] is None
    out = await add("agent", nightly, nights=4)
    assert out["catalogue"]["quantity"] == 12  # 3 travellers x 4 nights
    out = await add("agent", fee, quantity=2)
    assert out["catalogue"]["sell_amount"] == "50.00"
    with pytest.raises(Conflict) as err:
        await add("agent", foreign)
    assert err.value.code == "fx_rate_missing"
    async with tenant_tx(app_pool, agent, s["tenant"]) as ctx:
        await catalogue.update_service(ctx, uuid.UUID(fee["service_id"]), {"active": False})
        await catalogue.update_service(
            ctx, uuid.UUID(nightly["service_id"]), {"valid_to": (s["start"] - dt.timedelta(days=1)).isoformat()}
        )
    for gone, code in ((fee, "service_inactive"), (nightly, "service_not_valid")):
        with pytest.raises(Conflict) as err:
            await add("agent", gone)
        assert err.value.code == code
    # The assistant adds from the catalogue (the owner's prices) and never sees the net.
    out = await add("assistant", flight)
    assert "unit_net" not in out["catalogue"] and "net_amount" not in out["catalogue"]
    # A snapshot: deleting the service keeps the options.
    async with tenant_tx(app_pool, agent, s["tenant"]) as ctx:
        await catalogue.delete_service(ctx, uuid.UUID(flight["service_id"]))
    after = await _as(app_pool, s, "agent", builder.view, _pid(v))
    titles = [o["title"] for d in after["days"] for b in d["blocks"] for o in b["options"]]
    assert titles.count("Valley hop flight x 3") == 2


async def test_tenant_isolation_of_the_catalogue(app_pool: asyncpg.Pool) -> None:
    a = await _builder_setup(app_pool)
    b = await _builder_setup(app_pool)
    async with tenant_tx(app_pool, a["who"]["agent"], a["tenant"]) as ctx:
        made = await catalogue.create_service(
            ctx,
            {
                "category": "visa",
                "name": "Visa letter",
                "unit_sell": "30",
                "currency": "USD",
                "qty_basis": "per_person",
            },
        )
    async with tenant_tx(app_pool, b["who"]["agent"], b["tenant"]) as ctx:
        assert (await catalogue.list_services(ctx, category=None, active=None))["services"] == []
    v = await _create(app_pool, b)
    with pytest.raises(Exception) as err:
        await _as(
            app_pool,
            b,
            "agent",
            builder.add_catalogue_option,
            _pid(v),
            v["edit_seq"],
            uuid.UUID(_block(v, 1)["option_block_id"]),
            uuid.UUID(made["service_id"]),
        )
    assert getattr(err.value, "code", None) == "service_not_found"


# ── supplier requests ────────────────────────────────────────────────────────────────────────────


async def _lines(pool: asyncpg.Pool, d: dict[str, Any], supplier: uuid.UUID, *titles: str) -> list[uuid.UUID]:
    out = []
    async with tenant_tx(pool, d["owner"], d["tenant"]) as ctx:
        for i, title in enumerate(titles):
            made = await lines.create_line(
                ctx,
                d["trip"],
                {
                    "agency_supplier_id": supplier,
                    "title": title,
                    "service_from": (dt.date.today() + dt.timedelta(days=60 + i)).isoformat(),
                },
            )
            out.append(uuid.UUID(made["booking_line_id"]))
    return out


async def test_supplier_requests_from_draft_to_confirmation(app_pool: asyncpg.Pool) -> None:
    d = await _mail_setup(app_pool)  # the owner's own Gmail mailbox, an inquiry trip with its lead client
    air = await _supplier(app_pool, d, d["owner"], "Lantern Air", "flight", SUPPLIER_MAIL)
    boats = await _supplier(app_pool, d, d["owner"], "River Boats", "transport")  # no e-mail
    first, second = await _lines(app_pool, d, air, "Flight to the hills", "Flight back")
    (boat,) = await _lines(app_pool, d, boats, "Boat transfer")

    async with tenant_tx(app_pool, d["owner"], d["tenant"]) as ctx:
        built = await requests.build(ctx, Vault(d["keys"], ctx), d["trip"])
    by_name = {r["supplier_name"]: r for r in built["requests"]}
    assert by_name["River Boats"]["reason"] == "supplier_no_email" and "message_id" not in by_name["River Boats"]
    req = by_name["Lantern Air"]
    assert req["include_names"] is True  # a flight supplier books by name
    assert set(req["line_ids"]) == {str(first), str(second)}
    assert "Flight to the hills" in req["body"] and "Flight back" in req["body"]
    assert req["subject"].startswith("Booking request: ")
    # The thread is the supplier's, never the client's conversation of the trip.
    async with tenant_tx(app_pool, d["owner"], d["tenant"]) as ctx:
        thread = await ctx.conn.fetchrow(
            "SELECT trip_id, traveler_id, agency_supplier_id FROM crm.conversation WHERE conversation_id = $1",
            uuid.UUID(req["conversation_id"]),
        )
        assert (thread["trip_id"], thread["traveler_id"], thread["agency_supplier_id"]) == (d["trip"], None, air)
        client_thread = await threads.conversation_for(ctx, d["trip"], None)
        assert client_thread != uuid.UUID(req["conversation_id"])
        # Building again replaces the member's draft (one request draft per supplier).
        again = await requests.build(ctx, Vault(d["keys"], ctx), d["trip"], line_ids=[first])
        req = next(r for r in again["requests"] if r["supplier_name"] == "Lantern Air")
        assert req["line_ids"] == [str(first)]
        drafts = await ctx.conn.fetchval(
            "SELECT count(*) FROM crm.message WHERE conversation_id = $1 AND status = 'draft_autosaved'",
            uuid.UUID(req["conversation_id"]),
        )
        assert drafts == 1

    async with tenant_tx(app_pool, d["owner"], d["tenant"], action=Action.MESSAGE_SEND) as ctx:
        sent = await requests.send(
            ctx,
            Vault(d["keys"], ctx),
            d["trip"],
            message_id=uuid.UUID(req["message_id"]),
            subject=req["subject"],
            body=req["body"],
            line_ids=[first, second],
        )
    assert sent["requested"] == [str(first), str(second)] and sent["status"] == "queued"
    provider = FakeProvider(external_id=None)
    job = Job("j", "crm_dispatch", {"tenant_id": str(d["tenant"]), "dispatch_id": sent["dispatch_id"]}, 1, 5)
    assert (
        await build_dispatch(MailSender(MailRuntime(keys=d["keys"], providers={"gmail": provider})))(app_pool, job)
    )["status"] == "sent"
    ((raw, _secret, rcpt),) = provider.sent
    assert rcpt == [SUPPLIER_MAIL]
    delivered = email.message_from_bytes(raw, policy=email.policy.default)

    # The supplier answers; the reply threads on the supplier's conversation by its headers.
    store = MemoryIngestStore()
    store.objects["raw/supplier-reply"] = raw_mail(
        to=d["address"],
        sender=f"Lantern Air <{SUPPLIER_MAIL}>",
        subject="Re: booking",
        message_id="<la-reply-1@lantern-air.example.test>",
        in_reply_to=str(delivered["Message-ID"]),
        references=f"{delivered['References']} {delivered['Message-ID']}",
        text="Confirmed, reference LA-4471.",
        extra={"Date": email.utils.format_datetime(dt.datetime.now(dt.UTC) + dt.timedelta(minutes=1))},
    )
    assert await ingest.build_handler(store, None, d["keys"], DOMAIN)(app_pool, JOB) == {"stored_count": 1}
    async with tenant_tx(app_pool, d["owner"], d["tenant"]) as ctx:
        view = await lines.trip_lines(ctx, d["trip"])
        by_id = {x["booking_line_id"]: x for x in view["lines"]}
        assert by_id[str(first)]["status"] == "requested" and by_id[str(first)]["request_ref"] == req["message_id"]
        assert by_id[str(first)]["supplier_thread"]["replied"] is True
        assert by_id[str(boat)]["supplier_thread"] is None
        # A cancel would list the requested lines among the supplier bookings to cancel.
        from app.cancellation import SUPPLIER_LINES_SQL

        listed = {r["booking_line_id"] for r in await ctx.conn.fetch(SUPPLIER_LINES_SQL, d["trip"], 50)}
        assert {first, second} <= listed and boat not in listed
        # The number the supplier answered confirms the line in one call (level off: no money needed).
        await ctx.conn.execute(
            "INSERT INTO crm.tenant_settings (booking_validation_level) VALUES ('off') "
            "ON CONFLICT (tenant_id) DO UPDATE SET booking_validation_level = 'off'"
        )
        done = await lines.confirm(ctx, first, confirmation_no="LA-4471")
        assert (done["status"], done["confirmation_no"]) == ("confirmed", "LA-4471")
        assert done["requested_at"] is not None  # kept as history
        back = await requests.back_to_planning(ctx, second)
        assert back == d["trip"]
        again_line = await lines.line(ctx, second)
        assert (again_line["status"], again_line["requested_at"], again_line["request_ref"]) == ("planning", None, None)
        with pytest.raises(Conflict):
            await requests.back_to_planning(ctx, second)  # only a requested line


async def test_a_supplier_request_needs_lines_of_that_supplier(app_pool: asyncpg.Pool) -> None:
    d = await _mail_setup(app_pool)
    air = await _supplier(app_pool, d, d["owner"], "Lantern Air", "flight", SUPPLIER_MAIL)
    van = await _supplier(app_pool, d, d["owner"], "Hill Vans", "transport", "desk@hill-vans.example.test")
    (flight,) = await _lines(app_pool, d, air, "Flight to the hills")
    (ride,) = await _lines(app_pool, d, van, "Van to the lake")
    async with tenant_tx(app_pool, d["owner"], d["tenant"]) as ctx:
        built = await requests.build(ctx, Vault(d["keys"], ctx), d["trip"], include_names=False)
        assert all(r["include_names"] is False for r in built["requests"])
        req = next(r for r in built["requests"] if r["supplier_name"] == "Lantern Air")
    async with tenant_tx(app_pool, d["owner"], d["tenant"], action=Action.MESSAGE_SEND) as ctx:
        with pytest.raises(Conflict) as err:  # the van line is not Lantern Air's
            await requests.send(
                ctx,
                Vault(d["keys"], ctx),
                d["trip"],
                message_id=uuid.UUID(req["message_id"]),
                subject=req["subject"],
                body=req["body"],
                line_ids=[ride],
            )
        assert err.value.code == "nothing_to_request"
    async with tenant_tx(app_pool, d["owner"], d["tenant"]) as ctx:
        assert (await lines.line(ctx, ride))["status"] == "planning"
        assert (await lines.line(ctx, flight))["status"] == "planning"
        with pytest.raises(Conflict) as err:
            await requests.build(ctx, Vault(d["keys"], ctx), d["trip"], line_ids=[uuid.uuid4()])
        assert err.value.code == "nothing_to_request"


async def test_a_client_message_still_goes_to_the_client(app_pool: asyncpg.Pool) -> None:
    """A supplier thread on the trip does not change where a trip message goes (the client's conversation)."""
    d = await _mail_setup(app_pool)
    air = await _supplier(app_pool, d, d["owner"], "Lantern Air", "flight", SUPPLIER_MAIL)
    await _lines(app_pool, d, air, "Flight to the hills")
    async with tenant_tx(app_pool, d["owner"], d["tenant"]) as ctx:
        await requests.build(ctx, Vault(d["keys"], ctx), d["trip"])
    async with tenant_tx(app_pool, d["owner"], d["tenant"], action=Action.MESSAGE_SEND) as ctx:
        sent = await conversations.send(
            ctx,
            Vault(d["keys"], ctx),
            uuid.uuid4(),
            conversation_id=None,
            trip_id=d["trip"],
            subject="Your plan",
            body="Here is the plan.",
        )
        conv = await ctx.conn.fetchrow(
            "SELECT traveler_id, agency_supplier_id FROM crm.conversation WHERE conversation_id = $1",
            uuid.UUID(sent["conversation_id"]),
        )
    assert conv["traveler_id"] == d["lead"] and conv["agency_supplier_id"] is None
    provider = FakeProvider(external_id=None)
    job = Job("j", "crm_dispatch", {"tenant_id": str(d["tenant"]), "dispatch_id": sent["dispatch_id"]}, 1, 5)
    await build_dispatch(MailSender(MailRuntime(keys=d["keys"], providers={"gmail": provider})))(app_pool, job)
    assert provider.sent[0][2] == ["hoa.nguyen@example.test"]
