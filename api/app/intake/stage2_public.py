"""The public side of the Stage 2 planning form (C-02, card CRM-15): open it from a signed link, submit it. No token
but the link's own; the agency, trip and client come from the link inside :func:`app.tenancy.client_link_tx` (the
crm-012 lookup policy ``client_link_token_lookup``). This module is the only one that opens that context
(``tests/unit/test_code_rules.py``), and it never reaches ``app.travellers.reveal`` (``test_reveal_boundary.py``).

- :func:`read` answers the page: the definition (pages, R31 branches), the trip style the branches read, the agency's
  name, and the bands the client already gave (prefill, F2 only). The first open moves the link ``sent -> opened``.
- :func:`submit` checks the Origin (the client host, ``INTAKE_PAGE_ORIGINS``), validates on the server (hidden
  branches dropped and counted, health only with the consent), writes the answers (:func:`app.intake.stage2.apply`)
  and moves the link to ``submitted``. The link stays usable until it expires (a second device, the spouse: Fix B11);
  a repeated ``client_submission_id`` stores nothing new.

Unknown, expired, revoked or foreign token, or a link of another purpose: 404 ``link_not_found``, the same answer for
all (no probing).
"""

from __future__ import annotations

import hashlib
from typing import Any

import asyncpg

from app.errors import Forbidden, Unavailable
from app.intake import stage2
from app.intake.public import origin_allowed, record_drop
from app.settings import Settings
from app.tenancy import client_link_tx
from app.vault import KeyProvider, Vault

TRIP_SQL = "SELECT trip_style FROM crm.trip WHERE trip_id = $1"
AGENCY_SQL = "SELECT display_name, default_locale FROM crm.tenant WHERE tenant_id = $1"
OPENED_SQL = """
UPDATE crm.client_link SET status = 'opened', first_opened_at = now() WHERE link_id = $1 AND status = 'sent'
"""


def hash_hex(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def read(pool: asyncpg.Pool, keys: KeyProvider, token: str) -> dict[str, Any]:
    async with client_link_tx(pool, hash_hex(token), purpose=stage2.PURPOSE) as (ctx, link):
        trip_style = await ctx.conn.fetchval(TRIP_SQL, link.trip_id)
        agency = await ctx.conn.fetchrow(AGENCY_SQL, link.tenant_id)
        await ctx.conn.execute(OPENED_SQL, link.link_id)
        if link.status == "sent":
            ctx.audit("client_link_open", "allowed", "client_link", str(link.link_id))
        return {
            **stage2.definition(),
            "trip_style": trip_style,
            "agency_name": agency["display_name"],
            "default_locale": agency["default_locale"],
            "prefill": await stage2.prefill(ctx, Vault(keys, ctx), link.traveler_id),  # type: ignore[arg-type]
            "submitted": link.status == "submitted",
        }


async def submit(
    pool: asyncpg.Pool, keys: KeyProvider, settings: Settings, token: str, raw: Any, *, origin: str | None
) -> dict[str, Any]:
    if settings.intake_origin_check and not settings.intake_page_origins:
        raise Unavailable("intake_not_configured")
    async with client_link_tx(pool, hash_hex(token), purpose=stage2.PURPOSE) as (ctx, link):
        if not origin_allowed(origin, settings, ()):
            record_drop("stage2_origin", None)
            raise Forbidden("origin_not_allowed")
        trip_style = await ctx.conn.fetchval(TRIP_SQL, link.trip_id)
        answers = stage2.checked(raw, trip_style)
        if link.traveler_id is None:  # pragma: no cover — the crm-012 CHECK requires it for stage2_form
            raise Forbidden("link_not_usable")
        vault = Vault(keys, ctx)  # type: ignore[arg-type]
        done = await stage2.apply(
            ctx,
            vault,
            link.trip_id,
            link.traveler_id,
            answers,
            consent_source="form",
            link_id=link.link_id,
            idempotency_key=f"link:{answers.client_submission_id}"[:80],
        )
        await ctx.conn.execute(stage2.LINK_SUBMITTED_SQL, link.link_id)
        return {"status": "received" if done["status"] in ("applied", "duplicate") else done["status"]}
