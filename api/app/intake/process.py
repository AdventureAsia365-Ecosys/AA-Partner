"""``crm_intake_process``: one stored inquiry -> traveller, profile, trip at Inquiry, reply task (design v3 §2.7 F1).

The job payload is ``{tenant_id, submission_id}`` (ids only). The handler runs in :func:`app.tenancy.system_tx` (the
agency's RLS context, actor ``job:crm_intake_process``) and is idempotent: a submission that is no longer
``received`` is skipped, and the submission row is locked (``FOR UPDATE``) while it is processed.

Dedupe (R6, which absorbs C47): the e-mail's blind index (then the phone's) finds an existing traveller of the agency.
``crm.traveler`` has no unique index on the blind indexes (a traveller may have no e-mail), so the lookup runs under
transaction-scoped advisory locks keyed on (agency, e-mail index) and (agency, phone index), always in that order: two
jobs for the same new person (two workers overlapping in a rolling deploy) are serialised, and the second one finds
the traveller and the open Inquiry the first one created.

Consent (R6 evidence): every processed submission appends its ``contact`` and ``marketing`` rows to ``crm.consent``
(latest row per purpose wins) and the F2 flag ``traveler_profile.marketing_ok`` follows that latest marketing row on
every path. A submission is not verified (anyone may type someone's e-mail), so neither the row nor the flag is enough
to send marketing: CRM-17 / CRM-29 must confirm by double opt-in before any marketing mail.

- **Attach** (card E test): that traveller leads a trip that is still open (``inquiry`` or ``qualifying``): the
  submission is attached to it — the trip's ``last_inquiry_at`` moves, it waits on the agency again, the reply task is
  (re)opened and the prospect's message becomes a note. No second trip, no second traveller.
- **Known traveller, no open trip**: a new trip for the same traveller; the F2 profile's ``repeat_count`` grows.
- **New**: an F1 traveller (every identity value encrypted, blind indexes for e-mail, phone and name), an F2 profile
  (random token; the encrypted pointer is the only link), companions by name only (Fix E5), the trip with the
  Stage 1 answers, the party twins ``trip_traveler`` / ``trip_party``, the "Reply within 2 h" task (M1 success
  metric), the stage history row and the consent evidence.

The confirmation mail to the prospect is not sent here: traveller mail leaves from the agency's own mailbox (errata
S247), which CRM-17 (AA-945) connects. A failure that can never succeed (the payload does not open or no longer
validates) marks the submission ``failed`` with the error class and fails the job permanently.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import asyncpg

from app.errors import Forbidden
from app.intake import completeness, slots, stage1
from app.jobs.queue import Job
from app.jobs.worker import Handler, PermanentError
from app.tenancy import AnyContext, system_tx
from app.vault import DecryptionDenied, KeyProvider, Vault
from app.vault.blind_index import NormalisationError
from app.workflows import triggers

log = logging.getLogger(__name__)

ACTOR = "job:crm_intake_process"
OPEN_STAGES = ("inquiry", "qualifying")
REPLY_TASK = "reply_inquiry"
REPLY_WITHIN = timedelta(hours=2)

SUBMISSION_SQL = """
SELECT s.submission_id, s.intake_form_id, s.payload_enc, s.status, f.default_owner_member_id, f.lead_source,
       t.default_locale
  FROM crm.form_submission s
  JOIN crm.intake_form f ON f.intake_form_id = s.intake_form_id
  JOIN crm.tenant t ON t.tenant_id = s.tenant_id
 WHERE s.submission_id = $1 AND s.deleted_at IS NULL
   FOR UPDATE OF s
"""
BY_EMAIL_SQL = """
SELECT traveler_id, profile_token_enc FROM crm.traveler
 WHERE email_bidx = $1 AND deleted_at IS NULL ORDER BY created_at, traveler_id LIMIT 1
"""
BY_PHONE_SQL = """
SELECT traveler_id, profile_token_enc FROM crm.traveler
 WHERE phone_bidx = $1 AND deleted_at IS NULL ORDER BY created_at, traveler_id LIMIT 1
"""
DEDUPE_LOCK_SQL = """
SELECT pg_advisory_xact_lock(hashtextextended($1::text || ':' || $2::text || ':' || encode($3::bytea, 'hex'), 0))
"""
OPEN_TRIP_SQL = """
SELECT t.trip_id, t.owner_member_id FROM crm.trip t
  JOIN crm.trip_traveler tt ON tt.trip_id = t.trip_id AND tt.role = 'lead' AND tt.deleted_at IS NULL
 WHERE tt.traveler_id = $1 AND t.stage = ANY($2::text[]) AND t.deleted_at IS NULL
 ORDER BY t.last_inquiry_at DESC, t.trip_id
 LIMIT 1
   FOR UPDATE OF t
"""
INSERT_TRAVELER_SQL = """
INSERT INTO crm.traveler (traveler_id, display_name_enc, email_enc, email_bidx, phone_enc, phone_bidx, name_bidx,
                          name_token_bidx, preferred_channel, profile_token_enc, owner_member_id, lead_source)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
"""
INSERT_COMPANION_SQL = """
INSERT INTO crm.traveler (traveler_id, display_name_enc, name_bidx, name_token_bidx, profile_token_enc,
                          owner_member_id, lead_source, referrer_traveler_id)
VALUES ($1, $2, $3, $4, $5, $6, $7, NULL)
"""
UNARCHIVE_SQL = """
UPDATE crm.traveler SET archived_at = NULL, archive_reason = NULL, archived_by = NULL
 WHERE traveler_id = $1 AND archived_at IS NOT NULL
RETURNING traveler_id
"""
INSERT_PROFILE_SQL = """
INSERT INTO crm.traveler_profile (traveler_token, budget_band, pace, marketing_ok) VALUES ($1, $2, $3, $4)
"""
BUMP_PROFILE_SQL = """
UPDATE crm.traveler_profile
   SET repeat_count = repeat_count + 1, budget_band = coalesce($2, budget_band), pace = coalesce($3, pace),
       marketing_ok = $4
 WHERE traveler_token = $1
"""
SET_MARKETING_SQL = "UPDATE crm.traveler_profile SET marketing_ok = $2 WHERE traveler_token = $1"
INSERT_TRIP_SQL = """
INSERT INTO crm.trip (trip_id, title, start_date, end_date, month, date_flex_days, nights, destinations, party_adults,
                      party_child_ages, party_type, budget_band, trip_style, owner_member_id, intake_form_id,
                      lead_source)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
"""
ATTACH_TRIP_SQL = "UPDATE crm.trip SET last_inquiry_at = now(), waiting_on = 'agency' WHERE trip_id = $1"
INSERT_PARTY_SQL = "INSERT INTO crm.trip_traveler (trip_id, traveler_id, role) VALUES ($1, $2, $3)"
INSERT_PARTY_F2_SQL = "INSERT INTO crm.trip_party (trip_id, traveler_token, role) VALUES ($1, $2, $3)"
INSERT_HISTORY_SQL = """
INSERT INTO crm.trip_stage_history (trip_id, from_stage, to_stage, reason_code) VALUES ($1, NULL, 'inquiry', 'intake')
RETURNING history_id
"""
OPEN_REPLY_TASK_SQL = """
INSERT INTO crm.task (trip_id, kind, assignee_member_id, due_anchor, due_at)
VALUES ($1, $2, $3, 'created', $4)
ON CONFLICT (trip_id, kind) WHERE status = 'open' AND deleted_at IS NULL AND trip_id IS NOT NULL DO NOTHING
"""
INSERT_NOTE_SQL = "INSERT INTO crm.note (note_id, trip_id, traveler_id, kind, body_enc) VALUES ($1, $2, $3, $4, $5)"
INSERT_CONSENT_SQL = """
INSERT INTO crm.consent (traveler_id, purpose, value, source, submission_id) VALUES ($1, $2, $3, 'form', $4)
"""
DONE_SQL = """
UPDATE crm.form_submission
   SET status = 'processed', outcome = $2, trip_id = $3, traveler_id = $4, processed_at = now()
 WHERE submission_id = $1
"""
FAILED_SQL = """
UPDATE crm.form_submission SET status = 'failed', failure_class = $2 WHERE submission_id = $1 AND status = 'received'
"""


@dataclass(frozen=True)
class Outcome:
    status: str  # created | attached | skipped
    trip_id: uuid.UUID | None = None
    traveler_id: uuid.UUID | None = None

    def result(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "trip_id": str(self.trip_id) if self.trip_id else None,
            "traveler_id": str(self.traveler_id) if self.traveler_id else None,
        }


def _ids(payload: dict[str, Any]) -> tuple[uuid.UUID, uuid.UUID]:
    try:
        return uuid.UUID(str(payload["tenant_id"])), uuid.UUID(str(payload["submission_id"]))
    except (KeyError, ValueError):
        raise PermanentError("bad_payload") from None


def build_handler(keys: KeyProvider | None) -> Handler:
    async def crm_intake_process(pool: asyncpg.Pool, job: Job) -> dict[str, Any]:
        tenant_id, submission_id = _ids(job.payload)
        if keys is None:
            raise RuntimeError("key_provider_not_configured")  # retried: the provider may be configured by then
        try:
            async with system_tx(pool, tenant_id, actor=ACTOR) as ctx:
                outcome = await process_submission(ctx, Vault(keys, ctx), submission_id)
        except (DecryptionDenied, stage1.Stage1Invalid, stage1.ConsentMissing, NormalisationError) as exc:
            # DecryptionDenied is a Forbidden: caught first, so the submission is marked failed (and the denial was
            # already audited by system_tx).
            await mark_failed(pool, tenant_id, submission_id, type(exc).__name__)
            raise PermanentError(type(exc).__name__) from None
        except Forbidden as exc:  # the agency is gone or locked: nothing to do for it now
            raise PermanentError(exc.code) from None
        return outcome.result()

    return crm_intake_process


async def mark_failed(pool: asyncpg.Pool, tenant_id: uuid.UUID, submission_id: uuid.UUID, error_class: str) -> None:
    async with system_tx(pool, tenant_id, actor=ACTOR) as ctx:
        await ctx.conn.execute(FAILED_SQL, submission_id, error_class)
        ctx.audit("intake_failed", "allowed", "form_submission", str(submission_id), reason="processing_failed")


async def process_submission(ctx: AnyContext, vault: Vault, submission_id: uuid.UUID) -> Outcome:
    row = await ctx.conn.fetchrow(SUBMISSION_SQL, submission_id)
    if row is None or row["status"] != "received":
        return Outcome("skipped")
    raw = json.loads(await vault.decrypt("form_submission", "payload_enc", submission_id, row["payload_enc"]))
    answers = stage1.Stage1Answers.from_json(raw)
    owner = row["default_owner_member_id"]
    lead_source = row["lead_source"]

    email_idx = await vault.email_index("traveler", "email", answers.email)
    phone_idx = await vault.phone_index("traveler", "phone", answers.phone) if answers.phone else None
    await ctx.conn.execute(DEDUPE_LOCK_SQL, "crm_intake_email", str(ctx.tenant_id), email_idx)
    if phone_idx is not None:
        await ctx.conn.execute(DEDUPE_LOCK_SQL, "crm_intake_phone", str(ctx.tenant_id), phone_idx)
    match = await ctx.conn.fetchrow(BY_EMAIL_SQL, email_idx)
    if match is None and phone_idx is not None:
        match = await ctx.conn.fetchrow(BY_PHONE_SQL, phone_idx)

    if match is not None:
        traveler_id = match["traveler_id"]
        # AA-942 (R23): an archived client who writes again is back: unarchive, so the agency can answer (archived
        # clients are stopped at the dispatcher). One audit event, like every archive change.
        if await ctx.conn.fetchval(UNARCHIVE_SQL, traveler_id):
            ctx.audit("unarchive", "allowed", "traveler", str(traveler_id), reason="repeat_inquiry")
        open_trip = await ctx.conn.fetchrow(OPEN_TRIP_SQL, traveler_id, list(OPEN_STAGES))
        if open_trip is not None:
            trip_id = open_trip["trip_id"]
            await ctx.conn.execute(ATTACH_TRIP_SQL, trip_id)
            await _reply_task(ctx, trip_id, open_trip["owner_member_id"])
            await _message_note(ctx, vault, trip_id, traveler_id, answers)
            await _consent(ctx, traveler_id, answers, submission_id)
            token = await _profile_token(vault, traveler_id, match["profile_token_enc"])
            if token is not None:  # the flag follows the marketing row just written
                await ctx.conn.execute(SET_MARKETING_SQL, token, answers.consent_marketing)
            await ctx.conn.execute(DONE_SQL, submission_id, "attached", trip_id, traveler_id)
            await _after(ctx, vault, answers, trip_id, traveler_id, submission_id, open_trip["owner_member_id"])
            ctx.audit("intake_attached", "allowed", "trip", str(trip_id), reason="repeat_inquiry")
            return Outcome("attached", trip_id, traveler_id)
        token = await _profile_token(vault, traveler_id, match["profile_token_enc"])
        if token is not None:
            await ctx.conn.execute(
                BUMP_PROFILE_SQL, token, answers.budget_band, answers.trip_style, answers.consent_marketing
            )
    else:
        traveler_id = uuid.uuid4()
        token = uuid.uuid4()
        await ctx.conn.execute(
            INSERT_PROFILE_SQL, token, answers.budget_band, answers.trip_style, answers.consent_marketing
        )
        await ctx.conn.execute(
            INSERT_TRAVELER_SQL,
            traveler_id,
            await vault.encrypt("traveler", "display_name_enc", traveler_id, answers.name),
            await vault.encrypt("traveler", "email_enc", traveler_id, answers.email),
            email_idx,
            await vault.encrypt("traveler", "phone_enc", traveler_id, answers.phone) if answers.phone else None,
            phone_idx,
            await vault.name_index("traveler", "name", answers.name),
            await vault.name_token_indexes("traveler", "name", answers.name),
            answers.preferred_channel,
            await vault.encrypt("traveler", "profile_token_enc", traveler_id, str(token)),
            owner,
            lead_source,
        )

    trip_id = uuid.uuid4()
    await ctx.conn.execute(
        INSERT_TRIP_SQL,
        trip_id,
        stage1.trip_title(answers, row["default_locale"]),
        _date(answers.start_date),
        _date(answers.end_date),
        answers.month_date,
        answers.date_flex_days,
        answers.nights,
        list(answers.destinations),
        answers.adults,
        list(answers.child_ages),
        answers.party_type,
        answers.budget_band,
        answers.trip_style,
        owner,
        row["intake_form_id"],
        lead_source,
    )
    history_id = await ctx.conn.fetchval(INSERT_HISTORY_SQL, trip_id)
    await ctx.conn.execute(INSERT_PARTY_SQL, trip_id, traveler_id, "lead")
    if token is not None:
        await ctx.conn.execute(INSERT_PARTY_F2_SQL, trip_id, token, "lead")
    for name in answers.companions:
        companion_id, companion_token = uuid.uuid4(), uuid.uuid4()
        await ctx.conn.execute(INSERT_PROFILE_SQL, companion_token, None, None, False)
        await ctx.conn.execute(
            INSERT_COMPANION_SQL,
            companion_id,
            await vault.encrypt("traveler", "display_name_enc", companion_id, name),
            await vault.name_index("traveler", "name", name),
            await vault.name_token_indexes("traveler", "name", name),
            await vault.encrypt("traveler", "profile_token_enc", companion_id, str(companion_token)),
            owner,
            lead_source,
        )
        await ctx.conn.execute(INSERT_PARTY_SQL, trip_id, companion_id, "companion")
        await ctx.conn.execute(INSERT_PARTY_F2_SQL, trip_id, companion_token, "companion")
    await _reply_task(ctx, trip_id, owner)
    await _message_note(ctx, vault, trip_id, traveler_id, answers)
    await _consent(ctx, traveler_id, answers, submission_id)
    await ctx.conn.execute(DONE_SQL, submission_id, "created", trip_id, traveler_id)
    await _after(ctx, vault, answers, trip_id, traveler_id, submission_id, owner)
    if history_id is not None:  # AA-959: workflows that start on a new inquiry
        await triggers.on_stage_enter(ctx, trip_id, "inquiry", history_id)
    ctx.audit(
        "intake_created", "allowed", "trip", str(trip_id), reason="new_inquiry" if match is None else "repeat_client"
    )
    return Outcome("created", trip_id, traveler_id)


async def _after(
    ctx: AnyContext,
    vault: Vault,
    answers: stage1.Stage1Answers,
    trip_id: uuid.UUID,
    traveler_id: uuid.UUID,
    submission_id: uuid.UUID,
    owner: uuid.UUID | None,
) -> None:
    """AA-943 (CRM-15): the call slot the inquiry picked (R15), the trip's Stage 1 completeness, its last activity."""
    if answers.call_slot is not None:
        await slots.book(
            ctx, slot=answers.call_slot, trip_id=trip_id, traveler_id=traveler_id, submission_id=submission_id,
            owner=owner,
        )  # fmt: skip
    await completeness.refresh(ctx, vault, trip_id, actor=ACTOR)
    await ctx.conn.execute("UPDATE crm.trip SET last_activity_at = now() WHERE trip_id = $1", trip_id)


async def _reply_task(ctx: AnyContext, trip_id: uuid.UUID, owner: uuid.UUID | None) -> None:
    await ctx.conn.execute(OPEN_REPLY_TASK_SQL, trip_id, REPLY_TASK, owner, datetime.now(UTC) + REPLY_WITHIN)


async def _message_note(
    ctx: AnyContext, vault: Vault, trip_id: uuid.UUID, traveler_id: uuid.UUID, answers: stage1.Stage1Answers
) -> None:
    if not answers.message:
        return
    note_id = uuid.uuid4()
    body = await vault.encrypt("note", "body_enc", note_id, answers.message)
    await ctx.conn.execute(INSERT_NOTE_SQL, note_id, trip_id, traveler_id, "inquiry_message", body)


async def _consent(
    ctx: AnyContext, traveler_id: uuid.UUID, answers: stage1.Stage1Answers, submission_id: uuid.UUID
) -> None:
    await ctx.conn.execute(INSERT_CONSENT_SQL, traveler_id, "contact", True, submission_id)
    await ctx.conn.execute(INSERT_CONSENT_SQL, traveler_id, "marketing", answers.consent_marketing, submission_id)


async def _profile_token(vault: Vault, traveler_id: uuid.UUID, blob: bytes | None) -> uuid.UUID | None:
    if blob is None:
        return None
    return uuid.UUID(await vault.decrypt("traveler", "profile_token_enc", traveler_id, blob))


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None
