"""What one signed-in traveller has in the portal (card CRM-24a, AA-953; design v3 M11, R22, R33, Fix E4, §2.9 rules
2-3). Every function takes the open context of :mod:`app.portal.public` (the only module that opens the portal link)
and the traveller the session acts as; it never opens a context and never imports ``app.travellers.reveal``.

**Who reaches whom (Fix E4, "companion e-mails asked at the invite").** An e-mail address belongs to one client of the
agency (the CRM refuses a second client with the same address), so "one family e-mail serves two travellers" means: a
companion without an address of their own is reached through the lead's address. :func:`delivery_of` names the
traveller whose address gets a person's codes (their own, else the lead's when the person is a companion, else none);
the people one address reaches (:func:`group_of`) are the "Who are you?" choices after a code, and a session may
switch between them without a new code (the address owner proved control of it). **A session is bound to the
address its code went to** (``otp_challenge.sent_to_bidx``, crm-029): it acts as a traveller only while
:func:`delivery_bidx` of that traveller is still that address. So once a companion gets an address of their own (the
lead at the invite, :func:`set_companion_email`, or the agency), or the agency changes the lead's address, every session
that acted as them through the old address ends at its next request, whatever link it is presented with.

**Per person (R22, §2.9 rules 2-3).** :func:`documents` lists only the documents the agency marked visible to
travellers that belong to the trip as a whole or to this traveller; :func:`open_document` refuses (403) a write-only
document (a passport scan: card R), a document not shared with travellers, and another traveller's document.
:func:`services` lists the trip's lines the agency shows to travellers, never a cancelled one (the database refuses a
visible cancelled line too, ``booking_line_cancelled_hidden_chk``) and never a price. :func:`itinerary` is the
accepted proposal's days (titles, places, IANA zones). :func:`flights` / :func:`save_flights` keep this traveller's
arrival and departure flight (R33: local time at the airport and its zone, so an overnight flight shows the day it
lands). :func:`upload_passport_scan` stores a write-only passport scan (never shown back).
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import re
import uuid
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.documents import service as documents
from app.documents.store import DocumentStore
from app.errors import Conflict, Forbidden, Invalid, NotFound, RateLimited, Unavailable
from app.proposals import rules
from app.tenancy import AnyContext
from app.travellers.fields import clean_f1
from app.vault import Vault

DIRECTIONS = ("arrival", "departure")
FLIGHT_NO = re.compile(r"^[A-Z0-9]{2}[A-Z]?[0-9]{1,4}[A-Z]?$")
AIRPORT = re.compile(r"^[A-Z]{3}$")
ZONE = re.compile(r"^(UTC|[A-Z][A-Za-z_]+(/[A-Za-z0-9_+-]+){1,2})$")
FIRST_DAY = dt.datetime(2000, 1, 1)
LAST_DAY = dt.datetime(2100, 1, 1)

PARTY_SQL = """
SELECT tt.traveler_id, tt.role, t.email_bidx IS NOT NULL AS has_email, t.email_bidx, t.display_name_enc,
       a.access_id, a.status AS access_status
  FROM crm.trip_traveler tt
  JOIN crm.traveler t ON t.traveler_id = tt.traveler_id AND t.deleted_at IS NULL
  LEFT JOIN crm.traveler_access a
         ON a.trip_id = tt.trip_id AND a.traveler_id = tt.traveler_id AND a.status <> 'revoked' AND a.deleted_at IS NULL
 WHERE tt.trip_id = $1 AND tt.deleted_at IS NULL
 ORDER BY tt.role = 'lead' DESC, tt.created_at, tt.traveler_id
"""
DOCUMENTS_SQL = """
SELECT document_id, trip_id, traveler_id, kind, size_bytes, content_type, filename_enc, created_at FROM crm.document
 WHERE deleted_at IS NULL AND visible_to_travelers AND NOT write_only
   AND ((trip_id = $1 AND (traveler_id IS NULL OR traveler_id = $2)) OR (trip_id IS NULL AND traveler_id = $2))
 ORDER BY created_at DESC, document_id
"""
DOCUMENT_SQL = """
SELECT document_id, trip_id, traveler_id, kind, s3_key, sha256, size_bytes, content_type, filename_enc,
       visible_to_travelers, write_only, created_at
  FROM crm.document WHERE document_id = $1 AND deleted_at IS NULL
"""
# A traveller replaces a bad photo a few times at most; the agency trashes the old ones (abuse guard: 7 MiB each).
MAX_LIVE_SCANS = 5
SCAN_COUNT_SQL = """
SELECT count(*) FROM crm.document WHERE traveler_id = $1 AND kind = 'passport_scan' AND deleted_at IS NULL
"""
SCAN_ON_FILE_SQL = """
SELECT EXISTS (SELECT 1 FROM crm.document WHERE traveler_id = $1 AND kind = 'passport_scan' AND deleted_at IS NULL)
"""
SERVICES_SQL = """
SELECT title, service_from, service_to, status, confirm_class, collect FROM crm.booking_line
 WHERE trip_id = $1 AND visible_to_travelers AND status <> 'cancelled' AND deleted_at IS NULL
 ORDER BY service_from NULLS LAST, title, booking_line_id
"""
ITINERARY_SQL = """
SELECT d.day_no, d.title, d.place, d.tz, coalesce(p.starts_on, t.start_date) AS starts_on
  FROM crm.proposal p
  JOIN crm.trip t ON t.trip_id = p.trip_id
  JOIN crm.proposal_day d ON d.proposal_id = p.proposal_id AND d.deleted_at IS NULL
 WHERE p.trip_id = $1 AND p.status = 'accepted' AND p.deleted_at IS NULL
 ORDER BY d.day_no
"""
FLIGHTS_SQL = """
SELECT traveler_flight_id, direction, flight_no_enc, airport_iata, sched_local, tz FROM crm.traveler_flight
 WHERE trip_id = $1 AND traveler_id = $2 AND deleted_at IS NULL
 ORDER BY direction = 'departure', direction
"""
FLIGHT_ROW_SQL = """
SELECT traveler_flight_id FROM crm.traveler_flight
 WHERE trip_id = $1 AND traveler_id = $2 AND direction = $3 AND deleted_at IS NULL
   FOR UPDATE
"""
INSERT_FLIGHT_SQL = """
INSERT INTO crm.traveler_flight (traveler_flight_id, trip_id, traveler_id, direction, flight_no_enc, airport_iata,
                                 sched_local, tz, entered_by_kind)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'traveler')
"""
UPDATE_FLIGHT_SQL = """
UPDATE crm.traveler_flight SET flight_no_enc = $2, airport_iata = $3, sched_local = $4, tz = $5,
       entered_by_kind = 'traveler'
 WHERE traveler_flight_id = $1
"""
DELETE_FLIGHT_SQL = "DELETE FROM crm.traveler_flight WHERE traveler_flight_id = $1"
TRAVELER_EMAIL_SQL = "SELECT email_enc FROM crm.traveler WHERE traveler_id = $1 AND deleted_at IS NULL FOR UPDATE"
EMAIL_TAKEN_SQL = "SELECT 1 FROM crm.traveler WHERE email_bidx = $1 AND deleted_at IS NULL AND traveler_id <> $2"
SET_EMAIL_SQL = "UPDATE crm.traveler SET email_enc = $2, email_bidx = $3 WHERE traveler_id = $1"
# The client record is shared by every trip of that client: a lead sets an address only for someone on this trip alone.
OTHER_TRIP_SQL = """
SELECT EXISTS (SELECT 1 FROM crm.trip_traveler tt JOIN crm.trip t ON t.trip_id = tt.trip_id AND t.deleted_at IS NULL
                WHERE tt.traveler_id = $1 AND tt.trip_id <> $2 AND tt.deleted_at IS NULL)
"""
# Refused companion addresses per lead access (the denied audit events): the cap that keeps email_in_use from listing
# the agency's clients.
MAX_EMAIL_REFUSALS = 3
EMAIL_REFUSAL_WINDOW_H = 24
EMAIL_REFUSALS_SQL = """
SELECT count(*) FROM crm.audit_event
 WHERE object_kind = 'traveler_access' AND object_id = $1 AND action = 'portal_companion_email' AND outcome = 'denied'
   AND at > now() - make_interval(hours => $2)
"""


@dataclass(frozen=True)
class Member:
    traveler_id: uuid.UUID
    role: str
    has_email: bool
    name_enc: bytes | None
    access_id: uuid.UUID | None
    access_status: str | None
    # The blind index of the person's own address (None without one): a session keeps the index of the address its
    # code went to and stays valid only while that address still reaches the person it acts as (public._signed_in).
    email_bidx: bytes | None = None


async def party(ctx: AnyContext, trip_id: uuid.UUID) -> list[Member]:
    return [
        Member(
            r["traveler_id"],
            r["role"],
            r["has_email"],
            r["display_name_enc"],
            r["access_id"],
            r["access_status"],
            bytes(r["email_bidx"]) if r.get("email_bidx") is not None else None,
        )
        for r in await ctx.conn.fetch(PARTY_SQL, trip_id)
    ]


def delivery_of(members: list[Member], traveler_id: uuid.UUID) -> uuid.UUID | None:
    """The traveller whose e-mail address receives ``traveler_id``'s codes: their own, else (a companion) the lead's,
    else None (no address to send a code to)."""
    me = next((m for m in members if m.traveler_id == traveler_id), None)
    if me is None:
        return None
    if me.has_email:
        return me.traveler_id
    lead = next((m for m in members if m.role == "lead"), None)
    if me.role == "companion" and lead is not None and lead.has_email:
        return lead.traveler_id
    return None


def delivery_bidx(members: list[Member], traveler_id: uuid.UUID) -> bytes | None:
    """The blind index of the address that receives ``traveler_id``'s codes now (see :func:`delivery_of`)."""
    to = delivery_of(members, traveler_id)
    return next((m.email_bidx for m in members if m.traveler_id == to), None) if to is not None else None


def group_of(members: list[Member], traveler_id: uuid.UUID) -> list[Member]:
    """The travellers with live portal access whose codes go to the same address as ``traveler_id``'s (the "Who are
    you?" choices); empty when that person has no address to send a code to."""
    delivery = delivery_of(members, traveler_id)
    if delivery is None:
        return []
    return [m for m in members if m.access_id is not None and delivery_of(members, m.traveler_id) == delivery]


async def name_of(ctx: AnyContext, vault: Vault, member: Member) -> str | None:
    if member.name_enc is None:
        return None
    return await vault.decrypt("traveler", "display_name_enc", member.traveler_id, member.name_enc)


async def names(ctx: AnyContext, vault: Vault, members: list[Member], reason: str) -> dict[uuid.UUID, str | None]:
    """Display names of the given travellers (the picker, the lead's party list), one audit event for the set."""
    out = {m.traveler_id: await name_of(ctx, vault, m) for m in members}
    if out:
        ctx.audit(
            "portal_names",
            "allowed",
            "trip",
            None,
            fields=("display_name",),
            reason=reason,
            object_ids=tuple(sorted(str(t) for t in out)),
        )
    return out


# ── documents (§2.9 rule 2: "documents flagged visible to travellers"; rule 3: never a passport) ───────────────────


async def documents_view(ctx: AnyContext, vault: Vault, trip_id: uuid.UUID, traveler_id: uuid.UUID) -> list[dict]:
    rows = await ctx.conn.fetch(DOCUMENTS_SQL, trip_id, traveler_id)
    return [
        {
            "document_id": str(r["document_id"]),
            "kind": r["kind"],
            "filename": await vault.decrypt("document", "filename_enc", r["document_id"], r["filename_enc"]),
            "content_type": r["content_type"],
            "size_bytes": r["size_bytes"],
            "for": "me" if r["traveler_id"] is not None else "trip",
            "created_at": r["created_at"].isoformat(),
        }
        for r in rows
    ]


async def open_document(
    ctx: AnyContext,
    vault: Vault,
    store: DocumentStore,
    trip_id: uuid.UUID,
    traveler_id: uuid.UUID,
    access_id: uuid.UUID,
    document_id: uuid.UUID,
) -> dict[str, Any]:
    """The document's bytes for this traveller, or 403: write-only (a passport scan, card R), not shared with
    travellers, or someone else's (another traveller, another trip). Another agency's document is not found (RLS)."""
    row = await ctx.conn.fetchrow(DOCUMENT_SQL, document_id)
    if row is None:
        raise NotFound("document_not_found")
    refusal = None
    if row["write_only"]:
        refusal = "document_write_only"
    elif not row["visible_to_travelers"]:
        refusal = "document_not_shared"
    elif (row["trip_id"] is not None and row["trip_id"] != trip_id) or (
        row["traveler_id"] is not None and row["traveler_id"] != traveler_id
    ):
        refusal = "document_not_yours"
    elif row["trip_id"] is None and row["traveler_id"] is None:
        refusal = "document_not_yours"
    if refusal is not None:
        ctx.audit("portal_document", "denied", "document", str(document_id), reason=refusal)
        raise Forbidden(refusal, "this document is not shared with you")
    sealed = await store.get(row["s3_key"])
    if hashlib.sha256(sealed).digest() != bytes(row["sha256"]):
        ctx.audit("portal_document", "denied", "document", str(document_id), reason="digest_mismatch")
        raise Unavailable("document_integrity", "the stored document does not match its digest")
    content = await vault.decrypt_bytes("document", "content", document_id, sealed, purpose="docs")
    filename = await vault.decrypt("document", "filename_enc", document_id, row["filename_enc"])
    ctx.audit("portal_document", "allowed", "document", str(document_id), object_ids=(str(access_id),))
    return {
        "document_id": str(document_id),
        "filename": filename,
        "content_type": row["content_type"],
        "content_b64": base64.b64encode(content).decode("ascii"),
    }


async def passport_scan_on_file(ctx: AnyContext, traveler_id: uuid.UUID) -> bool:
    return bool(await ctx.conn.fetchval(SCAN_ON_FILE_SQL, traveler_id))


async def upload_passport_scan(
    ctx: AnyContext, vault: Vault, store: DocumentStore, trip_id: uuid.UUID, traveler_id: uuid.UUID, raw: Any
) -> dict[str, Any]:
    """Stage 3 "passport upload-only, never shown back" (delivery plan CRM-24): a write-only ``passport_scan`` of this
    traveller on this trip, uploaded by the traveller. The answer never carries the content."""
    if not isinstance(raw, dict):
        raise Invalid("invalid_submission", "an object", fields=["body"])
    content_b64, filename, content_type = raw.get("content_b64"), raw.get("filename"), raw.get("content_type")
    if not isinstance(content_b64, str) or not isinstance(filename, str) or not isinstance(content_type, str):
        raise Invalid("invalid_submission", "the file, its name and its type", fields=["content_b64"])
    if int(await ctx.conn.fetchval(SCAN_COUNT_SQL, traveler_id) or 0) >= MAX_LIVE_SCANS:
        raise Conflict("scan_limit", "your travel agent has your passport scans; ask them to remove an old one")
    content = documents.decode(content_b64)
    saved = await documents.upload(
        ctx,  # type: ignore[arg-type]  # the portal's context: no member, the uploader is the traveller
        vault,
        store,
        traveler_id=traveler_id,
        trip_id=trip_id,
        kind="passport_scan",
        filename=filename,
        content_type=content_type,
        content=content,
        uploaded_by_kind="traveler",
    )
    return {"received": True, "kind": "passport_scan", "size_bytes": saved["size_bytes"]}


# ── the trip as the traveller sees it (no price, no cancelled line, no supplier detail) ─────────────────────────────


async def services(ctx: AnyContext, trip_id: uuid.UUID) -> list[dict[str, Any]]:
    return [
        {
            "title": r["title"],
            "from": _iso(r["service_from"]),
            "to": _iso(r["service_to"]),
            "status": r["status"],
            "confirm_class": r["confirm_class"],
            "pay_on_arrival": r["collect"] == "on_arrival",
        }
        for r in await ctx.conn.fetch(SERVICES_SQL, trip_id)
    ]


async def itinerary(ctx: AnyContext, trip_id: uuid.UUID) -> list[dict[str, Any]]:
    out = []
    for r in await ctx.conn.fetch(ITINERARY_SQL, trip_id):
        day = r["starts_on"] + dt.timedelta(days=r["day_no"] - 1) if r["starts_on"] and r["day_no"] >= 1 else None
        out.append(
            {
                "day_no": r["day_no"],
                "before_you_go": r["day_no"] == 0,
                "date": _iso(day),
                "title": r["title"],
                "place": r["place"],
                "tz": r["tz"],
                "utc_offset": rules.utc_offset(r["tz"], day or r["starts_on"]),
            }
        )
    return out


# ── flights (R33) ────────────────────────────────────────────────────────────────────────────────────────────────


def _offset(local: dt.datetime, tz: str) -> str:
    offset = local.replace(tzinfo=ZoneInfo(tz)).utcoffset() or dt.timedelta(0)
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    hours, mins = divmod(abs(minutes), 60)
    return f"{sign}{hours:02d}:{mins:02d}"


def flight_view(direction: str, flight_no: str, airport: str, local: dt.datetime, tz: str, start: dt.date | None):
    return {
        "direction": direction,
        "flight_no": flight_no,
        "airport": airport,
        "local_time": local.strftime("%Y-%m-%dT%H:%M"),
        "local_date": local.date().isoformat(),
        "tz": tz,
        "utc_offset": _offset(local, tz),
        # R33: an overnight flight lands on its own local date; 0 = the trip's first day, -1 = the day before.
        "days_from_start": (local.date() - start).days if start is not None else None,
    }


async def flights(
    ctx: AnyContext, vault: Vault, trip_id: uuid.UUID, traveler_id: uuid.UUID, start: dt.date | None
) -> list[dict[str, Any]]:
    out = []
    for r in await ctx.conn.fetch(FLIGHTS_SQL, trip_id, traveler_id):
        number = await vault.decrypt("traveler_flight", "flight_no_enc", r["traveler_flight_id"], r["flight_no_enc"])
        out.append(flight_view(r["direction"], number, r["airport_iata"], r["sched_local"], r["tz"], start))
    return out


def clean_flight(raw: Any) -> tuple[str, tuple[str, str, dt.datetime, str] | None]:
    """(direction, (flight number, airport, local time, zone)) or (direction, None) to remove it; 422 otherwise."""
    if not isinstance(raw, dict) or raw.get("direction") not in DIRECTIONS:
        raise Invalid("invalid_flight", "arrival or departure", fields=["direction"])
    direction = raw["direction"]
    if raw.get("remove") is True:
        return direction, None
    bad: list[str] = []
    number = raw.get("flight_no")
    number = re.sub(r"[\s-]", "", number).upper() if isinstance(number, str) else ""
    if not FLIGHT_NO.match(number):
        bad.append("flight_no")
    airport = raw.get("airport").strip().upper() if isinstance(raw.get("airport"), str) else ""
    if not AIRPORT.match(airport):
        bad.append("airport")
    local: dt.datetime | None = None
    try:
        local = dt.datetime.fromisoformat(str(raw.get("local_time")))
        if local.tzinfo is not None or not FIRST_DAY <= local < LAST_DAY:
            local = None
    except ValueError:
        local = None
    if local is None:
        bad.append("local_time")
    tz = raw.get("tz") if isinstance(raw.get("tz"), str) else ""
    try:
        ZoneInfo(tz) if ZONE.match(tz) else None
    except (ZoneInfoNotFoundError, ValueError):
        tz = ""
    if not ZONE.match(tz):
        bad.append("tz")
    if bad:
        raise Invalid("invalid_flight", "fields to fix", fields=bad)
    return direction, (number, airport, local.replace(second=0, microsecond=0), tz)  # type: ignore[union-attr]


async def save_flights(
    ctx: AnyContext, vault: Vault, trip_id: uuid.UUID, traveler_id: uuid.UUID, raw: Any, start: dt.date | None
) -> list[dict[str, Any]]:
    items = raw.get("flights") if isinstance(raw, dict) else None
    if not isinstance(items, list) or not 1 <= len(items) <= len(DIRECTIONS):
        raise Invalid("invalid_submission", "one or two flights", fields=["flights"])
    cleaned = [clean_flight(i) for i in items]
    if len({d for d, _ in cleaned}) != len(cleaned):
        raise Invalid("invalid_submission", "one flight per direction", fields=["flights"])
    for direction, flight in cleaned:
        current = await ctx.conn.fetchval(FLIGHT_ROW_SQL, trip_id, traveler_id, direction)
        if flight is None:
            if current is not None:
                await ctx.conn.execute(DELETE_FLIGHT_SQL, current)
            continue
        number, airport, local, tz = flight
        row_id = current or uuid.uuid4()
        sealed = await vault.encrypt("traveler_flight", "flight_no_enc", row_id, number)
        if current is None:
            await ctx.conn.execute(
                INSERT_FLIGHT_SQL, row_id, trip_id, traveler_id, direction, sealed, airport, local, tz
            )
        else:
            await ctx.conn.execute(UPDATE_FLIGHT_SQL, row_id, sealed, airport, local, tz)
    ctx.audit("portal_flights", "allowed", "traveler", str(traveler_id), fields=tuple(sorted(d for d, _ in cleaned)))
    return await flights(ctx, vault, trip_id, traveler_id, start)


# ── the lead: companion e-mails at the invite ──────────────────────────────────────────────────────────────────


async def email_refusals(ctx: AnyContext, access_id: uuid.UUID) -> int:
    """How many companion addresses this lead's access had refused in the last :data:`EMAIL_REFUSAL_WINDOW_H` hours
    (the denied audit events, written even when the request fails)."""
    return int(await ctx.conn.fetchval(EMAIL_REFUSALS_SQL, str(access_id), EMAIL_REFUSAL_WINDOW_H) or 0)


async def set_companion_email(
    ctx: AnyContext,
    vault: Vault,
    members: list[Member],
    trip_id: uuid.UUID,
    lead_access_id: uuid.UUID,
    companion_id: uuid.UUID,
    raw_email: Any,
) -> None:
    """Give a companion without an address of their own one (the lead asks them at the portal invite).

    The address lands on the agency's client record, which every trip of that client shares, so the lead may set it
    only for a companion who is on **this trip alone** (409 ``ask_your_agent``: otherwise the lead would choose where
    the codes and links of the companion's other trips go). Never replaces an address on file (409
    ``email_on_file``). An address another client of the agency has is refused (409 ``email_in_use``, without saying
    whose); every refusal is audited as denied on the lead's access (the agency sees it in its access log) and after
    :data:`MAX_EMAIL_REFUSALS` refusals in :data:`EMAIL_REFUSAL_WINDOW_H` hours the lead is stopped (429
    ``too_many_attempts``), so the answer cannot be used to list the agency's clients."""
    target = next((m for m in members if m.traveler_id == companion_id), None)
    if target is None or target.role != "companion":
        raise NotFound("traveler_not_on_trip")
    email = clean_f1("email", raw_email)
    if not email:
        raise Invalid("invalid_email", "an e-mail address", fields=["email"])
    if await email_refusals(ctx, lead_access_id) >= MAX_EMAIL_REFUSALS:
        _refused(ctx, lead_access_id, companion_id, "attempt_cap")
        raise RateLimited("too_many_attempts", EMAIL_REFUSAL_WINDOW_H * 3600, "ask your travel agent to add it")
    blob = await ctx.conn.fetchrow(TRAVELER_EMAIL_SQL, companion_id)
    if blob is None:
        raise NotFound("traveler_not_on_trip")
    if blob["email_enc"] is not None:
        raise Conflict("email_on_file", "your agent already has an address for this traveller")
    if await ctx.conn.fetchval(OTHER_TRIP_SQL, companion_id, trip_id):
        _refused(ctx, lead_access_id, companion_id, "other_trip")
        raise Conflict("ask_your_agent", "ask your travel agent to add this traveller's address")
    index = await vault.email_index("traveler", "email", email)
    if await ctx.conn.fetchval(EMAIL_TAKEN_SQL, index, companion_id):
        _refused(ctx, lead_access_id, companion_id, "email_in_use")
        raise Conflict("email_in_use", "use another address, or leave it empty to reach them through yours")
    sealed = await vault.encrypt("traveler", "email_enc", companion_id, email)
    await ctx.conn.execute(SET_EMAIL_SQL, companion_id, sealed, index)
    ctx.audit(
        "portal_companion_email",
        "allowed",
        "traveler",
        str(companion_id),
        fields=("email",),
        object_ids=(str(lead_access_id),),
    )


def _refused(ctx: AnyContext, lead_access_id: uuid.UUID, companion_id: uuid.UUID, reason: str) -> None:
    ctx.audit(
        "portal_companion_email",
        "denied",
        "traveler_access",
        str(lead_access_id),
        fields=("email",),
        reason=reason,
        object_ids=(str(companion_id),),
    )


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None
