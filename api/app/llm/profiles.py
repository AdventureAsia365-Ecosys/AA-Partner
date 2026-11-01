"""CRM AI stage profiles (AA-964, S263 gateway plan F2; root ADR 0009 decisions 4 and 5, root ADR 0008a §6).

A stage profile is the CRM's own control plane for one AI stage, in schema ``crm`` (crm-037), never ``shared.llm_*``:

- ``crm.llm_stage_profile``: role, ``is_active`` (the stage kill switch), ``max_attempts`` (1..6), ``route_deadline_s``
  and ``degrade_mode`` (``manual_reply_needed``);
- ``crm.llm_stage_route``: the route in order (position 0 = primary): the Bedrock model id, its family (the maker) and
  the catalog prices per million tokens (both or neither), each entry switchable on its own.

:class:`DbProfileSource` is the CRM's ``ConfigSource`` of the shared core (``app.aa_llm.ports``): one SELECT in the
call's own transaction (platform tables, no tenant rows; ``crm_app`` may only read them). ``load_route`` returns None
for a stage without a profile and raises when the tables cannot be read; the gateway fails closed on both.
:func:`fixed_profile` is the one-attempt profile of a gateway built without a source (tests, ``stub_gateway``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.tenancy import AnyContext

MAX_ATTEMPTS = 6  # root ADR 0009 §5: at most six attempts per chain
DEFAULT_DEADLINE_S = 30
DEGRADE_MANUAL = "manual_reply_needed"

PROFILE_SQL = """
SELECT p.stage, p.role, p.is_active, p.max_attempts, p.route_deadline_s, p.degrade_mode,
       r.position, r.model, r.family, r.price_in_per_mtok, r.price_out_per_mtok
  FROM crm.llm_stage_profile p
  LEFT JOIN crm.llm_stage_route r ON r.stage = p.stage AND r.is_enabled
 WHERE p.stage = $1
 ORDER BY r.position
"""

# Bedrock inference profile prefixes (geography) before the maker: us.anthropic.claude-... -> anthropic.
_GEO_PREFIXES = frozenset({"us", "eu", "apac", "global", "us-gov", "ca", "jp", "au"})
_FAMILY = re.compile(r"^[a-z][a-z0-9_.-]{0,40}$")


def family_of(model_id: str | None) -> str | None:
    """The model family = its maker (root ADR 0009 §3) from a Bedrock wire id: ``us.anthropic.claude-haiku-...`` and
    ``anthropic.claude-...`` -> ``anthropic``, ``amazon.nova-lite-v1:0`` -> ``amazon``. None when the id names no
    maker (a bare model name)."""
    if not model_id:
        return None
    parts = model_id.rsplit("/", 1)[-1].lower().split(".")
    if len(parts) >= 3 and parts[0] in _GEO_PREFIXES:
        parts = parts[1:]
    if len(parts) < 2:
        return None
    maker = parts[0]
    return maker if _FAMILY.match(maker) else None


@dataclass(frozen=True)
class RouteEntry:
    """One step of a stage's route: a Bedrock model, its family and its catalog prices (USD per million tokens)."""

    position: int
    model: str
    family: str | None = None
    price_in_per_mtok: Decimal | None = None
    price_out_per_mtok: Decimal | None = None

    @property
    def catalog_prices(self) -> tuple[Decimal, Decimal] | None:
        if self.price_in_per_mtok is None or self.price_out_per_mtok is None:
            return None
        return self.price_in_per_mtok, self.price_out_per_mtok


@dataclass(frozen=True)
class StageProfile:
    stage: str
    role: str
    is_active: bool
    max_attempts: int
    route_deadline_s: int
    route: tuple[RouteEntry, ...]
    degrade_mode: str = DEGRADE_MANUAL

    @property
    def attempts(self) -> tuple[RouteEntry, ...]:
        """The route entries one request may try: the enabled ones in order, at most ``max_attempts`` (<= 6)."""
        return self.route[: max(1, min(self.max_attempts, MAX_ATTEMPTS))]


def profile_from_rows(rows: list[Any]) -> StageProfile | None:
    """The profile of ``PROFILE_SQL``'s rows (one per enabled route entry; one row with NULL route columns when the
    stage has none), None when there is no row."""
    if not rows:
        return None
    head = rows[0]
    route = tuple(
        RouteEntry(
            position=int(r["position"]),
            model=r["model"],
            family=r["family"] or family_of(r["model"]),
            price_in_per_mtok=r["price_in_per_mtok"],
            price_out_per_mtok=r["price_out_per_mtok"],
        )
        for r in rows
        if r["model"] is not None
    )
    return StageProfile(
        stage=head["stage"],
        role=head["role"],
        is_active=bool(head["is_active"]),
        max_attempts=int(head["max_attempts"]),
        route_deadline_s=int(head["route_deadline_s"]),
        route=route,
        degrade_mode=head["degrade_mode"] or DEGRADE_MANUAL,
    )


class DbProfileSource:
    """The stage profiles in ``crm`` (crm-037), read in the caller's context (``app.aa_llm.ports.ConfigSource``)."""

    async def load_route(self, db: AnyContext, stage: str) -> StageProfile | None:
        return profile_from_rows(list(await db.conn.fetch(PROFILE_SQL, stage)))


def fixed_profile(stage: str, role: str, model: str, family: str | None) -> StageProfile:
    """The profile of a gateway without a source: active, one attempt of its own provider's model."""
    return StageProfile(
        stage=stage,
        role=role,
        is_active=True,
        max_attempts=1,
        route_deadline_s=DEFAULT_DEADLINE_S,
        route=(RouteEntry(position=0, model=model, family=family),),
    )
