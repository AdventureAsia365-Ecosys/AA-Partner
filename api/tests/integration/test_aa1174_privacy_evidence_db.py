"""AA-1174 (S288): crm.ops.v1 1.18 privacy evidence (``app.ops.privacy_evidence.platform_privacy_evidence``) on
PostgreSQL with the RDS role shape, as crm_app through ``platform_tx`` (RLS FORCE: a bare crm_app count reads 0 rows).

Two invented agencies, seeded as crm_app in each agency's context:

- a marketing send to a contact whose latest marketing consent before the send is a yes (counted, no violation);
- a send after the contact said no (a violation);
- a send while the contact still said yes, withdrawn afterwards (the consent AT send time counts: no violation);
- a send to an address on the agency's suppression list since before the send (a violation);
- a send to a contact erased since: the traveller erase removes the consent and the dispatch row together, so the send
  leaves the counts entirely (never a false violation; ``excluded_erased`` stays 0);
- what is not a counted marketing send: a transactional mail, a marketing mail the gate dropped, a send outside the
  window;
- a second agency's send, so the answer sums across agencies.

The database is shared with the other integration files (agencies they leave behind are counted too), so every
assertion is on the change the seed makes. Invented ids and bytes only, no names or addresses.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import asyncpg
import pytest

from app.ops.privacy_evidence import platform_privacy_evidence
from tests.integration.test_crm010_schema import _ctx, _erase

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

DAYS = 30
STAFF = "staff-sub-aa1174"


def _blob() -> bytes:
    return os.urandom(40)  # shaped like a sealed value; never opened here


async def _traveller(conn: asyncpg.Connection, *, email: bool = False) -> uuid.UUID:
    tid = uuid.uuid4()
    await conn.execute(
        "INSERT INTO crm.traveler (traveler_id, display_name_enc, email_enc, email_bidx) VALUES ($1, $2, $3, $4)",
        tid,
        _blob(),
        _blob() if email else None,
        os.urandom(16) if email else None,
    )
    return tid


async def _consent(conn: asyncpg.Connection, tid: uuid.UUID, value: bool, days_ago: float) -> None:
    await conn.execute(
        "INSERT INTO crm.consent (traveler_id, purpose, value, source, at) "
        "VALUES ($1, 'marketing', $2, 'agent_recorded', now() - make_interval(secs => $3))",
        tid,
        value,
        days_ago * 86400,
    )


async def _send(
    conn: asyncpg.Connection,
    tid: uuid.UUID,
    days_ago: float,
    *,
    purpose: str = "marketing",
    status: str = "sent",
    gate: str = "sent",
) -> None:
    await conn.execute(
        "INSERT INTO crm.dispatch_queue (traveler_id, channel, purpose, origin, status, gate_result, decided_at) "
        "VALUES ($1, 'email', $2, 'system', $3, $4, now() - make_interval(secs => $5))",
        tid,
        purpose,
        status,
        gate,
        days_ago * 86400,
    )


async def _seed_agency_a(pool: asyncpg.Pool) -> dict[str, Any]:
    tenant = uuid.uuid4()
    ids: dict[str, Any] = {"tenant": tenant}
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant, "seed:test")
        await conn.execute("INSERT INTO crm.tenant (tenant_id) VALUES ($1)", tenant)
        await conn.execute("INSERT INTO crm.tenant_settings DEFAULT VALUES ON CONFLICT (tenant_id) DO NOTHING")

        yes = await _traveller(conn)
        await _consent(conn, yes, True, 2)
        await _send(conn, yes, 1)  # sent with a yes: counted, no violation

        said_no = await _traveller(conn)
        await _consent(conn, said_no, True, 5)
        await _consent(conn, said_no, False, 3)
        await _send(conn, said_no, 1)  # sent after the no: violation

        withdrew_later = await _traveller(conn)
        await _consent(conn, withdrew_later, True, 5)
        await _send(conn, withdrew_later, 3)  # yes at send time ...
        await _consent(conn, withdrew_later, False, 1)  # ... withdrawn afterwards: no violation

        suppressed = await _traveller(conn, email=True)
        await _consent(conn, suppressed, True, 5)
        await conn.execute(
            "INSERT INTO crm.campaign_suppression (email_bidx, reason, created_at) "
            "SELECT email_bidx, 'unsubscribed', now() - interval '4 days' FROM crm.traveler WHERE traveler_id = $1",
            suppressed,
        )
        await _send(conn, suppressed, 1)  # on the do-not-mail list at send time: violation

        uncertain_no = await _traveller(conn)
        # held send_uncertain (the provider may have delivered it) with no consent: sent and a violation
        await _send(conn, uncertain_no, 1, status="held", gate="send_uncertain")
        uncertain_yes = await _traveller(conn)
        await _consent(conn, uncertain_yes, True, 2)
        await _send(conn, uncertain_yes, 1, status="held", gate="send_uncertain")  # with a yes: sent, no violation

        erased = await _traveller(conn)
        await _consent(conn, erased, True, 5)
        await _send(conn, erased, 1)
        ids["erased"] = erased

        other = await _traveller(conn)
        await _consent(conn, other, True, 6)
        await _send(conn, other, 1, purpose="transactional")  # not marketing
        await _send(conn, other, 1, status="dropped", gate="no_consent")  # the gate stopped it
        await _send(conn, other, DAYS + 10)  # outside the window
    return ids


async def _seed_agency_b(pool: asyncpg.Pool) -> uuid.UUID:
    tenant = uuid.uuid4()
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant, "seed:test")
        await conn.execute("INSERT INTO crm.tenant (tenant_id) VALUES ($1)", tenant)
        yes = await _traveller(conn)
        await _consent(conn, yes, True, 2)
        await _send(conn, yes, 1)
    return tenant


async def test_privacy_evidence_counts_sends_against_consent_at_send_time(app_pool: asyncpg.Pool) -> None:
    before = await platform_privacy_evidence(app_pool, staff_sub=STAFF, days=DAYS)
    a = await _seed_agency_a(app_pool)
    await _seed_agency_b(app_pool)
    seeded = await platform_privacy_evidence(app_pool, staff_sub=STAFF, days=DAYS)

    def delta(answer: dict[str, Any]) -> dict[str, int]:
        keys = (
            "tenants",
            "marketing_sent",
            "marketing_uncertain",
            "violations",
            "excluded_erased",
            "consents_recorded",
        )
        return {k: answer[k] - before[k] for k in keys}

    # A: yes, said_no, withdrew_later, suppressed, uncertain_no, uncertain_yes, erased (7 sends, 2 held send_uncertain);
    # B: one send. Consents in the window: A 1+2+2+1+1+1+1, B 1. RLS FORCE under platform_tx: a missing agency context
    # would read 0 of them.
    assert delta(seeded) == {
        "tenants": 2,
        "marketing_sent": 8,
        "marketing_uncertain": 2,
        "violations": 3,
        "excluded_erased": 0,
        "consents_recorded": 10,
    }
    assert seeded["window_days"] == DAYS and seeded["last_consent_at"] is not None
    assert not ({"names", "emails", "tenant_ids"} & set(seeded))  # counts only

    # The erase takes the contact's consent and its dispatch rows: the send leaves the counts, no false violation.
    done = await _erase(app_pool, a["tenant"], "traveler", a["erased"])
    assert done["counts"]["consent"] == 1 and done["counts"]["dispatch_queue"] == 1
    erased = await platform_privacy_evidence(app_pool, staff_sub=STAFF, days=DAYS)
    assert delta(erased) == {
        "tenants": 2,
        "marketing_sent": 7,
        "marketing_uncertain": 2,
        "violations": 3,
        "excluded_erased": 0,
        "consents_recorded": 9,
    }
