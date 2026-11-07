"""AA-1038 R5 slice 3 on PostgreSQL 15 with the RDS role shape, as crm_app: TripPlanner leads (lead.submitted.v1) into
the agency's intake (app.tripplanner.leads.ingest), then through crm_intake_process onto the board.

- A lead of agency A lands in A's intake: one form_submission (ciphertext only) with A's system form "Trip planner"
  (lead source tripplanner, disabled for the public), one crm_intake_process job; after the job, a trip at Inquiry.
- Pulled twice, it stores once (idempotent on tp-lead:<lead_id>).
- Isolation: agency B sees none of A's submissions, forms or trips; A's lead offered under B is refused (ForeignLead)
  and writes nothing in B.
Invented names only.
"""

from __future__ import annotations

import os
import uuid
from datetime import date

import asyncpg
import pytest

from app.intake.process import build_handler
from app.jobs.queue import Job
from app.tenancy import system_tx
from app.tripplanner import leads
from app.vault import KeyProvider, Vault
from app.vault.wrapping import LocalKeyWrapper

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

TODAY = date(2026, 11, 7)


def _keys() -> KeyProvider:
    return KeyProvider({"local": LocalKeyWrapper({"k1": os.urandom(32)}, "k1")}, "local")


async def _agency(pool: asyncpg.Pool, name: str) -> uuid.UUID:
    tenant_id = uuid.uuid4()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        await conn.execute("SELECT set_config('app.actor', 'seed:test', true)")
        await conn.execute("INSERT INTO crm.tenant (tenant_id, display_name) VALUES ($1, $2)", tenant_id, name)
    return tenant_id


def _lead(tenant: uuid.UUID) -> dict:
    lid = str(uuid.uuid4())
    return {
        "contract": "lead.submitted.v1",
        "version": "1.0",
        "lead_id": lid,
        "idempotency_key": f"tp-lead:{lid}",
        "submitted_at": "2026-11-07T09:00:00Z",
        "tenant": {"tenant_id": str(tenant), "slug": "quillmere-voyages"},
        "source": {"app": "tripplanner", "trip_ref": str(uuid.uuid4())},
        "traveller": {"name": "Ada Fernwick", "email": f"ada.{lid[:8]}@example.invalid", "phone": None, "locale": "en"},
        "party": {"adults": 2, "child_ages": []},
        "trip": {
            "days": 4,
            "countries": ["Vietnam"],
            "start_date": None,
            "budget_band": None,
            "message": None,
            "tours": [],
            "day_plan": [{"day": 1, "stops": ["Jade Bay"]}],
        },
        "consent": {"contact": True, "marketing": False, "given_at": "2026-11-07T09:00:00Z"},
    }


async def _counts(pool: asyncpg.Pool, tenant_id: uuid.UUID, lead_id: str) -> dict[str, int]:
    async with system_tx(pool, tenant_id, actor="job:crm_tp_lead_pull") as ctx:
        return {
            "submissions": await ctx.conn.fetchval(
                "SELECT count(*) FROM crm.form_submission WHERE idempotency_key = $1", f"tp-lead:{lead_id}"
            ),
            "forms": await ctx.conn.fetchval("SELECT count(*) FROM crm.intake_form WHERE lead_source = 'tripplanner'"),
            "jobs": await ctx.conn.fetchval(
                "SELECT count(*) FROM crm.job WHERE kind = 'crm_intake_process' AND payload->>'tenant_id' = $1",
                str(tenant_id),
            ),
        }


async def test_a_lead_lands_in_its_agency_only(app_pool: asyncpg.Pool) -> None:
    keys = _keys()
    a = await _agency(app_pool, "Quillmere Voyages synthetic")
    b = await _agency(app_pool, "Ottervale Journeys synthetic")
    item = _lead(a)

    async with system_tx(app_pool, a, actor=leads.ACTOR) as ctx:
        assert await leads.ingest(ctx, Vault(keys, ctx), item, today=TODAY) == "stored"
    async with system_tx(app_pool, a, actor=leads.ACTOR) as ctx:
        assert await leads.ingest(ctx, Vault(keys, ctx), item, today=TODAY) == "duplicate"

    in_a = await _counts(app_pool, a, item["lead_id"])
    assert in_a == {"submissions": 1, "forms": 1, "jobs": 1}
    async with system_tx(app_pool, a, actor=leads.ACTOR) as ctx:
        form = await ctx.conn.fetchrow(
            "SELECT status, label, created_by FROM crm.intake_form WHERE lead_source = 'tripplanner'"
        )
        payload = await ctx.conn.fetchval(
            "SELECT payload_enc FROM crm.form_submission WHERE idempotency_key = $1", f"tp-lead:{item['lead_id']}"
        )
    assert form["status"] == "disabled" and form["label"] == "Trip planner" and form["created_by"] == leads.ACTOR
    assert b"Ada Fernwick" not in bytes(payload) and b"example.invalid" not in bytes(payload)

    # B sees nothing of A's lead, and A's lead offered under B is refused before anything is written.
    assert await _counts(app_pool, b, item["lead_id"]) == {"submissions": 0, "forms": 0, "jobs": 0}
    with pytest.raises(leads.ForeignLead):
        async with system_tx(app_pool, b, actor=leads.ACTOR) as ctx:
            await leads.ingest(ctx, Vault(keys, ctx), item, today=TODAY)
    assert await _counts(app_pool, b, item["lead_id"]) == {"submissions": 0, "forms": 0, "jobs": 0}

    # The intake job turns it into a trip at Inquiry in A (and only A).
    async with system_tx(app_pool, a, actor=leads.ACTOR) as ctx:
        submission_id = await ctx.conn.fetchval(
            "SELECT submission_id FROM crm.form_submission WHERE idempotency_key = $1", f"tp-lead:{item['lead_id']}"
        )
    handler = build_handler(keys)
    outcome = await handler(
        app_pool,
        Job(
            id="j-tp",
            kind="crm_intake_process",
            payload={"tenant_id": str(a), "submission_id": str(submission_id)},
            attempt=1,
            max_attempts=5,
        ),
    )
    assert outcome["status"] == "created"
    async with system_tx(app_pool, a, actor=leads.ACTOR) as ctx:
        trip = await ctx.conn.fetchrow(
            "SELECT stage, lead_source FROM crm.trip WHERE trip_id = $1", uuid.UUID(outcome["trip_id"])
        )
    assert trip["stage"] == "inquiry" and trip["lead_source"] == "tripplanner"
    async with system_tx(app_pool, b, actor=leads.ACTOR) as ctx:
        assert await ctx.conn.fetchval("SELECT count(*) FROM crm.trip WHERE lead_source = 'tripplanner'") == 0
