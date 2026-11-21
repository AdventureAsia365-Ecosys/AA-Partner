"""AA-1147 (crm-083): the agency's own services from the accepted proposal to the invoice, without a database.

- the accept's writer input (agency items of the choice, their share of the total, the day's date; later blocks out);
- the deadline task (kind by item type, the lead time from the setting, one per line and deadline);
- the client documents: the group "Services provided by <agency>" with prices (none when folded into the tour price)
  and the disclaimer, on the invoice HTML and PDF and in the proposal text (which the proposal PDF prints);
- the line view: a line without a supplier reads "handled by the agency".
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal
from typing import Any

import pytest

from app.documents import renders
from app.errors import Invalid
from app.invoices import render
from app.ledger import agency_due, agency_lines, lines
from app.proposals import option_fields
from app.proposals import text_render as tr
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import TENANT, system_ctx

DAY1, DAY2 = uuid.UUID(int=0x11), uuid.UUID(int=0x12)
B_FLIGHT, B_VISA, B_INS, B_LATER, B_AA = (uuid.UUID(int=0x20 + i) for i in range(5))
O_FLIGHT, O_VISA, O_INS, O_LATER, O_AA = (uuid.UUID(int=0x30 + i) for i in range(5))
START = dt.date(2031, 3, 1)
DISCLAIMER = "These services are arranged and provided by the agency named above."


def _option(oid: uuid.UUID, block: uuid.UUID, source: str, title: str, sell: str, **kw: Any) -> dict[str, Any]:
    return {"option_id": oid, "option_block_id": block, "source": source, "title": title,
            "sell_amount": Decimal(sell), "pay_on_arrival": False, **kw}  # fmt: skip


def _structure(discount: str | None = None) -> dict[str, Any]:
    return {
        "days": [{"proposal_day_id": DAY1, "day_no": 1}, {"proposal_day_id": DAY2, "day_no": 3}],
        "blocks": [
            {"option_block_id": B_FLIGHT, "proposal_day_id": DAY1},
            {"option_block_id": B_VISA, "proposal_day_id": DAY1},
            {"option_block_id": B_INS, "proposal_day_id": DAY2},
            {"option_block_id": B_LATER, "proposal_day_id": DAY2},
            {"option_block_id": B_AA, "proposal_day_id": DAY2},
        ],
        "options": [
            _option(O_FLIGHT, B_FLIGHT, "agency", "Return flight", "270", category="flight", bundled=True),
            _option(O_VISA, B_VISA, "agency", "E-visa", "50", category="visa", bundled=False),
            _option(O_INS, B_INS, "agency", "Travel insurance", "72", category="insurance", bundled=False),
            _option(O_LATER, B_LATER, "agency", "Agency fee", "40", category="fee", bundled=False),
            _option(O_AA, B_AA, "aa", "Lantern bay cruise", "500"),
        ],
        "discount_pct": Decimal(discount) if discount else None,
    }


def _choice() -> dict[str, str]:
    return {str(b): str(o) for b, o in ((B_FLIGHT, O_FLIGHT), (B_VISA, O_VISA), (B_INS, O_INS), (B_LATER, O_LATER),
                                        (B_AA, O_AA))}  # fmt: skip


# ── the accept writes one agency line per chosen agency item ──────────────────────────────────────────────────────
def test_the_writer_gets_the_agency_items_of_the_choice() -> None:
    items = agency_lines.chosen(_structure(), _choice(), {str(B_LATER)}, START)
    by_id = {i["option_id"]: i for i in items}
    assert set(by_id) == {str(O_FLIGHT), str(O_VISA), str(O_INS)}  # the AA item and the later block are not written
    assert by_id[str(O_VISA)] == {"option_id": str(O_VISA), "sell": "50.00", "service_from": "2031-03-01"}
    assert by_id[str(O_INS)]["service_from"] == "2031-03-03"


def test_the_writer_takes_each_items_share_after_the_discount() -> None:
    items = agency_lines.chosen(_structure(discount="10"), _choice(), set(), None)
    sells = {i["option_id"]: Decimal(i["sell"]) for i in items}
    assert sells[str(O_VISA)] == Decimal("45.00") and sells[str(O_INS)] == Decimal("64.80")
    assert all(i["service_from"] is None for i in items)  # no start date yet


@pytest.mark.asyncio
async def test_one_flight_and_one_visa_make_two_lines_in_one_call() -> None:
    s = _structure()
    s["blocks"], s["options"] = s["blocks"][:2], s["options"][:2]
    conn = ScriptedConn().on("crm.agency_lines_from_proposal", 2)
    ctx = system_ctx(conn)
    proposal = uuid.UUID(int=0x99)
    made = await agency_lines.write_accepted(ctx, proposal, s, {str(B_FLIGHT): str(O_FLIGHT), str(B_VISA): str(O_VISA)},
                                             set(), START)  # fmt: skip
    assert made == 2
    (args,) = conn.sql("crm.agency_lines_from_proposal")
    assert args[0] == proposal and {i["option_id"] for i in json.loads(args[1])} == {str(O_FLIGHT), str(O_VISA)}


@pytest.mark.asyncio
async def test_a_choice_without_agency_items_calls_nothing() -> None:
    conn = ScriptedConn()
    assert await agency_lines.write_accepted(system_ctx(conn), uuid.uuid4(), _structure(), {str(B_AA): str(O_AA)},
                                             set(), START) == 0  # fmt: skip
    assert not conn.calls


# ── a line nearing its deadline opens a task ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "category,kind",
    [("flight", "agency_flight_due"), ("visa", "agency_visa_due"), ("insurance", "agency_insurance_due"),
     ("fee", "agency_line_due"), (None, "agency_line_due")],
)  # fmt: skip
def test_the_task_kind_follows_the_item_type(category: str | None, kind: str) -> None:
    assert agency_due.kind_of(category) == kind


@pytest.mark.asyncio
async def test_due_lines_open_their_task_within_the_lead_time(monkeypatch: pytest.MonkeyPatch) -> None:
    async def lead_days(_conn: Any, tenant: Any) -> int:
        assert tenant == TENANT
        return 5

    monkeypatch.setattr(agency_due.agency_extras, "lead_days", lead_days)
    now = dt.datetime(2031, 3, 1, 9, tzinfo=dt.UTC)
    line_a, line_b, trip = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    due = now + dt.timedelta(days=2)
    conn = ScriptedConn().on("FROM crm.booking_line l", [
        {"booking_line_id": line_a, "trip_id": trip, "category": "flight", "due_at": due},
        {"booking_line_id": line_b, "trip_id": trip, "category": "flight", "due_at": due},
    ])  # fmt: skip
    answers = iter([uuid.uuid4(), None])  # the second flight waits for the first one's task (one open per kind)
    conn.on("INSERT INTO crm.task", lambda *_a: next(answers))
    counts = await agency_due.due_tasks(system_ctx(conn), now)
    assert counts == {"agency_lines_due": 2, "agency_line_tasks_opened": 1}
    (query,) = conn.sql("FROM crm.booking_line l")
    assert query[0] == now + dt.timedelta(days=5) and query[1] == ["planning", "requested"]
    assert set(query[3]) == set(agency_due.KINDS)
    first = conn.sql("INSERT INTO crm.task")[0]
    assert first == (trip, "agency_flight_due", due, line_a)


# ── the client's documents ───────────────────────────────────────────────────────────────────────────────────────
INVOICE = {
    "currency": "USD", "status": "open", "number": "INV-000123", "issue_date": "2031-01-02", "due_on": "2031-01-20",
    "kind": "deposit", "tax_label": "VAT", "tax_rate": "0", "tax_inclusive": True, "tax_amount": "0",
    "subtotal_amount": "300.00", "total_amount": "300.00", "memo": None,
    "lines": [
        {"kind": "charge", "source": "schedule", "description": "Deposit", "quantity": "1", "unit_amount": "300.00",
         "amount": "300.00"},
        {"kind": "included", "source": "aa_line", "description": "Lantern bay cruise", "quantity": None,
         "unit_amount": None, "amount": None},
        {"kind": "included", "source": "agency_line", "description": "Return flight", "quantity": None,
         "unit_amount": None, "amount": None},
        {"kind": "included", "source": "agency_line", "description": "E-visa", "quantity": None, "unit_amount": None,
         "amount": "50.00"},
    ],
}  # fmt: skip


def test_the_invoice_page_shows_the_agency_group_and_the_disclaimer() -> None:
    html = render.render(INVOICE, agency_name="Lotus & Pine Travel", bill_to="Mira", disclaimer=DISCLAIMER)
    assert "This invoice covers:<ul><li>Lantern bay cruise</li></ul>" in html
    assert "Services provided by Lotus &amp; Pine Travel" in html and DISCLAIMER in html
    assert '<td>E-visa</td><td class="num">USD 50.00</td>' in html
    assert '<td>Return flight</td><td class="num">Included in the trip price</td>' in html
    plain = render.render({**INVOICE, "lines": INVOICE["lines"][:2]}, agency_name="A", bill_to=None, disclaimer="X")
    assert "Services provided by" not in plain  # no agency line, no group, no disclaimer


class _Recorder(renders.Document):
    written: list[str] = []

    def text(self, value: str, *args: Any, **kw: Any) -> None:  # type: ignore[override]
        _Recorder.written.append(value)
        super().text(value, *args, **kw)

    def table(self, header: list[str], rows: list[list[str]], *args: Any, **kw: Any) -> None:  # type: ignore[override]
        _Recorder.written.extend(" | ".join(r) for r in rows)
        super().table(header, rows, *args, **kw)


@pytest.mark.parametrize(
    "locale,heading,in_price",
    [("en", "Services provided by Lotus Trails", "Included in the trip price"),
     ("vi", "Dịch vụ do Lotus Trails cung cấp", "Đã gồm trong giá chuyến đi")],
)  # fmt: skip
def test_the_invoice_pdf_shows_the_agency_group_and_the_disclaimer(
    monkeypatch: pytest.MonkeyPatch, locale: str, heading: str, in_price: str
) -> None:
    _Recorder.written = []
    monkeypatch.setattr(renders, "Document", _Recorder)
    data = renders.invoice(INVOICE, locale, agency="Lotus Trails", bill_to="Mira", pay_instructions=[],
                           disclaimer=DISCLAIMER)  # fmt: skip
    assert data.startswith(b"%PDF-")
    written = _Recorder.written
    assert heading in written and DISCLAIMER in written
    assert "E-visa | 50.00 USD" in written and f"Return flight | {in_price}" in written
    assert "• Lantern bay cruise" in written and "• E-visa" not in written


def _snapshot(**kw: Any) -> tr.Snapshot:
    base: dict[str, Any] = {
        "kind": "builder", "version": 2, "price_hash": "ab" * 32, "agency_name": "Lotus Trails",
        "trip_title": "Lantern coast", "currency": "USD", "total": Decimal("892"), "deposit": Decimal("300"),
        "balance_due_on": None,
    }  # fmt: skip
    return tr.Snapshot(**{**base, **kw})


@pytest.mark.parametrize("fmt", ["email", "whatsapp"])
def test_the_proposal_text_and_pdf_body_show_the_agency_group(fmt: str) -> None:
    snap = _snapshot(
        agency_services=(tr.AgencyService("Return flight", None), tr.AgencyService("E-visa", Decimal("50"))),
        agency_disclaimer=DISCLAIMER,
    )
    body = tr.render(snap, fmt, "en").body
    group = body.split("Services provided by Lotus Trails", 1)[1]
    assert "- Return flight: included in the trip price" in group and "- E-visa: 50.00 USD" in group
    assert DISCLAIMER in group
    vi = tr.render(snap, fmt, "vi").body
    assert "Dịch vụ do Lotus Trails cung cấp" in vi and "đã gồm trong giá chuyến đi" in vi
    pdf_bytes = renders.proposal("Lantern coast", body, agency="Lotus Trails", locale="en")
    assert pdf_bytes.startswith(b"%PDF-")
    assert "Services provided by" not in tr.render(_snapshot(), fmt, "en").body


# ── the line view ────────────────────────────────────────────────────────────────────────────────────────────────
def _line_row(**kw: Any) -> dict[str, Any]:
    base = {
        "booking_line_id": uuid.uuid4(), "trip_id": uuid.uuid4(), "supplier_kind": "agency", "agency_supplier_id": None,
        "supplier_name": None, "status": "planning", "aa_sub_status": None, "title": "E-visa", "category": "visa",
        "confirmation_no": None, "service_from": None, "service_to": None, "collect": "agency", "confirm_class": None,
        "visible_to_travelers": False, "confirmed_at": None, "requested_at": None, "request_ref": None,
        "option_id": uuid.UUID(int=7), "due_at": dt.datetime(2031, 2, 1, tzinfo=dt.UTC), "bundled": False,
    }  # fmt: skip
    return {**base, **kw}


def test_a_line_without_a_supplier_is_handled_by_the_agency() -> None:
    view = lines._line_view(_line_row(), None)
    assert view["handled_by_agency"] is True and view["agency_supplier_id"] is None
    assert view["option_id"] == str(uuid.UUID(int=7)) and view["due_at"] == "2031-02-01T00:00:00+00:00"
    own = lines._line_view(_line_row(agency_supplier_id=uuid.UUID(int=5), supplier_name="Gull Air"), None)
    assert own["handled_by_agency"] is False and own["agency_supplier_id"] == str(uuid.UUID(int=5))


def test_the_deadline_field() -> None:
    assert lines._due(None) is None and lines._due("") is None
    assert lines._due("2031-02-01T10:00:00+07:00") == dt.datetime(2031, 2, 1, 3, tzinfo=dt.UTC)
    assert lines._due("2031-02-01").tzinfo is dt.UTC
    with pytest.raises(Invalid) as err:
        lines._due("next week")
    assert err.value.code == "invalid_due_at"


# ── the builder's "fold into the tour price" ─────────────────────────────────────────────────────────────────────
def test_an_edit_keeps_or_sets_the_fold_by_the_rules() -> None:
    fee = {"source": "agency", "category": "fee", "bundled": False}
    assert option_fields.bundled_after({}, {**fee, "bundled": True}) is True  # not sent: kept
    assert option_fields.bundled_after({"bundled": None}, fee) is False
    assert option_fields.bundled_after({"bundled": True}, fee) is True
    for item in ({"source": "agency", "category": "visa"}, {"source": "aa", "category": None}):
        with pytest.raises(Invalid) as err:
            option_fields.bundled_after({"bundled": True}, item)
        assert err.value.code == "bundle_not_allowed"


@pytest.mark.asyncio
async def test_a_new_item_is_folded_in_only_when_asked() -> None:
    conn = ScriptedConn()
    ctx = system_ctx(conn)
    await option_fields.set_bundled(ctx, O_FLIGHT, None, "flight")  # type: ignore[arg-type]
    await option_fields.set_bundled(ctx, O_FLIGHT, True, "flight")  # type: ignore[arg-type]
    assert conn.sql("SET bundled") == [(O_FLIGHT, True)]
    await option_fields.copy_note(ctx, O_VISA, {"display_note": None, "bundled": True})  # type: ignore[arg-type]
    assert conn.sql("SET bundled")[-1] == (O_VISA, True)


def test_the_pdf_splits_the_agency_services_from_what_the_charge_covers() -> None:
    covers, own = renders.split_included(INVOICE["lines"])  # type: ignore[arg-type]
    assert [ln["description"] for ln in covers] == ["Lantern bay cruise"]
    assert [ln["description"] for ln in own] == ["Return flight", "E-visa"]
