"""``/crm/ops/v1``: the L1 platform console contract ``crm.ops.v1`` (route class OPS; root ``docs/contracts``).

WORKFORCE ``cis-admin`` tokens only (the CIS admin BFF forwards the staff token); group ``cis-admin`` reads,
``crm-platform-admin`` writes (``require_group``). AA-936 adds the two reads CIS needs to see the CRM (card CRM-08
"CIS AppInfo("crm") + jobs tab", D12):

- ``GET /crm/ops/v1/jobs/summary``: ``crm.job`` per kind (queued, running, failed in 24 h, oldest queued age);
- ``GET /crm/ops/v1/llm/usage?from=&to=``: the LLM usage rollup per day x stage x model (no text, no agency split).

Counts only: no agency id, no trip, no personal data. The agency operations arrive with CRM-13a (AA-941).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated, Any

from fastapi import HTTPException, Query, Request

from app.auth.identity_config import OPS
from app.auth.route_guard import class_router
from app.db import request_pool
from app.jobs import queue
from app.llm.usage import MAX_DAYS, llm_usage

router = class_router(OPS, tags=["ops"])


@router.get("/jobs/summary")
async def jobs_summary(request: Request) -> dict[str, Any]:
    pool = await request_pool(request)
    return {"generated_at": datetime.now(UTC).isoformat(), "kinds": await queue.kind_summary(pool)}


@router.get("/llm/usage")
async def get_llm_usage(
    request: Request,
    from_: Annotated[date, Query(alias="from")],
    to: Annotated[date, Query()],
) -> dict[str, Any]:
    if to < from_ or (to - from_).days > MAX_DAYS:
        raise HTTPException(status_code=422, detail="invalid_range")
    pool = await request_pool(request)
    return {"from": from_.isoformat(), "to": to.isoformat(), "rows": await llm_usage(pool, from_, to)}
