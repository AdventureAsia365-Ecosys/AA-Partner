"""AA-945 forward-in ingest on scripted SQL: card R (an unknown address is discarded, count only, raw deleted), the
Gmail forwarding code, a stored and a duplicate mail, attachments kept or noted, the agency's own copied mail, the
per-minute scan hook, and the threading order (headers, trip token, sender, Unmatched)."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest

from app.documents.store import MemoryDocumentStore
from app.errors import NotFound
from app.jobs.payload_guard import check_payload
from app.jobs.queue import Job
from app.mail import ingest, threads
from app.mail.metrics import DROPPED
from app.mail.parse import parse
from app.mail.store import MemoryIngestStore
from app.tenancy import IngestAddress
from tests.unit.fakes import ScriptedConn
from tests.unit.mail_fakes import INGEST_DOMAIN, SES_AUTHENTICATED, MailVault, raw_mail
from tests.unit.slice_fakes import TENANT, system_ctx

CONV = uuid.UUID("00000000-0000-4000-8000-00000000c011")
TRIP = uuid.UUID("00000000-0000-4000-8000-00000000c022")
CLIENT = uuid.UUID("00000000-0000-4000-8000-00000000c033")
ADDRESS = IngestAddress(ingest_address_id=uuid.UUID("00000000-0000-4000-8000-00000000c044"), tenant_id=TENANT)
PDF = b"%PDF-1.4 tiny"
JOB = Job("j", "crm_mail_ingest", {"purpose": "mail_ingest"}, 1, 3)


def _conn(*, stored: Any = "new", own: Any = None) -> ScriptedConn:
    conn = ScriptedConn()
    conn.on("INSERT INTO crm.conversation", CONV)
    conn.on("INSERT INTO crm.message", (lambda *a: a[0]) if stored == "new" else stored)
    conn.on("FROM crm.channel_account WHERE address_bidx", own)
    return conn


def _patch(monkeypatch: pytest.MonkeyPatch, conn: ScriptedConn, *, known: bool = True) -> None:
    @asynccontextmanager
    async def fake(_pool: Any, local_part: str):  # noqa: ANN202
        if not known:
            raise NotFound("address_not_found")
        yield system_ctx(conn), ADDRESS

    monkeypatch.setattr(ingest, "ingest_tx", fake)
    monkeypatch.setattr(ingest, "Vault", lambda keys, ctx: MailVault())


@pytest.mark.asyncio
async def test_card_r_unknown_address_is_discarded_and_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    store = MemoryIngestStore()
    store.objects["raw/m1"] = raw_mail()
    store.objects["raw/m2"] = raw_mail(to="nobody@elsewhere.example")
    store.objects["raw/m3"] = b"\x00"  # no headers: not a mail
    store.objects["raw/m4"] = raw_mail(extra={"X-SES-Virus-Verdict": "FAIL"})
    store.objects["raw/AMAZON_SES_SETUP_NOTIFICATION"] = b"setup"
    conn = _conn()
    _patch(monkeypatch, conn, known=False)
    DROPPED.take()
    result = await ingest.build_handler(store, None, object(), INGEST_DOMAIN)(object(), JOB)  # type: ignore[arg-type]
    check_payload(result, what="result")
    assert result == {"dropped_count": 3, "setup_notice_count": 1, "unparsable_count": 1}
    assert store.objects == {} and conn.sql("INSERT INTO crm.message") == []
    assert DROPPED.take() == 3 and DROPPED.take() == 0


@pytest.mark.asyncio
async def test_a_mail_is_stored_threaded_and_the_raw_object_deleted(monkeypatch: pytest.MonkeyPatch) -> None:
    store = MemoryIngestStore()
    store.objects["raw/m1"] = raw_mail(attachments=[("visa.pdf", "application/pdf", PDF)])
    store.objects["raw/m2"] = raw_mail(
        message_id="<other@x.test>",
        sender="forwarding-noreply@google.com",
        subject="(#123456789) Gmail Forwarding Confirmation",
        text="Confirmation code: 123456789",
    )
    conn = _conn()
    conn.on("FROM crm.traveler WHERE email_bidx", [{"traveler_id": CLIENT}])
    conn.on("t.stage = ANY", TRIP)
    docs = MemoryDocumentStore()
    _patch(monkeypatch, conn)
    result = await ingest.build_handler(store, docs, object(), INGEST_DOMAIN)(object(), JOB)  # type: ignore[arg-type]
    assert result == {"forwarding_code_count": 1, "stored_count": 1}
    assert store.objects == {}
    (args,) = conn.sql("INSERT INTO crm.message")
    assert args[1] == CONV and args[2] == "in" and args[3] == "received" and args[16] == "sender"
    assert args[6].startswith(b"enc:message.subject_enc:Our trip")  # sealed under the mail DEK
    (doc,) = conn.sql("INSERT INTO crm.document")
    assert doc[1] == TRIP and doc[2] == CLIENT and len(docs.objects) == 1
    assert args[20] == [doc[0]] and args[21] == []
    (code,) = conn.sql("UPDATE crm.ingest_address")
    assert code[0] == ADDRESS.ingest_address_id and b"123456789" in code[1]


@pytest.mark.asyncio
async def test_duplicates_failures_and_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    store = MemoryIngestStore()
    store.objects["raw/m1"] = raw_mail()
    conn = _conn(stored=None)
    _patch(monkeypatch, conn)
    handler = ingest.build_handler(store, None, object(), INGEST_DOMAIN)
    assert await handler(object(), JOB) == {"duplicate_count": 1}  # type: ignore[arg-type]
    assert await ingest.build_handler(None, None, object(), INGEST_DOMAIN)(object(), JOB) == {  # type: ignore[arg-type]
        "status": "not_configured"
    }
    with pytest.raises(RuntimeError):
        await ingest.build_handler(store, None, None, INGEST_DOMAIN)(object(), JOB)  # type: ignore[arg-type]
    store.objects["raw/m2"] = raw_mail()
    conn.on("INSERT INTO crm.message", RuntimeError("db down"))
    assert await handler(object(), JOB) == {"failed_count": 1}  # type: ignore[arg-type]
    assert "raw/m2" in store.objects  # kept for the next run

    @asynccontextmanager
    async def other_404(*_: Any):  # noqa: ANN202
        raise NotFound("trip_not_found")
        yield  # pragma: no cover

    monkeypatch.setattr(ingest, "ingest_tx", other_404)
    assert await handler(object(), JOB) == {"failed_count": 1}  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_attachment_notes_and_the_agency_own_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _conn(own=uuid.uuid4())
    vault = MailVault()
    mail = parse(
        raw_mail(
            sender="hello@sunshine.example",
            to=f"lan@example.test, sunshine-ab12cd@{INGEST_DOMAIN}",
            attachments=[("a.exe", "application/octet-stream", b"MZ"), ("big.pdf", "application/pdf", b"%PDF" * 3)],
            extra=SES_AUTHENTICATED,
        ),
        ingest_domain=INGEST_DOMAIN,
    )
    outcome, found = await ingest.store_message(system_ctx(conn), vault, MemoryDocumentStore(), mail)
    assert outcome == "stored" and found is not None and found.match_method == "unmatched"
    (args,) = conn.sql("INSERT INTO crm.message")
    assert args[2:5] == ("out", "sent", "agency_mailbox") and args[18] is not None  # the agent's own sent copy
    assert args[21] == ["no_subject", "no_subject"]
    conn2 = _conn()
    conn2.on("FROM crm.traveler WHERE email_bidx", [{"traveler_id": CLIENT}])
    await ingest.store_message(system_ctx(conn2), vault, None, mail.__class__(**{**mail.__dict__, "trip_token": None}))
    assert conn2.sql("INSERT INTO crm.message")[0][21] == ["not_configured", "not_configured"]


@pytest.mark.asyncio
async def test_threading_order() -> None:
    ctx = system_ctx(
        ScriptedConn().on(
            "FROM crm.message m JOIN crm.conversation c",
            {"conversation_id": CONV, "trip_id": TRIP, "traveler_id": None},
        )
    )
    found = await threads.thread(ctx, reference_hashes=[b"h"], trip_token=None, client_bidxs=[], sender_bidx=None)
    assert (found.conversation_id, found.match_method) == (CONV, "reply_header")
    conn = ScriptedConn().on("FROM crm.trip WHERE mail_token", TRIP).on("SELECT conversation_id FROM", CONV)
    conn.on("FROM crm.traveler WHERE email_bidx", [{"traveler_id": CLIENT}]).on("FROM crm.trip_traveler WHERE", 1)
    found = await threads.thread(
        system_ctx(conn), reference_hashes=[], trip_token="ab12cd34", client_bidxs=[b"e"], sender_bidx=b"e"
    )
    assert (found.trip_id, found.traveler_id, found.match_method) == (TRIP, CLIENT, "trip_token")
    # Two clients share the address: no guess -> Unmatched (an existing one from the same sender).
    conn = ScriptedConn().on("FROM crm.traveler WHERE email_bidx", [{"traveler_id": CLIENT}, {"traveler_id": TRIP}])
    conn.on("EXISTS (SELECT 1 FROM crm.message m", CONV)
    found = await threads.thread(
        system_ctx(conn), reference_hashes=[], trip_token=None, client_bidxs=[b"e"], sender_bidx=b"e"
    )
    assert (found.conversation_id, found.match_method) == (CONV, "unmatched")


@pytest.mark.asyncio
async def test_the_scan_queues_one_job_per_minute_only_when_mail_waits() -> None:
    store = MemoryIngestStore()
    now = datetime(2026, 10, 25, 3, 0, tzinfo=UTC)
    clock = [now]
    scan = ingest.MailIngestScan(store, lambda: clock[0])
    pool = ScriptedConn().on("INSERT INTO crm.job", "job-1")
    assert await scan(pool) is None and pool.sql("INSERT INTO crm.job") == []  # nothing waiting
    store.objects["raw/m1"] = b"x"
    assert await scan(pool) is None  # within the minute: no second listing
    clock[0] = now.replace(minute=1)
    assert await scan(pool) == "job-1"
    (args,) = pool.sql("INSERT INTO crm.job")
    assert args[0] == "crm_mail_ingest" and args[2] == "crm_mail_ingest:schedule:2026-10-25T03:01"
    assert await ingest.MailIngestScan(None)(pool) is None


@pytest.mark.asyncio
async def test_a_spoofed_own_mailbox_from_is_stored_as_inbound() -> None:
    """Review S250: a mail that only claims the agency's mailbox in From (DKIM failed) never becomes a "sent" message
    in a client's timeline; the own-mailbox lookup is not even made."""
    spoofed = {
        **SES_AUTHENTICATED,
        "Authentication-Results": "amazonses.com; spf=fail client-ip=203.0.113.9; dkim=fail; dmarc=fail",
    }
    for extra in (spoofed, {}, {**SES_AUTHENTICATED, "X-SES-Spam-Verdict": "GRAY"}):
        conn = _conn(own=uuid.uuid4())
        mail = parse(
            raw_mail(
                sender="hello@sunshine.example", to=f"lan@example.test, sunshine-ab12cd@{INGEST_DOMAIN}", extra=extra
            ),
            ingest_domain=INGEST_DOMAIN,
        )
        assert not mail.sender_authenticated
        outcome, _ = await ingest.store_message(system_ctx(conn), MailVault(), None, mail)
        (args,) = conn.sql("INSERT INTO crm.message")
        assert outcome == "stored" and args[2:6] == ("in", "received", None, None)
        assert conn.sql("FROM crm.channel_account WHERE address_bidx") == []
    # An import is the member's own export: its From is trusted without SES headers.
    conn = _conn(own=uuid.uuid4())
    mail = parse(raw_mail(sender="hello@sunshine.example", to="lan@example.test"), ingest_domain=INGEST_DOMAIN)
    await ingest.store_message(system_ctx(conn), MailVault(), None, mail, imported=True)
    assert conn.sql("INSERT INTO crm.message")[0][2:5] == ("out", "sent", "agency_mailbox")


@pytest.mark.asyncio
async def test_a_duplicate_race_puts_no_attachment_file(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _conn(stored=None)
    conn.on("FROM crm.traveler WHERE email_bidx", [{"traveler_id": CLIENT}])
    docs = MemoryDocumentStore()
    mail = parse(raw_mail(attachments=[("visa.pdf", "application/pdf", PDF)]), ingest_domain=INGEST_DOMAIN)
    assert await ingest.store_message(system_ctx(conn), MailVault(), docs, mail) == ("duplicate", None)
    assert docs.objects == {}  # the document row rolled back with the savepoint; no file was put


@pytest.mark.asyncio
async def test_any_parser_error_deletes_the_object(monkeypatch: pytest.MonkeyPatch) -> None:
    store = MemoryIngestStore()
    store.objects["raw/m1"] = raw_mail()

    def broken(*_: Any, **__: Any) -> Any:
        raise LookupError("unknown charset")

    monkeypatch.setattr(ingest, "parse", broken)
    _patch(monkeypatch, _conn())
    result = await ingest.build_handler(store, None, object(), INGEST_DOMAIN)(object(), JOB)  # type: ignore[arg-type]
    assert result == {"unparsable_count": 1} and store.objects == {}


@pytest.mark.asyncio
async def test_failing_objects_do_not_starve_new_mail_and_age_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review S250: 50 objects that keep failing must not block the 51st, and an old one is dropped, not retried."""
    store = MemoryIngestStore()
    now = datetime(2026, 10, 25, 3, 0, tzinfo=UTC)
    for i in range(ingest.BATCH):
        key = f"raw/a{i:03d}"
        store.objects[key] = raw_mail(to=f"brokenbox{i:03d}@{INGEST_DOMAIN}")
        store.modified[key] = now
    store.objects["raw/z-new"] = raw_mail()
    store.modified["raw/z-new"] = now
    conn = _conn()

    @asynccontextmanager
    async def fake(_pool: Any, local_part: str):  # noqa: ANN202
        if local_part.startswith("brokenbox"):
            raise RuntimeError("db down for this one")
        yield system_ctx(conn), ADDRESS

    monkeypatch.setattr(ingest, "ingest_tx", fake)
    monkeypatch.setattr(ingest, "Vault", lambda keys, ctx: MailVault())
    handler = ingest.build_handler(store, None, object(), INGEST_DOMAIN, now=lambda: now)
    result = await handler(object(), JOB)  # type: ignore[arg-type]
    assert result == {"failed_count": ingest.BATCH, "stored_count": 1}
    assert "raw/z-new" not in store.objects and len(store.objects) == ingest.BATCH
    DROPPED.take()
    later = ingest.build_handler(store, None, object(), INGEST_DOMAIN, now=lambda: now + ingest.POISON_AGE)
    result = await later(object(), JOB)  # type: ignore[arg-type]
    assert result == {"dropped_count": ingest.BATCH, "failed_permanent_count": ingest.BATCH}
    assert store.objects == {} and DROPPED.take() == ingest.BATCH


@pytest.mark.asyncio
async def test_s3_listing_pages_with_start_after() -> None:
    from app.mail.store import S3IngestStore

    when = datetime(2026, 10, 25, 2, 0, tzinfo=UTC)
    calls: list[dict[str, Any]] = []

    class Client:
        def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return {"Contents": [{"Key": "raw/b", "LastModified": when}]}

    store = S3IngestStore("bucket", client=Client())
    (item,) = await store.list_objects("raw/", 50, "raw/a")
    assert (item.key, item.last_modified) == ("raw/b", when)
    await store.list_objects("raw/", 50)
    assert calls[0]["StartAfter"] == "raw/a" and "StartAfter" not in calls[1]
