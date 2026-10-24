"""One trash for every user-deletable object (design v3 §3.5, R21, Fix B7; lane §3.3).

- **Delete** moves the row to the trash: ``deleted_at``, ``deleted_by`` = the member, ``purge_after`` = now + the
  agency's ``trash_days`` (default 7). Lists already hide trashed rows. The daily ``crm_lifecycle`` run purges rows
  whose window passed (``app.lifecycle.job``), through ``crm.erase_subject`` like an erasure.
- **Delete block** (trips): a trip with a succeeded payment, a paid / claimed invoice, a signed signature request, a
  claimed / received commission or an AA line held or confirmed is not deletable: 409 with the first blocker
  (``has_money`` > ``has_signature`` > ``has_commission`` > ``has_aa_line``), every blocker, and ``alternative =
  cancel_trip`` (the UI turns Delete into Cancel trip, CRM-27). A trip whose AA lines are all released, expired,
  cancelled or declined is deletable. A client on a live trip is not deletable (409 ``traveler_on_trip``,
  ``alternative = archive``).
- **Legal hold** (every kind): an object whose erase the hold rule refuses (crm-010 header item 4;
  ``crm.legal_hold_blocks(kind, id)`` = "``crm.erase_subject`` would refuse with CRM10") is not moved to the trash:
  409 ``legal_hold``, a committed ``denied`` audit event. The pre-check and the purge use the same function, so the
  trash never holds something its purge could not erase (review S249).
- **Restore** inside the window by the owner or the member who deleted it. A trip with AA lines still open in the
  ledger (requested / held / confirmed) re-checks Booking first (:mod:`app.lifecycle.booking`): holds may have
  expired, and the lines come back with Booking's current status; Booking unreachable = 503, the trip stays in the
  trash.
- Who deletes (design §4.6): trip — owner, an agent their own (or an unassigned) trip; client — owner only (R8);
  task, document — owner or agent; note — owner or its author. Assistants delete nothing. A refusal is a committed
  ``denied`` audit event, like every refused owner-only action (AA-940).
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.lifecycle import booking
from app.lifecycle.window import trash_days
from app.tenancy import TenantContext
from app.vault import Vault

BLOCKER_ORDER = ("has_money", "has_signature", "has_commission", "has_aa_line")
OPEN_AA_STATUSES = ("requested", "held", "confirmed")


@dataclass(frozen=True)
class Kind:
    table: str
    key: str
    roles: frozenset[str]


KINDS: dict[str, Kind] = {
    "trip": Kind("trip", "trip_id", frozenset({"owner", "agent"})),
    "traveler": Kind("traveler", "traveler_id", frozenset({"owner"})),
    "task": Kind("task", "task_id", frozenset({"owner", "agent"})),
    "note": Kind("note", "note_id", frozenset({"owner", "agent", "assistant"})),
    "document": Kind("document", "document_id", frozenset({"owner", "agent"})),
}

_ROW_EXTRA = {
    "trip": "owner_member_id, NULL::uuid AS trip_id",
    "traveler": "NULL::uuid AS owner_member_id, NULL::uuid AS trip_id",
    "task": "NULL::uuid AS owner_member_id, trip_id",
    "note": "NULL::uuid AS owner_member_id, trip_id",
    "document": "NULL::uuid AS owner_member_id, trip_id",
}
ROW_SQL = {
    k: f"SELECT deleted_at, deleted_by, purge_after, created_by, {_ROW_EXTRA[k]} FROM crm.{v.table} "  # noqa: S608
    f"WHERE {v.key} = $1 FOR UPDATE"
    for k, v in KINDS.items()
}
TRASH_SQL = {
    k: f"UPDATE crm.{v.table} SET deleted_at = now(), deleted_by = $2, "  # noqa: S608 — fixed names
    f"purge_after = now() + make_interval(days => $3) WHERE {v.key} = $1 RETURNING purge_after"
    for k, v in KINDS.items()
}
RESTORE_SQL = {
    k: f"UPDATE crm.{v.table} SET deleted_at = NULL, deleted_by = NULL, purge_after = NULL "  # noqa: S608
    f"WHERE {v.key} = $1"
    for k, v in KINDS.items()
}
BLOCKERS_SQL = "SELECT crm.trip_delete_blockers($1)"
HOLD_SQL = "SELECT crm.legal_hold_blocks($1, $2)"
LIVE_TRIPS_SQL = """
SELECT count(*) FROM crm.trip_traveler tt JOIN crm.trip t ON t.trip_id = tt.trip_id
 WHERE tt.traveler_id = $1 AND t.deleted_at IS NULL
"""
TRIP_TRASHED_SQL = "SELECT deleted_at IS NOT NULL FROM crm.trip WHERE trip_id = $1"
OPEN_LINES_SQL = """
SELECT commitment_id, source_ref, line_status FROM crm.trip_commitment
 WHERE trip_id = $1 AND kind = 'aa_line' AND line_status = ANY($2::text[]) AND deleted_at IS NULL
 ORDER BY source_ref
"""
SET_LINE_SQL = "UPDATE crm.trip_commitment SET line_status = $2, checked_at = now() WHERE commitment_id = $1"
CHECKED_SQL = "UPDATE crm.trip_commitment SET checked_at = now() WHERE commitment_id = $1"

LIST_SQL = """
SELECT 'trip' AS kind, trip_id AS id, title AS label, NULL::bytea AS label_enc, deleted_at, deleted_by, purge_after
  FROM crm.trip WHERE deleted_at IS NOT NULL AND ($1::text IS NULL OR deleted_by = $1)
UNION ALL
SELECT 'traveler', traveler_id, NULL, display_name_enc, deleted_at, deleted_by, purge_after
  FROM crm.traveler WHERE deleted_at IS NOT NULL AND ($1::text IS NULL OR deleted_by = $1)
UNION ALL
SELECT 'task', task_id, kind, NULL, deleted_at, deleted_by, purge_after
  FROM crm.task WHERE deleted_at IS NOT NULL AND ($1::text IS NULL OR deleted_by = $1)
UNION ALL
SELECT 'note', note_id, kind, NULL, deleted_at, deleted_by, purge_after
  FROM crm.note WHERE deleted_at IS NOT NULL AND ($1::text IS NULL OR deleted_by = $1)
UNION ALL
SELECT 'document', document_id, NULL, filename_enc, deleted_at, deleted_by, purge_after
  FROM crm.document WHERE deleted_at IS NOT NULL AND ($1::text IS NULL OR deleted_by = $1)
ORDER BY deleted_at DESC, id
LIMIT $2
"""
MAX_LIST = 200


def _kind(kind: str) -> Kind:
    try:
        return KINDS[kind]
    except KeyError:
        raise Invalid("invalid_kind", ", ".join(KINDS)) from None


def _me(ctx: TenantContext) -> str:
    return f"member:{ctx.member_id}"


def _refuse(ctx: TenantContext, action: str, kind: str, object_id: uuid.UUID, reason: str, message: str) -> None:
    ctx.audit(action, "denied", kind, str(object_id), reason=reason)
    raise Forbidden(reason, message)


async def _row(ctx: TenantContext, kind: str, object_id: uuid.UUID) -> Any:
    row = await ctx.conn.fetchrow(ROW_SQL[kind], object_id)
    if row is None:
        raise NotFound(f"{kind}_not_found")
    return row


async def blockers(ctx: TenantContext, trip_id: uuid.UUID) -> list[str]:
    codes = list(await ctx.conn.fetchval(BLOCKERS_SQL, trip_id) or [])
    return sorted(codes, key=lambda c: BLOCKER_ORDER.index(c) if c in BLOCKER_ORDER else len(BLOCKER_ORDER))


async def move_to_trash(ctx: TenantContext, kind: str, object_id: uuid.UUID) -> dict[str, Any]:
    spec = _kind(kind)
    if ctx.member_role not in spec.roles:
        _refuse(
            ctx,
            "delete",
            kind,
            object_id,
            "owner_only" if spec.roles == {"owner"} else "not_allowed_for_role",
            "your role cannot delete this",
        )
    row = await _row(ctx, kind, object_id)
    if row["deleted_at"] is not None:
        raise NotFound(f"{kind}_not_found")
    if kind == "trip" and ctx.member_role == "agent" and row["owner_member_id"] not in (None, ctx.member_id):
        _refuse(ctx, "delete", kind, object_id, "not_your_trip", "agents delete their own trips")
    if kind == "note" and ctx.member_role != "owner" and row["created_by"] != _me(ctx):
        _refuse(ctx, "delete", kind, object_id, "not_your_note", "a note is deleted by its author or the owner")
    if await ctx.conn.fetchval(HOLD_SQL, kind, object_id):
        ctx.audit("delete", "denied", kind, str(object_id), reason="legal_hold")
        raise Conflict("legal_hold", "an active legal hold reaches this, so it cannot be deleted until it is released")
    if kind == "trip":
        codes = await blockers(ctx, object_id)
        if codes:
            ctx.audit("delete", "denied", "trip", str(object_id), reason=codes[0])
            raise Conflict(
                codes[0],
                "this trip has money, a signature, a commission or an AA booking",
                blockers=codes,
                alternative="cancel_trip",
            )
    if kind == "traveler" and await ctx.conn.fetchval(LIVE_TRIPS_SQL, object_id):
        raise Conflict("traveler_on_trip", "the client is on a trip that is not deleted", alternative="archive")
    purge_after = await ctx.conn.fetchval(TRASH_SQL[kind], object_id, _me(ctx), await trash_days(ctx))
    ctx.audit("delete", "allowed", kind, str(object_id), reason="to_trash")
    return {"kind": kind, "id": str(object_id), "purge_after": purge_after.isoformat()}


async def restore(
    ctx: TenantContext, kind: str, object_id: uuid.UUID, checker: booking.BookingLineChecker | None
) -> dict[str, Any]:
    spec = _kind(kind)
    row = await _row(ctx, kind, object_id)
    if row["deleted_at"] is None:
        raise NotFound("not_in_trash")
    if ctx.member_role != "owner" and (row["deleted_by"] != _me(ctx) or ctx.member_role not in spec.roles):
        _refuse(ctx, "restore", kind, object_id, "owner_only", "the owner or whoever deleted it restores it")
    if row["purge_after"] <= dt.datetime.now(dt.UTC):
        raise Conflict("trash_window_passed", "this item is waiting to be purged")
    if row["trip_id"] is not None and await ctx.conn.fetchval(TRIP_TRASHED_SQL, row["trip_id"]):
        raise Conflict("parent_in_trash", "restore the trip first")
    rechecked: dict[str, int] = {}
    if kind == "trip":
        rechecked = await recheck_booking(ctx, object_id, checker)
    await ctx.conn.execute(RESTORE_SQL[kind], object_id)
    ctx.audit("restore", "allowed", kind, str(object_id), reason="booking_rechecked" if rechecked else None)
    return {"kind": kind, "id": str(object_id), "restored": True, "aa_lines": rechecked}


async def recheck_booking(
    ctx: TenantContext, trip_id: uuid.UUID, checker: booking.BookingLineChecker | None
) -> dict[str, int]:
    """Card E "restore re-checks Booking": every AA line the ledger still shows open gets Booking's current status."""
    lines = await ctx.conn.fetch(OPEN_LINES_SQL, trip_id, list(OPEN_AA_STATUSES))
    if not lines:
        return {}
    if checker is None:
        raise booking.BookingUnavailable("booking_not_configured")
    current = await checker.line_statuses(ctx.tenant_id, [r["source_ref"] for r in lines])
    changed = 0
    for r in lines:
        status = current.get(r["source_ref"])
        if status is None:
            raise booking.BookingUnavailable("booking_line_unknown")
        if status not in booking.LINE_STATUSES:
            raise booking.BookingUnavailable("booking_status_unknown")
        if status != r["line_status"]:
            await ctx.conn.execute(SET_LINE_SQL, r["commitment_id"], status)
            changed += 1
        else:
            await ctx.conn.execute(CHECKED_SQL, r["commitment_id"])
    return {"checked_count": len(lines), "changed_count": changed}


async def list_trash(ctx: TenantContext, vault: Vault) -> dict[str, Any]:
    """The trash: the owner sees every item, other roles what they deleted. Names of clients and file names are
    decrypted for display, one ``list_view`` event naming them (design §4.4)."""
    mine = None if ctx.member_role == "owner" else _me(ctx)
    rows = await ctx.conn.fetch(LIST_SQL, mine, MAX_LIST)
    items: list[dict[str, Any]] = []
    shown: list[str] = []
    for r in rows:
        label = r["label"]
        if r["label_enc"] is not None:
            table, column = (
                ("traveler", "display_name_enc") if r["kind"] == "traveler" else ("document", "filename_enc")
            )
            label = await vault.decrypt(table, column, r["id"], r["label_enc"])
            shown.append(str(r["id"]))
        items.append(
            {
                "kind": r["kind"],
                "id": str(r["id"]),
                "label": label,
                "deleted_at": r["deleted_at"].isoformat(),
                "deleted_by": r["deleted_by"],
                "purge_after": r["purge_after"].isoformat(),
            }
        )
    if shown:
        ctx.audit(
            "list_view",
            "allowed",
            "trash",
            None,
            fields=("display_name", "document_filename"),
            reason="trash_list",
            object_ids=tuple(shown),
        )
    return {"items": items, "trash_days": await trash_days(ctx)}
