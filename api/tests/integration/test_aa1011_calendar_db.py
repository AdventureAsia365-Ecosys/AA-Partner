"""AA-1011 part 3 on PostgreSQL with the RDS role shape and real field encryption (crm-089 ``crm.calendar_connection``),
with a fake calendar provider (no network):

- the calendar token is stored sealed in the member's own ``crm.integration_credential`` row (never plaintext) and only
  the push opens it; the push writes the member's task into the calendar and records its counters;
- no token and no address in any log line of connect, push or disconnect;
- a disconnect queues the clean-up, which removes the pushed events and deletes the credential (the connection row
  goes with it), never revoking at the provider, and leaves the member's Gmail mailbox credential as it was;
- tenant isolation: another agency sees neither the connection nor its credential and cannot disconnect it.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from app.calendar import connections, oauth_sync, push
from app.errors import NotFound
from app.jobs.queue import Job
from app.mail import mailbox
from app.permissions import Action
from app.tenancy import tenant_tx
from app.vault import Vault
from tests.integration.test_crm005_intake import _keys
from tests.integration.test_crm010_lifecycle import _inquiry
from tests.unit.test_aa1011_calendar_push import FakeCalendar

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_TENANTS: set[uuid.UUID] = set()
REFRESH = "rt-integration-token-0042"
ADDRESS = "minh.calendar@senvang.example"


@pytest_asyncio.fixture(autouse=True)
async def _no_calendar_credential_left(app_pool: asyncpg.Pool) -> Any:
    """Delete the calendar credentials (and so the connections) these tests made: test_reapply_is_a_no_op re-runs
    crm-057, whose provider CHECK does not list the calendar providers, and fails on a row left behind."""
    yield
    for tenant in list(_TENANTS):
        async with app_pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
            await conn.execute("SELECT set_config('app.actor', 'job:crm_calendar_push', true)")
            await conn.execute(
                "DELETE FROM crm.integration_credential "
                "WHERE provider IN ('google_calendar', 'microsoft_calendar', 'google')"
            )
    _TENANTS.clear()


async def _setup(pool: asyncpg.Pool) -> dict[str, Any]:
    keys = _keys()
    d = await _inquiry(pool, keys, "owner")
    _TENANTS.add(d["tenant"])
    d["keys"] = keys
    async with tenant_tx(pool, d["owner"], d["tenant"], action=Action.CALENDAR_FEED_MANAGE) as ctx:
        out = await connections.connect(
            ctx, Vault(keys, ctx), provider="google", scope="mine",
            secret={"refresh_token": REFRESH, "address": ADDRESS},
        )  # fmt: skip
    d["connection"] = uuid.UUID(out["connection"]["calendar_connection_id"])
    return d


async def _rows(pool: asyncpg.Pool, tenant: uuid.UUID, sql: str, *args: Any) -> list[asyncpg.Record]:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
        return list(await conn.fetch(sql, *args))


async def _jobs(pool: asyncpg.Pool, connection: uuid.UUID) -> list[asyncpg.Record]:
    return list(await pool.fetch(
        "SELECT payload, max_attempts FROM crm.job WHERE kind = 'crm_calendar_push' "
        "AND payload->>'calendar_connection_id' = $1 ORDER BY created_at, id", str(connection),
    ))  # fmt: skip


def _runtime(d: dict[str, Any], client: FakeCalendar) -> push.Runtime:
    return push.Runtime(keys=d["keys"], clients={"google": client}, web_origin="https://partner.test")


async def test_the_token_is_sealed_and_the_push_writes_the_task(
    app_pool: asyncpg.Pool, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    d = await _setup(app_pool)
    (cred,) = await _rows(
        app_pool, d["tenant"],
        "SELECT integration_credential_id AS id, provider, member_id, secret_enc FROM crm.integration_credential "
        "WHERE provider = 'google_calendar'",
    )  # fmt: skip
    assert cred["member_id"] == d["owner_id"]
    assert REFRESH.encode() not in bytes(cred["secret_enc"]) and ADDRESS.encode() not in bytes(cred["secret_enc"])
    (row,) = await _rows(app_pool, d["tenant"], "SELECT address_enc, credential_id FROM crm.calendar_connection")
    assert ADDRESS.encode() not in bytes(row["address_enc"]) and row["credential_id"] == cred["id"]
    async with tenant_tx(app_pool, d["owner"], d["tenant"], action=Action.CALENDAR_FEED_MANAGE) as ctx:
        opened = json.loads(await Vault(d["keys"], ctx).decrypt_secret(cred["id"], cred["secret_enc"]))
        view = await connections.view(ctx, Vault(d["keys"], ctx), {"google": FakeCalendar()})
    assert opened == {"refresh_token": REFRESH, "address": ADDRESS}
    assert view["connections"][0]["address"] == ADDRESS and "secret" not in json.dumps(view)
    (job,) = await _jobs(app_pool, d["connection"])
    assert json.loads(job["payload"])["mode"] == "connect" and job["max_attempts"] == 3

    client = FakeCalendar(rotated={"refresh_token": "rt-rotated-0043", "address": ADDRESS})
    out = await push.sync_connection(app_pool, _runtime(d, client), d["tenant"], d["connection"],
                                     now=dt.datetime.now(dt.UTC))  # fmt: skip
    assert out["outcome"] == "pushed" and out["created_count"] == 1  # the intake's "reply within 2 h" task
    assert [c for c in client.calls if c[0] == "create"][0][1].startswith("task-")
    (after,) = await _rows(
        app_pool, d["tenant"], "SELECT event_count, failure_class, last_ok_at FROM crm.calendar_connection"
    )
    assert after["event_count"] == 1 and after["failure_class"] is None and after["last_ok_at"] is not None
    (sealed,) = await _rows(app_pool, d["tenant"], "SELECT secret_enc FROM crm.integration_credential "
                                                   "WHERE provider = 'google_calendar'")  # fmt: skip
    async with tenant_tx(app_pool, d["owner"], d["tenant"], action=Action.CALENDAR_FEED_MANAGE) as ctx:
        rotated = json.loads(await Vault(d["keys"], ctx).decrypt_secret(cred["id"], sealed["secret_enc"]))
    assert rotated["refresh_token"] == "rt-rotated-0043"
    for secret in (REFRESH, "rt-rotated-0043", ADDRESS):
        assert secret not in caplog.text


async def test_disconnect_removes_the_events_and_the_token(
    app_pool: asyncpg.Pool, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    d = await _setup(app_pool)
    mailbox_credential = uuid.UUID("00000000-0000-4000-8000-0000000a1011")
    async with tenant_tx(app_pool, d["owner"], d["tenant"], action=Action.CALENDAR_FEED_MANAGE) as ctx:
        # The same member's Gmail mailbox credential on the shared Google OAuth client (the S294 review case).
        sealed = await oauth_sync.seal_secret(Vault(d["keys"], ctx), mailbox_credential, {"refresh_token": "rt-mail-7"})
        await ctx.conn.execute(mailbox.INSERT_CREDENTIAL_SQL, mailbox_credential, "google", d["owner_id"], sealed)
        out = await connections.disconnect(ctx, Vault(d["keys"], ctx), d["connection"])
    assert out["connection"]["status"] == "revoked"
    jobs = await _jobs(app_pool, d["connection"])
    payload = json.loads(jobs[-1]["payload"])
    assert payload["mode"] == "disconnect" and payload["tenant_id"] == str(d["tenant"])
    client = FakeCalendar(remote=[push.oauth_sync.RemoteEvent("ev-1", "task-x", "h")])
    handler = push.build_handler(_runtime(d, client))
    result = await handler(app_pool, Job("j", "crm_calendar_push", payload, 1, 3))
    assert result["outcome"] == "removed" and result["deleted_count"] == 1
    assert client.revoked == []  # no provider revoke: the Google grant is shared with the mailbox
    assert await _rows(app_pool, d["tenant"], "SELECT 1 AS ok FROM crm.integration_credential "
                                              "WHERE provider = 'google_calendar'") == []  # fmt: skip
    assert await _rows(app_pool, d["tenant"], "SELECT 1 AS ok FROM crm.calendar_connection") == []
    kept_sql = "SELECT status FROM crm.integration_credential WHERE provider = 'google' AND member_id = $1"
    (kept,) = await _rows(app_pool, d["tenant"], kept_sql, d["owner_id"])
    assert kept["status"] == "connected"  # the mailbox is untouched
    # The mailbox and other credentials of the agency are untouched; a second run finds nothing to do.
    assert (await handler(app_pool, Job("j2", "crm_calendar_push", payload, 1, 3)))["outcome"] == "gone"
    assert REFRESH not in caplog.text and ADDRESS not in caplog.text


async def test_another_agency_sees_and_disconnects_nothing(app_pool: asyncpg.Pool) -> None:
    a = await _setup(app_pool)
    b = await _setup(app_pool)
    assert len(await _rows(app_pool, b["tenant"], "SELECT 1 AS ok FROM crm.calendar_connection")) == 1
    assert await _rows(app_pool, b["tenant"], "SELECT 1 AS ok FROM crm.calendar_connection "
                                              "WHERE calendar_connection_id = $1", a["connection"]) == []  # fmt: skip
    async with tenant_tx(app_pool, b["owner"], b["tenant"], action=Action.CALENDAR_FEED_MANAGE) as ctx:
        with pytest.raises(NotFound):
            await connections.disconnect(ctx, Vault(b["keys"], ctx), a["connection"])
    # B's sweep context never pushes A's connection (one agency per transaction).
    out = await push.sync_connection(app_pool, _runtime(b, FakeCalendar()), b["tenant"], a["connection"],
                                     now=dt.datetime.now(dt.UTC))  # fmt: skip
    assert out == {"outcome": "gone"}
    (still,) = await _rows(app_pool, a["tenant"], "SELECT status FROM crm.calendar_connection")
    assert still["status"] == "active"
