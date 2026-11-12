""" "Book now" on the public AA tour page (AA-1072, item crm-02): the agency's booking widget, the live price, and one
booking = traveller + trip + a one-option day-by-day proposal sent to the client, who then holds, accepts and pays
on the existing client page (``/c/p``).

The page is AA's catalogue page (``/trip/<tourId>``); the agency's link adds its **intake form key** (``?f=``), so the
public routes open :func:`app.tenancy.intake_tx` exactly like the inquiry form: an unknown, disabled or locked form is
404 ``form_not_found`` (keys cannot be probed), and the form's honeypot field, allowed page origins and default owner
apply. The agency must have turned "Book now" on (``tenant_settings.public_booking_enabled``, default off), else 404
``booking_not_offered``.

- :func:`widget`: the tour (the catalogue key from the link's ``p`` or the page id itself, a served tour of the cache),
  its fixed departures from Booking, the agency's name, currency and age bands, the honeypot field name. A tour
  without fixed departures, or one AA does not sell to this agency, is 404 ``tour_not_bookable`` (the page then shows
  "Ask the agency" only).
- :func:`price`: the live retail price of a departure for a party: a Booking quote (no hold), then the agency's
  retail on it (:func:`retail_total`, the builder's own suggestion: owner markup on the B2B price, else AA's RRP, with
  the agency's price rules), shared per head as the per-traveller grid. The B2B price never leaves the server.
  **Never below cost** (S277 live check): when that retail is under what AA charges the agency for the party (AA's
  RRP per person x 1 under the B2B price of a single traveller, who pays a supplement), the builder's owner discount
  gate would stop the version at publish (``below_cost``), and nobody approves on the public path. So the price check
  and the booking answer 409 ``price_on_request`` (the page says to ask the agency for a price) before anything is
  sold, and such a refused booking does not count towards the visitor's attempts (:data:`UNCOUNTED_REFUSALS`).
  **Per agency** (AA-1092): before anything goes to Booking a check takes one of the agency's price checks a minute
  (``tenant_settings.public_price_per_minute``, crm-066, the owner's setting on Settings > Payments; every address
  together, per API task): past it 429 ``agency_rate_limited`` with ``Retry-After`` (the per-address bucket of the
  router still applies first).
- :func:`submit`, in three steps so no connection is held while Booking answers (the AA-947 rule):

  1. ``intake_tx``: the page origin, the honeypot (202 as if stored, nothing written), consent, the answers, the
     per-address ceiling (:data:`MAX_PER_ADDRESS_HOUR` attempts an hour per agency, counted on the attempt rows'
     ``ip_hash``: an HMAC under the agency's index key, never the address), and one ``crm.public_booking_attempt``
     (``received``; a repeated press of the same page is the same attempt: ``already_created`` / ``in_progress``);
  2. no transaction: the departure from Booking's product read and a live quote for the party;
  3. ``intake_tx`` again: always a **new** traveller (the visitor's e-mail and phone are not verified, so a client
     with the same e-mail or phone is never reused: the trip gets an internal note and the agent merges the two on
     Duplicates once the client confirms; :func:`_traveller`), a trip
     at **Proposal** (``lead_source`` ``web_booking``, stage history reason ``web_booking``), a builder version with
     day 0, day 1 and one block holding the AA option at the retail price (per-traveller rows), published and sent by
     the builder's own ``publish`` / ``send`` (price hash, terms, cancellation policy, the agency's schedule
     template), then the attempt ``created``. The answer carries the client link path (``/c/p/<token>``, once): it
     opens a trip of the client this press made, never one of an existing client.

  A Booking refusal (sold out, not served, no price) marks the attempt ``refused`` with the code and answers 409 with
  it; any other failure of step 3 rolls it back and marks the attempt ``failed``.

Responses never carry an id, a name, a B2B price or whether the e-mail is known.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from typing import Any

import asyncpg

from app.booking import contract
from app.booking.client import BookingClient, BookingRefusal, BookingUnavailable
from app.booking.products import ProductCache
from app.errors import Conflict, Forbidden, Invalid, NotFound, RateLimited, ServiceError, Unavailable
from app.esign import terms as esign_terms
from app.intake import process, stage1
from app.intake.booking_settings import DEFAULT_PRICE_PER_AGENCY_MINUTE
from app.intake.public import origin_allowed, record_drop
from app.payments import schedule_plan
from app.proposals import aa_options, builder, pricing, retail, rules
from app.proposals import service as proposal_service
from app.settings import Settings
from app.tenancy import AnyContext, IntakeForm, SystemContext, intake_tx
from app.vault import KeyProvider, Vault
from app.vault.blind_index import NormalisationError
from app.workflows import triggers

log = logging.getLogger(__name__)

LEAD_SOURCE = "web_booking"
MAX_PER_ADDRESS_HOUR = 5
PRICE_ON_REQUEST = "price_on_request"
# Refusals that are the offer's doing, not the visitor's: they never use one of the address's attempts an hour.
UNCOUNTED_REFUSALS = (PRICE_ON_REQUEST,)
CENT = Decimal("0.01")
RETENTION_DAYS = 90
MAX_ADULTS = 20
MAX_CHILDREN = 10
DEPARTURE_DAYS = 365
TOUR_BLOCK_TITLE = "Tour"
# The catalogue country of the tour -> the trip's destination code (crm.trip destinations, app.intake.stage1).
COUNTRY_DESTINATION = {
    "VN": "vietnam",
    "LA": "laos",
    "KH": "cambodia",
    "TH": "thailand",
    "MM": "myanmar",
    "BT": "bhutan",
    "NP": "nepal",
    "ID": "indonesia",
}

SETTINGS_SQL = """
SELECT s.public_booking_enabled, s.base_currency, s.timezone, t.display_name, s.consent_text,
       s.privacy_notice_url AS agency_privacy_notice_url, s.public_price_per_minute
  FROM crm.tenant t LEFT JOIN crm.tenant_settings s ON s.tenant_id = t.tenant_id
 WHERE t.tenant_id = $1
"""
TOUR_SQL = """
SELECT source_id, title, country_code, duration_days, served_state FROM crm.aa_catalog_tour
 WHERE source_system = 'cis' AND source_id = ANY($1::text[]) AND served_state = 'served'
 ORDER BY array_position($1::text[], source_id)
 LIMIT 1
"""
ATTEMPT_SQL = """
SELECT attempt_id, status, refusal_code, proposal_id FROM crm.public_booking_attempt WHERE client_submission_id = $1
   FOR UPDATE
"""
INSERT_ATTEMPT_SQL = """
INSERT INTO crm.public_booking_attempt (attempt_id, intake_form_id, tour_source_id, departure_id, party, currency,
                                        client_submission_id, ip_hash, proposal_id)
VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, NULL)
"""
ADDRESS_COUNT_SQL = """
SELECT count(*) FROM crm.public_booking_attempt
 WHERE ip_hash = $1 AND created_at > now() - interval '1 hour'
   AND NOT (status = 'refused' AND refusal_code = ANY($2::text[]))
"""
REFUSE_SQL = """
UPDATE crm.public_booking_attempt SET status = $2, refusal_code = $3 WHERE attempt_id = $1 AND status = 'received'
"""
QUOTED_SQL = "UPDATE crm.public_booking_attempt SET quote_id = $2, quoted_total = $3 WHERE attempt_id = $1"
CREATED_SQL = """
UPDATE crm.public_booking_attempt SET status = 'created', trip_id = $2, proposal_id = $3 WHERE attempt_id = $1
"""
# A held trip's attempt stays (the hold guard would refuse its delete and fail the whole purge).
PURGE_SQL = """
DELETE FROM crm.public_booking_attempt
 WHERE created_at < now() - make_interval(days => $1) AND (trip_id IS NULL OR NOT crm.hold_row(trip_id, NULL))
"""
INSERT_TRIP_SQL = """
INSERT INTO crm.trip (trip_id, title, stage, waiting_on, start_date, end_date, nights, destinations, party_adults,
                      party_child_ages, party_type, owner_member_id, intake_form_id, lead_source)
VALUES ($1, $2, 'proposal', 'client', $3, $4, $5, $6, $7, $8, $9, $10, $11, 'web_booking')
"""
INSERT_HISTORY_SQL = """
INSERT INTO crm.trip_stage_history (trip_id, from_stage, to_stage, reason_code)
VALUES ($1, NULL, 'proposal', 'web_booking')
RETURNING history_id
"""
INSERT_CONSENT_SQL = """
INSERT INTO crm.consent (traveler_id, purpose, value, source, submission_id) VALUES ($1, $2, $3, 'form', NULL)
"""
# A live client of the agency that already has the booking's e-mail or phone (the Duplicates page's own match).
KNOWN_CONTACT_SQL = """
SELECT EXISTS (
  SELECT 1 FROM crm.traveler
   WHERE deleted_at IS NULL AND (email_bidx = $1 OR ($2::bytea IS NOT NULL AND phone_bidx = $2))
) AS known
"""
KNOWN_CONTACT_NOTE = (
    "Booked online with an e-mail or phone that an existing client already has. The booking was made as a new "
    "client because the details were not checked: confirm with the client, then merge the two on Clients > Duplicates."
)
INSERT_NOTE_SQL = "INSERT INTO crm.note (note_id, trip_id, traveler_id, kind, body_enc) VALUES ($1, $2, $3, $4, $5)"
EDIT_SEQ_SQL = "SELECT edit_seq FROM crm.proposal WHERE proposal_id = $1"
AGENCY_TZ_SQL = "SELECT timezone FROM crm.tenant_settings"


# ── answers ───────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Party:
    adults: int
    child_ages: tuple[int, ...]

    def as_json(self) -> str:
        return json.dumps({"adults": self.adults, "child_ages": list(self.child_ages)})

    @property
    def party_type(self) -> str:
        if self.child_ages:
            return "family"
        return {1: "solo", 2: "couple"}.get(self.adults, "friends" if self.adults <= 6 else "group")


@dataclass(frozen=True)
class Booking:
    tour: str
    departure_id: str
    party: Party
    name: str
    email: str
    phone: str | None
    message: str | None
    consent_marketing: bool
    client_submission_id: uuid.UUID


def _int(value: Any, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ValueError
    return value


def read_party(raw: Mapping[str, Any]) -> Party:
    """``adults`` 1..20 and ``child_ages`` (0..10 ages of 0..17): 422 ``invalid_party`` otherwise."""
    try:
        adults = _int(raw.get("adults"), 1, MAX_ADULTS)
        ages = raw.get("child_ages") or []
        if not isinstance(ages, list) or len(ages) > MAX_CHILDREN:
            raise ValueError
        return Party(adults, tuple(_int(a, 0, 17) for a in ages))
    except ValueError:
        raise Invalid("invalid_party", "1 to 20 adults and up to 10 children aged 0 to 17", fields=["party"]) from None


def _tour_key(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or any(ord(c) < 32 for c in value):
        raise Invalid("invalid_tour", "the AA tour key", fields=["tour"])
    return value


def _departure_id(value: Any) -> str:
    if not isinstance(value, str) or not contract._ID.match(value) or len(value) > 80:
        raise Invalid("invalid_departure", "pick a departure", fields=["departure_id"])
    return value


def read_booking(raw: Any) -> Booking:
    """The answers of one "Book now" press: 422 ``consent_required`` without the contact box, ``invalid_submission``
    with the field codes (never the values) otherwise."""
    if not isinstance(raw, dict):
        raise Invalid("invalid_submission", "a JSON object", fields=["body"])
    if raw.get("consent_contact") is not True:
        raise Invalid("consent_required", "tick the box to let the agency contact you")
    bad: list[str] = []
    party = read_party(raw)
    name = stage1.clean_text(raw.get("name"), stage1.MAX_NAME)
    if not name or not stage1.has_name_token(name):
        bad.append("name")
    email = ""
    try:
        email = stage1.normalise_email(stage1.clean_text(raw.get("email"), 254) or "")
    except NormalisationError:
        bad.append("email")
    phone = None
    try:
        text = stage1.clean_text(raw.get("phone"), 40)
        phone = stage1.normalise_phone(text) if text else None
    except NormalisationError:
        bad.append("phone")
    message = stage1.clean_text(raw.get("message"), stage1.MAX_MESSAGE)
    marketing = raw.get("consent_marketing")
    if marketing not in (None, True, False):
        bad.append("consent_marketing")
    try:
        submission = uuid.UUID(str(raw.get("client_submission_id")))
    except ValueError:
        bad.append("client_submission_id")
        submission = uuid.UUID(int=0)
    try:
        tour = _tour_key(raw.get("tour"))
        departure = _departure_id(raw.get("departure_id"))
    except Invalid as exc:
        fields = [*bad, *exc.extra.get("fields", [])]
        raise Invalid("invalid_submission", "some answers are not valid", fields=fields) from None
    if bad:
        raise Invalid("invalid_submission", "some answers are not valid", fields=bad)
    assert name is not None  # noqa: S101 — checked above
    return Booking(tour, departure, party, name, email, phone, message, marketing is True, submission)


# ── shared reads ──────────────────────────────────────────────────────────────────────────────────


async def _agency(ctx: AnyContext) -> Any:
    row = await ctx.conn.fetchrow(SETTINGS_SQL, ctx.tenant_id)
    if row is None or not row["public_booking_enabled"]:
        raise NotFound("booking_not_offered", "this agency takes bookings through its team: send an inquiry")
    return row


async def _tour(ctx: AnyContext, keys: list[str]) -> Any:
    row = await ctx.conn.fetchrow(TOUR_SQL, [k for k in keys if k])
    if row is None:
        raise NotFound("tour_not_bookable", "this tour cannot be booked online: ask the agency")
    return row


@dataclass(frozen=True)
class Offer:
    """What Booking sells this agency for one tour: the whole-tour component and the open fixed departures."""

    component_id: str
    departures: dict[str, contract.Departure]


def _booking_error(exc: Exception) -> ServiceError:
    if isinstance(exc, BookingRefusal) and exc.code in ("unknown_partner", "forbidden"):
        return NotFound("tour_not_bookable", "this tour cannot be booked online: ask the agency")
    return Unavailable("booking_unavailable", "bookings cannot be checked right now; try again in a minute")


async def offer(cache: ProductCache, client: BookingClient, tenant_id: uuid.UUID, source_id: str,
                today: dt.date) -> Offer:  # fmt: skip
    try:
        product = (await cache.products(tenant_id)).get(source_id)
        if product is None or not product.has_fixed_departures:
            raise NotFound("tour_not_bookable", "this tour cannot be booked online: ask the agency")
        component = next((c for c in product.components if c.kind == "trip_plan"), None)
        if component is None:
            raise NotFound("tour_not_bookable", "this tour cannot be booked online: ask the agency")
        data = await client.departures(tenant_id, source_id, date_from=(today + dt.timedelta(days=1)).isoformat(),
                                       date_to=(today + dt.timedelta(days=DEPARTURE_DAYS)).isoformat())  # fmt: skip
        found = {
            d.departure_id: d
            for d in contract.departures(data)
            if d.status != "cancelled" and d.available > 0 and d.start_date > today
        }
    except (BookingUnavailable, BookingRefusal, contract.ContractError) as exc:
        raise _booking_error(exc) from None
    return Offer(component.component_id, found)


def retail_total(quote: aa_options.AaQuote, *, kind: str, source_id: str, party: Party, rctx: dict[str, Any],
                 service_date: dt.date) -> Decimal | None:  # fmt: skip
    """The agency's sell of the whole tour for the party (the builder's own AA-1030 / AA-1066 suggestion), or None
    when the agency has no markup and AA no RRP for it."""
    option = {
        "source": "aa",
        "aa_component_kind": kind,
        "aa_tour_source_id": source_id,
        "net_amount": quote.b2b_price,
        "aa_from_price": quote.from_price,
    }
    heads = party.adults + len(party.child_ages)
    suggested = builder._suggestion(option, party=heads, rctx=rctx, service_date=service_date)
    return suggested[0] if suggested is not None and suggested[0] > 0 else None


def check_not_below_cost(sell: Decimal, quote: aa_options.AaQuote) -> None:
    """409 ``price_on_request`` when the party's retail ``sell`` (the grid total) is under the B2B price of the quote:
    the margin the builder's discount gate checks at publish (``builder._discount_gate``, ``below_cost``) would be
    negative, and on the public path there is no owner to approve a sale below cost. Typical case: no agency markup,
    so the retail is AA's RRP per person x 1, while the B2B price of one traveller carries a single supplement."""
    if quote.b2b_price is not None and sell < rules.money(quote.b2b_price):
        raise Conflict(PRICE_ON_REQUEST, "the agency prices this party on request: send them a question")


def price_grid(total: Decimal, party: Party, bands: pricing.Settings, currency: str) -> list[dict[str, Any]]:
    """The per-traveller rows of the option: adults and children share the total per head, rounded **up** to the
    currency's unit (whole dong for VND, so a gateway can charge the amounts exactly), infants at 0. The option's sell
    is their sum, so the page shows exactly what the proposal will charge."""
    counts = pricing.party_counts(None, party.adults, party.child_ages, bands)
    heads = counts["adult"] + counts["child"]
    unit = (total / heads).quantize(schedule_plan.quantum(currency), rounding=ROUND_CEILING).quantize(CENT)
    rows = []
    for pax in ("adult", "child", "infant"):
        qty = int(counts.get(pax, 0))
        if qty > 0:
            rows.append({"pax_type": pax, "unit_sell": unit if pax != "infant" else Decimal("0.00"), "unit_net": None,
                         "qty": qty})  # fmt: skip
    return rows


def grid_total(rows: list[dict[str, Any]]) -> Decimal:
    return sum((Decimal(r["unit_sell"]) * int(r["qty"]) for r in rows), Decimal("0.00"))


async def _rctx(ctx: AnyContext) -> dict[str, Any]:
    return {
        "markups": await retail.markups(ctx),
        "rules": await pricing.rules_of(ctx),
        "today": pricing.today_in(await ctx.conn.fetchval(AGENCY_TZ_SQL)),
    }


def _plan(*, proposal_id: uuid.UUID, currency: str, offer_: Offer, departure: contract.Departure, source_id: str,
          party: Party, title: str) -> dict[str, Any]:  # fmt: skip
    rooms = rules.default_rooms(party.adults, party.child_ages, party.party_type)
    return {
        "rooms": rooms,
        "option_id": str(uuid.uuid4()),
        "proposal_id": str(proposal_id),
        "version": 1,
        "currency": currency,
        "title": title,
        "tour_source_id": source_id,
        "component_id": offer_.component_id,
        "component_kind": "trip_plan",
        "departure_id": departure.departure_id,
        "nights": None,
        "service_date": departure.start_date.isoformat(),
        "pax": rules.room_pax(rooms, party.adults, party.child_ages),
        "party": party.adults + len(party.child_ages),
    }


# ── widget and price ──────────────────────────────────────────────────────────────────────────────


async def widget(
    pool: asyncpg.Pool,
    cache: ProductCache | None,
    client: BookingClient | None,
    public_key: str,
    *,
    tour: str | None,
    page_id: str | None,
) -> dict[str, Any]:
    keys = [k for k in (tour, page_id) if isinstance(k, str) and 1 <= len(k) <= 128]
    async with intake_tx(pool, public_key) as (ctx, form):
        agency = await _agency(ctx)
        row = await _tour(ctx, keys)
        bands = await pricing.settings(ctx)
        today = pricing.today_in(agency["timezone"])
        tenant_id = ctx.tenant_id
    if cache is None or client is None:
        raise Unavailable("booking_unavailable", "bookings cannot be checked right now; try again in a minute")
    found = await offer(cache, client, tenant_id, row["source_id"], today)
    return {
        "agency_name": agency["display_name"],
        "tour": row["source_id"],
        "title": row["title"],
        "duration_days": row["duration_days"],
        "currency": agency["base_currency"] or "USD",
        "child_age_max": bands.child_age_max,
        "infant_age_max": bands.infant_age_max,
        "honeypot_field": form.honeypot_field,
        "consent_text": agency["consent_text"],
        "privacy_notice_url": form.privacy_notice_url or agency["agency_privacy_notice_url"],
        "departures": [
            {"departure_id": d.departure_id, "start_date": d.start_date.isoformat(), "seats_left": min(d.available, 9)}
            for d in sorted(found.departures.values(), key=lambda d: (d.start_date, d.departure_id))
        ],
    }


async def _quote(client: BookingClient, tenant_id: uuid.UUID, plan: dict[str, Any]) -> aa_options.AaQuote:
    try:
        return await aa_options.quote(client, tenant_id, plan)
    except Unavailable:
        raise
    except (Conflict, Invalid) as exc:
        code = exc.code if exc.code in ("aa_sold_out", "aa_tour_not_served") else "aa_quote_refused"
        raise Conflict(code, "this departure cannot be booked online now: pick another or ask the agency") from None


async def price(
    pool: asyncpg.Pool,
    cache: ProductCache | None,
    client: BookingClient | None,
    public_key: str,
    raw: Any,
    *,
    agency_quota: Callable[[uuid.UUID, int], bool] | None = None,
    quota_window_s: int = 60,
) -> dict[str, Any]:
    """The live retail price of a departure for a party (a quote, no hold; nothing stored).

    ``agency_quota(tenant_id, ceiling)`` takes one of the agency's price checks a minute (False = none left)."""
    if not isinstance(raw, dict):
        raise Invalid("invalid_submission", "a JSON object", fields=["body"])
    party = read_party(raw)
    tour, departure_id = _tour_key(raw.get("tour")), _departure_id(raw.get("departure_id"))
    async with intake_tx(pool, public_key) as (ctx, _form):
        agency = await _agency(ctx)
        row = await _tour(ctx, [tour])
        bands = await pricing.settings(ctx)
        rctx = await _rctx(ctx)
        tenant_id = ctx.tenant_id
    ceiling = agency.get("public_price_per_minute") or DEFAULT_PRICE_PER_AGENCY_MINUTE
    if agency_quota is not None and not agency_quota(tenant_id, int(ceiling)):
        raise RateLimited("agency_rate_limited", quota_window_s)
    if cache is None or client is None:
        raise Unavailable("booking_unavailable", "bookings cannot be checked right now; try again in a minute")
    found = await offer(cache, client, tenant_id, row["source_id"], rctx["today"])
    departure = found.departures.get(departure_id)
    if departure is None:
        raise Conflict("departure_not_available", "this departure is no longer available: pick another")
    currency = agency["base_currency"] or "USD"
    plan = _plan(proposal_id=uuid.uuid4(), currency=currency, offer_=found, departure=departure,
                 source_id=row["source_id"], party=party, title=row["title"])  # fmt: skip
    quote = await _quote(client, tenant_id, plan)
    if quote.availability == "sold_out":
        raise Conflict("aa_sold_out", "this departure is full: pick another")
    total = retail_total(quote, kind="trip_plan", source_id=row["source_id"], party=party, rctx=rctx,
                         service_date=departure.start_date)  # fmt: skip
    if total is None:
        raise Conflict("no_retail_price", "the agency prices this tour on request: send an inquiry")
    rows = price_grid(total, party, bands, currency)
    check_not_below_cost(grid_total(rows), quote)
    return {
        "currency": currency,
        "total": str(grid_total(rows)),
        "lines": [{"pax_type": r["pax_type"], "qty": r["qty"], "unit": str(r["unit_sell"])} for r in rows],
        "availability": quote.availability,
        "start_date": departure.start_date.isoformat(),
    }


# ── submit ────────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Started:
    attempt_id: uuid.UUID
    tenant_id: uuid.UUID
    tour: Any
    currency: str
    today: dt.date


async def _start(
    ctx: SystemContext, form: IntakeForm, vault: Vault, answers: Booking, *, client_ip: str
) -> Started | dict[str, Any]:
    agency = await _agency(ctx)
    tour = await _tour(ctx, [answers.tour])
    await ctx.conn.execute(PURGE_SQL, RETENTION_DAYS)  # DM-7.1: the agency's attempts older than 90 days
    existing = await ctx.conn.fetchrow(ATTEMPT_SQL, answers.client_submission_id)
    if existing is not None:
        if existing["status"] == "created":
            return {"status": "already_created"}
        if existing["status"] == "received":
            raise Conflict("booking_in_progress", "your booking is being made: wait a moment")
        raise Conflict(existing["refusal_code"] or "booking_failed", "this booking could not be made")
    ip_hash = await vault.address_index("public_booking_attempt", "ip_hash", client_ip or "unknown")
    if int(await ctx.conn.fetchval(ADDRESS_COUNT_SQL, ip_hash, list(UNCOUNTED_REFUSALS))) >= MAX_PER_ADDRESS_HOUR:
        record_drop("booking_address", form.intake_form_id)
        raise RateLimited("booking_rate_limited", 3600)
    attempt_id = uuid.uuid4()
    currency = agency["base_currency"] or "USD"
    await ctx.conn.execute(INSERT_ATTEMPT_SQL, attempt_id, form.intake_form_id, tour["source_id"],
                           answers.departure_id, answers.party.as_json(), currency, answers.client_submission_id,
                           ip_hash)  # fmt: skip
    ctx.audit("public_booking_received", "allowed", "public_booking_attempt", str(attempt_id))
    return Started(attempt_id, ctx.tenant_id, dict(tour), currency, pricing.today_in(agency["timezone"]))


async def _finish_refused(pool: asyncpg.Pool, public_key: str, attempt_id: uuid.UUID, status: str, code: str) -> None:
    async with intake_tx(pool, public_key) as (ctx, _form):
        await ctx.conn.execute(REFUSE_SQL, attempt_id, status, code)
        ctx.audit("public_booking_" + status, "allowed", "public_booking_attempt", str(attempt_id), reason=code)


async def submit(
    pool: asyncpg.Pool,
    keys: KeyProvider,
    settings: Settings,
    cache: ProductCache | None,
    client: BookingClient | None,
    public_key: str,
    raw: Any,
    *,
    origin: str | None,
    client_ip: str,
    form_quota: Callable[[], bool] | None = None,
    quota_window_s: int = 3600,
) -> dict[str, Any]:
    if settings.intake_origin_check and not settings.intake_page_origins:
        raise Unavailable("intake_not_configured")
    async with intake_tx(pool, public_key) as (ctx, form):
        if not origin_allowed(origin, settings, form.allowed_origins):
            record_drop("origin", form.intake_form_id)
            raise Forbidden("origin_not_allowed")
        if stage1.honeypot_filled(raw, form.honeypot_field):
            record_drop("honeypot", form.intake_form_id)
            return {"status": "received"}  # the bot learns nothing
        answers = read_booking(raw)
        # AA-1092: the agency's hourly ceiling (the inquiry form's per-form quota, its own bucket), after the checks a
        # bot fails: each booking makes a client, a trip and a mailed client link from the agency's mailbox.
        if form_quota is not None and not form_quota():
            record_drop("form_quota", form.intake_form_id)
            raise RateLimited("form_rate_limited", quota_window_s)
        started = await _start(ctx, form, Vault(keys, ctx), answers, client_ip=client_ip)
        owner = form.default_owner_member_id
        form_id = form.intake_form_id
    if isinstance(started, dict):
        return started
    try:
        if cache is None or client is None:
            raise Unavailable("booking_unavailable", "bookings cannot be checked right now; try again in a minute")
        found = await offer(cache, client, started.tenant_id, started.tour["source_id"], started.today)
        departure = found.departures.get(answers.departure_id)
        if departure is None:
            raise Conflict("departure_not_available", "this departure is no longer available: pick another")
        proposal_id = uuid.uuid4()
        plan = _plan(proposal_id=proposal_id, currency=started.currency, offer_=found, departure=departure,
                     source_id=started.tour["source_id"], party=answers.party, title=started.tour["title"])  # fmt: skip
        quote = await _quote(client, started.tenant_id, plan)
        if quote.availability == "sold_out":
            raise Conflict("aa_sold_out", "this departure is full: pick another")
    except (Conflict, NotFound) as exc:
        await _finish_refused(pool, public_key, started.attempt_id, "refused", exc.code)
        raise
    except Unavailable:
        await _finish_refused(pool, public_key, started.attempt_id, "failed", "booking_unavailable")
        raise
    try:
        async with intake_tx(pool, public_key) as (ctx, _form):
            return await _create(
                ctx, Vault(keys, ctx), started, answers, plan=plan, quote=quote, departure=departure, owner=owner,
                form_id=form_id,
            )  # fmt: skip
    except Conflict as exc:
        await _finish_refused(pool, public_key, started.attempt_id, "refused", exc.code)
        raise
    except Exception as exc:
        code = exc.code if isinstance(exc, ServiceError) else "booking_failed"
        log.error("public_booking_failed attempt_id=%s error_class=%s", started.attempt_id, type(exc).__name__)
        await _finish_refused(pool, public_key, started.attempt_id, "failed", code)
        if isinstance(exc, ServiceError):
            raise
        raise Conflict("booking_failed", "this booking could not be made: send an inquiry instead") from None


async def _traveller(
    ctx: SystemContext, vault: Vault, answers: Booking, owner: uuid.UUID | None
) -> tuple[uuid.UUID, bool]:
    """The lead: **always a new client** of the agency, with its own F2 profile. The name, e-mail and phone are typed by
    an anonymous visitor and nobody checks them, while the answer hands that visitor a live client link: reusing a
    client whose e-mail or phone matches would let anyone who knows a client's e-mail act on that client's record (the
    terms acceptance, the hold and the deposit invoice pinned to their traveller id, their profile pre-filled). So a
    match is never reused or unarchived; the new client gets an internal note on the trip instead, and the agent merges
    the two on Duplicates once the client confirms (:mod:`app.travellers.merge`). The answer is the same either way, so
    it never says whether the e-mail is known. Returns the new client and whether another live client already has the
    e-mail or phone (blind indexes only)."""
    email_idx = await vault.email_index("traveler", "email", answers.email)
    phone_idx = await vault.phone_index("traveler", "phone", answers.phone) if answers.phone else None
    known = bool(await ctx.conn.fetchval(KNOWN_CONTACT_SQL, email_idx, phone_idx))
    traveler_id, token = uuid.uuid4(), uuid.uuid4()
    await ctx.conn.execute(process.INSERT_PROFILE_SQL, token, None, None, answers.consent_marketing)
    await ctx.conn.execute(
        process.INSERT_TRAVELER_SQL,
        traveler_id,
        await vault.encrypt("traveler", "display_name_enc", traveler_id, answers.name),
        await vault.encrypt("traveler", "email_enc", traveler_id, answers.email),
        email_idx,
        await vault.encrypt("traveler", "phone_enc", traveler_id, answers.phone) if answers.phone else None,
        phone_idx,
        await vault.name_index("traveler", "name", answers.name),
        await vault.name_token_indexes("traveler", "name", answers.name),
        "email",
        await vault.encrypt("traveler", "profile_token_enc", traveler_id, str(token)),
        owner,
        LEAD_SOURCE,
    )
    return traveler_id, known


async def _profile_token(ctx: SystemContext, vault: Vault, traveler_id: uuid.UUID) -> uuid.UUID | None:
    blob = await ctx.conn.fetchval("SELECT profile_token_enc FROM crm.traveler WHERE traveler_id = $1", traveler_id)
    return await process._profile_token(vault, traveler_id, blob)


async def _create(
    ctx: SystemContext,
    vault: Vault,
    started: Started,
    answers: Booking,
    *,
    plan: dict[str, Any],
    quote: aa_options.AaQuote,
    departure: contract.Departure,
    owner: uuid.UUID | None,
    form_id: uuid.UUID,
) -> dict[str, Any]:
    attempt = await ctx.conn.fetchrow(ATTEMPT_SQL, answers.client_submission_id)
    if attempt is None or attempt["status"] != "received":
        return {"status": "already_created"}
    bands = await pricing.settings(ctx)
    total = retail_total(quote, kind="trip_plan", source_id=plan["tour_source_id"], party=answers.party,
                         rctx=await _rctx(ctx), service_date=departure.start_date)  # fmt: skip
    if total is None:
        raise Conflict("no_retail_price", "the agency prices this tour on request: send an inquiry")
    rows = price_grid(total, answers.party, bands, plan["currency"])
    sell = grid_total(rows)
    check_not_below_cost(sell, quote)  # before anything is written; the refusal does not use an attempt
    await ctx.conn.execute(QUOTED_SQL, started.attempt_id, quote.quote_id, sell)

    traveler_id, known = await _traveller(ctx, vault, answers, owner)
    token = await _profile_token(ctx, vault, traveler_id)
    tour = started.tour
    days = int(tour["duration_days"] or 1)
    start = departure.start_date
    destination = COUNTRY_DESTINATION.get(str(tour["country_code"] or "").upper())
    trip_id = uuid.uuid4()
    await ctx.conn.execute(
        INSERT_TRIP_SQL, trip_id, str(tour["title"])[:120].strip() or "AA tour", start,
        start + dt.timedelta(days=days - 1), days - 1 if days > 1 else None, [destination] if destination else [],
        answers.party.adults, list(answers.party.child_ages), answers.party.party_type, owner, form_id,
    )  # fmt: skip
    history_id = await ctx.conn.fetchval(INSERT_HISTORY_SQL, trip_id)
    await ctx.conn.execute(process.INSERT_PARTY_SQL, trip_id, traveler_id, "lead")
    if token is not None:
        await ctx.conn.execute(process.INSERT_PARTY_F2_SQL, trip_id, token, "lead")
        await ctx.conn.execute(process.SET_MARKETING_SQL, token, answers.consent_marketing)
    await ctx.conn.execute(INSERT_CONSENT_SQL, traveler_id, "contact", True)
    await ctx.conn.execute(INSERT_CONSENT_SQL, traveler_id, "marketing", answers.consent_marketing)
    if answers.message:
        note_id = uuid.uuid4()
        await ctx.conn.execute(INSERT_NOTE_SQL, note_id, trip_id, traveler_id, "inquiry_message",
                               await vault.encrypt("note", "body_enc", note_id, answers.message))  # fmt: skip
    if known:
        note_id = uuid.uuid4()
        await ctx.conn.execute(INSERT_NOTE_SQL, note_id, trip_id, traveler_id, "internal",
                               await vault.encrypt("note", "body_enc", note_id, KNOWN_CONTACT_NOTE))  # fmt: skip
        ctx.audit("public_booking_known_contact", "allowed", "trip", str(trip_id), reason=LEAD_SOURCE)

    proposal_id = uuid.UUID(plan["proposal_id"])
    await _proposal(ctx, vault, trip_id, proposal_id, plan=plan, quote=quote, sell=sell, rows=rows, start=start,
                    destination=destination)  # fmt: skip
    seq = int(await ctx.conn.fetchval(EDIT_SEQ_SQL, proposal_id))
    await builder.publish(ctx, vault, proposal_id, seq, acknowledge_selection_seq=None)  # type: ignore[arg-type]
    sent = await proposal_service.send(ctx, vault, proposal_id)  # type: ignore[arg-type]
    await ctx.conn.execute(CREATED_SQL, started.attempt_id, trip_id, proposal_id)
    if history_id is not None:  # AA-959: workflows that start when a trip enters Proposal
        await triggers.on_stage_enter(ctx, trip_id, "proposal", history_id)
    ctx.audit("public_booking_created", "allowed", "trip", str(trip_id), reason=LEAD_SOURCE)
    return {"status": "created", "link_path": sent["link_path"], "currency": plan["currency"], "total": str(sell)}


async def _proposal(
    ctx: SystemContext,
    vault: Vault,
    trip_id: uuid.UUID,
    proposal_id: uuid.UUID,
    *,
    plan: dict[str, Any],
    quote: aa_options.AaQuote,
    sell: Decimal,
    rows: list[dict[str, Any]],
    start: dt.date,
    destination: str | None,
) -> None:
    """Version 1 of the trip: day 0 (Before you go), day 1 with one "Tour" block holding the AA option (its default),
    the per-traveller rows, the agency's schedule template dated from the departure."""
    party = plan["party"]
    tz = rules.default_tz([destination] if destination else [], await ctx.conn.fetchval(AGENCY_TZ_SQL))
    terms = await esign_terms.current(ctx)
    await ctx.conn.execute(
        builder.INSERT_PROPOSAL_SQL, proposal_id, trip_id, 1,
        await vault.encrypt("proposal", "body_enc", proposal_id, ""),
        plan["currency"], None, terms["doc_id"] if terms is not None else None, "total_with_deltas",
        json.dumps(plan["rooms"]), builder.DEFAULT_DEPOSIT_PCT, start, None, None,
    )  # fmt: skip
    await builder._write_plan(ctx, proposal_id, schedule_plan.from_template(await builder._template(ctx), start))
    day0, day1 = uuid.uuid4(), uuid.uuid4()
    await ctx.conn.execute(builder.INSERT_DAY_SQL, day0, proposal_id, trip_id, 0, None, None, tz, None, None)
    await ctx.conn.execute(builder.INSERT_DAY_SQL, day1, proposal_id, trip_id, 1, None, None, tz, None, None)
    block_id = uuid.uuid4()
    await ctx.conn.execute(builder.INSERT_BLOCK_SQL, block_id, proposal_id, trip_id, day1, 0, TOUR_BLOCK_TITLE,
                           "activity", False, None)  # fmt: skip
    below = None
    if quote.from_price is not None and party >= 1:
        below = sell / party < quote.from_price
    option_id = uuid.UUID(plan["option_id"])
    await ctx.conn.execute(
        builder.INSERT_OPTION_SQL, option_id, proposal_id, trip_id, block_id, 0, "aa", str(plan["title"])[:200],
        None, None, sell, rules.money(quote.b2b_price), False, False,
        builder.AVAILABILITY_FROM_QUOTE[quote.availability], dt.datetime.now(dt.UTC), plan["tour_source_id"],
        plan["component_id"], "trip_plan", plan["departure_id"], quote.quote_id, quote.valid_until,
        rules.money(quote.from_price) if quote.from_price is not None else None, below, None, None, None,
    )  # fmt: skip
    await ctx.conn.execute(builder.SET_DEFAULT_SQL, block_id, option_id, True)
    await pricing.replace(ctx, proposal_id, option_id, rows)
