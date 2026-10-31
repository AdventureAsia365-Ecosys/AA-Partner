"""AA-945 (card CRM-17) end to end on PostgreSQL 15 with the RDS role shape, real field encryption, a fake send-as
provider and an in-memory ingest bucket.

- **E**: a forwarded reply without ``References`` lands on the trip by its ``[ref:<mail_token>]`` and its client by the
  sender's blind index; the raw object is deleted; the thread decrypts for the agent.
- **R**: mail to an unknown forward-in address leaves no row in any agency and is deleted; a revoked token holds the
  message ``held_reconnect`` and disconnects the mailbox; reconnecting re-queues it and it is sent.
- ``ingest_tx`` sees only the agency whose active address it was called with.
- Review round S250: a rotated refresh token replaces the stored credential; the agency's own BCC copy is "sent" only
  when SES authenticated its From (a spoofed one is inbound); a history import runs in slices with added counts; a
  send whose retries run out fails the message instead of leaving it queued.
- S252 live finding: a reply carrying only the Message-ID the CRM put on the sent mail (In-Reply-To when the provider
  kept it, References when it replaced it, as Gmail's API does) threads to that mail's conversation, every mailbox kind.
"""

from __future__ import annotations

import email
import email.policy
import uuid
from typing import Any

import asyncpg
import pytest

from app.dispatch.worker import build_handler as build_dispatch
from app.documents.store import MemoryDocumentStore
from app.errors import NotFound
from app.jobs.queue import Job
from app.mail import conversations, history_import, ingest, mailbox
from app.mail.sender import MailSender
from app.mail.state import MailRuntime
from app.mail.store import MemoryIngestStore
from app.permissions import Action
from app.settings import Settings
from app.tenancy import ingest_tx, tenant_tx
from app.vault import Vault
from tests.integration.test_crm005_intake import _keys
from tests.integration.test_crm010_lifecycle import _inquiry
from tests.unit.mail_fakes import REVOKED, SES_AUTHENTICATED, FakeProvider, raw_mail

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

DOMAIN = "crm-in.example.test"
SETTINGS = Settings(environment="test", db_ssl="disable", crm_ingest_domain=DOMAIN)
JOB = Job("j", "crm_mail_ingest", {"purpose": "mail_ingest"}, 1, 3)


async def _setup(pool: asyncpg.Pool) -> dict[str, Any]:
    keys = _keys()
    d = await _inquiry(pool, keys, "owner")
    d["keys"] = keys
    async with tenant_tx(pool, d["owner"], d["tenant"], action=Action.OWN_MAILBOX_CONNECT) as ctx:
        vault = Vault(keys, ctx)
        await mailbox.connect(
            ctx,
            vault,
            scope="own",
            provider="google",
            secret={"refresh_token": "rt", "address": "minh@senvang.example"},
        )
        d["address"] = (await mailbox.ingest_address(ctx, vault, SETTINGS))["address"]
        d["token"] = await ctx.conn.fetchval("SELECT mail_token FROM crm.trip WHERE trip_id = $1", d["trip"])
    return d


async def _count(pool: asyncpg.Pool, tenant_id: uuid.UUID, table: str) -> int:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        return int(await conn.fetchval(f"SELECT count(*) FROM crm.{table}"))  # noqa: S608 — fixed names


async def test_card_e_a_forwarded_reply_lands_on_the_trip(app_pool: asyncpg.Pool) -> None:
    d = await _setup(app_pool)
    store = MemoryIngestStore()
    store.objects["raw/m1"] = raw_mail(
        to=d["address"],
        sender="Hoa <hoa.nguyen@example.test>",
        subject=f"Fwd: Re: our plan [ref:{d['token']}]",
        references=None,
        attachments=[("visa.pdf", "application/pdf", b"%PDF-1.4 visa")],
    )
    store.objects["raw/m2"] = raw_mail(to=f"nobody-xyz123@{DOMAIN}")
    docs = MemoryDocumentStore()
    result = await ingest.build_handler(store, docs, d["keys"], DOMAIN)(app_pool, JOB)
    assert result == {"dropped_count": 1, "stored_count": 1}
    assert store.objects == {} and len(docs.objects) == 1
    async with tenant_tx(app_pool, d["owner"], d["tenant"], action=Action.TRIP_VIEW) as ctx:
        listed = await conversations.list_conversations(ctx, trip_id=d["trip"])
        (conv,) = listed["conversations"]
        assert conv["traveler_id"] == str(d["lead"]) and conv["message_count"] == 1
        thread = await conversations.conversation_messages(
            ctx, Vault(d["keys"], ctx), uuid.UUID(conv["conversation_id"])
        )
    (message,) = thread["messages"]
    assert message["subject"].startswith("Fwd: Re: our plan") and message["from"] == "hoa.nguyen@example.test"
    assert message["match_method"] == "trip_token" and len(message["attachments"]) == 1
    # The same mail again (BCC + forward): stored once.
    again = raw_mail(to=d["address"], sender="hoa.nguyen@example.test", message_id="<abc-2@mail.example.test>")
    store.objects["raw/m3"] = again
    store.objects["raw/m4"] = again
    assert await ingest.build_handler(store, docs, d["keys"], DOMAIN)(app_pool, JOB) == {
        "duplicate_count": 1,
        "stored_count": 1,
    }


async def test_ingest_tx_sees_only_its_agency(app_pool: asyncpg.Pool) -> None:
    a = await _setup(app_pool)
    b = await _setup(app_pool)
    local_part = a["address"].split("@")[0]
    async with ingest_tx(app_pool, local_part) as (ctx, address):
        assert address.tenant_id == a["tenant"]
        assert await ctx.conn.fetchval("SELECT count(*) FROM crm.trip") == 1
        assert await ctx.conn.fetchval("SELECT count(*) FROM crm.trip WHERE trip_id = $1", b["trip"]) == 0
    with pytest.raises(NotFound):
        async with ingest_tx(app_pool, "nobody-xyz123"):
            pass  # pragma: no cover


async def test_card_r_revoked_token_holds_then_reconnect_sends(app_pool: asyncpg.Pool) -> None:
    d = await _setup(app_pool)
    provider = FakeProvider(error=REVOKED)
    runtime = MailRuntime(keys=d["keys"], providers={"gmail": provider})
    message_id = uuid.uuid4()
    async with tenant_tx(app_pool, d["owner"], d["tenant"], action=Action.MESSAGE_SEND) as ctx:
        sent = await conversations.send(
            ctx,
            Vault(d["keys"], ctx),
            message_id,
            conversation_id=None,
            trip_id=d["trip"],
            subject="Your plan",
            body="Dear Hoa, here is the plan.",
        )
    job = Job("j", "crm_dispatch", {"tenant_id": str(d["tenant"]), "dispatch_id": sent["dispatch_id"]}, 1, 5)
    dispatch = build_dispatch(MailSender(runtime))
    assert (await dispatch(app_pool, job))["reason_code"] == "held_reconnect"
    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(d["tenant"]))
        assert await conn.fetchval("SELECT status FROM crm.message WHERE message_id = $1", message_id) == (
            "held_reconnect"
        )
        assert await conn.fetchval("SELECT status FROM crm.channel_account") == "disconnected"
    # Reconnect: the held message and its dispatch row go back to the queue, and the send goes out.
    provider.error = None
    async with tenant_tx(app_pool, d["owner"], d["tenant"], action=Action.OWN_MAILBOX_CONNECT) as ctx:
        connected = await mailbox.connect(
            ctx,
            Vault(d["keys"], ctx),
            scope="own",
            provider="google",
            secret={"refresh_token": "rt-2", "address": "minh@senvang.example"},
        )
    assert connected["requeued_count"] == 1
    assert (await dispatch(app_pool, job))["status"] == "sent"
    ((raw, _secret, rcpt),) = provider.sent
    assert rcpt == ["hoa.nguyen@example.test"] and f"[ref:{d['token']}]".encode() in raw
    assert await _count(app_pool, d["tenant"], "dispatch_queue") == 1


async def _scalar(pool: asyncpg.Pool, tenant_id: uuid.UUID, sql: str, *args: Any) -> Any:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        return await conn.fetchval(sql, *args)


async def _send(pool: asyncpg.Pool, d: dict[str, Any]) -> tuple[uuid.UUID, Job]:
    message_id = uuid.uuid4()
    async with tenant_tx(pool, d["owner"], d["tenant"], action=Action.MESSAGE_SEND) as ctx:
        sent = await conversations.send(
            ctx,
            Vault(d["keys"], ctx),
            message_id,
            conversation_id=None,
            trip_id=d["trip"],
            subject="Your plan",
            body="Dear Hoa, here is the plan.",
        )
    return message_id, Job(
        "j", "crm_dispatch", {"tenant_id": str(d["tenant"]), "dispatch_id": sent["dispatch_id"]}, 1, 5
    )


async def test_a_rotated_refresh_token_replaces_the_stored_credential(app_pool: asyncpg.Pool) -> None:
    d = await _setup(app_pool)
    sql = "SELECT secret_enc FROM crm.integration_credential"
    before = await _scalar(app_pool, d["tenant"], sql)
    provider = FakeProvider(
        external_id=None, rotated={"refresh_token": "rt-rotated", "address": "minh@senvang.example"}
    )
    _, job = await _send(app_pool, d)
    assert (
        await build_dispatch(MailSender(MailRuntime(keys=d["keys"], providers={"gmail": provider})))(app_pool, job)
    )["status"] == "sent"
    after = await _scalar(app_pool, d["tenant"], sql)
    assert after != before
    # The next send opens the rotated credential.
    provider.rotated = None
    _, job = await _send(app_pool, d)
    await build_dispatch(MailSender(MailRuntime(keys=d["keys"], providers={"gmail": provider})))(app_pool, job)
    assert provider.sent[-1][1]["refresh_token"] == "rt-rotated"


async def test_only_an_authenticated_own_mailbox_copy_is_stored_as_sent(app_pool: asyncpg.Pool) -> None:
    d = await _setup(app_pool)
    store = MemoryIngestStore()
    own = {"sender": "minh@senvang.example", "subject": f"Plan [ref:{d['token']}]"}
    store.objects["raw/a-spoofed"] = raw_mail(to=d["address"], message_id="<spoof@x.test>", **own)
    store.objects["raw/b-bcc"] = raw_mail(
        to=f"hoa.nguyen@example.test, {d['address']}",
        message_id="<bcc@x.test>",
        extra=SES_AUTHENTICATED
        | {"Authentication-Results": "amazonses.com; dkim=pass header.i=@senvang.example; dmarc=pass"},
        **own,
    )
    assert await ingest.build_handler(store, None, d["keys"], DOMAIN)(app_pool, JOB) == {"stored_count": 2}
    rows = await _rows(
        app_pool,
        d["tenant"],
        "SELECT direction, status, sender_kind, channel_account_id IS NOT NULL AS own FROM crm.message"
        " WHERE imported = false ORDER BY direction",
    )
    assert [tuple(r) for r in rows] == [("in", "received", None, False), ("out", "sent", "agency_mailbox", True)]


async def _rows(pool: asyncpg.Pool, tenant_id: uuid.UUID, sql: str) -> list[Any]:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        return list(await conn.fetch(sql))


async def test_a_history_import_runs_in_slices(app_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch) -> None:
    d = await _setup(app_pool)
    monkeypatch.setattr(history_import, "SLICE", 2)
    store = MemoryIngestStore()
    data = "date,from,to,subject,body\n" + "".join(f",guest{i}@example.test,,Hello {i},Body {i}\n" for i in range(5))
    async with tenant_tx(app_pool, d["owner"], d["tenant"], action=Action.OWN_MAILBOX_CONNECT) as ctx:
        created = await history_import.create_import(ctx, store, source_kind="csv", data=data.encode())
    import_id = created["mail_import_id"]
    handler = history_import.build_handler(store, None, d["keys"], DOMAIN)
    payload: dict[str, Any] = {"tenant_id": str(d["tenant"]), "mail_import_id": import_id}
    statuses = []
    for batch in range(3):
        result = await handler(app_pool, Job("j", "crm_mail_history_import", payload | {"batch": batch}, 1, 3))
        statuses.append(result["status"])
    assert statuses == ["running", "running", "done"]
    row = await _rows(
        app_pool,
        d["tenant"],
        f"SELECT status, total_count, imported_count FROM crm.mail_import WHERE mail_import_id = '{import_id}'",  # noqa: S608
    )
    assert tuple(row[0]) == ("done", 5, 5)
    queued = await app_pool.fetchval(
        "SELECT count(*) FROM crm.job WHERE kind = 'crm_mail_history_import' AND idempotency_key LIKE $1",
        f"crm_mail_history_import:{import_id}%",
    )
    assert queued == 3  # the first slice from the upload, two continuations


async def test_a_send_whose_retries_run_out_fails_the_message(app_pool: asyncpg.Pool) -> None:
    d = await _setup(app_pool)

    class Unreachable(FakeProvider):
        async def send(self, *_: Any, **__: Any) -> Any:
            raise ConnectionError("provider down")

    message_id, job = await _send(app_pool, d)
    dispatch = build_dispatch(MailSender(MailRuntime(keys=d["keys"], providers={"gmail": Unreachable()})))
    with pytest.raises(ConnectionError):
        await dispatch(app_pool, job)
    last = Job(job.id, job.kind, job.payload, 5, 5)
    assert (await dispatch(app_pool, last))["reason_code"] == "send_failed"
    status = await _scalar(
        app_pool, d["tenant"], "SELECT status || ':' || failure_code FROM crm.message WHERE message_id = $1", message_id
    )
    assert status == "failed:retries_exhausted"


async def test_a_message_to_an_invalid_address_is_recorded_and_never_sent(app_pool: asyncpg.Pool) -> None:
    """AA-1014: a synthetic client at a reserved ``.invalid`` domain (RFC 6761) gets no mail through the agency's
    mailbox; the message ends ``failed`` / ``invalid_tld`` and its dispatch row ``dropped`` / ``send_failed``."""
    d = await _setup(app_pool)
    async with tenant_tx(app_pool, d["owner"], d["tenant"], action=Action.CLIENT_EDIT) as ctx:
        sealed = await Vault(d["keys"], ctx).encrypt("traveler", "email_enc", d["lead"], "Hoa@Tour.Example.INVALID")
        await ctx.conn.execute("UPDATE crm.traveler SET email_enc = $2 WHERE traveler_id = $1", d["lead"], sealed)
    provider = FakeProvider()
    message_id, job = await _send(app_pool, d)
    result = await build_dispatch(MailSender(MailRuntime(keys=d["keys"], providers={"gmail": provider})))(app_pool, job)
    assert result["status"] == "dropped" and result["reason_code"] == "send_failed"
    assert provider.sent == []
    status = await _scalar(
        app_pool, d["tenant"], "SELECT status || ':' || failure_code FROM crm.message WHERE message_id = $1", message_id
    )
    assert status == "failed:invalid_tld"
    gate = await _scalar(app_pool, d["tenant"], "SELECT status || ':' || gate_result FROM crm.dispatch_queue")
    assert gate == "dropped:send_failed"


PROVIDER_ID = "<CAKnvzAQx-Q=WQ3_providerid@mail.gmail.com>"
PROVIDER_OF = {"gmail": "google", "microsoft": "microsoft", "smtp": "smtp"}


@pytest.mark.parametrize("kind", ["gmail", "microsoft", "smtp"])
@pytest.mark.parametrize("rewrites", [False, True], ids=["id-kept", "id-replaced"])
async def test_a_header_only_reply_threads_to_the_sent_mail(app_pool: asyncpg.Pool, kind: str, rewrites: bool) -> None:
    """S252 live finding: Gmail's API put its own Message-ID on mail A, so the client's reply (In-Reply-To = the id it
    received, no trip tag) went to Unmatched. A reply that carries the id the CRM put on the raw message, in
    In-Reply-To (provider kept it) or in References (provider replaced it), lands in A's conversation by header, for
    every mailbox kind; the sender is not a client, so no other rule could place it there."""
    d = await _setup(app_pool)
    if kind != "gmail":
        async with tenant_tx(app_pool, d["owner"], d["tenant"], action=Action.OWN_MAILBOX_CONNECT) as ctx:
            await mailbox.connect(
                ctx,
                Vault(d["keys"], ctx),
                scope="own",
                provider=PROVIDER_OF[kind],
                secret={"refresh_token": "rt", "address": "minh@senvang.example"},
            )
    provider = FakeProvider(external_id=None)
    message_id, job = await _send(app_pool, d)
    sent = await build_dispatch(MailSender(MailRuntime(keys=d["keys"], providers={kind: provider})))(app_pool, job)
    assert sent["status"] == "sent"
    delivered = email.message_from_bytes(provider.sent[0][0], policy=email.policy.default)
    if rewrites:
        del delivered["Message-ID"]
        delivered["Message-ID"] = PROVIDER_ID
    parent_id = str(delivered["Message-ID"])
    store = MemoryIngestStore()
    store.objects["raw/reply"] = raw_mail(
        to=d["address"],
        sender="Spouse <spouse@example.test>",
        subject="Re: Your plan",
        message_id="<reply-1@example.test>",
        in_reply_to=parent_id,
        references=f"{delivered['References']} {parent_id}",
    )
    assert await ingest.build_handler(store, None, d["keys"], DOMAIN)(app_pool, JOB) == {"stored_count": 1}
    rows = await _rows(
        app_pool,
        d["tenant"],
        "SELECT r.match_method, r.conversation_id = a.conversation_id AS same FROM crm.message r, crm.message a"
        f" WHERE r.direction = 'in' AND a.message_id = '{message_id}'",  # noqa: S608 — a uuid we made
    )
    assert [tuple(r) for r in rows] == [("reply_header", True)]
