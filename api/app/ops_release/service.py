"""The ops release lifecycle in one agency (AA-956, card CRM-26; design v3 F8.3, F10.3, C12 step 6, Fix B2).

1. **Release when an AA line is confirmed** (:func:`sync_trip`): a trip that is not Lost / cancelled / trashed and has
   at least one confirmed AA line gets one live ``crm.ops_release`` (actor ``job:crm_ops_release_sync``; the crm-034
   guard lets only a job create one). No confirmed AA line, no release (card R: S-02 answers 404).
2. **Payload refresh** (same function, run hourly and on every nudge): the document is rebuilt; when its text
   changed it is sealed again and ``payload_version`` moves up; completeness, dates, the Booking ids and ``purge_at``
   follow the trip. **Dates moved -> purge date recomputed** (card E): ``purge_at`` = the trip's ``end_date`` + 90 days.
3. **T-21 alert** (same function): a live release still incomplete within :data:`T21_DAYS` days of departure gets one
   ``ops_release_incomplete`` task for the trip owner, ``t21_alerted_at`` and the structured log line
   ``crm_ops_release_incomplete_t21`` (AA ops are alerted from that line, like the break-glass alarm of AA-971: the API
   task has no SNS permission), once per release. Like ``crm_ops_release_withdrawn`` (every withdrawal: the job's,
   a member's, a cancel's) it is a :class:`Notice` the caller writes after the commit.
4. **Withdraw** (:func:`withdraw`, C12 step 6 and the agency's own "Withdraw"): a member marks the live release
   withdrawn (AA ops stop seeing the document at once) and nudges the job, which purges it when the trip has not
   started; no confirmed AA line left or the trip cancelled -> the job withdraws it too. A withdrawal for lost AA lines
   is lifted when a new AA line is confirmed (the job re-releases); a member's withdrawal and a cancel stay, also after
   the purge (no new release for that trip). A trip past its window (return + 90 days) is never released again.
5. **Purge with a receipt** (:func:`purge_due`): at ``purge_at`` (or at once for a release withdrawn before departure)
   the document is deleted (``payload_enc`` NULL), the supplier manifest records are deleted, and the row keeps
   ``deletion_receipt`` (counts only) and the audit event ``ops_release_purge`` (the agency's Activity). A held trip
   is skipped (crm-010 hold rule; the guard would refuse with CRM10).

Every function takes the open context; the job (:mod:`app.ops_release.job`) and the routes open it.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from app.errors import Conflict, NotFound
from app.jobs import queue
from app.jobs.kinds import OPS_RELEASE_SYNC
from app.ops_release import build
from app.permissions import Action, may
from app.tenancy import AnyContext, TenantContext
from app.vault import Vault

log = logging.getLogger(__name__)

ACTOR = "job:crm_ops_release_sync"
RETENTION_DAYS = 90
T21_DAYS = 21
ALERT_TASK = "ops_release_incomplete"
MAX_ACTIVITY = 20
CLOSED_STAGES = ("lost", "cancelled")
MEMBER_WITHDRAW_REASONS = ("trip_cancelled", "agency_withdrawn")

TODAY_SQL = "SELECT (now() AT TIME ZONE coalesce((SELECT timezone FROM crm.tenant_settings LIMIT 1), $1))::date"
TRIP_SQL = """
SELECT trip_id, stage, start_date, end_date, party_adults, party_child_ages, deleted_at
  FROM crm.trip WHERE trip_id = $1
"""
LINES_SQL = """
SELECT booking_line_id, title, aa_tour_source_id, departure_id, bk_booking_id, service_from, service_to
  FROM crm.booking_line
 WHERE trip_id = $1 AND supplier_kind = 'aa' AND status = 'confirmed' AND deleted_at IS NULL
 ORDER BY service_from NULLS LAST, booking_line_id
"""
RELEASE_COLUMNS = """
ops_release_id, trip_id, payload_enc, payload_version, fields_released, completeness, complete, traveller_count,
departure_ids, bk_booking_ids, start_date, end_date, released_at, refreshed_at, purge_at, t21_alerted_at,
withdrawn_at, withdraw_reason, purged_at, purge_reason, deletion_receipt, view_count, last_viewed_at
"""
LIVE_SQL = f"SELECT {RELEASE_COLUMNS} FROM crm.ops_release WHERE trip_id = $1 AND purged_at IS NULL FOR UPDATE"  # noqa: S608
LATEST_SQL = f"""
SELECT {RELEASE_COLUMNS} FROM crm.ops_release WHERE trip_id = $1
 ORDER BY purged_at IS NULL DESC, released_at DESC, ops_release_id LIMIT 1
"""  # noqa: S608 — the column list is a constant
INSERT_SQL = """
INSERT INTO crm.ops_release (ops_release_id, trip_id, payload_enc, fields_released, completeness, complete,
                             traveller_count, departure_ids, bk_booking_ids, start_date, end_date, purge_at)
VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9, $10, $11, $12)
"""
REFRESH_SQL = """
UPDATE crm.ops_release
   SET payload_enc = $2, payload_version = $3, refreshed_at = CASE WHEN $3 > payload_version THEN now()
                                                                   ELSE refreshed_at END,
       fields_released = $4, completeness = $5::jsonb, complete = $6, traveller_count = $7, departure_ids = $8,
       bk_booking_ids = $9, start_date = $10, end_date = $11, purge_at = $12, withdrawn_at = NULL,
       withdraw_reason = NULL
 WHERE ops_release_id = $1
"""
ALERTED_SQL = "UPDATE crm.ops_release SET t21_alerted_at = now() WHERE ops_release_id = $1"
WITHDRAW_SQL = """
UPDATE crm.ops_release SET withdrawn_at = now(), withdraw_reason = $2 WHERE ops_release_id = $1
RETURNING withdrawn_at
"""
TASK_SQL = """
INSERT INTO crm.task (trip_id, kind, priority, assignee_member_id, due_anchor, due_at)
SELECT $1, $2, 'urgent', t.owner_member_id, 'ops_handoff', now() FROM crm.trip t WHERE t.trip_id = $1
ON CONFLICT (trip_id, kind) WHERE status = 'open' AND deleted_at IS NULL AND trip_id IS NOT NULL DO NOTHING
"""
DUE_TRIPS_SQL = """
SELECT DISTINCT bl.trip_id FROM crm.booking_line bl
  JOIN crm.trip t ON t.trip_id = bl.trip_id
 WHERE bl.supplier_kind = 'aa' AND bl.status = 'confirmed' AND bl.deleted_at IS NULL
   AND (t.end_date IS NULL OR t.end_date + $1::int >= $2)
UNION
SELECT r.trip_id FROM crm.ops_release r WHERE r.purged_at IS NULL
 LIMIT $3
"""
PURGE_DUE_SQL = """
SELECT r.ops_release_id, r.trip_id, r.withdrawn_at, r.start_date FROM crm.ops_release r
 WHERE r.purged_at IS NULL
   AND (r.purge_at <= $1 OR (r.withdrawn_at IS NOT NULL AND (r.start_date IS NULL OR r.start_date > $1)))
   AND NOT crm.hold_row(r.trip_id, NULL)
 ORDER BY r.purge_at NULLS FIRST, r.ops_release_id
 LIMIT $2
 FOR UPDATE SKIP LOCKED
"""
MANIFEST_DELETE_SQL = "DELETE FROM crm.supplier_manifest_export WHERE ops_release_id = $1"
PURGE_SQL = """
UPDATE crm.ops_release
   SET payload_enc = NULL, purged_at = now(), purge_reason = $2, deletion_receipt = $3::jsonb
 WHERE ops_release_id = $1
"""
ACTIVITY_SQL = """
SELECT at, actor_kind, action, reason FROM crm.audit_event
 WHERE object_kind = 'trip' AND object_id = $1 AND action LIKE 'ops\\_%'
 ORDER BY at DESC, event_id DESC LIMIT $2
"""
BATCH = 200

WITHDRAWN_EVENT = "crm_ops_release_withdrawn"
T21_EVENT = "crm_ops_release_incomplete_t21"


@dataclass(frozen=True)
class Notice:
    """One AA ops alarm line (AA-CIS-Infra ``crm_ops_release_alarms.tf``: a metric filter on the event name), ids and
    counts only. The work queues it and the caller writes it once the transaction that caused it has committed (the
    break-glass pattern of :mod:`app.support.sessions`): a rolled-back withdrawal or T-21 alert never reaches AA ops,
    and a T-21 alert whose ``t21_alerted_at`` was not saved is not sent again every hour."""

    event: str
    level: int
    fields: tuple[tuple[str, Any], ...]

    def emit(self) -> None:
        log.log(self.level, "%s %s", self.event, " ".join(f"{k}={v}" for k, v in self.fields))


Notices = list[Notice]


def emit(notices: Notices) -> None:
    """Write the queued alarm lines (call after the commit), then forget them."""
    for notice in notices:
        notice.emit()
    notices.clear()


def _notify(notices: Notices | None, notice: Notice) -> None:
    if notices is None:
        notice.emit()  # the caller holds no list (a test or a one-off script): written at once
    else:
        notices.append(notice)


def _withdrawn(ctx: AnyContext, release_id: uuid.UUID, reason: str) -> Notice:
    fields = (("tenant_id", ctx.tenant_id), ("ops_release_id", release_id), ("reason", reason))
    return Notice(WITHDRAWN_EVENT, logging.INFO, fields)


@asynccontextmanager
async def item_tx(ctx: AnyContext) -> AsyncIterator[None]:
    """One trip or one purge in a savepoint of the agency's transaction, so one bad item (a refused decrypt, a bad
    stored value, a guard) loses that item only. On an error the context is put back as it was before the item: the
    item's queued audit events go (its ``denied`` ones stay, as :func:`app.tenancy.system_tx` keeps them), and so do
    the DEKs it generated (their ``crm.tenant_key`` rows rolled back with the savepoint: a later item must not seal
    under them)."""
    events = len(ctx.audit_events)
    memo, current = dict(ctx.dek_memo), dict(ctx.dek_current)
    try:
        async with ctx.conn.transaction():
            yield
    except BaseException:
        kept = [e for e in ctx.audit_events[events:] if e.outcome == "denied"]
        del ctx.audit_events[events:]
        ctx.audit_events.extend(kept)
        ctx.dek_memo.clear()
        ctx.dek_memo.update(memo)
        ctx.dek_current.clear()
        ctx.dek_current.update(current)
        raise


def _iso(value: dt.date | dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _rows(status: str) -> int:
    return int(status.split()[-1]) if status and status.split()[-1].isdigit() else 0


def purge_on(end_date: dt.date | None) -> dt.date | None:
    """Return + 90 days; NULL while the return date is unknown (the trip stays Travelling with a task, F10.1)."""
    return end_date + dt.timedelta(days=RETENTION_DAYS) if end_date is not None else None


async def today(ctx: AnyContext) -> dt.date:
    return await ctx.conn.fetchval(TODAY_SQL, build.DEFAULT_TZ)


async def nudge(ctx: AnyContext, trip_id: uuid.UUID, reason: str) -> str | None:
    """Queue one ``crm_ops_release_sync`` run for this trip in the caller's transaction (ids only), so a confirm, a
    cancel or a withdrawal reaches AA ops within a minute, not at the next hourly pass."""
    return await queue.enqueue(
        ctx.conn,
        OPS_RELEASE_SYNC,
        {"tenant_id": str(ctx.tenant_id), "trip_id": str(trip_id)},
        idempotency_key=f"{OPS_RELEASE_SYNC}:{trip_id}:{reason}:{uuid.uuid4().hex[:12]}",
        max_attempts=5,
        created_by=f"nudge:{reason}"[:60],
    )


async def sync_trip(
    ctx: AnyContext, vault: Vault, trip_id: uuid.UUID, on: dt.date, notices: Notices | None = None
) -> str:
    """Create, refresh, alert or withdraw the release of one trip (the job's context). Returns an outcome code. The
    AA ops alarm lines (withdrawn, T-21) go to ``notices``: the job writes them after the agency's commit."""
    trip = await ctx.conn.fetchrow(TRIP_SQL, trip_id)
    release = await ctx.conn.fetchrow(LIVE_SQL, trip_id)
    closed = trip is None or trip["deleted_at"] is not None or trip["stage"] in CLOSED_STAGES
    lines = [] if closed else await ctx.conn.fetch(LINES_SQL, trip_id)
    if closed or not lines:
        if release is not None and release["withdrawn_at"] is None:
            reason = "trip_cancelled" if closed else "aa_lines_cancelled"
            await ctx.conn.fetchval(WITHDRAW_SQL, release["ops_release_id"], reason)
            ctx.audit("ops_release_withdraw", "allowed", "trip", str(trip_id), reason=reason)
            _notify(notices, _withdrawn(ctx, release["ops_release_id"], reason))
            return "withdrawn"
        return "none"
    if (
        release is not None
        and release["withdrawn_at"] is not None
        and release["withdraw_reason"] != ("aa_lines_cancelled")
    ):
        return "withdrawn"  # a cancel or the agency's own withdrawal stays (the purge follows)
    if release is None:
        end = trip["end_date"]
        if end is not None and end + dt.timedelta(days=RETENTION_DAYS) < on:
            return "none"  # past its retention window: never released again
        last = await ctx.conn.fetchrow(LATEST_SQL, trip_id)
        if last is not None and last["withdraw_reason"] in MEMBER_WITHDRAW_REASONS:
            return "withdrawn"  # the agency withdrew this trip's release: the purge does not invite a new one
    built = await build.document(ctx, vault, trip, lines)
    deps = sorted({ln["departure_id"] for ln in lines if ln["departure_id"]})
    bks = sorted({ln["bk_booking_id"] for ln in lines if ln["bk_booking_id"]})
    meta = (
        list(built.fields),
        json.dumps(built.completeness, sort_keys=True),
        built.complete,
        built.traveller_count,
        deps,
        bks,
        trip["start_date"],
        trip["end_date"],
        purge_on(trip["end_date"]),
    )
    outcome: str
    if release is None:
        release_id = uuid.uuid4()
        blob = await vault.encrypt("ops_release", "payload_enc", release_id, built.text, purpose="ops_release")
        await ctx.conn.execute(INSERT_SQL, release_id, trip_id, blob, *meta)
        ctx.audit("ops_release_create", "allowed", "trip", str(trip_id), fields=built.fields,
                  reason="aa_line_confirmed")  # fmt: skip
        outcome = "created"
        version, alerted, withdrawn = 1, None, None
    else:
        release_id = release["ops_release_id"]
        old = await vault.decrypt("ops_release", "payload_enc", release_id, release["payload_enc"],
                                  purpose="ops_release")  # fmt: skip
        relisted = release["withdrawn_at"] is not None
        changed = old != built.text
        version = release["payload_version"] + int(changed or relisted)
        blob = (
            await vault.encrypt("ops_release", "payload_enc", release_id, built.text, purpose="ops_release")
            if changed
            else release["payload_enc"]
        )
        same_meta = (
            list(release["fields_released"]),
            json.dumps(json.loads(release["completeness"]), sort_keys=True)
            if isinstance(release["completeness"], str)
            else json.dumps(release["completeness"], sort_keys=True),
            release["complete"],
            release["traveller_count"],
            list(release["departure_ids"]),
            list(release["bk_booking_ids"]),
            release["start_date"],
            release["end_date"],
            release["purge_at"],
        ) == meta
        if changed or relisted or not same_meta:
            await ctx.conn.execute(REFRESH_SQL, release_id, blob, version, *meta)
        if relisted:
            ctx.audit("ops_release_create", "allowed", "trip", str(trip_id), fields=built.fields,
                      reason="aa_line_confirmed")  # fmt: skip
            outcome = "relisted"
        elif changed:
            ctx.audit("ops_release_refresh", "allowed", "trip", str(trip_id), fields=built.fields,
                      reason="payload_changed")  # fmt: skip
            outcome = "refreshed"
        else:
            outcome = "updated" if not same_meta else "unchanged"
        alerted, withdrawn = release["t21_alerted_at"], None
    if outcome in ("created", "relisted", "refreshed"):
        log.info("crm_ops_release_%s tenant_id=%s ops_release_id=%s version=%s complete=%s", outcome, ctx.tenant_id,
                 release_id, version, built.complete)  # fmt: skip
    start = trip["start_date"]
    if (
        alerted is None
        and withdrawn is None
        and not built.complete
        and start is not None
        and on <= start <= on + dt.timedelta(days=T21_DAYS)
    ):
        await ctx.conn.execute(ALERTED_SQL, release_id)
        await ctx.conn.execute(TASK_SQL, trip_id, ALERT_TASK)
        ctx.audit("ops_release_t21_alert", "allowed", "trip", str(trip_id), reason="incomplete_t21")
        # AA ops are alerted from this line (a log metric filter, like crm_breakglass_opened); ids and counts only.
        fields = (
            ("tenant_id", ctx.tenant_id),
            ("ops_release_id", release_id),
            ("days_left", (start - on).days),
            ("travellers", built.traveller_count),
            ("complete", built.completeness["complete"]),
        )
        _notify(notices, Notice(T21_EVENT, logging.WARNING, fields))
        outcome += "+alerted"
    return outcome


def opened_fields_event(ctx: AnyContext, trip_ids: list[str]) -> None:
    """One audit event per run and agency for the F1 fields the builds opened (design §5.7 "volume control")."""
    if trip_ids:
        ctx.audit("ops_release_build", "allowed", "trip", None, fields=build.OPENED_FIELDS,
                  object_ids=tuple(trip_ids[:200]), reason="ops_release")  # fmt: skip


async def due_trips(ctx: AnyContext, on: dt.date) -> list[uuid.UUID]:
    """Every trip the hourly pass looks at: a confirmed AA line (not past its purge window), or a live release."""
    return [r["trip_id"] for r in await ctx.conn.fetch(DUE_TRIPS_SQL, RETENTION_DAYS, on, BATCH)]


async def purge_due(ctx: AnyContext, on: dt.date) -> int:
    """Purge every release due (``purge_at`` passed, or withdrawn before departure) that no legal hold covers, each in
    its own savepoint (:func:`item_tx`): a failed purge is logged by id and tried again on the next pass."""
    purged = 0
    for r in await ctx.conn.fetch(PURGE_DUE_SQL, on, BATCH):
        reason = "retention" if r["withdrawn_at"] is None else "withdrawn"
        try:
            async with item_tx(ctx):
                await purge(ctx, r["ops_release_id"], r["trip_id"], reason)
        except Exception as exc:  # noqa: BLE001 — one release's error must not keep every later one
            log.error("ops_release_purge_failed tenant_id=%s ops_release_id=%s error_class=%s", ctx.tenant_id,
                      r["ops_release_id"], type(exc).__name__)  # fmt: skip
            continue
        purged += 1
    return purged


async def purge(ctx: AnyContext, release_id: uuid.UUID, trip_id: uuid.UUID, reason: str) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(
        "SELECT payload_version, traveller_count, view_count FROM crm.ops_release WHERE ops_release_id = $1",
        release_id,
    )
    manifests = _rows(await ctx.conn.execute(MANIFEST_DELETE_SQL, release_id))
    receipt = {
        "payload_version": row["payload_version"],
        "travellers": row["traveller_count"],
        "manifest_exports": manifests,
        "views": row["view_count"],
        "reason": reason,
    }
    await ctx.conn.execute(PURGE_SQL, release_id, reason, json.dumps(receipt, sort_keys=True))
    ctx.audit("ops_release_purge", "allowed", "trip", str(trip_id), reason=reason)
    log.info("crm_ops_release_purged tenant_id=%s ops_release_id=%s reason=%s manifests=%s", ctx.tenant_id,
             release_id, reason, manifests)  # fmt: skip
    return receipt


# ── The agency's side (/crm/v1) ──────────────────────────────────────────────────────────────────────


async def withdraw(
    ctx: TenantContext, trip_id: uuid.UUID, reason: str, *, notices: Notices | None = None
) -> dict[str, Any]:
    """C12 step 6 (cancel trip) and the agency's own "Withdraw from AA ops": the live release is withdrawn now (AA ops
    stop seeing the document), and the job is nudged to purge it when the trip has not started. 404
    ``release_not_found`` without a live release, 409 ``release_withdrawn`` when it already is. The AA ops notice
    (``crm_ops_release_withdrawn``, the Infra alarm) goes to ``notices``: the route writes it after the commit."""
    if reason not in MEMBER_WITHDRAW_REASONS:
        raise ValueError(f"not a member withdraw reason: {reason!r}")
    release = await ctx.conn.fetchrow(LIVE_SQL, trip_id)
    if release is None:
        raise NotFound("release_not_found", "this trip has no ops release")
    if release["withdrawn_at"] is not None:
        raise Conflict("release_withdrawn", "the ops release is already withdrawn")
    await ctx.conn.fetchval(WITHDRAW_SQL, release["ops_release_id"], reason)
    ctx.audit("ops_release_withdraw", "allowed", "trip", str(trip_id), reason=reason)
    _notify(notices, _withdrawn(ctx, release["ops_release_id"], reason))
    await nudge(ctx, trip_id, "withdraw")
    return await agency_view(ctx, trip_id)


async def live_count(ctx: AnyContext, trip_id: uuid.UUID) -> int:
    """1 when the trip has a live (not withdrawn) release: the C12 dry run lists it as an effect."""
    release = await ctx.conn.fetchrow(LIVE_SQL, trip_id)
    return int(release is not None and release["withdrawn_at"] is None)


async def withdraw_on_cancel(ctx: TenantContext, trip_id: uuid.UUID, *, notices: Notices | None = None) -> int:
    """C12 step 6, inside the cancel run (:func:`app.stages.cancel`): withdraw the live release, if any (0 / 1)."""
    if not await live_count(ctx, trip_id):
        return 0
    await withdraw(ctx, trip_id, "trip_cancelled", notices=notices)
    return 1


async def agency_view(ctx: TenantContext, trip_id: uuid.UUID) -> dict[str, Any]:
    """The trip workspace panel "AA ground operations": whether AA ops have the trip's details, how complete they
    are, when they were last refreshed and viewed, the purge countdown or the receipt. Never the document itself (the
    agency has the source data). ``activity`` (the ops lines of the trip's audit: created, refreshed, viewed, manifest,
    alert, withdrawn, purged; the newest :data:`MAX_ACTIVITY`, ``activity_more`` when older ones exist) for owner /
    agent (A-03 Activity, ``TRIP_ACTIVITY_VIEW``)."""
    trip = await ctx.conn.fetchval("SELECT 1 FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL", trip_id)
    if trip is None:
        raise NotFound("trip_not_found", "no trip with this id")
    r = await ctx.conn.fetchrow(LATEST_SQL, trip_id)
    on = await today(ctx)
    lines = await ctx.conn.fetchval(
        "SELECT count(*) FROM crm.booking_line WHERE trip_id = $1 AND supplier_kind = 'aa' AND status = 'confirmed'"
        " AND deleted_at IS NULL",
        trip_id,
    )
    view: dict[str, Any] = {"trip_id": str(trip_id), "confirmed_aa_lines": int(lines), "release": None}
    if r is not None:
        status = "purged" if r["purged_at"] else "withdrawn" if r["withdrawn_at"] else "live"
        completeness = r["completeness"]
        if isinstance(completeness, str):
            completeness = json.loads(completeness)
        receipt = r["deletion_receipt"]
        if isinstance(receipt, str):
            receipt = json.loads(receipt)
        view["release"] = {
            "ops_release_id": str(r["ops_release_id"]),
            "status": status,
            "payload_version": r["payload_version"],
            "fields_released": list(r["fields_released"]),
            "completeness": completeness,
            "complete": r["complete"],
            "traveller_count": r["traveller_count"],
            "released_at": _iso(r["released_at"]),
            "refreshed_at": _iso(r["refreshed_at"]),
            "purge_at": _iso(r["purge_at"]),
            "days_to_purge": (r["purge_at"] - on).days if r["purge_at"] and not r["purged_at"] else None,
            "t21_alerted_at": _iso(r["t21_alerted_at"]),
            "withdrawn_at": _iso(r["withdrawn_at"]),
            "withdraw_reason": r["withdraw_reason"],
            "purged_at": _iso(r["purged_at"]),
            "purge_reason": r["purge_reason"],
            "deletion_receipt": receipt,
            "view_count": r["view_count"],
            "last_viewed_at": _iso(r["last_viewed_at"]),
            "can_withdraw": status == "live" and may(ctx.member_role, Action.TRIP_CANCEL),
        }
    view["activity"] = None
    view["activity_more"] = False
    if may(ctx.member_role, Action.TRIP_ACTIVITY_VIEW):
        # One line past the cap tells the panel there is more (S262 AA-956: a list cut without saying so read as a
        # receipt that over-counts); the whole history stays in Settings > Access log.
        rows = await ctx.conn.fetch(ACTIVITY_SQL, str(trip_id), MAX_ACTIVITY + 1)
        view["activity"] = [
            {"at": _iso(a["at"]), "actor_kind": a["actor_kind"], "action": a["action"], "reason": a["reason"]}
            for a in rows[:MAX_ACTIVITY]
        ]
        view["activity_more"] = len(rows) > MAX_ACTIVITY
    return view
