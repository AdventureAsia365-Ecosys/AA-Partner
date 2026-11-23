"""``crm_tp_price_publish`` (AA-1152; S285 decision B2 option C, contract ``tripplanner.agency_prices.v1``): the
agency's own "from" prices on its TripPlanner pages.

The CRM owns the agency's pricing (its markup per AA product type, its rounding step, its show-prices switch), so the
CRM computes every number and hands TripPlanner the result; TripPlanner never sees AA's B2B price, the markup or a
supplier (root ADR 0019 b5 as amended by AA-1152, ADR 0008a: no AA app reads ``crm``, hence HTTP).

One price (:func:`app.proposals.rounding.from_price`, the function behind every suggestion of the builder): AA's B2B
"from" price per traveller x (1 + the agency's markup of that product type), else AA's RRP, rounded up to the agency's
step of its base currency. No date rule (the page shows the lowest, "from"):

- a tour: the lowest ``price_per_person`` of the agency's current tariff (booking.b2b.v1 ``GET /tariff``) among the
  bands that hold 2 travellers (else among all bands), and the lowest RRP the same way;
- an add-on: ``GET /add-ons`` for 2 adults (each offer once, its first sellable day).

A tour or add-on AA cannot price gets no row (never a guess). With the switch off the lists are empty and TripPlanner
hides every price of the agency at once.

When: once per hour slot for every agency whose TripPlanner leads come to this CRM (:func:`app.tripplanner.leads.
routed_here`: the agency has a planner and the CRM add-on), and once at once for one agency after its owner changes the
markups, the rounding steps or the switch (:func:`enqueue_agency`). A run logs one summary line (counts and error
classes only) and never fails for one agency; Booking or TripPlanner down is counted and retried by the next slot.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections import Counter
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import asyncpg

from app.audit_export import registry
from app.booking import addons as booking_addons
from app.booking import contract
from app.booking.client import BookingClient, BookingRefusal, BookingUnavailable
from app.errors import Forbidden, NotFound
from app.jobs import queue
from app.jobs.kinds import TP_PRICE_PUBLISH
from app.jobs.queue import Job
from app.proposals import retail, rounding
from app.tenancy import SystemContext, system_tx
from app.tripplanner import leads, planner

log = logging.getLogger(__name__)

SLOT_SECONDS = 3600  # technical-constant: the hourly slot of the sweep (AA prices move with tariffs and FX)
MAX_ATTEMPTS = 1  # technical-constant: the next slot is the retry
HISTORY_DAYS = 7  # technical-constant: finished rows of this kind kept for the job page
SCHEDULER = "scheduler:crm_tp_price_publish"
ACTOR = "job:crm_tp_price_publish"
TWO = 2  # technical-constant: the "from" party of AA's prices (price.from.v1: per person for 2 travellers)


def _covers_two(item: contract.TariffItem) -> bool:
    return item.pax_min <= TWO and (item.pax_max is None or item.pax_max >= TWO)


def tour_bases(tariff: contract.Tariff) -> dict[str, tuple[Decimal | None, Decimal | None]]:
    """``{source_id: (lowest B2B per person, lowest RRP per person)}`` of the tariff, bands holding 2 first."""
    out: dict[str, tuple[Decimal | None, Decimal | None]] = {}
    by_tour: dict[str, list[contract.TariffItem]] = {}
    for item in tariff.items:
        by_tour.setdefault(item.source_id, []).append(item)
    for source_id, items in by_tour.items():
        pick = [i for i in items if _covers_two(i)] or items
        rrps = [i.rrp_per_person for i in pick if i.rrp_per_person is not None and i.rrp_currency == tariff.currency]
        out[source_id] = (min(i.price_per_person for i in pick), min(rrps) if rrps else None)
    return out


def _text(value: Decimal, currency: str) -> str:
    whole = currency in ("VND", "JPY", "KRW") or value == value.to_integral_value()
    return format(value.quantize(rounding.ONE), "f") if whole else format(value.quantize(rounding.CENT), "f")


def price_list(
    settings: rounding.PriceSettings,
    markups: dict[str, Decimal],
    tours: dict[str, tuple[Decimal | None, Decimal | None]],
    addons: list[booking_addons.Addon],
    now: dt.datetime,
) -> dict[str, Any]:
    """The contract ``AgencyPrices`` body (no B2B price, no markup, no RRP in it: the final numbers only)."""
    currency = settings.base_currency
    step = settings.step(currency)
    body: dict[str, Any] = {"show_prices": settings.show_planner_prices, "currency": currency,
                            "computed_at": now.isoformat(), "tours": [], "addons": []}  # fmt: skip
    if not settings.show_planner_prices:
        return body
    for source_id, (b2b, rrp) in sorted(tours.items()):
        price = rounding.from_price(b2b, rrp, markups.get("trip_plan"), step)
        if price is not None:
            body["tours"].append({"source_id": source_id, "from_per_person": _text(price[0], currency)})
    for addon in sorted(addons, key=lambda a: a.component_id):
        price = rounding.from_price(addon.b2b_per_person, addon.rrp_per_person, markups.get(addon.kind), step)
        if price is not None:
            body["addons"].append({"component_id": addon.component_id, "from_per_person": _text(price[0], currency)})
    return body


async def _agency_inputs(ctx: SystemContext) -> tuple[rounding.PriceSettings, dict[str, Decimal]] | None:
    """The agency's settings and markups, or None when its TripPlanner leads do not come to this CRM."""
    if not await leads.routed_here(ctx):
        return None
    return await rounding.settings(ctx), await retail.markups(ctx)


async def publish_agency(
    pool: asyncpg.Pool, client: BookingClient, config: Any, tenant_id: uuid.UUID, now: dt.datetime
) -> str:
    """One agency: ``published`` or ``skipped_not_routed``. Raises the Booking / TripPlanner errors."""
    async with system_tx(pool, tenant_id, actor=ACTOR) as ctx:
        inputs = await _agency_inputs(ctx)
    if inputs is None:
        return "skipped_not_routed"
    settings, markups = inputs
    tours: dict[str, tuple[Decimal | None, Decimal | None]] = {}
    addons: list[booking_addons.Addon] = []
    if settings.show_planner_prices:
        currency = settings.base_currency
        tours = tour_bases(contract.tariff(await client.tariff(tenant_id, currency=currency), currency))
        addons = await booking_addons.load(client, tenant_id, currency=currency, pax=TWO)
    body = price_list(settings, markups, tours, addons, now)
    await planner._call(config, "PUT", tenant_id, "prices", body)  # noqa: SLF001 — the planner module's M2M call
    return "published"


class PricePublish:
    """The ``crm_tp_price_publish`` handler (``config`` / ``client`` None = not configured). A row's payload may name
    one agency (``tenant_id``, an owner's change); a slot row runs every agency."""

    def __init__(
        self,
        config: Any,
        client: BookingClient | None,
        now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    ) -> None:
        self._config = config
        self._client = client
        self._now = now

    async def __call__(self, pool: asyncpg.Pool, job: Job) -> dict[str, Any]:
        if self._config is None or self._client is None:
            log.info("tp_price_publish not_configured")
            return {"status": "not_configured"}
        one = (job.payload or {}).get("tenant_id")
        tenants = [uuid.UUID(one)] if one else [a.tenant_id for a in await registry.agencies(pool)]
        counts: Counter[str] = Counter()
        errors: Counter[str] = Counter()
        for tenant_id in tenants:
            try:
                counts[await publish_agency(pool, self._client, self._config, tenant_id, self._now())] += 1
            except (Forbidden, NotFound):
                counts["skipped_agency"] += 1
            except planner.PlannerRefused as exc:
                counts[f"refused_{exc.code}"] += 1
            except (planner.PlannerUnavailable, BookingUnavailable) as exc:
                errors[getattr(exc, "error_class", None) or str(exc) or type(exc).__name__] += 1
            except (BookingRefusal, contract.ContractError) as exc:
                errors[getattr(exc, "code", None) or type(exc).__name__] += 1
            except Exception as exc:  # noqa: BLE001 — one agency's error must not hold every later agency
                errors[type(exc).__name__] += 1
                log.error("tp_price_publish_failed tenant_id=%s error_class=%s", tenant_id, type(exc).__name__)
        log.info("tp_price_publish_summary agencies=%d %s errors=%d", len(tenants),
                 " ".join(f"{k}={v}" for k, v in sorted(counts.items())), sum(errors.values()))  # fmt: skip
        return {
            "status": "ok" if not errors else "partial",
            "agency_count": len(tenants),
            **{f"{k}_count": v for k, v in sorted(counts.items())},
            "error_count": sum(errors.values()),
            "reason_code": errors.most_common(1)[0][0][:64] if errors else None,
        }


async def enqueue_agency(conn: Any, tenant_id: uuid.UUID) -> None:
    """One run for one agency now (an owner changed its markups, rounding steps or switch), in the caller's
    transaction; a second change before it runs is the same row."""
    stamp = int(dt.datetime.now(dt.UTC).timestamp()) // 60
    await queue.enqueue(conn, TP_PRICE_PUBLISH, {"tenant_id": str(tenant_id)},
                        idempotency_key=f"{TP_PRICE_PUBLISH}:{tenant_id}:{stamp}", max_attempts=MAX_ATTEMPTS,
                        created_by=SCHEDULER)  # fmt: skip


def schedule_key(now: dt.datetime) -> str:
    return f"{TP_PRICE_PUBLISH}:slot:{int(now.timestamp()) // SLOT_SECONDS}"


class PricePublishSchedule:
    """Worker maintenance hook: one ``crm_tp_price_publish`` run per hour slot, due now, never ahead."""

    def __init__(self, now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC)) -> None:
        self._now = now
        self._done_slot: str | None = None

    async def __call__(self, pool: asyncpg.Pool) -> str | None:
        key = schedule_key(self._now())
        if self._done_slot == key:
            return None
        job_id = await queue.enqueue(pool, TP_PRICE_PUBLISH, {"purpose": "slot_schedule"}, idempotency_key=key,
                                     max_attempts=MAX_ATTEMPTS, created_by=SCHEDULER)  # fmt: skip
        await queue.prune(pool, TP_PRICE_PUBLISH, HISTORY_DAYS)
        self._done_slot = key
        return job_id
