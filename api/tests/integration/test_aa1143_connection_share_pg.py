"""AA-1143 (decision G3, crm-079) on PostgreSQL as crm_app: the CIS copy of the CRM's connection shares is written
whole (a connection left out is dropped), read back for crm.ops.v1, and wins over the setting in the gateway's read.
Invented connection keys only; every row is removed at the end."""

from __future__ import annotations

from decimal import Decimal

import asyncpg
import pytest

from app.llm import connection_share

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

REASON = "zz test: the CIS budget save reason"


async def test_the_copy_round_trips_and_wins(app_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(connection_share, "cap_for", lambda key: Decimal("5") if key == "zz-b" else None)
    try:
        first = connection_share.check_copy(
            [{"connection_key": "zz-a", "share_usd": "8"}, {"connection_key": "zz-c", "share_usd": "0"}]
        )
        out = await connection_share.write_copy(app_pool, first, reason=REASON, actor="platform:zz")
        assert out["shares"] == [
            {"connection_key": "zz-a", "share_usd": "8.00"},
            {"connection_key": "zz-c", "share_usd": "0.00"},
        ]
        out = await connection_share.write_copy(app_pool, {"zz-a": Decimal("9.00")}, reason=REASON, actor="platform:zz")
        assert out["shares"] == [{"connection_key": "zz-a", "share_usd": "9.00"}] and out["updated_at"]
        assert (await connection_share.read_copy(app_pool))["shares"] == out["shares"]
        async with app_pool.acquire() as conn:
            assert await connection_share.share_for(conn, "zz-a") == Decimal("9.00")
            assert await connection_share.share_for(conn, "zz-b") == Decimal("5")  # not in the copy: the setting
            assert await connection_share.share_for(conn, "zz-c") is None
    finally:
        await connection_share.write_copy(app_pool, {}, reason=REASON, actor="platform:zz")
