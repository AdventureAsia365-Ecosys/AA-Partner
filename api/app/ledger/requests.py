"""Supplier booking requests grouped per supplier (AA-1069, crm-053; S275 study item crm-24).

- **Build** (:func:`build`, the trip's Bookings tab "Request from suppliers"): the trip's agency lines still planning
  (or requested already: asking again) are grouped by supplier; each supplier gets one e-mail **draft** in the trip's
  supplier request thread (``crm.conversation.agency_supplier_id``: created on the first request, one per trip and
  supplier), from the member's own mailbox else the agency's, exactly like a client message draft. The text lists the
  services with their dates, the travel dates, the travellers by type and the rooms; traveller names only when asked
  (the default asks for them for flight, insurance and visa suppliers, who book by name). A supplier without an e-mail
  gets no draft (``supplier_no_email``), the member adds it in Suppliers first. English, or Vietnamese for an agency
  whose default locale is Vietnamese; the member edits the draft before sending.
- **Send** (:func:`send`): the draft goes through the normal send path (:func:`app.mail.conversations.send`: 10 s
  Undo, the agency's mailbox, held while it is disconnected) and the lines it names become ``requested``
  (``requested_at``, ``request_ref`` = the message id).
- **The answer**: the supplier's reply threads on the same conversation by its headers (the normal forward-in). The
  member types the confirmation number on the line (``POST /lines/{id}/confirm`` with ``confirmation_no``), which runs
  ``crm.booking_line_confirm`` (a requested line is confirmed like a planning one). **Back to planning**
  (:func:`back_to_planning`) clears the request, e.g. after an Undo.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections import OrderedDict
from typing import Any

import asyncpg

from app import cancellation, stages
from app.errors import Conflict, Invalid, NotFound
from app.ledger import refusal
from app.mail import conversations
from app.tenancy import TenantContext
from app.vault import Vault

REQUESTABLE = ("planning", "requested")
NAME_CATEGORIES = ("flight", "insurance", "visa")
MAX_LINES = 100

TRIP_SQL = """
SELECT t.trip_id, t.title, t.start_date, t.end_date, t.nights, t.party_adults, t.party_child_ages,
       te.display_name AS agency_name, te.default_locale
  FROM crm.trip t CROSS JOIN crm.tenant te
 WHERE t.trip_id = $1 AND t.deleted_at IS NULL
"""
LINES_SQL = """
SELECT l.booking_line_id, l.agency_supplier_id, l.title, l.service_from, l.service_to, l.status,
       s.name AS supplier_name, s.category, s.contact_email_enc IS NOT NULL AS has_email
  FROM crm.booking_line l JOIN crm.agency_supplier s ON s.agency_supplier_id = l.agency_supplier_id
 WHERE l.trip_id = $1 AND l.supplier_kind = 'agency' AND l.deleted_at IS NULL AND s.deleted_at IS NULL
 ORDER BY s.name, l.service_from NULLS LAST, l.created_at, l.booking_line_id
"""
ROOMS_SQL = """
SELECT room_config FROM crm.proposal
 WHERE trip_id = $1 AND deleted_at IS NULL AND room_config IS NOT NULL
 ORDER BY (status = 'accepted') DESC, version DESC
 LIMIT 1
"""
NAMES_SQL = """
SELECT t.traveler_id, t.display_name_enc FROM crm.trip_traveler tt JOIN crm.traveler t ON t.traveler_id = tt.traveler_id
 WHERE tt.trip_id = $1 AND t.deleted_at IS NULL
 ORDER BY tt.role = 'lead' DESC, t.created_at, t.traveler_id
"""
THREAD_SQL = """
SELECT conversation_id FROM crm.conversation
 WHERE trip_id = $1 AND agency_supplier_id = $2 AND channel = 'email' AND deleted_at IS NULL
"""
NEW_THREAD_SQL = """
INSERT INTO crm.conversation (trip_id, traveler_id, channel, agency_supplier_id)
VALUES ($1, NULL, 'email', $2)
ON CONFLICT (tenant_id, trip_id, agency_supplier_id) WHERE agency_supplier_id IS NOT NULL AND deleted_at IS NULL
DO NOTHING
RETURNING conversation_id
"""
OLD_DRAFTS_SQL = """
DELETE FROM crm.message WHERE conversation_id = $1 AND author_member_id = $2 AND status = 'draft_autosaved'
"""
MESSAGE_SQL = """
SELECT m.message_id, m.status, m.author_member_id, c.conversation_id, c.trip_id, c.agency_supplier_id
  FROM crm.message m JOIN crm.conversation c ON c.conversation_id = m.conversation_id
 WHERE m.message_id = $1 AND c.deleted_at IS NULL
"""
MARK_SQL = """
UPDATE crm.booking_line SET status = 'requested', requested_at = now(), request_ref = $4
 WHERE booking_line_id = ANY($1::uuid[]) AND trip_id = $2 AND agency_supplier_id = $3 AND supplier_kind = 'agency'
   AND status = ANY($5::text[]) AND deleted_at IS NULL
RETURNING booking_line_id
"""
BACK_SQL = """
UPDATE crm.booking_line SET status = 'planning', requested_at = NULL, request_ref = NULL
 WHERE booking_line_id = $1 AND supplier_kind = 'agency' AND status = 'requested' AND deleted_at IS NULL
RETURNING trip_id
"""
LINE_SQL = """
SELECT trip_id, status, supplier_kind FROM crm.booking_line WHERE booking_line_id = $1 AND deleted_at IS NULL
"""

TEXT = {
    "en": {
        "subject": "Booking request: {trip}",
        "greeting": "Dear {supplier} team,",
        "intro": "Please book the services below for our clients and reply to this e-mail with your confirmation "
        "number for each one.",
        "dates": "Travel dates: {start} to {end}",
        "dates_open": "Travel dates: to be confirmed",
        "party": "Travellers: {party}",
        "rooms": "Rooms: {rooms}",
        "services": "Services:",
        "names": "Traveller names (as in their passports):",
        "thanks": "Thank you,",
        "adult": ("{n} adult", "{n} adults"),
        "child": ("{n} child", "{n} children"),
        "ages": " (ages {ages})",
        "room": {"single": "single", "double": "double", "twin": "twin", "triple": "triple", "family": "family"},
        "to": " to ",
    },
    "vi": {
        "subject": "Yêu cầu đặt dịch vụ: {trip}",
        "greeting": "Kính gửi {supplier},",
        "intro": "Nhờ quý đối tác đặt giúp các dịch vụ dưới đây cho khách của chúng tôi và trả lời e-mail này kèm mã "
        "xác nhận của từng dịch vụ.",
        "dates": "Ngày đi: {start} đến {end}",
        "dates_open": "Ngày đi: sẽ xác nhận sau",
        "party": "Số khách: {party}",
        "rooms": "Phòng: {rooms}",
        "services": "Dịch vụ:",
        "names": "Tên khách (theo hộ chiếu):",
        "thanks": "Trân trọng cảm ơn,",
        "adult": ("{n} người lớn", "{n} người lớn"),
        "child": ("{n} trẻ em", "{n} trẻ em"),
        "ages": " ({ages} tuổi)",
        "room": {"single": "đơn", "double": "đôi", "twin": "hai giường", "triple": "ba người", "family": "gia đình"},
        "to": " đến ",
    },
}


def _lang(locale: str | None) -> str:
    return "vi" if (locale or "").lower().startswith("vi") else "en"


def _day(value: dt.date | None) -> str:
    return value.strftime("%d %b %Y") if value else ""


def _uuid_list(value: Any) -> list[uuid.UUID] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_LINES:
        raise Invalid("invalid_line_ids", f"1 to {MAX_LINES} line ids")
    try:
        return [v if isinstance(v, uuid.UUID) else uuid.UUID(str(v)) for v in value]
    except ValueError:
        raise Invalid("invalid_line_ids", "line ids") from None


def party_text(lang: str, adults: int, child_ages: list[int]) -> str:
    t = TEXT[lang]
    parts = [t["adult"][adults != 1].format(n=adults)] if adults else []
    if child_ages:
        ages = ", ".join(str(a) for a in sorted(child_ages))
        parts.append(t["child"][len(child_ages) != 1].format(n=len(child_ages)) + t["ages"].format(ages=ages))
    return ", ".join(parts) or "-"


def rooms_text(lang: str, rooms: list[dict[str, Any]] | None) -> str | None:
    if not rooms:
        return None
    counts: OrderedDict[str, int] = OrderedDict()
    for room in rooms:
        kind = str(room.get("type", ""))
        counts[kind] = counts.get(kind, 0) + 1
    names = TEXT[lang]["room"]
    return ", ".join(f"{n} {names.get(k, k)}" for k, n in counts.items())


def compose(
    lang: str,
    *,
    agency: str,
    supplier: str,
    trip: dict[str, Any],
    lines: list[dict[str, Any]],
    rooms: list[dict[str, Any]] | None,
    names: list[str] | None,
) -> tuple[str, str]:
    """The subject and text of one supplier's request (pure: no database, no names unless given)."""
    t = TEXT[lang]
    trip_label = trip["title"] + (f" ({_day(trip['start_date'])})" if trip.get("start_date") else "")
    body = [t["greeting"].format(supplier=supplier), "", t["intro"], ""]
    if trip.get("start_date") and trip.get("end_date"):
        body.append(t["dates"].format(start=_day(trip["start_date"]), end=_day(trip["end_date"])))
    else:
        body.append(t["dates_open"])
    body.append(t["party"].format(party=party_text(lang, int(trip.get("party_adults") or 0),
                                                   list(trip.get("party_child_ages") or []))))  # fmt: skip
    room_line = rooms_text(lang, rooms)
    if room_line:
        body.append(t["rooms"].format(rooms=room_line))
    body += ["", t["services"]]
    for i, line in enumerate(lines, 1):
        when = _day(line.get("service_from"))
        if line.get("service_to") and line["service_to"] != line.get("service_from"):
            when += t["to"] + _day(line["service_to"])
        body.append(f"{i}. {line['title']}" + (f" - {when}" if when else ""))
    if names:
        body += ["", t["names"], *[f"- {n}" for n in names]]
    body += ["", t["thanks"], agency]
    return t["subject"].format(trip=trip_label), "\n".join(body)


async def _thread(ctx: TenantContext, trip_id: uuid.UUID, supplier_id: uuid.UUID) -> uuid.UUID:
    found = await ctx.conn.fetchval(THREAD_SQL, trip_id, supplier_id)
    if found is not None:
        return found
    created = await ctx.conn.fetchval(NEW_THREAD_SQL, trip_id, supplier_id)
    return created if created is not None else await ctx.conn.fetchval(THREAD_SQL, trip_id, supplier_id)


async def _names(ctx: TenantContext, vault: Vault, trip_id: uuid.UUID) -> list[str]:
    """The display names of the trip's travellers (lead first), what every agency view already shows."""
    out, ids = [], []
    for r in await ctx.conn.fetch(NAMES_SQL, trip_id):
        name = await vault.decrypt("traveler", "display_name_enc", r["traveler_id"], r["display_name_enc"])
        if name and name.strip():
            out.append(name.strip())
            ids.append(str(r["traveler_id"]))
    if ids:
        ctx.audit("view", "allowed", "traveler", None, fields=("display_name",), reason="supplier_request",
                  object_ids=tuple(ids))  # fmt: skip
    return out


async def build(
    ctx: TenantContext, vault: Vault, trip_id: uuid.UUID, *, line_ids: Any = None, include_names: bool | None = None
) -> dict[str, Any]:
    """One e-mail draft per supplier of the trip's requestable agency lines (all of them, or ``line_ids``)."""
    trip = await ctx.conn.fetchrow(TRIP_SQL, trip_id)
    if trip is None:
        raise NotFound("trip_not_found")
    await cancellation.ensure_not_cancelled(ctx, trip_id, "no supplier is asked to book")
    wanted = _uuid_list(line_ids)
    rows = [dict(r) for r in await ctx.conn.fetch(LINES_SQL, trip_id)]
    skipped: list[dict[str, str]] = []
    if wanted is not None:
        known = {r["booking_line_id"]: r for r in rows}
        for line in wanted:
            if line not in known:
                skipped.append({"booking_line_id": str(line), "reason": "not_an_agency_line"})
            elif known[line]["status"] not in REQUESTABLE:
                skipped.append({"booking_line_id": str(line), "reason": f"line_{known[line]['status']}"})
        rows = [r for r in rows if r["booking_line_id"] in set(wanted)]
    rows = [r for r in rows if r["status"] in REQUESTABLE]
    if not rows:
        raise Conflict("nothing_to_request", "no planning line of an own supplier on this trip", skipped=skipped)
    by_supplier: OrderedDict[uuid.UUID, list[dict[str, Any]]] = OrderedDict()
    for r in rows:
        by_supplier.setdefault(r["agency_supplier_id"], []).append(r)
    rooms_raw = await ctx.conn.fetchval(ROOMS_SQL, trip_id)
    rooms = json.loads(rooms_raw) if isinstance(rooms_raw, str) else rooms_raw
    lang = _lang(trip["default_locale"])
    names_cache: list[str] | None = None
    requests: list[dict[str, Any]] = []
    for supplier_id, lines in by_supplier.items():
        first = lines[0]
        wants_names = include_names if include_names is not None else first["category"] in NAME_CATEGORIES
        entry: dict[str, Any] = {
            "agency_supplier_id": str(supplier_id),
            "supplier_name": first["supplier_name"],
            "category": first["category"],
            "line_ids": [str(r["booking_line_id"]) for r in lines],
            "include_names": bool(wants_names),
            "has_email": bool(first["has_email"]),
        }
        if not first["has_email"]:
            entry["reason"] = "supplier_no_email"
            requests.append(entry)
            continue
        if wants_names and names_cache is None:
            names_cache = await _names(ctx, vault, trip_id)
        subject, body = compose(
            lang, agency=trip["agency_name"] or "", supplier=first["supplier_name"], trip=dict(trip), lines=lines,
            rooms=rooms if isinstance(rooms, list) else None, names=names_cache if wants_names else None,
        )  # fmt: skip
        thread = await _thread(ctx, trip_id, supplier_id)
        await ctx.conn.execute(OLD_DRAFTS_SQL, thread, ctx.member_id)  # one request draft per member and supplier
        draft = await conversations.save_draft(
            ctx, vault, uuid.uuid4(), conversation_id=thread, trip_id=None, subject=subject, body=body
        )
        entry.update({"conversation_id": draft["conversation_id"], "message_id": draft["message_id"],
                      "subject": subject, "body": body})  # fmt: skip
        requests.append(entry)
    drafted = tuple(r["message_id"] for r in requests if "message_id" in r)
    if drafted:
        ctx.audit("create", "allowed", "message", None, reason="supplier_request", object_ids=drafted)
    return {"trip_id": str(trip_id), "locale": lang, "requests": requests, "skipped": skipped}


async def send(
    ctx: TenantContext,
    vault: Vault,
    trip_id: uuid.UUID,
    *,
    message_id: uuid.UUID,
    subject: str | None,
    body: str | None,
    line_ids: Any,
) -> dict[str, Any]:
    """Send one supplier's request draft (the normal send path) and mark the lines it names ``requested``."""
    lines = _uuid_list(line_ids)
    assert lines is not None  # noqa: S101 — the route requires it
    row = await ctx.conn.fetchrow(MESSAGE_SQL, message_id)
    if row is None or row["trip_id"] != trip_id or row["agency_supplier_id"] is None:
        raise NotFound("draft_not_found")
    await cancellation.ensure_not_cancelled(ctx, trip_id, "no supplier is asked to book")
    sent = await conversations.send(
        ctx, vault, message_id, conversation_id=row["conversation_id"], trip_id=None, subject=subject, body=body
    )
    try:
        async with ctx.conn.transaction():
            marked = await ctx.conn.fetch(
                MARK_SQL, lines, trip_id, row["agency_supplier_id"], str(message_id), list(REQUESTABLE)
            )
    except asyncpg.PostgresError as exc:
        mapped = refusal(exc)
        raise (mapped if mapped is not None else exc) from None
    done = {r["booking_line_id"] for r in marked}
    if not done:
        raise Conflict("nothing_to_request", "none of these lines can be requested from this supplier")
    ctx.audit("update", "allowed", "booking_line", None, fields=("status",), reason="supplier_request",
              object_ids=tuple(str(x) for x in done))  # fmt: skip
    await stages.refresh_waiting_on(ctx, trip_id)
    return {**sent, "requested": [str(x) for x in lines if x in done],
            "not_requested": [str(x) for x in lines if x not in done]}  # fmt: skip


async def back_to_planning(ctx: TenantContext, line_id: uuid.UUID) -> uuid.UUID:
    """A requested agency line back to planning (the request is cleared). Returns the trip id."""
    current = await ctx.conn.fetchrow(LINE_SQL, line_id)
    if current is None:
        raise NotFound("line_not_found")
    if current["supplier_kind"] == "aa" or current["status"] != "requested":
        raise Conflict("line_not_editable", "only a requested line goes back to planning")
    try:
        async with ctx.conn.transaction():
            trip_id = await ctx.conn.fetchval(BACK_SQL, line_id)
    except asyncpg.PostgresError as exc:
        mapped = refusal(exc)
        raise (mapped if mapped is not None else exc) from None
    ctx.audit("update", "allowed", "booking_line", str(line_id), fields=("status",), reason="back_to_planning")
    await stages.refresh_waiting_on(ctx, trip_id)
    return trip_id
