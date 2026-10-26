"""What the client sees of a builder version (card CRM-21b, AA-950; design v3 C-03, C-04, C-12, WF-6 / WF-7, §2.9
rules 3 and 5, Decision D1, Fix B9, R19, R26). Built inside the client link's context by :mod:`app.proposals.public`;
the agency side reads the same choice diff (R19) through :mod:`app.proposals.service`.

- **The choice**: one option per block, the client's pick where there is one, else the default. A pick whose option
  sold out no longer counts (card E: the block falls back to its default and the total is recomputed).
  "Choose later" (Fix B9) is a pick of the block's default marked ``choose_later``; only an agency item allows it.
- **Prices (D1)**: the total, the deposit and the balance always; per option only the sell difference to the block's
  default (``total_with_deltas``), or nothing (``total_only``). Never a per-line price, a net, a margin, a traveller
  name or a cancelled line (§2.9 rule 3).
- **Groups (R26)**: the chosen items by confirmation class: confirmed when you pay (AA, instant), confirmed by our
  partner within 1 business day (AA, on request), your agent books (agency items), paid on arrival (listed, never
  charged).
- **Hashes**: ``selection_hash`` (the whole choice, accept sends it back) and the AA choice hash (what a hold covers).
- **Diffs**: the notice of a new version on the same link (what changed, how many picks carried over), and R19 the
  client's choice against the defaults (the bundled diff the agent gets).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from decimal import Decimal
from typing import Any

from app.proposals import builder, rules
from app.tenancy import AnyContext

CONFIRM_ON_PAYMENT = "on_payment"
CONFIRM_PARTNER = "partner"
CONFIRM_AGENT = "agent"
CONFIRM_ARRIVAL = "arrival"
CONFIRM_ORDER = (CONFIRM_ON_PAYMENT, CONFIRM_PARTNER, CONFIRM_AGENT, CONFIRM_ARRIVAL)
MAX_RECHECKS = 3
MAX_NOTICE = 30


def _money(value: Any) -> str:
    return str(rules.money(value))


def confirm_class(option: dict[str, Any]) -> str:
    if option["pay_on_arrival"]:
        return CONFIRM_ARRIVAL
    if option["source"] == "aa":
        return CONFIRM_ON_PAYMENT if option["availability"] == "instant" else CONFIRM_PARTNER
    return CONFIRM_AGENT


def choose_later_ok(block: dict[str, Any], options: dict[str, dict[str, Any]]) -> bool:
    """Fix B9: a block may be left for later only when its default is an agency item that allows it (no hold, no AA
    billing); an AA block is decided at accept."""
    default = options.get(str(block["default_option_id"])) if block["default_option_id"] else None
    return bool(default and default["source"] == "agency" and default["choose_later_allowed"])


def effective(s: dict[str, Any], pick_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The client's choice: ``picks`` (block -> option, sold-out picks dropped), ``later`` (blocks left for later),
    ``choice`` (every block: pick or default), ``dropped`` (blocks whose pick sold out)."""
    options = builder._option_map(s)
    picks: dict[str, str] = {}
    later: set[str] = set()
    dropped: list[str] = []
    for p in pick_rows:
        block, option = str(p["option_block_id"]), str(p["option_id"])
        o = options.get(option)
        if o is None:
            continue
        if o["availability"] == "sold_out":
            dropped.append(block)
            continue
        picks[block] = option
        if p["choose_later"]:
            later.add(block)
    choice = rules.chosen(builder._defaults(s), picks)
    return {"picks": picks, "later": later, "choice": choice, "dropped": dropped}


def selection_hash(choice: dict[str, str], later: set[str]) -> str:
    record = sorted([b, o, b in later] for b, o in choice.items())
    return hashlib.sha256(json.dumps(record, separators=(",", ":")).encode()).hexdigest()


def aa_chosen(choice: dict[str, str], options: dict[str, dict[str, Any]]) -> list[str]:
    return sorted(o for o in choice.values() if o in options and options[o]["source"] == "aa")


def aa_hash(aa_ids: list[str]) -> bytes:
    return hashlib.sha256(json.dumps(sorted(aa_ids), separators=(",", ":")).encode()).digest()


def totals(s: dict[str, Any], row: Any, picks: dict[str, str]) -> dict[str, Any]:
    sums = builder._charged(s, picks, row["deposit_pct"])
    return {
        "currency": row["currency"],
        "total": _money(sums["sell_total"]),
        "pay_on_arrival_total": _money(sums["pay_on_arrival_total"]),
        "deposit": _money(sums["deposit"]),
        "balance": _money(sums["sell_total"] - sums["deposit"]),
        "balance_due_on": row["balance_due_on"].isoformat() if row["balance_due_on"] else None,
        "_sell": sums["sell_total"],
        "_deposit": sums["deposit"],
    }


def public_totals(t: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in t.items() if not k.startswith("_")}


def _blocked(
    option_id: str, block_id: str, choice: dict[str, str], rule_objs: list[rules.Rule], titles: dict[str, str]
) -> dict[str, Any] | None:
    """Why choosing this option would break a rule (greyed with the reason, F5.2), or None."""
    trial = {**choice, block_id: option_id}
    for v in rules.violations(trial, rule_objs):
        if option_id in (v["option_id"], v["target_option_id"]):
            other = v["target_option_id"] if v["option_id"] == option_id else v["option_id"]
            return {"reason": v["kind"], "with": titles.get(other)}
    return None


def hold_state(row: Any, aa_ids: list[str], now: dt.datetime) -> dict[str, Any]:
    """The C-04 / C-12 hold box: ``none`` (no AA item chosen: nothing to hold), ``missing`` (not placed yet),
    ``active``, ``expired``, ``stale`` (the client changed an AA pick since)."""
    today = now.date()
    used = int(row["recheck_count_day"]) if row["recheck_day"] == today else 0
    left = max(0, MAX_RECHECKS - used) if int(row["hold_count"]) >= 1 else MAX_RECHECKS
    if not aa_ids:
        status = "none"
    elif row["hold_id"] is None:
        status = "missing"
    elif bytes(row["hold_selection_sha256"]) != aa_hash(aa_ids):
        status = "stale"
    elif row["hold_expires_at"] <= now:
        status = "expired"
    else:
        status = "active"
    return {
        "status": status,
        "expires_at": row["hold_expires_at"].isoformat() if row["hold_expires_at"] and status != "none" else None,
        "first_hold": int(row["hold_count"]) == 0,
        "rechecks_left": left,
    }


async def view(ctx: AnyContext, vault: Any, row: Any, s: dict[str, Any], pick_rows: list[dict[str, Any]],
               now: dt.datetime) -> dict[str, Any]:  # fmt: skip
    """C-03 / C-04 of one sent (or accepted) builder version. ``row`` is the client page's proposal row."""
    state = effective(s, pick_rows)
    choice, picks, later = state["choice"], state["picks"], state["later"]
    options = builder._option_map(s)
    defaults = builder._defaults(s)
    rule_objs = builder._rule_objs(s["rules"])
    titles = {str(o["option_id"]): o["title"] for o in s["options"]}
    deltas = row["price_visibility"] == "total_with_deltas"
    by_block: dict[str, list[dict[str, Any]]] = {}
    for o in s["options"]:
        oid, bid = str(o["option_id"]), str(o["option_block_id"])
        default = options.get(defaults.get(bid) or "")
        item: dict[str, Any] = {
            "option_id": oid,
            "title": o["title"],
            "is_default": defaults.get(bid) == oid,
            "chosen": choice.get(bid) == oid,
            "availability": "agency" if o["source"] == "agency" else o["availability"],
            "confirm_class": confirm_class(o),
            "pay_on_arrival": o["pay_on_arrival"],
            "delta": _money(Decimal(o["sell_amount"]) - Decimal(default["sell_amount"]))
            if deltas and default is not None
            else None,
            "blocked": {"reason": "sold_out"} if o["availability"] == "sold_out" else None,
        }
        if item["blocked"] is None and not item["chosen"]:
            item["blocked"] = _blocked(oid, bid, choice, rule_objs, titles)
        by_block.setdefault(bid, []).append(item)
    blocks_by_day: dict[str, list[dict[str, Any]]] = {}
    for b in s["blocks"]:
        bid = str(b["option_block_id"])
        if bid not in by_block:
            continue  # an empty block is nothing for the client
        blocks_by_day.setdefault(str(b["proposal_day_id"]), []).append({
            "option_block_id": bid,
            "title": b["title"],
            "type": b["type"],
            "client_may_swap": b["client_may_swap"],
            "choose_later": bid in later,
            "choose_later_allowed": choose_later_ok(b, options),
            "fell_back": bid in state["dropped"],
            "options": by_block[bid],
        })  # fmt: skip
    days = []
    for d in s["days"]:
        date = builder._day_date(row["starts_on"], d["day_no"])
        notes = None
        if d["notes_enc"] is not None:
            notes = await vault.decrypt("proposal_day", "notes_enc", d["proposal_day_id"], d["notes_enc"])
        blocks = blocks_by_day.get(str(d["proposal_day_id"]), [])
        if d["day_no"] == 0 and not blocks and not notes:
            continue  # an empty "Before you go" (CRM-22 fills it)
        days.append({
            "day_no": d["day_no"],
            "before_you_go": d["day_no"] == 0,
            "date": date.isoformat() if date else None,
            "tz": d["tz"],
            "utc_offset": rules.utc_offset(d["tz"], date or row["starts_on"]),
            "title": d["title"],
            "place": d["place"],
            "notes": notes,
            "blocks": blocks,
        })  # fmt: skip
    groups: dict[str, list[dict[str, Any]]] = {c: [] for c in CONFIRM_ORDER}
    for bid, oid in choice.items():
        o = options.get(oid)
        if o is not None:
            groups[confirm_class(o)].append({"title": o["title"], "choose_later": bid in later})
    t = totals(s, row, picks)
    aa_ids = aa_chosen(choice, options)
    return {
        "kind": "builder",
        "price_visibility": row["price_visibility"],
        "rooms": builder._jsonb(row["room_config"]) or [],
        "days": days,
        "groups": [{"confirm_class": c, "items": groups[c]} for c in CONFIRM_ORDER if groups[c]],
        "totals": public_totals(t),
        "selection_hash": selection_hash(choice, later),
        "hold": hold_state(row, aa_ids, now),
    }


def choice_diff(s: dict[str, Any], pick_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """R19: every block where the client's choice is not the default, or left for later (titles and the sell
    difference: what the agent needs, no personal data)."""
    state = effective(s, pick_rows)
    options = builder._option_map(s)
    defaults = builder._defaults(s)
    days = {d["proposal_day_id"]: d["day_no"] for d in s["days"]}
    by_block = {str(p["option_block_id"]): p for p in pick_rows}
    out = []
    for b in s["blocks"]:
        bid = str(b["option_block_id"])
        chosen, default = state["choice"].get(bid), defaults.get(bid)
        if chosen is None or (chosen == default and bid not in state["later"] and bid not in state["dropped"]):
            continue
        o, d = options.get(chosen), options.get(default or "")
        out.append({
            "day_no": days.get(b["proposal_day_id"]),
            "block": b["title"],
            "default": d["title"] if d else None,
            "chosen": o["title"] if o else None,
            "sell_delta": _money(Decimal(o["sell_amount"]) - Decimal(d["sell_amount"])) if o and d else None,
            "choose_later": bid in state["later"],
            "fell_back": bid in state["dropped"],
            "chosen_by": by_block[bid]["chosen_by"] if bid in by_block else None,
        })  # fmt: skip
    return out


def version_notice(prev: dict[str, Any], cur: dict[str, Any], prev_total: Any, cur_total: Any,
                   carried: int) -> dict[str, Any]:  # fmt: skip
    """The bundled notice of a new version on the same link: what changed since the version the client saw (titles
    only: no per-line price, §2.9 rule 3), the total before and after, and how many of their picks carried over."""
    changes: list[dict[str, Any]] = []
    prev_opts = {str(o["option_id"]): o for o in prev["options"]}
    sources = {str(o["source_option_id"]) for o in cur["options"] if o["source_option_id"]}
    for o in cur["options"]:
        before = prev_opts.get(str(o["source_option_id"])) if o["source_option_id"] else None
        if before is None:
            changes.append({"kind": "added", "title": o["title"]})
        elif (Decimal(before["sell_amount"]) != Decimal(o["sell_amount"]) or before["title"] != o["title"]
              or before["pay_on_arrival"] != o["pay_on_arrival"]):  # fmt: skip
            changes.append({"kind": "changed", "title": o["title"]})
    for oid, o in prev_opts.items():
        if oid not in sources:
            changes.append({"kind": "removed", "title": o["title"]})
    prev_defaults = {str(b["option_block_id"]): b["default_option_id"] for b in prev["blocks"]}
    for b in cur["blocks"]:
        if b["source_block_id"] is None or b["default_option_id"] is None:
            continue
        default = next((o for o in cur["options"] if o["option_id"] == b["default_option_id"]), None)
        before = prev_defaults.get(str(b["source_block_id"]))
        if default is not None and str(default["source_option_id"]) != str(before):
            changes.append({"kind": "default", "block": b["title"], "title": default["title"]})
    if len(prev["days"]) != len(cur["days"]):
        changes.append({"kind": "days", "from": len(prev["days"]), "to": len(cur["days"])})
    return {
        "changes": changes[:MAX_NOTICE],
        "more": max(0, len(changes) - MAX_NOTICE),
        "total_from": _money(prev_total),
        "total_to": _money(cur_total),
        "picks_carried": carried,
    }
