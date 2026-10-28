"""AA-993 G4b end to end on PostgreSQL: the ``crm_dispatch`` worker with the real two transactions, the real mail
sender and a fake provider (crm-023 ``crm.dispatch_attempt``).

- A run cancelled after the provider returned (the deploy's shutdown cancel) sends once; the re-run holds the row
  ``send_uncertain`` and fails the message ``send_uncertain``; a later run skips it.
- A run cancelled before the sender was called closes its attempt ``not_sent``; the re-run sends.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import asyncpg
import pytest

from app.dispatch import worker as dispatch_worker
from app.mail.sender import MailSender
from app.mail.state import MailRuntime
from tests.integration.test_crm010_schema import _count, _ctx, _erase
from tests.integration.test_mail_db import _scalar, _send, _setup
from tests.unit.mail_fakes import FakeProvider

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class CancelledAfterSend(FakeProvider):
    """The provider accepted the message, then the job's task was cancelled (the deploy's shutdown) before the
    decision committed."""

    async def send(self, raw: bytes, secret: dict[str, Any], recipients: list[str]) -> Any:
        await super().send(raw, secret, recipients)
        raise asyncio.CancelledError


async def _attempts(pool: asyncpg.Pool, tenant: uuid.UUID, dispatch_id: str) -> list[str | None]:
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant)
        rows = await conn.fetch(
            "SELECT outcome FROM crm.dispatch_attempt WHERE dispatch_id = $1 ORDER BY started_at, created_at",
            uuid.UUID(dispatch_id),
        )
    return [r["outcome"] for r in rows]


async def test_cancelled_after_the_provider_returned_is_sent_once_and_held_uncertain(app_pool: asyncpg.Pool) -> None:
    d = await _setup(app_pool)
    provider = CancelledAfterSend()
    message_id, job = await _send(app_pool, d)
    dispatch = dispatch_worker.build_handler(MailSender(MailRuntime(keys=d["keys"], providers={"gmail": provider})))
    with pytest.raises(asyncio.CancelledError):
        await dispatch(app_pool, job)
    assert len(provider.sent) == 1
    # The second transaction rolled back: the row is still queued, the attempt open.
    assert await _attempts(app_pool, d["tenant"], job.payload["dispatch_id"]) == [None]
    # The worker released the job; the new task re-claims it (same attempt number).
    assert await dispatch(app_pool, job) == {
        "dispatch_id": job.payload["dispatch_id"],
        "status": "held",
        "reason_code": "send_uncertain",
    }
    assert len(provider.sent) == 1
    assert await _attempts(app_pool, d["tenant"], job.payload["dispatch_id"]) == ["send_uncertain"]
    row = await _scalar(
        app_pool,
        d["tenant"],
        "SELECT status || ':' || gate_result FROM crm.dispatch_queue WHERE dispatch_id = $1",
        uuid.UUID(job.payload["dispatch_id"]),
    )
    assert row == "held:send_uncertain"
    message = await _scalar(
        app_pool, d["tenant"], "SELECT status || ':' || failure_code FROM crm.message WHERE message_id = $1", message_id
    )
    assert message == "failed:send_uncertain"
    assert (await dispatch(app_pool, job))["status"] == "skipped" and len(provider.sent) == 1
    await _erase_agency(app_pool, d["tenant"])


async def _erase_agency(pool: asyncpg.Pool, tenant: uuid.UUID) -> None:
    """No send_uncertain row may outlive a test: test_crm_migrations re-applies crm-015, whose gate check (DROP + ADD)
    predates the code, and would fail on it."""
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant)
        await conn.execute("UPDATE crm.tenant SET status = 'offboarding'")
    await _erase(pool, tenant, "tenant", tenant)
    assert await _count(pool, tenant, "dispatch_queue") == 0


async def test_cancelled_before_the_sender_was_called_is_sent_by_the_re_run(
    app_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = await _setup(app_pool)
    provider = FakeProvider()
    _, job = await _send(app_pool, d)
    dispatch = dispatch_worker.build_handler(MailSender(MailRuntime(keys=d["keys"], providers={"gmail": provider})))
    real_gate = dispatch_worker.gate
    calls = {"n": 0}

    def gate_then_cancel(row: Any) -> str | None:
        calls["n"] += 1
        if calls["n"] == 2:  # the second transaction, after the attempt committed and before the sender
            raise asyncio.CancelledError
        return real_gate(row)

    monkeypatch.setattr(dispatch_worker, "gate", gate_then_cancel)
    with pytest.raises(asyncio.CancelledError):
        await dispatch(app_pool, job)
    assert provider.sent == []
    assert await _attempts(app_pool, d["tenant"], job.payload["dispatch_id"]) == ["not_sent"]
    assert (await dispatch(app_pool, job))["status"] == "sent"
    assert len(provider.sent) == 1
    assert await _attempts(app_pool, d["tenant"], job.payload["dispatch_id"]) == ["not_sent", "sent"]
