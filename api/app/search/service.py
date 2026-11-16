"""The global search (A-00, design v3 §2.3 / §4.2; card CRM-32, R35) inside ``tenant_tx()``.

Order of one search:

1. the agency's switch ``crm.search.enabled`` (platform+tenant setting, off by default), read fresh and failing
   CLOSED (:func:`app.platform_settings.tenant_get_fail_closed`: a failed settings read means off): off -> 403
   ``search_not_enabled`` with a ``denied`` audit event;
2. the term (:mod:`app.search.query`): a partial e-mail or phone, or a term that folds to fewer letters than the
   minimum -> 422 before any read; a wildcard or an ``@`` inside a text term keeps the client kind out;
3. the kinds the member is entitled to (:mod:`app.search.kinds`: role-matrix action + feature key);
4. one bounded query per kind that answers the term's mode, at most ``crm.search.results_per_kind`` rows each, all
   under the agency's FORCE RLS (``tenant_tx``): no other agency's row can match.

Clients are found only through their blind indexes (e-mail, phone, whole name tokens); the candidates of a name are
confirmed after decrypting their display names (a 4-byte token index can collide; at agency scale it rarely does, see
:mod:`app.vault.blind_index` on what the index does and does not hide). No other field of a client is decrypted or
returned.

Every kind whose own list page writes a ``list_view`` audit event writes one here too, per search and per kind, naming
the records shown (reason ``global_search``): clients (``traveler``, field ``display_name``, like the directory), trips
(``trip``: titles are ``pii:travel``), booking lines (``booking_line``, like the ledger), suppliers
(``agency_supplier``), services (``agency_service``) and inclusion lines (``inclusion_item``). Message templates,
automations, tags and WhatsApp templates have no list audit on their own pages and get none here.

Nothing here reads, logs or returns the term.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from app import platform_settings as ps
from app.entitlements import feature_allowed
from app.errors import Forbidden
from app.search import fold
from app.search.kinds import Kind, entitled, features_needed
from app.search.query import Term, classify
from app.tenancy import TenantContext
from app.vault import Vault
from app.vault.blind_index import NormalisationError, name_tokens

# Name candidates read per client shown: a 4-byte token index can collide (rarely at agency scale, see
# app.vault.blind_index), a collision is dropped after decrypting, so a few more rows than the page shows are read.
CANDIDATES_PER_RESULT = 4  # technical-constant: headroom for blind-index collisions, not a policy value

CLIENT_SQL = """
SELECT traveler_id, display_name_enc, archived_at
  FROM crm.traveler
 WHERE deleted_at IS NULL
   AND ($1::bytea IS NULL OR email_bidx = $1)
   AND ($2::bytea IS NULL OR phone_bidx = $2)
   AND ($3::bytea[] IS NULL OR name_token_bidx @> $3)
 ORDER BY (archived_at IS NOT NULL), updated_at DESC, traveler_id
 LIMIT $4
"""

TRIP_SQL = f"""
SELECT trip_id, title, stage, start_date, month
  FROM crm.trip
 WHERE deleted_at IS NULL
   AND ({fold.all_words_sql("title", "$1")} OR lower(group_code) = lower($1))
 ORDER BY last_activity_at DESC, trip_id
 LIMIT $2
"""  # noqa: S608 — only fold.py expressions over fixed column names; the term is always $1
BOOKING_SQL = """
SELECT l.booking_line_id, l.trip_id, l.title, l.supplier_kind, l.status, t.title AS trip_title
  FROM crm.booking_line l
  JOIN crm.trip t ON t.trip_id = l.trip_id AND t.deleted_at IS NULL
 WHERE l.deleted_at IS NULL
   AND (lower(regexp_replace(btrim(l.confirmation_no), '[[:space:]]+', ' ', 'g')) = lower($1)
        OR lower(l.bk_booking_id) = lower($1) OR lower(l.bk_hold_id) = lower($1))
 ORDER BY l.updated_at DESC, l.booking_line_id
 LIMIT $2
"""
SUPPLIER_SQL = f"""
SELECT agency_supplier_id, name, kind, category
  FROM crm.agency_supplier
 WHERE deleted_at IS NULL AND {fold.contains_sql("name", "$1")}
 ORDER BY name, agency_supplier_id
 LIMIT $2
"""  # noqa: S608 — only fold.py expressions over fixed column names; the term is always $1
SERVICE_SQL = f"""
SELECT service_id, name, category, active
  FROM crm.agency_service
 WHERE deleted_at IS NULL AND {fold.contains_sql("name", "$1")}
 ORDER BY active DESC, name, service_id
 LIMIT $2
"""  # noqa: S608 — only fold.py expressions over fixed column names; the term is always $1
TEMPLATE_SQL = f"""
SELECT message_template_id, title, locale, source
  FROM crm.message_template
 WHERE deleted_at IS NULL AND {fold.contains_sql("title", "$1")}
 ORDER BY title, message_template_id
 LIMIT $2
"""  # noqa: S608 — only fold.py expressions over fixed column names; the term is always $1
WORKFLOW_SQL = f"""
SELECT workflow_rule_id, name, status
  FROM crm.workflow_rule
 WHERE deleted_at IS NULL AND {fold.contains_sql("name", "$1")}
 ORDER BY (status = 'archived'), name, workflow_rule_id
 LIMIT $2
"""  # noqa: S608 — only fold.py expressions over fixed column names; the term is always $1
INCLUSION_SQL = f"""
SELECT inclusion_item_id, kind, text_en, text_vi, archived
  FROM crm.inclusion_item
 WHERE deleted_at IS NULL
   AND ({fold.contains_sql("text_en", "$1")} OR {fold.contains_sql("coalesce(text_vi, '')", "$1")})
 ORDER BY archived, sort_order, inclusion_item_id
 LIMIT $2
"""  # noqa: S608 — only fold.py expressions over fixed column names; the term is always $1
TAG_SQL = f"""
SELECT tag_id, code, name, applies_to, tone
  FROM crm.tag
 WHERE deleted_at IS NULL AND archived_at IS NULL AND {fold.contains_sql("name", "$1")}
 ORDER BY name, tag_id
 LIMIT $2
"""  # noqa: S608 — only fold.py expressions over fixed column names; the term is always $1
WHATSAPP_TEMPLATE_SQL = f"""
SELECT channel_template_id, name, language, approval
  FROM crm.channel_template
 WHERE deleted_at IS NULL AND {fold.contains_sql("name", "$1")}
 ORDER BY name, channel_template_id
 LIMIT $2
"""  # noqa: S608 — only fold.py expressions over fixed column names; the term is always $1


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, dt.date) else None


def _item(id_: Any, label: str, **meta: Any) -> dict[str, Any]:
    return {
        "id": str(id_),
        "label": label,
        "meta": {k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in meta.items()},
    }


async def is_enabled(ctx: TenantContext) -> bool:
    """The agency's switch (its own approved value of ``crm.search.enabled``, else the platform value, else off), read
    now and failing closed: a settings read that fails answers off, never the last cached value."""
    return await ps.tenant_get_fail_closed(ctx.conn, ps.SEARCH_ENABLED, ctx.tenant_id, False) is True


async def kinds_for(ctx: TenantContext) -> list[Kind]:
    """The kinds this member may search in this agency (role matrix + the feature keys the agency holds)."""
    held = [f for f in features_needed() if await feature_allowed(ctx, f)]
    return entitled(ctx.member_role, held)


async def config(ctx: TenantContext) -> dict[str, Any]:
    """What the shell needs before it shows the search box (no term, no read of agency records)."""
    enabled = await is_enabled(ctx)
    return {
        "enabled": enabled,
        "kinds": [k.code for k in await kinds_for(ctx)] if enabled else [],
        "query_max_chars": int(ps.get(ps.SEARCH_QUERY_MAX_CHARS)),
        "min_text_chars": int(ps.get(ps.SEARCH_MIN_TEXT_CHARS)),
        "results_per_kind": int(ps.get(ps.SEARCH_RESULTS_PER_KIND)),
    }


async def search(ctx: TenantContext, vault: Vault, q: str) -> dict[str, Any]:
    if not await is_enabled(ctx):
        ctx.audit("search", "denied", "search", None, reason="search_not_enabled")
        raise Forbidden("search_not_enabled", "search is not switched on for this agency yet")
    term = classify(
        q,
        max_chars=int(ps.get(ps.SEARCH_QUERY_MAX_CHARS)),
        min_text_chars=int(ps.get(ps.SEARCH_MIN_TEXT_CHARS)),
    )
    kinds = await kinds_for(ctx)
    per_kind = int(ps.get(ps.SEARCH_RESULTS_PER_KIND))
    groups: list[dict[str, Any]] = []
    if term.mode != "too_short":
        for kind in kinds:
            if term.mode not in kind.modes or (kind.code == "client" and not term.clients):
                continue
            items = await _RUNNERS[kind.code](ctx, vault, term, per_kind)
            if items:
                _audit_shown(ctx, kind.code, items)
                groups.append({"kind": kind.code, "items": items})
    return {"mode": term.mode, "kinds": [k.code for k in kinds], "groups": groups, "notice": term.notice}


# The audit object kind of each search kind whose own list page writes a list_view (and the fields it names).
LIST_VIEW_AUDIT: dict[str, tuple[str, tuple[str, ...]]] = {
    "client": ("traveler", ("display_name",)),
    "trip": ("trip", ()),
    "booking": ("booking_line", ()),
    "supplier": ("agency_supplier", ()),
    "service": ("agency_service", ()),
    "inclusion": ("inclusion_item", ()),
}


def _audit_shown(ctx: TenantContext, kind: str, items: list[dict[str, Any]]) -> None:
    """One ``list_view`` per kind shown (reason ``global_search``), for the kinds of :data:`LIST_VIEW_AUDIT`."""
    audit = LIST_VIEW_AUDIT.get(kind)
    if audit is None:
        return
    object_kind, fields = audit
    ctx.audit(
        "list_view",
        "allowed",
        object_kind,
        None,
        fields=fields,
        reason="global_search",
        object_ids=tuple(i["id"] for i in items),
    )


# ── Clients: blind indexes only ──────────────────────────────────────────────────────────────────────
async def _clients(ctx: TenantContext, vault: Vault, term: Term, per_kind: int) -> list[dict[str, Any]]:
    email_idx = phone_idx = None
    tokens_idx: list[bytes] | None = None
    if term.mode == "email":
        email_idx = await vault.email_index("traveler", "email", term.text)
    elif term.mode == "phone":
        phone_idx = await vault.phone_index("traveler", "phone", term.text)
    elif term.name_tokens:
        tokens_idx = await vault.name_token_indexes("traveler", "name", term.text)
    else:
        return []
    rows = await ctx.conn.fetch(CLIENT_SQL, email_idx, phone_idx, tokens_idx, per_kind * CANDIDATES_PER_RESULT)
    wanted = set(term.name_tokens) if term.mode == "text" else set()
    items: list[dict[str, Any]] = []
    for r in rows:
        name = await vault.decrypt(
            "traveler", "display_name_enc", r["traveler_id"], r["display_name_enc"], tenant_id=ctx.tenant_id
        )
        if wanted and not wanted <= set(_tokens(name)):
            continue  # a 4-byte token index collision: not a match
        items.append(_item(r["traveler_id"], name, archived=r["archived_at"] is not None))
        if len(items) == per_kind:
            break
    return items


def _tokens(name: str) -> list[str]:
    try:
        return name_tokens(name)
    except NormalisationError:
        return []


# ── Plaintext kinds ──────────────────────────────────────────────────────────────────────────────────
async def _trips(ctx: TenantContext, _vault: Vault, term: Term, per_kind: int) -> list[dict[str, Any]]:
    rows = await ctx.conn.fetch(TRIP_SQL, term.text, per_kind)
    return [
        _item(r["trip_id"], r["title"], stage=r["stage"], start_date=_iso(r["start_date"]), month=_iso(r["month"]))
        for r in rows
    ]


async def _bookings(ctx: TenantContext, _vault: Vault, term: Term, per_kind: int) -> list[dict[str, Any]]:
    rows = await ctx.conn.fetch(BOOKING_SQL, term.text, per_kind)
    return [
        _item(
            r["booking_line_id"],
            r["title"],
            trip_id=r["trip_id"],
            trip_title=r["trip_title"],
            supplier_kind=r["supplier_kind"],
            status=r["status"],
        )
        for r in rows
    ]


async def _suppliers(ctx: TenantContext, _vault: Vault, term: Term, per_kind: int) -> list[dict[str, Any]]:
    rows = await ctx.conn.fetch(SUPPLIER_SQL, term.text, per_kind)
    return [_item(r["agency_supplier_id"], r["name"], kind=r["kind"], category=r["category"]) for r in rows]


async def _services(ctx: TenantContext, _vault: Vault, term: Term, per_kind: int) -> list[dict[str, Any]]:
    rows = await ctx.conn.fetch(SERVICE_SQL, term.text, per_kind)
    return [_item(r["service_id"], r["name"], category=r["category"], active=r["active"]) for r in rows]


async def _templates(ctx: TenantContext, _vault: Vault, term: Term, per_kind: int) -> list[dict[str, Any]]:
    rows = await ctx.conn.fetch(TEMPLATE_SQL, term.text, per_kind)
    return [_item(r["message_template_id"], r["title"], locale=r["locale"], source=r["source"]) for r in rows]


async def _workflows(ctx: TenantContext, _vault: Vault, term: Term, per_kind: int) -> list[dict[str, Any]]:
    rows = await ctx.conn.fetch(WORKFLOW_SQL, term.text, per_kind)
    return [_item(r["workflow_rule_id"], r["name"], status=r["status"]) for r in rows]


async def _inclusions(ctx: TenantContext, _vault: Vault, term: Term, per_kind: int) -> list[dict[str, Any]]:
    rows = await ctx.conn.fetch(INCLUSION_SQL, term.text, per_kind)
    return [
        _item(r["inclusion_item_id"], r["text_en"], text_vi=r["text_vi"], kind=r["kind"], archived=r["archived"])
        for r in rows
    ]


async def _tags(ctx: TenantContext, _vault: Vault, term: Term, per_kind: int) -> list[dict[str, Any]]:
    rows = await ctx.conn.fetch(TAG_SQL, term.text, per_kind)
    return [_item(r["tag_id"], r["name"], code=r["code"], applies_to=r["applies_to"], tone=r["tone"]) for r in rows]


async def _whatsapp_templates(ctx: TenantContext, _vault: Vault, term: Term, per_kind: int) -> list[dict[str, Any]]:
    rows = await ctx.conn.fetch(WHATSAPP_TEMPLATE_SQL, term.text, per_kind)
    return [_item(r["channel_template_id"], r["name"], language=r["language"], approval=r["approval"]) for r in rows]


Runner = Callable[[TenantContext, Vault, Term, int], Awaitable[list[dict[str, Any]]]]
_RUNNERS: dict[str, Runner] = {
    "client": _clients,
    "trip": _trips,
    "booking": _bookings,
    "supplier": _suppliers,
    "service": _services,
    "message_template": _templates,
    "workflow": _workflows,
    "inclusion": _inclusions,
    "tag": _tags,
    "whatsapp_template": _whatsapp_templates,
}
