"""AA-1147 (crm-083) end to end on PostgreSQL with the RDS role shape: the agency's own services follow the trip.

- Accepting a builder version with 1 flight (folded into the tour price) and 1 visa writes 2 planning agency lines,
  through the client link, with their type, supplier (none = handled by the agency), item, bundled flag and money;
  a second accept call writes nothing.
- The builder refuses to fold a visa into the tour price (422 bundle_not_allowed).
- A line without a supplier is created on the Lines tab ("Handled by the agency"); a line nearing its deadline opens
  one task in the hourly tick (lead time from the setting), never twice for one deadline.
- A file can be attached to one line of its trip (an e-ticket), never to a line of another trip.
- The deposit invoice lists the agency's services with their price (the flight folded in reads no price) and the
  printable page carries the group and the disclaimer.
- Nothing of an agency line is ever sent to Adventure Asia Booking: the Booking sync, the hold and the confirm of an
  accepted version with AA and agency items carry only the AA lines.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import Counter
from decimal import Decimal
from typing import Any

import asyncpg
import pytest

from app.booking.sync import BookingSync
from app.documents import service as documents
from app.documents.store import MemoryDocumentStore
from app.errors import Invalid
from app.invoices import service as invoices
from app.ledger import agency_due, lines
from app.proposals import builder, service
from app.proposals import public as page
from app.tenancy import system_tx, tenant_tx
from app.vault import Vault
from tests.aa_fakes import FakeBooking, booking_client
from tests.integration.test_builder_api import _as, _create, _pid, _setup
from tests.integration.test_client_proposal_api import PLAIN, _accept, _raw, _sent
from tests.integration.test_proposal_confirm_api import _pay_deposit

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

PDF = b"%PDF-1.7\n1 0 obj << >> endobj\ntrailer << >>\n%%EOF\n"


async def _other_block(pool: asyncpg.Pool, s: dict[str, Any], v: dict[str, Any], day_no: int, title: str) -> Any:
    day = next(d for d in v["days"] if d["day_no"] == day_no)
    v = await _as(pool, s, "agent", builder.add_block, _pid(v), v["edit_seq"], uuid.UUID(day["proposal_day_id"]),
                  {"title": title, "type": "other"})  # fmt: skip
    block = next(b for d in v["days"] if d["day_no"] == day_no for b in d["blocks"] if b["title"] == title)
    return v, uuid.UUID(block["option_block_id"])


async def _flight_and_visa(pool: asyncpg.Pool) -> dict[str, Any]:
    """A sent version whose only priced items are the agency's: a return flight folded into the tour price (day 1)
    and an e-visa (day 2)."""
    s = await _setup(pool)
    v = await _create(pool, s)
    v, flights = await _other_block(pool, s, v, 1, "Flights")
    v = await _as(pool, s, "agent", builder.add_agency_option, _pid(v), v["edit_seq"], flights,
                  {"title": "Return flight Riverton - Lantern Bay", "category": "flight", "bundled": True,
                   "sell_amount": Decimal("270"), "net_amount": Decimal("240")})  # fmt: skip
    v, visas = await _other_block(pool, s, v, 2, "Visa")
    v = await _as(pool, s, "agent", builder.add_agency_option, _pid(v), v["edit_seq"], visas,
                  {"title": "E-visa (2 travellers)", "category": "visa", "sell_amount": Decimal("50")})  # fmt: skip
    v = await _as(pool, s, "agent", builder.update_settings, _pid(v), v["edit_seq"],
                  {"balance_due_on": s["start"] - dt.timedelta(days=30)})  # fmt: skip
    v = await _as(pool, s, "agent", builder.publish, _pid(v), v["edit_seq"], acknowledge_selection_seq=None)
    async with tenant_tx(pool, s["who"]["agent"], s["tenant"]) as ctx:
        sent = await service.send(ctx, Vault(s["keys"], ctx), _pid(v))
    s.update(v=v, token=sent["link_path"].rsplit("/", 1)[1])
    return s


async def _agency_lines(pool: asyncpg.Pool, s: dict[str, Any]) -> list[dict[str, Any]]:
    rows = await _raw(pool, s, "SELECT booking_line_id, status, category, agency_supplier_id, option_id, bundled, "
                      "service_from, title FROM crm.booking_line WHERE trip_id = $1 AND supplier_kind = 'agency' "
                      "ORDER BY category", s["trip"])  # fmt: skip
    return [dict(r) for r in rows]


async def test_accepting_one_flight_and_one_visa_writes_two_agency_lines(app_pool: asyncpg.Pool) -> None:
    s = await _flight_and_visa(app_pool)
    p = await page.read(app_pool, s["keys"], s["token"])
    # The client page shows the group: the visa priced, the flight folded into the tour price, the disclaimer.
    services = p["agency_services"]
    assert {(i["category"], i["amount"], i["bundled"]) for i in services["items"]} == {
        ("flight", None, True), ("visa", "50.00", False),
    }  # fmt: skip
    assert services["disclaimer"]["en"].startswith("These services are arranged and provided by the agency")
    assert (await _accept(app_pool, s, p))["status"] == "accepted"

    made = await _agency_lines(app_pool, s)
    assert [(m["category"], m["status"], m["bundled"]) for m in made] == [
        ("flight", "planning", True), ("visa", "planning", False),
    ]  # fmt: skip
    assert all(m["agency_supplier_id"] is None and m["option_id"] is not None for m in made)
    assert [m["service_from"] for m in made] == [s["start"], s["start"] + dt.timedelta(days=1)]
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"]) as ctx:
        trip = await lines.trip_lines(ctx, s["trip"])
    views = {v["category"]: v for v in trip["lines"]}
    assert views["visa"]["handled_by_agency"] and views["visa"]["money"]["sell"]["amount"] == "50.00"
    assert views["flight"]["money"]["sell"]["amount"] == "270.00"
    assert views["flight"]["money"]["cost"]["amount"] == "240.00"
    # The writer is idempotent: a second call for the same version writes nothing.
    again = await _raw(app_pool, s, "SELECT crm.agency_lines_from_proposal($1, jsonb_build_array(jsonb_build_object("
                       "'option_id', $2::text, 'sell', '50')))", _pid(s["v"]), str(made[1]["option_id"]))  # fmt: skip
    assert again[0][0] == 0 and len(await _agency_lines(app_pool, s)) == 2
    # The writer refuses a sell above the item's own.
    with pytest.raises(asyncpg.PostgresError, match="sell_invalid"):
        await _raw(app_pool, s, "SELECT crm.agency_lines_from_proposal($1, jsonb_build_array(jsonb_build_object("
                   "'option_id', $2::text, 'sell', '9999')))", _pid(s["v"]), str(made[1]["option_id"]))  # fmt: skip
    audit = await _raw(app_pool, s, "SELECT count(*) AS n FROM crm.audit_event WHERE action = "
                       "'agency_lines_from_proposal'")  # fmt: skip
    assert audit[0]["n"] == 1

    # The deposit invoice: the agency's services with their price; the folded-in flight reads no price.
    sched = await _raw(app_pool, s, "SELECT schedule_id FROM crm.payment_schedule WHERE proposal_id = $1 "
                       "AND kind = 'deposit'", _pid(s["v"]))  # fmt: skip
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        draft = await invoices.create_from_schedule(ctx, s["trip"], sched[0]["schedule_id"])
        doc = await invoices.document(ctx, Vault(s["keys"], ctx), uuid.UUID(draft["invoice_id"]))
    own = {ln["description"]: ln["amount"] for ln in draft["lines"] if ln["source"] == "agency_line"}
    assert own == {"Return flight Riverton - Lantern Bay": None, "E-visa (2 travellers)": "50.00"}
    charges = [ln for ln in draft["lines"] if ln["kind"] == "charge"]
    assert sum(Decimal(c["amount"]) for c in charges) == Decimal(draft["total_amount"])  # the services are not summed
    assert "Services provided by" in doc["html"] and "These services are arranged and provided" in doc["html"]
    assert "Included in the trip price" in doc["html"]


async def test_a_visa_is_never_folded_into_the_tour_price(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    v = await _create(app_pool, s)
    v, block = await _other_block(app_pool, s, v, 1, "Visa")
    with pytest.raises(Invalid) as err:
        await _as(app_pool, s, "agent", builder.add_agency_option, _pid(v), v["edit_seq"], block,
                  {"title": "E-visa", "category": "visa", "bundled": True, "sell_amount": Decimal("50")})  # fmt: skip
    assert err.value.code == "bundle_not_allowed"
    v = await _as(app_pool, s, "agent", builder.add_agency_option, _pid(v), v["edit_seq"], block,
                  {"title": "Agency fee", "category": "fee", "sell_amount": Decimal("40")})  # fmt: skip
    option = next(o for d in v["days"] for b in d["blocks"] for o in b["options"] if o["title"] == "Agency fee")
    v = await _as(app_pool, s, "agent", builder.update_option, _pid(v), v["edit_seq"], uuid.UUID(option["option_id"]),
                  {"bundled": True})  # fmt: skip
    option = next(o for d in v["days"] for b in d["blocks"] for o in b["options"] if o["title"] == "Agency fee")
    assert option["bundled"] is True


async def test_a_line_nearing_its_deadline_opens_one_task(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    due = dt.datetime.now(dt.UTC) + dt.timedelta(days=1)
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        visa = await lines.create_line(ctx, s["trip"], {"title": "E-visa", "category": "visa", "due_at": due})
        later = await lines.create_line(ctx, s["trip"], {"title": "Travel insurance", "category": "insurance",
                                                         "due_at": due + dt.timedelta(days=40)})  # fmt: skip
        with pytest.raises(Invalid) as err:
            await lines.create_line(ctx, s["trip"], {"title": "E-visa", "category": "visa", "bundled": True})
    assert err.value.code == "bundle_not_allowed"
    assert visa["handled_by_agency"] and visa["agency_supplier_id"] is None and visa["due_at"] is not None
    async with system_tx(app_pool, s["tenant"], actor="job:crm_workflow_tick") as ctx:
        first = await agency_due.due_tasks(ctx, dt.datetime.now(dt.UTC))
    async with system_tx(app_pool, s["tenant"], actor="job:crm_workflow_tick") as ctx:
        second = await agency_due.due_tasks(ctx, dt.datetime.now(dt.UTC))
    assert first["agency_line_tasks_opened"] == 1 and second["agency_line_tasks_opened"] == 0
    tasks = await _raw(app_pool, s, "SELECT kind, booking_line_id, priority FROM crm.task WHERE trip_id = $1 "
                       "AND booking_line_id IS NOT NULL", s["trip"])  # fmt: skip
    assert [(t["kind"], str(t["booking_line_id"]), t["priority"]) for t in tasks] == [
        ("agency_visa_due", visa["booking_line_id"], "urgent"),
    ]  # fmt: skip
    assert later["booking_line_id"] not in {str(t["booking_line_id"]) for t in tasks}  # 40 days out: not yet


async def test_a_file_belongs_to_one_line_of_its_trip(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    other = await _setup(app_pool)
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        line = await lines.create_line(ctx, s["trip"], {"title": "Return flight", "category": "flight"})
        saved = await documents.upload(
            ctx, Vault(s["keys"], ctx), MemoryDocumentStore(), traveler_id=None, trip_id=s["trip"], kind="ticket",
            filename="e-ticket.pdf", content_type="application/pdf", content=PDF,
            booking_line_id=uuid.UUID(line["booking_line_id"]),
        )  # fmt: skip
        listed = await documents.trip_documents(ctx, Vault(s["keys"], ctx), s["trip"])
        with pytest.raises(Invalid) as err:
            await documents.upload(
                ctx, Vault(s["keys"], ctx), MemoryDocumentStore(), traveler_id=None, trip_id=s["trip"],
                kind="visa_letter", filename="letter.pdf", content_type="application/pdf", content=PDF,
                booking_line_id=uuid.uuid4(),
            )  # fmt: skip
    assert err.value.code == "invalid_booking_line_id"
    assert saved["booking_line_id"] == line["booking_line_id"]
    assert [d["booking_line_id"] for d in listed["documents"]] == [line["booking_line_id"]]
    assert other["trip"] != s["trip"]


class _RecordingBooking(FakeBooking):
    """The in-memory Booking, keeping every request body it received."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.bodies: list[str] = []

    async def __call__(self, method: str, url: str, headers: Any, body: bytes | None, timeout_s: float) -> Any:
        self.bodies.append(f"{method} {url} {(body or b'').decode()}")
        return await super().__call__(method, url, headers, body, timeout_s)


async def test_no_agency_line_is_ever_sent_to_booking(app_pool: asyncpg.Pool) -> None:
    s = await _sent(app_pool, second_aa=True)
    fake = _RecordingBooking(b2b_price="300.00", availability="available")
    fake.confirm_line_status = "confirmed"
    fake.bodies = []
    s["fake"] = fake
    status, out = await page.hold(app_pool, s["keys"], PLAIN, booking_client(fake), s["token"], origin=None)
    assert status == 200 and out["outcome"] == "held"
    assert (await _accept(app_pool, s, out["page"]))["status"] == "accepted"
    await BookingSync(booking_client(fake)).run_agency(app_pool, s["tenant"], Counter(), feed=False)
    await _pay_deposit(app_pool, s)
    await BookingSync(booking_client(fake)).run_agency(app_pool, s["tenant"], Counter(), feed=False)
    agency = await _agency_lines(app_pool, s)
    assert agency, "the accepted version has agency items"
    aa = await _raw(app_pool, s, "SELECT booking_line_id FROM crm.booking_line WHERE trip_id = $1 "
                    "AND supplier_kind = 'aa'", s["trip"])  # fmt: skip
    assert aa, "the AA lines were written and confirmed through Booking"
    sent = "\n".join(fake.bodies)
    assert "/holds" in sent and "/confirm" in sent  # Booking was asked for the hold, its read and the confirm
    for line in agency:
        assert str(line["booking_line_id"]) not in sent and str(line["option_id"]) not in sent
        assert line["title"] not in sent
    requests = await _raw(app_pool, s, "SELECT r.booking_line_id, l.supplier_kind FROM crm.aa_request r "
                          "JOIN crm.booking_line l ON l.booking_line_id = r.booking_line_id")  # fmt: skip
    assert requests and {r["supplier_kind"] for r in requests} == {"aa"}
