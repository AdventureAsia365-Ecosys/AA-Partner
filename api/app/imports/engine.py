"""Classify and (when applying) write the rows of one import file (AA-962, card CRM-31; design v3 R6 "duplicate merge
+ staged import", data-privacy lane "import order traveler -> relation -> trip -> note via job").

One function, :func:`run`, serves the **dry run** (``apply=False``: inside the member's ``tenant_tx()``, counts and
row codes only, nothing written) and the **apply** (``apply=True``: inside the import job's ``system_tx()``, one chunk
of rows per transaction), so the preview shows exactly what the job will do. Each row gets one outcome:

=====================  ==========================================================================================
``create``             a new client / relationship / trip / note
``match``              clients only: the e-mail or phone is already a client (blind index), or a client an
                       earlier row of the file creates (code ``duplicate_contact``): the row's client_ref then
                       points to that client (its own ref when it has none, else an alias, crm-030
                       ``import_ref_alias_bidx``), nothing else changes, nothing is created (R6)
``already_imported``   the row's ref is already in the agency (``import_ref_bidx``, crm-030; a relationship that
                       exists): **re-running an import creates no duplicates** (card E test)
``duplicate_in_file``  an earlier row of the same file has the same ref (or relationship pair, trip / note ref)
``invalid``            a value does not pass (``invalid_<column>``) or a ref names nothing (``unknown_client_ref``,
                       ``unknown_trip_ref``, ``unknown_companion_ref``): the row is skipped, the report says why
=====================  ==========================================================================================

A created client whose normalised name equals an existing client's (or an earlier row's) also gets the warning code
``possible_duplicate``: it is created, and the pair shows in Clients > Duplicates for review and merge
(:mod:`app.imports.duplicates`).

Rules kept from the rest of the CRM: values are cleaned like the API's own forms; F1 values are encrypted under the
agency's vault DEK and indexed (e-mail, phone, name, name tokens) like ``app.travellers.service.create_traveler``;
imported clients get ``lead_source = 'import'`` and belong to the member who started the import (R8); a marketing
consent column writes one ``crm.consent`` row (``source = 'import'``) for a **new** client only, so a re-import never
turns an unsubscribed client back on. Trips arrive closed (``completed`` / ``cancelled`` / ``lost``, reason ``other``)
or as an open Inquiry, with their lead and companions as party twins (``trip_traveler`` + ``trip_party``) and one
``trip_stage_history`` row (reason ``import``). Notes are ``internal`` notes under the vault DEK.

Concurrency (apply): each chunk transaction first takes the per-agency import lock, then for client rows the intake's
e-mail and phone dedupe locks (``app.intake.process``), every e-mail lock before any phone lock, each list sorted: an
inquiry processed meanwhile cannot create the same client twice, and the order cannot deadlock with the intake's
(e-mail, then phone). Nothing here logs or returns a value from the file: results are row numbers and codes.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import asyncpg

from app.audit import MAX_OBJECT_IDS
from app.errors import Invalid
from app.imports import spec
from app.imports.formats import Row
from app.tenancy import AnyContext
from app.travellers import age_bands
from app.travellers.fields import clean_f1
from app.vault import Vault
from app.vault.blind_index import NormalisationError, normalise_phone

OUTCOMES = ("create", "match", "already_imported", "duplicate_in_file", "invalid")
MAX_ISSUES = 200
IMPORT_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended('crm_client_import:' || $1::text, 0))"
# The intake's dedupe lock (app.intake.process DEDUPE_LOCK_SQL): same keys, so an inquiry and an import serialise.
DEDUPE_LOCK_SQL = """
SELECT pg_advisory_xact_lock(hashtextextended($1::text || ':' || $2::text || ':' || encode($3::bytea, 'hex'), 0))
"""
# A client answers to its own ref and to its aliases (crm-030 import_ref_alias_bidx: a client merged into it, a row
# matched to it). Rows are ordered so a client's own ref wins over another client's alias of the same ref (the last
# row per key is kept by _lookup).
TRAVELER_BY_REF_SQL = """
SELECT k.k AS k, t.traveler_id AS id
  FROM unnest($1::bytea[]) AS k(k)
  JOIN crm.traveler t ON t.import_ref_bidx = k.k OR t.import_ref_alias_bidx @> ARRAY[k.k]
 WHERE t.deleted_at IS NULL
 ORDER BY t.import_ref_bidx IS NOT DISTINCT FROM k.k, t.traveler_id
"""
TRAVELER_BY_EMAIL_SQL = """
SELECT email_bidx AS k, traveler_id AS id FROM crm.traveler WHERE email_bidx = ANY($1::bytea[]) AND deleted_at IS NULL
"""
TRAVELER_BY_PHONE_SQL = """
SELECT phone_bidx AS k, traveler_id AS id FROM crm.traveler WHERE phone_bidx = ANY($1::bytea[]) AND deleted_at IS NULL
"""
TRAVELER_BY_NAME_SQL = """
SELECT DISTINCT name_bidx AS k FROM crm.traveler WHERE name_bidx = ANY($1::bytea[]) AND deleted_at IS NULL
"""
TRIP_BY_REF_SQL = """
SELECT import_ref_bidx AS k, trip_id AS id FROM crm.trip WHERE import_ref_bidx = ANY($1::bytea[]) AND deleted_at IS NULL
"""
NOTE_BY_REF_SQL = """
SELECT import_ref_bidx AS k, note_id AS id FROM crm.note WHERE import_ref_bidx = ANY($1::bytea[]) AND deleted_at IS NULL
"""
RELATIONS_SQL = """
SELECT from_traveler_id, to_traveler_id, kind FROM crm.traveler_relation
 WHERE deleted_at IS NULL AND (from_traveler_id = ANY($1::uuid[]) OR to_traveler_id = ANY($1::uuid[]))
"""
INSERT_PROFILE_SQL = "INSERT INTO crm.traveler_profile (traveler_token, marketing_ok) VALUES ($1, $2)"
INSERT_TRAVELER_SQL = """
INSERT INTO crm.traveler (traveler_id, display_name_enc, name_bidx, name_token_bidx, given_name_enc, family_name_enc,
                          email_enc, email_bidx, phone_enc, phone_bidx, preferred_channel, profile_token_enc,
                          owner_member_id, lead_source, dob_enc, nationality_enc, import_ref_bidx)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, 'import', $14, $15, $16)
"""
# The row's ref becomes the client's own ref when it has none, else one more alias: every ref an import accepted keeps
# resolving for the relationships, trips and notes that name it.
LINK_REF_SQL = """
UPDATE crm.traveler
   SET import_ref_bidx = coalesce(import_ref_bidx, $2),
       import_ref_alias_bidx = CASE
           WHEN import_ref_bidx IS NULL OR import_ref_bidx = $2 OR import_ref_alias_bidx @> ARRAY[$2::bytea]
           THEN import_ref_alias_bidx ELSE array_append(import_ref_alias_bidx, $2::bytea) END
 WHERE traveler_id = $1
"""
INSERT_CONSENT_SQL = """
INSERT INTO crm.consent (traveler_id, purpose, value, source) VALUES ($1, 'marketing', $2, 'import')
"""
INSERT_RELATION_SQL = """
INSERT INTO crm.traveler_relation (from_traveler_id, to_traveler_id, kind) VALUES ($1, $2, $3) RETURNING relation_id
"""
SET_REFERRER_SQL = """
UPDATE crm.traveler SET referrer_traveler_id = $2
 WHERE traveler_id = $1 AND referrer_traveler_id IS DISTINCT FROM $2
"""
INSERT_TRIP_SQL = """
INSERT INTO crm.trip (trip_id, title, stage, waiting_on, start_date, end_date, party_adults, owner_member_id,
                      lead_source, lost_reason_code, cancel_reason_code, import_ref_bidx)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'import', $9, $10, $11)
"""
INSERT_HISTORY_SQL = """
INSERT INTO crm.trip_stage_history (trip_id, from_stage, to_stage, reason_code) VALUES ($1, NULL, $2, 'import')
"""
PARTY_SOURCE_SQL = "SELECT dob_enc, profile_token_enc FROM crm.traveler WHERE traveler_id = $1"
PROFILE_POINTER_SQL = "UPDATE crm.traveler SET profile_token_enc = $2 WHERE traveler_id = $1"
EMPTY_PROFILE_SQL = "INSERT INTO crm.traveler_profile (traveler_token) VALUES ($1)"
INSERT_PARTY_SQL = "INSERT INTO crm.trip_traveler (trip_id, traveler_id, role) VALUES ($1, $2, $3)"
INSERT_PARTY_F2_SQL = """
INSERT INTO crm.trip_party (trip_id, traveler_token, role) VALUES ($1, $2, $3) RETURNING trip_party_id
"""
INSERT_NOTE_SQL = """
INSERT INTO crm.note (note_id, trip_id, traveler_id, kind, body_enc, import_ref_bidx, created_at)
VALUES ($1, $2, $3, 'internal', $4, $5, coalesce($6, now()))
"""


@dataclass(frozen=True)
class Result:
    row: int
    outcome: str
    codes: tuple[str, ...] = ()
    object_id: uuid.UUID | None = None


@dataclass
class State:
    """What earlier rows of the same run already claimed (in-file duplicates). One per dry run or job slice."""

    refs: set[bytes] = field(default_factory=set)
    # e-mail / phone blind index -> the client an earlier row created (None in a dry run)
    emails: dict[bytes, uuid.UUID | None] = field(default_factory=dict)
    phones: dict[bytes, uuid.UUID | None] = field(default_factory=dict)
    names: set[bytes] = field(default_factory=set)
    pairs: set[tuple[uuid.UUID, uuid.UUID]] = field(default_factory=set)
    referred: set[uuid.UUID] = field(default_factory=set)  # clients given a referrer by an earlier row


def summarise(results: Sequence[Result]) -> dict[str, Any]:
    """Counts per outcome, the warning count and the first :data:`MAX_ISSUES` row issues (row numbers and codes)."""
    counts = Counter(r.outcome for r in results)
    issues = [
        {"row": r.row, "outcome": r.outcome, "codes": list(r.codes)}
        for r in results
        if r.codes or r.outcome in ("invalid", "duplicate_in_file")
    ]
    return {
        "row_count": len(results),
        "counts": {o: counts.get(o, 0) for o in OUTCOMES},
        "possible_duplicate_count": sum(1 for r in results if "possible_duplicate" in r.codes),
        "issues": issues[:MAX_ISSUES],
        "issues_truncated": len(issues) > MAX_ISSUES,
    }


async def run(
    ctx: AnyContext,
    vault: Vault,
    entity: str,
    rows: Sequence[Row],
    *,
    apply: bool,
    member_id: uuid.UUID | None,
    region: str | None = None,
    state: State | None = None,
) -> list[Result]:
    if entity not in spec.SPECS:
        raise Invalid("invalid_entity", ", ".join(spec.ENTITIES))
    state = state if state is not None else State()
    if apply:
        await ctx.conn.execute(IMPORT_LOCK_SQL, str(ctx.tenant_id))
    if entity == "clients":
        results = await _clients(ctx, vault, rows, apply, member_id, region, state)
    elif entity == "relationships":
        results = await _relationships(ctx, vault, rows, apply, state)
    elif entity == "trips":
        results = await _trips(ctx, vault, rows, apply, member_id, state)
    else:
        results = await _notes(ctx, vault, rows, apply, state)
    if apply:
        created = tuple(str(r.object_id) for r in results if r.outcome == "create" and r.object_id)
        if created:
            table = {"clients": "traveler", "relationships": "traveler_relation", "trips": "trip", "notes": "note"}
            ctx.audit("import", "allowed", table[entity], None, reason=entity, object_ids=created[:MAX_OBJECT_IDS])
    return results


async def _lookup(ctx: AnyContext, sql: str, keys: list[bytes]) -> dict[bytes, uuid.UUID]:
    if not keys:
        return {}
    return {bytes(r["k"]): r["id"] for r in await ctx.conn.fetch(sql, list(dict.fromkeys(keys)))}


async def _client_ids(ctx: AnyContext, vault: Vault, refs: list[str]) -> dict[str, uuid.UUID]:
    """client_ref -> traveler id, for the refs already in the agency."""
    idx = {r: await vault.ref_index("traveler", "import_ref", r) for r in dict.fromkeys(refs)}
    found = await _lookup(ctx, TRAVELER_BY_REF_SQL, list(idx.values()))
    return {r: found[k] for r, k in idx.items() if k in found}


def _clean(cells: dict[str, str], column: str, cleaner: Any, codes: list[str], *args: Any) -> Any:
    raw = cells.get(column, "")
    try:
        return cleaner(raw, *args)
    except (ValueError, TypeError, Invalid, NormalisationError):
        codes.append(f"invalid_{column}")
        return None


# ── clients ────────────────────────────────────────────────────────────────────────────────────────
def _client_values(cells: dict[str, str], region: str | None) -> tuple[dict[str, Any], list[str]]:
    codes: list[str] = []
    values: dict[str, Any] = {"ref": _clean(cells, "client_ref", spec.ref, codes)}
    for column, field_name in (
        ("name", "display_name"),
        ("given_name", "given_name"),
        ("family_name", "family_name"),
        ("email", "email"),
        ("phone", "phone"),
        ("dob", "dob"),
        ("nationality", "nationality"),
    ):
        raw = cells.get(column, "").strip()
        if not raw:
            if column == "name":
                codes.append("invalid_name")
            values[field_name] = None
            continue
        try:
            if field_name == "phone" and region and not raw.startswith("+"):
                values[field_name] = normalise_phone(raw, region)
            elif field_name == "dob":
                day = spec.date(raw)
                values[field_name] = clean_f1("dob", day.isoformat() if day else None)
            else:
                values[field_name] = clean_f1(field_name, raw)
        except (ValueError, TypeError, Invalid, NormalisationError):
            codes.append(f"invalid_{column}")
            values[field_name] = None
    values["channel"] = _clean(cells, "preferred_channel", spec.channel, codes)
    values["marketing"] = _clean(cells, "marketing_consent", spec.flag, codes)
    return values, codes


async def _clients(
    ctx: AnyContext,
    vault: Vault,
    rows: Sequence[Row],
    apply: bool,
    member_id: uuid.UUID | None,
    region: str | None,
    state: State,
) -> list[Result]:
    shape = spec.SPECS["clients"]
    prepared: list[tuple[Row, dict[str, Any], list[str]]] = []
    for row in rows:
        values, codes = _client_values(spec.cells(shape, row.cells), region)
        if not codes:
            values["ref_idx"] = await vault.ref_index("traveler", "import_ref", values["ref"])
            values["email_idx"] = (
                await vault.email_index("traveler", "email", values["email"]) if values["email"] else None
            )
            values["phone_idx"] = (
                await vault.phone_index("traveler", "phone", values["phone"]) if values["phone"] else None
            )
            values["name_idx"] = await vault.name_index("traveler", "name", values["display_name"])
        prepared.append((row, values, codes))
    ok = [v for _, v, c in prepared if not c]
    emails = sorted({v["email_idx"] for v in ok if v["email_idx"]})
    phones = sorted({v["phone_idx"] for v in ok if v["phone_idx"]})
    if apply:
        for idx in emails:
            await ctx.conn.execute(DEDUPE_LOCK_SQL, "crm_intake_email", str(ctx.tenant_id), idx)
        for idx in phones:
            await ctx.conn.execute(DEDUPE_LOCK_SQL, "crm_intake_phone", str(ctx.tenant_id), idx)
    by_ref = await _lookup(ctx, TRAVELER_BY_REF_SQL, [v["ref_idx"] for v in ok])
    by_email = await _lookup(ctx, TRAVELER_BY_EMAIL_SQL, emails)
    by_phone = await _lookup(ctx, TRAVELER_BY_PHONE_SQL, phones)
    names = {
        bytes(r["k"])
        for r in (await ctx.conn.fetch(TRAVELER_BY_NAME_SQL, list({v["name_idx"] for v in ok})) if ok else [])
    }

    results: list[Result] = []
    for row, v, codes in prepared:
        if codes:
            results.append(Result(row.number, "invalid", tuple(codes)))
            continue
        if v["ref_idx"] in by_ref:
            results.append(Result(row.number, "already_imported", object_id=by_ref[v["ref_idx"]]))
            continue
        if v["ref_idx"] in state.refs:
            results.append(Result(row.number, "duplicate_in_file", ("duplicate_client_ref",)))
            continue
        existing = by_email.get(v["email_idx"]) or by_phone.get(v["phone_idx"])
        if existing is not None:
            state.refs.add(v["ref_idx"])
            if apply:
                await ctx.conn.execute(LINK_REF_SQL, existing, v["ref_idx"])
            results.append(Result(row.number, "match", object_id=existing))
            continue
        if (v["email_idx"] and v["email_idx"] in state.emails) or (v["phone_idx"] and v["phone_idx"] in state.phones):
            # An earlier row of this file created the client with this e-mail / phone (one client per contact, R6;
            # family members often share one): this row's ref points to that client too, so its relationships, trips
            # and notes resolve. The code tells the agency (a separate person needs their own e-mail or phone).
            earlier = state.emails.get(v["email_idx"]) if v["email_idx"] in state.emails else None
            if earlier is None and v["phone_idx"] in state.phones:
                earlier = state.phones[v["phone_idx"]]
            state.refs.add(v["ref_idx"])
            if apply and earlier is not None:
                await ctx.conn.execute(LINK_REF_SQL, earlier, v["ref_idx"])
            results.append(Result(row.number, "match", ("duplicate_contact",), earlier))
            continue
        warn = ("possible_duplicate",) if v["name_idx"] in names or v["name_idx"] in state.names else ()
        state.refs.add(v["ref_idx"])
        state.names.add(v["name_idx"])
        traveler_id = await _insert_client(ctx, vault, v, member_id) if apply else None
        if v["email_idx"]:
            state.emails[v["email_idx"]] = traveler_id
        if v["phone_idx"]:
            state.phones[v["phone_idx"]] = traveler_id
        results.append(Result(row.number, "create", warn, traveler_id))
    return results


async def _insert_client(ctx: AnyContext, vault: Vault, v: dict[str, Any], member_id: uuid.UUID | None) -> uuid.UUID:
    traveler_id = uuid.uuid4()
    token = uuid.uuid4()
    await ctx.conn.execute(INSERT_PROFILE_SQL, token, bool(v["marketing"]))

    async def enc(name: str) -> bytes | None:
        value = v[name]
        return await vault.encrypt("traveler", f"{name}_enc", traveler_id, value) if value is not None else None

    name = v["display_name"]
    await ctx.conn.execute(
        INSERT_TRAVELER_SQL,
        traveler_id,
        await enc("display_name"),
        v["name_idx"],
        await vault.name_token_indexes("traveler", "name", name),
        await enc("given_name"),
        await enc("family_name"),
        await enc("email"),
        v["email_idx"],
        await enc("phone"),
        v["phone_idx"],
        v["channel"],
        await vault.encrypt("traveler", "profile_token_enc", traveler_id, str(token)),
        member_id,
        await enc("dob"),
        await enc("nationality"),
        v["ref_idx"],
    )
    if v["marketing"] is not None:
        await ctx.conn.execute(INSERT_CONSENT_SQL, traveler_id, v["marketing"])
    return traveler_id


# ── relationships ──────────────────────────────────────────────────────────────────────────────────
async def _relationships(ctx: AnyContext, vault: Vault, rows: Sequence[Row], apply: bool, state: State) -> list[Result]:
    shape = spec.SPECS["relationships"]
    prepared: list[tuple[Row, dict[str, Any], list[str]]] = []
    for row in rows:
        cells = spec.cells(shape, row.cells)
        codes: list[str] = []
        values = {
            "a": _clean(cells, "client_ref", spec.ref, codes),
            "b": _clean(cells, "related_client_ref", spec.ref, codes),
            "kind": _clean(cells, "relation", spec.relation, codes),
        }
        prepared.append((row, values, codes))
    ids = await _client_ids(ctx, vault, [v[k] for _, v, c in prepared if not c for k in ("a", "b")])
    known = list(set(ids.values()))
    pairs: set[tuple[uuid.UUID, uuid.UUID]] = set()
    has_referrer: set[uuid.UUID] = set()
    for r in await ctx.conn.fetch(RELATIONS_SQL, known) if known else []:
        pairs.add(_pair(r["from_traveler_id"], r["to_traveler_id"]))
        if r["kind"] == "referrer":
            has_referrer.add(r["from_traveler_id"])
    results: list[Result] = []
    for row, v, codes in prepared:
        if not codes:
            for side, column in (("a", "client_ref"), ("b", "related_client_ref")):
                if v[side] not in ids:
                    codes.append(f"unknown_{column}")
        if not codes and ids[v["a"]] == ids[v["b"]]:
            codes.append("same_client")
        if codes:
            results.append(Result(row.number, "invalid", tuple(dict.fromkeys(codes))))
            continue
        a, b = ids[v["a"]], ids[v["b"]]
        pair = _pair(a, b)
        if pair in pairs:
            results.append(Result(row.number, "already_imported"))
            continue
        if pair in state.pairs:
            results.append(Result(row.number, "duplicate_in_file", ("duplicate_relationship",)))
            continue
        # The client who gets a referrer: A "referrer" B = B referred A (A's referrer); A "referred" B = B's referrer.
        referred = a if v["kind"] == "referrer" else b if v["kind"] == "referred" else None
        if referred is not None and (referred in has_referrer or referred in state.referred):
            results.append(Result(row.number, "invalid", ("referrer_exists",)))
            continue
        relation_id = None
        if apply:
            try:
                async with ctx.conn.transaction():
                    relation_id = await ctx.conn.fetchval(INSERT_RELATION_SQL, a, b, v["kind"])
            except asyncpg.UniqueViolationError:
                results.append(Result(row.number, "invalid", ("relation_conflict",)))
                continue
            if v["kind"] == "referrer":
                await ctx.conn.execute(SET_REFERRER_SQL, a, b)
            elif v["kind"] == "referred":
                await ctx.conn.execute(SET_REFERRER_SQL, b, a)
        state.pairs.add(pair)
        if referred is not None:
            state.referred.add(referred)
        results.append(Result(row.number, "create", object_id=relation_id))
    return results


def _pair(a: uuid.UUID, b: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    return (a, b) if str(a) < str(b) else (b, a)


# ── trips ──────────────────────────────────────────────────────────────────────────────────────────
async def _trips(
    ctx: AnyContext, vault: Vault, rows: Sequence[Row], apply: bool, member_id: uuid.UUID | None, state: State
) -> list[Result]:
    shape = spec.SPECS["trips"]
    today = dt.date.today()
    prepared: list[tuple[Row, dict[str, Any], list[str]]] = []
    for row in rows:
        cells = spec.cells(shape, row.cells)
        codes: list[str] = []
        v: dict[str, Any] = {
            "ref": _clean(cells, "trip_ref", spec.ref, codes),
            "lead": _clean(cells, "client_ref", spec.ref, codes),
            "title": _clean(cells, "title", spec.text, codes, spec.MAX_TITLE),
            "start": _clean(cells, "start_date", spec.date, codes),
            "end": _clean(cells, "end_date", spec.date, codes),
            "status": _clean(cells, "status", spec.trip_status, codes),
            "adults": _clean(cells, "adults", spec.adults, codes),
            "companions": _clean(cells, "companion_refs", spec.refs, codes) or [],
        }
        if v["start"] and v["end"] and v["end"] < v["start"]:
            codes.append("invalid_end_date")
        if not codes:
            v["ref_idx"] = await vault.ref_index("trip", "import_ref", v["ref"])
        prepared.append((row, v, codes))
    ok = [v for _, v, c in prepared if not c]
    by_ref = await _lookup(ctx, TRIP_BY_REF_SQL, [v["ref_idx"] for v in ok])
    clients = await _client_ids(ctx, vault, [r for v in ok for r in (v["lead"], *v["companions"])])
    results: list[Result] = []
    for row, v, codes in prepared:
        if not codes:
            if v["lead"] not in clients:
                codes.append("unknown_client_ref")
            if any(c not in clients for c in v["companions"]):
                codes.append("unknown_companion_ref")
        if codes:
            results.append(Result(row.number, "invalid", tuple(codes)))
            continue
        if v["ref_idx"] in by_ref:
            results.append(Result(row.number, "already_imported", object_id=by_ref[v["ref_idx"]]))
            continue
        if v["ref_idx"] in state.refs:
            results.append(Result(row.number, "duplicate_in_file", ("duplicate_trip_ref",)))
            continue
        state.refs.add(v["ref_idx"])
        lead = clients[v["lead"]]
        party = [lead, *dict.fromkeys(c for c in (clients[x] for x in v["companions"]) if c != lead)]
        stage = v["status"] or ("completed" if (v["end"] or v["start"] or today) < today else "inquiry")
        trip_id = await _insert_trip(ctx, vault, v, stage, party, member_id) if apply else None
        results.append(Result(row.number, "create", object_id=trip_id))
    return results


async def _insert_trip(
    ctx: AnyContext,
    vault: Vault,
    v: dict[str, Any],
    stage: str,
    party: list[uuid.UUID],
    member_id: uuid.UUID | None,
) -> uuid.UUID:
    trip_id = uuid.uuid4()
    await ctx.conn.execute(
        INSERT_TRIP_SQL,
        trip_id,
        v["title"],
        stage,
        "agency" if stage == "inquiry" else "none",
        v["start"],
        v["end"],
        min(50, max(v["adults"] or len(party), 1)),
        member_id,
        "other" if stage == "lost" else None,
        "other" if stage == "cancelled" else None,
        v["ref_idx"],
    )
    await ctx.conn.execute(INSERT_HISTORY_SQL, trip_id, stage)
    for position, traveler_id in enumerate(party):
        role = "lead" if position == 0 else "companion"
        source = await ctx.conn.fetchrow(PARTY_SOURCE_SQL, traveler_id)
        token = await _profile_token(ctx, vault, traveler_id, source["profile_token_enc"])
        await ctx.conn.execute(INSERT_PARTY_SQL, trip_id, traveler_id, role)
        party_id = await ctx.conn.fetchval(INSERT_PARTY_F2_SQL, trip_id, token, role)
        await age_bands.recompute_party_member(ctx, vault, party_id, traveler_id, source["dob_enc"], v["start"])  # type: ignore[arg-type]
    return trip_id


async def _profile_token(ctx: AnyContext, vault: Vault, traveler_id: uuid.UUID, blob: bytes | None) -> uuid.UUID:
    if blob is not None:
        return uuid.UUID(await vault.decrypt("traveler", "profile_token_enc", traveler_id, blob))
    token = uuid.uuid4()
    await ctx.conn.execute(EMPTY_PROFILE_SQL, token)
    pointer = await vault.encrypt("traveler", "profile_token_enc", traveler_id, str(token))
    await ctx.conn.execute(PROFILE_POINTER_SQL, traveler_id, pointer)
    return token


# ── notes ──────────────────────────────────────────────────────────────────────────────────────────
def note_ref(cells: dict[str, str]) -> str:
    """The note's ref: its ``note_ref``, or a hash of what it is about and says (so a re-run without refs adds
    nothing)."""
    given = cells.get("note_ref", "").strip()
    if given:
        return spec.ref(given)
    basis = "\x1f".join(cells.get(k, "").strip() for k in ("client_ref", "trip_ref", "date", "text"))
    return "auto:" + hashlib.sha256(basis.encode("utf-8")).hexdigest()


async def _notes(ctx: AnyContext, vault: Vault, rows: Sequence[Row], apply: bool, state: State) -> list[Result]:
    shape = spec.SPECS["notes"]
    prepared: list[tuple[Row, dict[str, Any], list[str]]] = []
    for row in rows:
        cells = spec.cells(shape, row.cells)
        codes: list[str] = []
        v: dict[str, Any] = {
            "text": _clean(cells, "text", _note_text, codes),
            "date": _clean(cells, "date", spec.date, codes),
            "client": cells.get("client_ref", "").strip() or None,
            "trip": cells.get("trip_ref", "").strip() or None,
        }
        try:
            v["client"] = spec.ref(v["client"]) if v["client"] else None
            v["trip"] = spec.ref(v["trip"]) if v["trip"] else None
            v["ref"] = note_ref(cells)
        except ValueError:
            codes.append("invalid_ref")
        if not codes and not (v["client"] or v["trip"]):
            codes.append("missing_subject")
        if not codes:
            v["ref_idx"] = await vault.ref_index("note", "import_ref", v["ref"])
            v["trip_idx"] = await vault.ref_index("trip", "import_ref", v["trip"]) if v["trip"] else None
        prepared.append((row, v, codes))
    ok = [v for _, v, c in prepared if not c]
    by_ref = await _lookup(ctx, NOTE_BY_REF_SQL, [v["ref_idx"] for v in ok])
    trips = await _lookup(ctx, TRIP_BY_REF_SQL, [v["trip_idx"] for v in ok if v["trip_idx"]])
    clients = await _client_ids(ctx, vault, [v["client"] for v in ok if v["client"]])
    results: list[Result] = []
    for row, v, codes in prepared:
        if not codes:
            if v["client"] and v["client"] not in clients:
                codes.append("unknown_client_ref")
            if v["trip"] and v["trip_idx"] not in trips:
                codes.append("unknown_trip_ref")
        if codes:
            results.append(Result(row.number, "invalid", tuple(codes)))
            continue
        if v["ref_idx"] in by_ref:
            results.append(Result(row.number, "already_imported", object_id=by_ref[v["ref_idx"]]))
            continue
        if v["ref_idx"] in state.refs:
            results.append(Result(row.number, "duplicate_in_file", ("duplicate_note",)))
            continue
        state.refs.add(v["ref_idx"])
        note_id = None
        if apply:
            note_id = uuid.uuid4()
            at = dt.datetime.combine(v["date"], dt.time(12), tzinfo=dt.UTC) if v["date"] else None
            await ctx.conn.execute(
                INSERT_NOTE_SQL,
                note_id,
                trips.get(v["trip_idx"]) if v["trip_idx"] else None,
                clients.get(v["client"]) if v["client"] else None,
                await vault.encrypt("note", "body_enc", note_id, v["text"]),
                v["ref_idx"],
                at,
            )
        results.append(Result(row.number, "create", object_id=note_id))
    return results


def _note_text(value: str) -> str:
    return spec.text(value.replace("\r\n", "\n"), spec.MAX_NOTE)
