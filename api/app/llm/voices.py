"""Voice profiles of the AI reply draft (AA-965, card CRM-33b R41; design v3 A-18 "AI" > Voices, A-05 voice picker;
data-privacy-integrations §2.9 ``voice_profile``).

A voice is a few **fixed codes** (:data:`DESCRIPTORS`: formality, warmth, length, emoji, greeting, sign-off), never
free text: :func:`voice_instruction` turns the codes into fixed sentences of ours that the draft adds to its system
block, so nothing a member types about a voice ever reaches the model (the name is the agency's label, shown in the
picker only). Every member reads the agency's voices and uses any of them on a draft; a member edits and deletes their
own (the owner every voice). How many voices one member keeps is the admin-editable setting
``crm.ai.voice_profiles_per_member_max`` (AA-1096: never a literal), checked under a per-member advisory lock so two
requests cannot both pass at the limit. Delete is a hard delete (crm-076: never trashed); a draft that used a deleted
voice keeps its id as history.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from app import platform_settings as ps
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.tenancy import TenantContext

LOCALES = ("en", "vi")
CHANNELS = ("email", "whatsapp")
# The closed lists of the descriptor codes (crm-076 COMMENT); the first value of each is the default.
DESCRIPTORS: dict[str, tuple[str, ...]] = {
    "formality": ("neutral", "formal", "casual"),
    "warmth": ("warm", "neutral", "direct"),
    "length": ("short", "medium", "long"),
    "emoji": ("none", "light"),
    "greeting": ("first_name", "full_name", "none"),
    "sign_off": ("agent_name", "agency_name", "none"),
}
# The fixed sentence of each code (the only voice text the model sees).
SENTENCES: dict[str, dict[str, str]] = {
    "formality": {
        "neutral": "Use a polite, plain register.",
        "formal": "Use a formal register (no contractions, no slang).",
        "casual": "Use a relaxed, casual register, still professional.",
    },
    "warmth": {
        "warm": "Sound warm and friendly.",
        "neutral": "Keep the tone even and matter-of-fact.",
        "direct": "Be direct: lead with the answer, keep pleasantries to a minimum.",
    },
    "length": {
        "short": "Keep it to about three sentences.",
        "medium": "Keep it to one or two short paragraphs.",
        "long": "A fuller answer of up to four short paragraphs is fine.",
    },
    "emoji": {"none": "Do not use emoji.", "light": "At most one fitting emoji is fine."},
    "greeting": {
        "first_name": "Greet the client by their first name placeholder.",
        "full_name": "Greet the client by their full name placeholder.",
        "none": "Do not open with a greeting line.",
    },
    "sign_off": {
        "agent_name": "End with a short sign-off line for the agent, without inventing a name.",
        "agency_name": "End with a short sign-off line for the agency, without inventing a name.",
        "none": "Do not add a sign-off line.",
    },
}

LIST_SQL = """
SELECT voice_profile_id, member_id, name, locale, channel, descriptors, created_at, updated_at
  FROM crm.voice_profile
 ORDER BY (member_id = $1) DESC, created_at, voice_profile_id
"""
ONE_SQL = """
SELECT voice_profile_id, member_id, name, locale, channel, descriptors, created_at, updated_at
  FROM crm.voice_profile WHERE voice_profile_id = $1
"""
LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended('crm_voice_profile:' || $1::text, 0))"
COUNT_SQL = "SELECT count(*) FROM crm.voice_profile WHERE member_id = $1"
INSERT_SQL = """
INSERT INTO crm.voice_profile (member_id, name, locale, channel, descriptors) VALUES ($1, $2, $3, $4, $5::jsonb)
RETURNING voice_profile_id
"""
UPDATE_SQL = """
UPDATE crm.voice_profile SET name = $2, locale = $3, channel = $4, descriptors = $5::jsonb
 WHERE voice_profile_id = $1
"""
DELETE_SQL = "DELETE FROM crm.voice_profile WHERE voice_profile_id = $1"


def per_member_max() -> int:
    """``crm.ai.voice_profiles_per_member_max`` (AA-1096: admin-editable, read at each call)."""
    return int(ps.get(ps.AI_VOICES_PER_MEMBER_MAX))


def clean_descriptors(raw: Any) -> dict[str, str]:
    """Every key and value from :data:`DESCRIPTORS` (422 ``invalid_descriptors`` otherwise); missing keys get the
    first value of their list."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise Invalid("invalid_descriptors", "descriptors is an object of fixed codes")
    unknown = sorted(set(raw) - set(DESCRIPTORS))
    if unknown:
        raise Invalid("invalid_descriptors", f"unknown descriptor {unknown[0]}")
    out = {}
    for key, allowed in DESCRIPTORS.items():
        value = raw.get(key, allowed[0])
        if value not in allowed:
            raise Invalid("invalid_descriptors", f"{key} is one of {', '.join(allowed)}")
        out[key] = value
    return out


def _name(value: Any) -> str:
    name = str(value or "").strip()
    if not 1 <= len(name) <= 60 or any(ord(c) < 32 for c in name):
        raise Invalid("invalid_name", "a voice name is 1 to 60 characters")
    return name


def _choice(value: Any, allowed: tuple[str, ...], field: str) -> str:
    if value not in allowed:
        raise Invalid(f"invalid_{field}", f"{field} is one of {', '.join(allowed)}")
    return str(value)


def _descriptors(value: Any) -> dict[str, str]:
    return json.loads(value) if isinstance(value, str) else dict(value or {})


def view(row: Any, ctx: TenantContext) -> dict[str, Any]:
    return {
        "voice_profile_id": str(row["voice_profile_id"]),
        "member_id": str(row["member_id"]),
        "mine": row["member_id"] == ctx.member_id,
        "can_edit": row["member_id"] == ctx.member_id or ctx.member_role == "owner",
        "name": row["name"],
        "locale": row["locale"],
        "channel": row["channel"],
        "descriptors": _descriptors(row["descriptors"]),
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


async def list_voices(ctx: TenantContext) -> dict[str, Any]:
    rows = await ctx.conn.fetch(LIST_SQL, ctx.member_id)
    mine = sum(1 for r in rows if r["member_id"] == ctx.member_id)
    return {
        "voices": [view(r, ctx) for r in rows],
        "per_member_max": per_member_max(),
        "mine": mine,
        "descriptors": {k: list(v) for k, v in DESCRIPTORS.items()},
    }


async def _require_member(ctx: TenantContext) -> uuid.UUID:
    if ctx.member_id is None:
        raise Forbidden("member_only", "a voice belongs to an agency member")
    return ctx.member_id


async def create(ctx: TenantContext, body: dict[str, Any]) -> dict[str, Any]:
    member = await _require_member(ctx)
    name = _name(body.get("name"))
    locale = _choice(body.get("locale", "en"), LOCALES, "locale")
    channel = _choice(body.get("channel", "email"), CHANNELS, "channel")
    descriptors = clean_descriptors(body.get("descriptors"))
    await ctx.conn.execute(LOCK_SQL, str(member))
    limit = per_member_max()
    if int(await ctx.conn.fetchval(COUNT_SQL, member) or 0) >= limit:
        raise Conflict("voice_limit_reached", f"you already keep {limit} voices; delete one first", limit=limit)
    voice_id = await ctx.conn.fetchval(INSERT_SQL, member, name, locale, channel, json.dumps(descriptors))
    ctx.audit("create", "allowed", "voice_profile", str(voice_id))
    return view(await ctx.conn.fetchrow(ONE_SQL, voice_id), ctx)


async def _editable(ctx: TenantContext, voice_id: uuid.UUID) -> Any:
    row = await ctx.conn.fetchrow(ONE_SQL, voice_id)
    if row is None:
        raise NotFound("voice_not_found", "no voice with this id")
    if row["member_id"] != ctx.member_id and ctx.member_role != "owner":
        ctx.audit("update", "denied", "voice_profile", str(voice_id), reason="not_your_voice")
        raise Forbidden("not_your_voice", "only the member who made a voice (or the owner) changes it")
    return row


async def update(ctx: TenantContext, voice_id: uuid.UUID, body: dict[str, Any]) -> dict[str, Any]:
    row = await _editable(ctx, voice_id)
    name = _name(body["name"]) if body.get("name") is not None else row["name"]
    locale = _choice(body["locale"], LOCALES, "locale") if body.get("locale") is not None else row["locale"]
    channel = _choice(body["channel"], CHANNELS, "channel") if body.get("channel") is not None else row["channel"]
    current = _descriptors(row["descriptors"])
    descriptors = clean_descriptors({**current, **body["descriptors"]}) if body.get("descriptors") else current
    await ctx.conn.execute(UPDATE_SQL, voice_id, name, locale, channel, json.dumps(descriptors))
    fields = tuple(sorted(k for k in ("name", "locale", "channel", "descriptors") if body.get(k) is not None))
    ctx.audit("update", "allowed", "voice_profile", str(voice_id), fields=fields)
    return view(await ctx.conn.fetchrow(ONE_SQL, voice_id), ctx)


async def delete(ctx: TenantContext, voice_id: uuid.UUID) -> dict[str, Any]:
    await _editable(ctx, voice_id)
    await ctx.conn.execute(DELETE_SQL, voice_id)
    ctx.audit("delete", "allowed", "voice_profile", str(voice_id))
    return {"voice_profile_id": str(voice_id), "deleted": True}


async def for_draft(ctx: TenantContext, voice_id: uuid.UUID | None) -> dict[str, str] | None:
    """The descriptors of a voice the draft asked for (404 ``voice_not_found`` when the agency has no such voice)."""
    if voice_id is None:
        return None
    row = await ctx.conn.fetchrow(ONE_SQL, voice_id)
    if row is None:
        raise NotFound("voice_not_found", "no voice with this id")
    return clean_descriptors(_descriptors(row["descriptors"]))


def voice_instruction(descriptors: dict[str, str] | None) -> str | None:
    """The fixed sentences of a voice, for the draft's system block (None without a voice). Codes only: a stored
    value outside the closed lists is skipped, never sent."""
    if not descriptors:
        return None
    lines = [SENTENCES[k][v] for k, v in descriptors.items() if k in SENTENCES and v in SENTENCES[k]]
    return "Writing voice of the agency:\n" + "\n".join(f"- {line}" for line in lines) if lines else None
