"""AA-952 CRM-23 slice 3 end to end on PostgreSQL 15 with the RDS role shape: payments against invoices, refunds and
the agency's own payment methods, as crm_app through ``tenant_tx()``.

- A deposit invoice paid in two parts: the first leaves a balance, more than the balance is 422; the second pays it,
  the invoice is paid, its schedule row is paid and the trip moves Proposal -> Booked (deposit_paid) with the portal
  invites; the schedule's own "Mark paid" is refused while an issued invoice bills the row; a paid invoice is not void.
- Refunds: never more than is left to ask back; an agent approves up to the agency threshold (above it: 403, the
  owner approves); executed once approved; the refund entry joins crm.payment; a fully refunded payment's commitment
  reads refunded and the trip still cannot be deleted (has_money).
- Payment methods: the manual instructions print on the invoice; another provider is 409 provider_not_available.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import asyncpg
import pytest

from app.errors import Conflict, Forbidden, Invalid
from app.invoices import service as invoices
from app.payments import service as payments
from app.proposals import service as proposals
from app.tenancy import tenant_tx
from app.vault import Vault
from tests.integration.test_invoices_api import _accepted, _as
from tests.integration.test_lifecycle_api import _schedule

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _pay(pool: asyncpg.Pool, s: dict[str, Any], invoice_id: uuid.UUID, amount: str, role: str = "agent") -> dict:
    async with tenant_tx(pool, s["who"][role], s["tenant"]) as ctx:
        return await payments.record_payment(ctx, Vault(s["keys"], ctx), invoice_id, amount=Decimal(amount),
                                             method="bank_transfer", received_on=None, reference="TT 0042")  # fmt: skip


async def _commitment(pool: asyncpg.Pool, s: dict[str, Any], ref: str) -> str | None:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(s["tenant"]))
        return await conn.fetchval("SELECT line_status FROM crm.trip_commitment WHERE source_ref = $1", ref)


async def test_partial_payments_settle_the_deposit_and_refunds_follow_the_matrix(app_pool: asyncpg.Pool) -> None:
    s = await _accepted(app_pool)
    await _as(app_pool, s, "owner", lambda ctx: payments.save_manual(
        ctx, display_name="Bank transfer", instructions="Account 0000 1111, ref: invoice number"))  # fmt: skip
    draft = await _as(app_pool, s, "agent", lambda ctx: invoices.create_from_schedule(ctx, s["trip"], s["deposit"]))
    deposit = uuid.UUID(draft["invoice_id"])
    with pytest.raises(Conflict) as err:  # a draft takes no payment
        await _pay(app_pool, s, deposit, "100")
    assert err.value.code == "invoice_not_payable"
    issued = await _as(app_pool, s, "agent", lambda ctx: invoices.issue(ctx, deposit))
    assert issued["actions"] == ["record_payment", "void"]

    first = await _pay(app_pool, s, deposit, "100")
    assert first["invoice"]["status"] == "open" and first["invoice"]["balance_amount"] == "200.00"
    assert first["invoice"]["actions"] == ["record_payment"] and first["trip_moved"] is None
    with pytest.raises(Invalid) as err:
        await _pay(app_pool, s, deposit, "250")
    assert err.value.code == "amount_above_balance"
    with pytest.raises(Conflict) as err:  # the row's own "Mark paid" waits for the invoice
        async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
            await proposals.mark_paid(ctx, Vault(s["keys"], ctx), s["deposit"], received_on=None, method="cash")
    assert err.value.code == "invoice_issued_for_row"
    with pytest.raises(Conflict) as err:
        await _as(app_pool, s, "agent", lambda ctx: invoices.void(ctx, deposit, "wrong"))
    assert err.value.code == "invoice_has_payments"

    second = await _pay(app_pool, s, deposit, "200")
    assert second["invoice"]["status"] == "paid" and second["invoice"]["balance_amount"] == "0.00"
    assert second["trip_moved"]["stage"] == "booked" and second["portal"] is not None
    view = await _schedule(app_pool, s)
    assert view["stage"] == "booked" and view["deposit_paid"]
    assert next(r for r in view["schedule"] if r["kind"] == "deposit")["status"] == "paid"
    assert await _commitment(app_pool, s, f"invoice:{deposit}") == "paid"
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        doc = await invoices.document(ctx, Vault(s["keys"], ctx), deposit)
    assert "account 0000 1111" not in doc["html"]  # paid: no pay-by instructions
    assert "Paid " in doc["html"] and "USD 0.00" in doc["html"]

    # Refunds: what is left to ask back, the agency threshold (set low), approval, execution.
    paid_ids = [p["payment_id"] for p in second["invoice"]["payments"]]
    big, small = uuid.UUID(paid_ids[1]), uuid.UUID(paid_ids[0])  # 200 and 100
    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(s["tenant"]))
        await conn.execute(
            "INSERT INTO crm.tenant_settings (refund_owner_threshold_base) VALUES (150) "
            "ON CONFLICT (tenant_id) DO UPDATE SET refund_owner_threshold_base = 150"
        )
    with pytest.raises(Invalid) as err:
        await _as(app_pool, s, "agent", lambda ctx: payments.request_refund(ctx, small, amount=Decimal("120"),
                                                                           reason="Hotel night"))  # fmt: skip
    assert err.value.code == "amount_above_refundable"
    asked = await _as(app_pool, s, "agent", lambda ctx: payments.request_refund(ctx, big, amount=Decimal("200"),
                                                                               reason="Trip shortened"))  # fmt: skip
    refund_id = uuid.UUID(asked["refund_id"])
    assert asked["status"] == "proposed" and asked["requested_by"].startswith("member:")
    with pytest.raises(Conflict) as err:
        await _as(app_pool, s, "agent", lambda ctx: payments.execute_refund(ctx, refund_id, method="bank_transfer",
                                                                           refunded_on=None))  # fmt: skip
    assert err.value.code == "refund_not_approved"
    with pytest.raises(Forbidden) as err:  # 200 USD is above the agent's 150 threshold
        await _as(app_pool, s, "agent", lambda ctx: payments.decide_refund(ctx, refund_id, approve=True, note=None))
    assert err.value.code == "above_threshold"
    approved = await _as(app_pool, s, "owner", lambda ctx: payments.decide_refund(ctx, refund_id, approve=True,
                                                                                 note="OK"))  # fmt: skip
    assert approved["status"] == "approved"
    with pytest.raises(Conflict):
        await _as(app_pool, s, "owner", lambda ctx: payments.decide_refund(ctx, refund_id, approve=False, note=None))
    done = await _as(app_pool, s, "agent", lambda ctx: payments.execute_refund(
        ctx, refund_id, method="bank_transfer", refunded_on=None, reference="RF 7"))  # fmt: skip
    assert done["status"] == "executed" and done["executed_at"]
    assert await _commitment(app_pool, s, f"payment:{big}") == "refunded"
    small_ask = await _as(app_pool, s, "agent", lambda ctx: payments.request_refund(ctx, small, amount=Decimal("40"),
                                                                                   reason="Goodwill"))  # fmt: skip
    rejected = await _as(app_pool, s, "agent", lambda ctx: payments.decide_refund(
        ctx, uuid.UUID(small_ask["refund_id"]), approve=False, note="Not due"))  # fmt: skip
    assert rejected["status"] == "rejected"

    final = await _as(app_pool, s, "assistant", lambda ctx: invoices.get(ctx, deposit))
    assert (final["status"], final["paid_amount"], final["refunded_amount"]) == ("paid", "300.00", "200.00")
    assert [p["entry_kind"] for p in final["payments"]] == ["payment", "payment", "refund"]
    assert {r["status"] for r in final["refund_requests"]} == {"executed", "rejected"}
    listed = await _as(app_pool, s, "assistant", lambda ctx: payments.list_refunds(ctx, "executed"))
    assert [r["invoice_number"] for r in listed["refund_requests"]] == [final["number"]]
    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(s["tenant"]))
        assert "has_money" in await conn.fetchval("SELECT crm.trip_delete_blockers($1)", s["trip"])


async def test_payment_methods_and_the_printed_instructions(app_pool: asyncpg.Pool) -> None:
    s = await _accepted(app_pool)
    out = await _as(app_pool, s, "owner", lambda ctx: payments.save_manual(
        ctx, display_name="Bank transfer", instructions="Account 0000 2222"))  # fmt: skip
    assert [a["provider"] for a in out["accounts"]] == ["manual"]
    assert {p["provider"]: p["available"] for p in out["providers"]} == {"manual": True, "stripe": False,
                                                                         "vn_gateway": False}  # fmt: skip
    again = await _as(app_pool, s, "owner", lambda ctx: payments.save_manual(
        ctx, display_name="Bank transfer (VCB)", instructions="Account 0000 3333"))  # fmt: skip
    assert len(again["accounts"]) == 1 and again["accounts"][0]["instructions"] == "Account 0000 3333"
    with pytest.raises(Conflict) as err:
        await _as(app_pool, s, "owner", lambda ctx: payments.connect(ctx, "stripe"))
    assert err.value.code == "provider_not_available"
    draft = await _as(app_pool, s, "agent", lambda ctx: invoices.create_from_schedule(ctx, s["trip"], s["balance"]))
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        doc = await invoices.document(ctx, Vault(s["keys"], ctx), uuid.UUID(draft["invoice_id"]))
    assert "Bank transfer (VCB)" in doc["html"] and "Account 0000 3333" in doc["html"]
    gone = await _as(app_pool, s, "owner", lambda ctx: payments.disconnect(ctx, uuid.UUID(again["accounts"][0]
                                                                                       ["account_id"])))  # fmt: skip
    assert gone["accounts"][0]["status"] == "disconnected"
