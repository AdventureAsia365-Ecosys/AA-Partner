"""AA-958 Settings > Channels and the connection health (scripted SQL): what the owner types is checked; connect seals
the token under the integration DEK, keeps the hook id on a reconnect and queues a template sync; disconnect deletes
the credential; the owner sees health, URLs and templates, an agent the status only; the Test button, the template
sync, the daily check and its schedule."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

import pytest

from app.audit_export import registry
from app.errors import Conflict, Invalid, NotFound, Unavailable
from app.jobs.queue import Job
from app.jobs.worker import PermanentError
from app.messaging import connection, health, state
from app.messaging.providers import AuthRevoked, Rejected, TemplateInfo, Transient
from app.settings import Settings
from tests.unit.fakes import ScriptedConn
from tests.unit.messaging_fakes import (
    ACCOUNT_SID,
    AUTH_TOKEN,
    CONNECTION,
    CONTENT_SID,
    CREDENTIAL,
    HOOK_ID,
    FakeTextProvider,
    TextVault,
    connection_row,
    runtime,
)
from tests.unit.slice_fakes import TENANT, fake_tx, system_ctx, tenant_ctx

BOTH = ("twilio", "sandbox")


def _validate(**over: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "provider": "twilio",
        "account_sid": ACCOUNT_SID,
        "auth_token": AUTH_TOKEN,
        "api_key_sid": None,
        "api_key_secret": None,
        "sms_sender": None,
        "whatsapp_sender": "+61 491 570 156",
        "environment_providers": BOTH,
    }
    return connection.validate(**(values | over))


def test_validate_accepts_the_shapes_the_vendor_gives() -> None:
    out = _validate(sms_sender="Sunshine")
    assert out["secret"] == {"account_sid": ACCOUNT_SID, "auth_token": AUTH_TOKEN}
    assert (out["sms_sender"], out["whatsapp_sender"]) == ("Sunshine", "+61491570156")
    assert _validate(sms_sender="MG" + "3" * 32, whatsapp_sender=None)["sms_sender"] == "MG" + "3" * 32
    assert _validate(sms_sender="+61 491 570 157")["sms_sender"] == "+61491570157"
    key = _validate(api_key_sid="SK" + "4" * 32, api_key_secret="k" * 32)["secret"]
    assert key["api_key_sid"] == "SK" + "4" * 32 and key["api_key_secret"] == "k" * 32
    assert _validate(provider="sandbox", auth_token="dev-sandbox-token-1")["secret"]["auth_token"]


@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"provider": "sandbox", "environment_providers": ("twilio",)}, "provider_not_available"),
        ({"provider": "carrier-pigeon"}, "provider_not_available"),
        ({"account_sid": "AC123"}, "invalid_account_sid"),
        ({"auth_token": "short"}, "invalid_auth_token"),
        ({"api_key_sid": "SK1", "api_key_secret": "k" * 32}, "invalid_api_key_sid"),
        ({"api_key_sid": "SK" + "4" * 32}, "invalid_api_key_secret"),
        ({"sms_sender": "12345"}, "invalid_sms_sender"),
        ({"sms_sender": "Too long a name"}, "invalid_sms_sender"),
        ({"sms_sender": "+999"}, "invalid_sms_sender"),
        ({"whatsapp_sender": "0491 570 156"}, "invalid_whatsapp_sender"),
        ({"whatsapp_sender": " ", "sms_sender": None}, "sender_required"),
    ],
)
def test_validate_refuses(over: dict[str, Any], code: str) -> None:
    with pytest.raises(Invalid) as caught:
        _validate(**over)
    assert caught.value.code == code


@pytest.mark.asyncio
async def test_first_connect_creates_credential_connection_and_a_sync_job() -> None:
    conn = ScriptedConn().on("FROM crm.integration_credential", None).on("FROM crm.messaging_connection WHERE", None)
    conn.on("INSERT INTO crm.job", uuid.uuid4())
    ctx = tenant_ctx(conn)
    out = await connection.connect(
        ctx,
        TextVault(),  # type: ignore[arg-type]
        runtime(),
        provider="twilio",
        secret={"account_sid": ACCOUNT_SID, "auth_token": AUTH_TOKEN},
        sms_sender=None,
        whatsapp_sender="+61491570156",
    )
    (cred,) = conn.sql("INSERT INTO crm.integration_credential")
    assert cred[1] == ACCOUNT_SID and json.loads(bytes(cred[2])[7:]) == {
        "account_sid": ACCOUNT_SID,
        "auth_token": AUTH_TOKEN,
    }
    (row,) = conn.sql("INSERT INTO crm.messaging_connection")
    assert row[1] == "twilio" and row[2] == cred[0] and len(row[3]) == 32 and row[5] == "+61491570156"
    assert out["hook_url"] == f"https://go.crm.example.test/crm/public/v1/hooks/twilio/{row[3]}"
    assert out["status_callback_url"].endswith(f"{row[3]}/status")
    assert conn.sql("INSERT INTO crm.job")[0][0] == "crm_messaging_templates"
    assert [e.action for e in ctx.audit_events] == ["messaging_connect"]


@pytest.mark.asyncio
async def test_reconnect_keeps_the_hook_id() -> None:
    conn = ScriptedConn().on("FROM crm.integration_credential", CREDENTIAL)
    conn.on("FROM crm.messaging_connection WHERE", {"messaging_connection_id": CONNECTION, "hook_id": HOOK_ID})
    conn.on("INSERT INTO crm.job", uuid.uuid4())
    out = await connection.connect(
        tenant_ctx(conn),
        TextVault(),  # type: ignore[arg-type]
        runtime(),
        provider="sandbox",
        secret={"account_sid": ACCOUNT_SID, "auth_token": "dev-sandbox-token-1"},
        sms_sender="Sunshine",
        whatsapp_sender=None,
    )
    assert conn.sql("UPDATE crm.integration_credential")[0][:2] == (CREDENTIAL, ACCOUNT_SID)
    assert conn.sql("UPDATE crm.messaging_connection")[0] == (CONNECTION, "sandbox", CREDENTIAL, "Sunshine", None)
    assert out["hook_url"].endswith(HOOK_ID)


@pytest.mark.asyncio
async def test_connect_needs_a_webhook_origin_and_disconnect_deletes_the_credential() -> None:
    with pytest.raises(Unavailable):
        await connection.connect(
            tenant_ctx(),
            TextVault(),  # type: ignore[arg-type]
            runtime(hooks_origin=""),
            provider="twilio",
            secret={},
            sms_sender=None,
            whatsapp_sender=None,
        )
    conn = ScriptedConn().on("FROM crm.messaging_connection c", connection_row())
    ctx = tenant_ctx(conn)
    assert (await connection.disconnect(ctx))["status"] == "disconnected"
    assert conn.sql("DELETE FROM crm.integration_credential")[0] == (CREDENTIAL,)
    assert ctx.audit_events[0].action == "messaging_disconnect"
    with pytest.raises(NotFound):
        await connection.disconnect(tenant_ctx())


TEMPLATE_ROW = {
    "channel_template_id": uuid.uuid4(),
    "content_sid": CONTENT_SID,
    "name": "deposit_reminder",
    "language": "en",
    "category": "UTILITY",
    "approval": "approved",
    "body": "Hi {{1}}, {{2}}",
    "variables": [],
    "synced_at": dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
}


@pytest.mark.asyncio
async def test_the_owner_sees_health_urls_and_templates_an_agent_the_status() -> None:
    conn = ScriptedConn().on("FROM crm.messaging_connection c", connection_row(whatsapp_sender=None))
    conn.on("FROM crm.channel_template WHERE", [TEMPLATE_ROW])
    owner = await connection.view(tenant_ctx(conn), runtime())
    assert owner["status"] == "connected" and owner["channels"] == {"sms": True, "whatsapp": False}
    assert owner["connection"]["account"] == "AC01…cdef" and owner["connection"]["hook_url"].endswith(HOOK_ID)
    assert owner["templates"][0]["variables"] == ["1", "2"] and owner["can_manage"] is True
    assert "secret_enc" not in json.dumps(owner, default=str) and AUTH_TOKEN not in json.dumps(owner, default=str)
    agent = await connection.view(tenant_ctx(conn, role="agent"), runtime())
    assert set(agent) == {"status", "channels", "providers", "can_manage"} and agent["can_manage"] is False
    none = await connection.view(tenant_ctx(), runtime())
    assert none["status"] == "not_set_up" and none["channels"] == {"sms": False, "whatsapp": False}


@pytest.mark.parametrize(
    ("error", "outcome", "status_sql"),
    [
        (None, "ok", "SET status = 'connected'"),
        (AuthRevoked("auth_revoked"), "auth_revoked", "SET status = 'disconnected'"),
        (Rejected("vendor_rejected"), "vendor_rejected", "SET status = 'needs_attention'"),
        (Transient("rate_limited"), "rate_limited", "SET status = 'needs_attention'"),
        (Transient("bad_response"), "unreachable", "SET status = 'needs_attention'"),
    ],
)
@pytest.mark.asyncio
async def test_check_outcomes(monkeypatch: pytest.MonkeyPatch, error: Any, outcome: str, status_sql: str) -> None:
    monkeypatch.setattr(health, "Vault", lambda keys, ctx: TextVault())
    alerts: list[Any] = []

    async def alert(ctx: Any, rt: Any) -> int:
        alerts.append(1)
        return 1

    monkeypatch.setattr(state, "alert_owners", alert)
    conn = ScriptedConn().on("RETURNING integration_credential_id", CREDENTIAL)
    assert (
        await health.check_row(system_ctx(conn), runtime(FakeTextProvider(check_error=error)), connection_row())
        == outcome
    )
    assert conn.sql(status_sql)
    assert len(alerts) == (1 if outcome == "auth_revoked" else 0)
    no_provider = connection_row(provider="other")
    assert await health.check_row(system_ctx(conn), runtime(), no_provider) == "not_configured"


TEMPLATES = [
    TemplateInfo(CONTENT_SID, "deposit_reminder", "en", "UTILITY", "approved", "Hi {{1}}", ("1",)),
    TemplateInfo("HX" + "b2" * 16, "offer", "en", None, "pending", "Hello", ()),
]


@pytest.mark.asyncio
async def test_template_sync_upserts_and_drops_the_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "Vault", lambda keys, ctx: TextVault())
    conn = ScriptedConn()
    counts = await health.sync_templates(
        system_ctx(conn), runtime(FakeTextProvider(templates=TEMPLATES)), connection_row()
    )
    assert counts == {"template_count": 2, "approved_count": 1}
    upserts = conn.sql("INSERT INTO crm.channel_template")
    assert [u[1] for u in upserts] == [CONTENT_SID, "HX" + "b2" * 16] and upserts[0][7] == ["1"]
    assert conn.sql("DELETE FROM crm.channel_template")[0] == ([CONTENT_SID, "HX" + "b2" * 16],)
    assert await health.sync_templates(system_ctx(conn), runtime(), connection_row(provider="x")) == {
        "template_count": 0
    }
    rt = runtime()
    rt.keys = None
    with pytest.raises(PermanentError):
        await health.sync_templates(system_ctx(conn), rt, connection_row())


@pytest.mark.asyncio
async def test_the_test_button(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "Vault", lambda keys, ctx: TextVault())
    provider = FakeTextProvider()
    template = {"content_sid": CONTENT_SID, "body": "Hi {{1}}, your trip {{2}} is confirmed.", "variables": []}
    conn = ScriptedConn().on("FROM crm.messaging_connection c", connection_row(sms_sender=None))
    conn.on("FROM crm.channel_template WHERE approval = 'approved'", template)
    ctx = tenant_ctx(conn)
    out = await health.test_connection(ctx, runtime(provider), channel="whatsapp", to="+61 491 570 110")
    assert out == {
        "result": "ok",
        "check": "ok",
        "test_message_sent": True,
        "template": CONTENT_SID,
        "inbound_check": "pending",
    }
    ((_, sent),) = provider.sent
    # WhatsApp: an approved template (free text outside the owner's window would fail later, 63016), values "Test".
    assert sent.to == "+61491570110" and sent.content_sid == CONTENT_SID
    assert sent.variables == {"1": "Test", "2": "Test"} and sent.body == "Hi Test, your trip Test is confirmed."
    assert conn.sql("FROM crm.channel_template")[0] == (None,)
    assert conn.sql("SET last_test_at")[0][1] == "ok" and ctx.audit_events[-1].action == "messaging_test"
    await health.test_connection(ctx, runtime(provider), channel="whatsapp", to="+61491570110", content_sid=CONTENT_SID)
    assert conn.sql("FROM crm.channel_template")[-1] == (CONTENT_SID,)
    sms = FakeTextProvider()
    sms_conn = ScriptedConn().on("FROM crm.messaging_connection c", connection_row())
    out = await health.test_connection(tenant_ctx(sms_conn), runtime(sms), channel="sms", to="+61491570110")
    assert out["template"] is None and sms.sent[0][1].body == health.TEST_TEXT and sms.sent[0][1].content_sid is None
    assert sms_conn.sql("FROM crm.channel_template") == []
    out = await health.test_connection(ctx, runtime(provider), channel="sms", to="+61491570110")
    assert out["check"] == "channel_not_configured" and out["result"] == "failed"
    failing = FakeTextProvider(error=Rejected("window_closed"))
    out = await health.test_connection(ctx, runtime(failing), channel="whatsapp", to="+61491570110")
    assert (out["check"], out["test_message_sent"]) == ("window_closed", False)
    out = await health.test_connection(ctx, runtime(provider), channel=None, to=None)
    assert out["test_message_sent"] is False and out["result"] == "ok"
    with pytest.raises(Invalid):
        await health.test_connection(ctx, runtime(provider), channel="sms", to="not a number")
    with pytest.raises(NotFound):
        await health.test_connection(tenant_ctx(), runtime(provider), channel=None, to=None)


@pytest.mark.asyncio
async def test_a_whatsapp_test_without_an_approved_template_is_refused_before_any_call() -> None:
    provider = FakeTextProvider()
    conn = ScriptedConn().on("FROM crm.messaging_connection c", connection_row())
    with pytest.raises(Conflict) as caught:
        await health.test_connection(tenant_ctx(conn), runtime(provider), channel="whatsapp", to="+61491570110")
    assert caught.value.code == "template_required"
    assert provider.checked == 0 and provider.sent == [] and conn.sql("SET last_test_at") == []


def test_the_inbound_check_waits_for_a_message_after_the_test() -> None:
    at = dt.datetime(2026, 11, 1, 9, tzinfo=dt.UTC)
    assert state.inbound_check(None, at) is None
    assert state.inbound_check(at, None) == "pending"
    assert state.inbound_check(at, at - dt.timedelta(minutes=1)) == "pending"
    assert state.inbound_check(at, at + dt.timedelta(minutes=1)) == "ok"


def _job(payload: dict[str, Any]) -> Job:
    return Job(id=str(uuid.uuid4()), kind="crm_messaging_templates", payload=payload, attempt=1, max_attempts=3)


@pytest.mark.asyncio
async def test_the_template_job(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "Vault", lambda keys, ctx: TextVault())
    conn = ScriptedConn().on("FROM crm.messaging_connection c", connection_row())
    conn.on("RETURNING integration_credential_id", CREDENTIAL)
    conn.on("SELECT cognito_iss, cognito_sub FROM crm.member", [])
    monkeypatch.setattr(health, "system_tx", fake_tx(system_ctx(conn)))
    handler = health.build_templates_handler(runtime(FakeTextProvider(templates=TEMPLATES)))
    assert (await handler(object(), _job({"tenant_id": str(TENANT)})))["status"] == "done"  # type: ignore[arg-type]
    revoked = health.build_templates_handler(runtime(FakeTextProvider(check_error=AuthRevoked("auth_revoked"))))
    assert (await revoked(object(), _job({"tenant_id": str(TENANT)})))["reason_code"] == "auth_revoked"  # type: ignore[arg-type]
    rejected = health.build_templates_handler(runtime(FakeTextProvider(check_error=Rejected("vendor_rejected"))))
    assert (await rejected(object(), _job({"tenant_id": str(TENANT)})))["status"] == "failed"  # type: ignore[arg-type]
    with pytest.raises(PermanentError):
        await handler(object(), _job({}))  # type: ignore[arg-type]
    empty = ScriptedConn().on("FROM crm.messaging_connection c", None)
    monkeypatch.setattr(health, "system_tx", fake_tx(system_ctx(empty)))
    assert (await handler(object(), _job({"tenant_id": str(TENANT)})))["reason_code"] == "not_connected"  # type: ignore[arg-type]

    @__import__("contextlib").asynccontextmanager
    async def gone(*_: Any, **__: Any):  # noqa: ANN202
        raise NotFound("tenant_not_found")
        yield  # pragma: no cover

    monkeypatch.setattr(health, "system_tx", gone)
    assert (await handler(object(), _job({"tenant_id": str(TENANT)})))["reason_code"] == "tenant_inactive"  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_the_daily_check_over_agencies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "Vault", lambda keys, ctx: TextVault())
    agencies = [type("A", (), {"tenant_id": uuid.uuid4()})() for _ in range(4)]

    async def fake_agencies(_pool: Any) -> list[Any]:
        return agencies

    monkeypatch.setattr(registry, "agencies", fake_agencies)
    conns = iter(
        [
            ScriptedConn().on("FROM crm.messaging_connection c", connection_row()),
            ScriptedConn().on("FROM crm.messaging_connection c", None),
        ]
    )
    calls = {"n": 0}

    @__import__("contextlib").asynccontextmanager
    async def tx(*_: Any, **__: Any):  # noqa: ANN202
        calls["n"] += 1
        if calls["n"] == 3:
            raise NotFound("tenant_not_found")
        if calls["n"] == 4:
            raise RuntimeError("boom")
        yield system_ctx(next(conns))

    monkeypatch.setattr(health, "system_tx", tx)
    out = await health.build_health_handler(runtime(FakeTextProvider(templates=TEMPLATES)))(object(), _job({}))  # type: ignore[arg-type]
    assert out == {
        "agency_count": 2,
        "skipped_count": 1,
        "failed_count": 1,
        "ok_count": 1,
        "templates_synced_count": 1,
    }

    class SyncFails(FakeTextProvider):
        async def templates(self, secret: dict[str, Any]) -> list[TemplateInfo]:
            raise Transient("unreachable")

    counts = await health.run_agency(
        system_ctx(ScriptedConn().on("FROM crm.messaging_connection c", connection_row())), runtime(SyncFails())
    )
    assert counts["templates_failed"] == 1


@pytest.mark.asyncio
async def test_the_daily_schedule() -> None:
    clock = {"now": dt.datetime(2026, 11, 1, 1, 0, tzinfo=dt.UTC)}
    schedule = health.DailyMessagingHealthSchedule(3, lambda: clock["now"])
    conn = ScriptedConn().on("INSERT INTO crm.job", "job-1")
    assert await schedule(conn) is None  # type: ignore[arg-type]
    clock["now"] = clock["now"].replace(hour=4)
    assert await schedule(conn) == "job-1"  # type: ignore[arg-type]
    assert await schedule(conn) is None  # type: ignore[arg-type]
    assert conn.sql("INSERT INTO crm.job")[0][0] == "crm_messaging_health"


def test_hooks_origin_setting() -> None:
    assert Settings(environment="test").messaging_hooks_origin() == "https://go.crm.example.test"
    assert Settings(environment="dev").messaging_hooks_origin() == ""
    dev = Settings(environment="dev", intake_page_origins=("https://go-dev.example.test",))
    assert dev.messaging_hooks_origin() == "https://go-dev.example.test"
    own = Settings(environment="dev", messaging_hook_base="https://hooks.example.test")
    assert own.messaging_hooks_origin() == "https://hooks.example.test"
    assert runtime(hooks_origin="").hook_url(HOOK_ID) == "" and runtime(hooks_origin="").status_url(HOOK_ID) == ""
