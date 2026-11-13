"""An AA tour Adventure Asia no longer serves (AA-1093 part 1, S278 audit gap M29; crm-063 ``crm.tour_retired_flag``).

How the CRM learns it (root plan D9: no new CIS feed): the hourly ``crm_catalog_sync`` stores a CIS 410 as
``crm.aa_catalog_tour.served_state = 'retired'``. :func:`app.catalog.store.upsert` and
:func:`app.catalog.store.set_state` see the transition the write made (:func:`transition`) and queue, in the same
transaction, one ``crm_tour_retired_fanout`` row per transition (:func:`fanout_job`: payload ids and codes only,
idempotency key = tour + transition + ``state_changed_at``). ``paused`` alone does nothing (root ADR 0015 decision 5):
served <-> paused queues nothing, paused -> retired counts as a retirement.

The job (:func:`build_handler`), for every agency in its own :func:`app.tenancy.system_tx` (actor
``job:crm_tour_retired_fanout``; a locked, offboarding or gone agency is skipped, one agency's error never stops the
others):

- **retired**: every live trip (not lost, cancelled or completed, no cancellation) that uses the tour gets one flag per
  item (:data:`ITEMS_SQL`): ``open_option`` (an AA option in a draft, published or sent builder version),
  ``held_line`` (an AA line on hold or with a request in flight), ``quoted_line`` (an AA line quoted, not held),
  ``confirmed_line`` (a booked AA line: information only). The same transition run twice raises nothing new (crm-063
  unique keys); each trip that got a new flag gets one task ``aa_tour_discontinued`` (root plan D10: the trip owner
  while an active member, else the agency's first active owner; urgent unless only booked lines are hit). The job
  **never** writes a booking line, an option or a proposal: Booking keeps every booking and hold, the agent decides.
- **reinstated**: the tour's open flags are cleared; a trip whose last open flag this cleared and that still has the
  open task gets an internal note saying so (in the agency's language), the task stays for the agent to close.

A job that finds the tour in another state than its transition says (a retirement already reversed, a reinstatement
retired again) does nothing (``status: stale``): the later transition has its own row.

The reads (:func:`flagged`, :func:`trip_view`) feed the pipeline badge and filter ``issue=aa_tour_retired`` and the trip
page card. Every function on a context takes the open context and never opens one; only the handler opens
``system_tx`` (this module is on the job-module list of ``tests/unit/test_code_rules.py``).
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import uuid
from collections import Counter
from typing import Any

import asyncpg

from app.audit_export import registry
from app.errors import Forbidden, NotFound, ServiceError
from app.jobs.kinds import TOUR_RETIRED_FANOUT
from app.jobs.queue import Job
from app.jobs.worker import Handler, PermanentError
from app.tenancy import AnyContext, SystemContext, system_tx
from app.vault import DecryptionDenied, KeyProvider, Vault

log = logging.getLogger(__name__)

KIND = TOUR_RETIRED_FANOUT
ACTOR = f"job:{KIND}"
QUEUED_BY = "job:crm_catalog_sync"
MAX_ATTEMPTS = 3
TASK = "aa_tour_discontinued"
RETIRED = "retired"
REINSTATED = "reinstated"
TRANSITIONS = (RETIRED, REINSTATED)
KINDS = ("open_option", "held_line", "quoted_line", "confirmed_line")
CLOSED_STAGES = ("lost", "cancelled", "completed")
OPEN_VERSIONS = ("draft", "published", "sent")
MAX_ITEMS = 2000  # items of one agency on one tour (a tour sells far fewer in an agency)
_AUDIT_ID = re.compile(r"^[A-Za-z0-9:_-]{1,80}$")
_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)

NOTE_TEXT = {
    "en": 'Adventure Asia offers the AA tour "{title}" again. Its "AA tour discontinued" flags on this trip are '
    "cleared; close the task when you have checked the trip.",
    "vi": 'Adventure Asia đã mở bán lại tour AA "{title}". Các cờ "Tour AA ngừng bán" của chuyến này đã được gỡ; '
    "hãy đóng việc cần làm sau khi kiểm tra lại chuyến.",
}

# What one agency has on the tour: the AA options of live builder versions and the AA booking lines still alive (a
# cancelled line, or one Booking answered sold out / declined / expired / released / not served / failed, is left out:
# nothing stands on it any more). A booked line (confirmed, or booked on request with a Booking id) is confirmed_line.
ITEMS_SQL = """
WITH live AS (
  SELECT t.trip_id FROM crm.trip t
   WHERE t.deleted_at IS NULL AND t.stage <> ALL($2::text[])
     AND NOT EXISTS (SELECT 1 FROM crm.trip_cancellation x WHERE x.trip_id = t.trip_id)
)
SELECT o.trip_id, 'open_option' AS kind, o.option_id AS item_id
  FROM crm.option o
  JOIN live ON live.trip_id = o.trip_id
  JOIN crm.proposal p ON p.proposal_id = o.proposal_id AND p.deleted_at IS NULL AND p.status = ANY($3::text[])
 WHERE o.source = 'aa' AND o.aa_tour_source_id = $1 AND o.deleted_at IS NULL
UNION ALL
SELECT l.trip_id,
       CASE WHEN l.status = 'confirmed' OR l.bk_booking_id IS NOT NULL THEN 'confirmed_line'
            WHEN l.aa_sub_status IN ('held', 'pending') THEN 'held_line'
            ELSE 'quoted_line' END,
       l.booking_line_id
  FROM crm.booking_line l
  JOIN live ON live.trip_id = l.trip_id
 WHERE l.supplier_kind = 'aa' AND l.aa_tour_source_id = $1 AND l.deleted_at IS NULL AND l.status <> 'cancelled'
   AND (l.status = 'confirmed' OR l.bk_booking_id IS NOT NULL OR l.aa_sub_status IS NULL
        OR l.aa_sub_status IN ('held', 'pending', 'quoted'))
 ORDER BY 1, 3
 LIMIT $4
"""
FLAG_SQL = """
INSERT INTO crm.tour_retired_flag (trip_id, source_id, kind, option_id, booking_line_id, raised_key)
VALUES ($1, $2, $3, $4, $5, $6)
ON CONFLICT DO NOTHING
RETURNING tour_retired_flag_id
"""
# Root plan D10: the trip owner while an active member, else the agency's first active owner.
TASK_SQL = """
INSERT INTO crm.task (trip_id, kind, priority, assignee_member_id, due_anchor, due_at)
SELECT $1, $2, $3,
       COALESCE((SELECT m.member_id FROM crm.member m
                  WHERE m.member_id = t.owner_member_id AND m.status = 'active' AND m.deleted_at IS NULL),
                (SELECT m.member_id FROM crm.member m
                  WHERE m.role = 'owner' AND m.status = 'active' AND m.deleted_at IS NULL
                  ORDER BY m.created_at, m.member_id LIMIT 1)),
       'created', now() + interval '1 day'
  FROM crm.trip t WHERE t.trip_id = $1
ON CONFLICT (trip_id, kind) WHERE status = 'open' AND deleted_at IS NULL AND trip_id IS NOT NULL DO NOTHING
RETURNING task_id
"""
CLEAR_SQL = """
UPDATE crm.tour_retired_flag SET state = 'cleared', cleared_at = now(), cleared_key = $2
 WHERE source_id = $1 AND state = 'open' AND deleted_at IS NULL
RETURNING trip_id
"""
# The trips among $1 that have no open flag left and still have the open task.
NOTE_TRIPS_SQL = """
SELECT t.trip_id FROM crm.trip t
 WHERE t.trip_id = ANY($1::uuid[]) AND t.deleted_at IS NULL
   AND NOT EXISTS (SELECT 1 FROM crm.tour_retired_flag f
                    WHERE f.trip_id = t.trip_id AND f.state = 'open' AND f.deleted_at IS NULL)
   AND EXISTS (SELECT 1 FROM crm.task k
                WHERE k.trip_id = t.trip_id AND k.kind = $2 AND k.status = 'open' AND k.deleted_at IS NULL)
 ORDER BY t.trip_id
"""
NOTE_SQL = "INSERT INTO crm.note (note_id, trip_id, kind, body_enc) VALUES ($1, $2, 'internal', $3)"
TOUR_SQL = """
SELECT title, served_state, state_changed_at FROM crm.aa_catalog_tour WHERE source_system = 'cis' AND source_id = $1
"""
LOCALE_SQL = "SELECT default_locale FROM crm.tenant LIMIT 1"
FLAGGED_SQL = """
SELECT DISTINCT f.trip_id FROM crm.tour_retired_flag f
 WHERE f.trip_id = ANY($1::uuid[]) AND f.state = 'open' AND f.deleted_at IS NULL
"""
VIEW_SQL = """
SELECT f.source_id, c.title, f.kind, min(f.raised_at) AS raised_at, count(*)::int AS n
  FROM crm.tour_retired_flag f
  LEFT JOIN crm.aa_catalog_tour c ON c.source_system = 'cis' AND c.source_id = f.source_id
 WHERE f.trip_id = $1 AND f.state = 'open' AND f.deleted_at IS NULL
 GROUP BY f.source_id, c.title, f.kind
 ORDER BY min(f.raised_at), f.source_id, f.kind
"""


# ── the transition and its job row (pure; app.catalog.store queues it) ──────────────────────────


def transition(old_state: str | None, new_state: str | None) -> str | None:
    """``retired`` (served or paused -> retired), ``reinstated`` (retired -> served) or None. A tour seen for the first
    time (``old_state`` None) has no agency item yet: None."""
    if old_state is None or new_state is None or old_state == new_state:
        return None
    if new_state == "retired":
        return RETIRED
    if old_state == "retired" and new_state == "served":
        return REINSTATED
    return None


def changed_us(at: dt.datetime) -> int:
    """``state_changed_at`` in whole epoch microseconds (exact: the job compares it with the stored value)."""
    return (at - _EPOCH) // dt.timedelta(microseconds=1)


def _keyable(source_id: str) -> bool:
    """A job payload names ids only (UUID or integer, :mod:`app.jobs.payload_guard`); catalog.http.v1 source ids are
    uuid strings (CIS raw_tours.source_group_id)."""
    try:
        uuid.UUID(source_id)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def fanout_job(source_id: str, made: str, changed_at: dt.datetime) -> tuple[dict[str, Any], str] | None:
    """``(payload, idempotency_key)`` of the fan-out row of one transition, or None for a source id a payload cannot
    carry."""
    if made not in TRANSITIONS or not _keyable(source_id):
        return None
    us = changed_us(changed_at)
    return {"source_id": source_id, "purpose": made, "changed_at": us}, f"{KIND}:{made}:{source_id}:{us}"


def raised_key(made: str, us: int) -> str:
    return f"{made}:{us}"


# ── one agency ──────────────────────────────────────────────────────────────────────────────────


def _audit_id(source_id: str) -> str | None:
    return source_id if _AUDIT_ID.match(source_id) else None


async def raise_flags(ctx: AnyContext, source_id: str, us: int) -> Counter[str]:
    """``retired`` in one agency: the flags of every item on the tour (new ones only) and one task per trip that got
    one. Never writes a line, an option or a proposal."""
    counts: Counter[str] = Counter()
    rows = await ctx.conn.fetch(ITEMS_SQL, source_id, list(CLOSED_STAGES), list(OPEN_VERSIONS), MAX_ITEMS)
    if len(rows) >= MAX_ITEMS:
        log.warning("tour_retired_items_capped tenant_id=%s limit=%s", ctx.tenant_id, MAX_ITEMS)
    key = raised_key(RETIRED, us)
    new_kinds: dict[uuid.UUID, set[str]] = {}
    for r in rows:
        option, line = (r["item_id"], None) if r["kind"] == "open_option" else (None, r["item_id"])
        if await ctx.conn.fetchval(FLAG_SQL, r["trip_id"], source_id, r["kind"], option, line, key) is None:
            continue  # this transition flagged it already, or the item has an open flag
        new_kinds.setdefault(r["trip_id"], set()).add(r["kind"])
        counts["flag"] += 1
    for trip_id, kinds in new_kinds.items():
        priority = "normal" if kinds == {"confirmed_line"} else "urgent"
        if await ctx.conn.fetchval(TASK_SQL, trip_id, TASK, priority) is not None:
            counts["task"] += 1
    if new_kinds:
        counts["trip"] += len(new_kinds)
        ctx.audit("aa_sync", "allowed", "aa_tour", _audit_id(source_id), reason="tour_retired_flags",
                  object_ids=tuple(str(t) for t in list(new_kinds)[:200]))  # fmt: skip
    return counts


async def clear_flags(ctx: AnyContext, source_id: str, us: int, vault: Vault | None, title: str) -> Counter[str]:
    """``reinstated`` in one agency: clear the tour's open flags; a trip left without an open flag that still has the
    open task gets an internal note (the task stays for the agent). Without the key provider the note is skipped
    (``note_skipped``), the flags are still cleared."""
    counts: Counter[str] = Counter()
    trips = list(dict.fromkeys(r["trip_id"] for r in await ctx.conn.fetch(CLEAR_SQL, source_id,
                                                                          raised_key(REINSTATED, us))))  # fmt: skip
    if not trips:
        return counts
    counts["cleared_trip"] += len(trips)
    noted: list[uuid.UUID] = []
    for r in await ctx.conn.fetch(NOTE_TRIPS_SQL, trips, TASK):
        if vault is None:
            counts["note_skipped"] += 1
            continue
        locale = await ctx.conn.fetchval(LOCALE_SQL)
        text = NOTE_TEXT.get(str(locale or "en")[:2], NOTE_TEXT["en"]).format(title=title)
        note_id = uuid.uuid4()
        await ctx.conn.execute(NOTE_SQL, note_id, r["trip_id"], await vault.encrypt("note", "body_enc", note_id, text))
        noted.append(r["trip_id"])
        counts["note"] += 1
    ctx.audit("aa_sync", "allowed", "aa_tour", _audit_id(source_id), reason="tour_reinstated_flags",
              object_ids=tuple(str(t) for t in trips[:200]))  # fmt: skip
    return counts


# ── the job ─────────────────────────────────────────────────────────────────────────────────────


def _payload(payload: dict[str, Any]) -> tuple[str, str, int]:
    source_id, made, us = payload.get("source_id"), payload.get("purpose"), payload.get("changed_at")
    if not isinstance(source_id, str) or not _keyable(source_id) or made not in TRANSITIONS:
        raise PermanentError("bad_payload")
    if not isinstance(us, int) or isinstance(us, bool) or us <= 0:
        raise PermanentError("bad_payload")
    return source_id, made, us


def _current(tour: Any, made: str, us: int) -> bool:
    """The tour is still in the state the transition left it in."""
    if tour is None:
        return False
    if made == RETIRED:
        return tour["served_state"] == "retired" and changed_us(tour["state_changed_at"]) == us
    return tour["served_state"] != "retired"


async def _agency(ctx: SystemContext, keys: KeyProvider | None, source_id: str, made: str, us: int) -> Counter[str]:
    tour = await ctx.conn.fetchrow(TOUR_SQL, source_id)
    if not _current(tour, made, us):
        return Counter({"stale": 1})
    if made == RETIRED:
        return await raise_flags(ctx, source_id, us)
    vault = Vault(keys, ctx) if keys is not None else None
    return await clear_flags(ctx, source_id, us, vault, tour["title"])


def build_handler(keys: KeyProvider | None) -> Handler:
    async def crm_tour_retired_fanout(pool: asyncpg.Pool, job: Job) -> dict[str, Any]:
        source_id, made, us = _payload(job.payload)
        totals: Counter[str] = Counter()
        done = skipped = failed = 0
        for agency in await registry.agencies(pool):
            try:
                async with system_tx(pool, agency.tenant_id, actor=ACTOR) as ctx:
                    counts = await _agency(ctx, keys, source_id, made, us)
            except (Forbidden, NotFound):
                skipped += 1  # locked, offboarding or gone
                continue
            except DecryptionDenied:
                failed += 1  # audited by system_tx (the denied event survives the rollback)
                log.error("tour_retired_fanout_failed tenant_id=%s error_class=DecryptionDenied", agency.tenant_id)
                continue
            except ServiceError as exc:
                failed += 1
                log.error("tour_retired_fanout_failed tenant_id=%s code=%s", agency.tenant_id, exc.code)
                continue
            except Exception as exc:  # noqa: BLE001 — one agency's error must not skip every later agency
                failed += 1
                log.error("tour_retired_fanout_failed tenant_id=%s error_class=%s", agency.tenant_id,
                          type(exc).__name__)  # fmt: skip
                continue
            if counts.pop("stale", 0):
                # The tour moved on (the later transition has its own row): nothing to do in any agency.
                return {"status": "stale", "purpose": made}
            done += 1
            totals.update(counts)
        if sum(totals.values()):
            log.info("tour_retired_fanout purpose=%s %s", made, " ".join(f"{k}={v}" for k, v in sorted(totals.items())))
        if failed:
            # The agencies that failed are retried with the job (the others raise nothing new on the retry).
            raise RuntimeError(f"tour_retired_fanout: {failed} agencies failed")
        return {
            "status": "done",
            "purpose": made,
            "agency_count": done,
            "skipped_count": skipped,
            **{f"{k}_count": int(v) for k, v in sorted(totals.items())},
        }

    return crm_tour_retired_fanout


# ── reads (the pipeline badge and filter, the trip page card) ──────────────────────────────────


async def flagged(ctx: AnyContext, trip_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    """The trips among ``trip_ids`` with an open "AA tour discontinued" flag."""
    if not trip_ids:
        return set()
    return {r["trip_id"] for r in await ctx.conn.fetch(FLAGGED_SQL, trip_ids)}


async def trip_view(ctx: AnyContext, trip_id: uuid.UUID) -> list[dict[str, Any]]:
    """The trip page card: one entry per retired tour with open flags (title, since when, items per kind)."""
    tours: dict[str, dict[str, Any]] = {}
    for r in await ctx.conn.fetch(VIEW_SQL, trip_id):
        tour = tours.setdefault(
            r["source_id"],
            {
                "source_id": r["source_id"],
                "title": r["title"],
                "raised_at": r["raised_at"].isoformat(),
                "items": dict.fromkeys(KINDS, 0),
            },  # fmt: skip
        )
        tour["items"][r["kind"]] = r["n"]
    return list(tours.values())


__all__ = [
    "KIND",
    "TASK",
    "build_handler",
    "changed_us",
    "clear_flags",
    "fanout_job",
    "flagged",
    "raise_flags",
    "transition",
    "trip_view",
]
