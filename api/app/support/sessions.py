"""The staff side of AA support access (AA-971, design v3 §4.8 L2 / L3; root ADR 0006b decision 6).

Every function opens :func:`app.tenancy.staff_tx` in one agency with actor ``support:<sub>`` or ``breakglass:<sub>``
(the WORKFORCE sub of the ``crm-staff`` token; never an e-mail), so each start, end and refusal is an event in **that
agency's** audit (actor kinds ``support`` / ``breakglass``). These functions touch only the session rows; reading the
agency is :mod:`app.support.reads`.

- :func:`start_support` (group ``crm-support``): needs the owner's live grant (403 ``no_support_grant``, on record),
  one staff member at a time (409 ``support_session_in_use``); the session ends with the grant or after
  :data:`SUPPORT_SESSION_HOURS`, whichever is first, and shows identity only if the owner ticked it. Starting again
  while one's own session is live returns it.
- :func:`start_breakglass` (group ``crm-ops``): no grant; a reason code from :data:`BREAKGLASS_REASONS` and a ticket;
  15 to 60 minutes; identity shown; an active or locked agency. Then, after the commit: the ``crm_breakglass_opened``
  warning (AA ops alarm: AA-CIS-Infra metric filter on ``$.event``) and the AA system mail to every active owner
  (best effort; ``owner_notified_at`` is recorded when at least one went out).
- :func:`end_session`: the caller ends their own live session of one kind.
- :func:`my_sessions` / :func:`granted_agencies`: what the staff UI needs to start (no agency data).
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any

import asyncpg
import structlog

from app.audit_export import registry
from app.auth.route_guard import Caller
from app.errors import Conflict, Forbidden, NotFound
from app.members.directory import PartnerDirectory
from app.members.mail import BREAKGLASS_REASONS, Mailer, breakglass_message
from app.members.recovery import OWNERS_SQL, TENANT_LABEL_SQL, mail_owners
from app.support.grants import staff_ref
from app.tenancy import STAFF_STATUSES, TENANT_STATUSES, SystemContext, staff_actor, staff_tx

log = logging.getLogger(__name__)
alerts = structlog.get_logger("crm.support")

SUPPORT_SESSION_HOURS = 8  # a working day; the staff member starts again inside the same grant if needed
BREAKGLASS_MINUTES_MIN, BREAKGLASS_MINUTES_MAX = 15, 60

EXPIRE_SQL = """
UPDATE crm.support_session s SET status = 'expired', ended_at = now(), ended_by = $1
 WHERE s.status = 'active'
   AND (s.expires_at <= now()
        OR (s.kind = 'support' AND NOT EXISTS (
              SELECT 1 FROM crm.support_grant g
               WHERE g.support_grant_id = s.support_grant_id AND g.status = 'active' AND g.expires_at > now())))
"""
LIVE_GRANT_SQL = """
SELECT support_grant_id, show_identity, expires_at FROM crm.support_grant
 WHERE status = 'active' AND expires_at > now()
 FOR UPDATE
"""
ACTIVE_SQL = """
SELECT support_session_id, kind, status, staff_iss, staff_sub, ticket_ref, reason_code, show_identity, started_at,
       expires_at, ended_at, owner_notified_at, true AS live
  FROM crm.support_session WHERE status = 'active' AND kind = $1
"""
MINE_SQL = """
SELECT support_session_id, kind, status, staff_iss, staff_sub, ticket_ref, reason_code, show_identity, started_at,
       expires_at, ended_at, owner_notified_at,
       expires_at > now() AS live
  FROM crm.support_session
 WHERE status = 'active' AND staff_iss = $1 AND staff_sub = $2
 ORDER BY started_at DESC
"""
START_SUPPORT_SQL = """
INSERT INTO crm.support_session (kind, support_grant_id, staff_iss, staff_sub, ticket_ref, show_identity, expires_at)
VALUES ('support', $1, $2, $3, $4, $5, LEAST($6::timestamptz, now() + make_interval(hours => $7)))
RETURNING support_session_id, kind, status, staff_iss, staff_sub, ticket_ref, reason_code, show_identity, started_at,
          expires_at, ended_at, owner_notified_at, true AS live
"""
START_BREAKGLASS_SQL = """
INSERT INTO crm.support_session (kind, staff_iss, staff_sub, ticket_ref, reason_code, show_identity, expires_at)
VALUES ('breakglass', $1, $2, $3, $4, true, now() + make_interval(mins => $5))
RETURNING support_session_id, kind, status, staff_iss, staff_sub, ticket_ref, reason_code, show_identity, started_at,
          expires_at, ended_at, owner_notified_at, true AS live
"""
END_SQL = """
UPDATE crm.support_session SET status = 'ended', ended_at = now(), ended_by = $3
 WHERE status = 'active' AND kind = $4 AND staff_iss = $1 AND staff_sub = $2
RETURNING support_session_id
"""
NOTIFIED_SQL = "UPDATE crm.support_session SET owner_notified_at = now() WHERE support_session_id = $1"
AGENCY_SQL = "SELECT display_name FROM crm.tenant WHERE tenant_id = $1"
GRANT_SUMMARY_SQL = """
SELECT g.support_grant_id, g.show_identity, g.expires_at,
       (SELECT s.staff_sub FROM crm.support_session s
         WHERE s.support_grant_id = g.support_grant_id AND s.status = 'active' AND s.expires_at > now()
         LIMIT 1) AS session_sub
  FROM crm.support_grant g WHERE g.status = 'active' AND g.expires_at > now()
"""


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _view(tenant_id: uuid.UUID, row: asyncpg.Record) -> dict[str, Any]:
    return {
        "tenant_id": str(tenant_id),
        "session_id": str(row["support_session_id"]),
        "kind": row["kind"],
        "status": row["status"] if row["live"] else "expired",
        "ticket_ref": row["ticket_ref"],
        "reason_code": row["reason_code"],
        "show_identity": bool(row["show_identity"]),
        "started_at": _iso(row["started_at"]),
        "expires_at": _iso(row["expires_at"]),
    }


def _ticket(ticket_ref: str) -> tuple[str, ...]:
    return (f"ticket:{ticket_ref}",)


async def _expire_stale(ctx: SystemContext) -> None:
    await ctx.conn.execute(EXPIRE_SQL, ctx.actor)


async def start_support(pool: asyncpg.Pool, caller: Caller, tenant_id: uuid.UUID, *, ticket_ref: str) -> dict[str, Any]:
    actor = staff_actor("support", caller)
    async with staff_tx(pool, tenant_id, actor=actor, statuses=STAFF_STATUSES["support"]) as ctx:
        await _expire_stale(ctx)
        grant = await ctx.conn.fetchrow(LIVE_GRANT_SQL)
        if grant is None:
            ctx.audit("support_session_start", "denied", "tenant", str(tenant_id), reason="no_support_grant",
                      object_ids=_ticket(ticket_ref))  # fmt: skip
            raise Forbidden("no_support_grant", "the agency owner has not allowed AA support access")
        current = await ctx.conn.fetchrow(ACTIVE_SQL, "support")
        if current is not None:
            if (current["staff_iss"], current["staff_sub"]) == (caller.iss, caller.sub):
                return _view(tenant_id, current)
            raise Conflict("support_session_in_use", "another AA staff member is working in this agency now")
        try:
            row = await ctx.conn.fetchrow(
                START_SUPPORT_SQL,
                grant["support_grant_id"],
                caller.iss,
                caller.sub,
                ticket_ref,
                grant["show_identity"],
                grant["expires_at"],
                SUPPORT_SESSION_HOURS,
            )
        except asyncpg.UniqueViolationError as exc:
            raise Conflict("support_session_in_use", "another AA staff member is working in this agency now") from exc
        ctx.audit(
            "support_session_start",
            "allowed",
            "support_session",
            str(row["support_session_id"]),
            fields=("show_identity",) if row["show_identity"] else (),
            object_ids=_ticket(ticket_ref),
        )
        return _view(tenant_id, row)


async def start_breakglass(
    pool: asyncpg.Pool,
    caller: Caller,
    tenant_id: uuid.UUID,
    *,
    ticket_ref: str,
    reason_code: str,
    minutes: int,
    directory: PartnerDirectory | None,
    mailer: Mailer | None,
    web_origin: str,
) -> dict[str, Any]:
    if reason_code not in BREAKGLASS_REASONS or not BREAKGLASS_MINUTES_MIN <= minutes <= BREAKGLASS_MINUTES_MAX:
        raise ValueError("reason or minutes out of range")  # the route model bounds both
    actor = staff_actor("breakglass", caller)
    async with staff_tx(pool, tenant_id, actor=actor, statuses=STAFF_STATUSES["breakglass"]) as ctx:
        await _expire_stale(ctx)
        current = await ctx.conn.fetchrow(ACTIVE_SQL, "breakglass")
        if current is not None:
            if (current["staff_iss"], current["staff_sub"]) == (caller.iss, caller.sub):
                return {**_view(tenant_id, current), "owners_notified": 0}
            raise Conflict("breakglass_in_use", "another AA staff member holds break-glass access to this agency")
        try:
            row = await ctx.conn.fetchrow(
                START_BREAKGLASS_SQL, caller.iss, caller.sub, ticket_ref, reason_code, minutes
            )
        except asyncpg.UniqueViolationError as exc:
            raise Conflict("breakglass_in_use", "another AA staff member holds break-glass access") from exc
        session_id = row["support_session_id"]
        ctx.audit(
            "breakglass_open",
            "allowed",
            "support_session",
            str(session_id),
            reason=reason_code,
            object_ids=_ticket(ticket_ref),
        )
        owners = await ctx.conn.fetch(OWNERS_SQL)
        agency = await ctx.conn.fetchval(TENANT_LABEL_SQL, tenant_id) or "AA Partner"
    # Committed. AA ops alarm (Infra metric filter on this event name): ids and codes only.
    alerts.warning(
        "crm_breakglass_opened",
        tenant_id=str(tenant_id),
        session_id=str(session_id),
        reason_code=reason_code,
        staff_ref=staff_ref(caller.sub),
        expires_at=_iso(row["expires_at"]),
    )
    notified = await mail_owners(
        owners,
        directory,
        mailer,
        lambda email: breakglass_message(
            email,
            agency=agency,
            reason_code=reason_code,
            opened_at=row["started_at"],
            expires_at=row["expires_at"],
            link=web_origin,
        ),
        tenant_id=tenant_id,
        tag="breakglass_notice",
    )
    if notified:
        async with staff_tx(pool, tenant_id, actor=actor, statuses=TENANT_STATUSES) as ctx:
            await ctx.conn.execute(NOTIFIED_SQL, session_id)
            ctx.audit("breakglass_owner_notice", "allowed", "support_session", str(session_id))
    else:
        log.warning("crm_breakglass_owner_not_mailed tenant=%s session=%s", tenant_id, session_id)
    return {**_view(tenant_id, row), "owners_notified": notified}


async def end_session(pool: asyncpg.Pool, caller: Caller, tenant_id: uuid.UUID, *, kind: str) -> dict[str, Any]:
    """The caller ends their own live session of ``kind`` (any agency status: ending is always allowed). 404
    ``no_support_session`` when there is none."""
    actor = staff_actor(kind, caller)
    async with staff_tx(pool, tenant_id, actor=actor, statuses=TENANT_STATUSES) as ctx:
        ended = await ctx.conn.fetch(END_SQL, caller.iss, caller.sub, actor, kind)
        if not ended:
            raise NotFound("no_support_session")
        action = "breakglass_close" if kind == "breakglass" else "support_session_end"
        for r in ended:
            ctx.audit(action, "allowed", "support_session", str(r["support_session_id"]))
    return {"tenant_id": str(tenant_id), "ended": [str(r["support_session_id"]) for r in ended]}


async def my_sessions(pool: asyncpg.Pool, caller: Caller, tenant_id: uuid.UUID) -> dict[str, Any]:
    """The caller's sessions in this agency that are still marked active (expired ones shown as expired)."""
    async with staff_tx(pool, tenant_id, actor=staff_actor("support", caller), statuses=TENANT_STATUSES) as ctx:
        rows = await ctx.conn.fetch(MINE_SQL, caller.iss, caller.sub)
        agency = await ctx.conn.fetchval(AGENCY_SQL, tenant_id)
        return {
            "tenant_id": str(tenant_id),
            "agency_name": agency,
            "agency_status": ctx.status,
            "sessions": [_view(tenant_id, r) for r in rows],
        }


async def granted_agencies(pool: asyncpg.Pool, caller: Caller) -> dict[str, Any]:
    """The agencies whose owner allows AA support now (ids, the agency name, the window, whether someone works in it).
    The ids come from the crm-007 registry (``crm.tenant`` is FORCE RLS), then one staff context per agency."""
    actor = staff_actor("support", caller)
    items: list[dict[str, Any]] = []
    for agency in sorted(await registry.agencies(pool), key=lambda a: str(a.tenant_id)):
        try:
            async with staff_tx(pool, agency.tenant_id, actor=actor, statuses=STAFF_STATUSES["support"]) as ctx:
                grant = await ctx.conn.fetchrow(GRANT_SUMMARY_SQL)
                if grant is None:
                    continue
                items.append(
                    {
                        "tenant_id": str(agency.tenant_id),
                        "agency_name": await ctx.conn.fetchval(AGENCY_SQL, agency.tenant_id),
                        "expires_at": _iso(grant["expires_at"]),
                        "show_identity": bool(grant["show_identity"]),
                        "in_use": grant["session_sub"] is not None,
                        "mine": grant["session_sub"] == caller.sub,
                    }
                )
        except (NotFound, Conflict):
            continue  # trashed, locked or offboarding: no support work there
    return {"items": items}
