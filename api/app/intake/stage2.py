"""The Stage 2 planning form (C-02, card CRM-15; spec §5 "Stage 2", design v3 §2.7 F2, R31): its AA-prebuilt
definition with pages and conditional branches, the server validator, and the two ways answers arrive.

- **Sent to the client** (:func:`send_link`): a signed ``crm.client_link`` (purpose ``stage2_form``; a random 32-byte
  token, only its SHA-256 stored, 14 days) and one message through the dispatcher (``origin user_action``: it waits
  the 10 s Undo delay; with no sender configured it is held, ``no_sender``, until CRM-17, so the trip page offers
  "Copy link"). A link sent earlier and still open is revoked and named in ``reissued_from`` (Fix B11). Called by
  ``POST /crm/v1/trips/{id}/stage2/send`` and, with ``tenant_settings.stage2_auto_send``, inside the inquiry ->
  qualifying move (:mod:`app.stages`), so Undo of that move takes the form back (card E).
- **Filled in from a call** (``PUT /crm/v1/trips/{id}/stage2``, a member) or **submitted by the client**
  (:mod:`app.intake.stage2_public`): the same :func:`validate` and :func:`apply`.

R31 conditional branches: the altitude / trek questions show only for an adventure trip, the health page only after
the client ticked the health-data consent. The server evaluates visibility again from the trip and the answers and
**drops** every answer of a hidden branch (counted in ``form_submission.hidden_answers_dropped``, never stored), so a
crafted request cannot write a hidden field. Health answers sent without the consent are refused (422
``health_consent_required``) and nothing is stored.

Where answers go: bands and codes -> the F2 ``traveler_profile`` of the client; health codes -> ``traveler_health``
(F1, crm-006 consent rule; mirrored to the profile flags), free text (the must-do detail) -> an encrypted note of kind
``stage2_free_text`` (F1); the whole answer set -> one encrypted ``form_submission`` row (stage ``s2``, outcome
``applied``, redacted after 90 days). Then completeness, ``last_activity_at`` and ``waiting_on`` are refreshed.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.dispatch import queue as dispatch
from app.errors import Conflict, Invalid, NotFound
from app.intake import completeness
from app.tenancy import AnyContext
from app.travellers import fields as tf
from app.vault import Vault

SCHEMA_KEY = "stage2.v1"
VERSION = 1
LINK_DAYS = 14
PURPOSE = "stage2_form"
MAX_DETAIL = 1000

INTERESTS = (
    "culture", "food", "nature", "history", "beaches", "wellness", "photography", "wildlife", "trekking", "cycling",
    "cooking", "homestay", "markets", "temples", "boat_trips", "nightlife",
)  # fmt: skip
AVOID = ("long_drives", "crowds", "early_starts", "heights", "spicy_food", "boat_trips", "camping", "nightlife")
ADVENTURE = ("adventure",)


def _field(name: str, kind: str, options: tuple[str, ...] | None = None, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"name": name, "type": kind, "required": False, **extra}
    if options is not None:
        out["options"] = list(options)
    return out


PAGES: tuple[dict[str, Any], ...] = (
    {
        "key": "travel_style",
        "fields": [
            _field("accommodation_tier", "select", tf.ACCOMMODATION_TIERS),
            _field("group_pref", "select", tf.GROUP_PREFS),
            _field("fitness_band", "select", tf.FITNESS_BANDS),
            _field("trek_experience", "select", tf.TREK_EXPERIENCE, show_if={"field": "trip_style", "in": ADVENTURE}),
            _field("altitude_ok", "boolean", show_if={"field": "trip_style", "in": ADVENTURE}),
        ],
    },
    {
        "key": "interests",
        "fields": [
            _field("interests", "multi_select", INTERESTS, max_items=10),
            _field("must_do_codes", "multi_select", INTERESTS, max_items=10),
            _field("must_do_detail", "long_text", max_length=MAX_DETAIL),
            _field("must_avoid_codes", "multi_select", AVOID, max_items=10),
            _field("occasion", "select", tf.OCCASIONS),
        ],
    },
    {
        "key": "planning",
        "fields": [
            _field("budget_flex", "select", tf.BUDGET_FLEX),
            _field("decision_maker", "select", tf.DECISION_MAKERS),
            _field("booking_timeline", "select", tf.BOOKING_TIMELINES),
            _field("competing_quotes", "boolean"),
        ],
    },
    {"key": "health_consent", "fields": [_field("health_consent", "consent", purpose="health_data")]},
    {
        "key": "health",
        "show_if": {"field": "health_consent", "in": [True]},
        "fields": [
            _field("dietary_flags", "multi_select", tf.DIETARY_FLAGS),
            _field("mobility_flags", "multi_select", tf.MOBILITY_FLAGS),
        ],
    },
)
FIELDS: dict[str, dict[str, Any]] = {f["name"]: f for p in PAGES for f in p["fields"]}
PAGE_OF: dict[str, dict[str, Any]] = {f["name"]: p for p in PAGES for f in p["fields"]}
HEALTH_FIELDS = ("dietary_flags", "mobility_flags")
PROFILE_FIELDS = (
    "accommodation_tier", "group_pref", "fitness_band", "trek_experience", "altitude_ok", "interests", "must_do_codes",
    "must_avoid_codes", "occasion", "budget_flex", "decision_maker", "booking_timeline", "competing_quotes",
)  # fmt: skip
META_KEYS = frozenset({"client_submission_id"})
SUBMISSION_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def definition() -> dict[str, Any]:
    """The pages the web renders (codes only; the web words them, EN default / VI)."""
    return {"schema_key": SCHEMA_KEY, "version": VERSION, "pages": [dict(p) for p in PAGES]}


class HealthConsentMissing(Exception):
    pass


class Stage2Invalid(Exception):
    def __init__(self, fields: list[str]) -> None:
        super().__init__(", ".join(fields))
        self.fields = fields


@dataclass(frozen=True)
class Stage2Answers:
    values: dict[str, Any]
    health_consent: bool = False
    must_do_detail: str | None = None
    client_submission_id: str = ""
    hidden_answers_dropped: int = field(default=0, compare=False)

    def as_json(self) -> dict[str, Any]:
        return {**self.values, "health_consent": self.health_consent, "must_do_detail": self.must_do_detail}


def _visible(rule: dict[str, Any] | None, context: dict[str, Any]) -> bool:
    if rule is None:
        return True
    return context.get(rule["field"]) in rule["in"]


def visible_fields(trip_style: str | None, answers: dict[str, Any]) -> set[str]:
    """The fields a client could see for this trip and these answers (R31, evaluated on the server)."""
    context = {"trip_style": trip_style, "health_consent": answers.get("health_consent") is True}
    out = set()
    for page in PAGES:
        if not _visible(page.get("show_if"), context):
            continue
        out.update(f["name"] for f in page["fields"] if _visible(f.get("show_if"), context))
    return out


def _check(name: str, value: Any) -> Any:
    spec = FIELDS[name]
    kind = spec["type"]
    if value is None:
        return None
    if kind == "select":
        if value not in spec["options"]:
            raise ValueError(name)
        return value
    if kind in ("boolean", "consent"):
        if not isinstance(value, bool):
            raise ValueError(name)
        return value
    if kind == "multi_select":
        limit = spec.get("max_items", len(spec["options"]))
        if not isinstance(value, list) or len(value) > limit or any(v not in spec["options"] for v in value):
            raise ValueError(name)
        return list(dict.fromkeys(value))
    if kind == "long_text":
        if not isinstance(value, str):
            raise ValueError(name)
        text = unicodedata.normalize("NFC", value).strip()
        if len(text) > spec["max_length"] or any(ord(c) < 32 and c not in "\n\t" for c in text):
            raise ValueError(name)
        return text or None
    raise ValueError(name)  # pragma: no cover — every kind is handled above


def validate(raw: Any, *, trip_style: str | None) -> Stage2Answers:
    """The normalised answers, or :class:`Stage2Invalid` (field codes) / :class:`HealthConsentMissing`. Unknown keys
    and answers of a hidden branch are dropped and counted."""
    if not isinstance(raw, dict):
        raise Stage2Invalid(["body"])
    consent = raw.get("health_consent")
    if any(raw.get(h) for h in HEALTH_FIELDS) and consent is not True:
        raise HealthConsentMissing
    shown = visible_fields(trip_style, raw)
    dropped = 0
    errors: list[str] = []
    values: dict[str, Any] = {}
    for key, value in raw.items():
        if key in META_KEYS:
            continue
        if key not in FIELDS or key not in shown:
            dropped += 1
            continue
        try:
            values[key] = _check(key, value)
        except ValueError:
            errors.append(key)
    if errors:
        raise Stage2Invalid(sorted(errors))
    submission_id = raw.get("client_submission_id")
    if submission_id is not None and not (isinstance(submission_id, str) and SUBMISSION_ID.match(submission_id)):
        raise Stage2Invalid(["client_submission_id"])
    return Stage2Answers(
        values={k: v for k, v in values.items() if k not in ("health_consent", "must_do_detail")},
        health_consent=values.get("health_consent") is True,
        must_do_detail=values.get("must_do_detail"),
        client_submission_id=submission_id or uuid.uuid4().hex,
        hidden_answers_dropped=dropped,
    )


# ── SQL ───────────────────────────────────────────────────────────────────────────────────────────
TRIP_SQL = "SELECT stage, trip_style FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL FOR UPDATE"
TRAVELER_SQL = """
SELECT traveler_id, profile_token_enc, archived_at FROM crm.traveler WHERE traveler_id = $1 AND deleted_at IS NULL
"""
INSERT_PROFILE_SQL = "INSERT INTO crm.traveler_profile (traveler_token) VALUES ($1)"
POINTER_SQL = "UPDATE crm.traveler SET profile_token_enc = $2 WHERE traveler_id = $1"
PREFILL_SQL = f"SELECT {', '.join(PROFILE_FIELDS)} FROM crm.traveler_profile WHERE traveler_token = $1"  # noqa: S608
OPEN_LINKS_SQL = """
SELECT link_id FROM crm.client_link
 WHERE trip_id = $1 AND purpose = $2 AND status IN ('sent', 'opened') AND deleted_at IS NULL
 ORDER BY created_at DESC
"""
REVOKE_SQL = "UPDATE crm.client_link SET status = 'revoked', revoked_at = now() WHERE link_id = ANY($1::uuid[])"
INSERT_LINK_SQL = """
INSERT INTO crm.client_link (link_id, trip_id, traveler_id, purpose, token_hash, expires_at, reissued_from)
VALUES ($1, $2, $3, $4, $5, now() + make_interval(days => $6), $7)
RETURNING expires_at
"""
LINK_DISPATCH_SQL = "UPDATE crm.client_link SET dispatch_id = $2 WHERE link_id = $1"
INSERT_SUBMISSION_SQL = """
INSERT INTO crm.form_submission (submission_id, stage, schema_key, payload_enc, idempotency_key, hidden_answers_dropped,
                                 status, outcome, trip_id, traveler_id, client_link_id, processed_at)
VALUES ($1, 's2', $2, $3, $4, $5, 'processed', 'applied', $6, $7, $8, now())
ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
RETURNING submission_id
"""
INSERT_NOTE_SQL = """
INSERT INTO crm.note (note_id, trip_id, traveler_id, kind, body_enc) VALUES ($1, $2, $3, 'stage2_free_text', $4)
"""
HEALTH_SQL = "SELECT health_id, health_flags_enc FROM crm.traveler_health WHERE traveler_id = $1 AND deleted_at IS NULL"
CONSENT_SQL = """
INSERT INTO crm.consent (traveler_id, purpose, value, source, submission_id) VALUES ($1, 'health_data', true, $2, $3)
"""
LINK_SUBMITTED_SQL = """
UPDATE crm.client_link
   SET status = 'submitted', submitted_at = now(), first_opened_at = coalesce(first_opened_at, now())
 WHERE link_id = $1
"""


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


async def _profile_token(ctx: AnyContext, vault: Vault, traveler_id: uuid.UUID) -> uuid.UUID:
    row = await ctx.conn.fetchrow(TRAVELER_SQL, traveler_id)
    if row is None:
        raise NotFound("traveler_not_found")
    if row["profile_token_enc"] is not None:
        return uuid.UUID(await vault.decrypt("traveler", "profile_token_enc", traveler_id, row["profile_token_enc"]))
    token = uuid.uuid4()
    await ctx.conn.execute(INSERT_PROFILE_SQL, token)
    await ctx.conn.execute(
        POINTER_SQL, traveler_id, await vault.encrypt("traveler", "profile_token_enc", traveler_id, str(token))
    )
    return token


async def send_link(ctx: AnyContext, vault: Vault, trip_id: uuid.UUID, *, auto: bool = False) -> dict[str, Any]:
    """Create the trip's planning-form link for its lead and queue the message. Returns the link path once (the token
    is never stored: "Copy link" works from this answer only; a new send re-issues the link)."""
    lead, _token = await completeness.lead_token(ctx, vault, trip_id)
    if lead is None:
        raise Conflict("no_lead_traveller", "the trip has no lead traveller to send the form to")
    previous = [r["link_id"] for r in await ctx.conn.fetch(OPEN_LINKS_SQL, trip_id, PURPOSE)]
    if previous:
        await ctx.conn.execute(REVOKE_SQL, previous)
    token = secrets.token_urlsafe(32)
    link_id = uuid.uuid4()
    expires_at = await ctx.conn.fetchval(
        INSERT_LINK_SQL,
        link_id,
        trip_id,
        lead,
        PURPOSE,
        token_hash(token),
        LINK_DAYS,
        previous[0] if previous else None,
    )
    queued = await dispatch.enqueue_message(
        ctx,
        traveler_id=lead,
        channel="email",
        purpose="transactional",
        origin="user_action",
        trip_id=trip_id,
        message_ref=link_id,
    )
    await ctx.conn.execute(LINK_DISPATCH_SQL, link_id, uuid.UUID(queued["dispatch_id"]))
    ctx.audit("stage2_send", "allowed", "client_link", str(link_id), reason="auto" if auto else "agent")
    return {
        "link_id": str(link_id),
        "link_path": f"/c/s/{token}",
        "expires_at": expires_at.isoformat(),
        "dispatch_id": queued["dispatch_id"],
        "send_after": queued["not_before"],
        "reissued_from": str(previous[0]) if previous else None,
    }


async def prefill(ctx: AnyContext, vault: Vault, traveler_id: uuid.UUID | None) -> dict[str, Any]:
    """Bands and codes the client already gave (rule 4: a returning client sees them pre-filled). F2 only: never a
    name, contact detail or health code."""
    if traveler_id is None:
        return {}
    row = await ctx.conn.fetchrow(TRAVELER_SQL, traveler_id)
    if row is None or row["profile_token_enc"] is None:
        return {}
    token = uuid.UUID(await vault.decrypt("traveler", "profile_token_enc", traveler_id, row["profile_token_enc"]))
    profile = await ctx.conn.fetchrow(PREFILL_SQL, token)
    if profile is None:
        return {}
    out: dict[str, Any] = {}
    for name in PROFILE_FIELDS:
        value = profile[name]
        if value is None or value == [] or (isinstance(value, list | tuple) and not value):
            continue
        out[name] = list(value) if isinstance(value, list | tuple) else value
    return out


async def apply(
    ctx: AnyContext,
    vault: Vault,
    trip_id: uuid.UUID,
    traveler_id: uuid.UUID,
    answers: Stage2Answers,
    *,
    consent_source: str,
    link_id: uuid.UUID | None = None,
    idempotency_key: str,
) -> dict[str, Any]:
    """Write one validated answer set (see the module docstring). Idempotent per ``idempotency_key``."""
    submission_id = uuid.uuid4()
    blob = await vault.encrypt(
        "form_submission", "payload_enc", submission_id, json.dumps(answers.as_json(), ensure_ascii=False)
    )
    stored = await ctx.conn.fetchval(
        INSERT_SUBMISSION_SQL,
        submission_id,
        SCHEMA_KEY,
        blob,
        idempotency_key,
        answers.hidden_answers_dropped,
        trip_id,
        traveler_id,
        link_id,
    )
    if stored is None:
        return {"status": "duplicate"}
    token = await _profile_token(ctx, vault, traveler_id)
    profile = {k: v for k, v in answers.values.items() if k in PROFILE_FIELDS}
    if profile:
        names = sorted(profile)
        sets = ", ".join(f"{n} = ${i + 2}" for i, n in enumerate(names))
        await ctx.conn.execute(
            f"UPDATE crm.traveler_profile SET {sets} WHERE traveler_token = $1",  # noqa: S608 — names from PROFILE_FIELDS
            token,
            *[profile[n] for n in names],
        )
    if answers.must_do_detail:
        note_id = uuid.uuid4()
        body = await vault.encrypt("note", "body_enc", note_id, answers.must_do_detail)
        await ctx.conn.execute(INSERT_NOTE_SQL, note_id, trip_id, traveler_id, body)
    health = {k: answers.values[k] for k in HEALTH_FIELDS if k in answers.values}
    if answers.health_consent:
        await ctx.conn.execute(CONSENT_SQL, traveler_id, consent_source, submission_id)
        if health:
            await _write_health(ctx, vault, traveler_id, token, health)
    ctx.audit(
        "stage2_answers",
        "allowed",
        "form_submission",
        str(submission_id),
        fields=tuple(sorted({*profile, *health, *(("must_do_detail",) if answers.must_do_detail else ())})),
    )
    done = await completeness.refresh(ctx, vault, trip_id, actor=_actor(ctx))
    from app import stages

    await stages.touch(ctx, trip_id)
    await stages.refresh_waiting_on(ctx, trip_id)
    return {
        "status": "applied",
        "submission_id": str(submission_id),
        "hidden_answers_dropped": answers.hidden_answers_dropped,
        "completeness": done.view() if done is not None else None,
    }


def _actor(ctx: AnyContext) -> str:
    member = getattr(ctx, "member_id", None)
    return f"member:{member}" if member is not None else str(getattr(ctx, "actor", "system"))


async def _write_health(
    ctx: AnyContext, vault: Vault, traveler_id: uuid.UUID, token: uuid.UUID, health: dict[str, list[str]]
) -> None:
    """crm-006 health row under the consent just recorded; the medical codes an agent recorded stay as they are."""
    from app.travellers import service

    current: dict[str, list[str]] = {"dietary": [], "mobility": [], "medical": []}
    row = await ctx.conn.fetchrow(HEALTH_SQL, traveler_id)
    if row is not None:
        current.update(json.loads(await vault.decrypt("traveler_health", "health_flags_enc", row["health_id"],
                                                      row["health_flags_enc"])))  # fmt: skip
    await service.set_health(
        ctx,  # type: ignore[arg-type] — set_health needs conn and audit only
        vault,
        traveler_id,
        dietary=health.get("dietary_flags", current["dietary"]),
        mobility=health.get("mobility_flags", current["mobility"]),
        medical=current["medical"],
        consent_recorded=False,
    )


async def fill_from_call(
    ctx: AnyContext, vault: Vault, trip_id: uuid.UUID, raw: Any, *, client_submission_id: str | None = None
) -> dict[str, Any]:
    """A member types the answers in during a call (design §2.7 F2 [Fill in from call]): the same validator; the
    health consent, when ticked, is recorded as ``agent_recorded``."""
    trip = await ctx.conn.fetchrow(TRIP_SQL, trip_id)
    if trip is None:
        raise NotFound("trip_not_found")
    lead, _token = await completeness.lead_token(ctx, vault, trip_id)
    if lead is None:
        raise Conflict("no_lead_traveller", "add the lead traveller first")
    answers = checked(raw, trip["trip_style"])
    key = f"call:{client_submission_id or answers.client_submission_id}"[:80]
    return await apply(ctx, vault, trip_id, lead, answers, consent_source="agent_recorded", idempotency_key=key)


def checked(raw: Any, trip_style: str | None) -> Stage2Answers:
    """:func:`validate` with its errors as 422s."""
    try:
        return validate(raw, trip_style=trip_style)
    except HealthConsentMissing:
        raise Invalid("health_consent_required", "health answers need the health data consent") from None
    except Stage2Invalid as exc:
        raise Invalid("invalid_submission", "some answers are not valid", fields=exc.fields) from None
