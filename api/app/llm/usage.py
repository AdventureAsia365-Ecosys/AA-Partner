"""Platform reads for ``/crm/ops/v1`` (crm.ops.v1): the LLM usage rollup (``crm.llm_usage_daily``, crm-005).

A platform table (no tenant column, no personal data, summed over every agency), so it is read on a pool connection
outside any tenant context; listed in the DB lint's platform modules.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import asyncpg

MAX_DAYS = 92

USAGE_SQL = """
SELECT day, stage, model, calls, failed_calls, input_tokens, output_tokens, cost_usd
  FROM crm.llm_usage_daily WHERE day >= $1 AND day <= $2 ORDER BY day, stage, model
"""


async def llm_usage(pool: asyncpg.Pool, start: date, end: date) -> list[dict[str, Any]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(USAGE_SQL, start, end)
    return [
        {
            "day": r["day"].isoformat(),
            "stage": r["stage"],
            "model": r["model"],
            "calls": r["calls"],
            "failed_calls": r["failed_calls"],
            "input_tokens": int(r["input_tokens"]),
            "output_tokens": int(r["output_tokens"]),
            "cost_usd": f"{r['cost_usd']:.6f}",
        }
        for r in rows
    ]
