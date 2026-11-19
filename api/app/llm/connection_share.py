"""AA-1113 (crm-077; S284 tech lead redesign): the CRM's own monthly cap per model connection, the setting
``crm.ai.connection_monthly_cap_usd`` (CIS Platform > Settings, CRM tab, maker-checker; its guide in
``app/platform_settings/guides.json``).

A model connection has one budget at its provider. CIS (with TripPlanner) and the CRM spend on the same connections,
each through its own gateway and ledger, and neither reads the other's (root ADR 0008a). So each app keeps its OWN
monthly cap per connection, and the shares together stay under the provider budget: the CIS share is the
connection's monthly budget in the CIS gateway registry, the provider budget the CIS setting
``cis.spend.connection_provider_budget_usd`` (CIS 453), and this setting is the CRM share. CIS AI gateway > Budgets
reads it through the CRM settings relay (crm.ops.v1 ``/settings``) and flags a connection whose shares add up to more
than the provider budget.

The value is a JSON object {CIS connection key: USD per month}; a connection left out has no CRM cap. Read from the
settings snapshot (never the database on the call path). A value that is not such an object raises
:class:`CapUnreadable`: the caller (``app.llm.connection_cap``) treats it like any unreadable spend, never as "no cap".

AA-1143 (tech lead decision G3, crm-079): a connection's budget is now one set of rows in CIS (the total, one share per
app, CIS taking the remainder; CIS AI gateway > Budgets). CIS sends the CRM its part over crm.ops.v1
(``PUT /llm/connection-shares``, :func:`write_copy`) into ``crm.llm_connection_share``: every CIS connection that has a
total, with the CRM's share or 0. :func:`share_for` is the gateway's read: the copy's row for the connection, else (one
release, dual-read) this setting's entry. The copy is read on the call's own connection in a savepoint after a
``to_regclass`` check, so a database without crm-079 keeps the setting.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

import asyncpg

from app import platform_settings as ps

COPY_SQL = "SELECT connection_key, share_usd, updated_at FROM crm.llm_connection_share ORDER BY connection_key"
COPY_ONE_SQL = "SELECT share_usd FROM crm.llm_connection_share WHERE connection_key = $1"
COPY_READY_SQL = "SELECT to_regclass('crm.llm_connection_share') IS NOT NULL AS ok"
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")  # the CIS registry's connection key (crm-079 CHECK)
SHARE_MAX = Decimal("1000000")
REASON_MIN, REASON_MAX = 20, 500


class CapUnreadable(Exception):
    """The setting could not be read or is not an object of amounts >= 0."""


def parse(value: Any) -> dict[str, Decimal]:
    """{connection key: USD} from the setting value; :class:`CapUnreadable` on any other shape."""
    if not isinstance(value, Mapping):
        raise CapUnreadable("not a JSON object")
    out: dict[str, Decimal] = {}
    for key, raw in value.items():
        if not isinstance(key, str) or not key or isinstance(raw, bool) or not isinstance(raw, int | float | str):
            raise CapUnreadable(f"entry {key!r} is not a number")
        try:
            usd = Decimal(str(raw))
        except InvalidOperation:
            raise CapUnreadable(f"entry {key!r} is not a number") from None
        if not usd.is_finite() or usd < 0:
            raise CapUnreadable(f"entry {key!r} is below 0")
        out[key] = usd
    return out


def caps() -> dict[str, Decimal]:
    """Every CRM cap in force, by CIS connection key. Raises :class:`CapUnreadable`."""
    try:
        value = ps.get(ps.AI_CONNECTION_MONTHLY_CAP)
    except Exception as exc:  # noqa: BLE001 — the snapshot read never fails silently into "no cap"
        raise CapUnreadable(type(exc).__name__) from exc
    return parse(value)


def cap_for(connection_key: str) -> Decimal | None:
    """The CRM's monthly cap on one connection, None when it has none. Raises :class:`CapUnreadable`."""
    return caps().get(connection_key)


class CopyUnavailable(Exception):
    """crm-079 is not applied (the copy table is missing)."""


async def share_for(conn: asyncpg.Connection, connection_key: str) -> Decimal | None:
    """AA-1143: the CRM's monthly cap on one connection: its row in the CIS copy (crm-079), else the setting's entry
    (dual-read for one release), None when neither names it. Raises :class:`CapUnreadable` (the setting) or the read
    error as is (the caller fails closed under the dev rules)."""
    async with conn.transaction():
        if await conn.fetchval(COPY_READY_SQL):
            usd = await conn.fetchval(COPY_ONE_SQL, connection_key)
            if usd is not None:
                return Decimal(usd)
    return cap_for(connection_key)


def check_copy(shares: list[Mapping[str, Any]]) -> dict[str, Decimal]:
    """{connection key: USD} from a PUT body's ``shares`` list. ValueError on a bad key, a repeated key or an amount
    outside 0..1000000."""
    out: dict[str, Decimal] = {}
    for item in shares:
        key, raw = item.get("connection_key"), item.get("share_usd")
        if not isinstance(key, str) or not KEY_RE.fullmatch(key) or key in out:
            raise ValueError("connection_key")
        try:
            usd = Decimal(str(raw)).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            raise ValueError("share_usd") from None
        if isinstance(raw, bool) or not usd.is_finite() or usd < 0 or usd > SHARE_MAX:
            raise ValueError("share_usd")
        out[key] = usd
    return out


def _out(rows: list[Any]) -> dict:
    latest = max((r["updated_at"] for r in rows), default=None)
    return {
        "shares": [
            {"connection_key": r["connection_key"], "share_usd": f"{Decimal(r['share_usd']):.2f}"} for r in rows
        ],
        "updated_at": latest.isoformat() if latest is not None else None,
    }


async def read_copy(pool: asyncpg.Pool) -> dict:
    """GET /crm/ops/v1/llm/connection-shares: the copy as CIS sent it (``shares`` empty = none yet). Raises
    :class:`CopyUnavailable` before crm-079."""
    async with pool.acquire() as conn:
        if not await conn.fetchval(COPY_READY_SQL):
            raise CopyUnavailable
        return _out(await conn.fetch(COPY_SQL))


async def write_copy(pool: asyncpg.Pool, shares: dict[str, Decimal], *, reason: str, actor: str) -> dict:
    """PUT /crm/ops/v1/llm/connection-shares: replace the whole copy in one transaction (a connection the body does not
    name is dropped: it falls back to the setting). ValueError on a reason outside 20..500 characters;
    :class:`CopyUnavailable` before crm-079."""
    text = reason.strip()
    if not REASON_MIN <= len(text) <= REASON_MAX:
        raise ValueError("reason")
    async with pool.acquire() as conn:
        if not await conn.fetchval(COPY_READY_SQL):
            raise CopyUnavailable
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM crm.llm_connection_share WHERE NOT (connection_key = ANY($1::text[]))", list(shares)
            )
            for key, usd in shares.items():
                await conn.execute(
                    "INSERT INTO crm.llm_connection_share (connection_key, share_usd, reason, updated_by) "
                    "VALUES ($1, $2, $3, $4) ON CONFLICT (connection_key) DO UPDATE SET "
                    "share_usd = EXCLUDED.share_usd, reason = EXCLUDED.reason, updated_by = EXCLUDED.updated_by, "
                    "updated_at = now()",
                    key,
                    usd,
                    text,
                    actor[:120],
                )
        return _out(await conn.fetch(COPY_SQL))
