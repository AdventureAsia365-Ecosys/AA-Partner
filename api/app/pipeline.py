"""The agency's pipeline (A-02 board, A-03 trip, card CRM-08): queries and moves inside ``tenant_tx()``.

Every function takes the open :class:`~app.tenancy.TenantContext` (the DB lint allows ``ctx.conn`` on a context-typed
parameter) and a :class:`~app.vault.Vault` when it shows personal data.

Personal data on these screens (design v3 §4.4, Fix B15, Decision D7):

- the **board** decrypts the lead traveller's ``display_name`` only, and one page load writes **one**
  ``audit_event`` (``list_view``, field ``display_name``, the travellers shown in ``object_ids``, AA-937); no
  e-mail, no phone, no partial digits;
- the **trip** decrypts the party's display names, the notes and the non-identity answers of its submissions, and
  writes one ``view`` event naming those fields;
- the e-mail and phone open only on a click (:func:`app.travellers.reveal.reveal`, AA-937: with a reason code), one
  ``reveal`` event per field.

Stage moves (Slice 1): ``inquiry <-> qualifying``, either -> ``lost`` with a reason (R23), ``lost -> inquiry``. The DB
enforces the edges (``crm.enforce_stage_transition``, crm-005); a refused edge is 409 ``transition_not_allowed``.
Every move writes ``crm.trip_stage_history`` and an audit event. CRM-15 (AA-943) adds the later stages and gates.

Assignment (R8, design §4.6 "Reassign clients and trips: owner"): the owner assigns any active member (or nobody);
an agent or assistant may only take an unassigned trip for themselves.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass
from typing import Any

import asyncpg

from app.audit import MAX_OBJECT_IDS
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.intake import stage1
from app.tenancy import TenantContext
from app.travellers import age_bands
from app.vault import Vault

BOARD_STAGES = ("inquiry", "qualifying", "proposal", "booked", "pre_departure", "travelling")
CLOSED_STAGES = ("completed", "lost", "cancelled")
WAITING_ON = ("agency", "client", "aa", "supplier", "none")
LOST_REASONS = (
    "price",
    "price_aa_tour",
    "aa_sold_out",
    "aa_on_request_declined",
    "dates_changed",
    "went_elsewhere",
    "no_response",
    "postponed",
    "other",
)
TASK_STATUSES = ("open", "done", "skipped")
PER_STAGE = 50
MAX_NOTE = 4000
MAX_TITLE = 120

BOARD_SQL = """
WITH visible AS (
    SELECT t.*, row_number() OVER (PARTITION BY t.stage ORDER BY t.last_inquiry_at DESC, t.trip_id) AS n
      FROM crm.trip t
     WHERE t.deleted_at IS NULL AND t.stage = ANY($1::text[])
       AND ($2::text IS NULL OR t.waiting_on = $2)
       AND ($3::uuid IS NULL OR t.owner_member_id = $3)
)
SELECT v.trip_id, v.title, v.stage, v.waiting_on, v.owner_member_id, v.month, v.start_date, v.nights,
       v.party_adults, cardinality(v.party_child_ages) AS children, v.budget_band, v.lead_source,
       v.last_inquiry_at, v.stage_entered_at, v.created_at,
       lead.traveler_id AS lead_traveler_id, lead.display_name_enc AS lead_name_enc,
       (SELECT count(*) FROM crm.trip_traveler c
         WHERE c.trip_id = v.trip_id AND c.role = 'companion' AND c.deleted_at IS NULL)::int AS companions,
       (SELECT min(k.due_at) FROM crm.task k
         WHERE k.trip_id = v.trip_id AND k.status = 'open' AND k.deleted_at IS NULL) AS next_due_at,
       (SELECT count(*) FROM crm.form_submission s WHERE s.trip_id = v.trip_id AND s.deleted_at IS NULL)::int
           AS submissions
  FROM visible v
  LEFT JOIN LATERAL (
      SELECT tt.traveler_id, tr.display_name_enc FROM crm.trip_traveler tt
        JOIN crm.traveler tr ON tr.traveler_id = tt.traveler_id
       WHERE tt.trip_id = v.trip_id AND tt.role = 'lead' AND tt.deleted_at IS NULL
       LIMIT 1
  ) lead ON true
 WHERE v.n <= $4
 ORDER BY v.stage, v.last_inquiry_at DESC, v.trip_id
"""
STAGE_COUNTS_SQL = """
SELECT stage, count(*)::int AS n FROM crm.trip
 WHERE deleted_at IS NULL AND stage = ANY($1::text[]) AND ($2::text IS NULL OR waiting_on = $2)
   AND ($3::uuid IS NULL OR owner_member_id = $3)
 GROUP BY stage
"""
WAITING_COUNTS_SQL = """
SELECT waiting_on, count(*)::int AS n FROM crm.trip
 WHERE deleted_at IS NULL AND stage = ANY($1::text[]) AND ($2::uuid IS NULL OR owner_member_id = $2)
 GROUP BY waiting_on
"""
TRIP_SQL = """
SELECT trip_id, title, stage, stage_entered_at, waiting_on, start_date, end_date, month, date_flex_days, nights,
       destinations, party_adults, party_child_ages, party_type, budget_band, trip_style, owner_member_id,
       intake_form_id, lead_source, lost_reason_code, last_inquiry_at, created_at, updated_at
  FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL
"""
PARTY_SQL = """
SELECT tt.traveler_id, tt.role, tr.display_name_enc, tr.preferred_channel,
       tr.email_enc IS NOT NULL AS has_email, tr.phone_enc IS NOT NULL AS has_phone
  FROM crm.trip_traveler tt JOIN crm.traveler tr ON tr.traveler_id = tt.traveler_id
 WHERE tt.trip_id = $1 AND tt.deleted_at IS NULL AND tr.deleted_at IS NULL
 ORDER BY tt.role DESC, tt.created_at, tt.traveler_id
"""
TASKS_SQL = """
SELECT task_id, kind, assignee_member_id, status, due_at, done_at, created_at FROM crm.task
 WHERE trip_id = $1 AND deleted_at IS NULL ORDER BY (status = 'open') DESC, due_at NULLS LAST, created_at
"""
NOTES_SQL = """
SELECT note_id, kind, body_enc, created_at, created_by FROM crm.note
 WHERE trip_id = $1 AND deleted_at IS NULL ORDER BY created_at DESC, note_id LIMIT 100
"""
HISTORY_SQL = """
SELECT from_stage, to_stage, reason_code, at, created_by FROM crm.trip_stage_history
 WHERE trip_id = $1 ORDER BY at, history_id
"""
SUBMISSIONS_SQL = """
SELECT submission_id, payload_enc, outcome, status, created_at FROM crm.form_submission
 WHERE trip_id = $1 AND deleted_at IS NULL ORDER BY created_at, submission_id LIMIT 20
"""
MEMBERS_SQL = """
SELECT member_id, role, status FROM crm.member WHERE deleted_at IS NULL ORDER BY role, created_at, member_id
"""
ACTIVE_MEMBER_SQL = "SELECT 1 FROM crm.member WHERE member_id = $1 AND status = 'active' AND deleted_at IS NULL"
LOCK_TRIP_SQL = "SELECT stage, owner_member_id FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL FOR UPDATE"
MOVE_SQL = """
UPDATE crm.trip SET stage = $2, stage_entered_at = now(), lost_reason_code = $3 WHERE trip_id = $1
"""
HISTORY_INSERT_SQL = """
INSERT INTO crm.trip_stage_history (trip_id, from_stage, to_stage, reason_code) VALUES ($1, $2, $3, $4)
"""
OWNER_SQL = "UPDATE crm.trip SET owner_member_id = $2 WHERE trip_id = $1"
TASK_OWNER_SQL = """
UPDATE crm.task SET assignee_member_id = $2
 WHERE trip_id = $1 AND status = 'open' AND deleted_at IS NULL
   AND (assignee_member_id IS NULL OR assignee_member_id = $3)
"""
TASK_LOCK_SQL = "SELECT status FROM crm.task WHERE task_id = $1 AND deleted_at IS NULL FOR UPDATE"
TASK_STATUS_SQL = """
UPDATE crm.task SET status = $2,
       done_at = CASE WHEN $2 = 'open' THEN NULL ELSE now() END,
       done_by = CASE WHEN $2 = 'open' THEN NULL ELSE $3 END
 WHERE task_id = $1
RETURNING task_id, kind, assignee_member_id, status, due_at, done_at, created_at
"""
NOTE_INSERT_SQL = """
INSERT INTO crm.note (note_id, trip_id, kind, body_enc) VALUES ($1, $2, 'internal', $3) RETURNING created_at, created_by
"""
TRIP_EXISTS_SQL = "SELECT 1 FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL"
TRIP_DATES_SQL = "SELECT start_date, end_date FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL FOR UPDATE"
TRIP_EDITABLE = ("waiting_on", "title", "start_date", "end_date")

# The answers of a submission the trip page shows: everything except identity (name, e-mail, phone, companions are
# the party list, the message is a note).
SHOWN_ANSWERS = (
    "destinations",
    "month",
    "start_date",
    "end_date",
    "date_flex_days",
    "nights",
    "adults",
    "child_ages",
    "party_type",
    "budget_band",
    "trip_style",
    "preferred_channel",
    "heard_from",
    "consent_marketing",
    "locale",
)


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _id(value: Any) -> str | None:
    return str(value) if value is not None else None


@dataclass(frozen=True)
class BoardFilter:
    waiting_on: str | None = None
    owner: str = "anyone"  # anyone | me

    def owner_id(self, ctx: TenantContext) -> uuid.UUID | None:
        return ctx.member_id if self.owner == "me" else None


async def board(ctx: TenantContext, vault: Vault, flt: BoardFilter) -> dict[str, Any]:
    owner = flt.owner_id(ctx)
    rows = await ctx.conn.fetch(BOARD_SQL, list(BOARD_STAGES), flt.waiting_on, owner, PER_STAGE)
    stage_counts = {
        r["stage"]: r["n"] for r in await ctx.conn.fetch(STAGE_COUNTS_SQL, list(BOARD_STAGES), flt.waiting_on, owner)
    }
    waiting = {r["waiting_on"]: r["n"] for r in await ctx.conn.fetch(WAITING_COUNTS_SQL, list(BOARD_STAGES), owner)}
    cards: dict[str, list[dict[str, Any]]] = {s: [] for s in BOARD_STAGES}
    for r in rows:
        name = None
        if r["lead_name_enc"] is not None:
            name = await vault.decrypt(
                "traveler", "display_name_enc", r["lead_traveler_id"], r["lead_name_enc"], tenant_id=ctx.tenant_id
            )
        cards[r["stage"]].append(
            {
                "trip_id": str(r["trip_id"]),
                "title": r["title"],
                "stage": r["stage"],
                "waiting_on": r["waiting_on"],
                "owner_member_id": _id(r["owner_member_id"]),
                "is_mine": r["owner_member_id"] == ctx.member_id,
                "lead_name": name,
                "companions": r["companions"],
                "party_adults": r["party_adults"],
                "children": r["children"],
                "month": _iso(r["month"]),
                "start_date": _iso(r["start_date"]),
                "nights": r["nights"],
                "budget_band": r["budget_band"],
                "lead_source": r["lead_source"],
                "next_due_at": _iso(r["next_due_at"]),
                "submissions": r["submissions"],
                "last_inquiry_at": _iso(r["last_inquiry_at"]),
                "stage_entered_at": _iso(r["stage_entered_at"]),
            }
        )
    if rows:
        # AA-937 (crm-006): the event names the travellers whose names the page showed (design §4.4).
        shown = tuple(dict.fromkeys(str(r["lead_traveler_id"]) for r in rows if r["lead_name_enc"] is not None))
        ctx.audit("list_view", "allowed", "pipeline", None, fields=("display_name",), reason="board", object_ids=shown)
    return {
        "stages": [
            {
                "stage": s,
                "count": stage_counts.get(s, 0),
                "cards": cards[s],
                "truncated": stage_counts.get(s, 0) > len(cards[s]),
            }
            for s in BOARD_STAGES
        ],
        "waiting_on_counts": {w: waiting.get(w, 0) for w in WAITING_ON},
        "total": sum(waiting.values()),
        "filter": {"waiting_on": flt.waiting_on, "owner": flt.owner},
    }


async def trip_detail(ctx: TenantContext, vault: Vault, trip_id: uuid.UUID) -> dict[str, Any]:
    trip = await ctx.conn.fetchrow(TRIP_SQL, trip_id)
    if trip is None:
        raise NotFound("trip_not_found")
    party = []
    for r in await ctx.conn.fetch(PARTY_SQL, trip_id):
        party.append(
            {
                "traveler_id": str(r["traveler_id"]),
                "role": r["role"],
                "display_name": await vault.decrypt(
                    "traveler", "display_name_enc", r["traveler_id"], r["display_name_enc"], tenant_id=ctx.tenant_id
                ),
                "preferred_channel": r["preferred_channel"],
                "has_email": r["has_email"],
                "has_phone": r["has_phone"],
            }
        )
    notes = []
    for r in await ctx.conn.fetch(NOTES_SQL, trip_id):
        notes.append(
            {
                "note_id": str(r["note_id"]),
                "kind": r["kind"],
                "body": await vault.decrypt("note", "body_enc", r["note_id"], r["body_enc"]),
                "created_at": _iso(r["created_at"]),
                "created_by": r["created_by"],
            }
        )
    submissions = []
    for r in await ctx.conn.fetch(SUBMISSIONS_SQL, trip_id):
        answers: dict[str, Any] | None = None
        if r["payload_enc"] is not None:
            raw = json.loads(
                await vault.decrypt("form_submission", "payload_enc", r["submission_id"], r["payload_enc"])
            )
            answers = {k: raw.get(k) for k in SHOWN_ANSWERS}
        submissions.append(
            {
                "submission_id": str(r["submission_id"]),
                "outcome": r["outcome"],
                "status": r["status"],
                "created_at": _iso(r["created_at"]),
                "answers": answers,
            }
        )
    tasks = [_task(r) for r in await ctx.conn.fetch(TASKS_SQL, trip_id)]
    history = [
        {
            "from_stage": r["from_stage"],
            "to_stage": r["to_stage"],
            "reason_code": r["reason_code"],
            "at": _iso(r["at"]),
            "actor": r["created_by"],
        }
        for r in await ctx.conn.fetch(HISTORY_SQL, trip_id)
    ]
    # Design §4.4: the party's names are shown, so the event names those travellers.
    ctx.audit(
        "view",
        "allowed",
        "trip",
        str(trip_id),
        fields=("display_name", "note_body", "submission_answers"),
        object_ids=tuple(dict.fromkeys(p["traveler_id"] for p in party))[:MAX_OBJECT_IDS],
    )
    return {
        "trip": _trip(trip, ctx),
        "party": party,
        "tasks": tasks,
        "notes": notes,
        "history": history,
        "submissions": submissions,
        "allowed_moves": allowed_moves(trip["stage"]),
    }


def _trip(r: Any, ctx: TenantContext) -> dict[str, Any]:
    return {
        "trip_id": str(r["trip_id"]),
        "title": r["title"],
        "stage": r["stage"],
        "stage_entered_at": _iso(r["stage_entered_at"]),
        "waiting_on": r["waiting_on"],
        "start_date": _iso(r["start_date"]),
        "end_date": _iso(r["end_date"]),
        "month": _iso(r["month"]),
        "date_flex_days": r["date_flex_days"],
        "nights": r["nights"],
        "destinations": list(r["destinations"] or ()),
        "party_adults": r["party_adults"],
        "party_child_ages": list(r["party_child_ages"] or ()),
        "party_type": r["party_type"],
        "budget_band": r["budget_band"],
        "trip_style": r["trip_style"],
        "owner_member_id": _id(r["owner_member_id"]),
        "is_mine": r["owner_member_id"] == ctx.member_id,
        "lead_source": r["lead_source"],
        "lost_reason_code": r["lost_reason_code"],
        "last_inquiry_at": _iso(r["last_inquiry_at"]),
        "created_at": _iso(r["created_at"]),
    }


def _task(r: Any) -> dict[str, Any]:
    return {
        "task_id": str(r["task_id"]),
        "kind": r["kind"],
        "assignee_member_id": _id(r["assignee_member_id"]),
        "status": r["status"],
        "due_at": _iso(r["due_at"]),
        "done_at": _iso(r["done_at"]),
        "created_at": _iso(r["created_at"]),
    }


ALLOWED_MOVES: dict[str, tuple[str, ...]] = {
    "inquiry": ("qualifying", "lost"),
    "qualifying": ("inquiry", "lost"),
    "lost": ("inquiry",),
}


def allowed_moves(stage: str) -> list[str]:
    """The moves Slice 1 opens from ``stage`` (mirrors the crm-005 transition rows; the DB stays the judge)."""
    return list(ALLOWED_MOVES.get(stage, ()))


async def move_stage(
    ctx: TenantContext, trip_id: uuid.UUID, to: str, *, lost_reason: str | None, undo: bool = False
) -> dict[str, Any]:
    current = await ctx.conn.fetchrow(LOCK_TRIP_SQL, trip_id)
    if current is None:
        raise NotFound("trip_not_found")
    if current["stage"] == to:
        raise Conflict("already_in_stage")
    if to == "lost":
        if lost_reason not in LOST_REASONS:
            raise Invalid("lost_reason_required", "pick one of the fixed reasons", reasons=list(LOST_REASONS))
    elif lost_reason is not None:
        raise Invalid("lost_reason_not_allowed", "a reason goes with lost only")
    try:
        async with ctx.conn.transaction():
            await ctx.conn.execute(MOVE_SQL, trip_id, to, lost_reason if to == "lost" else None)
    except asyncpg.CheckViolationError:
        raise Conflict("transition_not_allowed", allowed=allowed_moves(current["stage"])) from None
    reason = "undo" if undo else (f"lost_{lost_reason}"[:40] if to == "lost" else "agent_move")
    await ctx.conn.execute(HISTORY_INSERT_SQL, trip_id, current["stage"], to, reason)
    ctx.audit("stage_change", "allowed", "trip", str(trip_id), reason=reason)
    return {"trip_id": str(trip_id), "from_stage": current["stage"], "stage": to, "allowed_moves": allowed_moves(to)}


async def assign(ctx: TenantContext, trip_id: uuid.UUID, member_id: uuid.UUID | None) -> dict[str, Any]:
    current = await ctx.conn.fetchrow(LOCK_TRIP_SQL, trip_id)
    if current is None:
        raise NotFound("trip_not_found")
    if ctx.member_role != "owner":
        taking = member_id == ctx.member_id and current["owner_member_id"] is None
        if not taking:
            raise Forbidden("owner_only", "only the owner reassigns; you may take an unassigned trip")
    if member_id is not None and not await ctx.conn.fetchval(ACTIVE_MEMBER_SQL, member_id):
        raise Invalid("invalid_member", "an active member of this agency")
    await ctx.conn.execute(OWNER_SQL, trip_id, member_id)
    await ctx.conn.execute(TASK_OWNER_SQL, trip_id, member_id, current["owner_member_id"])
    ctx.audit("assign", "allowed", "trip", str(trip_id), fields=("owner_member_id",))
    return {"trip_id": str(trip_id), "owner_member_id": _id(member_id), "is_mine": member_id == ctx.member_id}


async def update_trip(
    ctx: TenantContext, trip_id: uuid.UUID, changes: dict[str, Any], vault: Vault | None = None
) -> dict[str, Any]:
    """Waiting on, title, and (AA-937) the start / end dates. A changed start date recomputes the party's age bands
    (R32, :mod:`app.travellers.age_bands`; needs ``vault``); any other change leaves them alone."""
    sets: list[str] = []
    args: list[Any] = [trip_id]
    before = None
    if "start_date" in changes or "end_date" in changes:
        before = await ctx.conn.fetchrow(TRIP_DATES_SQL, trip_id)
        if before is None:
            raise NotFound("trip_not_found")
        for name in ("start_date", "end_date"):
            if name in changes:
                value = changes[name]
                if value is not None and not isinstance(value, dt.date):
                    raise Invalid(f"invalid_{name}", "an ISO date")
                args.append(value)
                sets.append(f"{name} = ${len(args)}")
        start = changes.get("start_date", before["start_date"])
        end = changes.get("end_date", before["end_date"])
        if start is not None and end is not None and end < start:
            raise Invalid("invalid_dates", "the end date is before the start date")
    if "waiting_on" in changes:
        if changes["waiting_on"] not in WAITING_ON:
            raise Invalid("invalid_waiting_on", "agency, client, aa, supplier or none")
        args.append(changes["waiting_on"])
        sets.append(f"waiting_on = ${len(args)}")
    if "title" in changes:
        title = stage1.clean_text(changes["title"], MAX_TITLE) if isinstance(changes["title"], str) else None
        if not title:
            raise Invalid("invalid_title", f"1 to {MAX_TITLE} characters")
        args.append(title)
        sets.append(f"title = ${len(args)}")
    if not sets:
        raise Invalid("nothing_to_change")
    row = await ctx.conn.fetchrow(
        f"UPDATE crm.trip SET {', '.join(sets)} WHERE trip_id = $1 AND deleted_at IS NULL RETURNING *",  # noqa: S608
        *args,
    )
    if row is None:
        raise NotFound("trip_not_found")
    ctx.audit(
        "update",
        "allowed",
        "trip",
        str(trip_id),
        fields=tuple(sorted(k for k in changes if k in TRIP_EDITABLE)),
    )
    if before is not None and "start_date" in changes and changes["start_date"] != before["start_date"]:
        if vault is None:
            raise RuntimeError("a start date change needs the vault (age bands)")
        await age_bands.recompute_for_trip(ctx, vault, trip_id, changes["start_date"])
    return _trip(row, ctx)


async def add_note(ctx: TenantContext, vault: Vault, trip_id: uuid.UUID, body: str) -> dict[str, Any]:
    text = stage1.clean_text(body, MAX_NOTE) if isinstance(body, str) else None
    if not text:
        raise Invalid("invalid_note", f"1 to {MAX_NOTE} characters")
    if not await ctx.conn.fetchval(TRIP_EXISTS_SQL, trip_id):
        raise NotFound("trip_not_found")
    note_id = uuid.uuid4()
    row = await ctx.conn.fetchrow(
        NOTE_INSERT_SQL, note_id, trip_id, await vault.encrypt("note", "body_enc", note_id, text)
    )
    ctx.audit("create", "allowed", "note", str(note_id))
    return {
        "note_id": str(note_id),
        "kind": "internal",
        "body": text,
        "created_at": _iso(row["created_at"]),
        "created_by": row["created_by"],
    }


async def set_task_status(ctx: TenantContext, task_id: uuid.UUID, status: str) -> dict[str, Any]:
    if status not in TASK_STATUSES:
        raise Invalid("invalid_status", "open, done or skipped")
    current = await ctx.conn.fetchval(TASK_LOCK_SQL, task_id)
    if current is None:
        raise NotFound("task_not_found")
    if current == status:
        raise Conflict("already_in_status")
    try:
        async with ctx.conn.transaction():
            row = await ctx.conn.fetchrow(TASK_STATUS_SQL, task_id, status, f"member:{ctx.member_id}")
    except asyncpg.CheckViolationError:
        raise Conflict("transition_not_allowed") from None
    ctx.audit("update", "allowed", "task", str(task_id), fields=("status",))
    return _task(row)


async def members(ctx: TenantContext) -> list[dict[str, Any]]:
    return [
        {
            "member_id": str(r["member_id"]),
            "role": r["role"],
            "status": r["status"],
            "is_me": r["member_id"] == ctx.member_id,
        }
        for r in await ctx.conn.fetch(MEMBERS_SQL)
    ]
