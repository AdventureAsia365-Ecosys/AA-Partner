"""AA-1113 (root ADR 0009 decision 6; S284 tech lead redesign; crm-077): the CRM's own monthly cap per model connection.

A model connection (an AWS account's Bedrock, a vendor API) has one budget at its provider. CIS (with TripPlanner) and
the CRM spend on the same connections, each through its own gateway and ledger, and neither reads the other's (root
ADR 0008a: no grants in either direction; tech lead S284: the CRM reads no CIS view). So each app keeps its OWN
monthly cap per connection, and the shares together stay under the provider budget, so the provider total is never
exceeded: the CIS share is the connection's monthly budget in the CIS gateway registry, the provider budget the CIS
setting ``cis.spend.connection_provider_budget_usd`` (CIS 453), and the CRM share the CRM setting
``crm.ai.connection_monthly_cap_usd`` (crm-077, :mod:`app.llm.connection_share`). CIS AI gateway > Budgets reads the
CRM share through the CRM settings relay and flags a connection whose shares add up to more than the provider budget.

Before every call the gateway asks :func:`check_route` (right after the app profile), which asks :func:`refusal` once
per connection of the stage's route (AA-1138: a route may span connections, crm-078):

- each route entry's connection is named in the CIS registry's terms (:data:`CIS_CONNECTION`: the task role's own
  Bedrock, ``bedrock``, is the account the CRM runs on, ``acc2``; the named connection ``bedrock_acc1`` of crm-078 is
  ``acc1``; a role-assumed connection, ``bedrock_role``, has no mapping yet, so no cap applies to it, logged once per
  process);
- no entry in the setting for that connection = no CRM cap: nothing to check, nothing read. Except a route entry's
  NAMED connection (``crm.llm_stage_route.connection_key``, AA-1138): it is the CRM's spend on an account it does not
  run on, so under the ``dev`` rules it is never used without its own CRM cap (fail closed: the entry is refused
  ``connection_cap_missing``, class ``budget``, and the route goes on with the next entry; a named connection the
  CIS registry has no key for is refused the same way). The order of the live steps (setting, Infra apply, deploy)
  then cannot open an uncapped window. The ``prod`` rules only log it (pay per use);
- an entry whose connection stops is skipped (logged): the route goes on with the entries left, so a capped acc1 sends
  the draft to Nova Lite on acc2; when every entry stops, the call is refused with the first entry's refusal;
- the CRM's spend of this UTC month on that connection (``crm.llm_usage_daily``, every agency): the rows of a
  (stage, model) pair a route entry runs on that named connection (``crm.llm_stage_route.connection_key``) count for
  that connection; every other row, the guardrail rows included, counts for the process's own connection (a pair
  that is routed on both counts for both: never under-counted). At or above the cap: the ``dev`` spend rules
  (:mod:`app.llm.spend_environment`, the default) refuse ``connection_budget_exhausted`` (class ``budget``, one 0 USD
  row, nothing scrubbed or sent, the draft says "write it by hand"); the ``prod`` rules (pay per use, tech lead S283:
  a connection cap never stops a customer call) let the call go on and log ``crm_llm_connection_cap_reached``;
- the setting unreadable (not an object of amounts) or the month spend unreadable: ``dev`` refuses ``spend_unreadable``
  (fail closed, the CRM's rule for its other budgets), ``prod`` goes on with a warning.

The cap comes from the settings snapshot (no database read); the month spend is read on the call's own connection in a
savepoint, like :mod:`app.llm.app_profile` (a platform rollup, no tenant rows), and only when the connection has a cap.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import asyncpg

from app.aa_llm import errors as llm_errors
from app.aa_llm.budget import REASON_SPEND_UNREADABLE
from app.llm import spend_environment

log = logging.getLogger(__name__)

CONNECTION_BUDGET_EXHAUSTED = "connection_budget_exhausted"
CONNECTION_CAP_MISSING = "connection_cap_missing"  # AA-1138: a named connection with no CRM cap (dev rules)
SPEND_UNREADABLE = REASON_SPEND_UNREADABLE
# The CRM provider's connection key (app.llm.providers) -> the CIS registry's connection key, the key of the setting's
# entries. The ECS task role's own Bedrock is the account the CRM runs on; bedrock_acc1 (AA-1138, crm-078) is acc1's
# invoker role; the CRM_LLM_ROLE_ARN connection is named once its account is (follow-up). Names, not a limit.
CIS_CONNECTION = {"bedrock": "acc2", "bedrock_acc1": "acc1"}

# The month spend of the process's own connection: every row no route entry sends elsewhere (guardrail rows included).
OWN_MONTH_SQL = """
SELECT coalesce(sum(u.cost_usd), 0) AS usd FROM crm.llm_usage_daily u WHERE u.day >= $1
   AND (NOT EXISTS (SELECT 1 FROM crm.llm_stage_route r
                     WHERE r.stage = u.stage AND r.model = u.model AND r.connection_key IS NOT NULL)
        OR EXISTS (SELECT 1 FROM crm.llm_stage_route r
                    WHERE r.stage = u.stage AND r.model = u.model AND r.connection_key IS NULL))
"""
# The month spend of a named connection: the rows of the (stage, model) pairs a route entry runs on it.
NAMED_MONTH_SQL = """
SELECT coalesce(sum(u.cost_usd), 0) AS usd FROM crm.llm_usage_daily u WHERE u.day >= $1
   AND EXISTS (SELECT 1 FROM crm.llm_stage_route r
                WHERE r.stage = u.stage AND r.model = u.model AND r.connection_key = $2)
"""

_unmapped_logged: set[str] = set()  # provider connection keys logged once


@dataclass(frozen=True)
class CapState:
    """One capped connection as the CRM sees it: its own cap and its own month spend."""

    connection_key: str
    monthly_cap_usd: Decimal
    month_spent_usd: Decimal

    @property
    def stops(self) -> bool:
        """At or above the cap (a 0 USD cap is reached at once)."""
        return self.month_spent_usd >= self.monthly_cap_usd


def month_start(now: dt.datetime | None = None) -> dt.date:
    """The first day of the current UTC month."""
    now = now or dt.datetime.now(dt.UTC)
    return now.astimezone(dt.UTC).date().replace(day=1)


def cis_connection(provider: Any) -> str | None:
    """The CIS connection key of the provider the call would use, None when it has none (the stub, a role-assumed
    connection not mapped yet: logged once per key and process)."""
    key = getattr(provider, "connection_key", None)
    if not isinstance(key, str) or not key or key.startswith("stub"):
        return None
    mapped = CIS_CONNECTION.get(key)
    if mapped is None and key not in _unmapped_logged:
        _unmapped_logged.add(key)
        log.warning("crm_llm_connection_cap_unmapped connection=%s", key)
    return mapped


async def month_spend(conn: asyncpg.Connection, since: dt.date | None = None, route_key: str | None = None) -> Decimal:
    """The CRM's AI spend of this UTC month (every agency) on one connection (``route_key``: a route entry's named
    connection, None = the process's own), read in a savepoint. Raises on a read error."""
    async with conn.transaction():
        if route_key is None:
            usd = await conn.fetchval(OWN_MONTH_SQL, since or month_start())
        else:
            usd = await conn.fetchval(NAMED_MONTH_SQL, since or month_start(), route_key)
    return Decimal(usd or 0)


async def read_state(conn: asyncpg.Connection, cis_key: str, route_key: str | None = None) -> CapState | None:
    """The connection's cap state read now, None when the CRM has no cap on it. Raises
    :class:`connection_share.CapUnreadable` or the read error as is (the caller decides). ``connection_share`` ships
    with the crm-077 migration PR (MIG-4: the key's reader rides with its seed); on a tree without it the setting does
    not exist yet, which is its default ``{}``: no cap (logged once per process)."""
    try:
        from app.llm import connection_share
    except ImportError:
        if "reader-missing" not in _unmapped_logged:
            _unmapped_logged.add("reader-missing")
            log.warning("crm_llm_connection_cap_reader_missing connection=%s", cis_key)
        return None
    cap = await connection_share.share_for(conn, cis_key)  # AA-1143: the CIS copy (crm-079), else the setting
    if cap is None:
        return None
    spent = await month_spend(conn, route_key=route_key)
    return CapState(connection_key=cis_key, monthly_cap_usd=cap, month_spent_usd=spent)


def _missing(connection: str) -> tuple[str | None, str | None]:
    """AA-1138: a route entry's named connection without a CRM cap: refused under the dev rules (the entry is left
    out of the route), logged only under the prod rules."""
    prod = spend_environment.is_prod()
    log.warning("crm_llm_connection_cap_missing connection=%s prod=%s", connection, prod)
    return (None, None) if prod else (CONNECTION_CAP_MISSING, llm_errors.BUDGET)


def _unreadable(cis_key: str, error: str, prod: bool) -> tuple[str | None, str | None]:
    log.warning("crm_llm_connection_cap_unreadable connection=%s error=%s prod=%s", cis_key, error, prod)
    return (None, None) if prod else (SPEND_UNREADABLE, None)


async def refusal(conn: asyncpg.Connection, provider: Any) -> tuple[str | None, str | None]:
    """(refusal code, error class) when the CRM's cap on this connection stops this call, else (None, None). Never
    raises."""
    route_key = getattr(provider, "route_connection", None)
    named = isinstance(route_key, str) and bool(route_key)
    cis_key = cis_connection(provider)
    if cis_key is None:
        return _missing(route_key) if named else (None, None)
    prod = spend_environment.is_prod()
    try:
        state = await read_state(conn, cis_key, route_key if named else None)
    except Exception as exc:  # noqa: BLE001 — class only
        return _unreadable(cis_key, type(exc).__name__, prod)
    if state is None:
        return _missing(cis_key) if named else (None, None)
    if not state.stops:
        return None, None
    log.warning(
        "crm_llm_connection_cap_reached connection=%s spent=%s cap=%s prod=%s",
        cis_key,
        state.month_spent_usd,
        state.monthly_cap_usd,
        prod,
    )
    if prod:
        return None, None
    return CONNECTION_BUDGET_EXHAUSTED, llm_errors.BUDGET


async def check_route(
    conn: asyncpg.Connection, profile: Any, provider: Any, entry_provider: Callable[[Any], Any]
) -> tuple[str | None, str | None, Any]:
    """AA-1138: (refusal code, error class, the profile the call goes on with). Each route entry's connection is
    checked once (:func:`refusal`); an entry whose connection stops is left out of the route, and only when every
    entry stops is the call refused (the first entry's refusal, the profile unchanged). Without a route (no profile,
    or a profile without entries: the gateway refuses it later) the gateway's own ``provider`` is checked. Never
    raises."""
    route = tuple(getattr(profile, "route", ()) or ())
    if not route:
        code, error = await refusal(conn, provider)
        return code, error, profile
    seen: dict[str | None, tuple[str | None, str | None]] = {}
    kept, first = [], None
    for entry in route:
        who = entry_provider(entry)
        key = getattr(who, "connection_key", None)
        if key not in seen:
            seen[key] = await refusal(conn, who)
        if seen[key][0] is None:
            kept.append(entry)
        elif first is None:
            first = seen[key]
    if first is None:
        return None, None, profile
    if not kept:
        return first[0], first[1], profile
    log.warning("crm_llm_connection_cap_skipped stage=%s kept=%d of=%d", profile.stage, len(kept), len(route))
    return None, None, dataclasses.replace(profile, route=tuple(kept))
