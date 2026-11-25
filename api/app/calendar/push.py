"""``crm_calendar_push`` (AA-1011 part 3): the one-way push of a member's departures and tasks into their own Google /
Microsoft 365 calendar (crm-089 ``crm.calendar_connection``; the provider APIs in :mod:`app.calendar.oauth_sync`).

**What is pushed**: the ``departure`` and ``task`` events of :func:`app.calendar.events.events` (the same source as the
in-app calendar and the ICS feed), the member's own (scope ``mine``) or the agency's (scope ``agency``), from
``crm.calendar_push.past_days`` before today to ``crm.calendar_push.window_days`` after it (the agency's timezone).
Each event is written like the ICS feed writes it (:func:`app.calendar.ics.summary`: a fixed label and the short trip
reference, a link back to the trip; no client name, no trip title, no amount).

**How (idempotent by external event id)**: one run lists the CRM's own events of the connection in the window (each
carries its CRM key and a digest of what was written, :class:`app.calendar.oauth_sync.RemoteEvent`), then creates the
missing ones, updates the changed ones and deletes the ones the CRM no longer has (a cancelled trip, a done task, a
moved date), at most ``crm.calendar_push.batch_size`` writes a run; the rest waits for the next run. Nothing about the
events is stored in the CRM: a run that stopped half way is finished by the next one. Events outside the window are
left as they are.

**Disconnect** (status ``revoked``, :func:`app.calendar.connections.disconnect`), or a member who is no longer active:
the run deletes every CRM event of the connection (any date), then deletes the credential (the connection row goes
with it, crm-089 cascade). It never revokes the grant at the provider: the calendar shares the mailbox's OAuth client,
and a Google revoke cancels the whole grant of that account and client, the member's Gmail mailbox included
(:mod:`app.calendar.oauth_sync`). A provider outage retries the run up to
``crm.calendar_push.max_attempts``; the last attempt (and a refused token) deletes the credential anyway, so a
disconnect never leaves a token behind (events left in the calendar are counted).

**When**: once per hour slot for every connection of every agency (due-now hook :class:`HourlyCalendarPush`, the next
slot is the retry), and at once for one connection after a connect or a disconnect (:func:`enqueue_connection`).

A refused token (``invalid_grant``, 401) or a missing permission leaves the connection active with its
``failure_class`` (the card asks for a reconnect); a good run clears it. Results and logs carry ids, codes and counts
only: never a token, an address or an event text.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import uuid
from collections import Counter
from collections.abc import Callable
from typing import Any

import asyncpg

from app import platform_settings as ps
from app.audit_export import registry
from app.calendar import events as calendar_events
from app.calendar import ics, oauth_sync
from app.errors import Forbidden, NotFound
from app.jobs import queue
from app.jobs.kinds import CALENDAR_PUSH
from app.jobs.queue import Job
from app.mail.providers import AuthRevoked, ProviderError, Transient
from app.tenancy import SystemContext, system_tx
from app.vault import KeyProvider, Vault

log = logging.getLogger(__name__)

ACTOR = "job:crm_calendar_push"
SCHEDULER = "scheduler:crm_calendar_push"
SLOT_SECONDS = 3600  # technical-constant: the hourly sweep (the connect / disconnect nudges run at once)
SWEEP_ATTEMPTS = 1  # technical-constant: a sweep row is not retried, the next slot is its retry
HISTORY_DAYS = 7  # technical-constant: finished rows of this kind kept for the job page
PUSH_KINDS = ("departure", "task")
MODES = ("connect", "disconnect")

CONNECTION_SQL = """
SELECT c.calendar_connection_id, c.member_id, c.provider, c.scope, c.status, c.credential_id, ic.secret_enc,
       (m.status = 'active' AND m.deleted_at IS NULL) AS member_active
  FROM crm.calendar_connection c
  JOIN crm.integration_credential ic ON ic.integration_credential_id = c.credential_id
  JOIN crm.member m ON m.member_id = c.member_id
 WHERE c.calendar_connection_id = $1
"""
LIST_SQL = "SELECT calendar_connection_id FROM crm.calendar_connection ORDER BY created_at, calendar_connection_id"
ROTATE_SQL = """
UPDATE crm.integration_credential SET secret_enc = $2, last_ok_at = now() WHERE integration_credential_id = $1
"""
RESULT_SQL = """
UPDATE crm.calendar_connection
   SET last_sync_at = now(), last_ok_at = CASE WHEN $2::text IS NULL THEN now() ELSE last_ok_at END,
       failure_class = $2, event_count = coalesce($3, event_count)
 WHERE calendar_connection_id = $1 AND status = 'active'
"""
DELETE_CREDENTIAL_SQL = "DELETE FROM crm.integration_credential WHERE integration_credential_id = $1"
FAILURE_CLASSES = frozenset({"auth_revoked", "permission_missing", "vendor_rejected", "rate_limited", "unreachable"})


@dataclasses.dataclass(frozen=True)
class Limits:
    window_days: int
    past_days: int
    batch_size: int

    @classmethod
    def read(cls) -> Limits:
        return cls(
            window_days=int(ps.get(ps.CALENDAR_PUSH_WINDOW_DAYS)),
            past_days=int(ps.get(ps.CALENDAR_PUSH_PAST_DAYS)),
            batch_size=int(ps.get(ps.CALENDAR_PUSH_BATCH_SIZE)),
        )


@dataclasses.dataclass
class Plan:
    creates: list[oauth_sync.PushEvent]
    updates: list[tuple[str, oauth_sync.PushEvent]]
    deletes: list[str]

    @property
    def size(self) -> int:
        return len(self.creates) + len(self.updates) + len(self.deletes)


def plan(desired: list[oauth_sync.PushEvent], remote: list[oauth_sync.RemoteEvent]) -> Plan:
    """What makes the calendar match ``desired``: creates, updates (the digest changed), deletes (not desired any more,
    or a second copy of one key)."""
    wanted = {e.key: e for e in desired}
    seen: set[str] = set()
    out = Plan(creates=[], updates=[], deletes=[])
    for r in remote:
        if r.key not in wanted or r.key in seen:
            out.deletes.append(r.event_id)
            continue
        seen.add(r.key)
        if r.digest != wanted[r.key].digest():
            out.updates.append((r.event_id, wanted[r.key]))
    out.creates = [e for k, e in wanted.items() if k not in seen]
    return out


def to_push(event: dict[str, Any], tz: dt.tzinfo, web_origin: str) -> oauth_sync.PushEvent:
    trip = event.get("trip_id")
    if trip and web_origin:
        description = f"Open the trip in the CRM: {web_origin}/pipeline/{trip}"
    elif web_origin:
        description = f"Open your tasks in the CRM: {web_origin}/tasks"
    else:
        description = "From your partner CRM"
    starts = event.get("starts_at")
    return oauth_sync.PushEvent(
        key=str(event["id"]),
        summary=ics.summary(event),
        description=description,
        all_day=bool(event.get("all_day", True)),
        day=event["date"],
        starts_at=starts,
        ends_at=(starts + dt.timedelta(minutes=ics.TASK_MINUTES)) if starts is not None else None,
        timezone=str(tz),
    )


async def desired_events(
    ctx: SystemContext, row: Any, limits: Limits, now: dt.datetime, web_origin: str
) -> tuple[list[oauth_sync.PushEvent], dt.datetime, dt.datetime]:
    """The connection's events in its window, and the window as times (for the provider's list)."""
    tz = calendar_events.zone(await ctx.conn.fetchval(calendar_events.TZ_SQL, calendar_events.DEFAULT_TZ))
    today = now.astimezone(tz).date()
    first, last = today - dt.timedelta(days=limits.past_days), today + dt.timedelta(days=limits.window_days)
    member = row["member_id"] if row["scope"] == "mine" else None
    items = await calendar_events.events(ctx, start=first, end=last, member_id=member, tz=tz)
    pushed = [to_push(e, tz, web_origin) for e in items if e["kind"] in PUSH_KINDS]
    since = dt.datetime.combine(first, dt.time.min, tz)
    until = dt.datetime.combine(last + dt.timedelta(days=1), dt.time.min, tz)
    return pushed, since, until


@dataclasses.dataclass
class Runtime:
    keys: KeyProvider | None
    clients: dict[str, Any]
    web_origin: str = ""


@dataclasses.dataclass
class _Loaded:
    row: Any
    secret: dict[str, Any]
    remove: bool
    desired: list[oauth_sync.PushEvent]
    since: dt.datetime | None
    until: dt.datetime | None
    rotated: dict[str, Any] | None = None


async def _load(
    pool: asyncpg.Pool, runtime: Runtime, tenant_id: uuid.UUID, connection_id: uuid.UUID, now: dt.datetime
) -> _Loaded | None:
    async with system_tx(pool, tenant_id, actor=ACTOR) as ctx:
        row = await ctx.conn.fetchrow(CONNECTION_SQL, connection_id)
        if row is None:
            return None
        vault = Vault(runtime.keys, ctx)  # type: ignore[arg-type]
        secret = await oauth_sync.open_secret(vault, row["credential_id"], row["secret_enc"])
        remove = row["status"] != "active" or not row["member_active"]
        if remove:
            return _Loaded(row, secret, True, [], None, None)
        desired, since, until = await desired_events(ctx, row, Limits.read(), now, runtime.web_origin)
        return _Loaded(row, secret, False, desired, since, until)


async def _apply(client: Any, token: str, connection_id: uuid.UUID, todo: Plan, budget: int) -> Counter[str]:
    done: Counter[str] = Counter()
    for event in todo.creates:
        if sum(done.values()) >= budget:
            return done
        await client.create(token, connection_id, event)
        done["created"] += 1
    for external, event in todo.updates:
        if sum(done.values()) >= budget:
            return done
        await client.update(token, connection_id, external, event)
        done["updated"] += 1
    for external in todo.deletes:
        if sum(done.values()) >= budget:
            return done
        await client.delete(token, external)
        done["deleted"] += 1
    return done


async def _finish(
    pool: asyncpg.Pool,
    runtime: Runtime,
    tenant_id: uuid.UUID,
    loaded: _Loaded,
    *,
    failure: str | None,
    count: int | None,
) -> None:
    async with system_tx(pool, tenant_id, actor=ACTOR) as ctx:
        credential = loaded.row["credential_id"]
        if loaded.remove:
            await ctx.conn.execute(DELETE_CREDENTIAL_SQL, credential)  # the connection goes with it (crm-089)
            ctx.audit("calendar_disconnect", "allowed", "calendar_connection",
                      str(loaded.row["calendar_connection_id"]), reason="credential_deleted")  # fmt: skip
            return
        await _rotate(ctx, runtime, loaded)
        await ctx.conn.execute(RESULT_SQL, loaded.row["calendar_connection_id"], failure, count)


async def _rotate(ctx: SystemContext, runtime: Runtime, loaded: _Loaded) -> None:
    """Store the refresh token the provider rotated (Microsoft on every refresh, Google sometimes)."""
    if loaded.rotated is not None:
        vault = Vault(runtime.keys, ctx)  # type: ignore[arg-type]
        sealed = await oauth_sync.seal_secret(vault, loaded.row["credential_id"], loaded.rotated)
        await ctx.conn.execute(ROTATE_SQL, loaded.row["credential_id"], sealed)


def _counts(done: Counter[str]) -> dict[str, int]:
    return {f"{k}_count": v for k, v in done.items()}


async def _push(
    pool: asyncpg.Pool, runtime: Runtime, tenant_id: uuid.UUID, loaded: _Loaded, client: Any
) -> dict[str, Any]:
    connection_id = loaded.row["calendar_connection_id"]
    token, loaded.rotated = await client.access_token(loaded.secret)
    remote = await client.list_ours(token, connection_id, loaded.since, loaded.until)
    todo = plan(loaded.desired, remote)
    # A removal deletes every listed event in one run (the token goes at its end); a push stays within the batch.
    budget = todo.size if loaded.remove else Limits.read().batch_size
    done = await _apply(client, token, connection_id, todo, budget)
    left = todo.size - sum(done.values())
    if loaded.remove:
        await _finish(pool, runtime, tenant_id, loaded, failure=None, count=None)
        return {"outcome": "removed", "left_count": left, **_counts(done)}
    count = max(len(remote) + done["created"] - done["deleted"], 0)
    await _finish(pool, runtime, tenant_id, loaded, failure=None, count=count)
    return {"outcome": "pushed", "left_count": left, **_counts(done)}


async def _failed(
    pool: asyncpg.Pool, runtime: Runtime, tenant_id: uuid.UUID, loaded: _Loaded, exc: ProviderError, final: bool
) -> dict[str, Any]:
    transient = isinstance(exc, Transient)
    code = exc.code if exc.code in FAILURE_CLASSES else ("unreachable" if transient else "vendor_rejected")
    if loaded.remove:
        if transient and not final:
            async with system_tx(pool, tenant_id, actor=ACTOR) as ctx:
                await _rotate(ctx, runtime, loaded)
            raise exc  # retried; the last attempt deletes the token whatever happens
        await _finish(pool, runtime, tenant_id, loaded, failure=None, count=None)
        return {"outcome": "removed_with_error", "reason_code": code}
    await _finish(pool, runtime, tenant_id, loaded, failure=code, count=None)
    return {"outcome": "auth_failed" if isinstance(exc, AuthRevoked) else "failed", "reason_code": code}


async def sync_connection(
    pool: asyncpg.Pool,
    runtime: Runtime,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    *,
    now: dt.datetime,
    final_attempt: bool = True,
) -> dict[str, Any]:
    """One connection: push (or remove), then record the result. Returns ``{"outcome", <counts>}``. Raises
    :class:`Transient` only when the run should be retried (a removal before its last attempt)."""
    loaded = await _load(pool, runtime, tenant_id, connection_id, now)
    if loaded is None:
        return {"outcome": "gone"}
    client = runtime.clients.get(loaded.row["provider"])
    if client is None or not client.configured:
        if loaded.remove:  # nothing can reach the calendar: the token goes at once
            await _finish(pool, runtime, tenant_id, loaded, failure=None, count=None)
            return {"outcome": "removed_not_configured"}
        return {"outcome": "not_configured"}
    try:
        return await _push(pool, runtime, tenant_id, loaded, client)
    except ProviderError as exc:
        return await _failed(pool, runtime, tenant_id, loaded, exc, final_attempt)


async def _connections(pool: asyncpg.Pool, tenant_id: uuid.UUID) -> list[uuid.UUID]:
    async with system_tx(pool, tenant_id, actor=ACTOR) as ctx:
        return [r["calendar_connection_id"] for r in await ctx.conn.fetch(LIST_SQL)]


class CalendarPush:
    """The ``crm_calendar_push`` handler. A row naming one connection (``tenant_id`` + ``calendar_connection_id``,
    a connect or a disconnect) runs it; a slot row runs every connection of every agency."""

    def __init__(self, runtime: Runtime, now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC)) -> None:
        self._runtime = runtime
        self._now = now

    async def __call__(self, pool: asyncpg.Pool, job: Job) -> dict[str, Any]:
        if self._runtime.keys is None:
            log.info("calendar_push not_configured")
            return {"status": "not_configured"}
        payload = job.payload or {}
        if payload.get("calendar_connection_id") and payload.get("tenant_id"):
            return await self._one(pool, job, uuid.UUID(payload["tenant_id"]),
                                   uuid.UUID(payload["calendar_connection_id"]))  # fmt: skip
        return await self._sweep(pool)

    async def _one(
        self, pool: asyncpg.Pool, job: Job, tenant_id: uuid.UUID, connection_id: uuid.UUID
    ) -> dict[str, Any]:
        try:
            result = await sync_connection(pool, self._runtime, tenant_id, connection_id, now=self._now(),
                                           final_attempt=job.attempt >= job.max_attempts)  # fmt: skip
        except (Forbidden, NotFound):  # the agency is locked or gone: nothing to do for it now
            return {"status": "ok", "outcome": "skipped_agency"}
        log.info("calendar_push_one connection_id=%s outcome=%s", connection_id, result.get("outcome"))
        return {"status": "ok", **result}

    async def _sweep(self, pool: asyncpg.Pool) -> dict[str, Any]:
        counts: Counter[str] = Counter()
        errors: Counter[str] = Counter()
        for agency in await registry.agencies(pool):
            try:
                ids = await _connections(pool, agency.tenant_id)
            except (Forbidden, NotFound):
                continue
            for connection_id in ids:
                try:
                    result = await sync_connection(
                        pool, self._runtime, agency.tenant_id, connection_id, now=self._now()
                    )
                    counts[str(result.get("outcome"))] += 1
                except (Forbidden, NotFound):
                    counts["skipped_agency"] += 1
                except Exception as exc:  # noqa: BLE001 — one connection's error must not hold every later one
                    errors[type(exc).__name__] += 1
                    log.error("calendar_push_failed connection_id=%s error_class=%s", connection_id, type(exc).__name__)
        log.info("calendar_push_summary %s errors=%d", " ".join(f"{k}={v}" for k, v in sorted(counts.items())),
                 sum(errors.values()))  # fmt: skip
        return {
            "status": "ok" if not errors else "partial",
            **{f"{k}_count": v for k, v in sorted(counts.items())},
            "error_count": sum(errors.values()),
            "reason_code": errors.most_common(1)[0][0][:64] if errors else None,
        }


def build_handler(runtime: Runtime) -> CalendarPush:
    return CalendarPush(runtime)


async def enqueue_connection(conn: Any, tenant_id: uuid.UUID, connection_id: uuid.UUID, mode: str) -> None:
    """One run of one connection now (a connect or a disconnect), in the caller's transaction."""
    if mode not in MODES:
        raise ValueError(mode)
    stamp = int(dt.datetime.now(dt.UTC).timestamp())
    await queue.enqueue(conn, CALENDAR_PUSH, {"tenant_id": str(tenant_id), "calendar_connection_id": str(connection_id),
                                              "mode": mode},
                        idempotency_key=f"{CALENDAR_PUSH}:{connection_id}:{mode}:{stamp}",
                        max_attempts=int(ps.get(ps.CALENDAR_PUSH_MAX_ATTEMPTS)), created_by=SCHEDULER)  # fmt: skip


def schedule_key(now: dt.datetime) -> str:
    return f"{CALENDAR_PUSH}:slot:{int(now.timestamp()) // SLOT_SECONDS}"


class HourlyCalendarPush:
    """Worker maintenance hook: one ``crm_calendar_push`` sweep per hour slot, due now, never ahead."""

    def __init__(self, now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC)) -> None:
        self._now = now
        self._done_slot: str | None = None

    async def __call__(self, pool: asyncpg.Pool) -> str | None:
        key = schedule_key(self._now())
        if self._done_slot == key:
            return None
        job_id = await queue.enqueue(pool, CALENDAR_PUSH, {"purpose": "slot_schedule"}, idempotency_key=key,
                                     max_attempts=SWEEP_ATTEMPTS, created_by=SCHEDULER)  # fmt: skip
        await queue.prune(pool, CALENDAR_PUSH, HISTORY_DAYS)
        self._done_slot = key
        return job_id
