"""AA-952 CRM-23 slice 2 end to end on PostgreSQL 15 with the RDS role shape: agency invoices as crm_app through
``tenant_tx()``.

- A deposit invoice from the accepted proposal's schedule row (always tax-inclusive, the agency's tax rate), one live
  invoice per row (409 ``invoice_exists``), issued with the agency's gapless number and sealed (409
  ``invoice_not_draft``); the trip's commitment row follows it.
- A service-fee invoice needs the setting (409 ``service_fee_disabled``); typed lines, tax on top, edited as a draft,
  issued, then voided with a reason (once).
- Receivables list the issued unpaid invoices; the document is escaped HTML with the lead as bill-to.
- The assistant cannot create one (403); another agency sees none (404); two issues at once get two numbers.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from typing import Any

import asyncpg
import pytest

from app.errors import Conflict, Forbidden, NotFound
from app.invoices import service as invoices
from app.permissions import Action
from app.tenancy import tenant_tx
from app.vault import Vault
from tests.integration.test_crm005_intake import _agency
from tests.integration.test_lifecycle_api import _accept, _proposal, _schedule, _setup

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _accepted(pool: asyncpg.Pool) -> dict[str, Any]:
    s = await _setup(pool)
    sent = await _proposal(pool, s)
    await _accept(pool, s, sent)
    rows = (await _schedule(pool, s))["schedule"]
    s["deposit"] = uuid.UUID(next(r["schedule_id"] for r in rows if r["kind"] == "deposit"))
    s["balance"] = uuid.UUID(next(r["schedule_id"] for r in rows if r["kind"] == "balance"))
    return s


async def _as(pool: asyncpg.Pool, s: dict[str, Any], role: str, fn: Any, *, action: Action | None = None) -> Any:
    async with tenant_tx(pool, s["who"][role], s["tenant"], action=action) as ctx:
        return await fn(ctx)


async def test_deposit_and_service_fee_invoices(app_pool: asyncpg.Pool) -> None:
    s = await _accepted(app_pool)
    await _as(app_pool, s, "owner", lambda ctx: invoices.update_settings(
        ctx, {"invoice_tax_rate": Decimal("10"), "invoice_tax_label": "VAT", "invoice_prefix": "SV"}))  # fmt: skip

    draft = await _as(app_pool, s, "agent", lambda ctx: invoices.create_from_schedule(ctx, s["trip"], s["deposit"]))
    assert (draft["status"], draft["kind"], draft["number"], draft["currency"]) == ("draft", "deposit", None, "USD")
    assert (draft["total_amount"], draft["tax_amount"], draft["subtotal_amount"]) == ("300.00", "27.27", "272.73")
    assert draft["tax_inclusive"] and draft["tax_label"] == "VAT" and draft["actions"] == ["edit", "issue", "void"]
    assert [(line["kind"], line["source"], line["amount"]) for line in draft["lines"]] == [
        ("charge", "schedule", "300.00")
    ]
    deposit = uuid.UUID(draft["invoice_id"])
    with pytest.raises(Conflict) as err:
        await _as(app_pool, s, "agent", lambda ctx: invoices.create_from_schedule(ctx, s["trip"], s["deposit"]))
    assert err.value.code == "invoice_exists"
    with pytest.raises(Conflict) as err:  # a deposit invoice bills its schedule row
        await _as(app_pool, s, "agent", lambda ctx: invoices.update_draft(ctx, deposit, {"tax_inclusive": False}))
    assert err.value.code == "schedule_invoice_fixed"
    edited = await _as(app_pool, s, "agent", lambda ctx: invoices.update_draft(ctx, deposit, {"memo": "Thank you."}))
    assert edited["memo"] == "Thank you."

    issued = await _as(app_pool, s, "agent", lambda ctx: invoices.issue(ctx, deposit))
    assert issued["status"] == "open" and issued["number"] == "SV-000001" and issued["issue_date"] is not None
    assert issued["due_on"] >= issued["issue_date"] and issued["actions"] == ["record_payment", "void"]
    with pytest.raises(Conflict) as err:
        await _as(app_pool, s, "agent", lambda ctx: invoices.update_draft(ctx, deposit, {"memo": "later"}))
    assert err.value.code == "invoice_not_draft"
    with pytest.raises(Conflict):
        await _as(app_pool, s, "agent", lambda ctx: invoices.issue(ctx, deposit))
    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(s["tenant"]))
        status = await conn.fetchval(
            "SELECT line_status FROM crm.trip_commitment WHERE kind = 'invoice' AND source_ref = $1",
            f"invoice:{deposit}",
        )
    assert status == "open"

    # A service fee needs the setting; typed lines, tax on top.
    fee = {"kind": "service_fee", "currency": "USD", "lines": [{"description": "Planning fee", "quantity": "1",
                                                               "unit_amount": "50"}]}  # fmt: skip
    with pytest.raises(Conflict) as err:
        await _as(app_pool, s, "agent", lambda ctx: invoices.create_typed(ctx, s["trip"], **fee))
    assert err.value.code == "service_fee_disabled"
    await _as(app_pool, s, "owner", lambda ctx: invoices.update_settings(
        ctx, {"service_fee_enabled": True, "invoice_tax_inclusive": False}))  # fmt: skip
    typed = await _as(app_pool, s, "agent", lambda ctx: invoices.create_typed(ctx, s["trip"], **fee))
    assert (typed["subtotal_amount"], typed["tax_amount"], typed["total_amount"]) == ("50.00", "5.00", "55.00")
    fee_id = uuid.UUID(typed["invoice_id"])
    lines = [{"description": "Planning fee", "quantity": Decimal(2), "unit_amount": Decimal("40")},
             {"description": "Visa letter", "quantity": Decimal(1), "unit_amount": Decimal("15.50")}]  # fmt: skip
    edited = await _as(app_pool, s, "agent", lambda ctx: invoices.update_draft(ctx, fee_id, {"lines": lines}))
    assert (edited["subtotal_amount"], edited["tax_amount"], edited["total_amount"]) == ("95.50", "9.55", "105.05")
    assert [line["position"] for line in edited["lines"]] == [1, 2]
    second = await _as(app_pool, s, "agent", lambda ctx: invoices.issue(ctx, fee_id))
    assert second["number"] == "SV-000002"

    listed = await _as(app_pool, s, "assistant", lambda ctx: invoices.receivables(ctx, None))
    assert {r["number"] for r in listed["invoices"]} == {"SV-000001", "SV-000002"}
    voided = await _as(app_pool, s, "agent", lambda ctx: invoices.void(ctx, fee_id, "Raised by mistake"))
    assert voided["status"] == "void" and voided["void_reason"] == "Raised by mistake" and voided["number"]
    with pytest.raises(Conflict) as err:
        await _as(app_pool, s, "agent", lambda ctx: invoices.void(ctx, fee_id, "again"))
    assert err.value.code == "invoice_not_voidable"
    listed = await _as(app_pool, s, "agent", lambda ctx: invoices.receivables(ctx, None))
    assert [r["number"] for r in listed["invoices"]] == ["SV-000001"] and listed["invoices"][0]["overdue"] is False
    trip_list = await _as(app_pool, s, "assistant", lambda ctx: invoices.list_for_trip(ctx, s["trip"]))
    assert len(trip_list["invoices"]) == 2
    # S261 F2: both lists name the agency timezone the claim note's deadline is printed in.
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        stored = await ctx.conn.fetchval("SELECT timezone FROM crm.tenant_settings")
    assert trip_list["timezone"] == listed["timezone"] == (stored or invoices.DEFAULT_TZ)

    async with tenant_tx(app_pool, s["who"]["assistant"], s["tenant"]) as ctx:
        doc = await invoices.document(ctx, Vault(s["keys"], ctx), deposit)
    html = doc["html"]
    assert doc["filename"] == "SV-000001.html" and "SV-000001" in html and "Grace Lee" in html
    assert "Content-Security-Policy" in html and "<script" not in html and "VAT 10% (included)" in html


async def test_who_and_which_agency(app_pool: asyncpg.Pool) -> None:
    s = await _accepted(app_pool)
    with pytest.raises(Forbidden):
        await _as(app_pool, s, "assistant", lambda ctx: invoices.create_from_schedule(ctx, s["trip"], s["deposit"]),
                  action=Action.PAYMENT_MARK_RECEIVED)  # fmt: skip
    draft = await _as(app_pool, s, "agent", lambda ctx: invoices.create_from_schedule(ctx, s["trip"], s["balance"]))
    other_tenant, people = await _agency(app_pool, "owner")
    async with tenant_tx(app_pool, people[0][0], other_tenant) as ctx:
        with pytest.raises(NotFound):
            await invoices.get(ctx, uuid.UUID(draft["invoice_id"]))
        assert (await invoices.receivables(ctx, "draft"))["invoices"] == []


async def test_two_issues_at_once_get_two_numbers(app_pool: asyncpg.Pool) -> None:
    s = await _accepted(app_pool)
    drafts = [
        await _as(app_pool, s, "agent", lambda ctx, k=k: invoices.create_from_schedule(ctx, s["trip"], s[k]))
        for k in ("deposit", "balance")
    ]
    issued = await asyncio.gather(
        *(_as(app_pool, s, "agent", lambda ctx, d=d: invoices.issue(ctx, uuid.UUID(d["invoice_id"]))) for d in drafts)
    )
    assert sorted(i["number"] for i in issued) == ["INV-000001", "INV-000002"]
