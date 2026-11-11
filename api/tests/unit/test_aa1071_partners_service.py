"""AA-1071 :mod:`app.partners.service` without a database: the term and contact checks, what each role's list
carries, link issue and revoke, the trip's partner, and the commission moves (the SQL runs on PostgreSQL in
tests/integration/test_aa1071_partners_api.py and test_crm056_schema.py)."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

import pytest

from app.errors import Conflict, Invalid, NotFound
from app.partners import service
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import FakeVault, enc, tenant_ctx

PARTNER = uuid.UUID("00000000-0000-4000-8000-0000000071b1")
TRIP = uuid.UUID("00000000-0000-4000-8000-0000000071b2")
PC = uuid.UUID("00000000-0000-4000-8000-0000000071b3")
LINK = uuid.UUID("00000000-0000-4000-8000-0000000071b4")
FORM = uuid.UUID("00000000-0000-4000-8000-0000000071b5")


def flat(sql: str) -> str:
    return " ".join(sql.split())


def _partner_row(**over: Any) -> dict[str, Any]:
    row = {
        "sales_partner_id": PARTNER, "kind": "sub_agent", "name_enc": enc("sales_partner", "name_enc", "Blue Kite"),
        "contact_email_enc": enc("sales_partner", "contact_email_enc", "desk@bluekite.example"),
        "contact_phone_enc": None, "commission_type": "percent", "rate": Decimal("10"), "currency": None,
        "basis": "revenue", "payout_due_days": 30, "status": "active",
    }  # fmt: skip
    return {**row, **over}


def _commission(**over: Any) -> dict[str, Any]:
    row = {
        "partner_commission_id": PC, "trip_id": TRIP, "sales_partner_id": PARTNER, "commission_type": "percent",
        "rate": Decimal("10"), "basis": "revenue", "basis_amount": Decimal("1000"), "pax": None,
        "amount": Decimal("100"), "currency": "USD", "base_currency": "USD", "amount_base": Decimal("100"),
        "computed_at": dt.datetime(2026, 11, 1, tzinfo=dt.UTC), "due_on": dt.date(2020, 1, 1), "approved_at": None,
        "paid_on": None, "payout_ref": None, "cancel_reason": None, "status": "accrued", "created_at": None,
        "start_date": dt.date(2027, 1, 5), "end_date": None, "stage": "booked",
    }  # fmt: skip
    return {**row, **over}


# ── terms and contact ─────────────────────────────────────────────────────────────────────────────


def test_clean_terms_defaults_and_merges() -> None:
    assert service.clean_terms({}) == {
        "commission_type": "percent", "rate": Decimal(0), "currency": None, "basis": "revenue", "payout_due_days": 30,
    }  # fmt: skip
    per_pax = service.clean_terms({"commission_type": "fixed_per_pax", "rate": "35.5", "currency": "EUR"})
    assert (per_pax["rate"], per_pax["currency"]) == (Decimal("35.5"), "EUR")
    back = service.clean_terms({"commission_type": "net_rate"}, per_pax)  # a percent again drops the currency
    assert (back["commission_type"], back["currency"]) == ("net_rate", None)


@pytest.mark.parametrize(
    "body",
    [
        {"commission_type": "share"},
        {"basis": "profit"},
        {"rate": "abc"},
        {"rate": "-1"},
        {"rate": "1.234"},
        {"rate": "101"},
        {"commission_type": "fixed_per_pax", "rate": "10"},
        {"commission_type": "fixed_per_pax", "rate": "10", "currency": "usd"},
        {"commission_type": "fixed_per_pax", "rate": "99999999", "currency": "USD"},
        {"payout_due_days": 400},
        {"payout_due_days": True},
    ],
)
def test_clean_terms_refuses(body: dict[str, Any]) -> None:
    with pytest.raises(Invalid):
        service.clean_terms(body)


@pytest.mark.parametrize(
    ("field", "value"),
    [("contact_email", "no-at-sign"), ("contact_email", "a@b"), ("contact_phone", "call me"), ("contact_email", 5)],
)
def test_contact_checks(field: str, value: Any) -> None:
    with pytest.raises(Invalid):
        service._contact({field: value}, field)


def test_text_and_ref() -> None:
    assert service._text("  ", "name", max_len=5) is None
    with pytest.raises(Invalid):
        service._text(None, "name", max_len=5, required=True)
    with pytest.raises(Invalid):
        service._text("a\u0007b", "name", max_len=5)
    assert service.trip_ref(TRIP) == "T-" + TRIP.hex[:8].upper()
    assert service._csv_cell("=SUM(A1)") == "'=SUM(A1)" and service._csv_cell(None) == ""
    assert service.money(None) is None and service.money(Decimal("2")) == "2.00"


# ── partners ──────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_by_role() -> None:
    conn = ScriptedConn()
    conn.on("FROM crm.sales_partner p", [{**_partner_row(), "has_email": True, "has_phone": False, "trip_count": 2,
                                          "created_at": None}])  # fmt: skip
    conn.on("FROM crm.sales_partner_link", [{"link_id": LINK, "sales_partner_id": PARTNER, "purpose": "intake",
                                            "intake_form_id": FORM, "expires_at": dt.datetime(2027, 1, 1),
                                            "last_used_at": None, "use_count": 3, "created_at": None}])  # fmt: skip
    conn.on("tenant_settings", "EUR")
    conn.on("FROM crm.partner_commission c", [{"sales_partner_id": PARTNER, "status": "accrued",
                                               "amount_base": Decimal("12.5"), "n": 1}])  # fmt: skip
    owner = await service.list_partners(tenant_ctx(conn, role="owner"), FakeVault())  # type: ignore[arg-type]
    [item] = owner["items"]
    assert item["name"] == "Blue Kite" and item["rate"] == "10.00" and item["accrued_base"] == "12.50"
    assert item["approved_base"] == "0.00" and item["links"]["intake"]["use_count"] == 3
    assert owner["base_currency"] == "EUR" and owner["terms_visible"]
    assistant = await service.list_partners(tenant_ctx(conn, role="assistant"), FakeVault())  # type: ignore[arg-type]
    assert set(assistant["items"][0]) == {"sales_partner_id", "kind", "name", "status", "has_email", "has_phone",
                                          "trip_count"}  # fmt: skip
    agent = await service.list_partners(tenant_ctx(conn, role="agent"), FakeVault())  # type: ignore[arg-type]
    assert "rate" in agent["items"][0] and "links" not in agent["items"][0]


@pytest.mark.asyncio
async def test_create_update_and_delete() -> None:
    conn = ScriptedConn().on("FROM crm.sales_partner WHERE sales_partner_id", _partner_row())
    ctx = tenant_ctx(conn)
    made = await service.create_partner(
        ctx, FakeVault(),  # type: ignore[arg-type]
        {"kind": "referrer", "name": " Blue Kite ", "contact_email": "desk@bluekite.example",
         "contact_phone": "+84 90 000 0000", "rate": "5"},
    )  # fmt: skip
    assert made["contact_email"] == "desk@bluekite.example" and conn.sql("INSERT INTO crm.tenant_settings")
    args = conn.sql("INSERT INTO crm.sales_partner")[0]
    assert args[1] == "referrer" and args[2] == enc("sales_partner", "name_enc", "Blue Kite")
    with pytest.raises(Invalid):
        await service.create_partner(ctx, FakeVault(), {"kind": "friend", "name": "X"})  # type: ignore[arg-type]
    await service.update_partner(
        ctx, FakeVault(), PARTNER,  # type: ignore[arg-type]
        {"kind": "ota", "name": "Blue Kite Online", "contact_phone": None, "rate": "12", "status": "inactive"},
    )  # fmt: skip
    [update] = [a for f, a in conn.calls if f.startswith("UPDATE crm.sales_partner SET")]
    sql = next(f for f, _ in conn.calls if f.startswith("UPDATE crm.sales_partner SET"))
    assert "kind = $2" in sql and "status =" in sql and update[0] == PARTNER
    for bad in ({"kind": "x"}, {"status": "gone"}):
        with pytest.raises(Invalid):
            await service.update_partner(ctx, FakeVault(), PARTNER, bad)  # type: ignore[arg-type]
    conn.on("EXISTS (SELECT 1 FROM crm.trip", True)
    with pytest.raises(Conflict):
        await service.delete_partner(ctx, PARTNER)
    # A commission row keeps the partner too (a paid row stays after the trip moved to another partner).
    assert "FROM crm.partner_commission WHERE sales_partner_id = $1" in service.IN_USE_SQL
    conn.on("EXISTS (SELECT 1 FROM crm.trip", False)
    assert (await service.delete_partner(ctx, PARTNER))["deleted"]
    conn.on("FROM crm.sales_partner WHERE sales_partner_id", None)
    with pytest.raises(NotFound):
        await service.get_partner(ctx, FakeVault(), PARTNER)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_links() -> None:
    conn = ScriptedConn().on("FROM crm.sales_partner WHERE sales_partner_id", _partner_row())
    conn.on("RETURNING expires_at", dt.datetime(2027, 5, 1, tzinfo=dt.UTC))
    ctx = tenant_ctx(conn)
    with pytest.raises(Invalid):
        await service.issue_link(ctx, PARTNER, "intake", intake_form_id=None, days=None)
    conn.on("FROM crm.intake_form", 1)
    made = await service.issue_link(ctx, PARTNER, "intake", intake_form_id=FORM, days=60)
    assert made["path"].startswith("/c/r/") and len(made["path"]) == 5 + 43
    assert conn.sql("SET status = 'revoked', revoked_at = now() WHERE sales_partner_id")  # the old one goes
    view = await service.issue_link(ctx, PARTNER, "view", intake_form_id=FORM, days=None)
    assert view["path"].startswith("/c/a/") and conn.sql("INSERT INTO crm.sales_partner_link")[-1][3] is None
    for purpose, days in (("both", None), ("view", 0), ("view", 401)):
        with pytest.raises(Invalid):
            await service.issue_link(ctx, PARTNER, purpose, intake_form_id=None, days=days)
    conn.on("FROM crm.sales_partner WHERE sales_partner_id", _partner_row(contact_email_enc=None))
    with pytest.raises(Conflict):
        await service.issue_link(ctx, PARTNER, "view", intake_form_id=None, days=None)
    conn.on("FROM crm.sales_partner WHERE sales_partner_id", _partner_row(status="inactive"))
    with pytest.raises(Conflict):
        await service.issue_link(ctx, PARTNER, "view", intake_form_id=None, days=None)
    with pytest.raises(NotFound):
        await service.revoke_link(ctx, PARTNER, LINK)
    conn.on("RETURNING link_id", LINK)
    assert (await service.revoke_link(ctx, PARTNER, LINK))["status"] == "revoked"


@pytest.mark.asyncio
async def test_trip_partner(monkeypatch: pytest.MonkeyPatch) -> None:
    async def open_trip(ctx: Any, trip_id: Any) -> None:
        return None

    monkeypatch.setattr(service.cancellation, "ensure_not_cancelled", open_trip)
    conn = ScriptedConn()
    ctx = tenant_ctx(conn, role="assistant")
    with pytest.raises(NotFound):
        await service.set_trip_partner(ctx, TRIP, PARTNER)
    conn.on("FROM crm.trip WHERE trip_id", {"trip_id": TRIP, "sales_partner_id": None})
    with pytest.raises(Invalid):  # not an active partner
        await service.set_trip_partner(ctx, TRIP, PARTNER)
    conn.on("status = 'active' AND deleted_at IS NULL", 1)
    assert (await service.set_trip_partner(ctx, TRIP, PARTNER))["sales_partner_id"] == str(PARTNER)
    assert conn.sql("UPDATE crm.trip SET sales_partner_id") == [(TRIP, PARTNER)]
    conn.on("FROM crm.trip WHERE trip_id", {"trip_id": TRIP, "sales_partner_id": PARTNER})
    assert (await service.set_trip_partner(ctx, TRIP, None))["sales_partner_id"] is None


# ── commissions ───────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_and_statement() -> None:
    rows = [_commission(), _commission(partner_commission_id=LINK, status="paid", amount_base=None, base_currency=None,
                                      paid_on=dt.date(2026, 11, 2), payout_ref="+CASH")]  # fmt: skip
    conn = ScriptedConn().on("JOIN crm.trip t ON t.trip_id = c.trip_id", rows).on("tenant_settings", "USD")
    ctx = tenant_ctx(conn, role="agent")
    listed = await service.list_commissions(ctx, status="all", partner_id=None, trip_id=None)
    assert listed["totals"]["accrued"] == "100.00" and listed["unconverted"] == 1
    assert listed["items"][0]["overdue"] and listed["items"][1]["overdue"] is False
    assert conn.sql("JOIN crm.trip t")[0][0] is None  # all
    await service.list_commissions(ctx, status=None, partner_id=PARTNER, trip_id=None)
    assert conn.sql("JOIN crm.trip t")[-1][0] == ["accrued", "approved"]
    with pytest.raises(Invalid):
        await service.list_commissions(ctx, status="lost", partner_id=None, trip_id=None)
    conn.on("FROM crm.sales_partner WHERE sales_partner_id", _partner_row())
    name, body = await service.statement_csv(ctx, PARTNER, status=None)
    assert name.startswith("partner-statement-") and "'+CASH" in body and body.count("\r\n") == 3


@pytest.mark.asyncio
async def test_commission_moves() -> None:
    conn = ScriptedConn().on("WHERE c.partner_commission_id", _commission())
    conn.on("crm.partner_commission_sync", '{"created": 0, "refreshed": 1, "cancelled": 0}')
    ctx = tenant_ctx(conn)
    with pytest.raises(NotFound):
        await service.recalculate(ctx, TRIP)
    conn.on("FROM crm.trip WHERE trip_id", {"trip_id": TRIP})
    assert (await service.recalculate(ctx, TRIP))["result"] == {"created": 0, "refreshed": 1, "cancelled": 0}
    await service.approve(ctx, PC)
    assert conn.sql("SET status = 'approved'")[0][1] == f"member:{ctx.member_id}"
    for fn in (service.unapprove,):
        with pytest.raises(Conflict):
            await fn(ctx, PC)
    with pytest.raises(Conflict):
        await service.pay(ctx, PC, paid_on=None, reference=None)
    await service.cancel(ctx, PC)
    assert conn.sql("SET status = 'cancelled'")[0][1] == "by_owner"
    conn.on("WHERE c.partner_commission_id", _commission(status="approved"))
    with pytest.raises(Conflict):
        await service.approve(ctx, PC)
    await service.unapprove(ctx, PC)
    with pytest.raises(Invalid):
        await service.pay(ctx, PC, paid_on=dt.date.today() + dt.timedelta(days=2), reference=None)
    with pytest.raises(Invalid):
        await service.pay(ctx, PC, paid_on=None, reference="bad\u0000ref")
    await service.pay(ctx, PC, paid_on=dt.date(2026, 11, 1), reference="  ")
    assert conn.sql("SET status = 'paid'")[-1][1:] == (dt.date(2026, 11, 1), None)
    conn.on("WHERE c.partner_commission_id", _commission(status="paid"))
    with pytest.raises(Conflict):
        await service.cancel(ctx, PC)
    conn.on("WHERE c.partner_commission_id", None)
    with pytest.raises(NotFound):
        await service.cancel(ctx, PC)


@pytest.mark.asyncio
async def test_approve_after_the_sync_cancelled_it() -> None:
    answers = iter([_commission(), _commission(status="cancelled", cancel_reason="unbooked")])
    conn = ScriptedConn().on("WHERE c.partner_commission_id", lambda *_: next(answers))
    with pytest.raises(Conflict) as err:
        await service.approve(tenant_ctx(conn), PC)
    assert err.value.code == "commission_changed"
