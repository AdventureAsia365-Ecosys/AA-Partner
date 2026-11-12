"""The "Included" / "Not included" lists of a builder proposal version (AA-1070 item crm-35; migration crm-059; S275
study row 24, behaviour only).

- **The agency library** (``crm.inclusion_item``): standard lines the owner or an agent keeps (kind ``included`` /
  ``not_included``, English text, optional Vietnamese text, sort order, archived). Every role reads it; an archived
  line is no longer offered in the builder, a version that picked it keeps its own copy.
- **A version's lines** (``crm.proposal_inclusion``): picked from the library (the text is copied) or typed for this
  version only. ``PUT /proposals/{id}/inclusions`` replaces them in one builder write (the version counter, draft
  only): pick, unpick and reorder are the new list. A line already on the draft keeps its copied text even when its
  library line was archived or edited since; a new pick copies the library text now. The database keeps them
  draft-only (CRM35), so a published version shows the lists it went out with.
- **AA tours** (fixed, read-only): the inclusions and exclusions of each AA tour the version holds, read from the CIS
  tour content the CRM already caches (``crm.aa_catalog_tour.body``: ``inclusions`` strings, ``exclusions`` objects
  with a ``text``). A draft shows them live; Publish copies them into the version (source ``aa_tour``) for every AA
  tour among its options, so the published version is frozen too; each page then shows the tours of the options
  chosen (the defaults for the builder and the texts, the client's picks on the client page). A version published
  before this card has no copy and reads the cache.
- **One section, three forms**: the client page (:func:`client_view`), the e-mail / WhatsApp text and the proposal
  PDF (:mod:`app.proposals.text_proposal` -> :mod:`app.proposals.text_render`) read the same lines.

Commercial wording, no personal data. Pure helpers first (unit tested without a database), then the context
functions (they never open one).
"""

from __future__ import annotations

import json
import unicodedata
import uuid
from typing import Any

import asyncpg

from app.errors import Conflict, Invalid, NotFound
from app.tenancy import AnyContext, TenantContext

KINDS = ("included", "not_included")
SOURCES = ("library", "custom", "aa_tour")
MAX_ITEMS = 200  # library lines per agency (archived ones count)
MAX_LINES = 60  # lines one version picks or types
MAX_TEXT = 300  # what a member types (the database allows 500: an AA tour's line is copied as CIS wrote it)
AA_TEXT = 500
MAX_AA_LINES = 40  # per AA tour and kind
MAX_SORT = 999
TABLE = "inclusion_item"

LIBRARY_SQL = """
SELECT inclusion_item_id, kind, text_en, text_vi, sort_order, archived, updated_at
  FROM crm.inclusion_item
 WHERE deleted_at IS NULL AND ($1::boolean OR NOT archived)
 ORDER BY kind, sort_order, lower(text_en), inclusion_item_id
"""
ITEM_SQL = """
SELECT inclusion_item_id, kind, text_en, text_vi, sort_order, archived, updated_at
  FROM crm.inclusion_item WHERE inclusion_item_id = $1 AND deleted_at IS NULL
"""
ITEMS_SQL = """
SELECT inclusion_item_id, kind, text_en, text_vi, archived
  FROM crm.inclusion_item WHERE inclusion_item_id = ANY($1::uuid[]) AND deleted_at IS NULL
"""
COUNT_SQL = "SELECT count(*) FROM crm.inclusion_item WHERE deleted_at IS NULL"
# The library hangs on the agency's settings row (the agency erase deletes it); an agency may not have one yet.
ENSURE_SETTINGS_SQL = "INSERT INTO crm.tenant_settings DEFAULT VALUES ON CONFLICT (tenant_id) DO NOTHING"
NEXT_SORT_SQL = "SELECT COALESCE(max(sort_order) + 1, 0) FROM crm.inclusion_item WHERE kind = $1 AND deleted_at IS NULL"
INSERT_ITEM_SQL = """
INSERT INTO crm.inclusion_item (inclusion_item_id, kind, text_en, text_vi, sort_order)
VALUES ($1, $2, $3, $4, $5)
"""
UPDATE_ITEM_SQL = """
UPDATE crm.inclusion_item SET kind = $2, text_en = $3, text_vi = $4, sort_order = $5, archived = $6
 WHERE inclusion_item_id = $1 AND deleted_at IS NULL
"""
DELETE_ITEM_SQL = "DELETE FROM crm.inclusion_item WHERE inclusion_item_id = $1 RETURNING inclusion_item_id"
LINES_SQL = """
SELECT proposal_inclusion_id, kind, source, inclusion_item_id, aa_tour_source_id, aa_tour_title, text_en, text_vi,
       sort_order
  FROM crm.proposal_inclusion WHERE proposal_id = $1 AND deleted_at IS NULL
 ORDER BY kind, sort_order, proposal_inclusion_id
"""
DELETE_AGENCY_LINES_SQL = "DELETE FROM crm.proposal_inclusion WHERE proposal_id = $1 AND source <> 'aa_tour'"
DELETE_AA_LINES_SQL = "DELETE FROM crm.proposal_inclusion WHERE proposal_id = $1 AND source = 'aa_tour'"
INSERT_LINE_SQL = """
INSERT INTO crm.proposal_inclusion (proposal_id, trip_id, kind, source, inclusion_item_id, aa_tour_source_id,
                                    aa_tour_title, text_en, text_vi, sort_order, source_inclusion_id)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
"""
AA_TOURS_SQL = """
SELECT source_id, title, body -> 'inclusions' AS inclusions, body -> 'exclusions' AS exclusions
  FROM crm.aa_catalog_tour
 WHERE source_system = 'cis' AND source_id = ANY($1::text[])
"""


# ── Pure helpers ─────────────────────────────────────────────────────────────────────────────────


def _jsonb(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def clean_text(value: Any, field: str, *, required: bool = True, limit: int = MAX_TEXT) -> str | None:
    """A typed line: trimmed, NFC, one line (no control character), 1..``limit`` characters; empty -> None when
    optional."""
    if value is None or (isinstance(value, str) and not value.strip() and not required):
        if required:
            raise Invalid(f"invalid_{field}", f"1 to {limit} characters")
        return None
    if not isinstance(value, str):
        raise Invalid(f"invalid_{field}", f"1 to {limit} characters")
    text = " ".join(unicodedata.normalize("NFC", value).split())
    if not 1 <= len(text) <= limit or any(unicodedata.category(c) == "Cc" for c in text):
        raise Invalid(f"invalid_{field}", f"1 to {limit} characters, one line")
    return text


def kind_of(value: Any) -> str:
    if value not in KINDS:
        raise Invalid("invalid_kind", "included or not_included")
    return str(value)


def _uuid(value: Any, field: str) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except ValueError:
        raise Invalid(f"invalid_{field}", "an id") from None


def _aa_text(value: Any) -> str | None:
    """One CIS line as the client reads it (whitespace folded, at most 500 characters), None when empty."""
    if isinstance(value, dict):
        value = value.get("text")
    if not isinstance(value, str):
        return None
    text = "".join(c if unicodedata.category(c) != "Cc" else " " for c in unicodedata.normalize("NFC", value))
    text = " ".join(text.split())
    if not text:
        return None
    return text if len(text) <= AA_TEXT else text[: AA_TEXT - 1].rstrip() + "…"


def aa_lists(inclusions: Any, exclusions: Any) -> tuple[list[str], list[str]]:
    """The included / not included lines of one CIS tour body (``inclusions``: strings; ``exclusions``: objects with
    ``text``, or strings), empty and repeated lines dropped, at most :data:`MAX_AA_LINES` each."""

    def lines(raw: Any) -> list[str]:
        out: list[str] = []
        items = _jsonb(raw)
        for item in items if isinstance(items, list) else []:
            text = _aa_text(item)
            if text and text not in out:
                out.append(text)
            if len(out) == MAX_AA_LINES:
                break
        return out

    return lines(inclusions), lines(exclusions)


def tours_in(s: dict[str, Any], choice: dict[str, str | None]) -> list[str]:
    """The AA tours of the options ``choice`` names (block -> option), in block order, each once."""
    options = {str(o["option_id"]): o for o in s["options"]}
    tours: list[str] = []
    for b in s["blocks"]:
        o = options.get(str(choice.get(str(b["option_block_id"])) or ""))
        if o is not None and o["source"] == "aa" and o.get("aa_tour_source_id"):
            if o["aa_tour_source_id"] not in tours:
                tours.append(o["aa_tour_source_id"])
    return tours


def all_aa_tours(s: dict[str, Any]) -> list[str]:
    """Every AA tour among the version's options (Publish copies each one, whatever the client picks later)."""
    tours: list[str] = []
    for o in s["options"]:
        if o["source"] == "aa" and o.get("aa_tour_source_id") and o["aa_tour_source_id"] not in tours:
            tours.append(o["aa_tour_source_id"])
    return tours


def text_of(line: dict[str, Any], locale: str) -> str:
    """The line in ``locale`` (Vietnamese when the line has it), else English (the default)."""
    return str(line.get("text_vi") or line["text_en"]) if locale == "vi" else str(line["text_en"])


def _line_view(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "proposal_inclusion_id": str(r["proposal_inclusion_id"]),
        "kind": r["kind"],
        "source": r["source"],
        "inclusion_item_id": str(r["inclusion_item_id"]) if r.get("inclusion_item_id") else None,
        "text_en": r["text_en"],
        "text_vi": r.get("text_vi"),
    }


def _item_view(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "inclusion_item_id": str(r["inclusion_item_id"]),
        "kind": r["kind"],
        "text_en": r["text_en"],
        "text_vi": r.get("text_vi"),
        "sort_order": r["sort_order"],
        "archived": bool(r["archived"]),
    }


def agency_lists(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """The agency's lines of a version (library and custom), per kind, in their order."""
    out: dict[str, list[dict[str, Any]]] = {k: [] for k in KINDS}
    for r in sorted(rows, key=lambda r: (r["sort_order"], str(r["proposal_inclusion_id"]))):
        if r["source"] != "aa_tour":
            out[r["kind"]].append({"text_en": r["text_en"], "text_vi": r.get("text_vi")})
    return out


def aa_from_rows(rows: list[dict[str, Any]], tours: list[str]) -> list[dict[str, Any]]:
    """The copied AA tour lines of a published version, for the chosen ``tours`` (in that order)."""
    by_tour: dict[str, dict[str, Any]] = {}
    for r in sorted(rows, key=lambda r: (r["sort_order"], str(r["proposal_inclusion_id"]))):
        if r["source"] != "aa_tour":
            continue
        t = by_tour.setdefault(
            r["aa_tour_source_id"],
            {"source_id": r["aa_tour_source_id"], "title": r["aa_tour_title"], "included": [], "not_included": []},
        )
        t[r["kind"]].append(r["text_en"])
    return [by_tour[t] for t in tours if t in by_tour]


def hash_record(rows: list[dict[str, Any]]) -> list[list[Any]]:
    """The lines as the price hash covers them (what the client sees and accepts), sorted."""
    return sorted(
        [
            r["kind"],
            r["source"],
            str(r["inclusion_item_id"]) if r.get("inclusion_item_id") else None,
            r.get("aa_tour_source_id"),
            int(r["sort_order"]),
            r["text_en"],
            r.get("text_vi"),
        ]
        for r in rows
    )


def _refusal(exc: asyncpg.PostgresError) -> Exception:
    if isinstance(exc, asyncpg.UniqueViolationError):
        return Conflict("inclusion_item_exists", "the library already has this line")
    if isinstance(exc, asyncpg.CheckViolationError):
        return Invalid("invalid_inclusion_item", "check the kind, the texts and the order")
    return exc


# ── The library (every role reads; owner / agent write: the route's MONEY_FIELDS_EDIT) ─────────────


async def library(ctx: TenantContext, *, archived: bool = False) -> dict[str, Any]:
    rows = [dict(r) for r in await ctx.conn.fetch(LIBRARY_SQL, archived)]
    items = [_item_view(r) for r in rows]
    ctx.audit("list_view", "allowed", TABLE, None, object_ids=tuple(i["inclusion_item_id"] for i in items[:200]))
    return {"items": items, "kinds": list(KINDS), "max_items": MAX_ITEMS}


async def _one(ctx: TenantContext, item_id: uuid.UUID) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(ITEM_SQL, item_id)
    if row is None:
        raise NotFound("inclusion_item_not_found")
    return dict(row)


def _sort(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_SORT:
        raise Invalid("invalid_sort_order", f"0 to {MAX_SORT}")
    return value


async def create_item(ctx: TenantContext, body: dict[str, Any]) -> dict[str, Any]:
    kind = kind_of(body.get("kind"))
    text_en = clean_text(body.get("text_en"), "text_en")
    text_vi = clean_text(body.get("text_vi"), "text_vi", required=False)
    if int(await ctx.conn.fetchval(COUNT_SQL) or 0) >= MAX_ITEMS:
        raise Conflict("inclusion_library_full", f"the library holds at most {MAX_ITEMS} lines; delete unused ones")
    sort = _sort(body["sort_order"]) if body.get("sort_order") is not None else min(
        int(await ctx.conn.fetchval(NEXT_SORT_SQL, kind) or 0), MAX_SORT)  # fmt: skip
    item_id = uuid.uuid4()
    await ctx.conn.execute(ENSURE_SETTINGS_SQL)
    try:
        await ctx.conn.execute(INSERT_ITEM_SQL, item_id, kind, text_en, text_vi, sort)
    except asyncpg.PostgresError as exc:
        raise _refusal(exc) from None
    ctx.audit("create", "allowed", TABLE, str(item_id))
    return _item_view(await _one(ctx, item_id))


async def update_item(ctx: TenantContext, item_id: uuid.UUID, changes: dict[str, Any]) -> dict[str, Any]:
    row = await _one(ctx, item_id)
    kind = kind_of(changes["kind"]) if "kind" in changes else row["kind"]
    text_en = clean_text(changes["text_en"], "text_en") if "text_en" in changes else row["text_en"]
    text_vi = clean_text(changes["text_vi"], "text_vi", required=False) if "text_vi" in changes else row["text_vi"]
    sort = _sort(changes["sort_order"]) if "sort_order" in changes else row["sort_order"]
    archived = changes.get("archived", row["archived"])
    if not isinstance(archived, bool):
        raise Invalid("invalid_archived", "true or false")
    try:
        await ctx.conn.execute(UPDATE_ITEM_SQL, item_id, kind, text_en, text_vi, sort, archived)
    except asyncpg.PostgresError as exc:
        raise _refusal(exc) from None
    ctx.audit("update", "allowed", TABLE, str(item_id), fields=tuple(sorted(changes)))
    return _item_view(await _one(ctx, item_id))


async def delete_item(ctx: TenantContext, item_id: uuid.UUID) -> dict[str, Any]:
    """Delete a library line for good; versions that picked it keep their copy (no foreign key)."""
    if await ctx.conn.fetchval(DELETE_ITEM_SQL, item_id) is None:
        raise NotFound("inclusion_item_not_found")
    ctx.audit("delete", "allowed", TABLE, str(item_id))
    return {"inclusion_item_id": str(item_id), "deleted": True}


# ── A version's lines ────────────────────────────────────────────────────────────────────────────


async def lines(ctx: AnyContext, proposal_id: uuid.UUID) -> list[dict[str, Any]]:
    return [dict(r) for r in await ctx.conn.fetch(LINES_SQL, proposal_id)]


async def aa_live(ctx: AnyContext, tours: list[str]) -> list[dict[str, Any]]:
    """The AA tours' lists from the catalogue cache, in ``tours`` order (a tour the cache does not have is left
    out)."""
    if not tours:
        return []
    found = {r["source_id"]: dict(r) for r in await ctx.conn.fetch(AA_TOURS_SQL, tours)}
    out = []
    for t in tours:
        r = found.get(t)
        if r is None:
            continue
        included, not_included = aa_lists(r.get("inclusions"), r.get("exclusions"))
        out.append({"source_id": t, "title": r["title"], "included": included, "not_included": not_included})
    return out


async def aa_block(ctx: AnyContext, status: str, rows: list[dict[str, Any]], tours: list[str]) -> list[dict[str, Any]]:
    """The fixed AA block: the copy a published version made at Publish, else (a draft, or a version published before
    the copy existed) the catalogue cache now."""
    if status != "draft" and any(r["source"] == "aa_tour" for r in rows):
        return aa_from_rows(rows, tours)
    return await aa_live(ctx, tours)


async def builder_view(
    ctx: TenantContext, proposal_id: uuid.UUID, status: str, s: dict[str, Any], defaults: dict[str, str | None]
) -> dict[str, Any]:
    """The builder's panel: the version's own lines (with ids, for the picker), the AA block of the default options,
    and the library lines still offered (not archived)."""
    rows = await lines(ctx, proposal_id)
    offered = [_item_view(dict(r)) for r in await ctx.conn.fetch(LIBRARY_SQL, False)]
    own = [_line_view(r) for r in sorted(rows, key=lambda r: (KINDS.index(r["kind"]), r["sort_order"]))
           if r["source"] != "aa_tour"]  # fmt: skip
    return {
        "lines": own,
        "aa_tours": await aa_block(ctx, status, rows, tours_in(s, defaults)),
        "library": offered,
        "max_lines": MAX_LINES,
    }


async def replace(ctx: TenantContext, proposal_id: uuid.UUID, trip_id: uuid.UUID, entries: list[dict[str, Any]]) -> int:
    """The draft's own lines become ``entries`` in that order (per kind): ``{inclusion_item_id}`` picks a library
    line, ``{kind, text_en, text_vi?}`` types one. The caller holds the draft lock
    (:func:`app.proposals.builder._lock`)."""
    if len(entries) > MAX_LINES:
        raise Invalid("too_many_lines", f"at most {MAX_LINES} lines")
    current = {str(r["inclusion_item_id"]): r for r in await lines(ctx, proposal_id) if r.get("inclusion_item_id")}
    picked: list[uuid.UUID] = []
    for e in entries:
        if e.get("inclusion_item_id") is not None:
            item_id = _uuid(e["inclusion_item_id"], "inclusion_item_id")
            if item_id not in picked:
                picked.append(item_id)
    library_rows = {r["inclusion_item_id"]: dict(r) for r in await ctx.conn.fetch(ITEMS_SQL, picked)} if picked else {}
    planned: list[tuple[str, str, uuid.UUID | None, str, str | None]] = []
    seen: set[uuid.UUID] = set()
    for e in entries:
        if e.get("inclusion_item_id") is not None:
            item_id = _uuid(e["inclusion_item_id"], "inclusion_item_id")
            if item_id in seen:
                continue
            seen.add(item_id)
            kept = current.get(str(item_id))
            if kept is not None:  # already on the draft: its copy stays (the library line may be archived or edited)
                planned.append((kept["kind"], "library", item_id, kept["text_en"], kept["text_vi"]))
                continue
            item = library_rows.get(item_id)
            if item is None:
                raise NotFound("inclusion_item_not_found")
            if item["archived"]:
                raise Conflict("inclusion_item_archived", "this library line is archived")
            planned.append((item["kind"], "library", item_id, item["text_en"], item["text_vi"]))
        else:
            planned.append((
                kind_of(e.get("kind")), "custom", None, clean_text(e.get("text_en"), "text_en") or "",
                clean_text(e.get("text_vi"), "text_vi", required=False),
            ))  # fmt: skip
    await ctx.conn.execute(DELETE_AGENCY_LINES_SQL, proposal_id)
    order = dict.fromkeys(KINDS, 0)
    for kind, source, item_id, text_en, text_vi in planned:
        await ctx.conn.execute(INSERT_LINE_SQL, proposal_id, trip_id, kind, source, item_id, None, None, text_en,
                               text_vi, order[kind], None)  # fmt: skip
        order[kind] += 1
    return len(planned)


async def copy(ctx: TenantContext, source_id: uuid.UUID, proposal_id: uuid.UUID, trip_id: uuid.UUID) -> None:
    """A working copy keeps its base version's own lines (their copied texts); the AA copy is made again at its
    Publish."""
    for r in await lines(ctx, source_id):
        if r["source"] == "aa_tour":
            continue
        await ctx.conn.execute(
            INSERT_LINE_SQL, proposal_id, trip_id, r["kind"], r["source"], r["inclusion_item_id"], None, None,
            r["text_en"], r["text_vi"], r["sort_order"], r["proposal_inclusion_id"],
        )  # fmt: skip


async def snapshot_aa(ctx: TenantContext, proposal_id: uuid.UUID, trip_id: uuid.UUID, s: dict[str, Any]) -> int:
    """At Publish (still a draft): copy the lists of every AA tour among the version's options from the catalogue
    cache, so the published version keeps them. Returns the number of lines copied."""
    await ctx.conn.execute(DELETE_AA_LINES_SQL, proposal_id)
    count = 0
    for tour in await aa_live(ctx, all_aa_tours(s)):
        title = " ".join(str(tour["title"] or tour["source_id"]).split())[:300]
        for kind in KINDS:
            for order, text in enumerate(tour[kind]):
                await ctx.conn.execute(INSERT_LINE_SQL, proposal_id, trip_id, kind, "aa_tour", None,
                                       tour["source_id"], title, text, None, order, None)  # fmt: skip
                count += 1
    return count


async def client_view(ctx: AnyContext, proposal_id: uuid.UUID, status: str, tours: list[str]) -> dict[str, Any]:
    """What the client sees: the agency's two lists (both languages; the page shows its own, English when a line has
    no Vietnamese) and the AA tours' fixed lists for the chosen ``tours``. No id, no library detail."""
    rows = await lines(ctx, proposal_id)
    own = agency_lists(rows)
    return {"included": own["included"], "not_included": own["not_included"],
            "aa_tours": await aa_block(ctx, status, rows, tours)}  # fmt: skip
