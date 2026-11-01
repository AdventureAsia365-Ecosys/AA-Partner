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
  15 to 60 minutes; identity never shown (no MFA step-up here; crm-033 CHECK); an active or locked agency. Then,
  after the commit: the ``crm_breakglass_opened`` warning (AA ops alarm: AA-CIS-Infra metric filter on ``$.event``)
  and the AA system mail to every active owner (best effort; ``owner_notified_at`` is recorded when at least one went
  out).
- :func:`end_session`: the caller ends their own live session of one kind.
- :func:`my_sessions` / :func:`granted_agencies`: what the staff UI needs to start, and why a session is over (no
  agency data).
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
MY_SESSIONS_LIMIT = 10  # the console's "Your sessions in this agency": the live one and the last few that ended
# Why a session the caller holds is over (AA-1019 D1), for the console's wording: the staff member ended it, the owner
# ended AA support (grant revoked), its own window passed, or the owner's grant window passed first.
END_REASONS = ("staff_ended", "owner_ended", "session_expired", "grant_expired")

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
# The caller's own sessions in this agency for the console (AA-1019 S262 live finding D1): the live one first, then the
# most recent ended / revoked / expired ones, so the page can say why the session is over and list the history. A
# support session is live only while its grant is active and unexpired too (as ``support_tx`` and the owner's view).
MINE_SQL = """
SELECT s.support_session_id, s.kind, s.status, s.staff_iss, s.staff_sub, s.ticket_ref, s.reason_code, s.show_identity,
       s.started_at, s.expires_at, s.ended_at, s.owner_notified_at,
       s.status = 'active' AND s.expires_at > now()
         AND (s.kind = 'breakglass' OR (g.status = 'active' AND g.expires_at > now())) AS live,
       s.expires_at <= coalesce(s.ended_at, now()) AS window_passed,
       g.status AS grant_status
  FROM crm.support_session s
  LEFT JOIN crm.support_grant g ON g.support_grant_id = s.support_grant_id
 WHERE s.staff_iss = $1 AND s.staff_sub = $2
 ORDER BY (s.status = 'active') DESC, s.started_at DESC, s.support_session_id
 LIMIT $3
"""
START_SUPPORT_SQL = """
INSERT INTO crm.support_session (kind, support_grant_id, staff_iss, staff_sub, ticket_ref, show_identity, expires_at)
VALUES ('support', $1, $2, $3, $4, $5, LEAST($6::timestamptz, now() + make_interval(hours => $7)))
RETURNING support_session_id, kind, status, staff_iss, staff_sub, ticket_ref, reason_code, show_identity, started_at,
          expires_at, ended_at, owner_notified_at, true AS live
"""
START_BREAKGLASS_SQL = """
INSERT INTO crm.support_session (kind, staff_iss, staff_sub, ticket_ref, reason_code, show_identity, expires_at)
VALUES ('breakglass', $1, $2, $3, $4, false, now() + make_interval(mins => $5))
RETURNING support_session_id, kind, status, staff_iss, staff_sub, ticket_ref, reason_code, show_identity, started_at,
          expires_at, ended_at, owner_notified_at, true AS live
"""
END_SQL = """
UPDATE crm.support_session SET status = 'ended', ended_at = now(), ended_by = $3
 WHERE status = 'active' AND kind = $4 AND staff_iss = $1 AND staff_sub = $2
RETURNING support_session_id
"""
MY_BREAKGLASS_SQL = """
SELECT expires_at FROM crm.support_session
 WHERE kind = 'breakglass' AND status = 'active' AND expires_at > now() AND staff_iss = $1 AND staff_sub = $2
 ORDER BY started_at DESC LIMIT 1
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


def _end_reason(row: asyncpg.Record) -> str | None:
    """One of :data:`END_REASONS`, or None while the session is live. Rows without the ``window_passed`` /
    ``grant_status`` columns (the start and active queries) are live by construction."""
    if row["live"]:
        return None
    if row["status"] == "ended":
        return "staff_ended"
    if row["status"] == "revoked":
        return "owner_ended"
    # The grant's status speaks only for a session still marked active (not swept yet): revoking a grant revokes only
    # its active sessions, so one that had already expired keeps 'expired' and must not read "ended by the owner".
    if row["status"] == "active" and row["grant_status"] == "revoked":
        return "owner_ended"
    return "session_expired" if row["window_passed"] else "grant_expired"


def _view(tenant_id: uuid.UUID, row: asyncpg.Record) -> dict[str, Any]:
    status = row["status"]
    if status == "active" and not row["live"]:
        status = "expired"  # its window, or its grant's, passed: shown as what it is
    return {
        "tenant_id": str(tenant_id),
        "session_id": str(row["support_session_id"]),
        "kind": row["kind"],
        "status": status,
        "ticket_ref": row["ticket_ref"],
        "reason_code": row["reason_code"],
        "show_identity": bool(row["show_identity"]),
        "started_at": _iso(row["started_at"]),
        "expires_at": _iso(row["expires_at"]),
        "ended_at": _iso(row["ended_at"]),
        "end_reason": _end_reason(row),
    }


def _ticket(ticket_ref: str) -> tuple[str, ...]:
    return (f"ticket:{ticket_ref}",)


async def _expire_stale(ctx: SystemContext) -> None:
    await ctx.conn.execute(EXPIRE_SQL, ctx.actor)


async def start_support(pool: asyncpg.Pool, caller: Caller, tenant_id: uuid.UUID, *, ticket_ref: str) -> dict[str, Any]:
    actor = staff_actor("support", caller)
    async with staff_tx(pool, tenant_id, caller=caller, actor=actor, statuses=STAFF_STATUSES["support"]) as ctx:
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
    async with staff_tx(pool, tenant_id, caller=caller, actor=actor, statuses=STAFF_STATUSES["breakglass"]) as ctx:
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
        async with staff_tx(pool, tenant_id, caller=caller, actor=actor, statuses=TENANT_STATUSES) as ctx:
            await ctx.conn.execute(NOTIFIED_SQL, session_id)
            ctx.audit("breakglass_owner_notice", "allowed", "support_session", str(session_id))
    else:
        log.warning("crm_breakglass_owner_not_mailed tenant=%s session=%s", tenant_id, session_id)
    return {**_view(tenant_id, row), "owners_notified": notified}


async def end_session(pool: asyncpg.Pool, caller: Caller, tenant_id: uuid.UUID, *, kind: str) -> dict[str, Any]:
    """The caller ends their own live session of ``kind`` (any agency status: ending is always allowed). 404
    ``no_support_session`` when there is none."""
    actor = staff_actor(kind, caller)
    async with staff_tx(pool, tenant_id, caller=caller, actor=actor, statuses=TENANT_STATUSES) as ctx:
        ended = await ctx.conn.fetch(END_SQL, caller.iss, caller.sub, actor, kind)
        if not ended:
            raise NotFound("no_support_session")
        action = "breakglass_close" if kind == "breakglass" else "support_session_end"
        for r in ended:
            ctx.audit(action, "allowed", "support_session", str(r["support_session_id"]))
    return {"tenant_id": str(tenant_id), "ended": [str(r["support_session_id"]) for r in ended]}


async def my_sessions(pool: asyncpg.Pool, caller: Caller, tenant_id: uuid.UUID) -> dict[str, Any]:
    """The caller's own sessions in this agency, newest first with the live one on top: the live session (if any) and
    the last :data:`MY_SESSIONS_LIMIT` in all, ended / revoked / expired ones with their ``end_reason`` (AA-1019 D1:
    after the owner ends AA support the console says so instead of "no open session"). Ids, codes and times only."""
    actor = staff_actor("support", caller)
    async with staff_tx(pool, tenant_id, caller=caller, actor=actor, statuses=TENANT_STATUSES) as ctx:
        rows = await ctx.conn.fetch(MINE_SQL, caller.iss, caller.sub, MY_SESSIONS_LIMIT)
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
            async with staff_tx(pool, agency.tenant_id, caller=caller, actor=actor,
                                statuses=STAFF_STATUSES["support"]) as ctx:  # fmt: skip
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
        except (NotFound, Conflict, Forbidden):
            continue  # trashed, locked or offboarding: no support work there (the E2E identity: not synthetic)
    return {"items": items}


async def breakglass_agencies(pool: asyncpg.Pool, caller: Caller) -> dict[str, Any]:
    """AA-1019 (S-04): the agencies break-glass may open now (active or locked), for the staff console's picker: id,
    name, status and whether the caller already holds a live break-glass session there. No agency data, no grant;
    one staff context per agency (no audit event: nothing of the agency is read but its name, as
    :func:`granted_agencies`). Sorted by name."""
    actor = staff_actor("breakglass", caller)
    items: list[dict[str, Any]] = []
    for agency in sorted(await registry.agencies(pool), key=lambda a: str(a.tenant_id)):
        try:
            async with staff_tx(pool, agency.tenant_id, caller=caller, actor=actor,
                                statuses=STAFF_STATUSES["breakglass"]) as ctx:  # fmt: skip
                mine = await ctx.conn.fetchrow(MY_BREAKGLASS_SQL, caller.iss, caller.sub)
                items.append(
                    {
                        "tenant_id": str(agency.tenant_id),
                        "agency_name": await ctx.conn.fetchval(AGENCY_SQL, agency.tenant_id),
                        "agency_status": ctx.status,
                        "my_session_expires_at": _iso(mine["expires_at"]) if mine is not None else None,
                    }
                )
        except (NotFound, Conflict, Forbidden):
            continue  # trashed or offboarding (the E2E identity: not synthetic)
    items.sort(key=lambda i: ((i["agency_name"] or "").casefold(), i["tenant_id"]))
    return {"items": items}
