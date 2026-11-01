"""S263 alert diet (AA-1025): the ops-alert ledger row in crm.job as crm_app on PostgreSQL 15.

One ``succeeded`` row per alert key (a second insert returns nothing, so two API tasks never both publish), never
claimable, not counted as busy work, and removed again when the publish failed; the job sampler runs the checks end to
end with a recording SNS stub.
"""

from __future__ import annotations

import asyncpg
import pytest

from app.jobs import queue
from app.jobs.metrics import JobMetricsSampler
from app.observability import ops_alerts

pytestmark = pytest.mark.asyncio


async def _clean(admin: asyncpg.Connection) -> None:
    await admin.execute("DELETE FROM crm.job WHERE kind IN ($1, 'crm_s263_probe')", ops_alerts.OPS_ALERT_KIND)


async def test_one_row_per_key_never_claimed_and_forgettable(admin: asyncpg.Connection, app_pool: asyncpg.Pool) -> None:
    await _clean(admin)
    kind, key = ops_alerts.OPS_ALERT_KIND, "crm_ops_alert:breakglass_opened:s263-test"
    first = await queue.record_alert(app_pool, kind, key, {"kind": "breakglass_opened", "scope": "s263-test"})
    assert first is not None
    assert await queue.record_alert(app_pool, kind, key, {"kind": "breakglass_opened"}) is None
    row = await admin.fetchrow(
        "SELECT status AS st, finished_at IS NOT NULL AS done, created_by AS by FROM crm.job WHERE id = $1::uuid", first
    )
    assert (row["st"], row["done"], row["by"]) == ("succeeded", True, "ops_alerts")
    assert await queue.claim(app_pool, "w-s263", [kind], 60) is None
    await queue.forget_alert(app_pool, first)
    assert await queue.record_alert(app_pool, kind, key, {"kind": "breakglass_opened"}) is not None
    await _clean(admin)


async def test_sampler_mails_a_stuck_queue_once(admin: asyncpg.Connection, app_pool: asyncpg.Pool) -> None:
    await _clean(admin)
    await admin.execute(
        "INSERT INTO crm.job (kind, payload, created_by, run_after) VALUES ('crm_s263_probe', '{}', 's263-test',"
        " now() - interval '20 minutes')"
    )
    calls: list[dict] = []

    class Sns:
        def publish(self, **kw: object) -> dict:
            calls.append(kw)
            return {}

    async def provide() -> asyncpg.Pool:
        return app_pool

    ops_alerts.PENDING.drain()
    pub = ops_alerts.AlertPublisher("topic-placeholder", environment="test", client_factory=Sns)
    sampler = JobMetricsSampler(provide, publisher=pub)
    try:
        sample = await sampler.sample_once()
        await sampler.sample_once()
        assert sample is not None and sample.oldest_queued_age_s >= 1195
        assert [c["Subject"] for c in calls] == ["[AA-Partner test] Ops check: Job queue stuck"]
        n = await admin.fetchval("SELECT count(*) FROM crm.job WHERE kind = $1", ops_alerts.OPS_ALERT_KIND)
        assert n == 1
    finally:
        await _clean(admin)
