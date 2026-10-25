"""AA-945 send-as behind the dispatcher (scripted SQL, fake providers).

Card R: a revoked token holds the message ``held_reconnect``, disconnects the mailbox and its credential, alerts the
owners once through AA's system mail, and the client's message never goes through SES. Also: no mailbox -> held,
rejected -> failed, sent -> the Message-ID hash and the provider id; the nightly health check and the Test button;
reconnect re-queues held messages; the sender table covers every send path."""

from __future__ import annotations

import ast
import email
import email.policy
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.jobs.queue import Job
from app.mail import health, senders, state
from app.mail.providers import Rejected, Transient
from app.mail.sender import MailSender
from app.mail.state import MailRuntime
from app.members.directory import MemoryDirectory
from app.members.mail import MemoryMailer
from tests.unit.fakes import ScriptedConn
from tests.unit.mail_fakes import REVOKED, FakeProvider, MailVault, secret_blob
from tests.unit.slice_fakes import enc, system_ctx, tenant_ctx

MESSAGE = uuid.UUID("00000000-0000-4000-8000-00000000d001")
DISPATCH = uuid.UUID("00000000-0000-4000-8000-00000000d002")
MAILBOX = uuid.UUID("00000000-0000-4000-8000-00000000d003")
CREDENTIAL = uuid.UUID("00000000-0000-4000-8000-00000000d004")
CLIENT = uuid.UUID("00000000-0000-4000-8000-00000000d005")
CONV = uuid.UUID("00000000-0000-4000-8000-00000000d006")
APP = Path(__file__).resolve().parents[2] / "app"


def _message(**over: Any) -> dict[str, Any]:
    return {
        "message_id": MESSAGE,
        "status": "queued",
        "direction": "out",
        "channel_account_id": MAILBOX,
        "subject_enc": enc("message", "subject_enc", "Your Hoi An plan"),
        "body_enc": enc("message", "body_enc", "Dear Lan, here is the plan."),
        "conversation_id": CONV,
        "traveler_id": CLIENT,
        "trip_id": None,
        "mail_token": "ref000000001",
    } | over


def _mailbox(**over: Any) -> dict[str, Any]:
    return {
        "channel_account_id": MAILBOX,
        "kind": "gmail",
        "status": "connected",
        "address_enc": enc("channel_account", "address_enc", "hello@sunshine.example"),
        "credential_id": CREDENTIAL,
        "secret_enc": secret_blob(refresh_token="rt", address="hello@sunshine.example"),
    } | over


def _conn(message: Any = None, mailbox: Any = None) -> ScriptedConn:
    conn = ScriptedConn()
    conn.on("FROM crm.message m JOIN crm.conversation c", _message() if message is None else message)
    conn.on("FROM crm.channel_account a LEFT JOIN", _mailbox() if mailbox is None else mailbox)
    conn.on("SELECT email_enc FROM crm.traveler", enc("traveler", "email_enc", "lan@example.test"))
    conn.on("SELECT display_name FROM crm.tenant", "Sunshine Travel")
    conn.on(
        "WHERE conversation_id = $1 AND direction = 'in'",
        {"message_id": uuid.uuid4(), "rfc_message_id_enc": enc("message", "x", "<p@x.t>")},
    )
    conn.on("SELECT cognito_iss, cognito_sub FROM crm.member", [])
    conn.on("RETURNING channel_account_id", MAILBOX)
    return conn


def _runtime(provider: FakeProvider, *, owners: bool = False) -> tuple[MailRuntime, MemoryMailer, MemoryDirectory]:
    mailer = MemoryMailer()
    directory = MemoryDirectory()
    runtime = MailRuntime(
        keys=object(),
        providers={"gmail": provider, "smtp": provider},
        mailer=mailer,  # type: ignore[arg-type]
        directory=directory,
        web_origin="https://partner.test",
    )
    return runtime, mailer, directory


@pytest.fixture(autouse=True)
def fake_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.mail import health as health_module
    from app.mail import sender as sender_module

    monkeypatch.setattr(sender_module, "Vault", lambda keys, ctx: MailVault())
    monkeypatch.setattr(health_module, "Vault", lambda keys, ctx: MailVault())


@pytest.mark.asyncio
async def test_sent_through_the_agency_mailbox() -> None:
    provider = FakeProvider()
    runtime, mailer, _ = _runtime(provider)
    conn = _conn()
    assert await MailSender(runtime).send(system_ctx(conn), DISPATCH, "email", MESSAGE) == "sent"
    ((raw, secret, rcpt),) = provider.sent
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    assert rcpt == ["lan@example.test"] and secret["refresh_token"] == "rt"
    assert msg["Subject"] == "Your Hoi An plan [ref:ref000000001]" and msg["In-Reply-To"] == "<p@x.t>"
    (sent,) = conn.sql("SET status = 'sent'")
    assert sent[0] == MESSAGE and len(sent[1]) == 32 and sent[3] == "gm-1"
    assert mailer.sent == []  # nothing through AA's SES


@pytest.mark.asyncio
async def test_a_rotated_refresh_token_is_sealed_again() -> None:
    """Review S250: the refresh token a provider rotates on send or on the nightly check replaces the stored one."""
    new = {"refresh_token": "rt-2", "address": "hello@sunshine.example"}
    runtime, _, _ = _runtime(FakeProvider(rotated=new))
    conn = _conn()
    assert await MailSender(runtime).send(system_ctx(conn), DISPATCH, "email", MESSAGE) == "sent"
    (rotated,) = conn.sql("UPDATE crm.integration_credential SET secret_enc")
    assert rotated[0] == CREDENTIAL and rotated[1] == secret_blob(**new)
    health_conn = ScriptedConn().on("FROM crm.channel_account a JOIN", [_health_row()])
    health_conn.on("RETURNING channel_account_id", MAILBOX)
    assert dict(await health.run_agency(tenant_ctx(health_conn), runtime)) == {"ok": 1}
    assert health_conn.sql("UPDATE crm.integration_credential SET secret_enc")[0][1] == secret_blob(**new)
    # The Test button sends with the rotated token, not the one it read before the check.
    provider = FakeProvider(rotated=new)
    runtime, _, _ = _runtime(provider)
    button = (
        ScriptedConn().on("AND a.channel_account_id = $1", _health_row()).on("RETURNING channel_account_id", MAILBOX)
    )
    assert (await health.test_mailbox(tenant_ctx(button), runtime, MAILBOX, "minh@sunshine.example"))["test_mail_sent"]
    assert provider.sent[0][1]["refresh_token"] == "rt-2"
    # Nothing rotated: no write.
    runtime, _, _ = _runtime(FakeProvider())
    plain = _conn()
    await MailSender(runtime).send(system_ctx(plain), DISPATCH, "email", MESSAGE)
    assert plain.sql("SET secret_enc") == []


@pytest.mark.asyncio
async def test_card_r_revoked_token_holds_and_never_uses_ses(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeProvider(error=REVOKED)
    runtime, mailer, directory = _runtime(provider)
    owner_sub = directory.add("owner@sunshine.example")
    conn = _conn()
    conn.on(
        "SELECT cognito_iss, cognito_sub FROM crm.member", [{"cognito_iss": directory.issuer, "cognito_sub": owner_sub}]
    )
    ctx = system_ctx(conn)
    assert await MailSender(runtime).send(ctx, DISPATCH, "email", MESSAGE) == "held_reconnect"
    assert conn.sql("SET status = 'held_reconnect'") == [(MESSAGE, "auth_revoked")]
    assert conn.sql("UPDATE crm.channel_account SET status = 'disconnected'") == [(MAILBOX,)]
    assert conn.sql("UPDATE crm.integration_credential SET status = 'disconnected'") == [(CREDENTIAL, "auth_revoked")]
    # The only SES mail is the owner's alert; the client's text never reaches it.
    assert [m.kind for m in mailer.sent] == ["mailbox_disconnected"] and mailer.sent[0].to == "owner@sunshine.example"
    assert all("Dear Lan" not in m.text and "lan@example.test" != m.to for m in mailer.sent)
    # Already disconnected: no second alert.
    conn.on("RETURNING channel_account_id", None)
    assert await MailSender(runtime).send(system_ctx(conn), DISPATCH, "email", MESSAGE) == "held_reconnect"
    assert len(mailer.sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "mailbox", "expected"),
    [
        (None, {"status": "disconnected"}, "held_reconnect"),
        ({"channel_account_id": None}, None, "held_reconnect"),
        ({"status": "sent"}, None, "no_message"),
        ({"traveler_id": None}, None, "no_message"),
    ],
)
async def test_holds_and_skips(message: Any, mailbox: Any, expected: str) -> None:
    runtime, _, _ = _runtime(FakeProvider())
    conn = _conn(_message(**message) if message else None, _mailbox(**mailbox) if mailbox else None)
    assert await MailSender(runtime).send(system_ctx(conn), DISPATCH, "email", MESSAGE) == expected


@pytest.mark.asyncio
async def test_rejected_transient_and_no_message() -> None:
    runtime, _, _ = _runtime(FakeProvider(error=Rejected("provider_rejected")))
    conn = _conn()
    assert await MailSender(runtime).send(system_ctx(conn), DISPATCH, "email", MESSAGE) == "send_failed"
    assert conn.sql("SET status = 'failed'") == [(MESSAGE, "provider_rejected")]
    runtime, _, _ = _runtime(FakeProvider(error=Transient("provider_unavailable")))
    with pytest.raises(Transient):
        await MailSender(runtime).send(system_ctx(_conn()), DISPATCH, "email", MESSAGE)
    assert await MailSender(runtime).send(system_ctx(_conn()), DISPATCH, "sms", MESSAGE) == "no_message"
    assert await MailSender(runtime).send(system_ctx(_conn()), DISPATCH, "email", None) == "no_message"
    no_address = _conn().on("SELECT email_enc FROM crm.traveler", None)
    assert await MailSender(runtime).send(system_ctx(no_address), DISPATCH, "email", MESSAGE) == "send_failed"


@pytest.mark.asyncio
async def test_reconnect_requeues_held_messages_with_fresh_jobs() -> None:
    conn = ScriptedConn()
    conn.on("UPDATE crm.message SET status = 'queued'", [{"message_id": MESSAGE}])
    conn.on("SELECT dispatch_id FROM crm.dispatch_queue", [{"dispatch_id": DISPATCH}])
    conn.on("UPDATE crm.dispatch_queue SET status = 'queued'", [{"dispatch_id": DISPATCH}])
    assert await state.mark_connected(tenant_ctx(conn), MAILBOX, CREDENTIAL) == 1
    (job,) = conn.sql("INSERT INTO crm.job")
    assert job[0] == "crm_dispatch" and job[2].startswith(f"crm_dispatch:{DISPATCH}:requeue:")
    assert await state.requeue_mailbox(tenant_ctx(ScriptedConn()), MAILBOX) == 0


def _health_row(**over: Any) -> dict[str, Any]:
    return {
        "channel_account_id": MAILBOX,
        "kind": "gmail",
        "status": "connected",
        "credential_id": CREDENTIAL,
        "address_enc": enc("channel_account", "address_enc", "hello@sunshine.example"),
        "secret_enc": secret_blob(refresh_token="rt", address="hello@sunshine.example"),
    } | over


@pytest.mark.asyncio
async def test_health_check_outcomes() -> None:
    for error, outcome in ((None, "ok"), (REVOKED, "auth_revoked"), (Transient("unreachable"), "unreachable")):
        runtime, _, _ = _runtime(FakeProvider(error=error))
        conn = ScriptedConn().on("FROM crm.channel_account a JOIN", [_health_row()])
        conn.on("RETURNING channel_account_id", MAILBOX)
        assert dict(await health.run_agency(tenant_ctx(conn), runtime)) == {outcome: 1}
    runtime = MailRuntime(keys=None)
    assert await health.check_row(tenant_ctx(), runtime, _health_row()) == "not_configured"


@pytest.mark.asyncio
async def test_health_job_over_agencies(monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import asynccontextmanager

    from app.audit_export import registry
    from app.errors import Forbidden

    agencies = [type("A", (), {"tenant_id": uuid.uuid4()})() for _ in range(3)]

    async def fake_agencies(_pool: Any) -> list[Any]:
        return agencies

    calls = {"n": 0}

    @asynccontextmanager
    async def fake_tx(_pool: Any, tenant_id: Any, *, actor: str):  # noqa: ANN202
        calls["n"] += 1
        if calls["n"] == 2:
            raise Forbidden("tenant_locked")
        if calls["n"] == 3:
            raise RuntimeError("boom")
        yield system_ctx(ScriptedConn().on("FROM crm.channel_account a JOIN", []))

    monkeypatch.setattr(registry, "agencies", fake_agencies)
    monkeypatch.setattr(health, "system_tx", fake_tx)
    runtime, _, _ = _runtime(FakeProvider())
    result = await health.build_handler(runtime)(object(), Job("j", "crm_mail_health", {}, 1, 3))  # type: ignore[arg-type]
    assert result == {"agency_count": 1, "skipped_count": 1, "failed_count": 1}


@pytest.mark.asyncio
async def test_the_test_button_sends_from_the_mailbox_to_the_member() -> None:
    provider = FakeProvider()
    runtime, mailer, _ = _runtime(provider)
    conn = ScriptedConn().on("AND a.channel_account_id = $1", _health_row()).on("RETURNING channel_account_id", MAILBOX)
    conn.on("SELECT display_name FROM crm.tenant", "Sunshine Travel")
    result = await health.test_mailbox(tenant_ctx(conn), runtime, MAILBOX, "minh@sunshine.example")
    assert result == {"result": "ok", "check": "ok", "test_mail_sent": True}
    assert provider.sent[0][2] == ["minh@sunshine.example"] and mailer.sent == []
    failing = FakeProvider(error=Rejected("provider_rejected"))
    runtime, _, _ = _runtime(failing)
    failing.error = None
    original = failing.send

    async def rejecting(*a: Any, **k: Any) -> Any:
        raise Rejected("provider_rejected")

    failing.send = rejecting  # type: ignore[method-assign]
    result = await health.test_mailbox(tenant_ctx(conn), runtime, MAILBOX, "minh@sunshine.example")
    assert result["result"] == "failed" and result["check"] == "provider_rejected"
    failing.send = original  # type: ignore[method-assign]
    with pytest.raises(Exception, match="mailbox_not_found"):
        await health.test_mailbox(tenant_ctx(ScriptedConn()), runtime, MAILBOX, None)


@pytest.mark.asyncio
async def test_the_daily_health_schedule() -> None:
    from datetime import UTC, datetime

    pool = ScriptedConn().on("INSERT INTO crm.job", "job-1")
    early = health.DailyMailHealthSchedule(19, lambda: datetime(2026, 10, 25, 3, tzinfo=UTC))
    assert await early(pool) is None
    schedule = health.DailyMailHealthSchedule(19, lambda: datetime(2026, 10, 25, 20, tzinfo=UTC))
    assert await schedule(pool) == "job-1" and await schedule(pool) is None
    assert pool.sql("INSERT INTO crm.job")[0][2] == "crm_mail_health:schedule:2026-10-25"


def test_the_sender_table_covers_every_send_path() -> None:
    kinds = {row.path for row in senders.SENDERS}
    assert {row.sender_kind for row in senders.SENDERS} <= senders.SENDER_KINDS  # errata S247: no platform kind
    tree = ast.parse((APP / "members" / "mail.py").read_text(encoding="utf-8"))
    message_kinds = {
        kw.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Message"
        for kw in node.keywords
        if kw.arg == "kind" and isinstance(kw.value, ast.Constant)
    }
    assert message_kinds and message_kinds <= kinds
    # Every module that calls a provider's send is a row's module.
    senders_modules = {row.module for row in senders.SENDERS}
    for path in sorted((APP / "mail").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if ".send(built.raw" in source:
            module = "app." + ".".join(path.relative_to(APP).with_suffix("").parts)
            assert module in senders_modules, module
    assert senders.table()["senders"][0]["sender_kind"] == "agency_mailbox"
