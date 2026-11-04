"""AA-958 Twilio webhooks (scripted SQL): card R, a missing or wrong signature (or another account) is 403 with
nothing stored; an inbound message is threaded by the sender's number (one client -> their latest conversation on
the channel, else a new one on their open trip; unknown or shared numbers -> Unmatched, never a random trip), stored
once (Twilio retries), opens the WhatsApp window, honours STOP / START and emits ``message.received``; card E, a status
callback only moves the delivery state forward."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.errors import Forbidden
from app.messaging import signature, webhook
from app.messaging.webhook import HookRequest
from app.tenancy import MessagingHook
from tests.unit.fakes import ScriptedConn
from tests.unit.messaging_fakes import (
    ACCOUNT_SID,
    AGENCY_WA,
    AUTH_TOKEN,
    CLIENT_PHONE,
    CONNECTION,
    HOOK_ID,
    HOOKS_ORIGIN,
    OTHER_TOKEN,
    TextVault,
    connection_row,
    runtime,
)
from tests.unit.slice_fakes import TENANT, fake_tx, system_ctx

CLIENT = uuid.UUID("00000000-0000-4000-8000-00000000a001")
TRIP = uuid.UUID("00000000-0000-4000-8000-00000000a002")
CONV = uuid.UUID("00000000-0000-4000-8000-00000000a003")
NEW_CONV = uuid.UUID("00000000-0000-4000-8000-00000000a004")
MSG_SID = "SM" + "d4" * 16
URL = f"{HOOKS_ORIGIN}/crm/public/v1/hooks/twilio/{HOOK_ID}"


def _params(**over: str) -> list[tuple[str, str]]:
    base = {
        "MessageSid": MSG_SID,
        "AccountSid": ACCOUNT_SID,
        "From": f"whatsapp:{CLIENT_PHONE}",
        "To": f"whatsapp:{AGENCY_WA}",
        "Body": "Can we add a cooking class?",
        "NumMedia": "0",
    }
    return list((base | over).items())


def _request(
    params: list[tuple[str, str]], *, kind: str = "inbound", token: str = AUTH_TOKEN, sig: Any = "sign"
) -> Any:
    url = URL + ("/status" if kind == "status" else "")
    return HookRequest(
        hook_id=HOOK_ID,
        kind=kind,
        params=params,
        signature=signature.sign(token, url, params) if sig == "sign" else sig,
    )


def _conn() -> ScriptedConn:
    conn = ScriptedConn()
    conn.on("FROM crm.messaging_connection c", connection_row())
    conn.on("SELECT 1 FROM crm.message WHERE channel", None)
    conn.on("FROM crm.traveler WHERE phone_bidx", [{"traveler_id": CLIENT}])
    conn.on("WHERE channel = $1 AND traveler_id = $2", {"conversation_id": CONV, "trip_id": TRIP})
    conn.on("INSERT INTO crm.conversation", NEW_CONV)
    conn.on("INSERT INTO crm.message (message_id", lambda *a: a[0])
    conn.on("SELECT owner_member_id FROM crm.trip", uuid.uuid4())
    conn.on("INSERT INTO crm.metric_event", uuid.uuid4())
    return conn


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch) -> Any:
    def install(conn: ScriptedConn) -> Any:
        ctx = system_ctx(conn)
        hook = MessagingHook(messaging_connection_id=CONNECTION, tenant_id=TENANT)
        monkeypatch.setattr(webhook, "messaging_hook_tx", fake_tx(ctx, hook))
        monkeypatch.setattr(webhook, "Vault", lambda keys, ctx: TextVault())
        return ctx

    return install


@pytest.mark.parametrize(
    "request_",
    [
        _request(_params(), sig=None),
        _request(_params(), sig="bad"),
        _request(_params(), token=OTHER_TOKEN),
        _request(_params(AccountSid="AC" + "9" * 32)),
        # signed for the inbound URL, posted to the status URL
        HookRequest(HOOK_ID, "status", _params(), signature.sign(AUTH_TOKEN, URL, _params())),
    ],
)
@pytest.mark.asyncio
async def test_card_r_bad_signature_is_403_and_nothing_is_stored(setup: Any, request_: Any) -> None:
    conn = _conn()
    setup(conn)
    with pytest.raises(Forbidden) as caught:
        await webhook.handle(object(), runtime(), request_)  # type: ignore[arg-type]
    assert caught.value.code == "invalid_signature"
    assert conn.sql("INSERT INTO") == [] and conn.sql("UPDATE crm.") == []


@pytest.mark.asyncio
async def test_no_connection_or_another_connection_or_no_url_is_refused(setup: Any) -> None:
    for row in (None, connection_row(messaging_connection_id=uuid.uuid4())):
        setup(_conn().on("FROM crm.messaging_connection c", row))
        with pytest.raises(Forbidden):
            await webhook.handle(object(), runtime(), _request(_params()))  # type: ignore[arg-type]
    setup(_conn())
    with pytest.raises(Forbidden):
        await webhook.handle(object(), runtime(hooks_origin=""), _request(_params()))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_inbound_from_a_known_client_joins_their_latest_conversation(setup: Any) -> None:
    conn = _conn()
    ctx = setup(conn)
    assert await webhook.handle(object(), runtime(), _request(_params())) == "stored"  # type: ignore[arg-type]
    (insert,) = conn.sql("INSERT INTO crm.message (message_id")
    assert insert[1] == CONV and insert[2] == "whatsapp" and insert[5] == f"p:{CLIENT_PHONE}".encode()
    assert insert[7] == MSG_SID and insert[8] == "sender" and insert[9] == []
    assert conn.sql("UPDATE crm.conversation SET last_message_at")[0] == (CONV,)
    assert conn.sql("SET last_inbound_at")[0] == (CONNECTION,)
    assert conn.sql("SET opted_out_at") == []
    (event,) = conn.sql("INSERT INTO crm.metric_event")
    assert event[0] == TRIP and event[2] == "message.received" and event[4] is not None
    assert [e.action for e in ctx.audit_events] == ["message_received"]


@pytest.mark.asyncio
async def test_a_client_without_a_conversation_gets_one_on_their_open_trip(setup: Any) -> None:
    conn = _conn().on("WHERE channel = $1 AND traveler_id = $2", None).on("FROM crm.trip_traveler tt", TRIP)
    setup(conn)
    params = _params(From=CLIENT_PHONE, To="+61491570157", NumMedia="2", Body="")
    assert await webhook.handle(object(), runtime(), _request(params)) == "stored"  # type: ignore[arg-type]
    assert conn.sql("INSERT INTO crm.conversation")[0] == (CLIENT, TRIP, "sms")
    (insert,) = conn.sql("INSERT INTO crm.message (message_id")
    assert insert[1] == NEW_CONV and insert[2] == "sms" and insert[3] is None and insert[9] == ["media_not_kept"]


@pytest.mark.parametrize(
    ("travellers", "unmatched", "expected"),
    [
        ([], None, NEW_CONV),
        ([{"traveler_id": CLIENT}, {"traveler_id": uuid.uuid4()}], None, NEW_CONV),  # a shared number: no guess
        ([], CONV, CONV),
    ],
)
@pytest.mark.asyncio
async def test_unknown_numbers_go_to_unmatched(
    setup: Any, travellers: list[Any], unmatched: Any, expected: Any
) -> None:
    conn = _conn().on("FROM crm.traveler WHERE phone_bidx", travellers)
    conn.on("WHERE c.trip_id IS NULL AND c.traveler_id IS NULL", unmatched)
    setup(conn)
    assert await webhook.handle(object(), runtime(), _request(_params())) == "stored"  # type: ignore[arg-type]
    (insert,) = conn.sql("INSERT INTO crm.message (message_id")
    assert insert[1] == expected and insert[8] == "unmatched"
    assert conn.sql("INSERT INTO crm.metric_event") == []  # no trip: no bell
    if expected == NEW_CONV:
        assert conn.sql("INSERT INTO crm.conversation")[0] == (None, None, "whatsapp")


@pytest.mark.asyncio
async def test_a_short_code_is_stored_never_matched(setup: Any) -> None:
    conn = _conn()
    setup(conn)
    assert await webhook.handle(object(), runtime(), _request(_params(From="AGENCY"))) == "stored"  # type: ignore[arg-type]
    (insert,) = conn.sql("INSERT INTO crm.message (message_id")
    assert insert[5] is None and insert[8] == "unmatched"
    assert conn.sql("FROM crm.traveler WHERE phone_bidx") == []


@pytest.mark.asyncio
async def test_a_retried_or_bad_message_is_not_stored_twice(setup: Any) -> None:
    conn = _conn().on("SELECT 1 FROM crm.message WHERE channel", 1)
    setup(conn)
    assert await webhook.handle(object(), runtime(), _request(_params())) == "duplicate"  # type: ignore[arg-type]
    conn = _conn().on("INSERT INTO crm.message (message_id", None)  # lost a race with the retry
    setup(conn)
    assert await webhook.handle(object(), runtime(), _request(_params())) == "duplicate"  # type: ignore[arg-type]
    assert conn.sql("UPDATE crm.conversation SET last_message_at") == []
    conn = _conn()
    setup(conn)
    bad = _params(MessageSid="nope")
    assert await webhook.handle(object(), runtime(), _request(bad)) == "ignored"  # type: ignore[arg-type]
    assert conn.sql("INSERT INTO") == []


@pytest.mark.parametrize(
    ("over", "opt"),
    [
        ({"Body": " stop "}, True),
        ({"Body": "Unsubscribe"}, True),
        ({"Body": "start"}, False),
        ({"Body": "please stop the noise", "OptOutType": "STOP"}, True),
        ({"Body": "yes", "OptOutType": "START"}, False),
        ({"Body": "please stop sending me offers"}, None),
        ({"Body": "x", "NumMedia": "lots"}, None),
    ],
)
@pytest.mark.asyncio
async def test_stop_and_start(setup: Any, over: dict[str, str], opt: Any) -> None:
    conn = _conn()
    ctx = setup(conn)
    assert await webhook.handle(object(), runtime(), _request(_params(**over))) == "stored"  # type: ignore[arg-type]
    changes = conn.sql("SET opted_out_at")
    if opt is None:
        assert changes == []
    else:
        assert changes[0] == (CONV, CLIENT, "whatsapp", opt)
        assert ctx.audit_events[0].action == ("messaging_opt_out" if opt else "messaging_opt_in")


def _status(**over: str) -> list[tuple[str, str]]:
    return list(({"MessageSid": MSG_SID, "AccountSid": ACCOUNT_SID, "MessageStatus": "delivered"} | over).items())


@pytest.mark.asyncio
async def test_card_e_status_only_moves_forward(setup: Any) -> None:
    conn = _conn().on("UPDATE crm.message_delivery d", uuid.uuid4())
    setup(conn)
    assert await webhook.handle(object(), runtime(), _request(_status(), kind="status")) == "status_applied"  # type: ignore[arg-type]
    assert conn.sql("UPDATE crm.message_delivery d")[0] == (MSG_SID, "delivered", 5, None)
    assert conn.sql("SET last_status_at")[0] == (CONNECTION,)
    conn = _conn().on("UPDATE crm.message_delivery d", None)  # a later state is already stored
    setup(conn)
    late = _status(MessageStatus="sent")
    assert await webhook.handle(object(), runtime(), _request(late, kind="status")) == "status_kept"  # type: ignore[arg-type]
    assert conn.sql("UPDATE crm.message_delivery d")[0][1:3] == ("sent", 3)


@pytest.mark.asyncio
async def test_status_errors_and_unknown_states(setup: Any) -> None:
    conn = _conn().on("UPDATE crm.message_delivery d", uuid.uuid4())
    conn.on("WHERE m.external_id = $1 AND m.direction = 'out'", {
        "message_id": uuid.uuid4(), "channel": "sms", "conversation_id": CONV, "traveler_id": CLIENT,
    })  # fmt: skip
    setup(conn)
    failed = _status(MessageStatus="undelivered", ErrorCode="21610")
    await webhook.handle(object(), runtime(), _request(failed, kind="status"))  # type: ignore[arg-type]
    assert conn.sql("UPDATE crm.message_delivery d")[0] == (MSG_SID, "undelivered", 4, "21610")
    assert conn.sql("SET opted_out_at")[0] == (CONV, CLIENT, "sms", True)
    conn = _conn()
    setup(conn)
    odd = _status(MessageStatus="receiving", ErrorCode="x" * 9)
    assert await webhook.handle(object(), runtime(), _request(odd, kind="status")) == "ignored"  # type: ignore[arg-type]
    assert conn.sql("UPDATE crm.message_delivery d") == []


@pytest.mark.asyncio
async def test_the_hook_context_finds_the_agency_by_hook_id() -> None:
    from app.errors import NotFound
    from app.tenancy import messaging_hook_tx

    with pytest.raises(NotFound):
        async with messaging_hook_tx(ScriptedConn(), "NOT-A-HOOK"):  # type: ignore[arg-type]
            pass  # pragma: no cover
    with pytest.raises(NotFound):
        async with messaging_hook_tx(ScriptedConn(), HOOK_ID):  # type: ignore[arg-type]
            pass  # pragma: no cover
    found = {"messaging_connection_id": CONNECTION, "tenant_id": TENANT}
    locked = ScriptedConn().on("FROM crm.messaging_connection WHERE hook_id", found)
    locked.on("SELECT status FROM crm.tenant", "locked")
    with pytest.raises(NotFound):
        async with messaging_hook_tx(locked, HOOK_ID):  # type: ignore[arg-type]
            pass  # pragma: no cover
    conn = ScriptedConn().on("FROM crm.messaging_connection WHERE hook_id", found)
    conn.on("SELECT status FROM crm.tenant", "active")
    async with messaging_hook_tx(conn, HOOK_ID) as (ctx, hook):  # type: ignore[arg-type]
        assert (ctx.tenant_id, hook.messaging_connection_id, ctx.actor) == (
            TENANT,
            CONNECTION,
            "job:crm_messaging_hook",
        )
        ctx.audit("message_received", "allowed", "message", "x")
    assert conn.sql("set_config('app.messaging_hook_id'")[0] == (HOOK_ID,)
    assert conn.sql("INSERT INTO crm.audit_event")
    denied = ScriptedConn().on("FROM crm.messaging_connection WHERE hook_id", found)
    denied.on("SELECT status FROM crm.tenant", "active")
    with pytest.raises(Forbidden):
        async with messaging_hook_tx(denied, HOOK_ID) as (ctx, _hook):  # type: ignore[arg-type]
            ctx.audit("decrypt", "denied", "integration_credential", "x", reason="integrity_check_failed")
            raise Forbidden("invalid_signature")
    assert denied.sql("INSERT INTO crm.audit_event")
    with pytest.raises(RuntimeError):
        async with messaging_hook_tx(conn, HOOK_ID):  # type: ignore[arg-type]
            raise RuntimeError("boom")


# ── AA-969 review round 3: the task's hook cache ────────────────────────────────────────────────────────────────


def _counting(monkeypatch: pytest.MonkeyPatch, conn: ScriptedConn) -> list[bool]:
    """Replace the hook context with one that records each database lookup (and the gate key it would take)."""
    from contextlib import asynccontextmanager

    lookups: list[bool] = []
    ctx = system_ctx(conn)
    hook = MessagingHook(messaging_connection_id=CONNECTION, tenant_id=TENANT)

    @asynccontextmanager
    async def tx(_pool: Any, hook_id: str, *, known: bool = False) -> Any:
        lookups.append(known)
        if hook_id != HOOK_ID:
            from app.errors import NotFound

            raise NotFound("hook_not_found")
        yield ctx, hook

    monkeypatch.setattr(webhook, "messaging_hook_tx", tx)
    monkeypatch.setattr(webhook, "Vault", lambda keys, ctx: TextVault())
    return lookups


@pytest.mark.asyncio
async def test_an_unsigned_request_is_refused_before_the_database(monkeypatch: pytest.MonkeyPatch) -> None:
    lookups = _counting(monkeypatch, _conn())
    for sig in (None, ""):
        with pytest.raises(Forbidden):
            await webhook.handle(object(), runtime(), _request(_params(), sig=sig), webhook.HookCache())  # type: ignore[arg-type]
    assert lookups == []


@pytest.mark.asyncio
async def test_bad_signatures_on_a_known_hook_cost_no_lookup_and_never_lock_it(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]
    cache = webhook.HookCache(clock=lambda: now[0])
    conn = _conn()
    lookups = _counting(monkeypatch, conn)
    assert await webhook.handle(object(), runtime(), _request(_params()), cache) == "stored"  # type: ignore[arg-type]
    assert lookups == [False] and cache.known(HOOK_ID) and len(cache) == 1
    for _ in range(50):
        with pytest.raises(Forbidden):
            await webhook.handle(object(), runtime(), _request(_params(), sig="bad"), cache)  # type: ignore[arg-type]
        with pytest.raises(Forbidden):  # another account's token, or another account's SID
            await webhook.handle(object(), runtime(), _request(_params(), token=OTHER_TOKEN), cache)  # type: ignore[arg-type]
    assert lookups == [False]
    # a genuine callback right in the flood: through the database again (a cached token only ever refuses)
    duplicate = await webhook.handle(object(), runtime(), _request(_params()), cache)  # type: ignore[arg-type]
    assert duplicate == "stored" and lookups == [False, True]


@pytest.mark.asyncio
async def test_a_rotated_token_gets_through_within_the_recheck_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """The agency rotated its Twilio Auth Token: the cached old one refuses the first new-token callback only until
    a re-check is due (RECHECK_S), which goes to the database and loads the new token."""
    now = [0.0]
    cache = webhook.HookCache(clock=lambda: now[0], recheck_s=5.0)
    cache.remember(HOOK_ID, OTHER_TOKEN, ACCOUNT_SID)  # the old token
    lookups = _counting(monkeypatch, _conn())
    with pytest.raises(Forbidden):
        await webhook.handle(object(), runtime(), _request(_params()), cache)  # type: ignore[arg-type]
    assert lookups == []
    now[0] = 5.0
    assert await webhook.handle(object(), runtime(), _request(_params()), cache) == "stored"  # type: ignore[arg-type]
    assert lookups == [True]
    # the database loaded the new token: the next genuine callback verifies against the cache at once
    assert cache.precheck(_request(_params()), URL) is True


@pytest.mark.asyncio
async def test_an_unknown_hook_is_forgotten_and_takes_the_unknown_gate_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.errors import NotFound

    cache = webhook.HookCache()
    lookups = _counting(monkeypatch, _conn())
    stray = HookRequest(hook_id="f" * 32, kind="inbound", params=_params(), signature="x")
    cache.remember(stray.hook_id, AUTH_TOKEN, ACCOUNT_SID)  # known once, deleted since
    cache._entries[stray.hook_id].rechecked_at = -10.0  # noqa: SLF001 — a re-check is due
    with pytest.raises(NotFound):
        await webhook.handle(object(), runtime(), stray, cache)  # type: ignore[arg-type]
    assert lookups == [True] and not cache.known(stray.hook_id)
    with pytest.raises(NotFound):
        await webhook.handle(object(), runtime(), stray, cache)  # type: ignore[arg-type]
    assert lookups == [True, False]


def test_the_hook_cache_expires_and_is_bounded() -> None:
    now = [0.0]
    cache = webhook.HookCache(clock=lambda: now[0], ttl_s=10.0, max_entries=2)
    for hook in ("a" * 32, "b" * 32, "c" * 32):
        cache.remember(hook, AUTH_TOKEN, ACCOUNT_SID)
    assert len(cache) == 2 and not cache.known("a" * 32) and cache.known("c" * 32)
    assert cache.precheck(HookRequest("d" * 32, "inbound", [], "x"), URL) is None
    now[0] = 10.0
    assert not cache.known("c" * 32) and len(cache) == 1
    cache.forget("b" * 32)
    assert len(cache) == 0


@pytest.mark.asyncio
async def test_known_hooks_take_their_own_gate_key() -> None:
    from contextlib import asynccontextmanager

    from app import tenant_gate
    from app.tenancy import messaging_hook_tx

    seen: list[tuple[int, int]] = []
    gate = tenant_gate.TenantGate(2, 0.05)
    found = {"messaging_connection_id": CONNECTION, "tenant_id": TENANT}

    class Pool:
        @asynccontextmanager
        async def acquire(self, timeout: float | None = None) -> Any:  # noqa: ASYNC109
            seen.append((gate.in_use(tenant_gate.HOOK_KEY), gate.in_use(tenant_gate.HOOK_UNKNOWN_KEY)))
            conn = ScriptedConn().on("FROM crm.messaging_connection WHERE hook_id", found)
            yield conn.on("SELECT status FROM crm.tenant", "active")

    before = tenant_gate.GATE
    tenant_gate.GATE = gate
    try:
        for known in (True, False):
            async with messaging_hook_tx(Pool(), HOOK_ID, known=known):  # type: ignore[arg-type]
                pass
    finally:
        tenant_gate.GATE = before
    assert seen == [(1, 0), (0, 1)]
