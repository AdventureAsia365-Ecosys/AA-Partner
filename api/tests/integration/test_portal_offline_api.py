"""AA-954 (card CRM-24b, R16; design v3 §2.9 rule 7) on PostgreSQL 15 with the RDS role shape: what the portal home
gives the installed page so it can keep the itinerary offline, as crm_app through the portal link context.

- ``offline_ref`` (both answers): opaque and stable per link, different for another link, never the token or its
  stored SHA-256; the page uses it to drop an offline copy that belongs to another link.
- ``link_expires_at`` (signed in only): the link's own expiry, so the offline copy dies with the link.
- ``agency_contact`` (signed in only): the agency mailbox the portal mails come from, the trip owner's own first, then
  the agency's; ``null`` without one; never in the signed-out answer.

The party and the agency are invented (``test_portal_person_api._party``).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import asyncpg
import pytest

from app.mail import mailbox
from app.portal import public as portal
from app.tenancy import tenant_tx
from app.vault import Vault
from tests.integration.test_portal_person_api import _home, _party, _sign_in

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

SIGNED_IN_ONLY = ("link_expires_at", "agency_contact")


async def _connect(pool: asyncpg.Pool, s: dict[str, Any], *, scope: str, address: str) -> None:
    async with tenant_tx(pool, s["agent"], s["tenant"]) as ctx:
        await mailbox.connect(
            ctx,
            Vault(s["keys"], ctx),
            scope=scope,
            provider="google",
            secret={"refresh_token": "rt", "address": address},
        )


async def test_the_signed_out_answer_carries_only_the_offline_ref(app_pool: asyncpg.Pool) -> None:
    s = await _party(app_pool)
    lead_token, adult_token = s["links"][s["lead"]], s["links"][s["adult"]]
    out = await _home(app_pool, s, lead_token, None)
    assert out["signed_in"] is False
    assert set(out) == {"agency_name", "signed_in", "offline_ref"}
    ref = out["offline_ref"]
    assert len(ref) == 32 and all(c in "0123456789abcdef" for c in ref)
    assert ref not in (lead_token, portal.hash_hex(lead_token)) and ref not in portal.hash_hex(lead_token)
    assert (await _home(app_pool, s, lead_token, None))["offline_ref"] == ref  # stable
    assert (await _home(app_pool, s, adult_token, None))["offline_ref"] != ref  # per link
    for key in SIGNED_IN_ONLY:
        assert key not in out


async def test_signed_in_the_link_expiry_and_no_contact_without_a_mailbox(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    s = await _party(app_pool)
    token = s["links"][s["lead"]]
    session = await _sign_in(app_pool, s, token, s["lead"])  # the lead's address reaches the child too
    out = await _home(app_pool, s, token, session)
    assert out["signed_in"] is True
    assert out["offline_ref"] == portal.offline_ref(token)
    assert out["agency_contact"] is None
    async with admin.transaction():  # the master reads the row inside the agency (FORCE row level security)
        await admin.execute("SELECT set_config('app.tenant_id', $1, true)", str(s["tenant"]))
        expires = await admin.fetchval(
            "SELECT expires_at FROM crm.client_link WHERE token_hash = decode($1, 'hex')", portal.hash_hex(token)
        )
    assert dt.datetime.fromisoformat(out["link_expires_at"]) == expires
    assert expires > dt.datetime.now(dt.UTC)


async def test_the_agency_contact_is_the_trip_owners_mailbox_first(app_pool: asyncpg.Pool) -> None:
    s = await _party(app_pool)
    token = s["links"][s["lead"]]
    session = await _sign_in(app_pool, s, token, s["lead"])  # the lead's address reaches the child too
    await _connect(app_pool, s, scope="agency", address="desk@lantern-tours.example")
    assert (await _home(app_pool, s, token, session))["agency_contact"] == {"email": "desk@lantern-tours.example"}
    await _connect(app_pool, s, scope="own", address="ines@lantern-tours.example")  # the agent owns the trip
    assert (await _home(app_pool, s, token, session))["agency_contact"] == {"email": "ines@lantern-tours.example"}
    # A companion's session gets the same agency contact; the signed-out answer never carries it.
    adult = s["links"][s["adult"]]
    assert (await _home(app_pool, s, adult, await _sign_in(app_pool, s, adult)))["agency_contact"] == {
        "email": "ines@lantern-tours.example"
    }
    assert "agency_contact" not in await _home(app_pool, s, token, None)
