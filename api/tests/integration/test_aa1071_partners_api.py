"""AA-1071 (crm-056) on PostgreSQL 15 with the RDS role shape, as crm_app through ``tenant_tx()`` and the partner's
own link context: sales partners, their commission and their pages.

- **Partners**: an owner creates and edits one (contact under the vault DEK); an agent or an assistant cannot (403
  ``role_forbidden``); the assistant's list carries names only, no terms; a partner a trip names is not deleted.
- **Commission**: a booked trip given a partner accrues its commission (the accepted proposal total x rate); the
  owner approves and pays it; the statement CSV names the trip by its short reference only.
- **Intake link**: the form the partner's link opens is the agency's; an inquiry sent through it creates a trip
  with the partner and the lead source ``sales_partner``.
- **View link**: no session -> agency name only; the e-mail code (Dev echo) signs in; the signed-in view lists the
  partner's trips and commissions and no traveller name; a wrong code counts an attempt; sign-out ends the session.
- **Cancel**: cancelling the trip cancels its open partner commission unless the member keeps it.
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from typing import Any

import asyncpg
import pytest

from app.errors import Forbidden, NotFound
from app.jobs.queue import Job
from app.partners import public, service
from app.permissions import Action
from app.settings import Settings
from app.tenancy import tenant_tx
from app.vault import Vault
from tests.integration.test_crm005_intake import _agency, _form, _keys, answers
from tests.integration.test_crm056_schema import _accepted_proposal

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ECHO = Settings(environment="test", db_ssl="disable", portal_otp_dev_echo=True)


async def _seed(pool: asyncpg.Pool, tenant: uuid.UUID, sql: str, *args: Any) -> Any:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
        await conn.execute("SELECT set_config('app.actor', 'seed:test', true)")
        return await conn.fetchval(sql, *args)


async def _setup(pool: asyncpg.Pool) -> dict[str, Any]:
    tenant, people = await _agency(pool, "owner", "agent", "assistant")
    who = {role: people[i][0] for i, role in enumerate(("owner", "agent", "assistant"))}
    keys = _keys()
    async with tenant_tx(pool, who["owner"], tenant, action=Action.SALES_PARTNER_MANAGE) as ctx:
        made = await service.create_partner(
            ctx, Vault(keys, ctx),
            {"kind": "sub_agent", "name": "Kite Hill Tours", "contact_email": "desk@kitehill.example",
             "commission_type": "percent", "rate": "10", "basis": "revenue", "payout_due_days": 15},
        )  # fmt: skip
    return {"tenant": tenant, "who": who, "keys": keys, "partner": uuid.UUID(made["sales_partner_id"])}


async def _booked_trip(pool: asyncpg.Pool, tenant: uuid.UUID) -> uuid.UUID:
    trip = uuid.uuid4()
    await _seed(
        pool, tenant,
        "INSERT INTO crm.trip (trip_id, title, stage, waiting_on, party_adults, start_date, end_date) "
        "VALUES ($1, 'Kim Lien river 5 days', 'booked', 'none', 2, DATE '2027-02-10', DATE '2027-02-14')",
        trip,
    )  # fmt: skip
    return trip


async def _accepted(admin: asyncpg.Connection, tenant: uuid.UUID, trip: uuid.UUID, total: str) -> None:
    await _accepted_proposal(admin, tenant, trip, total)


async def test_only_the_owner_manages_partners_and_the_assistant_sees_names(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    for role in ("agent", "assistant"):
        with pytest.raises(Forbidden):
            async with tenant_tx(app_pool, s["who"][role], s["tenant"], action=Action.SALES_PARTNER_MANAGE):
                pass
    async with tenant_tx(app_pool, s["who"]["assistant"], s["tenant"], action=Action.TRIP_VIEW) as ctx:
        listed = await service.list_partners(ctx, Vault(s["keys"], ctx))
    [item] = listed["items"]
    assert item["name"] == "Kite Hill Tours" and "rate" not in item and "links" not in item
    assert listed["terms_visible"] is False
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"], action=Action.SALES_PARTNER_MANAGE) as ctx:
        edited = await service.update_partner(
            ctx,
            Vault(s["keys"], ctx),
            s["partner"],
            {"commission_type": "fixed_per_pax", "rate": "25", "currency": "EUR"},
        )
        assert (edited["commission_type"], edited["rate"], edited["currency"]) == ("fixed_per_pax", "25.00", "EUR")
        assert edited["contact_email"] == "desk@kitehill.example"
        listed = await service.list_partners(ctx, Vault(s["keys"], ctx))
    assert listed["items"][0]["rate"] == "25.00" and listed["items"][0]["links"] == {}


async def test_a_booked_trip_accrues_then_the_owner_approves_and_pays(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    s = await _setup(app_pool)
    trip = await _booked_trip(app_pool, s["tenant"])
    await _accepted(admin, s["tenant"], trip, "1800")
    async with tenant_tx(app_pool, s["who"]["assistant"], s["tenant"], action=Action.TRIP_EDIT) as ctx:
        assert (await service.set_trip_partner(ctx, trip, s["partner"]))["sales_partner_id"] == str(s["partner"])
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"], action=Action.VIEW_MARGIN) as ctx:
        listed = await service.list_commissions(ctx, status=None, partner_id=s["partner"], trip_id=None)
        with pytest.raises(NotFound):
            await service.delete_partner(ctx, uuid.uuid4())
    [row] = listed["items"]
    assert (row["status"], row["basis_amount"], row["amount"], row["currency"]) == (
        "accrued",
        "1800.00",
        "180.00",
        "USD",
    )
    assert row["due_on"] == "2027-03-01" and listed["totals"]["accrued"] == "180.00"
    pc = uuid.UUID(row["partner_commission_id"])
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"], action=Action.PARTNER_COMMISSION_APPROVE) as ctx:
        approved = await service.approve(ctx, pc)
        assert approved["status"] == "approved"
        paid = await service.pay(ctx, pc, paid_on=None, reference="=BANK 88")
    assert paid["status"] == "paid" and paid["payout_ref"] == "=BANK 88"
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"], action=Action.VIEW_MARGIN) as ctx:
        name, body = await service.statement_csv(ctx, s["partner"], status=None)
        with pytest.raises(Exception) as err:
            await service.delete_partner(ctx, s["partner"])
    assert getattr(err.value, "code", None) == "partner_in_use"
    rows = list(csv.DictReader(io.StringIO(body)))
    assert name.startswith("partner-statement-") and rows[0]["trip_ref"] == "T-" + trip.hex[:8].upper()
    assert rows[0]["payout_ref"] == "'=BANK 88" and "Kim Lien" not in body  # formula guard; never the trip title
    # The trip moves to another partner: the paid row stays, and the first partner is still not deleted (409).
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"], action=Action.SALES_PARTNER_MANAGE) as ctx:
        other = await service.create_partner(
            ctx, Vault(s["keys"], ctx),
            {"kind": "referrer", "name": "Pine Ridge Desk", "commission_type": "percent", "rate": "5"},
        )  # fmt: skip
        await service.set_trip_partner(ctx, trip, uuid.UUID(other["sales_partner_id"]))
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"], action=Action.SALES_PARTNER_MANAGE) as ctx:
        with pytest.raises(Exception) as err:
            await service.delete_partner(ctx, s["partner"])
    assert getattr(err.value, "code", None) == "partner_in_use"
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"], action=Action.VIEW_MARGIN) as ctx:
        listed = await service.list_commissions(ctx, status="all", partner_id=s["partner"], trip_id=trip)
    assert [r["status"] for r in listed["items"]] == ["paid"]


async def test_the_intake_link_stamps_the_partner_on_the_new_trip(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    form = await _form(app_pool, s["tenant"], s["who"]["owner"])
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"], action=Action.SALES_PARTNER_MANAGE) as ctx:
        link = await service.issue_link(ctx, s["partner"], "intake", intake_form_id=form.intake_form_id, days=30)
    token = link["path"].removeprefix("/c/r/")
    assert len(token) == 43
    page = await public.read_form(app_pool, token)
    assert page["agency_name"] == "Sen Vàng Travel" and "honeypot_field" in page
    assert await public.submit(app_pool, s["keys"], ECHO, token, answers(), origin=None)
    from app.intake.process import build_handler

    async with app_pool.acquire() as conn:
        job = await conn.fetchrow(
            "SELECT id::text, payload FROM crm.job WHERE kind = 'crm_intake_process' AND payload->>'tenant_id' = $1",
            str(s["tenant"]),
        )
    payload = json.loads(job["payload"]) if isinstance(job["payload"], str) else dict(job["payload"])
    await build_handler(s["keys"])(app_pool, Job(job["id"], "crm_intake_process", payload, 1, 5))
    async with app_pool.acquire() as conn:
        await conn.execute(
            "UPDATE crm.job SET status = 'cancelled', finished_at = now() WHERE id = $1::uuid", job["id"]
        )
    trip = await _seed(app_pool, s["tenant"], "SELECT row(sales_partner_id, lead_source)::text FROM crm.trip LIMIT 1")
    assert trip == f"({s['partner']},sales_partner)"
    with pytest.raises(NotFound):  # the intake token never opens the view
        await public.home(app_pool, s["keys"], token, None)


async def test_the_view_link_signs_in_with_a_code_and_lists_trips(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    s = await _setup(app_pool)
    trip = await _booked_trip(app_pool, s["tenant"])
    await _accepted(admin, s["tenant"], trip, "900")
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"], action=Action.SALES_PARTNER_MANAGE) as ctx:
        await service.set_trip_partner(ctx, trip, s["partner"])
        link = await service.issue_link(ctx, s["partner"], "view", intake_form_id=None, days=None)
    token = link["path"].removeprefix("/c/a/")
    anonymous = await public.home(app_pool, s["keys"], token, None)
    assert anonymous["signed_in"] is False and anonymous["agency"] == "Sen Vàng Travel" and "trips" not in anonymous
    sent = await public.request_code(app_pool, s["keys"], ECHO, token, origin=None)
    assert sent["sent"] is False and len(sent["dev_code"]) == 6  # no mailbox on this agency: the Dev echo only
    wrong = "000000" if sent["dev_code"] != "000000" else "111111"
    session, refused = await public.verify_code(app_pool, ECHO, token, wrong, origin=None)
    assert session is None and refused == {"detail": "code_invalid", "attempts_left": 4}
    session, ok = await public.verify_code(app_pool, ECHO, token, sent["dev_code"], origin=None)
    assert session and ok == {"signed_in": True}
    view = await public.home(app_pool, s["keys"], token, session)
    assert view["partner_name"] == "Kite Hill Tours"
    [seen] = view["trips"]
    assert seen["ref"] == "T-" + trip.hex[:8].upper() and seen["stage"] == "booked" and seen["travellers"] == 2
    assert seen["commission"]["amount"] == "90.00" and view["totals"] == [
        {"currency": "USD", "open": "90.00", "paid": "0.00"}
    ]
    assert "Kim Lien" not in json.dumps(view)
    await public.sign_out(app_pool, token, session)
    assert (await public.home(app_pool, s["keys"], token, session))["signed_in"] is False
    # Revoked by the owner: the link is gone.
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"], action=Action.SALES_PARTNER_MANAGE) as ctx:
        await service.revoke_link(ctx, s["partner"], uuid.UUID(link["link_id"]))
    with pytest.raises(NotFound):
        await public.home(app_pool, s["keys"], token, None)


async def test_cancelling_the_trip_cancels_the_partner_commission(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    s = await _setup(app_pool)
    trip = await _booked_trip(app_pool, s["tenant"])
    await _accepted(admin, s["tenant"], trip, "500")
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"], action=Action.TRIP_EDIT) as ctx:
        await service.set_trip_partner(ctx, trip, s["partner"])
    from app import stages

    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"], action=Action.TRIP_CANCEL) as ctx:
        plan = await stages.cancel(ctx, trip, reason="client_request", dry_run=True)
        assert [c["amount"] for c in plan["partner_commissions"]] == ["50.00"]
        await stages.cancel(ctx, trip, reason="client_request", dry_run=False)
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"], action=Action.VIEW_MARGIN) as ctx:
        listed = await service.list_commissions(ctx, status="all", partner_id=None, trip_id=trip)
    assert [(r["status"], r["cancel_reason"]) for r in listed["items"]] == [("cancelled", "trip_cancelled")]
