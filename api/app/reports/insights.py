"""AA insights (AA-966, card CRM-34; spec module 17, design v3 S-03, Q2 3.12): cross-agency totals with no agency in
them.

The facts come in two grains, read per agency as ``crm_insights`` by :func:`app.ops.console.platform_insights`:

- the **destination** dimension reads ``crm.v_insight_fact`` (crm-006): one row per trip AND destination, so a trip
  counts its party once in each of its destinations;
- every other dimension reads ``crm.v_insight_trip_fact`` (crm-031): trips per agency, month and band combination
  (``trip_count``), so a trip counts its party exactly once. :func:`aggregate` refuses per-destination facts for those
  dimensions: a trip with four destinations would otherwise add its party four times and could cross the threshold on
  duplicates alone.

This module only adds them up:

- a **cell** is one value of one dimension (a destination, a budget band, ...) over ONE calendar quarter
  (:func:`quarter_months`; the API accepts no other period, so two answers never overlap or nest and cannot be
  subtracted from each other);
- ``traveller_count`` = the party sizes summed (times ``trip_count``), ``agency_count`` = the distinct agencies;
- a cell is **published** only when ``traveller_count >= 10`` **and** ``agency_count >= 3`` (card R, "k >= 10 and >= 3
  agencies") **and** no single agency holds more than 70 % of its travellers (a dominance rule on top of card R, so a
  published cell never mostly describes one agency); every other cell is **suppressed**: neither its value nor its
  counts leave this module, only how many cells were held back (no total is returned either, so within one answer a
  suppressed cell cannot be found by subtraction);
- an agency that opted out (``crm.tenant_entitlement.insights_opt_out``) never contributes: the platform reader skips
  it before reading, and :func:`aggregate` drops any fact of an agency in ``opted_out`` as a second lock;
- no output carries an agency id (``tenant_id`` is not a metric dimension, delivery plan CRM-34).

What this does NOT guarantee: the cells are computed live, so two reads of the same quarter differ by whatever changed
in between (a new trip, an agency's opt-out set or lifted, an agency locked). A platform caller who changes one agency's
opt-out between two reads can learn that agency's contribution to the cells published both times. The opt-out is a
platform write on the owner's written request (audited, ``ticket_ref``), and the read is for AA staff only; a frozen
per-quarter table (``crm.insight_aggregate`` of the data lane) is the follow-up that removes this.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

K_MIN_TRAVELLERS = 10
MIN_AGENCIES = 3
MAX_AGENCY_SHARE = 0.7  # dominance: no published cell has more than 70 % of its travellers from one agency
DIMENSIONS = ("destination", "budget_band", "age_band", "party_size", "stage_reached", "lost_reason_code")
QUARTER_RE = re.compile(r"^(20[0-9]{2})-Q([1-4])$")
TRIP = "trip"  # crm.v_insight_trip_fact: one row stands for trip_count whole trips
TRIP_DESTINATION = "trip_destination"  # crm.v_insight_fact: one row per trip and destination


@dataclass(frozen=True)
class Fact:
    tenant_id: uuid.UUID
    month: dt.date | None
    destination: str | None
    budget_band: str | None
    age_band: str | None
    party_size: int
    stage_reached: str | None
    lost_reason_code: str | None
    trip_count: int = 1
    grain: str = TRIP


def grain_for(dimension: str) -> str:
    """The fact grain a dimension must be counted on (see the module docstring)."""
    return TRIP_DESTINATION if dimension == "destination" else TRIP


def quarter_months(quarter: str) -> tuple[dt.date, dt.date]:
    """``YYYY-Qn`` -> (first day of its first month, first day of its last month); ValueError otherwise."""
    m = QUARTER_RE.match(quarter or "")
    if not m:
        raise ValueError("a quarter is YYYY-Q1 to YYYY-Q4")
    year, q = int(m.group(1)), int(m.group(2))
    return dt.date(year, 3 * q - 2, 1), dt.date(year, 3 * q, 1)


def party_band(size: int) -> str:
    if size <= 1:
        return "1"
    if size == 2:
        return "2"
    if size <= 4:
        return "3_4"
    return "5_plus"


def bucket_of(fact: Fact, dimension: str) -> str | None:
    if dimension == "party_size":
        return party_band(fact.party_size)
    value = getattr(fact, dimension)
    return value if value else None


def aggregate(
    facts: Iterable[Fact],
    dimension: str,
    *,
    opted_out: frozenset[uuid.UUID] = frozenset(),
    k_min: int = K_MIN_TRAVELLERS,
    min_agencies: int = MIN_AGENCIES,
    max_share: float = MAX_AGENCY_SHARE,
) -> dict[str, Any]:
    """Published cells (largest first) and the number of suppressed ones. A fact without a value for the dimension
    (no destination, a trip not lost for ``lost_reason_code``) is not a cell. Facts must be of the dimension's grain
    (:func:`grain_for`): per-destination facts would count a trip once per destination."""
    if dimension not in DIMENSIONS:
        raise ValueError(f"unknown insight dimension {dimension!r}")
    want = grain_for(dimension)
    per_agency: dict[str, dict[uuid.UUID, int]] = {}
    for fact in facts:
        if fact.grain != want:
            raise ValueError(f"{dimension} counts {want} facts, got a {fact.grain} fact")
        if fact.tenant_id in opted_out:
            continue
        bucket = bucket_of(fact, dimension)
        if bucket is None:
            continue
        cell = per_agency.setdefault(bucket, {})
        cell[fact.tenant_id] = cell.get(fact.tenant_id, 0) + max(int(fact.party_size), 0) * max(int(fact.trip_count), 0)
    cells, suppressed = [], 0
    for bucket, agencies in per_agency.items():
        n, a = sum(agencies.values()), len(agencies)
        if n >= k_min and a >= min_agencies and max(agencies.values()) <= max_share * n:
            cells.append({"bucket": bucket, "traveller_count": n, "agency_count": a})
        else:
            suppressed += 1
    cells.sort(key=lambda c: (-c["traveller_count"], c["bucket"]))
    return {"cells": cells, "suppressed_cells": suppressed, "k_min": k_min, "agencies_min": min_agencies}
