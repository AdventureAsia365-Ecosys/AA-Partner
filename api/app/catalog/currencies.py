"""The enabled currencies (AA-1191): which currencies the CRM shows and accepts for what an agency sells and is billed.

The value is a CIS setting, ``cis.billing.enabled_currencies`` (CIS Platform › Settings › Enabled currencies; tech lead
S295: USD only for now). The CRM holds no grant on any CIS schema (root ADR 0010 decision 4), so it reads it over
``catalog.http.v1`` 1.5 (``GET /catalog/v1/currencies``, the same M2M client and scope as the AA tour cache) and keeps
the answer for the ``cache_s`` CIS sends (the setting's own cache window). USD is the base currency and always enabled.

When CIS cannot be read the last answer stays; before any answer (or without ``CIS_API_URL``) the CRM serves USD only,
the setting's default, so a CIS outage never opens a currency the platform closed.

Readers: the proposal currency (``POST /trips/{id}/builder``), the agency's base currency (Settings › Payments), the AA
prices currencies, the planner price rounding steps and ``GET /currencies`` (the web hides what is not enabled). A
write in a currency that is not enabled is 422 ``currency_not_enabled`` (:func:`refuse_unless_enabled`). Supplier cost
currencies (an agency line's cost, supplier bills, commissions, FX overrides) are outside this rule.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

from app.catalog.client import CatalogClient, CatalogConfig, CatalogUnavailable
from app.errors import Invalid

if TYPE_CHECKING:
    from app.tenancy import TenantContext

log = logging.getLogger(__name__)

# technical-constant: the base currency (every price has a USD form); never disabled, not a policy value
BASE = "USD"
CODE = "currency_not_enabled"
# technical-constant: how long a failed CIS read is not retried (the CIS reader's default cache window, CIS 407)
RETRY_S = 30.0


def normalise(values: Iterable[Any] | None) -> tuple[str, ...]:
    """USD first, then every other code of ``values`` in its order, upper-cased, without duplicates. Pure."""
    out = [BASE]
    for v in values or ():
        code = v.strip().upper() if isinstance(v, str) else ""
        if code and code not in out:
            out.append(code)
    return tuple(out)


def problem(code: str | None, allowed: Iterable[str], field: str = "currency") -> str | None:
    """Why a write in ``code`` is refused (None = accepted; an empty value writes no currency). Pure."""
    allowed = tuple(allowed)
    if not code or code.strip().upper() in allowed:
        return None
    return (
        f"{field}: {code} is not an enabled currency (enabled: {', '.join(allowed)}). "
        "Adventure Asia enables currencies for the whole platform."
    )


def refuse_unless_enabled(code: str | None, allowed: Iterable[str], field: str = "currency") -> None:
    why = problem(code, allowed, field)
    if why:
        raise Invalid(CODE, why)


# The currency of a proposal of the caller's agency (RLS on crm.proposal keeps it to the tenant of the transaction).
PROPOSAL_CURRENCY_SQL = "SELECT currency FROM crm.proposal WHERE proposal_id = $1"


async def refuse_copy_unless_enabled(ctx: TenantContext, source_id: Any, allowed: Iterable[str]) -> None:
    """A builder working copy of a version in a currency that is not enabled is refused (422): the copy keeps the
    source's prices and is never re-priced. ``ctx`` = the route's tenant transaction. A missing source is left to the
    builder (its own 404)."""
    refuse_unless_enabled(await ctx.conn.fetchval(PROPOSAL_CURRENCY_SQL, source_id), allowed)


class EnabledCurrencies:
    """The process cache of the CIS answer (one per api task; every agency shares it: the setting is platform-wide)."""

    def __init__(self, config: CatalogConfig | None, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._client = CatalogClient(config) if config is not None else None
        self._clock = clock
        self._lock = asyncio.Lock()
        self._value: tuple[str, ...] = (BASE,)
        self._until = 0.0

    async def get(self) -> tuple[str, ...]:
        """The enabled currencies; never raises (the last answer, else USD only)."""
        if self._client is None or self._clock() < self._until:
            return self._value
        async with self._lock:
            if self._clock() < self._until:
                return self._value
            try:
                enabled, cache_s = await self._client.get_currencies()
            except CatalogUnavailable as exc:
                log.warning("crm_enabled_currencies_unavailable error=%s kept=%s", exc, ",".join(self._value))
                self._until = self._clock() + RETRY_S
                return self._value
            self._value, self._until = normalise(enabled), self._clock() + cache_s
            return self._value


async def enabled(state: Any) -> tuple[str, ...]:
    """The enabled currencies of the app (``app.state.enabled_currencies``); USD only when it is not wired."""
    reader = getattr(state, "enabled_currencies", None)
    return await reader.get() if isinstance(reader, EnabledCurrencies) else (BASE,)
