"""Clients (A-06 directory, A-07 profile) and the party of a trip (A-03): queries and edits inside ``tenant_tx()``.

Personal data rules (design v3 §4.4, Fix B15, Decision D7):

- the **directory** and its search results decrypt ``display_name`` only; one page load writes **one**
  ``audit_event`` (``list_view``, field ``display_name``, the ids shown); no e-mail, no phone, no partial digits;
- search is exact only (R35): an e-mail or a phone goes through its blind index, a name through the 4-byte token
  indexes (lossy on purpose) and the candidates are confirmed after decrypting their display names;
- the **profile** decrypts the display names of the traveller and of the related travellers, and the document file
  names, and writes one ``view`` event naming those fields; every other F1 field is click-to-reveal
  (:mod:`app.travellers.reveal`);
- F2 (``crm.traveler_profile``) is reached only through the encrypted pointer ``traveler.profile_token_enc``.

Writes: create (an e-mail or phone already known -> 409 ``duplicate_traveler`` with the existing id, R6), F1 edit
(encrypted values, blind indexes kept in step, the age bands recomputed only when the DOB changed), the F2 profile
codes, health codes (only with a ``health_data`` consent; dietary and mobility codes mirrored to F2), relationships
(R7: the database writes the inverse) and the party of a trip (twins ``trip_traveler`` / ``trip_party`` in one
transaction; a companion needs a name only, Fix E5).
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

import asyncpg

from app.audit import MAX_OBJECT_IDS
from app.errors import Conflict, Invalid, NotFound
from app.tenancy import TenantContext
from app.travellers import age_bands
from app.travellers.fields import (
    CHANNELS,
    EDITABLE_F1,
    LEAD_SOURCE,
    PROFILE_FIELDS,
    RELATION_KINDS,
    REVEAL_COLUMNS,
    clean_f1,
    clean_health,
    clean_profile,
)
from app.vault import Vault
from app.vault.blind_index import NormalisationError, name_tokens

PAGE_SIZE = 50
MAX_PAGE = 200
TRASH_DAYS = 7

LIST_SQL = """
SELECT tr.traveler_id, tr.display_name_enc, tr.owner_member_id, tr.lead_source, tr.archived_at, tr.created_at,
       tr.email_enc IS NOT NULL AS has_email, tr.phone_enc IS NOT NULL AS has_phone,
       (SELECT count(*) FROM crm.trip_traveler tt JOIN crm.trip t ON t.trip_id = tt.trip_id AND t.deleted_at IS NULL
         WHERE tt.traveler_id = tr.traveler_id AND tt.deleted_at IS NULL)::int AS trips
  FROM crm.traveler tr
 WHERE tr.deleted_at IS NULL
   AND (tr.archived_at IS NOT NULL) = $1
   AND ($2::uuid IS NULL OR tr.owner_member_id = $2)
   AND ($3::bytea IS NULL OR tr.email_bidx = $3)
   AND ($4::bytea IS NULL OR tr.phone_bidx = $4)
   AND ($5::bytea[] IS NULL OR tr.name_token_bidx @> $5)
 ORDER BY tr.created_at DESC, tr.traveler_id
 LIMIT $6 OFFSET $7
"""
TRAVELER_SQL = """
SELECT traveler_id, display_name_enc, given_name_enc, family_name_enc, email_enc, phone_enc, dob_enc, nationality_enc,
       passport_no_enc, passport_expiry_enc, passport_country_enc, emergency_contact_enc, profile_token_enc,
       preferred_channel, owner_member_id, lead_source, referrer_traveler_id, archived_at, archive_reason, created_at
  FROM crm.traveler WHERE traveler_id = $1 AND deleted_at IS NULL
"""
CONSENT_SQL = """
SELECT DISTINCT ON (purpose) purpose, value, at, source FROM crm.consent
 WHERE traveler_id = $1 ORDER BY purpose, at DESC, consent_id
"""
PROFILE_SQL = """
SELECT traveler_token, residence_country, budget_band, pace, group_pref, fitness_band, trek_experience, altitude_ok,
       accommodation_tier, interests, must_do_codes, must_avoid_codes, occasion, dietary_flags, mobility_flags,
       budget_flex, decision_maker, booking_timeline, competing_quotes, repeat_count, stage2_completeness, marketing_ok
  FROM crm.traveler_profile WHERE traveler_token = $1 AND deleted_at IS NULL
"""
RELATIONS_SQL = """
SELECT r.relation_id, r.kind, r.to_traveler_id, o.display_name_enc,
       (SELECT count(*) FROM crm.trip_traveler a JOIN crm.trip_traveler b ON b.trip_id = a.trip_id
         WHERE a.traveler_id = r.from_traveler_id AND b.traveler_id = r.to_traveler_id
           AND a.deleted_at IS NULL AND b.deleted_at IS NULL)::int AS trips_together
  FROM crm.traveler_relation r JOIN crm.traveler o ON o.traveler_id = r.to_traveler_id AND o.deleted_at IS NULL
 WHERE r.from_traveler_id = $1 AND r.deleted_at IS NULL
 ORDER BY r.kind, r.created_at, r.relation_id
"""
TRIPS_SQL = """
SELECT t.trip_id, t.title, t.stage, t.waiting_on, t.month, t.start_date, tt.role
  FROM crm.trip_traveler tt JOIN crm.trip t ON t.trip_id = tt.trip_id AND t.deleted_at IS NULL
 WHERE tt.traveler_id = $1 AND tt.deleted_at IS NULL
 ORDER BY coalesce(t.start_date, t.month, t.created_at::date) DESC, t.trip_id
"""
BANDS_SQL = """
SELECT trip_id, age_band_at_departure FROM crm.trip_party WHERE traveler_token = $1 AND deleted_at IS NULL
"""
DOCUMENTS_SQL = """
SELECT document_id, trip_id, kind, size_bytes, content_type, filename_enc, visible_to_travelers, write_only, created_at
  FROM crm.document WHERE traveler_id = $1 AND deleted_at IS NULL ORDER BY created_at DESC, document_id
"""
HAS_HEALTH_SQL = "SELECT EXISTS (SELECT 1 FROM crm.traveler_health WHERE traveler_id = $1 AND deleted_at IS NULL)"
EXISTS_SQL = "SELECT 1 FROM crm.traveler WHERE traveler_id = $1 AND deleted_at IS NULL"
BY_EMAIL_SQL = "SELECT traveler_id FROM crm.traveler WHERE email_bidx = $1 AND deleted_at IS NULL AND traveler_id <> $2"
BY_PHONE_SQL = "SELECT traveler_id FROM crm.traveler WHERE phone_bidx = $1 AND deleted_at IS NULL AND traveler_id <> $2"
INSERT_PROFILE_SQL = "INSERT INTO crm.traveler_profile (traveler_token) VALUES ($1)"
INSERT_TRAVELER_SQL = """
INSERT INTO crm.traveler (traveler_id, display_name_enc, name_bidx, name_token_bidx, given_name_enc, family_name_enc,
                          email_enc, email_bidx, phone_enc, phone_bidx, preferred_channel, profile_token_enc,
                          owner_member_id, lead_source)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
"""
LATEST_HEALTH_CONSENT_SQL = """
SELECT consent_id, value FROM crm.consent WHERE traveler_id = $1 AND purpose = 'health_data'
 ORDER BY at DESC, consent_id LIMIT 1
"""
INSERT_CONSENT_SQL = """
INSERT INTO crm.consent (traveler_id, purpose, value, source) VALUES ($1, 'health_data', true, 'agent_recorded')
RETURNING consent_id
"""
HEALTH_ROW_SQL = "SELECT health_id FROM crm.traveler_health WHERE traveler_id = $1 AND deleted_at IS NULL FOR UPDATE"
INSERT_HEALTH_SQL = """
INSERT INTO crm.traveler_health (health_id, traveler_id, health_flags_enc, consent_id) VALUES ($1, $2, $3, $4)
"""
UPDATE_HEALTH_SQL = "UPDATE crm.traveler_health SET health_flags_enc = $2, consent_id = $3 WHERE health_id = $1"
MIRROR_HEALTH_SQL = "UPDATE crm.traveler_profile SET dietary_flags = $2, mobility_flags = $3 WHERE traveler_token = $1"
INSERT_RELATION_SQL = """
INSERT INTO crm.traveler_relation (from_traveler_id, to_traveler_id, kind) VALUES ($1, $2, $3) RETURNING relation_id
"""
RELATION_SQL = """
SELECT relation_id, from_traveler_id, to_traveler_id, kind FROM crm.traveler_relation
 WHERE relation_id = $1 AND from_traveler_id = $2 AND deleted_at IS NULL
"""
TRASH_RELATION_SQL = """
UPDATE crm.traveler_relation SET deleted_at = now(), deleted_by = $2, purge_after = now() + make_interval(days => $3)
 WHERE relation_id = $1
"""
# The explicit relation wins over a referrer the intake recorded earlier (the column then names the same person as
# the one live referrer relation, which traveler_relation_one_referrer_uq keeps unique).
SET_REFERRER_SQL = """
UPDATE crm.traveler SET referrer_traveler_id = $2
 WHERE traveler_id = $1 AND referrer_traveler_id IS DISTINCT FROM $2
"""
CLEAR_REFERRER_SQL = """
UPDATE crm.traveler SET referrer_traveler_id = NULL WHERE traveler_id = $1 AND referrer_traveler_id = $2
"""
TRIP_SQL = "SELECT trip_id, start_date FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL FOR UPDATE"
PARTY_ROW_SQL = """
SELECT trip_traveler_id, role FROM crm.trip_traveler WHERE trip_id = $1 AND traveler_id = $2 AND deleted_at IS NULL
"""
INSERT_PARTY_SQL = "INSERT INTO crm.trip_traveler (trip_id, traveler_id, role) VALUES ($1, $2, 'companion')"
INSERT_PARTY_F2_SQL = """
INSERT INTO crm.trip_party (trip_id, traveler_token, role) VALUES ($1, $2, 'companion') RETURNING trip_party_id
"""
DELETE_PARTY_SQL = "DELETE FROM crm.trip_traveler WHERE trip_traveler_id = $1"
DELETE_PARTY_F2_SQL = "DELETE FROM crm.trip_party WHERE trip_id = $1 AND traveler_token = $2"
PROFILE_POINTER_SQL = "UPDATE crm.traveler SET profile_token_enc = $2 WHERE traveler_id = $1"


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _id(value: Any) -> str | None:
    return str(value) if value is not None else None


# ── A-06 directory ─────────────────────────────────────────────────────────────────────────────────
async def directory(
    ctx: TenantContext,
    vault: Vault,
    *,
    q: str | None = None,
    archived: bool = False,
    owner: str = "anyone",
    page: int = 1,
) -> dict[str, Any]:
    if not 1 <= page <= MAX_PAGE:
        raise Invalid("invalid_page", f"1 to {MAX_PAGE}")
    email_idx = phone_idx = None
    tokens_idx: list[bytes] | None = None
    wanted: list[str] = []
    query = (q or "").strip()
    search = "none"
    try:
        if not query:
            pass
        elif "@" in query:
            email_idx, search = await vault.email_index("traveler", "email", query), "email"
        elif query.lstrip("+").replace(" ", "").replace("-", "").isdigit():
            phone_idx, search = await vault.phone_index("traveler", "phone", query), "phone"
        else:
            wanted = name_tokens(query)
            tokens_idx, search = await vault.name_token_indexes("traveler", "name", query), "name"
    except NormalisationError:
        raise Invalid("invalid_query", "an exact e-mail, an international phone (+…) or a name") from None
    owner_id = ctx.member_id if owner == "me" else None
    rows = await ctx.conn.fetch(
        LIST_SQL, archived, owner_id, email_idx, phone_idx, tokens_idx, PAGE_SIZE + 1, (page - 1) * PAGE_SIZE
    )
    more = len(rows) > PAGE_SIZE
    items = []
    for r in rows[:PAGE_SIZE]:
        name = await vault.decrypt(
            "traveler", "display_name_enc", r["traveler_id"], r["display_name_enc"], tenant_id=ctx.tenant_id
        )
        if wanted and not set(wanted) <= set(_tokens(name)):
            continue  # a token index collision (4 bytes, lossy on purpose): not a match
        items.append(
            {
                "traveler_id": str(r["traveler_id"]),
                "display_name": name,
                "owner_member_id": _id(r["owner_member_id"]),
                "is_mine": r["owner_member_id"] == ctx.member_id,
                "lead_source": r["lead_source"],
                "archived": r["archived_at"] is not None,
                "has_email": r["has_email"],
                "has_phone": r["has_phone"],
                "trips": r["trips"],
                "created_at": _iso(r["created_at"]),
            }
        )
    if items:
        ctx.audit(
            "list_view",
            "allowed",
            "traveler",
            None,
            fields=("display_name",),
            reason="client_search" if search != "none" else "client_list",
            object_ids=tuple(i["traveler_id"] for i in items),
        )
    return {
        "items": items,
        "page": page,
        "has_more": more,
        "filter": {"q": query or None, "search": search, "archived": archived, "owner": owner},
    }


def _tokens(name: str) -> list[str]:
    try:
        return name_tokens(name)
    except NormalisationError:
        return []


# ── A-07 profile ───────────────────────────────────────────────────────────────────────────────────
async def profile(ctx: TenantContext, vault: Vault, traveler_id: uuid.UUID) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(TRAVELER_SQL, traveler_id)
    if row is None:
        raise NotFound("traveler_not_found")
    name = await vault.decrypt(
        "traveler", "display_name_enc", traveler_id, row["display_name_enc"], tenant_id=ctx.tenant_id
    )
    token = await _token(vault, traveler_id, row["profile_token_enc"])
    f2 = await ctx.conn.fetchrow(PROFILE_SQL, token) if token else None
    bands = {r["trip_id"]: r["age_band_at_departure"] for r in await ctx.conn.fetch(BANDS_SQL, token)} if token else {}
    relations = []
    for r in await ctx.conn.fetch(RELATIONS_SQL, traveler_id):
        relations.append(
            {
                "relation_id": str(r["relation_id"]),
                "kind": r["kind"],
                "traveler_id": str(r["to_traveler_id"]),
                "display_name": await vault.decrypt(
                    "traveler", "display_name_enc", r["to_traveler_id"], r["display_name_enc"], tenant_id=ctx.tenant_id
                ),
                "trips_together": r["trips_together"],
            }
        )
    trips = [
        {
            "trip_id": str(r["trip_id"]),
            "title": r["title"],
            "stage": r["stage"],
            "waiting_on": r["waiting_on"],
            "month": _iso(r["month"]),
            "start_date": _iso(r["start_date"]),
            "role": r["role"],
            "age_band_at_departure": bands.get(r["trip_id"]),
        }
        for r in await ctx.conn.fetch(TRIPS_SQL, traveler_id)
    ]
    documents = []
    for r in await ctx.conn.fetch(DOCUMENTS_SQL, traveler_id):
        documents.append(
            {
                "document_id": str(r["document_id"]),
                "trip_id": _id(r["trip_id"]),
                "kind": r["kind"],
                "filename": await vault.decrypt("document", "filename_enc", r["document_id"], r["filename_enc"]),
                "size_bytes": r["size_bytes"],
                "content_type": r["content_type"],
                "visible_to_travelers": r["visible_to_travelers"],
                "write_only": r["write_only"],
                "created_at": _iso(r["created_at"]),
            }
        )
    consent = {
        r["purpose"]: {"value": r["value"], "at": _iso(r["at"]), "source": r["source"]}
        for r in await ctx.conn.fetch(CONSENT_SQL, traveler_id)
    }
    fields = ["display_name"]
    if relations:
        fields.append("relation_display_name")
    if documents:
        fields.append("document_filename")
    # Design §4.4: when several identities are shown, the event names them (related travellers' names, document
    # file names) besides the profile's own id.
    shown = tuple(dict.fromkeys([r["traveler_id"] for r in relations] + [d["document_id"] for d in documents]))
    ctx.audit("view", "allowed", "traveler", str(traveler_id), fields=tuple(fields), object_ids=shown[:MAX_OBJECT_IDS])
    return {
        "traveler": {
            "traveler_id": str(traveler_id),
            "display_name": name,
            "preferred_channel": row["preferred_channel"],
            "owner_member_id": _id(row["owner_member_id"]),
            "is_mine": row["owner_member_id"] == ctx.member_id,
            "lead_source": row["lead_source"],
            "referrer_traveler_id": _id(row["referrer_traveler_id"]),
            "archived": row["archived_at"] is not None,
            "archive_reason": row["archive_reason"],
            "created_at": _iso(row["created_at"]),
            # Which F1 fields hold a value (shown masked, click to reveal); never the value or part of it.
            "filled": {field: row[column] is not None for field, column in REVEAL_COLUMNS.items()},
            "has_health": bool(await ctx.conn.fetchval(HAS_HEALTH_SQL, traveler_id)),
        },
        "profile": _profile(f2),
        "consent": consent,
        "relations": relations,
        "trips": trips,
        "documents": documents,
    }


def _profile(r: Any) -> dict[str, Any] | None:
    if r is None:
        return None
    out = {k: r[k] for k in PROFILE_FIELDS}
    for k in ("interests", "must_do_codes", "must_avoid_codes", "dietary_flags", "mobility_flags"):
        out[k] = list(r[k] or ())
    out.update(
        repeat_count=r["repeat_count"], stage2_completeness=r["stage2_completeness"], marketing_ok=r["marketing_ok"]
    )
    return out


async def _token(vault: Vault, traveler_id: uuid.UUID, blob: bytes | None) -> uuid.UUID | None:
    if blob is None:
        return None
    return uuid.UUID(await vault.decrypt("traveler", "profile_token_enc", traveler_id, blob))


async def _ensure_token(ctx: TenantContext, vault: Vault, traveler_id: uuid.UUID, blob: bytes | None) -> uuid.UUID:
    token = await _token(vault, traveler_id, blob)
    if token is None:
        token = uuid.uuid4()
        await ctx.conn.execute(INSERT_PROFILE_SQL, token)
        pointer = await vault.encrypt("traveler", "profile_token_enc", traveler_id, str(token))
        await ctx.conn.execute(PROFILE_POINTER_SQL, traveler_id, pointer)
    return token


# ── create / edit ──────────────────────────────────────────────────────────────────────────────────
async def create_traveler(ctx: TenantContext, vault: Vault, data: dict[str, Any]) -> dict[str, Any]:
    clean = {f: clean_f1(f, data.get(f)) for f in ("display_name", "given_name", "family_name", "email", "phone")}
    channel = data.get("preferred_channel")
    if channel is not None and channel not in CHANNELS:
        raise Invalid("invalid_preferred_channel", ", ".join(CHANNELS))
    lead_source = data.get("lead_source") or "agent_created"
    if not LEAD_SOURCE.match(lead_source):
        raise Invalid("invalid_lead_source", "a code")
    traveler_id = uuid.uuid4()
    email_idx = await vault.email_index("traveler", "email", clean["email"]) if clean["email"] else None
    phone_idx = await vault.phone_index("traveler", "phone", clean["phone"]) if clean["phone"] else None
    await _refuse_duplicate(ctx, traveler_id, email_idx, phone_idx)
    token = uuid.uuid4()
    await ctx.conn.execute(INSERT_PROFILE_SQL, token)

    async def enc(field: str) -> bytes | None:
        value = clean[field]
        return await vault.encrypt("traveler", f"{field}_enc", traveler_id, value) if value is not None else None

    name = clean["display_name"] or ""
    await ctx.conn.execute(
        INSERT_TRAVELER_SQL,
        traveler_id,
        await enc("display_name"),
        await vault.name_index("traveler", "name", name),
        await vault.name_token_indexes("traveler", "name", name),
        await enc("given_name"),
        await enc("family_name"),
        await enc("email"),
        email_idx,
        await enc("phone"),
        phone_idx,
        channel,
        await vault.encrypt("traveler", "profile_token_enc", traveler_id, str(token)),
        ctx.member_id,
        lead_source,
    )
    ctx.audit("create", "allowed", "traveler", str(traveler_id), fields=tuple(f for f, v in clean.items() if v))
    return {"traveler_id": str(traveler_id), "display_name": name}


async def _refuse_duplicate(
    ctx: TenantContext, traveler_id: uuid.UUID, email_idx: bytes | None, phone_idx: bytes | None
) -> None:
    for sql, idx in ((BY_EMAIL_SQL, email_idx), (BY_PHONE_SQL, phone_idx)):
        if idx is None:
            continue
        existing = await ctx.conn.fetchval(sql, idx, traveler_id)
        if existing is not None:
            raise Conflict("duplicate_traveler", "a client with this e-mail or phone exists", traveler_id=str(existing))


async def update_traveler(
    ctx: TenantContext, vault: Vault, traveler_id: uuid.UUID, changes: dict[str, Any]
) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(TRAVELER_SQL + " FOR UPDATE", traveler_id)
    if row is None:
        raise NotFound("traveler_not_found")
    sets: list[str] = []
    args: list[Any] = [traveler_id]

    def put(column: str, value: Any) -> None:
        args.append(value)
        sets.append(f"{column} = ${len(args)}")

    touched: list[str] = []
    new_dob: dt.date | None = None
    dob_changed = False
    for field, raw in changes.items():
        if field == "preferred_channel":
            if raw is not None and raw not in CHANNELS:
                raise Invalid("invalid_preferred_channel", ", ".join(CHANNELS))
            put("preferred_channel", raw)
            touched.append(field)
            continue
        if field not in EDITABLE_F1:
            raise Invalid("invalid_field", f"{field} is not an editable field")
        value = clean_f1(field, raw)
        column = f"{field}_enc"
        put(column, await vault.encrypt("traveler", column, traveler_id, value) if value is not None else None)
        touched.append(field)
        if field == "display_name":
            put("name_bidx", await vault.name_index("traveler", "name", value or ""))
            put("name_token_bidx", await vault.name_token_indexes("traveler", "name", value or ""))
        elif field == "email":
            idx = await vault.email_index("traveler", "email", value) if value else None
            await _refuse_duplicate(ctx, traveler_id, idx, None)
            put("email_bidx", idx)
        elif field == "phone":
            idx = await vault.phone_index("traveler", "phone", value) if value else None
            await _refuse_duplicate(ctx, traveler_id, None, idx)
            put("phone_bidx", idx)
        elif field == "dob":
            new_dob = dt.date.fromisoformat(value) if value else None
            old = row["dob_enc"]
            old_dob = (
                dt.date.fromisoformat(await vault.decrypt("traveler", "dob_enc", traveler_id, old)) if old else None
            )
            dob_changed = old_dob != new_dob
    if not sets:
        raise Invalid("nothing_to_change")
    await ctx.conn.execute(
        f"UPDATE crm.traveler SET {', '.join(sets)} WHERE traveler_id = $1",  # noqa: S608 — fixed column names
        *args,
    )
    ctx.audit("update", "allowed", "traveler", str(traveler_id), fields=tuple(sorted(touched)))
    recomputed = await age_bands.recompute_for_traveler(ctx, vault, traveler_id, new_dob) if dob_changed else 0
    return {"traveler_id": str(traveler_id), "updated": sorted(touched), "age_bands_changed": recomputed}


async def update_profile(
    ctx: TenantContext, vault: Vault, traveler_id: uuid.UUID, changes: dict[str, Any]
) -> dict[str, Any]:
    clean = clean_profile(changes)
    if not clean:
        raise Invalid("nothing_to_change")
    row = await ctx.conn.fetchrow(TRAVELER_SQL, traveler_id)
    if row is None:
        raise NotFound("traveler_not_found")
    token = await _ensure_token(ctx, vault, traveler_id, row["profile_token_enc"])
    names = sorted(clean)
    sql = ", ".join(f"{name} = ${i + 2}" for i, name in enumerate(names))
    await ctx.conn.execute(
        f"UPDATE crm.traveler_profile SET {sql} WHERE traveler_token = $1",  # noqa: S608 — names from PROFILE_FIELDS
        token,
        *[clean[n] for n in names],
    )
    ctx.audit("update", "allowed", "traveler_profile", str(traveler_id), fields=tuple(names))
    return {"traveler_id": str(traveler_id), "profile": _profile(await ctx.conn.fetchrow(PROFILE_SQL, token))}


async def set_health(
    ctx: TenantContext,
    vault: Vault,
    traveler_id: uuid.UUID,
    *,
    dietary: Any,
    mobility: Any,
    medical: Any,
    consent_recorded: bool,
) -> dict[str, Any]:
    """Health codes (F1) with consent (design §4.3 "health is stored as F1 codes with consent and mirrored as F2
    flags"). ``consent_recorded``: the member records that the traveller gave the health_data consent now."""
    codes = clean_health(dietary, mobility, medical)
    row = await ctx.conn.fetchrow(TRAVELER_SQL, traveler_id)
    if row is None:
        raise NotFound("traveler_not_found")
    if consent_recorded:
        consent_id = await ctx.conn.fetchval(INSERT_CONSENT_SQL, traveler_id)
    else:
        latest = await ctx.conn.fetchrow(LATEST_HEALTH_CONSENT_SQL, traveler_id)
        if latest is None or not latest["value"]:
            raise Invalid("consent_required", "record the traveller's health data consent first")
        consent_id = latest["consent_id"]
    health_id = await ctx.conn.fetchval(HEALTH_ROW_SQL, traveler_id)
    new = health_id is None
    health_id = health_id or uuid.uuid4()
    blob = await vault.encrypt("traveler_health", "health_flags_enc", health_id, json.dumps(codes, sort_keys=True))
    if new:
        await ctx.conn.execute(INSERT_HEALTH_SQL, health_id, traveler_id, blob, consent_id)
    else:
        await ctx.conn.execute(UPDATE_HEALTH_SQL, health_id, blob, consent_id)
    token = await _ensure_token(ctx, vault, traveler_id, row["profile_token_enc"])
    await ctx.conn.execute(MIRROR_HEALTH_SQL, token, codes["dietary"], codes["mobility"])
    ctx.audit("update", "allowed", "traveler", str(traveler_id), fields=("health",))
    return {
        "traveler_id": str(traveler_id),
        "has_health": True,
        "dietary_flags": codes["dietary"],
        "mobility_flags": codes["mobility"],
    }


# ── relationships (R7) ─────────────────────────────────────────────────────────────────────────────
async def add_relation(
    ctx: TenantContext, traveler_id: uuid.UUID, to_traveler_id: uuid.UUID, kind: str
) -> dict[str, Any]:
    if kind not in RELATION_KINDS:
        raise Invalid("invalid_kind", ", ".join(RELATION_KINDS))
    if traveler_id == to_traveler_id:
        raise Invalid("same_traveler", "a client cannot be related to themselves")
    for tid in (traveler_id, to_traveler_id):
        if not await ctx.conn.fetchval(EXISTS_SQL, tid):
            raise NotFound("traveler_not_found")
    try:
        async with ctx.conn.transaction():
            relation_id = await ctx.conn.fetchval(INSERT_RELATION_SQL, traveler_id, to_traveler_id, kind)
    except asyncpg.UniqueViolationError:
        raise Conflict("relation_exists", "these clients are already related (or a referrer is already set)") from None
    if kind == "referrer":
        await ctx.conn.execute(SET_REFERRER_SQL, traveler_id, to_traveler_id)
    elif kind == "referred":
        await ctx.conn.execute(SET_REFERRER_SQL, to_traveler_id, traveler_id)
    ctx.audit(
        "create", "allowed", "traveler_relation", str(relation_id), object_ids=(str(traveler_id), str(to_traveler_id))
    )
    return {"relation_id": str(relation_id), "traveler_id": str(to_traveler_id), "kind": kind}


async def remove_relation(ctx: TenantContext, traveler_id: uuid.UUID, relation_id: uuid.UUID) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(RELATION_SQL, relation_id, traveler_id)
    if row is None:
        raise NotFound("relation_not_found")
    await ctx.conn.execute(TRASH_RELATION_SQL, relation_id, f"member:{ctx.member_id}", TRASH_DAYS)
    if row["kind"] == "referrer":
        await ctx.conn.execute(CLEAR_REFERRER_SQL, row["from_traveler_id"], row["to_traveler_id"])
    elif row["kind"] == "referred":
        await ctx.conn.execute(CLEAR_REFERRER_SQL, row["to_traveler_id"], row["from_traveler_id"])
    ctx.audit("delete", "allowed", "traveler_relation", str(relation_id))
    return {"relation_id": str(relation_id), "removed": True}


# ── the party of a trip (A-03 Travellers) ──────────────────────────────────────────────────────────
async def add_to_trip(
    ctx: TenantContext,
    vault: Vault,
    trip_id: uuid.UUID,
    *,
    traveler_id: uuid.UUID | None = None,
    new: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Add an existing client, or a new companion (a name is enough: no e-mail needed, card CRM-09 E test), to a trip
    as a companion. Both party twins are written here, in the request's transaction."""
    trip = await ctx.conn.fetchrow(TRIP_SQL, trip_id)
    if trip is None:
        raise NotFound("trip_not_found")
    if (traveler_id is None) == (new is None):
        raise Invalid("traveler_or_new", "either an existing client or a new companion")
    if traveler_id is None:
        created = await create_traveler(ctx, vault, {"lead_source": "companion", **(new or {})})
        traveler_id = uuid.UUID(created["traveler_id"])
    row = await ctx.conn.fetchrow(TRAVELER_SQL, traveler_id)
    if row is None:
        raise NotFound("traveler_not_found")
    if await ctx.conn.fetchrow(PARTY_ROW_SQL, trip_id, traveler_id) is not None:
        raise Conflict("already_on_trip")
    token = await _ensure_token(ctx, vault, traveler_id, row["profile_token_enc"])
    await ctx.conn.execute(INSERT_PARTY_SQL, trip_id, traveler_id)
    party_id = await ctx.conn.fetchval(INSERT_PARTY_F2_SQL, trip_id, token)
    band = await age_bands.recompute_party_member(ctx, vault, party_id, traveler_id, row["dob_enc"], trip["start_date"])
    ctx.audit(
        "party_add",
        "allowed",
        "trip",
        str(trip_id),
        fields=("dob",) if row["dob_enc"] is not None and trip["start_date"] is not None else (),
        object_ids=(str(traveler_id),),
    )
    return {
        "trip_id": str(trip_id),
        "traveler_id": str(traveler_id),
        "role": "companion",
        "age_band_at_departure": band,
    }


async def remove_from_trip(
    ctx: TenantContext, vault: Vault, trip_id: uuid.UUID, traveler_id: uuid.UUID
) -> dict[str, Any]:
    party = await ctx.conn.fetchrow(PARTY_ROW_SQL, trip_id, traveler_id)
    if party is None:
        raise NotFound("not_on_trip")
    if party["role"] == "lead":
        raise Conflict("lead_required", "a trip keeps its lead traveller")
    row = await ctx.conn.fetchrow(TRAVELER_SQL, traveler_id)
    token = await _token(vault, traveler_id, row["profile_token_enc"]) if row is not None else None
    await ctx.conn.execute(DELETE_PARTY_SQL, party["trip_traveler_id"])
    if token is not None:
        await ctx.conn.execute(DELETE_PARTY_F2_SQL, trip_id, token)
    ctx.audit("party_remove", "allowed", "trip", str(trip_id), object_ids=(str(traveler_id),))
    return {"trip_id": str(trip_id), "traveler_id": str(traveler_id), "removed": True}
