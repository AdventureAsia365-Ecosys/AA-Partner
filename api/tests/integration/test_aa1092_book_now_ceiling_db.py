"""AA-1092 residual (needs crm-062) on PostgreSQL: the agency's Book now price checks a minute.

- Settings > Online booking reads the column default (60) and the owner's new value; the value is audited by field.
- ``booking.price`` reads the agency's own value from ``tenant_settings`` and hands it to the per-agency quota; a
  refusal is 429 ``agency_rate_limited`` and Booking is never asked.
All names are invented.
"""

from __future__ import annotations

import uuid

import asyncpg
import pytest

from app.errors import RateLimited
from app.intake import booking, booking_settings
from app.tenancy import tenant_tx
from tests.aa_fakes import booking_client
from tests.integration.test_aa1072_book_now_api import (  # noqa: F401 — the autouse clean-up fixture runs here too
    TOUR,
    _leave_no_trace,
    _setup,
)
from tests.integration.test_client_proposal_api import PLAIN

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

BODY = {"tour": TOUR, "departure_id": "d1", "adults": 2, "child_ages": [7]}


async def test_the_owner_sets_the_ceiling_and_the_price_check_reads_it(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"]) as ctx:
        assert (await booking_settings.read(ctx, PLAIN))["price_checks_per_minute"] == 60
        out = await booking_settings.update(ctx, PLAIN, None, 12)
    assert out["price_checks_per_minute"] == 12 and out["enabled"] is True
    asked: list[tuple[uuid.UUID, int]] = []

    def quota(tenant_id: uuid.UUID, ceiling: int) -> bool:
        asked.append((tenant_id, ceiling))
        return len(asked) <= 1

    first = await booking.price(app_pool, s["cache"], booking_client(s["fake"]), s["key"], BODY, agency_quota=quota)
    assert first["total"] == "12000000.00"
    s["fake"].down = True  # the refused check must not reach Booking
    with pytest.raises(RateLimited) as err:
        await booking.price(app_pool, s["cache"], booking_client(s["fake"]), s["key"], BODY, agency_quota=quota)
    assert (err.value.code, err.value.retry_after_s) == ("agency_rate_limited", 60)
    assert asked == [(s["tenant"], 12), (s["tenant"], 12)]
