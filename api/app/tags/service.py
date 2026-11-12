"""Tags for the routes (AA-960, card CRM-29b; design v3 R3, A-02 / A-06 / A-07 / A-17): the vocabulary, the tags of
one trip or client, and bulk apply with its preview.

- **Vocabulary** (every role): the agency's tags of a subject (trip / client) with how many records carry each, plus the
  system tags the app has made. A new agency tag gets a fixed code (:func:`app.tags.model.code_for`); the same name
  (case-insensitive) on the same subject is 409 ``tag_exists`` (with the existing tag's id, so the picker uses it).
- **Manage** (owner, ``tag_manage``): rename, recolour, archive / restore, delete (its links go with it). A system tag
  is the app's: 409 ``system_tag``.
- **One record** (every role, ``tag_apply``): put an agency tag on a trip / client or take it off. A system tag is 409
  ``system_tag``; an archived tag is not put on (409 ``tag_archived``); a tag of the other subject is 422
  ``tag_subject_mismatch``. Putting a tag on starts the tag workflows of the agency (``trip_tag.added`` /
  ``client_tag.added``, :func:`app.workflows.triggers.on_tag_added`); on an archived client they are suppressed (card
  R). A held record keeps its tags (CRM10 -> 409 ``legal_hold``).
- **Bulk** (owner, agent: ``tag_bulk_apply``): one tag added to / removed from a selection (explicit ids, or a filter:
  trips by stage / owner / tag; clients by archived / owner / tag), at most :data:`MAX_BULK` records. The dry run is
  the preview (card E): how many change, how many already have it, how many archived clients would get it without
  their workflows, how many held records are skipped, a sample of ids. The apply sends back the previewed ``expected``
  count: another count is 409 ``count_changed`` (the selection moved since the preview).

Every function takes the open member context and never opens one.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

import asyncpg

from app.errors import Conflict, Invalid, NotFound
from app.permissions import Action, require
from app.stages import STAGES
from app.tags import model
from app.tenancy import LEGAL_HOLD_SQLSTATE, AnyContext, TenantContext

MAX_BULK = 500
SAMPLE = 20

VOCAB_SQL = """
SELECT g.tag_id, g.kind, g.applies_to, g.code, g.name, g.tone, g.archived_at, g.created_at,
       (SELECT count(*) FROM crm.tag_link l WHERE l.tag_id = g.tag_id)::int AS uses
  FROM crm.tag g
 WHERE ($1::text IS NULL OR g.applies_to = $1) AND ($2 OR g.archived_at IS NULL)
 ORDER BY g.applies_to, g.kind, lower(g.name), g.code
"""
TAG_SQL = "SELECT tag_id, kind, applies_to, code, name, tone, archived_at FROM crm.tag WHERE tag_id = $1"
CODES_SQL = "SELECT code, lower(name) AS lname, tag_id FROM crm.tag WHERE applies_to = $1"
COUNT_SQL = "SELECT count(*)::int FROM crm.tag WHERE applies_to = $1 AND kind = 'agency'"
INSERT_SQL = """
INSERT INTO crm.tag (applies_to, code, name, tone) VALUES ($1, $2, $3, $4)
RETURNING tag_id, kind, applies_to, code, name, tone, archived_at
"""
UPDATE_SQL = """
UPDATE crm.tag SET name = $2, tone = $3, archived_at = CASE WHEN $4 THEN coalesce(archived_at, now()) END
 WHERE tag_id = $1
RETURNING tag_id, kind, applies_to, code, name, tone, archived_at
"""
USES_SQL = "SELECT count(*)::int FROM crm.tag_link WHERE tag_id = $1"
# crm.tag hangs on the agency's settings row (crm-061 tag_settings_fk); provisioning does not make that row.
ENSURE_SETTINGS_SQL = "INSERT INTO crm.tenant_settings DEFAULT VALUES ON CONFLICT (tenant_id) DO NOTHING"
DELETE_SQL = "DELETE FROM crm.tag WHERE tag_id = $1"
SUBJECT_TAGS_SQL = """
SELECT l.tag_link_id, l.source, l.reasons, l.created_at, g.tag_id, g.kind, g.code, g.name, g.tone, g.archived_at
  FROM crm.tag_link l JOIN crm.tag g ON g.tag_id = l.tag_id
 WHERE ($1::uuid IS NOT NULL AND l.trip_id = $1) OR ($2::uuid IS NOT NULL AND l.traveler_id = $2)
 ORDER BY g.kind, lower(g.name), g.code
"""
TAGS_OF_TRIPS_SQL = """
SELECT l.trip_id AS subject_id, g.code, g.name, g.tone, g.kind
  FROM crm.tag_link l JOIN crm.tag g ON g.tag_id = l.tag_id
 WHERE l.trip_id = ANY($1::uuid[])
 ORDER BY g.kind DESC, lower(g.name), g.code
"""
TAGS_OF_CLIENTS_SQL = """
SELECT l.traveler_id AS subject_id, g.code, g.name, g.tone, g.kind
  FROM crm.tag_link l JOIN crm.tag g ON g.tag_id = l.tag_id
 WHERE l.traveler_id = ANY($1::uuid[])
 ORDER BY g.kind DESC, lower(g.name), g.code
"""
TRIP_SQL = "SELECT trip_id FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL"
CLIENT_SQL = """
SELECT traveler_id, archived_at IS NOT NULL AS archived FROM crm.traveler WHERE traveler_id = $1 AND deleted_at IS NULL
"""
LINK_TRIP_SQL = """
INSERT INTO crm.tag_link (tag_id, trip_id) VALUES ($1, $2)
ON CONFLICT (tenant_id, tag_id, trip_id) WHERE trip_id IS NOT NULL DO NOTHING
RETURNING tag_link_id
"""
LINK_CLIENT_SQL = """
INSERT INTO crm.tag_link (tag_id, traveler_id) VALUES ($1, $2)
ON CONFLICT (tenant_id, tag_id, traveler_id) WHERE traveler_id IS NOT NULL DO NOTHING
RETURNING tag_link_id
"""
UNLINK_SQL = """
DELETE FROM crm.tag_link
 WHERE tag_id = $1 AND (($2::uuid IS NOT NULL AND trip_id = $2) OR ($3::uuid IS NOT NULL AND traveler_id = $3))
RETURNING tag_link_id
"""
# Bulk selections: live records only; a filter's owner is a member id, its tag a code of the same subject.
BULK_TRIPS_SQL = """
SELECT t.trip_id AS id, false AS archived,
       EXISTS (SELECT 1 FROM crm.tag_link l WHERE l.tag_id = $1 AND l.trip_id = t.trip_id) AS has_tag,
       crm.hold_row(t.trip_id, NULL) AS held
  FROM crm.trip t
 WHERE t.deleted_at IS NULL
   AND ($2::uuid[] IS NULL OR t.trip_id = ANY($2))
   AND ($3::text IS NULL OR t.stage = $3)
   AND ($4::uuid IS NULL OR t.owner_member_id = $4)
   AND ($5::text IS NULL OR EXISTS (SELECT 1 FROM crm.tag_link f JOIN crm.tag fg ON fg.tag_id = f.tag_id
                                     WHERE f.trip_id = t.trip_id AND fg.applies_to = 'trip' AND fg.code = $5))
 ORDER BY t.last_activity_at DESC, t.trip_id
 LIMIT $6
"""
BULK_CLIENTS_SQL = """
SELECT tr.traveler_id AS id, tr.archived_at IS NOT NULL AS archived,
       EXISTS (SELECT 1 FROM crm.tag_link l WHERE l.tag_id = $1 AND l.traveler_id = tr.traveler_id) AS has_tag,
       crm.hold_row(NULL, tr.traveler_id) AS held
  FROM crm.traveler tr
 WHERE tr.deleted_at IS NULL
   AND ($2::uuid[] IS NULL OR tr.traveler_id = ANY($2))
   AND ($3::boolean IS NULL OR (tr.archived_at IS NOT NULL) = $3)
   AND ($4::uuid IS NULL OR tr.owner_member_id = $4)
   AND ($5::text IS NULL OR EXISTS (SELECT 1 FROM crm.tag_link f JOIN crm.tag fg ON fg.tag_id = f.tag_id
                                     WHERE f.traveler_id = tr.traveler_id AND fg.applies_to = 'client'
                                       AND fg.code = $5))
 ORDER BY tr.created_at DESC, tr.traveler_id
 LIMIT $6
"""


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _json(raw: Any) -> Any:
    return json.loads(raw) if isinstance(raw, str) else raw


def tag_view(r: Any) -> dict[str, Any]:
    out = {
        "tag_id": str(r["tag_id"]),
        "kind": r["kind"],
        "applies_to": r["applies_to"],
        "code": r["code"],
        "name": r["name"],
        "tone": r["tone"],
        "archived": r["archived_at"] is not None,
    }
    if "uses" in r.keys():
        out["uses"] = r["uses"]
    return out


def _hold(exc: asyncpg.PostgresError) -> None:
    if getattr(exc, "sqlstate", None) == LEGAL_HOLD_SQLSTATE:
        raise Conflict("legal_hold", "this record is on legal hold") from None


async def vocabulary(ctx: TenantContext, *, applies_to: str | None = None, archived: bool = False) -> dict[str, Any]:
    if applies_to is not None:
        model.subject_of(applies_to)
    rows = await ctx.conn.fetch(VOCAB_SQL, applies_to, archived)
    role = ctx.member_role
    return {
        "tags": [tag_view(r) for r in rows],
        "tones": list(model.TONES),
        "can_manage": role == "owner",
        "can_bulk": role in ("owner", "agent"),
    }


async def _tag(ctx: AnyContext, tag_id: uuid.UUID) -> Any:
    row = await ctx.conn.fetchrow(TAG_SQL, tag_id)
    if row is None:
        raise NotFound("tag_not_found")
    return row


async def create(ctx: TenantContext, applies_to: str, name: Any, tone: Any = "neutral") -> dict[str, Any]:
    """A new agency tag (``tag_apply``: any member makes one from the picker)."""
    require(ctx, Action.TAG_APPLY, object_kind="tag")
    applies_to = model.subject_of(applies_to)
    clean = model.clean_name(name)
    tone = model.clean_tone(tone)
    existing = await ctx.conn.fetch(CODES_SQL, applies_to)
    for r in existing:
        if r["lname"] == clean.lower():
            raise Conflict("tag_exists", "a tag with this name exists", tag_id=str(r["tag_id"]))
    if int(await ctx.conn.fetchval(COUNT_SQL, applies_to) or 0) >= model.MAX_TAGS:
        raise Conflict("too_many_tags", f"at most {model.MAX_TAGS} tags per kind of record")
    code = model.code_for(clean, (r["code"] for r in existing))
    await ctx.conn.execute(ENSURE_SETTINGS_SQL)
    row = await ctx.conn.fetchrow(INSERT_SQL, applies_to, code, clean, tone)
    ctx.audit("tag_create", "allowed", "tag", str(row["tag_id"]))
    return tag_view(row)


async def update(
    ctx: TenantContext, tag_id: uuid.UUID, *, name: Any = None, tone: Any = None, archived: bool | None = None
) -> dict[str, Any]:
    require(ctx, Action.TAG_MANAGE, object_kind="tag", object_id=str(tag_id))
    row = await _tag(ctx, tag_id)
    if row["kind"] == "system":
        raise Conflict("system_tag", "the app keeps its own tags")
    clean = model.clean_name(name) if name is not None else row["name"]
    if clean.lower() != row["name"].lower():
        for r in await ctx.conn.fetch(CODES_SQL, row["applies_to"]):
            if r["lname"] == clean.lower() and r["tag_id"] != tag_id:
                raise Conflict("tag_exists", "a tag with this name exists", tag_id=str(r["tag_id"]))
    tone = model.clean_tone(tone) if tone is not None else row["tone"]
    keep_archived = (row["archived_at"] is not None) if archived is None else archived
    out = await ctx.conn.fetchrow(UPDATE_SQL, tag_id, clean, tone, keep_archived)
    ctx.audit("tag_update", "allowed", "tag", str(tag_id))
    return tag_view(out)


async def delete(ctx: TenantContext, tag_id: uuid.UUID) -> dict[str, Any]:
    """Delete an agency tag and every link of it (the vocabulary page shows the count first)."""
    require(ctx, Action.TAG_MANAGE, object_kind="tag", object_id=str(tag_id))
    row = await _tag(ctx, tag_id)
    if row["kind"] == "system":
        raise Conflict("system_tag", "the app keeps its own tags")
    uses = int(await ctx.conn.fetchval(USES_SQL, tag_id) or 0)
    try:
        async with ctx.conn.transaction():
            await ctx.conn.execute(DELETE_SQL, tag_id)
    except asyncpg.PostgresError as exc:
        _hold(exc)
        raise
    ctx.audit("tag_delete", "allowed", "tag", str(tag_id), reason=f"links_{min(uses, 99999)}")
    return {"tag_id": str(tag_id), "deleted": True, "removed_links": uses}


def link_view(r: Any) -> dict[str, Any]:
    return {
        "tag_link_id": str(r["tag_link_id"]),
        "tag_id": str(r["tag_id"]),
        "kind": r["kind"],
        "code": r["code"],
        "name": r["name"],
        "tone": r["tone"],
        "archived": r["archived_at"] is not None,
        "source": r["source"],
        "reasons": _json(r["reasons"]) or [],
        "since": _iso(r["created_at"]),
    }


async def of_subject(
    ctx: TenantContext, *, trip_id: uuid.UUID | None = None, traveler_id: uuid.UUID | None = None
) -> dict[str, Any]:
    """The tags of one trip or one client (system tags with their reasons)."""
    if (trip_id is None) == (traveler_id is None):
        raise Invalid("invalid_subject", "one trip or one client")
    sql, key = (TRIP_SQL, trip_id) if trip_id is not None else (CLIENT_SQL, traveler_id)
    if await ctx.conn.fetchrow(sql, key) is None:
        raise NotFound("trip_not_found" if trip_id is not None else "traveler_not_found")
    rows = await ctx.conn.fetch(SUBJECT_TAGS_SQL, trip_id, traveler_id)
    return {"tags": [link_view(r) for r in rows]}


async def of_many(ctx: AnyContext, applies_to: str, ids: list[uuid.UUID]) -> dict[uuid.UUID, list[dict[str, Any]]]:
    """Tags (code, name, tone, kind) per trip / client for a list or a board."""
    if not ids:
        return {}
    sql = TAGS_OF_TRIPS_SQL if applies_to == "trip" else TAGS_OF_CLIENTS_SQL
    out: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for r in await ctx.conn.fetch(sql, ids):
        out.setdefault(r["subject_id"], []).append(
            {"code": r["code"], "name": r["name"], "tone": r["tone"], "kind": r["kind"]}
        )
    return out


async def _start_workflows(
    ctx: AnyContext, tag: Any, link_id: uuid.UUID, subject_id: uuid.UUID, *, archived: bool
) -> int:
    from app.workflows import triggers  # app.workflows imports app.tags (conditions): import late

    return await triggers.on_tag_added(
        ctx, tag["applies_to"], tag["code"], subject_id, link_id, client_archived=archived
    )


async def put(
    ctx: TenantContext, tag_id: uuid.UUID, *, trip_id: uuid.UUID | None = None, traveler_id: uuid.UUID | None = None
) -> dict[str, Any]:
    """Put an agency tag on one trip or client (idempotent: already there = ``added`` false)."""
    require(ctx, Action.TAG_APPLY, object_kind="tag", object_id=str(tag_id))
    tag = await _tag(ctx, tag_id)
    subject = "trip" if trip_id is not None else "client"
    if tag["kind"] == "system":
        raise Conflict("system_tag", "the app keeps its own tags")
    if tag["applies_to"] != subject:
        raise Invalid("tag_subject_mismatch", f"a {tag['applies_to']} tag")
    if tag["archived_at"] is not None:
        raise Conflict("tag_archived", "restore the tag first")
    archived = False
    if trip_id is not None:
        if await ctx.conn.fetchrow(TRIP_SQL, trip_id) is None:
            raise NotFound("trip_not_found")
        link_id = await ctx.conn.fetchval(LINK_TRIP_SQL, tag_id, trip_id)
        subject_id: uuid.UUID = trip_id
    else:
        client = await ctx.conn.fetchrow(CLIENT_SQL, traveler_id)
        if client is None:
            raise NotFound("traveler_not_found")
        archived = bool(client["archived"])
        link_id = await ctx.conn.fetchval(LINK_CLIENT_SQL, tag_id, traveler_id)
        subject_id = client["traveler_id"]
    started = 0
    if link_id is not None:
        ctx.audit("tag_put", "allowed", "trip" if trip_id else "traveler", str(subject_id), reason=tag["code"])
        started = await _start_workflows(ctx, tag, link_id, subject_id, archived=archived)
    return {"tag_id": str(tag_id), "added": link_id is not None, "workflows_started": started,
            "workflows_suppressed": archived and link_id is not None}  # fmt: skip


async def remove(
    ctx: TenantContext, tag_id: uuid.UUID, *, trip_id: uuid.UUID | None = None, traveler_id: uuid.UUID | None = None
) -> dict[str, Any]:
    require(ctx, Action.TAG_APPLY, object_kind="tag", object_id=str(tag_id))
    tag = await _tag(ctx, tag_id)
    if tag["kind"] == "system":
        raise Conflict("system_tag", "the app keeps its own tags")
    try:
        async with ctx.conn.transaction():
            rows = await ctx.conn.fetch(UNLINK_SQL, tag_id, trip_id, traveler_id)
    except asyncpg.PostgresError as exc:
        _hold(exc)
        raise
    if rows:
        ctx.audit("tag_remove", "allowed", "trip" if trip_id else "traveler", str(trip_id or traveler_id),
                  reason=tag["code"])  # fmt: skip
    return {"tag_id": str(tag_id), "removed": bool(rows)}


# ── Bulk ──────────────────────────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Selection:
    """Who a bulk apply is about: explicit ids, or a filter of the subject's list."""

    ids: tuple[uuid.UUID, ...] = ()
    stage: str | None = None
    owner: str | None = None  # "me" or a member id
    tag: str | None = None  # a code of the same subject
    archived: bool | None = None  # clients only

    def check(self, subject: str) -> None:
        if len(self.ids) > MAX_BULK:
            raise Invalid("too_many_records", f"at most {MAX_BULK} records")
        if not self.ids and self.stage is None and self.owner is None and self.tag is None and self.archived is None:
            raise Invalid("selection_required", "pick records or a filter")
        if self.stage is not None and (subject != "trip" or self.stage not in STAGES):
            raise Invalid("invalid_filter", "a stage of a trip", field="filter.stage")
        if self.archived is not None and subject != "client":
            raise Invalid("invalid_filter", "archived is a client filter", field="filter.archived")
        if self.tag is not None and not model.is_code(self.tag):
            raise Invalid("invalid_filter", "a tag code", field="filter.tag")
        if self.owner is not None and self.owner != "me":
            try:
                uuid.UUID(self.owner)
            except ValueError:
                raise Invalid("invalid_filter", "me or a member id", field="filter.owner") from None


async def bulk(
    ctx: TenantContext,
    tag_id: uuid.UUID,
    *,
    action: str,
    selection: Selection,
    dry_run: bool = True,
    expected: int | None = None,
) -> dict[str, Any]:
    require(ctx, Action.TAG_BULK_APPLY, object_kind="tag", object_id=str(tag_id))
    if action not in ("add", "remove"):
        raise Invalid("invalid_action", "add or remove", field="action")
    tag = await _tag(ctx, tag_id)
    if tag["kind"] == "system":
        raise Conflict("system_tag", "the app keeps its own tags")
    if action == "add" and tag["archived_at"] is not None:
        raise Conflict("tag_archived", "restore the tag first")
    subject = tag["applies_to"]
    selection.check(subject)
    owner = (
        ctx.member_id if selection.owner == "me" else uuid.UUID(selection.owner) if selection.owner else None
    )  # fmt: skip
    sql = BULK_TRIPS_SQL if subject == "trip" else BULK_CLIENTS_SQL
    filter_value: Any = selection.stage if subject == "trip" else selection.archived
    rows = await ctx.conn.fetch(
        sql, tag_id, list(selection.ids) or None, filter_value, owner, selection.tag, MAX_BULK + 1
    )
    if len(rows) > MAX_BULK:
        raise Invalid("too_many_records", f"at most {MAX_BULK} records: narrow the filter")
    change: list[Any] = []
    already = held = archived = 0
    for r in rows:
        if (action == "add") == bool(r["has_tag"]):
            already += 1
        elif action == "remove" and r["held"]:
            held += 1
        else:
            change.append(r)
            archived += action == "add" and bool(r["archived"])
    out: dict[str, Any] = {
        "tag_id": str(tag_id),
        "action": action,
        "applies_to": subject,
        "total": len(rows),
        "count": len(change),
        "already": already,
        "held": held,
        "archived_suppressed": archived,
        "sample": [str(r["id"]) for r in change[:SAMPLE]],
        "dry_run": dry_run,
    }
    if dry_run:
        return out
    if expected is None or expected != len(change):
        raise Conflict("count_changed", "the selection changed since the preview: check the count again",
                       count=len(change))  # fmt: skip
    started = 0
    for r in change:
        if action == "add":
            link_sql = LINK_TRIP_SQL if subject == "trip" else LINK_CLIENT_SQL
            link_id = await ctx.conn.fetchval(link_sql, tag_id, r["id"])
            if link_id is not None:
                started += await _start_workflows(ctx, tag, link_id, r["id"], archived=bool(r["archived"]))
        else:
            trip, client = (r["id"], None) if subject == "trip" else (None, r["id"])
            try:
                async with ctx.conn.transaction():
                    await ctx.conn.execute(UNLINK_SQL, tag_id, trip, client)
            except asyncpg.PostgresError as exc:
                _hold(exc)
                raise
    ctx.audit("tag_bulk_" + action, "allowed", "tag", str(tag_id),
              object_ids=tuple(str(r["id"]) for r in change)[:200])  # fmt: skip
    return {**out, "workflows_started": started}
