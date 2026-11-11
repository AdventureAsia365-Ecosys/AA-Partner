"""The day-list builder, agency side (card CRM-21a, AA-949; design v3 M3, F3, WF-4 Fix E1; migration crm-019).
Routes in :mod:`app.routers.builder`; the pure rules in :mod:`app.proposals.rules`; the live AA quote in
:mod:`app.proposals.aa_options`.

A builder version is a ``crm.proposal`` row with ``kind = builder``:

- **Create** (``PROPOSAL_DRAFT``, every role): a skeleton from Stage 1/2 (spec rule 5): one day per trip date (or
  night + 1), the "Before you go" day 0 (R38 slot), a Lodging block on every night, the day zone from the destination
  (R33), the rooms from the party (Fix B10), 30 % deposit. Or a **working copy** of an earlier version (``from``): every
  day, block, option and rule copied with its lineage, and the pick counter of that version noted
  (``based_on_selection_seq``). One builder draft per trip at a time (409 ``builder_draft_exists``).
- **Edit** (``PROPOSAL_DRAFT``; money fields need ``MONEY_FIELDS_EDIT``, checked by the router): settings (price
  visibility D1, rooms, deposit share, day 1 date, balance date, terms, intro text), days (add, edit, delete,
  reorder), blocks (title, type, client may swap, default), options (agency items; AA items through the live quote),
  rules (requires / excludes, the Advanced section). **Every write names the ``edit_seq`` it read**; another value is
  409 ``version_conflict`` (two editors or two tabs, §11.2) and nothing is written. A rule set the defaults break is
  422 ``option_conflict`` (card R).
- **Publish vN** (``PROPOSAL_PUBLISH``, owner / agent): checks (a default per block, none sold out, the rules hold,
  something to charge, the balance date only when a balance is left, AA quotes still valid, every AA item priced),
  then — when the working copy came from a version the client picked on since it was copied — 409
  ``version_conflict`` with those picks until the agent acknowledges them (card E, §11.2). Then the price hash is
  fixed, the amounts computed, the published v(N-1) superseded and the client's picks carried over to the copied
  options (``carried``). Sending a published version is CRM-21b (AA-950). **Terms** (design v3 F6 step 3, WF-7, R34;
  S261 live finding): a version without booking terms is pinned to the agency's published booking terms version at
  publish (a new skeleton starts with it too), so the client accepts the terms with the proposal and no re-accept
  waits before the deposit (:func:`app.proposals.service.pin_current_terms`). A version that names terms keeps them;
  when that version is retired (the agency published a newer one), the draft is **not publishable** (blocking check
  ``terms_retired``, ``terms_not_published`` for a draft or removed version) until the agent picks the current terms
  (S262 round 3, AA-957): the agent chose the version the client accepts, so publish never swaps it silently. A
  working copy of a version whose terms are retired starts with the current published terms, like a new skeleton.
  Settings take only a published terms version (409 ``terms_not_published``); keeping the version already named is
  allowed.
- **Picks** (:func:`record_pick`, the seam CRM-21b's client page calls): one pick per block of a published / sent
  version; a swap the block does not allow, a sold-out option or a rule break is refused; every pick moves
  ``selection_seq``.

Every function takes the open context and never opens one. Net, cost and margin keys are built only for a role that
may see them (design §4.6; the serializer strips them again for the assistant).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from decimal import Decimal
from typing import Any

from app import cancellation, stages
from app.cancel_policy import service as cancel_policy
from app.catalog import store as catalog
from app.catalogue import service as catalogue
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.esign import terms as esign_terms
from app.intake import completeness, stage2
from app.margin import sees_margin
from app.payments import schedule_plan
from app.proposals import discount, prep_packs, pricing, retail, rules
from app.proposals.service import (
    ACCEPTED_EXISTS_SQL,
    DRAFTABLE_STAGES,
    MAX_BODY,
    MAX_VERSIONS,
    NEXT_VERSION_SQL,
    pin_current_terms,
)
from app.tenancy import AnyContext, TenantContext
from app.vault import Vault

KIND = "builder"
DEFAULT_DEPOSIT_PCT = Decimal("30")
MAX_DAYS = 60
MAX_BLOCKS_PER_DAY = 12
MAX_OPTIONS_PER_BLOCK = 12
MAX_RULES = 100
MAX_NOTES = 4000
MAX_SKELETON_DAYS = 30
OPEN_FOR_PICKS = ("published", "sent")
AVAILABILITY_FROM_QUOTE = {"available": "instant", "on_request": "on_request", "sold_out": "sold_out"}

TRIP_SQL = """
SELECT trip_id, stage, start_date, end_date, nights, destinations, party_adults, party_child_ages, party_type
  FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL FOR UPDATE
"""
TRIP_PARTY_SQL = """
SELECT trip_id, stage, destinations, party_adults, party_child_ages, party_type
  FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL
"""
DRAFT_EXISTS_SQL = """
SELECT proposal_id FROM crm.proposal WHERE trip_id = $1 AND kind = 'builder' AND status = 'draft' AND deleted_at IS NULL
"""
AGENCY_TZ_SQL = "SELECT timezone FROM crm.tenant_settings LIMIT 1"
PROPOSAL_SQL = """
SELECT p.proposal_id, p.trip_id, p.version, p.status, p.kind, p.body_enc, p.currency, p.total_amount,
       p.deposit_amount, p.balance_due_on, p.terms_doc_id, p.price_hash, p.price_visibility, p.room_config,
       p.deposit_pct, p.starts_on, p.edit_seq, p.selection_seq, p.based_on_proposal_id, p.based_on_selection_seq,
       p.published_at, p.sent_at, p.created_at, p.updated_at, p.cancellation_policy_id, t.stage AS trip_stage,
       t.party_adults,
       t.party_child_ages, t.party_type, t.destinations, d.status AS terms_status
  FROM crm.proposal p JOIN crm.trip t ON t.trip_id = p.trip_id AND t.deleted_at IS NULL
  LEFT JOIN crm.agreement_doc d ON d.doc_id = p.terms_doc_id AND d.kind = 'booking_terms' AND d.deleted_at IS NULL
 WHERE p.proposal_id = $1 AND p.deleted_at IS NULL
"""
LOCK_SQL = PROPOSAL_SQL + "   FOR UPDATE OF p"
INSERT_PROPOSAL_SQL = """
INSERT INTO crm.proposal (proposal_id, trip_id, version, body_enc, currency, total_amount, deposit_amount,
                          balance_due_on, terms_doc_id, kind, price_visibility, room_config, deposit_pct, starts_on,
                          based_on_proposal_id, based_on_selection_seq)
VALUES ($1, $2, $3, $4, $5, 0, 0, $6, $7, 'builder', $8, $9::jsonb, $10, $11, $12, $13)
"""
DAYS_SQL = """
SELECT proposal_day_id, day_no, title, place, tz, notes_enc, source_day_id FROM crm.proposal_day
 WHERE proposal_id = $1 ORDER BY day_no
"""
BLOCKS_SQL = """
SELECT option_block_id, proposal_day_id, sort_order, title, type, client_may_swap, default_option_id, default_pinned,
       source_block_id
  FROM crm.option_block WHERE proposal_id = $1 ORDER BY sort_order, created_at
"""
OPTIONS_SQL = """
SELECT o.option_id, o.option_block_id, o.sort_order, o.source, o.title, o.tier, o.agency_supplier_id, o.sell_amount,
       o.net_amount, o.pay_on_arrival, o.choose_later_allowed, o.availability, o.availability_checked_at,
       o.aa_tour_source_id, o.aa_component_id, o.aa_component_kind, o.aa_departure_id, o.aa_nights, o.aa_quote_id,
       o.aa_quote_valid_until, o.aa_from_price, o.below_from_price, o.source_option_id, o.price_rules,
       c.served_state AS aa_served_state
  FROM crm.option o
  LEFT JOIN crm.aa_catalog_tour c ON c.source_system = 'cis' AND c.source_id = o.aa_tour_source_id
 WHERE o.proposal_id = $1 ORDER BY o.sort_order, o.created_at
"""
RULES_SQL = """
SELECT option_rule_id, kind, option_id, target_option_id FROM crm.option_rule WHERE proposal_id = $1
 ORDER BY created_at
"""
PICKS_SQL = """
SELECT s.option_block_id, s.option_id, s.chosen_by, s.choose_later, s.updated_at FROM crm.selection s
 WHERE s.proposal_id = $1
"""
PICKS_SINCE_SQL = """
SELECT b.title AS block_title, o.title AS option_title, s.chosen_by, s.updated_at FROM crm.selection s
  JOIN crm.option_block b ON b.option_block_id = s.option_block_id
  JOIN crm.option o ON o.option_id = s.option_id
 WHERE s.proposal_id = $1 AND s.chosen_by IN ('client', 'agent')
 ORDER BY s.updated_at DESC
 LIMIT 50
"""
BASE_SQL = "SELECT proposal_id, version, status, selection_seq FROM crm.proposal WHERE proposal_id = $1"
BUMP_SQL = """
UPDATE crm.proposal SET edit_seq = edit_seq + 1, total_amount = $2, deposit_amount = $3 WHERE proposal_id = $1
RETURNING edit_seq
"""
SETTINGS_SQL = """
UPDATE crm.proposal SET price_visibility = $2, room_config = $3::jsonb, deposit_pct = $4, starts_on = $5,
       balance_due_on = $6, terms_doc_id = $7, body_enc = $8, discount_pct = $9, schedule_plan = $10::jsonb,
       cancellation_policy_id = $11
 WHERE proposal_id = $1
"""
# AA-1067: the version's payment lines (NULL = the deposit / balance pair) and currency, read with the structure.
PLAN_SQL = "SELECT p.schedule_plan, p.currency FROM crm.proposal p WHERE p.proposal_id = $1 AND p.kind = 'builder'"
SET_PLAN_SQL = """
UPDATE crm.proposal SET schedule_plan = $2::jsonb, deposit_pct = $3, balance_due_on = $4 WHERE proposal_id = $1
"""
TEMPLATE_SQL = "SELECT schedule_template FROM crm.tenant_settings"
COPY_POLICY_SQL = "UPDATE crm.proposal SET cancellation_policy_id = $2 WHERE proposal_id = $1"
TERMS_SQL = "SELECT doc_id, status, body_sha256 FROM crm.agreement_doc WHERE doc_id = $1 AND kind = 'booking_terms'"
SUPPLIER_SQL = """
SELECT agency_supplier_id, kind FROM crm.agency_supplier WHERE agency_supplier_id = $1 AND deleted_at IS NULL
"""
INSERT_DAY_SQL = """
INSERT INTO crm.proposal_day (proposal_day_id, proposal_id, trip_id, day_no, title, place, tz, notes_enc, source_day_id)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
"""
SHIFT_DAYS_SQL = """
UPDATE crm.proposal_day SET day_no = day_no + $3 WHERE proposal_id = $1 AND day_no >= $2
"""
UPDATE_DAY_SQL = """
UPDATE crm.proposal_day SET title = $2, place = $3, tz = $4, notes_enc = $5 WHERE proposal_day_id = $1
"""
RENUMBER_SQL = """
UPDATE crm.proposal_day d SET day_no = x.n
  FROM unnest($2::uuid[]) WITH ORDINALITY AS x(id, n)
 WHERE d.proposal_id = $1 AND d.proposal_day_id = x.id AND d.day_no <> x.n
"""
INSERT_BLOCK_SQL = """
INSERT INTO crm.option_block (option_block_id, proposal_id, trip_id, proposal_day_id, sort_order, title, type,
                              client_may_swap, source_block_id)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
"""
UPDATE_BLOCK_SQL = """
UPDATE crm.option_block SET title = $2, type = $3, client_may_swap = $4, default_option_id = $5, sort_order = $6,
       default_pinned = $7
 WHERE option_block_id = $1
"""
SET_DEFAULT_SQL = "UPDATE crm.option_block SET default_option_id = $2, default_pinned = $3 WHERE option_block_id = $1"
INSERT_OPTION_SQL = """
INSERT INTO crm.option (option_id, proposal_id, trip_id, option_block_id, sort_order, source, title, tier,
                        agency_supplier_id, sell_amount, net_amount, pay_on_arrival, choose_later_allowed,
                        availability, availability_checked_at, aa_tour_source_id, aa_component_id, aa_component_kind,
                        aa_departure_id, aa_quote_id, aa_quote_valid_until, aa_from_price, below_from_price,
                        source_option_id, aa_nights, price_rules)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22, $23, $24,
        $25, $26::jsonb)
"""
UPDATE_OPTION_SQL = """
UPDATE crm.option SET title = $2, tier = $3, agency_supplier_id = $4, sell_amount = $5, net_amount = $6,
       pay_on_arrival = $7, choose_later_allowed = $8, below_from_price = $9, price_rules = $10::jsonb
 WHERE option_id = $1
"""
# AA-1066: an option priced from its per-traveller rows (sell = their sum) or from the suggestion with the rules used.
OPTION_PRICE_SQL = """
UPDATE crm.option SET sell_amount = $2, net_amount = $3, below_from_price = $4, price_rules = $5::jsonb
 WHERE option_id = $1
"""
# AA-1066: the version's discount (read with the structure, so every total, the client page included, follows it).
DISCOUNT_SQL = "SELECT p.discount_pct FROM crm.proposal p WHERE p.proposal_id = $1 AND p.kind = 'builder'"
QUOTE_UPDATE_SQL = """
UPDATE crm.option SET net_amount = $2, availability = $3, availability_checked_at = now(), aa_quote_id = $4,
       aa_quote_valid_until = $5, aa_from_price = $6, below_from_price = $7
 WHERE option_id = $1
"""
SOLD_OUT_SQL = """
UPDATE crm.option SET availability = 'sold_out', availability_checked_at = now() WHERE option_id = $1
"""
INSERT_RULE_SQL = """
INSERT INTO crm.option_rule (option_rule_id, proposal_id, trip_id, kind, option_id, target_option_id)
VALUES ($1, $2, $3, $4, $5, $6)
"""
SUPERSEDE_PUBLISHED_SQL = """
UPDATE crm.proposal SET status = 'superseded'
 WHERE trip_id = $1 AND status = 'published' AND proposal_id <> $2 AND deleted_at IS NULL
RETURNING proposal_id
"""
PUBLISH_SQL = """
UPDATE crm.proposal SET status = 'published', published_at = now(), price_hash = $2, total_amount = $3,
       deposit_amount = $4, edit_seq = edit_seq + 1
 WHERE proposal_id = $1
RETURNING published_at
"""
INSERT_PICK_SQL = """
INSERT INTO crm.selection (proposal_id, trip_id, option_block_id, option_id, chosen_by, choose_later)
VALUES ($1, $2, $3, $4, $5, $6)
ON CONFLICT (tenant_id, proposal_id, option_block_id)
DO UPDATE SET option_id = EXCLUDED.option_id, chosen_by = EXCLUDED.chosen_by, choose_later = EXCLUDED.choose_later
"""
PICK_SEQ_SQL = """
UPDATE crm.proposal SET selection_seq = selection_seq + 1 WHERE proposal_id = $1 RETURNING selection_seq
"""


# ── Small helpers ────────────────────────────────────────────────────────────────────────────────


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _amount(value: Any) -> str | None:
    return str(rules.money(value)) if value is not None else None


def _jsonb(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _text(value: Any, name: str, limit: int, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise Invalid(f"{name}_required")
        return None
    if not isinstance(value, str):
        raise Invalid(f"invalid_{name}")
    clean = " ".join(value.split())
    if not clean:
        if required:
            raise Invalid(f"{name}_required")
        return None
    if len(clean) > limit:
        raise Invalid(f"{name}_too_long", f"at most {limit} characters")
    return clean


def _notes(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise Invalid("invalid_notes")
    clean = value.replace("\r\n", "\n").strip()
    if len(clean) > MAX_NOTES:
        raise Invalid("notes_too_long", f"at most {MAX_NOTES} characters")
    return clean or None


def _tz(value: Any) -> str:
    if not rules.valid_tz(value):
        raise Invalid("invalid_tz", "an IANA time zone such as Asia/Kathmandu")
    return str(value)


def _money_in(value: Any, name: str) -> Decimal | None:
    if value is None:
        return None
    amount = Decimal(str(value))
    if amount < 0 or amount > Decimal("999999999999.99") or amount != amount.quantize(rules.CENT):
        raise Invalid(f"invalid_{name}", "zero or more, two decimals at most")
    return amount


def _day_date(starts_on: dt.date | None, day_no: int) -> dt.date | None:
    if starts_on is None or day_no < 1:
        return None
    return starts_on + dt.timedelta(days=day_no - 1)


async def _lock(ctx: TenantContext, proposal_id: uuid.UUID, seq: int | None) -> Any:
    """The builder draft, locked; ``seq`` must be its current ``edit_seq`` (None only for internal callers)."""
    row = await ctx.conn.fetchrow(LOCK_SQL, proposal_id)
    if row is None:
        raise NotFound("proposal_not_found")
    if row["kind"] != KIND:
        raise Conflict("proposal_not_builder", "this version is a text proposal (Proposal tab)")
    if row["status"] != "draft":
        raise Conflict("proposal_not_draft", "a published version never changes; start a new version from it")
    # S262 round 3 (C12 §2.7): a cancelled trip's builder is read-only (no AA quote, no new option, nothing to send).
    await cancellation.ensure_not_cancelled(ctx, row["trip_id"], "its proposal no longer changes")
    if seq is not None and seq != row["edit_seq"]:
        raise Conflict(
            "version_conflict",
            "this proposal changed since you opened it; reload to see the latest working copy",
            reason="edited",
            current_seq=row["edit_seq"],
        )
    return row


async def _structure(ctx: TenantContext, proposal_id: uuid.UUID) -> dict[str, Any]:
    days = [dict(r) for r in await ctx.conn.fetch(DAYS_SQL, proposal_id)]
    blocks = [dict(r) for r in await ctx.conn.fetch(BLOCKS_SQL, proposal_id)]
    options = [dict(r) for r in await ctx.conn.fetch(OPTIONS_SQL, proposal_id)]
    rule_rows = [dict(r) for r in await ctx.conn.fetch(RULES_SQL, proposal_id)]
    # AA-1066: the per-traveller rows of every option and the version's discount.
    prices = await pricing.prices_of(ctx, proposal_id)
    out = {"days": days, "blocks": blocks, "options": options, "rules": rule_rows, "prices": prices,
           "discount_pct": await _discount_of(ctx, proposal_id)}  # fmt: skip
    out.update(await _plan_of(ctx, proposal_id))
    return out


async def _plan_of(ctx: AnyContext, proposal_id: uuid.UUID) -> dict[str, Any]:
    """AA-1067: ``schedule_plan`` (the parsed lines, None = the deposit / balance pair) and ``currency``."""
    row = await ctx.conn.fetchrow(PLAN_SQL, proposal_id)
    if row is None or not isinstance(row.get("currency"), str):
        return {"schedule_plan": None, "currency": None}
    return {"schedule_plan": schedule_plan.from_json(row.get("schedule_plan")), "currency": row["currency"].strip()}


async def _discount_of(ctx: AnyContext, proposal_id: uuid.UUID) -> Decimal | None:
    raw = await ctx.conn.fetchval(DISCOUNT_SQL, proposal_id)
    if isinstance(raw, bool) or not isinstance(raw, (Decimal, int, str)):
        return None
    return Decimal(raw) or None


def _rule_objs(rule_rows: list[dict[str, Any]]) -> list[rules.Rule]:
    return [
        rules.Rule(str(r["option_rule_id"]), r["kind"], str(r["option_id"]), str(r["target_option_id"]))
        for r in rule_rows
    ]


def _defaults(s: dict[str, Any]) -> dict[str, str | None]:
    return {
        str(b["option_block_id"]): str(b["default_option_id"]) if b["default_option_id"] else None for b in s["blocks"]
    }


def _option_map(s: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(o["option_id"]): o for o in s["options"]}


def _charged(s: dict[str, Any], picks: dict[str, str] | None, deposit_pct: Any) -> dict[str, Any]:
    choice = rules.chosen(_defaults(s), picks)
    sums = rules.totals(choice, _option_map(s))
    pct = s.get("discount_pct")
    if pct:
        # AA-1066: the discount comes off the charged total (pay-on-arrival items are never discounted); the margin
        # is the discounted total less the net.
        subtotal = sums["sell_total"]
        off = rules.money(subtotal * Decimal(pct) / 100)
        sums["subtotal"], sums["discount"], sums["sell_total"] = subtotal, off, subtotal - off
        if sums["net_total"] is not None:
            sums["margin_total"] = rules.money(sums["sell_total"] - sums["net_total"])
    plan = s.get("schedule_plan")
    if plan and s.get("currency"):
        # AA-1067: the version's payment lines, each its share of the charged total, rounded per currency; the
        # deposit is the first, the last takes the rest.
        sums["schedule"] = schedule_plan.split(sums["sell_total"], plan, s["currency"])
        sums["deposit"] = sums["schedule"][0]
    else:
        sums["deposit"] = rules.deposit_of(sums["sell_total"], Decimal(deposit_pct or DEFAULT_DEPOSIT_PCT))
    return sums


def line_sells(s: dict[str, Any], choice: dict[str, str]) -> dict[str, Decimal]:
    """AA-1066: each chosen option's sell after the version's discount, what its ledger line records. The discount is
    spread over the charged options (pay-on-arrival items keep their sell), each ``sell × (1 − pct/100)`` to the cent,
    and the rounding remainder goes to the largest charged option, so the charged sells add up to exactly
    :func:`_charged`'s ``sell_total`` (what the client accepted and pays)."""
    options = _option_map(s)
    out = {oid: rules.money(options[oid]["sell_amount"]) for oid in set(choice.values()) if oid in options}
    pct = s.get("discount_pct")
    charged = sorted((oid for oid in out if not options[oid].get("pay_on_arrival")), key=lambda o: (out[o], o))
    if not pct or not charged:
        return out
    rate = Decimal(pct) / 100
    left = rules.money(sum((out[o] for o in charged), Decimal("0")) * rate)
    for oid in charged[:-1]:
        off = rules.money(out[oid] * rate)
        out[oid] -= off
        left -= off
    out[charged[-1]] -= left
    return out


async def _bump(ctx: TenantContext, row: Any) -> int:
    """After a write: the next edit_seq and the draft's amounts from its defaults."""
    s = await _structure(ctx, row["proposal_id"])
    sums = _charged(s, None, row["deposit_pct"])
    return int(await ctx.conn.fetchval(BUMP_SQL, row["proposal_id"], sums["sell_total"], sums["deposit"]))


def _conflicts(s: dict[str, Any], picks: dict[str, str] | None = None) -> list[dict[str, str]]:
    return rules.violations(rules.chosen(_defaults(s), picks), _rule_objs(s["rules"]))


def _raise_conflicts(found: list[dict[str, str]], s: dict[str, Any]) -> None:
    if not found:
        return
    titles = {str(o["option_id"]): o["title"] for o in s["options"]}
    raise Invalid(
        "option_conflict",
        "the chosen options break a rule: "
        + "; ".join(
            f"{titles.get(v['option_id'], '?')} {v['kind']} {titles.get(v['target_option_id'], '?')}" for v in found
        ),
        violations=found,
    )


# ── The view ─────────────────────────────────────────────────────────────────────────────────────


async def view(ctx: TenantContext, vault: Vault, proposal_id: uuid.UUID, *, audit: bool = True) -> dict[str, Any]:
    """The builder page (A-04): settings, days with their date and UTC offset, blocks, options, rules, picks,
    totals, checks. Net, cost and margin only for owner / agent."""
    row = await ctx.conn.fetchrow(PROPOSAL_SQL, proposal_id)
    if row is None:
        raise NotFound("proposal_not_found")
    if row["kind"] != KIND:
        raise Conflict("proposal_not_builder", "this version is a text proposal (Proposal tab)")
    margin = sees_margin(ctx.member_role)
    s = await _structure(ctx, proposal_id)
    picks_rows = [dict(r) for r in await ctx.conn.fetch(PICKS_SQL, proposal_id)]
    picks = {str(p["option_block_id"]): str(p["option_id"]) for p in picks_rows}
    defaults = _defaults(s)
    by_block: dict[str, list[dict[str, Any]]] = {}
    now = dt.datetime.now(dt.UTC)
    # AA-1030: the suggested sell of each AA option (the agency markup, else AA's RRP for a tour); AA-1066: then the
    # agency's price rules, and a per-traveller grid from the agency's age bands.
    rctx = await _retail_ctx(ctx, s)
    party = _party(row)
    bands = await pricing.settings(ctx)
    counts = pricing.party_counts(_jsonb(row["room_config"]), int(row["party_adults"]), row["party_child_ages"] or [],
                                  bands)  # fmt: skip
    dates = _option_dates(row, s)
    for o in s["options"]:
        default_id = defaults.get(str(o["option_block_id"]))
        default = next((x for x in s["options"] if str(x["option_id"]) == default_id), None)
        item: dict[str, Any] = {
            "option_id": str(o["option_id"]),
            "source": o["source"],
            "title": o["title"],
            "tier": o["tier"],
            "agency_supplier_id": str(o["agency_supplier_id"]) if o["agency_supplier_id"] else None,
            "sell_amount": _amount(o["sell_amount"]),
            "sell_delta": _amount(Decimal(o["sell_amount"]) - Decimal(default["sell_amount"])) if default else None,
            "pay_on_arrival": o["pay_on_arrival"],
            "choose_later_allowed": o["choose_later_allowed"],
            "availability": o["availability"],
            "availability_checked_at": _iso(o["availability_checked_at"]),
            "is_default": str(o["option_id"]) == default_id,
            "picked": picks.get(str(o["option_block_id"])) == str(o["option_id"]),
            # AA-1066: the per-traveller rows (unit net for owner / agent only) and the rules the sell came from.
            "prices": pricing.rows_out(s["prices"].get(str(o["option_id"]), []), sees_margin=margin),
            "price_rules": pricing.parse_applied(o.get("price_rules")),
        }
        suggestion = _suggestion(o, party=party, rctx=rctx, service_date=dates.get(str(o["option_id"])))
        shown = suggestion is not None and (suggestion[1] != retail.BASIS_MARKUP or margin)
        grid_total = suggestion[0] if shown and suggestion is not None else Decimal(o["sell_amount"])
        item["suggested_prices"] = pricing.rows_out(pricing.grid(grid_total, counts), sees_margin=False)
        if o["source"] == "aa":
            item["aa"] = {
                "tour_source_id": o["aa_tour_source_id"],
                "component_id": o["aa_component_id"],
                "component_kind": o["aa_component_kind"],
                "departure_id": o["aa_departure_id"],
                "nights": o["aa_nights"],
                "unpriced": Decimal(o["sell_amount"]) == 0,
                "quote_valid_until": _iso(o["aa_quote_valid_until"]),
                "quote_expired": _quote_expired(o, now),
                "from_price": _amount(o["aa_from_price"]),
                "below_from_price": o["below_from_price"],
                "no_longer_offered": o["aa_served_state"] == "retired",
                **retail.suggestion_out(suggestion[:2] if suggestion else None, sees_margin=margin),
                "suggested_rules": suggestion[2] if shown and suggestion else [],
            }
        if margin:
            item["net_amount"] = _amount(o["net_amount"])
            if default is not None and o["net_amount"] is not None and default["net_amount"] is not None:
                item["net_delta"] = _amount(Decimal(o["net_amount"]) - Decimal(default["net_amount"]))
            else:
                item["net_delta"] = None
        by_block.setdefault(str(o["option_block_id"]), []).append(item)
    blocks_by_day: dict[str, list[dict[str, Any]]] = {}
    for b in s["blocks"]:
        blocks_by_day.setdefault(str(b["proposal_day_id"]), []).append({
            "option_block_id": str(b["option_block_id"]),
            "title": b["title"],
            "type": b["type"],
            "sort_order": b["sort_order"],
            "client_may_swap": b["client_may_swap"],
            "default_option_id": str(b["default_option_id"]) if b["default_option_id"] else None,
            "default_pinned": b["default_pinned"],
            "options": by_block.get(str(b["option_block_id"]), []),
        })  # fmt: skip
    days = []
    for d in s["days"]:
        date = _day_date(row["starts_on"], d["day_no"])
        notes = None
        if d["notes_enc"] is not None:
            notes = await vault.decrypt("proposal_day", "notes_enc", d["proposal_day_id"], d["notes_enc"])
        days.append({
            "proposal_day_id": str(d["proposal_day_id"]),
            "day_no": d["day_no"],
            "before_you_go": d["day_no"] == 0,
            "date": _iso(date),
            "title": d["title"],
            "place": d["place"],
            "tz": d["tz"],
            "utc_offset": rules.utc_offset(d["tz"], date or row["starts_on"]),
            "notes": notes,
            "blocks": blocks_by_day.get(str(d["proposal_day_id"]), []),
        })  # fmt: skip
    sums = _charged(s, picks if row["status"] in OPEN_FOR_PICKS else None, row["deposit_pct"])
    totals: dict[str, Any] = {
        "currency": row["currency"],
        "sell_total": _amount(sums["sell_total"]),
        "pay_on_arrival_total": _amount(sums["pay_on_arrival_total"]),
        "deposit": _amount(sums["deposit"]),
        "balance": _amount(sums["sell_total"] - sums["deposit"]),
    }
    if s.get("schedule_plan") and s.get("currency"):  # AA-1067: every payment line with its amount
        totals["schedule"] = schedule_plan.view(s["schedule_plan"], sums["sell_total"], s["currency"])
    if "discount" in sums:  # AA-1066: only with a discount, so a version without one reads as before
        totals.update(subtotal=_amount(sums["subtotal"]), discount_pct=str(s["discount_pct"]),
                      discount=_amount(sums["discount"]))  # fmt: skip
    if margin:
        totals["net_total"] = _amount(sums["net_total"])
        totals["margin_total"] = _amount(sums["margin_total"])
    base = None
    if row["based_on_proposal_id"] is not None:
        b = await ctx.conn.fetchrow(BASE_SQL, row["based_on_proposal_id"])
        if b is not None:
            since = int(b["selection_seq"]) - int(row["based_on_selection_seq"] or 0)
            base = {
                "proposal_id": str(b["proposal_id"]),
                "version": b["version"],
                "status": b["status"],
                "client_picks_since_copy": max(0, since),
                "selection_seq": b["selection_seq"],
            }
    body = await vault.decrypt("proposal", "body_enc", proposal_id, row["body_enc"])
    out = {
        "proposal_id": str(proposal_id),
        "trip_id": str(row["trip_id"]),
        "trip_stage": row["trip_stage"],
        "version": row["version"],
        "status": row["status"],
        "kind": row["kind"],
        "edit_seq": row["edit_seq"],
        "selection_seq": row["selection_seq"],
        "currency": row["currency"],
        "price_visibility": row["price_visibility"],
        "room_config": _jsonb(row["room_config"]) or [],
        "deposit_pct": str(row["deposit_pct"]) if row["deposit_pct"] is not None else None,
        "starts_on": _iso(row["starts_on"]),
        "balance_due_on": _iso(row["balance_due_on"]),
        # AA-1067: the payment lines the agent set (None = the deposit / balance pair) and the cancellation policy.
        "schedule": schedule_plan.to_json(s["schedule_plan"]) if s.get("schedule_plan") else None,
        "cancellation_policy": await cancel_policy.summary(ctx, row.get("cancellation_policy_id")),
        "terms_doc_id": str(row["terms_doc_id"]) if row["terms_doc_id"] else None,
        # S262 round 3: the named version's state (published / retired / draft; None without terms or once removed).
        "terms_status": row["terms_status"],
        "intro": body,
        "price_hash": bytes(row["price_hash"]).hex() if row["price_hash"] else None,
        "published_at": _iso(row["published_at"]),
        "based_on": base,
        "days": days,
        "rules": [
            {
                "option_rule_id": str(r["option_rule_id"]),
                "kind": r["kind"],
                "option_id": str(r["option_id"]),
                "target_option_id": str(r["target_option_id"]),
            }
            for r in s["rules"]
        ],  # fmt: skip
        "picks": [
            {
                "option_block_id": str(p["option_block_id"]),
                "option_id": str(p["option_id"]),
                "chosen_by": p["chosen_by"],
                "choose_later": p["choose_later"],
                "at": _iso(p["updated_at"]),
            }
            for p in picks_rows
        ],  # fmt: skip
        "totals": totals,
        "checks": _checks(row, s, now),
        "stage2": await _stage2_basis(ctx, vault, row),
        # AA-951 (CRM-22, R38): the day 0 packs, the suggestions from the AA items, the reminders.
        "before_you_go": await prep_packs.view(ctx, proposal_id, row["trip_id"]),
    }
    if row["status"] == "draft" and out["before_you_go"]["packs_not_chosen"]:
        out["checks"]["warnings"] = sorted({*out["checks"]["warnings"], "prep_packs_not_chosen"})
    # AA-1066: the agency's bands, the party by retail type, the discount and its approval.
    draft_sums = _charged(s, None, row["deposit_pct"])
    state = await discount.state(ctx, trip_id=row["trip_id"], proposal_id=proposal_id, discount_pct=s["discount_pct"],
                                 margin_total=draft_sums["margin_total"], bands=bands, sees_margin=margin)  # fmt: skip
    out["pricing"] = {
        "bands": {"child_age_max": bands.child_age_max, "infant_age_max": bands.infant_age_max},
        "party": counts,
        "discount": state,
    }
    if row["status"] == "draft":
        extra_warnings = {"prices_party_changed"} if _prices_party_changed(s, counts) else set()
        if state["needs_approval"]:
            if ctx.member_role == "owner":
                extra_warnings.add("discount_owner_approves")  # the owner's publish records their own approval
            else:
                out["checks"]["blocking"] = sorted({*out["checks"]["blocking"], "discount_needs_approval"})
                out["checks"]["publishable"] = False
        out["checks"]["warnings"] = sorted({*out["checks"]["warnings"], *extra_warnings})
    if audit:
        ctx.audit("view", "allowed", "proposal", str(proposal_id), fields=("builder",))
    return out


def _quote_expired(o: Any, now: dt.datetime) -> bool:
    """An AA option whose quote ran out and must be refreshed before Publish. A sold-out option is left out: refresh
    marks it sold out without a new quote, so its old quote stays expired for good; it can never be the default (a
    sold-out default is ``default_sold_out``) and a client cannot pick it, so its price is never charged."""
    return (
        o["source"] == "aa"
        and o["availability"] != "sold_out"
        and o["aa_quote_valid_until"] is not None
        and o["aa_quote_valid_until"] < now
    )


async def _retail_ctx(ctx: AnyContext, s: dict[str, Any]) -> dict[str, Any]:
    """What an AA option's suggestion needs (AA-1030 markups, AA-1066 rules and the agency's today); read only when
    the version has an AA option."""
    if not any(o["source"] == "aa" for o in s["options"]):
        return {"markups": {}, "rules": [], "today": dt.datetime.now(dt.UTC).date()}
    return {
        "markups": await retail.markups(ctx),
        "rules": await pricing.rules_of(ctx),
        "today": pricing.today_in(await ctx.conn.fetchval(AGENCY_TZ_SQL)),
    }


def _suggestion(
    o: Any, *, party: int, rctx: dict[str, Any], service_date: dt.date | None
) -> tuple[Decimal, str, list[dict[str, str]]] | None:
    """``(sell, basis, rules used)`` of an AA option: the AA-1030 suggestion (agency markup, else AA's RRP), then the
    agency's price rules (AA-1066); None for an agency item or when AA-1030 has nothing to suggest."""
    if o["source"] != "aa":
        return None
    base = retail.suggest(
        o["aa_component_kind"],
        b2b_price=Decimal(o["net_amount"]) if o["net_amount"] is not None else None,
        from_price=Decimal(o["aa_from_price"]) if o["aa_from_price"] is not None else None,
        party=party,
        agency_markups=rctx["markups"],
    )
    if base is None:
        return None
    pct, used = pricing.adjustment(rctx["rules"], kind=o["aa_component_kind"], tour_source_id=o["aa_tour_source_id"],
                                   service_date=service_date, pax=party, today=rctx["today"])  # fmt: skip
    return pricing.adjusted(base[0], pct), base[1], used


def _option_dates(row: Any, s: dict[str, Any]) -> dict[str, dt.date | None]:
    """option id -> the date of its day (None without a day 1 date or on day 0)."""
    day_no = {d["proposal_day_id"]: d["day_no"] for d in s["days"]}
    block_day = {b["option_block_id"]: day_no.get(b["proposal_day_id"], 0) for b in s["blocks"]}
    return {str(o["option_id"]): _day_date(row["starts_on"], block_day.get(o["option_block_id"], 0))
            for o in s["options"]}  # fmt: skip


def _prices_party_changed(s: dict[str, Any], counts: dict[str, int]) -> bool:
    """An option's per-traveller rows count other travellers than the party now has (the rooms or the party changed
    since): a warning, the agent decides."""
    people = counts["adult"] + counts["child"] + counts["infant"]
    for rows in s["prices"].values():
        counted = sum(int(r["qty"]) for r in rows if r["pax_type"] != "single_supp")
        if counted != people:
            return True
    return False


def _checks(row: Any, s: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    """What blocks Publish, and the warnings that never block (the "from" price, ADR 0005a decision 1)."""
    blocking: list[str] = []
    warnings: list[str] = []
    opts = _option_map(s)
    if not s["options"]:
        blocking.append("no_options")
    for b in s["blocks"]:
        has_options = any(str(o["option_block_id"]) == str(b["option_block_id"]) for o in s["options"])
        if has_options and b["default_option_id"] is None:
            blocking.append("block_without_default")
        default = opts.get(str(b["default_option_id"])) if b["default_option_id"] else None
        if default is not None and default["availability"] == "sold_out":
            blocking.append("default_sold_out")
    if _conflicts(s):
        blocking.append("option_conflict")
    if any(_quote_expired(o, now) for o in s["options"]):
        blocking.append("aa_prices_stale")
    if any(o["source"] == "aa" and Decimal(o["sell_amount"]) == 0 for o in s["options"]):
        # An AA item added without a sell (the assistant never sets one, §4.6): an owner / agent prices it first.
        blocking.append("aa_item_unpriced")
    if any(o["below_from_price"] for o in s["options"]):
        warnings.append("below_from_price")
    if any(o["aa_served_state"] == "retired" for o in s["options"]):
        warnings.append("aa_no_longer_offered")
    if any(o["net_amount"] is None for o in s["options"]):
        warnings.append("net_missing")
    if row["status"] == "draft" and row["terms_doc_id"] is not None and row["terms_status"] != "published":
        # S262 round 3 (AA-957 x AA-952): Publish refuses terms that are not the published version; say so up front.
        # Only a draft: a published / sent / accepted version keeps the terms it went out with (R34), never "blocked".
        blocking.append("terms_retired" if row["terms_status"] == "retired" else "terms_not_published")
    sums = _charged(s, None, row["deposit_pct"])
    if sums["sell_total"] <= 0:
        blocking.append("nothing_to_charge")
    elif s.get("schedule_plan") and s.get("currency"):
        # AA-1067: every line after the deposit has a date, the dates go forward and stay before day 1, and the
        # percents leave money for every line.
        blocking += schedule_plan.problems(s["schedule_plan"], sums["sell_total"], s["currency"], row["starts_on"])
    elif sums["deposit"] < sums["sell_total"] and row["balance_due_on"] is None:
        blocking.append("balance_due_on_required")
    elif sums["deposit"] >= sums["sell_total"] and row["balance_due_on"] is not None:
        # The deposit is the whole total (proposal_amounts_chk: no balance date then).
        blocking.append("balance_due_on_not_needed")
    return {"publishable": not blocking, "blocking": sorted(set(blocking)), "warnings": sorted(set(warnings))}


async def _stage2_basis(ctx: TenantContext, vault: Vault, row: Any) -> dict[str, Any]:
    """The Stage 2 answers the defaults follow (rule 5): bands and codes only (F2), never a name."""
    lead, _token = await completeness.lead_token(ctx, vault, row["trip_id"])
    answers = await stage2.prefill(ctx, vault, lead)
    return {
        "accommodation_tier": answers.get("accommodation_tier"),
        "group_pref": answers.get("group_pref"),
        "pace": answers.get("pace"),
        "party_adults": row["party_adults"],
        "party_children": len(row["party_child_ages"] or []),
    }


# ── Create ───────────────────────────────────────────────────────────────────────────────────────


async def create(
    ctx: TenantContext,
    vault: Vault,
    trip_id: uuid.UUID,
    *,
    currency: str | None,
    from_proposal_id: uuid.UUID | None,
) -> dict[str, Any]:
    trip = await ctx.conn.fetchrow(TRIP_SQL, trip_id)
    if trip is None:
        raise NotFound("trip_not_found")
    if trip["stage"] not in DRAFTABLE_STAGES:
        raise Conflict("trip_not_open_for_proposal", "a proposal is built before the trip is booked")
    if await ctx.conn.fetchval(ACCEPTED_EXISTS_SQL, trip_id):
        raise Conflict("proposal_already_accepted", "the client accepted a version already")
    existing = await ctx.conn.fetchval(DRAFT_EXISTS_SQL, trip_id)
    if existing is not None:
        raise Conflict("builder_draft_exists", "this trip has a working copy already", proposal_id=str(existing))
    version = int(await ctx.conn.fetchval(NEXT_VERSION_SQL, trip_id))
    if version > MAX_VERSIONS:
        raise Conflict("too_many_versions")
    proposal_id = uuid.uuid4()
    if from_proposal_id is not None:
        await _copy(ctx, vault, trip, proposal_id, version, from_proposal_id)
        reason = "builder_copy"
    else:
        await _skeleton(ctx, vault, trip, proposal_id, version, currency)
        reason = "builder"
    row = await ctx.conn.fetchrow(LOCK_SQL, proposal_id)
    await _bump(ctx, row)
    await stages.touch(ctx, trip_id)
    ctx.audit("proposal_draft", "allowed", "proposal", str(proposal_id), reason=reason)
    return await view(ctx, vault, proposal_id, audit=False)


async def _skeleton(
    ctx: TenantContext, vault: Vault, trip: Any, proposal_id: uuid.UUID, version: int, currency: str | None
) -> None:
    """Spec rule 5 / Fix B10: days, zones and rooms from Stage 1/2; Lodging blocks on every night."""
    if currency is None or not (len(currency) == 3 and currency.isalpha() and currency.isupper()):
        raise Invalid("invalid_currency", "a three-letter code (ISO 4217)")
    if trip["start_date"] and trip["end_date"]:
        count = (trip["end_date"] - trip["start_date"]).days + 1
    elif trip["nights"]:
        count = int(trip["nights"]) + 1
    else:
        count = 1
    count = max(1, min(count, MAX_SKELETON_DAYS))
    tz = rules.default_tz(trip["destinations"], await ctx.conn.fetchval(AGENCY_TZ_SQL))
    rooms = rules.default_rooms(trip["party_adults"], trip["party_child_ages"] or [], trip["party_type"])
    # The agency's published booking terms (when there is a version): the client accepts them with the proposal.
    terms = await esign_terms.current(ctx)
    await ctx.conn.execute(
        INSERT_PROPOSAL_SQL,
        proposal_id,
        trip["trip_id"],
        version,
        await vault.encrypt("proposal", "body_enc", proposal_id, ""),
        currency,
        None,
        terms["doc_id"] if terms is not None else None,
        "total_with_deltas",
        json.dumps(rooms),
        DEFAULT_DEPOSIT_PCT,
        trip["start_date"],
        None,
        None,
    )
    # AA-1067: the agency's payment schedule template (else the AA default), dated from day 1.
    await _write_plan(ctx, proposal_id, schedule_plan.from_template(await _template(ctx), trip["start_date"]))
    for day_no in range(0, count + 1):
        day_id = uuid.uuid4()
        await ctx.conn.execute(INSERT_DAY_SQL, day_id, proposal_id, trip["trip_id"], day_no, None, None, tz, None, None)
        if 1 <= day_no < count:
            await ctx.conn.execute(
                INSERT_BLOCK_SQL, uuid.uuid4(), proposal_id, trip["trip_id"], day_id, 0, "Lodging", "lodging", True,
                None,
            )  # fmt: skip


async def _copy(
    ctx: TenantContext, vault: Vault, trip: Any, proposal_id: uuid.UUID, version: int, source_id: uuid.UUID
) -> None:
    """A working copy of an earlier version of the same trip, with its lineage and the pick counter it saw."""
    src = await ctx.conn.fetchrow(PROPOSAL_SQL, source_id)
    if src is None or src["trip_id"] != trip["trip_id"]:
        raise NotFound("proposal_not_found")
    if src["kind"] != KIND:
        raise Conflict("proposal_not_builder", "only a builder version is copied")
    body = await vault.decrypt("proposal", "body_enc", source_id, src["body_enc"])
    terms_doc_id = src["terms_doc_id"]
    if terms_doc_id is not None and src["terms_status"] != "published":
        # S262 round 3: the source's terms were retired since it went out; the copy starts with the current version.
        current = await esign_terms.current(ctx)
        terms_doc_id = current["doc_id"] if current is not None else None
    await ctx.conn.execute(
        INSERT_PROPOSAL_SQL,
        proposal_id,
        trip["trip_id"],
        version,
        await vault.encrypt("proposal", "body_enc", proposal_id, body),
        src["currency"],
        src["balance_due_on"],
        terms_doc_id,
        src["price_visibility"],
        json.dumps(_jsonb(src["room_config"])) if src["room_config"] is not None else None,
        src["deposit_pct"],
        src["starts_on"],
        source_id,
        src["selection_seq"],
    )
    s = await _structure(ctx, source_id)
    day_map: dict[str, uuid.UUID] = {}
    for d in s["days"]:
        day_id = uuid.uuid4()
        day_map[str(d["proposal_day_id"])] = day_id
        notes = None
        if d["notes_enc"] is not None:
            text = await vault.decrypt("proposal_day", "notes_enc", d["proposal_day_id"], d["notes_enc"])
            notes = await vault.encrypt("proposal_day", "notes_enc", day_id, text)
        await ctx.conn.execute(INSERT_DAY_SQL, day_id, proposal_id, trip["trip_id"], d["day_no"], d["title"],
                               d["place"], d["tz"], notes, d["proposal_day_id"])  # fmt: skip
    block_map: dict[str, uuid.UUID] = {}
    for b in s["blocks"]:
        block_id = uuid.uuid4()
        block_map[str(b["option_block_id"])] = block_id
        await ctx.conn.execute(INSERT_BLOCK_SQL, block_id, proposal_id, trip["trip_id"],
                               day_map[str(b["proposal_day_id"])], b["sort_order"], b["title"], b["type"],
                               b["client_may_swap"], b["option_block_id"])  # fmt: skip
    option_map: dict[str, uuid.UUID] = {}
    for o in s["options"]:
        option_id = uuid.uuid4()
        option_map[str(o["option_id"])] = option_id
        await ctx.conn.execute(
            INSERT_OPTION_SQL, option_id, proposal_id, trip["trip_id"], block_map[str(o["option_block_id"])],
            o["sort_order"], o["source"], o["title"], o["tier"], o["agency_supplier_id"], o["sell_amount"],
            o["net_amount"], o["pay_on_arrival"], o["choose_later_allowed"], o["availability"],
            o["availability_checked_at"], o["aa_tour_source_id"], o["aa_component_id"], o["aa_component_kind"],
            o["aa_departure_id"], o["aa_quote_id"], o["aa_quote_valid_until"], o["aa_from_price"],
            o["below_from_price"], o["option_id"], o["aa_nights"], _rules_json(o.get("price_rules")),
        )  # fmt: skip
        # AA-1066: the per-traveller rows go with their option.
        await pricing.replace(ctx, proposal_id, option_id, s["prices"].get(str(o["option_id"]), []))
    if s["discount_pct"]:
        await ctx.conn.execute("UPDATE crm.proposal SET discount_pct = $2 WHERE proposal_id = $1", proposal_id,
                               s["discount_pct"])  # fmt: skip
    if s.get("schedule_plan"):  # AA-1067: the payment lines go with the copy
        await _write_plan(ctx, proposal_id, s["schedule_plan"])
    policy_id = src.get("cancellation_policy_id")
    if policy_id is not None:
        # The policy the source went out with, while it is still current (else Publish pins the agency default).
        if await _policy_live(ctx, policy_id):
            await ctx.conn.execute(COPY_POLICY_SQL, proposal_id, policy_id)
    for b in s["blocks"]:
        if b["default_option_id"] is not None:
            await ctx.conn.execute(SET_DEFAULT_SQL, block_map[str(b["option_block_id"])],
                                   option_map[str(b["default_option_id"])], b["default_pinned"])  # fmt: skip
    for r in s["rules"]:
        await ctx.conn.execute(INSERT_RULE_SQL, uuid.uuid4(), proposal_id, trip["trip_id"], r["kind"],
                               option_map[str(r["option_id"])], option_map[str(r["target_option_id"])])  # fmt: skip
    await prep_packs.copy(ctx, source_id, proposal_id, trip["trip_id"])  # AA-951: the day 0 packs it was sent with


# ── Settings ─────────────────────────────────────────────────────────────────────────────────────


async def _template(ctx: AnyContext) -> Any:
    raw = await ctx.conn.fetchval(TEMPLATE_SQL)
    return json.loads(raw) if isinstance(raw, str) else raw


async def _write_plan(ctx: AnyContext, proposal_id: uuid.UUID, lines: list[schedule_plan.Line]) -> None:
    await ctx.conn.execute(SET_PLAN_SQL, proposal_id, json.dumps(schedule_plan.to_json(lines)),
                           schedule_plan.deposit_pct(lines), schedule_plan.balance_due_on(lines))  # fmt: skip


async def _policy_live(ctx: AnyContext, policy_id: uuid.UUID) -> bool:
    try:
        await cancel_policy.check_choice(ctx, policy_id)
    except (Conflict, NotFound):
        return False
    return True


def _plan_changes(
    changes: dict[str, Any], plan: list[schedule_plan.Line] | None, starts_on: dt.date | None, template: Any
) -> list[schedule_plan.Line] | None:
    """AA-1067: the version's payment lines after a settings write: ``schedule`` (the lines, ``null`` = back to the
    deposit / balance pair), ``schedule_reset`` (from the agency template), or the old ``deposit_pct`` /
    ``balance_due_on`` fields applied to the deposit and the last line; a new day 1 moves the lines that follow the
    template."""
    if changes.get("schedule_reset"):
        return schedule_plan.from_template(template, starts_on)
    if "schedule" in changes:
        if changes["schedule"] is None:
            return None
        return schedule_plan.check_plan(changes["schedule"], starts_on)
    if plan is None:
        return None
    lines = list(plan)
    if "deposit_pct" in changes and changes["deposit_pct"] is not None:
        pct = Decimal(str(changes["deposit_pct"]))
        if pct == 100:
            return [schedule_plan.Line("deposit", 1, pct, None, None)]  # paid in full at accept: one line
        lines[0] = schedule_plan.Line("deposit", 1, pct, None, None)
    if "balance_due_on" in changes and len(lines) > 1:
        last = lines[-1]
        lines[-1] = schedule_plan.Line(last.kind, last.seq, last.percent, changes["balance_due_on"], None)
    if "starts_on" in changes:
        lines = schedule_plan.redate(lines, starts_on)
    if lines != list(plan):
        # Re-checked as a whole (a new deposit share may leave nothing for the balance).
        return schedule_plan.check_plan(schedule_plan.to_json(lines), starts_on)
    return lines


async def update_settings(
    ctx: TenantContext, vault: Vault, proposal_id: uuid.UUID, seq: int, changes: dict[str, Any]
) -> dict[str, Any]:
    row = await _lock(ctx, proposal_id, seq)
    visibility = changes.get("price_visibility", row["price_visibility"])
    if visibility not in ("total_with_deltas", "total_only"):
        raise Invalid("invalid_price_visibility", "total_with_deltas or total_only")
    rooms = _jsonb(row["room_config"])
    if "room_config" in changes:
        try:
            rooms = rules.check_rooms(changes["room_config"])
        except ValueError as exc:
            raise Invalid("invalid_room_config", str(exc)) from None
    if "deposit_pct" in changes and changes["deposit_pct"] is None:
        raise Invalid("invalid_deposit_pct", "1 to 100")
    pct = Decimal(str(changes["deposit_pct"])) if "deposit_pct" in changes else row["deposit_pct"]
    if pct is None or not (Decimal("1") <= pct <= Decimal("100")) or pct != pct.quantize(rules.CENT):
        raise Invalid("invalid_deposit_pct", "1 to 100")
    starts_on = changes["starts_on"] if "starts_on" in changes else row["starts_on"]
    balance = changes["balance_due_on"] if "balance_due_on" in changes else row["balance_due_on"]
    terms = changes["terms_doc_id"] if "terms_doc_id" in changes else row["terms_doc_id"]
    if terms is not None and terms != row["terms_doc_id"]:
        # A new choice must be the published version (S262 round 3); the version already named may stay (a retired
        # one then blocks Publish with terms_retired until the agent picks the current terms).
        doc = await ctx.conn.fetchrow(TERMS_SQL, terms)
        if doc is None:
            raise NotFound("terms_not_found")
        if doc["status"] != "published":
            raise Conflict("terms_not_published", "choose the published booking terms version")
    # AA-1067: the payment lines (money fields: the route checks MONEY_FIELDS_EDIT) and the cancellation policy.
    s_plan = await _plan_of(ctx, proposal_id)
    plan = _plan_changes(changes, s_plan["schedule_plan"], starts_on, await _template(ctx))
    if plan is not None:
        pct = schedule_plan.deposit_pct(plan) or pct
        balance = schedule_plan.balance_due_on(plan)
    policy_id = row.get("cancellation_policy_id")
    if "cancellation_policy_id" in changes:
        policy_id = await cancel_policy.check_choice(ctx, changes["cancellation_policy_id"])
    # AA-1066: the discount (a money field: the route checks MONEY_FIELDS_EDIT); 0 or null = none.
    disc = changes["discount_pct"] if "discount_pct" in changes else await _discount_of(ctx, proposal_id)
    if disc is not None:
        disc = Decimal(str(disc))
        if not (Decimal("0") <= disc <= Decimal("100")) or disc != disc.quantize(rules.CENT):
            raise Invalid("invalid_discount_pct", "0 to 100, two decimals at most")
        disc = disc or None
    if "discount_pct" in changes:
        ctx.audit("proposal_discount", "allowed", "proposal", str(proposal_id), reason="set" if disc else "cleared")
    body = row["body_enc"]
    if "intro" in changes:
        text = (changes["intro"] or "").replace("\r\n", "\n").strip()
        if len(text) > MAX_BODY:
            raise Invalid("body_too_long", f"at most {MAX_BODY} characters")
        body = await vault.encrypt("proposal", "body_enc", proposal_id, text)
    plan_json = json.dumps(schedule_plan.to_json(plan)) if plan is not None else None
    await ctx.conn.execute(SETTINGS_SQL, proposal_id, visibility, json.dumps(rooms) if rooms else None, pct,
                           starts_on, balance, terms, body, disc, plan_json, policy_id)  # fmt: skip
    return await _done(ctx, vault, proposal_id, "settings")


async def set_before_you_go(
    ctx: TenantContext, vault: Vault, proposal_id: uuid.UUID, seq: int, group_keys: list[str]
) -> dict[str, Any]:
    """AA-951 (CRM-22, R38): the day 0 packs of a draft (every role; the version counter like every builder write)."""
    row = await _lock(ctx, proposal_id, seq)
    await prep_packs.replace(ctx, proposal_id, row["trip_id"], group_keys)
    return await _done(ctx, vault, proposal_id, "prep_packs")


async def _done(ctx: TenantContext, vault: Vault, proposal_id: uuid.UUID, reason: str) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(LOCK_SQL, proposal_id)
    await _bump(ctx, row)
    await stages.touch(ctx, row["trip_id"])
    ctx.audit("proposal_builder_edit", "allowed", "proposal", str(proposal_id), reason=reason)
    return await view(ctx, vault, proposal_id, audit=False)


# ── Days ─────────────────────────────────────────────────────────────────────────────────────────


async def add_day(
    ctx: TenantContext, vault: Vault, proposal_id: uuid.UUID, seq: int, body: dict[str, Any]
) -> dict[str, Any]:
    row = await _lock(ctx, proposal_id, seq)
    s = await _structure(ctx, proposal_id)
    trip_days = [d for d in s["days"] if d["day_no"] >= 1]
    if len(trip_days) >= MAX_DAYS:
        raise Conflict("too_many_days", f"at most {MAX_DAYS} days")
    last = max((d["day_no"] for d in trip_days), default=0)
    day_no = body.get("day_no") or last + 1
    if not 1 <= day_no <= last + 1:
        raise Invalid("invalid_day_no", f"1 to {last + 1}")
    if day_no <= last:
        # Make room: the days from day_no on move one later (the unique key is checked at commit).
        await ctx.conn.execute(SHIFT_DAYS_SQL, proposal_id, day_no, 1)
    tz = _tz(body["tz"]) if body.get("tz") else (trip_days[-1]["tz"] if trip_days else
                                                  rules.default_tz(row["destinations"], None))  # fmt: skip
    day_id = uuid.uuid4()
    notes = _notes(body.get("notes"))
    await ctx.conn.execute(
        INSERT_DAY_SQL, day_id, proposal_id, row["trip_id"], day_no, _text(body.get("title"), "title", 120),
        _text(body.get("place"), "place", 120), tz,
        await vault.encrypt("proposal_day", "notes_enc", day_id, notes) if notes else None, None,
    )  # fmt: skip
    return await _done(ctx, vault, proposal_id, "day_add")


async def _day(s: dict[str, Any], day_id: uuid.UUID) -> dict[str, Any]:
    day = next((d for d in s["days"] if d["proposal_day_id"] == day_id), None)
    if day is None:
        raise NotFound("day_not_found")
    return day


async def update_day(
    ctx: TenantContext, vault: Vault, proposal_id: uuid.UUID, seq: int, day_id: uuid.UUID, changes: dict[str, Any]
) -> dict[str, Any]:
    await _lock(ctx, proposal_id, seq)
    day = await _day(await _structure(ctx, proposal_id), day_id)
    title = _text(changes["title"], "title", 120) if "title" in changes else day["title"]
    place = _text(changes["place"], "place", 120) if "place" in changes else day["place"]
    tz = _tz(changes["tz"]) if "tz" in changes else day["tz"]
    notes_enc = day["notes_enc"]
    if "notes" in changes:
        notes = _notes(changes["notes"])
        notes_enc = await vault.encrypt("proposal_day", "notes_enc", day_id, notes) if notes else None
    await ctx.conn.execute(UPDATE_DAY_SQL, day_id, title, place, tz, notes_enc)
    return await _done(ctx, vault, proposal_id, "day_edit")


async def _delete_blocks(ctx: TenantContext, block_ids: list[uuid.UUID]) -> None:
    """Child first (crm-019 has no ON DELETE actions): picks, rules naming the options, the defaults, options,
    blocks."""
    if not block_ids:
        return
    await ctx.conn.execute(
        "DELETE FROM crm.option_rule WHERE option_id IN (SELECT option_id FROM crm.option "
        "WHERE option_block_id = ANY($1::uuid[])) OR target_option_id IN (SELECT option_id FROM crm.option "
        "WHERE option_block_id = ANY($1::uuid[]))",
        block_ids,
    )
    await ctx.conn.execute(
        "UPDATE crm.option_block SET default_option_id = NULL, default_pinned = false"
        " WHERE option_block_id = ANY($1::uuid[])",
        block_ids,
    )
    await ctx.conn.execute("DELETE FROM crm.option WHERE option_block_id = ANY($1::uuid[])", block_ids)
    await ctx.conn.execute("DELETE FROM crm.option_block WHERE option_block_id = ANY($1::uuid[])", block_ids)


async def delete_day(
    ctx: TenantContext, vault: Vault, proposal_id: uuid.UUID, seq: int, day_id: uuid.UUID
) -> dict[str, Any]:
    await _lock(ctx, proposal_id, seq)
    s = await _structure(ctx, proposal_id)
    day = await _day(s, day_id)
    if day["day_no"] == 0:
        raise Conflict("before_you_go_kept", "the Before you go day stays (CRM-22 fills it)")
    if len([d for d in s["days"] if d["day_no"] >= 1]) <= 1:
        raise Conflict("last_day_kept", "a proposal keeps at least one day")
    await _delete_blocks(ctx, [b["option_block_id"] for b in s["blocks"] if b["proposal_day_id"] == day_id])
    await ctx.conn.execute("DELETE FROM crm.proposal_day WHERE proposal_day_id = $1", day_id)
    await ctx.conn.execute(SHIFT_DAYS_SQL, proposal_id, day["day_no"] + 1, -1)
    return await _done(ctx, vault, proposal_id, "day_delete")


async def reorder_days(
    ctx: TenantContext, vault: Vault, proposal_id: uuid.UUID, seq: int, day_ids: list[uuid.UUID]
) -> dict[str, Any]:
    await _lock(ctx, proposal_id, seq)
    s = await _structure(ctx, proposal_id)
    current = [d["proposal_day_id"] for d in s["days"] if d["day_no"] >= 1]
    if sorted(map(str, day_ids)) != sorted(map(str, current)) or len(set(day_ids)) != len(day_ids):
        raise Invalid("invalid_day_order", "name every trip day once (Before you go stays first)")
    await ctx.conn.execute(RENUMBER_SQL, proposal_id, day_ids)
    return await _done(ctx, vault, proposal_id, "day_order")


# ── Blocks ───────────────────────────────────────────────────────────────────────────────────────


def _block_type(value: Any) -> str:
    if value not in rules.BLOCK_TYPES:
        raise Invalid("invalid_block_type", ", ".join(rules.BLOCK_TYPES))
    return str(value)


async def add_block(
    ctx: TenantContext, vault: Vault, proposal_id: uuid.UUID, seq: int, day_id: uuid.UUID, body: dict[str, Any]
) -> dict[str, Any]:
    row = await _lock(ctx, proposal_id, seq)
    s = await _structure(ctx, proposal_id)
    await _day(s, day_id)
    on_day = [b for b in s["blocks"] if b["proposal_day_id"] == day_id]
    if len(on_day) >= MAX_BLOCKS_PER_DAY:
        raise Conflict("too_many_blocks", f"at most {MAX_BLOCKS_PER_DAY} blocks a day")
    await ctx.conn.execute(
        INSERT_BLOCK_SQL, uuid.uuid4(), proposal_id, row["trip_id"], day_id,
        max((b["sort_order"] for b in on_day), default=-1) + 1, _text(body.get("title"), "title", 120, required=True),
        _block_type(body.get("type", "other")), bool(body.get("client_may_swap", True)), None,
    )  # fmt: skip
    return await _done(ctx, vault, proposal_id, "block_add")


def _block(s: dict[str, Any], block_id: uuid.UUID) -> dict[str, Any]:
    block = next((b for b in s["blocks"] if b["option_block_id"] == block_id), None)
    if block is None:
        raise NotFound("block_not_found")
    return block


async def update_block(
    ctx: TenantContext, vault: Vault, proposal_id: uuid.UUID, seq: int, block_id: uuid.UUID, changes: dict[str, Any]
) -> dict[str, Any]:
    await _lock(ctx, proposal_id, seq)
    s = await _structure(ctx, proposal_id)
    block = _block(s, block_id)
    default = block["default_option_id"]
    pinned = block["default_pinned"]
    if "default_option_id" in changes:
        default = changes["default_option_id"]
        pinned = default is not None  # chosen by hand: the Stage 2 tier rule no longer moves it
        option = _option_map(s).get(str(default)) if default is not None else None
        if default is not None and (option is None or option["option_block_id"] != block_id):
            raise Invalid("invalid_default_option", "one of this block's own options")
        if option is not None and option["availability"] == "sold_out":
            raise Conflict("option_sold_out", "a sold-out option cannot be the default")
    sort_order = block["sort_order"] if changes.get("sort_order") is None else int(changes["sort_order"])
    if not 0 <= sort_order <= 99:
        raise Invalid("invalid_sort_order")
    title = _text(changes["title"], "title", 120, required=True) if "title" in changes else block["title"]
    kind = _block_type(changes["type"]) if "type" in changes else block["type"]
    swap = block["client_may_swap"] if changes.get("client_may_swap") is None else bool(changes["client_may_swap"])
    await ctx.conn.execute(UPDATE_BLOCK_SQL, block_id, title, kind, swap, default, sort_order, pinned)
    _raise_conflicts(_conflicts(await _structure(ctx, proposal_id)), s)
    return await _done(ctx, vault, proposal_id, "block_edit")


async def delete_block(
    ctx: TenantContext, vault: Vault, proposal_id: uuid.UUID, seq: int, block_id: uuid.UUID
) -> dict[str, Any]:
    await _lock(ctx, proposal_id, seq)
    _block(await _structure(ctx, proposal_id), block_id)
    await _delete_blocks(ctx, [block_id])
    return await _done(ctx, vault, proposal_id, "block_delete")


# ── Options ──────────────────────────────────────────────────────────────────────────────────────


async def _tier(ctx: TenantContext, vault: Vault, row: Any) -> str | None:
    return (await _stage2_basis(ctx, vault, row))["accommodation_tier"]


async def _settle_default(ctx: TenantContext, proposal_id: uuid.UUID, block_id: uuid.UUID, tier: str | None) -> None:
    """Spec rule 5: a lodging block's default is the option of the lead's Stage 2 accommodation tier when it has one;
    any block without a default takes its first option that is not sold out. A default the agent chose by hand
    (``default_pinned``) is never moved: what the client is offered changes only when the agent changes it."""
    s = await _structure(ctx, proposal_id)
    block = _block(s, block_id)
    options = [o for o in s["options"] if o["option_block_id"] == block_id]
    current = _option_map(s).get(str(block["default_option_id"])) if block["default_option_id"] else None
    if current is not None and block["default_pinned"]:
        return
    want = rules.tier_default(options, tier) if block["type"] == "lodging" else None
    if want is not None and (current is None or current["tier"] != tier):
        await ctx.conn.execute(SET_DEFAULT_SQL, block_id, uuid.UUID(want), False)
    elif current is None:
        first = next((o["option_id"] for o in options if o["availability"] != "sold_out"), None)
        if first is not None:
            await ctx.conn.execute(SET_DEFAULT_SQL, block_id, first, False)


def _tier_in(value: Any) -> str | None:
    if value is not None and value not in rules.TIERS:
        raise Invalid("invalid_tier", ", ".join(rules.TIERS))
    return value


async def _supplier(ctx: TenantContext, supplier_id: uuid.UUID | None) -> uuid.UUID | None:
    if supplier_id is None:
        return None
    row = await ctx.conn.fetchrow(SUPPLIER_SQL, supplier_id)
    if row is None:
        raise NotFound("supplier_not_found")
    if row["kind"] == "aa":
        raise Invalid("aa_items_from_the_drawer", "AA items come from the AA drawer (a live quote), never typed")
    return supplier_id


async def add_agency_option(
    ctx: TenantContext, vault: Vault, proposal_id: uuid.UUID, seq: int, block_id: uuid.UUID, body: dict[str, Any]
) -> dict[str, Any]:
    """An item the agency books itself (Suppliers, or typed): sell and net are the agent's (money fields)."""
    row = await _lock(ctx, proposal_id, seq)
    s = await _structure(ctx, proposal_id)
    _block(s, block_id)
    in_block = [o for o in s["options"] if o["option_block_id"] == block_id]
    if len(in_block) >= MAX_OPTIONS_PER_BLOCK:
        raise Conflict("too_many_options", f"at most {MAX_OPTIONS_PER_BLOCK} options a block")
    await ctx.conn.execute(
        INSERT_OPTION_SQL, uuid.uuid4(), proposal_id, row["trip_id"], block_id,
        max((o["sort_order"] for o in in_block), default=-1) + 1, "agency",
        _text(body.get("title"), "title", 200, required=True), _tier_in(body.get("tier")),
        await _supplier(ctx, body.get("agency_supplier_id")), _money_in(body.get("sell_amount"), "sell_amount") or 0,
        _money_in(body.get("net_amount"), "net_amount"), bool(body.get("pay_on_arrival", False)),
        bool(body.get("choose_later_allowed", False)), "agency", None, None, None, None, None, None, None, None, None,
        None, None, None,
    )  # fmt: skip
    await _settle_default(ctx, proposal_id, block_id, await _tier(ctx, vault, row))
    _raise_conflicts(_conflicts(await _structure(ctx, proposal_id)), s)
    return await _done(ctx, vault, proposal_id, "option_add")


TRIP_NIGHTS_SQL = "SELECT nights FROM crm.trip WHERE trip_id = $1"


async def add_catalogue_option(
    ctx: TenantContext,
    vault: Vault,
    proposal_id: uuid.UUID,
    seq: int,
    block_id: uuid.UUID,
    service_id: uuid.UUID,
    *,
    quantity: int | None = None,
    nights: int | None = None,
) -> dict[str, Any]:
    """AA-1069 "Add from catalogue": one agency option priced from a catalogue service (a snapshot: title, supplier,
    sell and net copied now; no key back to the service). The quantity comes from the party, the rooms and the nights
    (:func:`app.catalogue.service.quantity`) unless the member typed one. The view carries ``catalogue`` (how it was
    priced; the net parts for owner / agent only)."""
    row = await _lock(ctx, proposal_id, seq)
    s = await _structure(ctx, proposal_id)
    day = await _day(s, _block(s, block_id)["proposal_day_id"])
    proposal = {
        "currency": row["currency"],
        "room_config": _jsonb(row["room_config"]),
        "party_adults": row["party_adults"],
        "party_child_ages": row["party_child_ages"],
        "trip_nights": await ctx.conn.fetchval(TRIP_NIGHTS_SQL, row["trip_id"]),
    }
    body, summary = await catalogue.plan_option(
        ctx, service_id, proposal=proposal, service_date=_day_date(row["starts_on"], day["day_no"]),
        days=len(s["days"]), quantity_override=quantity, nights_override=nights,
    )  # fmt: skip
    out = await add_agency_option(ctx, vault, proposal_id, seq, block_id, body)
    out["catalogue"] = summary
    return out


def _option(s: dict[str, Any], option_id: uuid.UUID) -> dict[str, Any]:
    option = next((o for o in s["options"] if o["option_id"] == option_id), None)
    if option is None:
        raise NotFound("option_not_found")
    return option


def _below_from(option: dict[str, Any], sell: Decimal, pax: int) -> bool | None:
    if option.get("aa_from_price") is None or option.get("aa_component_kind") != "trip_plan" or pax < 1 or sell <= 0:
        return None  # an unpriced AA item (sell 0) is blocked by aa_item_unpriced, not warned about
    return sell / pax < Decimal(option["aa_from_price"])


def _flag(changes: dict[str, Any], name: str, current: dict[str, Any]) -> bool:
    """A yes / no field of a PATCH: absent or null keeps the current value."""
    return bool(current[name]) if changes.get(name) is None else bool(changes[name])


def _party(row: Any) -> int:
    rooms = _jsonb(row["room_config"]) or []
    counted = sum(int(r.get("adults", 0)) + int(r.get("children", 0)) for r in rooms)
    return counted or int(row["party_adults"]) + len(row["party_child_ages"] or [])


async def update_option(
    ctx: TenantContext, vault: Vault, proposal_id: uuid.UUID, seq: int, option_id: uuid.UUID, changes: dict[str, Any]
) -> dict[str, Any]:
    """Title, tier, supplier, sell, net, pay on arrival, choose later. An AA item's net is the quote's (never typed:
    403 ``aa_net_from_quote``) and it is never chosen later (Fix B9)."""
    row = await _lock(ctx, proposal_id, seq)
    s = await _structure(ctx, proposal_id)
    o = _option(s, option_id)
    aa = o["source"] == "aa"
    if aa and "net_amount" in changes:
        raise Forbidden("aa_net_from_quote", "an AA item's net price comes from the live quote (Refresh AA prices)")
    if aa and changes.get("choose_later_allowed"):
        raise Invalid("choose_later_agency_only", "choose later is for agency items only (AA: decided at accept)")
    if aa and changes.get("agency_supplier_id") is not None:
        raise Invalid("aa_items_from_the_drawer")
    rows = s["prices"].get(str(option_id), [])
    if rows and ("sell_amount" in changes or "net_amount" in changes or changes.get("use_suggested")):
        # AA-1066: the per-traveller rows decide the amounts (edit them, or remove them first).
        raise Conflict("amount_from_prices", "this item is priced per traveller; edit the traveller prices")
    applied = o.get("price_rules")
    if changes.get("use_suggested"):
        # AA-1066: the suggestion (AA-1030 markup or RRP, then the agency's price rules), with the rules used kept.
        suggested = _suggestion(o, party=_party(row), rctx=await _retail_ctx(ctx, s),
                                service_date=_option_dates(row, s).get(str(option_id)))  # fmt: skip
        if suggested is None:
            raise Conflict("no_suggestion", "AA suggests no price for this item; type the sell")
        changes = {**changes, "sell_amount": suggested[0]}
        applied = suggested[2]
    elif "sell_amount" in changes:
        applied = None  # priced by hand
    sell = _money_in(changes["sell_amount"], "sell_amount") if "sell_amount" in changes else o["sell_amount"]
    if sell is None:
        raise Invalid("invalid_sell_amount")
    net = _money_in(changes["net_amount"], "net_amount") if "net_amount" in changes else o["net_amount"]
    supplier = (await _supplier(ctx, changes["agency_supplier_id"]) if "agency_supplier_id" in changes
                else o["agency_supplier_id"])  # fmt: skip
    await ctx.conn.execute(
        UPDATE_OPTION_SQL, option_id,
        _text(changes["title"], "title", 200, required=True) if "title" in changes else o["title"],
        _tier_in(changes["tier"]) if "tier" in changes else o["tier"], supplier, sell, net,
        _flag(changes, "pay_on_arrival", o), _flag(changes, "choose_later_allowed", o),
        _below_from(o, Decimal(sell), _party(row)) if aa else None, _rules_json(applied),
    )  # fmt: skip
    if "tier" in changes:
        await _settle_default(ctx, proposal_id, o["option_block_id"], await _tier(ctx, vault, row))
    return await _done(ctx, vault, proposal_id, "option_edit")


def _rules_json(value: Any) -> str | None:
    """``option.price_rules`` for a write: the rules used (a list), as stored (JSON text), or None."""
    if value is None:
        return None
    return value if isinstance(value, str) else json.dumps(value)


async def set_option_prices(
    ctx: TenantContext,
    vault: Vault,
    proposal_id: uuid.UUID,
    seq: int,
    option_id: uuid.UUID,
    prices: list[dict[str, Any]] | None,
    *,
    use_suggested: bool = False,
) -> dict[str, Any]:
    """AA-1066 (item crm-05): the price per traveller type of an option (money fields: the route checks
    ``MONEY_FIELDS_EDIT``). ``prices`` replaces the rows, ``[]`` removes them (the sell stays their last sum);
    ``use_suggested`` takes the grid the page shows (the suggestion of an AA item after the agency's price rules, else
    the current sell, shared per head), with the rules used kept on the option. The sell becomes the rows' sum; an
    agency item's net the sum of its unit nets when the rows carry them."""
    row = await _lock(ctx, proposal_id, seq)
    s = await _structure(ctx, proposal_id)
    o = _option(s, option_id)
    aa = o["source"] == "aa"
    if prices == [] and not use_suggested:
        await pricing.replace(ctx, proposal_id, option_id, [])
        return await _done(ctx, vault, proposal_id, "option_prices")
    applied: list[dict[str, str]] | None = None
    if use_suggested:
        party = _party(row)
        suggested = _suggestion(o, party=party, rctx=await _retail_ctx(ctx, s),
                                service_date=_option_dates(row, s).get(str(option_id)))  # fmt: skip
        total = suggested[0] if suggested is not None else Decimal(o["sell_amount"])
        counts = pricing.party_counts(_jsonb(row["room_config"]), int(row["party_adults"]),
                                      row["party_child_ages"] or [], await pricing.settings(ctx))  # fmt: skip
        prices = pricing.grid(total, counts)
        applied = suggested[2] if suggested is not None else None
    clean = pricing.check_rows(prices, aa=aa)
    sell, net_sum = pricing.sums(clean)
    net = o["net_amount"] if aa or net_sum is None else net_sum
    await pricing.replace(ctx, proposal_id, option_id, clean)
    await ctx.conn.execute(OPTION_PRICE_SQL, option_id, sell, net, _below_from(o, sell, _party(row)) if aa else None,
                           _rules_json(applied))  # fmt: skip
    return await _done(ctx, vault, proposal_id, "option_prices")


async def delete_option(
    ctx: TenantContext, vault: Vault, proposal_id: uuid.UUID, seq: int, option_id: uuid.UUID
) -> dict[str, Any]:
    row = await _lock(ctx, proposal_id, seq)
    s = await _structure(ctx, proposal_id)
    o = _option(s, option_id)
    await ctx.conn.execute(
        "DELETE FROM crm.option_rule WHERE proposal_id = $1 AND (option_id = $2 OR target_option_id = $2)",
        proposal_id,
        option_id,
    )
    await ctx.conn.execute(
        "UPDATE crm.option_block SET default_option_id = NULL, default_pinned = false"
        " WHERE option_block_id = $1 AND default_option_id = $2",
        o["option_block_id"],
        option_id,
    )
    await ctx.conn.execute("DELETE FROM crm.option WHERE option_id = $1", option_id)
    await _settle_default(ctx, proposal_id, o["option_block_id"], await _tier(ctx, vault, row))
    return await _done(ctx, vault, proposal_id, "option_delete")


# ── AA items (the drawer): two steps around the live Booking quote ──────────────────────────────


async def aa_plan(
    ctx: TenantContext, proposal_id: uuid.UUID, seq: int, block_id: uuid.UUID, body: dict[str, Any]
) -> dict[str, Any]:
    """Step 1, inside the transaction: what to quote (the served tour from the catalog cache, the day's date, the
    party from the rooms). Nothing is written; Booking is asked after the transaction (no connection held)."""
    row = await _lock(ctx, proposal_id, seq)
    s = await _structure(ctx, proposal_id)
    block = _block(s, block_id)
    if len([o for o in s["options"] if o["option_block_id"] == block_id]) >= MAX_OPTIONS_PER_BLOCK:
        raise Conflict("too_many_options", f"at most {MAX_OPTIONS_PER_BLOCK} options a block")
    if body.get("component_kind") == "hotel_room" and body.get("nights") is None:
        raise Invalid("invalid_nights", "a hotel room needs its nights")  # as AA-947 aa_lines
    tour = await catalog.tour(ctx, body["tour_source_id"])
    if tour is None or tour["served_state"] != "served":
        raise Conflict("aa_tour_not_served", "AA no longer offers this tour")
    day = await _day(s, block["proposal_day_id"])
    date = _day_date(row["starts_on"], day["day_no"])
    if date is None:
        raise Invalid("day_has_no_date", "set the date of day 1 first (an AA item is quoted for a date)")
    pax = rules.room_pax(_jsonb(row["room_config"]), int(row["party_adults"]), row["party_child_ages"] or [])
    return {
        "option_id": str(uuid.uuid4()),
        "proposal_id": str(proposal_id),
        "version": row["version"],
        "currency": row["currency"],
        "block_id": str(block_id),
        "title": tour["title"],
        "tour_source_id": body["tour_source_id"],
        "component_id": body["component_id"],
        "component_kind": body["component_kind"],
        "departure_id": body.get("departure_id"),
        "nights": body.get("nights"),
        "service_date": date.isoformat(),
        "pax": pax,
        "party": _party(row),
        "seq": seq,
    }


async def add_aa_option(
    ctx: TenantContext,
    vault: Vault,
    plan: dict[str, Any],
    quote: Any,
    body: dict[str, Any],
) -> dict[str, Any]:
    """Step 2, in a new transaction: the option with the live quote (net = b2b price, availability). A sold-out
    answer adds nothing (409 ``aa_sold_out``: "Sold out (cannot add)", WF-4). Without a sell in the request the sell
    stays 0 (never the b2b price: the assistant reads the sell, §4.6) and Publish waits for an owner / agent to set it
    (``aa_item_unpriced``). AA-1030: an owner or agent who sends no sell gets the suggested retail price
    (:mod:`app.proposals.retail`: the agency markup on the B2B price, else AA's RRP for a tour), when there is one."""
    proposal_id = uuid.UUID(plan["proposal_id"])
    block_id = uuid.UUID(plan["block_id"])
    row = await _lock(ctx, proposal_id, int(plan["seq"]))
    if quote.availability == "sold_out":
        raise Conflict("aa_sold_out", "AA has no space for this item on that date")
    s = await _structure(ctx, proposal_id)
    in_block = [o for o in s["options"] if o["option_block_id"] == _block(s, block_id)["option_block_id"]]
    sell = _money_in(body.get("sell_amount"), "sell_amount") or Decimal("0.00")
    applied: list[dict[str, str]] | None = None
    if body.get("sell_amount") is None and sees_margin(ctx.member_role) and quote.b2b_price is not None:
        # AA-1066: the agency's price rules on top of the AA-1030 suggestion, the rules used kept on the option.
        new = {
            "source": "aa", "aa_component_kind": plan["component_kind"], "aa_tour_source_id": plan["tour_source_id"],
            "net_amount": rules.money(quote.b2b_price),
            "aa_from_price": rules.money(quote.from_price) if quote.from_price is not None else None,
        }  # fmt: skip
        rctx = {"markups": await retail.markups(ctx), "rules": await pricing.rules_of(ctx),
                "today": pricing.today_in(await ctx.conn.fetchval(AGENCY_TZ_SQL))}  # fmt: skip
        date = dt.date.fromisoformat(plan["service_date"]) if plan.get("service_date") else None
        suggested = _suggestion(new, party=plan["party"], rctx=rctx, service_date=date)
        if suggested is not None:
            sell, applied = suggested[0], suggested[2]
    below = None
    if quote.from_price is not None and plan["component_kind"] == "trip_plan" and plan["party"] >= 1 and sell > 0:
        below = sell / plan["party"] < quote.from_price
    title = plan["title"] if plan["component_kind"] == "trip_plan" else f"{plan['title']} ({plan['component_kind']})"
    await ctx.conn.execute(
        INSERT_OPTION_SQL, uuid.UUID(plan["option_id"]), proposal_id, row["trip_id"], block_id,
        max((o["sort_order"] for o in in_block), default=-1) + 1, "aa", title[:200], _tier_in(body.get("tier")),
        None, sell, rules.money(quote.b2b_price), False, False, AVAILABILITY_FROM_QUOTE[quote.availability],
        dt.datetime.now(dt.UTC), plan["tour_source_id"], plan["component_id"], plan["component_kind"],
        plan["departure_id"], quote.quote_id, quote.valid_until,
        rules.money(quote.from_price) if quote.from_price is not None else None, below, None, plan.get("nights"),
        _rules_json(applied),
    )  # fmt: skip
    await _settle_default(ctx, proposal_id, block_id, await _tier(ctx, vault, row))
    _raise_conflicts(_conflicts(await _structure(ctx, proposal_id)), s)
    return await _done(ctx, vault, proposal_id, "aa_option_add")


async def aa_refresh_plan(ctx: TenantContext, proposal_id: uuid.UUID, seq: int) -> list[dict[str, Any]]:
    """ "Refresh AA prices": every AA item of the draft, with what to quote again (the same nights it was quoted
    for), the oldest quote first (an expired or never-checked one before a fresh one), so a draft with more AA items
    than one run quotes (:data:`app.proposals.aa_options.MAX_REFRESH`) catches up run after run. An item on a day with
    no date has ``service_date`` None (reported, not quoted)."""
    row = await _lock(ctx, proposal_id, seq)
    s = await _structure(ctx, proposal_id)
    days = {d["proposal_day_id"]: d for d in s["days"]}
    blocks = {b["option_block_id"]: b for b in s["blocks"]}
    pax = rules.room_pax(_jsonb(row["room_config"]), int(row["party_adults"]), row["party_child_ages"] or [])
    oldest = dt.datetime.min.replace(tzinfo=dt.UTC)
    aa = sorted((o for o in s["options"] if o["source"] == "aa"),
                key=lambda o: o["aa_quote_valid_until"] or oldest)  # fmt: skip
    out = []
    for o in aa:
        date = _day_date(row["starts_on"], days[blocks[o["option_block_id"]]["proposal_day_id"]]["day_no"])
        out.append({
            "option_id": str(o["option_id"]), "proposal_id": str(proposal_id), "version": row["version"],
            "currency": row["currency"], "tour_source_id": o["aa_tour_source_id"],
            "component_id": o["aa_component_id"], "component_kind": o["aa_component_kind"],
            "departure_id": o["aa_departure_id"], "nights": o["aa_nights"],
            "service_date": date.isoformat() if date else None, "pax": pax, "party": _party(row),
            "sell": str(o["sell_amount"]), "seq": seq,
        })  # fmt: skip
    return out


async def apply_refresh(
    ctx: TenantContext, vault: Vault, proposal_id: uuid.UUID, seq: int, answers: list[tuple[dict[str, Any], Any]]
) -> dict[str, Any]:
    """The new quotes onto the draft (net, availability, validity, the "from"-price warning); the sell stays the
    agent's. A default that sold out blocks Publish until the agent picks another."""
    await _lock(ctx, proposal_id, seq)
    for plan, quote in answers:
        if quote.quote_id is None:  # sold out, no quote: keep the last price, mark the item
            await ctx.conn.execute(SOLD_OUT_SQL, uuid.UUID(plan["option_id"]))
            continue
        sell = Decimal(plan["sell"])
        below = None
        if quote.from_price is not None and plan["component_kind"] == "trip_plan" and plan["party"] >= 1 and sell > 0:
            below = sell / plan["party"] < quote.from_price
        await ctx.conn.execute(
            QUOTE_UPDATE_SQL, uuid.UUID(plan["option_id"]), rules.money(quote.b2b_price),
            AVAILABILITY_FROM_QUOTE[quote.availability], quote.quote_id, quote.valid_until,
            rules.money(quote.from_price) if quote.from_price is not None else None, below,
        )  # fmt: skip
    return await _done(ctx, vault, proposal_id, "aa_refresh")


# ── Rules (the Advanced section) ─────────────────────────────────────────────────────────────────


async def add_rule(
    ctx: TenantContext, vault: Vault, proposal_id: uuid.UUID, seq: int, body: dict[str, Any]
) -> dict[str, Any]:
    row = await _lock(ctx, proposal_id, seq)
    s = await _structure(ctx, proposal_id)
    kind = body.get("kind")
    if kind not in rules.RULE_KINDS:
        raise Invalid("invalid_rule_kind", "requires or excludes")
    a, b = _option(s, body["option_id"]), _option(s, body["target_option_id"])
    if a["option_block_id"] == b["option_block_id"]:
        raise Invalid("rule_same_block", "a rule links options of two different blocks")
    if len(s["rules"]) >= MAX_RULES:
        raise Conflict("too_many_rules", f"at most {MAX_RULES} rules")
    if any(r["kind"] == kind and r["option_id"] == a["option_id"] and r["target_option_id"] == b["option_id"]
           for r in s["rules"]):  # fmt: skip
        raise Conflict("rule_exists")
    await ctx.conn.execute(INSERT_RULE_SQL, uuid.uuid4(), proposal_id, row["trip_id"], kind, a["option_id"],
                           b["option_id"])  # fmt: skip
    _raise_conflicts(_conflicts(await _structure(ctx, proposal_id)), s)
    return await _done(ctx, vault, proposal_id, "rule_add")


async def delete_rule(
    ctx: TenantContext, vault: Vault, proposal_id: uuid.UUID, seq: int, rule_id: uuid.UUID
) -> dict[str, Any]:
    await _lock(ctx, proposal_id, seq)
    done = await ctx.conn.execute(
        "DELETE FROM crm.option_rule WHERE proposal_id = $1 AND option_rule_id = $2", proposal_id, rule_id
    )
    if done.endswith(" 0"):
        raise NotFound("rule_not_found")
    return await _done(ctx, vault, proposal_id, "rule_delete")


# ── Publish vN ───────────────────────────────────────────────────────────────────────────────────


def price_hash(row: Any, body: str, terms_sha256: bytes | None, s: dict[str, Any], sums: dict[str, Any]) -> bytes:
    """SHA-256 of the canonical record the client will see (no net, no margin): the version, its settings, every day,
    block and option with its sell amount, the rules, the computed amounts and the terms."""
    days = {d["proposal_day_id"]: d for d in s["days"]}
    record = {
        "proposal_id": str(row["proposal_id"]),
        "version": row["version"],
        "kind": KIND,
        "currency": row["currency"],
        "price_visibility": row["price_visibility"],
        "room_config": _jsonb(row["room_config"]),
        "starts_on": _iso(row["starts_on"]),
        "total": str(sums["sell_total"]),
        "deposit": str(sums["deposit"]),
        "pay_on_arrival": str(sums["pay_on_arrival_total"]),
        "balance_due_on": _iso(row["balance_due_on"]),
        "text_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "terms_doc_id": str(row["terms_doc_id"]) if row["terms_doc_id"] else None,
        "terms_sha256": terms_sha256.hex() if terms_sha256 else None,
        "days": [
            [d["day_no"], d["tz"], d["title"], d["place"], hashlib.sha256(bytes(d["notes_enc"] or b"")).hexdigest()]
            for d in s["days"]
        ],  # fmt: skip
        "blocks": [
            [
                str(b["option_block_id"]),
                days[b["proposal_day_id"]]["day_no"],
                b["sort_order"],
                b["title"],
                b["type"],
                b["client_may_swap"],
                str(b["default_option_id"]) if b["default_option_id"] else None,
            ]
            for b in s["blocks"]
        ],  # fmt: skip
        "options": [
            [
                str(o["option_id"]),
                str(o["option_block_id"]),
                o["title"],
                str(rules.money(o["sell_amount"])),
                o["pay_on_arrival"],
                o["choose_later_allowed"],
                o["availability"],
                o["aa_nights"],
            ]
            for o in s["options"]
        ],  # fmt: skip
        "rules": sorted([r["kind"], str(r["option_id"]), str(r["target_option_id"])] for r in s["rules"]),
    }
    # AA-1066: the per-traveller rows and the discount, added only when the version has them (an older version's
    # record, and so its hash, is unchanged).
    prices = s.get("prices") or {}
    if prices:
        record["option_prices"] = sorted(
            [oid, r["pax_type"], str(rules.money(r["unit_sell"])), int(r["qty"])]
            for oid, rows in prices.items() for r in rows
        )  # fmt: skip
    if s.get("discount_pct"):
        record["discount_pct"] = str(rules.money(s["discount_pct"]))
    # AA-1067: the payment lines and the cancellation policy, only when the version has them (older hashes unchanged).
    if s.get("schedule_plan"):
        record["schedule"] = schedule_plan.hash_record(s["schedule_plan"])
    if row.get("cancellation_policy_id") is not None:
        record["cancellation_policy_id"] = str(row["cancellation_policy_id"])
    return hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).digest()


async def publish(
    ctx: TenantContext, vault: Vault, proposal_id: uuid.UUID, seq: int, *, acknowledge_selection_seq: int | None
) -> dict[str, Any]:
    row = await _lock(ctx, proposal_id, seq)
    if row["trip_stage"] not in DRAFTABLE_STAGES:
        raise Conflict("trip_not_open_for_proposal", "a proposal is published before the trip is booked")
    s = await _structure(ctx, proposal_id)
    now = dt.datetime.now(dt.UTC)
    checks = _checks(row, s, now)
    if "option_conflict" in checks["blocking"]:
        _raise_conflicts(_conflicts(s), s)
    if checks["blocking"]:
        raise Invalid("not_publishable", "fix the items listed first", blocking=checks["blocking"])
    await _discount_gate(ctx, row, s)
    # A version without terms takes the agency's published booking terms (before the price hash, which covers them).
    row = await pin_current_terms(ctx, row)
    row = await cancel_policy.pin(ctx, row)  # AA-1067: the agency's cancellation policy goes out with the version
    terms_sha = None
    if row["terms_doc_id"] is not None:
        terms = await ctx.conn.fetchrow(TERMS_SQL, row["terms_doc_id"])
        if terms is None or terms["status"] != "published":
            raise Conflict("terms_not_published", "publish the booking terms first (Settings > Agreements)")
        terms_sha = bytes(terms["body_sha256"])
    base = await ctx.conn.fetchrow(BASE_SQL + " FOR SHARE", row["based_on_proposal_id"]) if row[
        "based_on_proposal_id"] else None  # fmt: skip
    if base is not None and int(base["selection_seq"]) > int(row["based_on_selection_seq"] or 0):
        # Card E / §11.2: the client picked on the version this copy came from after it was copied.
        if acknowledge_selection_seq != int(base["selection_seq"]):
            picks = [
                {"block": r["block_title"], "option": r["option_title"], "chosen_by": r["chosen_by"],
                 "at": _iso(r["updated_at"])}
                for r in await ctx.conn.fetch(PICKS_SINCE_SQL, base["proposal_id"])
            ]  # fmt: skip
            raise Conflict(
                "version_conflict",
                f"the client changed their picks on v{base['version']} while you edited; review them, then publish",
                reason="client_selected",
                base_version=base["version"],
                selection_seq=int(base["selection_seq"]),
                picks=picks,
            )
    sums = _charged(s, None, row["deposit_pct"])
    body = await vault.decrypt("proposal", "body_enc", proposal_id, row["body_enc"])
    digest = price_hash(row, body, terms_sha, s, sums)
    superseded = [str(r["proposal_id"]) for r in await ctx.conn.fetch(SUPERSEDE_PUBLISHED_SQL, row["trip_id"],
                                                                      proposal_id)]  # fmt: skip
    published_at = await ctx.conn.fetchval(PUBLISH_SQL, proposal_id, digest, sums["sell_total"], sums["deposit"])
    carried = await _carry_picks(ctx, row, s, base) if base is not None else 0
    await stages.touch(ctx, row["trip_id"])
    ctx.audit("proposal_publish", "allowed", "proposal", str(proposal_id), reason=f"v{row['version']}")
    out = await view(ctx, vault, proposal_id, audit=False)
    out["publish"] = {"published_at": _iso(published_at), "price_hash": digest.hex(), "superseded": superseded,
                      "picks_carried": carried}  # fmt: skip
    return out


async def _discount_gate(ctx: TenantContext, row: Any, s: dict[str, Any]) -> None:
    """AA-1066 (item crm-07): a discount above the agency limit, or a discounted total below the net, needs an
    owner's approval covering it. An owner's publish records their own; anyone else gets 409
    ``discount_needs_approval``."""
    sums = _charged(s, None, row["deposit_pct"])
    bands = await pricing.settings(ctx)
    above, below = discount.need(s["discount_pct"], sums["margin_total"], bands.discount_owner_limit_pct)
    if not (above or below):
        return
    if discount.covered(await discount.approvals_of(ctx, row["trip_id"]), s["discount_pct"], below):
        return
    if ctx.member_role == "owner":
        await discount.owner_record(ctx, trip_id=row["trip_id"], proposal_id=row["proposal_id"],
                                    discount_pct=s["discount_pct"], below_cost=below, bands=bands)  # fmt: skip
        return
    raise Conflict(
        "discount_needs_approval",
        "the agency owner approves this discount first (ask for approval on the builder page)",
        discount_pct=str(s["discount_pct"] or "0"),
        limit_pct=str(bands.discount_owner_limit_pct),
        reason="below_cost" if below and not above else "above_limit",
    )


async def request_discount_approval(
    ctx: TenantContext, vault: Vault, proposal_id: uuid.UUID, seq: int
) -> dict[str, Any]:
    """AA-1066: an agent asks the owner to approve the draft's discount (the version counter is checked, not moved:
    the content does not change)."""
    row = await _lock(ctx, proposal_id, seq)
    s = await _structure(ctx, proposal_id)
    sums = _charged(s, None, row["deposit_pct"])
    await discount.request(ctx, trip_id=row["trip_id"], proposal_id=proposal_id, discount_pct=s["discount_pct"],
                           margin_total=sums["margin_total"], bands=await pricing.settings(ctx))  # fmt: skip
    return await view(ctx, vault, proposal_id, audit=False)


async def decide_discount_approval(
    ctx: TenantContext, vault: Vault, proposal_id: uuid.UUID, approval_id: uuid.UUID, decision: str
) -> dict[str, Any]:
    """AA-1066: the owner approves or rejects a pending request of this draft (``DISCOUNT_APPROVE``)."""
    await _lock(ctx, proposal_id, None)
    await discount.decide(ctx, approval_id, decision, proposal_id=proposal_id)
    return await view(ctx, vault, proposal_id, audit=False)


async def _carry_picks(ctx: TenantContext, row: Any, s: dict[str, Any], base: Any) -> int:
    """The base version's picks onto the copied options (``carried``) where the option is still there, open and
    allowed, and the rules still hold; a pick that no longer fits falls back to the default."""
    base_picks = await ctx.conn.fetch(PICKS_SQL, base["proposal_id"])
    by_source = {str(o["source_option_id"]): o for o in s["options"] if o["source_option_id"]}
    blocks = {b["option_block_id"]: b for b in s["blocks"]}
    defaults = _defaults(s)
    kept: dict[str, str] = {}
    for p in base_picks:
        o = by_source.get(str(p["option_id"]))
        if o is None or o["availability"] == "sold_out":
            continue
        block = blocks[o["option_block_id"]]
        if not block["client_may_swap"] and str(block["default_option_id"]) != str(o["option_id"]):
            continue
        trial = {**kept, str(block["option_block_id"]): str(o["option_id"])}
        if rules.violations(rules.chosen(defaults, trial), _rule_objs(s["rules"])):
            continue
        kept = trial
    for block_id, option_id in kept.items():
        await ctx.conn.execute(INSERT_PICK_SQL, row["proposal_id"], row["trip_id"], uuid.UUID(block_id),
                               uuid.UUID(option_id), "carried", False)  # fmt: skip
    return len(kept)


# ── Picks (the seam CRM-21b's client page calls) ────────────────────────────────────────────────


async def record_pick(
    ctx: AnyContext,
    proposal_id: uuid.UUID,
    block_id: uuid.UUID,
    option_id: uuid.UUID,
    *,
    chosen_by: str = "client",
    choose_later: bool = False,
) -> dict[str, Any]:
    """One pick on a published / sent version (C-03 Keep / Swap). Refused: a closed version (409), a sold-out option
    (409 ``option_sold_out``), a swap the block does not allow (422 ``swap_not_allowed``), choose later on an AA item
    (422), a rule break (422 ``option_conflict``, card R). Moves ``selection_seq``, which a working copy of this version
    compares at publish (card E)."""
    row = await ctx.conn.fetchrow(PROPOSAL_SQL + "   FOR UPDATE OF p", proposal_id)
    if row is None:
        raise NotFound("proposal_not_found")
    if row["kind"] != KIND or row["status"] not in OPEN_FOR_PICKS:
        raise Conflict("proposal_not_open_for_picks")
    s = await _structure(ctx, proposal_id)
    block = _block(s, block_id)
    option = _option(s, option_id)
    if option["option_block_id"] != block_id:
        raise Invalid("option_not_in_block")
    if option["availability"] == "sold_out":
        raise Conflict("option_sold_out", "this option is no longer available")
    if not block["client_may_swap"] and option_id != block["default_option_id"]:
        raise Invalid("swap_not_allowed", "this item is fixed in the proposal")
    if choose_later and not option["choose_later_allowed"]:
        raise Invalid("choose_later_not_allowed")
    picks = {str(p["option_block_id"]): str(p["option_id"]) for p in await ctx.conn.fetch(PICKS_SQL, proposal_id)}
    picks[str(block_id)] = str(option_id)
    _raise_conflicts(_conflicts(s, picks), s)
    await ctx.conn.execute(INSERT_PICK_SQL, proposal_id, row["trip_id"], block_id, option_id, chosen_by, choose_later)
    selection_seq = int(await ctx.conn.fetchval(PICK_SEQ_SQL, proposal_id))
    sums = _charged(s, picks, row["deposit_pct"])
    ctx.audit("proposal_pick", "allowed", "proposal", str(proposal_id), reason=chosen_by)
    return {
        "proposal_id": str(proposal_id),
        "selection_seq": selection_seq,
        "sell_total": _amount(sums["sell_total"]),
        "deposit": _amount(sums["deposit"]),
    }
