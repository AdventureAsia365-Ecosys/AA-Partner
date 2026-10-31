"""AA insights (AA-966, card CRM-34; spec module 17, design v3 S-03, Q2 3.12): cross-agency totals with no agency in
them.

The facts are rows of ``crm.v_insight_fact`` (crm-006: one row per trip and destination, bands and codes only, no
trip id, no token), read per agency as ``crm_insights`` by :func:`app.ops.console.platform_insights`. This module only
adds them up:

- a **cell** is one value of one dimension (a destination, a budget band, ...) over the requested months;
- ``traveller_count`` = the party sizes summed, ``agency_count`` = the distinct agencies;
- a cell is **published** only when ``traveller_count >= 10`` **and** ``agency_count >= 3`` (card R, "k >= 10 and >= 3
  agencies"); every other cell is **suppressed**: neither its value nor its counts leave this module, only how many
  cells were held back (no total is returned either, so a suppressed cell cannot be found by subtraction);
- an agency that opted out (``crm.tenant_entitlement.insights_opt_out``) never contributes: the platform reader skips
  it before reading, and :func:`aggregate` drops any fact of an agency in ``opted_out`` as a second lock;
- no output carries an agency id (``tenant_id`` is not a metric dimension, delivery plan CRM-34).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

K_MIN_TRAVELLERS = 10
MIN_AGENCIES = 3
DIMENSIONS = ("destination", "budget_band", "age_band", "party_size", "stage_reached", "lost_reason_code")


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
) -> dict[str, Any]:
    """Published cells (largest first) and the number of suppressed ones. A fact without a value for the dimension
    (no destination, a trip not lost for ``lost_reason_code``) is not a cell."""
    if dimension not in DIMENSIONS:
        raise ValueError(f"unknown insight dimension {dimension!r}")
    travellers: dict[str, int] = {}
    agencies: dict[str, set[uuid.UUID]] = {}
    for fact in facts:
        if fact.tenant_id in opted_out:
            continue
        bucket = bucket_of(fact, dimension)
        if bucket is None:
            continue
        travellers[bucket] = travellers.get(bucket, 0) + max(int(fact.party_size), 0)
        agencies.setdefault(bucket, set()).add(fact.tenant_id)
    cells, suppressed = [], 0
    for bucket in travellers:
        n, a = travellers[bucket], len(agencies[bucket])
        if n >= k_min and a >= min_agencies:
            cells.append({"bucket": bucket, "traveller_count": n, "agency_count": a})
        else:
            suppressed += 1
    cells.sort(key=lambda c: (-c["traveller_count"], c["bucket"]))
    return {"cells": cells, "suppressed_cells": suppressed, "k_min": k_min, "agencies_min": min_agencies}
