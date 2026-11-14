"""AA-1067 end to end on PostgreSQL with the RDS role shape, as crm_app through ``tenant_tx()`` / the client link with
real field encryption: the payment schedule with instalments, the traveller payment reminders, the traveller
cancellation policy and the cancellation charge.

- **Instalments** (item crm-08): a new builder version starts with the AA default lines (30 % now, 40 % at T-60, the
  rest at T-30); the agent edits them; Publish refuses lines without dates or in the wrong order; the price hash
  covers them and the published version keeps them (CRM31); the accept writes one row per line adding up to the
  accepted total; the live status shows partial (a part paid on its invoice) and overdue (due before today).
- **Reminders** (item crm-09): the library's "payment due soon" automation installs paused with a starter message;
  switched on, the hourly tick starts one run per open row 7 days before its due date, never twice, never for a row
  paid in full, and the 3-day cool-down holds a second rule back; the run's merge fields name the open amount.
- **Cancellation policy** (item crm-11): the owner starts from the AA table and publishes it; an agent may not edit
  it; a text proposal sent afterwards is pinned to it, the client page shows it and the booking terms merge it; a new
  version leaves the sent proposal on the old one. The cancel dialog computes days, tier, charge, paid and refund
  due; an agent waiving more than the refund threshold is refused; the owner may.
- **Charge and refund** (item crm-12): the run proposes the refunds prefilled with paid - charge, records the charge,
  and bills the rest of a charge above the money kept on a ``cancellation_fee`` invoice.
- Another agency sees none of it.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal
from typing import Any

import asyncpg
import pytest

from app import stages
from app.cancel_policy import service as policy
from app.errors import Conflict, Forbidden, Invalid
from app.invoices import service as invoices
from app.payments import schedule_settings
from app.payments import service as payments
from app.permissions import Action
from app.proposals import agreements, builder, service
from app.proposals import public as page
from app.tenancy import system_tx, tenant_tx
from app.vault import Vault
from app.workflows import engine, tick
from app.workflows import service as workflows
from tests.integration.conftest import SET_AGENCY_TZ, agency_today
from tests.integration.test_builder_api import _as, _pid, _publishable
from tests.integration.test_builder_api import _setup as _builder_setup
from tests.integration.test_lifecycle_api import PLAIN, _token
from tests.integration.test_lifecycle_api import _setup as _lc_setup

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

D = Decimal


async def _rows(pool: asyncpg.Pool, s: dict[str, Any], sql: str, *args: Any) -> list[Any]:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(s["tenant"]))
        await conn.execute("SELECT set_config('app.actor', 'seed:test', true)")
        await conn.execute(SET_AGENCY_TZ)  # current_date = the CRM's agency-local today
        return list(await conn.fetch(sql, *args))


async def _owner(pool: asyncpg.Pool, s: dict[str, Any], fn: Any, *args: Any, **kw: Any) -> Any:
    async with tenant_tx(pool, s["who"]["owner"], s["tenant"], action=Action.PAYMENT_SETTINGS) as ctx:
        return await fn(ctx, *args, **kw)


async def _accept_page(pool: asyncpg.Pool, s: dict[str, Any], token: str, **extra: Any) -> dict[str, Any]:
    p = await page.read(pool, s["keys"], token)
    body = {"full_name": "Mira Tanaka", "accept_terms": True, "price_hash": p["price_hash"],
            "terms_doc_id": p["terms"]["doc_id"] if p.get("terms") else None, **extra}  # fmt: skip
    if p["kind"] == "builder":
        body.update(selection_hash=p["selection_hash"], rooms_confirmed=True)
    return await page.accept(pool, s["keys"], PLAIN, token, body, origin=None)


# ── crm-08: instalments ──────────────────────────────────────────────────────────────────────────


async def test_a_builder_version_with_instalments_from_draft_to_live_status(app_pool: asyncpg.Pool) -> None:
    s = await _builder_setup(app_pool)
    v = await _publishable(app_pool, s)  # total 1000, day 1 = start
    start = s["start"]
    lines = v["schedule"]
    assert [(x["kind"], x["percent"], x["days_before"]) for x in lines] == [
        ("deposit", "30", None), ("instalment", "40", 60), ("balance", None, None),  # the balance date was typed
    ]  # fmt: skip
    assert [x["amount"] for x in v["totals"]["schedule"]] == ["300.00", "400.00", "300.00"]
    # The agent sets four lines; dates out of order block Publish, a fix publishes.
    bad = [{"kind": "deposit", "percent": D("25")},
           {"kind": "instalment", "percent": D("25"), "due_on": start - dt.timedelta(days=30)},
           {"kind": "instalment", "percent": D("25"), "due_on": start - dt.timedelta(days=60)},
           {"kind": "balance", "due_on": start - dt.timedelta(days=14)}]  # fmt: skip
    v = await _as(app_pool, s, "agent", builder.update_settings, _pid(v), v["edit_seq"], {"schedule": bad})
    assert "schedule_dates_order" in v["checks"]["blocking"] and v["deposit_pct"] == "25.00"
    with pytest.raises(Invalid) as err:
        await _as(app_pool, s, "agent", builder.publish, _pid(v), v["edit_seq"], acknowledge_selection_seq=None)
    assert err.value.code == "not_publishable"
    good = [bad[0], bad[2], bad[1], bad[3]]
    v = await _as(app_pool, s, "agent", builder.update_settings, _pid(v), v["edit_seq"], {"schedule": good})
    assert v["checks"]["publishable"] and [x["seq"] for x in v["schedule"]] == [1, 1, 2, 1]
    assert v["balance_due_on"] == (start - dt.timedelta(days=14)).isoformat()
    with pytest.raises(Invalid):  # the percents leave nothing for the balance
        await _as(app_pool, s, "agent", builder.update_settings, _pid(v), v["edit_seq"],
                  {"schedule": [{"kind": "deposit", "percent": D("60")},
                                {"kind": "instalment", "percent": D("40"), "due_on": start},
                                {"kind": "balance", "due_on": start}]})  # fmt: skip
    # The assistant prepares drafts without money fields: the schedule is one.
    async with tenant_tx(app_pool, s["who"]["assistant"], s["tenant"]) as ctx:
        with pytest.raises(Forbidden):
            from app.permissions import require

            require(ctx, Action.MONEY_FIELDS_EDIT, object_kind="proposal")
    out = await _as(app_pool, s, "agent", builder.publish, _pid(v), v["edit_seq"], acknowledge_selection_seq=None)
    assert out["status"] == "published"
    with pytest.raises(asyncpg.PostgresError) as sealed:
        await _rows(app_pool, s, "UPDATE crm.proposal SET schedule_plan = NULL WHERE proposal_id = $1", _pid(v))
    assert sealed.value.sqlstate == "CRM31"
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        sent = await service.send(ctx, Vault(s["keys"], ctx), _pid(v))
    token = _token(sent["link_path"])
    client = await page.read(app_pool, s["keys"], token)
    assert [(x["kind"], x["seq"], x["amount"]) for x in client["totals"]["schedule"]] == [
        ("deposit", 1, "250.00"), ("instalment", 1, "250.00"), ("instalment", 2, "250.00"), ("balance", 1, "250.00"),
    ]  # fmt: skip
    assert (await _accept_page(app_pool, s, token))["status"] == "accepted"
    rows = await _rows(app_pool, s, "SELECT kind, seq, percent, amount, due_on FROM crm.payment_schedule "
                       "WHERE proposal_id = $1 ORDER BY due_on, kind, seq", _pid(v))  # fmt: skip
    assert [(r["kind"], r["seq"], r["amount"]) for r in rows] == [
        ("deposit", 1, D("250.00")), ("instalment", 1, D("250.00")), ("instalment", 2, D("250.00")),
        ("balance", 1, D("250.00")),
    ]  # fmt: skip
    assert sum(r["amount"] for r in rows) == D("1000.00") and rows[1]["percent"] == D("25.00")
    # Live status: half of the first instalment paid on its invoice = partial; the second made overdue = overdue.
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        overview = await service.trip_overview(ctx, Vault(s["keys"], ctx), s["trip"])
        first = next(r for r in overview["schedule"] if r["kind"] == "instalment" and r["seq"] == 1)
        second = next(r for r in overview["schedule"] if r["kind"] == "instalment" and r["seq"] == 2)
        bill = await invoices.create_from_schedule(ctx, s["trip"], uuid.UUID(first["schedule_id"]))
        assert bill["lines"][0]["description"].startswith("Instalment 1 (proposal version")
        await invoices.issue(ctx, uuid.UUID(bill["invoice_id"]))
        await payments.record_payment(ctx, Vault(s["keys"], ctx), uuid.UUID(bill["invoice_id"]), amount=D("100"),
                                      method="bank_transfer", received_on=None)  # fmt: skip
        await ctx.conn.execute(SET_AGENCY_TZ)
        await ctx.conn.execute("UPDATE crm.payment_schedule SET due_on = current_date - 4 WHERE schedule_id = $1",
                               uuid.UUID(second["schedule_id"]))  # fmt: skip
        overview = await service.trip_overview(ctx, Vault(s["keys"], ctx), s["trip"])
    # The client's pay box names the instalment invoice by its number (its schedule row's seq).
    client = await page.read(app_pool, s["keys"], token)
    billed = [(x["kind"], x["seq"]) for x in client["payments"] if x["kind"] == "instalment"]
    assert billed == [("instalment", 1)]
    live = {(r["kind"], r["seq"]): r for r in overview["schedule"]}
    assert (live[("instalment", 1)]["live_status"], live[("instalment", 1)]["paid_amount"],
            live[("instalment", 1)]["open_amount"]) == ("partial", "100.00", "150.00")  # fmt: skip
    assert (live[("instalment", 2)]["live_status"], live[("instalment", 2)]["days_overdue"]) == ("overdue", 4)
    assert live[("deposit", 1)]["live_status"] == "due" and live[("instalment", 1)]["status"] == "due"


async def test_the_schedule_template_and_a_full_payment_version(app_pool: asyncpg.Pool) -> None:
    s = await _builder_setup(app_pool)
    with pytest.raises(Forbidden):
        async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"], action=Action.PAYMENT_SETTINGS):
            pass
    template = [{"kind": "deposit", "percent": "50", "due": {"anchor": "accept"}},
                {"kind": "balance", "due": {"anchor": "before_start", "days": 45}}]  # fmt: skip
    # A deposit under 1 % is refused (422), never saved: crm.proposal.deposit_pct mirrors it (proposal_kind_chk 1-100).
    tiny = [{**template[0], "percent": "0.5"}, template[1]]
    with pytest.raises(Invalid) as err:
        await _owner(app_pool, s, schedule_settings.update, tiny)
    assert err.value.code == "invalid_schedule" and err.value.extra["field"] == "schedule.0.percent"
    saved = await _owner(app_pool, s, schedule_settings.update, template)
    assert saved["template"] == template and not saved["is_default"]
    v = await _publishable(app_pool, s)
    assert [(x["kind"], x["percent"]) for x in v["schedule"]] == [("deposit", "50"), ("balance", None)]
    with pytest.raises(Invalid) as err:  # the same in the builder: a 422, not a CheckViolation
        await _as(app_pool, s, "agent", builder.update_settings, _pid(v), v["edit_seq"],
                  {"schedule": [{"kind": "deposit", "percent": D("0.5")},
                                {"kind": "balance", "due_on": s["start"] - dt.timedelta(days=30)}]})  # fmt: skip
    assert err.value.code == "invalid_schedule"
    v = await _as(app_pool, s, "agent", builder.update_settings, _pid(v), v["edit_seq"], {"deposit_pct": D("100")})
    assert [x["kind"] for x in v["schedule"]] == ["deposit"] and v["balance_due_on"] is None
    assert v["checks"]["publishable"] and v["totals"]["deposit"] == v["totals"]["sell_total"] == "1000.00"
    v = await _as(app_pool, s, "agent", builder.update_settings, _pid(v), v["edit_seq"], {"schedule_reset": True})
    assert [x["kind"] for x in v["schedule"]] == ["deposit", "balance"]
    assert (await _owner(app_pool, s, schedule_settings.update, None))["is_default"]


# ── crm-09: reminders ────────────────────────────────────────────────────────────────────────────


async def _accepted_text(pool: asyncpg.Pool, s: dict[str, Any], *, terms: uuid.UUID | None = None) -> dict[str, Any]:
    async with tenant_tx(pool, s["who"]["agent"], s["tenant"]) as ctx:
        draft = await service.create_draft(ctx, Vault(s["keys"], ctx), s["trip"], body="Day 1 harbour. Day 2 hills.",
                                           currency="USD", total=D("1000"), deposit=D("300"),
                                           balance_due_on=agency_today() + dt.timedelta(days=7),
                                           terms_doc_id=terms or s["terms"])  # fmt: skip
        sent = await service.send(ctx, Vault(s["keys"], ctx), uuid.UUID(draft["proposal_id"]))
    sent["token"] = _token(sent["link_path"])
    sent["page"] = await page.read(pool, s["keys"], sent["token"])
    await _accept_page(pool, s, sent["token"])
    return sent


async def _tick(pool: asyncpg.Pool, s: dict[str, Any]) -> Any:
    async with system_tx(pool, s["tenant"], actor=tick.ACTOR) as ctx:
        return await tick.run_agency(ctx, Vault(s["keys"], ctx), dt.datetime.now(dt.UTC))


async def test_payment_reminders_are_opt_in_once_per_row_and_skip_paid_rows(app_pool: asyncpg.Pool) -> None:
    s = await _lc_setup(app_pool)
    sent = await _accepted_text(app_pool, s)
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"], action=Action.WORKFLOW_EDIT) as ctx:
        rule = await workflows.install(ctx, "payment_due_soon", vault=Vault(s["keys"], ctx), starter_mail=True)
        overdue = await workflows.install(ctx, "payment_overdue", vault=Vault(s["keys"], ctx), starter_mail=True)
    assert rule["status"] == "paused" and rule["steps"][0]["message_template_id"]  # opt-in, message ready
    templates_made = await _rows(app_pool, s, "SELECT title, source FROM crm.message_template ORDER BY title")
    assert [(r["title"], r["source"]) for r in templates_made] == [
        ("Payment overdue: {{trip_name}}", "agency"),
        ("Payment reminder: {{trip_name}}", "agency"),
    ]
    # Paused: nothing starts.
    assert (await _tick(app_pool, s))["schedule_runs_started"] == 0
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"], action=Action.WORKFLOW_EDIT) as ctx:
        await workflows.set_status(ctx, uuid.UUID(rule["workflow_rule_id"]), "active")
        await workflows.set_status(ctx, uuid.UUID(overdue["workflow_rule_id"]), "active")
    # The balance is due in 7 days: one run for it; ticking again starts nothing more.
    assert (await _tick(app_pool, s))["schedule_runs_started"] == 1
    assert (await _tick(app_pool, s))["schedule_runs_started"] == 0
    runs = await _rows(app_pool, s, "SELECT trigger_key, trip_id FROM crm.workflow_run")
    balance = (await _rows(app_pool, s, "SELECT schedule_id FROM crm.payment_schedule WHERE kind = 'balance'"))[0]
    assert len(runs) == 1 and runs[0]["trigger_key"].startswith(f"schedule:{balance['schedule_id']}:schedule_due_soon")
    # The run's merge fields name the open amount and the due date.
    async with system_tx(app_pool, s["tenant"], actor=engine.ACTOR) as ctx:
        row = await engine.schedule_row(ctx, runs[0])
        values = await engine.merge_values(ctx, Vault(s["keys"], ctx), engine.Subject(None, None, None), "en", row)
    assert values["payment_amount"] == "700.00 USD"
    # The deposit, made overdue, is in the cool-down? No: a different row. Paid in full it starts nothing.
    await _rows(app_pool, s, "UPDATE crm.payment_schedule SET due_on = current_date - 3 WHERE kind = 'deposit'")
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        deposit = (await _rows(app_pool, s, "SELECT schedule_id FROM crm.payment_schedule WHERE kind = 'deposit'"))[0]
        await service.mark_paid(ctx, Vault(s["keys"], ctx), deposit["schedule_id"], received_on=None, method="cash")
    assert (await _tick(app_pool, s))["schedule_runs_started"] == 0
    # The balance overdue 3 days later would start the overdue rule, but the cool-down (3 days per row) holds it.
    await _rows(app_pool, s, "UPDATE crm.payment_schedule SET due_on = current_date - 3 WHERE kind = 'balance'")
    assert (await _tick(app_pool, s))["schedule_runs_started"] == 0
    await _rows(app_pool, s, "UPDATE crm.workflow_run SET created_at = now() - interval '4 days'")
    assert (await _tick(app_pool, s))["schedule_runs_started"] == 1
    assert sent["page"]["schedule"] == []  # the page read before the accept: no rows yet


# ── crm-11 / crm-12: the cancellation policy, the charge and the refunds ─────────────────────────


async def _policy(pool: asyncpg.Pool, s: dict[str, Any]) -> dict[str, Any]:
    draft = await _owner(pool, s, policy.create, {"aa_default": True})
    return await _owner(pool, s, policy.publish, uuid.UUID(draft["policy_id"]), make_default=True)


async def _paid_deposit(pool: asyncpg.Pool, s: dict[str, Any], amount: str = "300") -> None:
    """The deposit billed on its invoice and paid (so the refund goes through a refund request), Booked."""
    async with tenant_tx(pool, s["who"]["agent"], s["tenant"]) as ctx:
        rows = (await service.trip_overview(ctx, Vault(s["keys"], ctx), s["trip"]))["schedule"]
        deposit = next(r for r in rows if r["kind"] == "deposit")
        bill = await invoices.create_from_schedule(ctx, s["trip"], uuid.UUID(deposit["schedule_id"]))
        await invoices.issue(ctx, uuid.UUID(bill["invoice_id"]))
        await payments.record_payment(ctx, Vault(s["keys"], ctx), uuid.UUID(bill["invoice_id"]), amount=D(amount),
                                      method="bank_transfer", received_on=None)  # fmt: skip


async def _cancel(pool: asyncpg.Pool, s: dict[str, Any], role: str, *, dry_run: bool, **kw: Any) -> dict[str, Any]:
    async with tenant_tx(pool, s["who"][role], s["tenant"], action=Action.TRIP_CANCEL) as ctx:
        out = await stages.cancel(ctx, s["trip"], reason="client_request", dry_run=dry_run, **kw)
    json.dumps(out, default=str)  # the route serialises it: nothing internal rides along
    return out


async def test_the_policy_is_owner_set_pinned_shown_and_merged(app_pool: asyncpg.Pool) -> None:
    s = await _lc_setup(app_pool)
    with pytest.raises(Forbidden):
        async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"], action=Action.PAYMENT_SETTINGS):
            pass
    live = await _policy(app_pool, s)
    assert live["status"] == "published" and live["is_default"] and live["version"] == 1
    assert [t["charge_pct"] for t in live["tiers"]] == ["0", "25", "50", "100"]
    with pytest.raises(Conflict):
        await _owner(app_pool, s, policy.update_draft, uuid.UUID(live["policy_id"]), {"name": "Changed"})
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"]) as ctx:
        terms = await agreements.create(ctx, kind="booking_terms", version_label="v2026-11",
                                        body_text="Our terms.\n\n{{cancellation.policy}}")  # fmt: skip
        await agreements.publish(ctx, uuid.UUID(terms["doc_id"]))
    sent = await _accepted_text(app_pool, s, terms=uuid.UUID(terms["doc_id"]))
    p = sent["page"]
    assert p["cancellation_policy"]["name"] == "Standard cancellation policy"
    assert "- 31 days or more before departure: 0 % of the trip price" in p["terms"]["body_text"]
    pinned = await _rows(app_pool, s, "SELECT cancellation_policy_id FROM crm.proposal WHERE status = 'accepted'")
    assert str(pinned[0]["cancellation_policy_id"]) == live["policy_id"]
    # A new version: the old one retires, the accepted proposal keeps it.
    draft = await _owner(app_pool, s, policy.create, {"from_policy_id": live["policy_id"]})
    draft = await _owner(app_pool, s, policy.update_draft, uuid.UUID(draft["policy_id"]),
                         {"tiers": [{"days_before_min": 0, "charge_pct": "40"}]})  # fmt: skip
    v2 = await _owner(app_pool, s, policy.publish, uuid.UUID(draft["policy_id"]), make_default=False)
    assert v2["version"] == 2 and v2["is_default"]
    listing = await _owner(app_pool, s, policy.listing)
    assert {(x["version"], x["status"]) for x in listing["policies"]} == {(1, "retired"), (2, "published")}
    await _paid_deposit(app_pool, s)  # Booked: the trip may be cancelled
    preview = await _cancel(app_pool, s, "agent", dry_run=True)
    assert preview["charge"]["policy"]["version"] == 1 and preview["charge"]["policy"]["frozen"]
    # Another agency sees none of it.
    other = await _lc_setup(app_pool)
    assert (await _owner(app_pool, other, policy.listing))["policies"] == []
    async with tenant_tx(app_pool, other["who"]["owner"], other["tenant"]) as ctx:
        assert await policy.charge_of(ctx, s["trip"]) is None
        assert await ctx.conn.fetchval("SELECT count(*) FROM crm.v_schedule_status") == 0


async def test_cancel_charges_by_the_policy_prefills_refunds_and_records_the_charge(app_pool: asyncpg.Pool) -> None:
    s = await _lc_setup(app_pool)  # the trip starts in 90 days: the 0 % tier
    await _policy(app_pool, s)
    await _accepted_text(app_pool, s)
    await _paid_deposit(app_pool, s)
    early = (await _cancel(app_pool, s, "agent", dry_run=True))["charge"]
    assert (early["days_before"], early["tier"]["charge_pct"], early["charge_amount"], early["refund_due"]) == (
        90, "0", "0.00", "300.00")  # fmt: skip
    # Twenty days before departure: the 25 % tier.
    await _rows(app_pool, s, "UPDATE crm.trip SET start_date = current_date + 20")
    preview = await _cancel(app_pool, s, "agent", dry_run=True)
    c = preview["charge"]
    got = (c["days_before"], c["tier"]["charge_pct"], c["policy_charge"], c["charge_amount"])
    assert got == (20, "25", "250.00", "250.00")
    assert (c["paid_amount"], c["kept_amount"], c["refund_due"], c["invoice_amount"]) == (
        "300.00", "250.00", "50.00", "0.00")  # fmt: skip
    (pay,) = preview["payments"]
    assert pay["refund_amount"] == "50.00" and pay["refundable"] == "300.00"  # prefilled with paid - charge
    # An agent waiving more than the refund threshold (lowered to 50 here) is refused; the owner may.
    await _rows(app_pool, s, "UPDATE crm.tenant_settings SET refund_owner_threshold_base = 50")
    waive = {"mode": "waive", "reason": "Family emergency"}
    agent_view = await _cancel(app_pool, s, "agent", dry_run=True, charge=waive)
    assert agent_view["charge"]["needs_owner"] and agent_view["payments"][0]["refund_amount"] == "300.00"
    with pytest.raises(Forbidden) as err:
        await _cancel(app_pool, s, "agent", dry_run=False, charge=waive)
    assert err.value.code == "cancel_charge_owner_only"
    limit = {"limit": "50.00", "limit_currency": err.value.extra["limit_currency"]}
    assert agent_view["charge"]["owner_limit"] == limit and err.value.extra["limit"] == "50.00"
    out = await _cancel(app_pool, s, "owner", dry_run=False,
                        charge={"mode": "override", "amount": "60.00", "reason": "Rebooked for spring"})  # fmt: skip
    assert out["stage"] == "cancelled" and out["charge"]["decision"] == "override"
    refunds = await _rows(app_pool, s, "SELECT amount, status FROM crm.refund_request")
    assert [(r["amount"], r["status"]) for r in refunds] == [(D("240.00"), "proposed")]
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        charge = await policy.charge_of(ctx, s["trip"])
    assert charge is not None and (charge["decision"], charge["charge_amount"], charge["kept_amount"],
                                   charge["refund_due"], charge["invoice_id"]) == ("override", "60.00", "60.00",
                                                                                   "240.00", None)  # fmt: skip
    assert charge["reason"] == "Rebooked for spring" and charge["policy_charge"] == "250.00"


async def test_a_charge_above_the_money_kept_is_billed_on_a_cancellation_fee_invoice(app_pool: asyncpg.Pool) -> None:
    s = await _lc_setup(app_pool)
    await _policy(app_pool, s)
    await _accepted_text(app_pool, s)
    await _paid_deposit(app_pool, s)
    # Ten days before departure: the 50 % tier (500), 300 paid and kept: 200 billed, nothing refunded.
    await _rows(app_pool, s, "UPDATE crm.trip SET start_date = current_date + 10")
    out = await _cancel(app_pool, s, "agent", dry_run=False)
    c = out["charge"]
    got = (c["tier"]["charge_pct"], c["charge_amount"], c["refund_due"], c["invoice_amount"])
    assert got == ("50", "500.00", "0.00", "200.00")
    assert {e["kind"]: e["count"] for e in out["effects"]}["cancellation_fee_invoiced"] == 1
    assert await _rows(app_pool, s, "SELECT 1 FROM crm.refund_request") == []
    fee = await _rows(app_pool, s, "SELECT invoice_id, status, total_amount, number, schedule_id FROM crm.invoice "
                      "WHERE kind = 'cancellation_fee'")  # fmt: skip
    assert len(fee) == 1 and (fee[0]["status"], fee[0]["total_amount"], fee[0]["schedule_id"]) == ("open", D("200.00"),
                                                                                                 None)  # fmt: skip
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        full = await invoices.full(ctx, fee[0]["invoice_id"])
        charge = await policy.charge_of(ctx, s["trip"])
    assert full["lines"][0]["description"].startswith("Cancellation charge (50 % under Standard cancellation policy v1")
    assert charge is not None and charge["invoice_id"] == str(fee[0]["invoice_id"]) and charge["invoiced_amount"] == (
        "200.00")  # fmt: skip
    # The client pays the fee invoice on the cancelled trip.
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        paid = await payments.record_payment(ctx, Vault(s["keys"], ctx), fee[0]["invoice_id"], amount=D("200"),
                                             method="bank_transfer", received_on=None)  # fmt: skip
    assert paid["invoice"]["status"] == "paid"
