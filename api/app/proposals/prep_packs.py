"""The "Before you go" day 0 of a builder proposal version (card CRM-22, AA-951; design v3 R38, F3.4, Q1 2.3; migration
crm-022).

- **Which packs**: AA prep packs (CIS AA-authored content, AA-977, cached in ``crm.aa_content``) whose ``applies_to``
  names a country or a tour of the trip's **AA items**: the AA options of this version and the trip's live AA booking
  lines, their tours read from the catalog cache (``crm.aa_catalog_tour``). A trip without AA items gets no suggestion
  and renders no pack (card E): the section then holds the agency's own day 0 notes and the reminders only.
- **The pick** (``PUT /proposals/{id}/before-you-go``, every role, draft only): the agent chooses packs by group key;
  each pick copies every locale CIS has of that group (``crm.prep_pack_selection``: content id + version, title,
  body, applies_to, hash), so a published or sent version keeps the text it went out with (CRM-B5 card E). A working
  copy copies its base's picks. Unknown group: 422 ``pack_not_found``.
- **Visa = the official link only** (Q1 2.3): :func:`sections` cuts a pack into sections; a section about visas keeps
  its heading and its ``https://`` links and nothing else (no advice text ever reaches the client from a pack).
- **Insurance reminder**: always (the CRM's own sentence, not advice). **Fitness warning**: when an AA item's tour has
  a challenging / strenuous intensity, or trekking / climbing / high-altitude activity or title words.

Pure helpers first (unit tested without a database), then the context functions (they never open one).
"""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.catalog import store as catalog
from app.errors import Conflict, Invalid, NotFound
from app.tenancy import TenantContext

MAX_PACKS = 12
LOCALES = ("en", "vi")

_VISA = re.compile(r"(?<![\w])(e-?visas?|visas?|thị thực)(?![\w])", re.IGNORECASE)
# A payment card is not a visa (CIS AA-977 validate: "Visa card", "Visa debit / credit", "Visa/Mastercard").
_CARD = re.compile(r"\bvisa\s*(card|debit|credit|/\s*master\s*card|/\s*mastercard)\b", re.IGNORECASE)
_MD_LINK = re.compile(r"\[([^\]]{0,200})\]\((https://[^\s)]{1,500})\)")
_BARE_URL = re.compile(r"(?<![(\w])https://[^\s)\]>]{1,500}")
_HEADING = re.compile(r"^#{1,6}\s+(.*)$")
_STRENUOUS = ("challenging", "strenuous")
_FITNESS_WORDS = re.compile(
    r"\b(trek\w*|hik\w*|climb\w*|mountaineer\w*|summit\w*|high[- ]altitude|altitude|base camp)\b", re.IGNORECASE
)

TOURS_SQL = """
SELECT c.source_id, c.title, c.country_code, c.body -> 'facets' AS facets
  FROM crm.aa_catalog_tour c
 WHERE c.source_system = 'cis'
   AND c.source_id IN (SELECT ao.aa_tour_source_id FROM crm.option ao
                        WHERE ao.proposal_id = $1 AND ao.source = 'aa' AND ao.aa_tour_source_id IS NOT NULL
                       UNION
                       SELECT bl.aa_tour_source_id FROM crm.booking_line bl
                        WHERE bl.trip_id = $2 AND bl.supplier_kind = 'aa' AND bl.status <> 'cancelled'
                          AND bl.deleted_at IS NULL AND bl.aa_tour_source_id IS NOT NULL)
 ORDER BY c.source_id
"""
AA_COUNT_SQL = """
SELECT (SELECT count(*) FROM crm.option ao WHERE ao.proposal_id = $1 AND ao.source = 'aa')
     + (SELECT count(*) FROM crm.booking_line bl WHERE bl.trip_id = $2 AND bl.supplier_kind = 'aa'
           AND bl.status <> 'cancelled' AND bl.deleted_at IS NULL)
"""
SELECTED_SQL = """
SELECT prep_pack_selection_id, group_key, locale, content_id, content_version, title, body_markdown, applies_to,
       content_hash, sort_order
  FROM crm.prep_pack_selection WHERE proposal_id = $1
 ORDER BY sort_order, group_key, locale
"""
DELETE_SQL = "DELETE FROM crm.prep_pack_selection WHERE proposal_id = $1"
INSERT_SQL = """
INSERT INTO crm.prep_pack_selection (proposal_id, trip_id, group_key, locale, content_id, content_version, title,
                                     body_markdown, applies_to, content_hash, sort_order, source_selection_id)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11, $12)
"""
AGENCY_LOCALE_SQL = "SELECT default_locale FROM crm.tenant LIMIT 1"
PROPOSAL_SQL = "SELECT trip_id, kind FROM crm.proposal WHERE proposal_id = $1 AND deleted_at IS NULL"


# ── Pure helpers ─────────────────────────────────────────────────────────────────────────────────


def ui_locale(value: str | None) -> str:
    """The text locale of an agency locale (``vi-VN`` -> vi; everything else en: English is the default)."""
    return "vi" if (value or "").lower().startswith("vi") else "en"


def _jsonb(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def is_visa_text(text: str) -> bool:
    clean = _CARD.sub(" ", unicodedata.normalize("NFC", text))
    return bool(_VISA.search(clean))


def links(text: str) -> list[tuple[str, str]]:
    """The ``https://`` links of a text, in order, without duplicates: (label, url)."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for label, url in _MD_LINK.findall(text):
        if url not in seen:
            seen.add(url)
            found.append((label.strip() or url, url))
    for url in _BARE_URL.findall(_MD_LINK.sub(" ", text)):
        url = url.rstrip(".,;:")
        if url not in seen:
            seen.add(url)
            found.append((url, url))
    return found


def plain(text: str) -> str:
    """Markdown of a pack to plain text: ``[label](url)`` -> ``label (url)``, emphasis and checkboxes dropped."""
    out = _MD_LINK.sub(lambda m: f"{m.group(1)} ({m.group(2)})" if m.group(1) != m.group(2) else m.group(2), text)
    out = re.sub(r"\*\*|__", "", out)
    out = re.sub(r"^(\s*)[-*]\s+\[[ xX]\]\s+", r"\1- ", out, flags=re.MULTILINE)
    return out.strip()


@dataclass(frozen=True)
class Section:
    heading: str | None
    text: str  # plain text; empty for a visa section
    visa: bool = False
    official_links: tuple[tuple[str, str], ...] = field(default_factory=tuple)


def sections(body_markdown: str) -> list[Section]:
    """A pack cut into sections (a Markdown heading starts one; without headings, blank-line paragraphs). A section
    about visas keeps only its heading and official links (Q1 2.3: visa = the official link only)."""
    body = unicodedata.normalize("NFC", body_markdown.replace("\r\n", "\n"))
    raw: list[tuple[str | None, list[str]]] = []
    if any(_HEADING.match(line) for line in body.split("\n")):
        heading: str | None = None
        lines: list[str] = []
        for line in body.split("\n"):
            m = _HEADING.match(line)
            if m:
                if heading is not None or any(x.strip() for x in lines):
                    raw.append((heading, lines))
                heading, lines = m.group(1).strip(), []
            else:
                lines.append(line)
        raw.append((heading, lines))
    else:
        raw = [(None, [p]) for p in re.split(r"\n\s*\n", body) if p.strip()]
    out: list[Section] = []
    for heading, lines in raw:
        text = "\n".join(lines).strip()
        if not text and heading is None:
            continue
        if is_visa_text(f"{heading or ''}\n{text}"):
            out.append(Section(heading, "", True, tuple(links(text))))
        else:
            out.append(Section(heading, plain(text)))
    return out


def relevant(packs: list[dict[str, Any]], countries: set[str], tours: set[str]) -> list[str]:
    """Group keys of the packs whose ``applies_to`` names one of the countries or tours (a pack for everywhere, an
    empty applies_to, is not suggested: it is not about this trip)."""
    keys: list[str] = []
    for p in packs:
        applies = _jsonb(p.get("applies_to")) or {}
        named_countries = set(applies.get("country_codes") or [])
        named_tours = {t.get("source_id") for t in applies.get("tours") or [] if isinstance(t, dict)}
        if (named_countries & countries or named_tours & tours) and p["group_key"] not in keys:
            keys.append(p["group_key"])
    return keys


def fitness_warning(tours: list[dict[str, Any]]) -> bool:
    for t in tours:
        facets = _jsonb(t.get("facets")) or {}
        if facets.get("intensity") in _STRENUOUS:
            return True
        words = " ".join([t.get("title") or "", *[str(a) for a in facets.get("activities") or []]])
        if _FITNESS_WORDS.search(words.replace("_", " ")):
            return True
    return False


def best(rows: list[dict[str, Any]], locale: str) -> dict[str, Any]:
    """The row of a group in ``locale``, else English, else the first."""
    for want in (locale, "en"):
        for r in rows:
            if r["locale"] == want:
                return r
    return rows[0]


def by_group(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault(r["group_key"], []).append(r)
    return groups


# ── With an open context ─────────────────────────────────────────────────────────────────────────


async def agency_locale(ctx: TenantContext) -> str:
    return ui_locale(await ctx.conn.fetchval(AGENCY_LOCALE_SQL))


async def selected(ctx: TenantContext, proposal_id: uuid.UUID) -> list[dict[str, Any]]:
    return [dict(r) for r in await ctx.conn.fetch(SELECTED_SQL, proposal_id)]


async def basis(ctx: TenantContext, proposal_id: uuid.UUID, trip_id: uuid.UUID) -> dict[str, Any]:
    """The trip's AA items as seen by the day 0: their count, countries, tours and the fitness flag."""
    tours = [dict(r) for r in await ctx.conn.fetch(TOURS_SQL, proposal_id, trip_id)]
    count = int(await ctx.conn.fetchval(AA_COUNT_SQL, proposal_id, trip_id) or 0)
    return {
        "aa_items": count,
        "countries": sorted({t["country_code"].strip() for t in tours if t.get("country_code")}),
        "tours": sorted(t["source_id"] for t in tours),
        "fitness_warning": fitness_warning(tours),
    }


async def view(
    ctx: TenantContext, proposal_id: uuid.UUID, trip_id: uuid.UUID, locale: str | None = None
) -> dict[str, Any]:
    """The day 0 for the builder page: the picks (one entry per group), the suggestions from the AA items, the
    reminders. Titles in the agency's locale (English when CIS has no copy in it)."""
    locale = locale or await agency_locale(ctx)
    b = await basis(ctx, proposal_id, trip_id)
    picks = by_group(await selected(ctx, proposal_id))
    suggested: list[dict[str, Any]] = []
    if b["aa_items"]:
        packs = await catalog.prep_packs(ctx)
        groups = by_group(packs)
        for key in relevant(packs, set(b["countries"]), set(b["tours"])):
            row = best(groups[key], locale)
            suggested.append({
                "group_key": key,
                "title": row["title"],
                "countries": (_jsonb(row["applies_to"]) or {}).get("country_codes") or [],
                "locales": sorted(r["locale"] for r in groups[key]),
                "selected": key in picks,
            })  # fmt: skip
    chosen = []
    for key, rows in picks.items():
        row = best(rows, locale)
        chosen.append({
            "group_key": key,
            "title": row["title"],
            "content_version": row["content_version"],
            "locales": sorted(r["locale"] for r in rows),
            "suggested": any(s["group_key"] == key for s in suggested),
        })  # fmt: skip
    return {
        "aa_items": b["aa_items"],
        "countries": b["countries"],
        "fitness_warning": b["fitness_warning"],
        "insurance_reminder": True,
        "selected": chosen,
        "suggested": suggested,
        "packs_not_chosen": bool(suggested) and not chosen,
    }


async def replace(ctx: TenantContext, proposal_id: uuid.UUID, trip_id: uuid.UUID, group_keys: list[str]) -> int:
    """The draft's picks become ``group_keys`` (in that order), every locale CIS has of each, copied from the cache.
    The caller holds the draft lock (:func:`app.proposals.builder._lock`)."""
    keys = list(dict.fromkeys(group_keys))
    if len(keys) > MAX_PACKS:
        raise Invalid("too_many_packs", f"at most {MAX_PACKS} packs")
    rows = by_group(await catalog.content_groups(ctx, "prep_pack", keys)) if keys else {}
    if keys and not (await basis(ctx, proposal_id, trip_id))["aa_items"]:
        # Card E: packs come from the AA items; a trip without one renders none.
        raise Invalid("no_aa_items", "add an AA item first: the packs follow the trip's AA destinations")
    missing = [k for k in keys if k not in rows]
    if missing:
        raise Invalid("pack_not_found", "a pack is not published (any more)", group_keys=missing)
    await ctx.conn.execute(DELETE_SQL, proposal_id)
    for order, key in enumerate(keys):
        for r in rows[key]:
            await ctx.conn.execute(
                INSERT_SQL, proposal_id, trip_id, key, r["locale"], uuid.UUID(str(r["content_id"])), r["version"],
                r["title"], r["body_markdown"], json.dumps(_jsonb(r["applies_to"]) or {}), r["content_hash"], order,
                None,
            )  # fmt: skip
    return len(keys)


async def copy(ctx: TenantContext, source_id: uuid.UUID, proposal_id: uuid.UUID, trip_id: uuid.UUID) -> None:
    """A working copy keeps its base version's packs (the pinned versions, not the cache's latest)."""
    for r in await selected(ctx, source_id):
        await ctx.conn.execute(
            INSERT_SQL, proposal_id, trip_id, r["group_key"], r["locale"], r["content_id"], r["content_version"],
            r["title"], r["body_markdown"], json.dumps(_jsonb(r["applies_to"]) or {}), r["content_hash"],
            r["sort_order"], r["prep_pack_selection_id"],
        )  # fmt: skip


async def for_proposal(ctx: TenantContext, proposal_id: uuid.UUID) -> dict[str, Any]:
    """``GET /proposals/{id}/before-you-go``: :func:`view` of a builder version (any status)."""
    row = await ctx.conn.fetchrow(PROPOSAL_SQL, proposal_id)
    if row is None:
        raise NotFound("proposal_not_found")
    if row["kind"] != "builder":
        raise Conflict("proposal_not_builder", "a text version has no day 0")
    out = await view(ctx, proposal_id, row["trip_id"])
    ctx.audit("view", "allowed", "proposal", str(proposal_id), fields=("before_you_go",))
    return {"proposal_id": str(proposal_id), **out}
