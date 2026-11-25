"""AA-1011 part 3: the calendar OAuth connect and the provider calendar APIs (:mod:`app.calendar.oauth_sync`), on a
scripted HTTPS transport (no network). The SQL, the vault and RLS run on PostgreSQL in
tests/integration/test_aa1011_calendar_db.py."""

from __future__ import annotations

import base64
import datetime as dt
import json
import time
import urllib.parse
import uuid
from typing import Any

import pytest

from app.calendar import oauth_sync
from app.calendar.oauth_sync import GoogleCalendar, GraphCalendar, PushEvent, RemoteEvent
from app.errors import Conflict, Invalid, Unavailable
from app.mail import oauth as mail_oauth
from app.mail.providers import AuthRevoked, Rejected, Transient
from app.mail.providers.http import HttpClient, HttpRequest, HttpResponse
from app.settings import Settings
from tests.unit.mail_fakes import MailVault
from tests.unit.slice_fakes import MEMBER, OTHER_MEMBER, tenant_ctx

CONN = uuid.UUID("00000000-0000-4000-8000-00000000c011")
DAY = dt.date(2030, 3, 14)
AT = dt.datetime(2030, 3, 14, 2, 30, tzinfo=dt.UTC)


def _settings(**over: Any) -> Settings:
    base: dict[str, Any] = dict(
        environment="test",
        db_ssl="disable",
        mail_oauth_redirect_base="https://partner.test",
        mail_oauth=(("google_client_id", "gid"), ("google_client_secret", "gsecret")),
    )
    return Settings(**(base | over))


def _resp(status: int, body: dict[str, Any] | None = None) -> HttpResponse:
    return HttpResponse(status, json.dumps(body or {}).encode())


class Script:
    """A transport answering in order per (method, URL fragment); records every request."""

    def __init__(self, answers: list[tuple[str, str, HttpResponse]]) -> None:
        self.answers = list(answers)
        self.requests: list[HttpRequest] = []

    def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        for i, (method, fragment, response) in enumerate(self.answers):
            if method == request.method and fragment in request.url:
                del self.answers[i]
                return response
        raise AssertionError(f"{request.method} {request.url}")

    def body(self, i: int) -> dict[str, Any]:
        return json.loads(self.requests[i].body or b"{}")


def _event(key: str = "departure-t1", all_day: bool = True) -> PushEvent:
    return PushEvent(
        key=key,
        summary="Departure · trip t1",
        description="Open the trip in the CRM: https://partner.test/pipeline/t1",
        all_day=all_day,
        day=DAY,
        starts_at=None if all_day else AT,
        ends_at=None if all_day else AT + dt.timedelta(minutes=15),
        timezone="Asia/Ho_Chi_Minh",
    )


def _state(url: str) -> str:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))["state"]


# ── OAuth ────────────────────────────────────────────────────────────────────────────────────────


class _Exchange:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def exchange_code(self, code: str, verifier: str, redirect: str) -> dict[str, str]:
        assert redirect == "https://partner.test/connect/mail/callback" and len(verifier) > 40
        self.calls.append(code)
        return {"refresh_token": "rt-cal", "address": "lan@example.test"}


class _Client:
    def __init__(self, oauth: Any = None, configured: bool = True) -> None:
        self.oauth = oauth or _Exchange()
        self.configured = configured


@pytest.mark.asyncio
async def test_calendar_consent_asks_only_the_calendar_scope_and_reuses_the_mail_callback() -> None:
    ctx = tenant_ctx(role="agent")
    vault = MailVault()
    started = await oauth_sync.start(ctx, vault, _settings(), "google", "mine")
    url = urllib.parse.urlsplit(started["authorize_url"])
    q = dict(urllib.parse.parse_qsl(url.query))
    assert q["scope"] == "openid email https://www.googleapis.com/auth/calendar.events"
    assert q["redirect_uri"] == "https://partner.test/connect/mail/callback"
    assert q["state"].startswith("cal.") and q["code_challenge_method"] == "S256" and q["access_type"] == "offline"
    assert "gmail" not in q["scope"]
    done = await oauth_sync.complete(ctx, vault, _settings(), {"google": _Client()}, "code-1", q["state"])
    assert (done.provider, done.scope, done.secret["address"]) == ("google", "mine", "lan@example.test")
    assert [a.action for a in ctx.audit_events] == ["calendar_connect_start"]


@pytest.mark.asyncio
async def test_calendar_and_mailbox_states_never_cross() -> None:
    ctx = tenant_ctx()
    vault = MailVault()
    cal = _state((await oauth_sync.start(ctx, vault, _settings(), "google", "agency"))["authorize_url"])
    mail = _state((await mail_oauth.start(ctx, vault, _settings(), "google", "own"))["authorize_url"])
    with pytest.raises(Invalid):
        await mail_oauth.complete(ctx, vault, _settings(), {"gmail": _Exchange()}, "c", cal)
    with pytest.raises(Invalid):
        await oauth_sync.complete(ctx, vault, _settings(), {"google": _Client()}, "c", mail)


@pytest.mark.asyncio
async def test_state_checks() -> None:
    ctx = tenant_ctx()
    vault = MailVault()
    q = (await oauth_sync.start(ctx, vault, _settings(), "google", "mine"))["authorize_url"]
    state = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(q).query))["state"]
    with pytest.raises(Invalid):  # another member's state
        await oauth_sync.complete(
            tenant_ctx(member=OTHER_MEMBER), vault, _settings(), {"google": _Client()}, "c", state
        )
    payload = {"p": "google", "s": "mine", "m": str(MEMBER), "e": int(time.time()) - 1, "v": "v"}
    sealed = f"enc:oauth_state.calendar_state:{json.dumps(payload)}".encode()
    expired = "cal." + base64.urlsafe_b64encode(sealed).decode().rstrip("=")
    with pytest.raises(Conflict, match="again"):
        await oauth_sync.complete(ctx, vault, _settings(), {"google": _Client()}, "c", expired)
    bad = "cal." + base64.urlsafe_b64encode(b"enc:oauth_state.calendar_state:not json").decode()
    with pytest.raises(Invalid):
        await oauth_sync.complete(ctx, vault, _settings(), {"google": _Client()}, "c", bad)
    with pytest.raises(Invalid):
        await oauth_sync.complete(ctx, vault, _settings(), {"google": _Client()}, "", state)
    with pytest.raises(Unavailable):
        await oauth_sync.complete(ctx, vault, _settings(), {}, "c", state)

    class Refusing:
        async def exchange_code(self, *_: Any) -> Any:
            raise Rejected("oauth_code_rejected")

    with pytest.raises(Conflict):
        await oauth_sync.complete(ctx, vault, _settings(), {"google": _Client(Refusing())}, "c", state)


@pytest.mark.asyncio
async def test_start_refusals_microsoft_answers_503_until_the_entra_values_exist() -> None:
    ctx = tenant_ctx()
    with pytest.raises(Unavailable, match="microsoft"):
        await oauth_sync.start(ctx, MailVault(), _settings(), "microsoft", "mine")
    with pytest.raises(Invalid):
        await oauth_sync.start(ctx, MailVault(), _settings(), "apple", "mine")
    with pytest.raises(Invalid):
        await oauth_sync.start(ctx, MailVault(), _settings(), "google", "team")
    ms = _settings(mail_oauth=(("ms_client_id", "mid"), ("ms_client_secret", "msecret")))
    url = (await oauth_sync.start(ctx, MailVault(), ms, "microsoft", "mine"))["authorize_url"]
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert "Calendars.ReadWrite" in q["scope"] and "offline_access" in q["scope"] and q["prompt"] == "select_account"


@pytest.mark.asyncio
async def test_secret_round_trip() -> None:
    vault = MailVault()
    blob = await oauth_sync.seal_secret(vault, CONN, {"refresh_token": "rt", "address": "a@example.test"})
    assert await oauth_sync.open_secret(vault, CONN, blob) == {"refresh_token": "rt", "address": "a@example.test"}
    assert await oauth_sync.open_secret(vault, CONN, b"secret:[1]") == {}


def test_build_clients_follow_the_configured_pairs() -> None:
    clients = oauth_sync.build_clients(_settings())
    assert clients["google"].configured and not clients["microsoft"].configured
    assert isinstance(clients["google"], GoogleCalendar) and isinstance(clients["microsoft"], GraphCalendar)


# ── Events ───────────────────────────────────────────────────────────────────────────────────────


def test_event_id_is_deterministic_base32hex() -> None:
    first = oauth_sync.event_id(CONN, "departure-t1")
    assert first == oauth_sync.event_id(CONN, "departure-t1") != oauth_sync.event_id(CONN, "departure-t2")
    assert set(first) <= set(oauth_sync.BASE32HEX) and 5 <= len(first) <= 1024


def test_digest_changes_with_what_is_written() -> None:
    assert _event().digest() == _event().digest()
    assert _event().digest() != _event(all_day=False).digest()


# ── Google ───────────────────────────────────────────────────────────────────────────────────────


def _google(answers: list[tuple[str, str, HttpResponse]]) -> tuple[GoogleCalendar, Script]:
    script = Script(answers)
    return GoogleCalendar(oauth_sync.CAL_GOOGLE, ("gid", "gs"), HttpClient(script)), script


@pytest.mark.asyncio
async def test_google_lists_only_our_events_page_by_page() -> None:
    page1 = {"items": [{"id": "e1", "extendedProperties": {"private": {"aaCrmKey": "task-1", "aaCrmHash": "h1"}}},
                       {"id": "other"}], "nextPageToken": "p2"}  # fmt: skip
    page2 = {"items": [{"id": "e2", "extendedProperties": {"private": {"aaCrmKey": "departure-t1"}}}]}
    client, script = _google([("GET", "events?", _resp(200, page1)), ("GET", "pageToken=p2", _resp(200, page2))])
    since = dt.datetime(2030, 3, 1, tzinfo=dt.UTC)
    got = await client.list_ours("at", CONN, since, since + dt.timedelta(days=30))
    assert got == [RemoteEvent("e1", "task-1", "h1"), RemoteEvent("e2", "departure-t1", "None")]
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(script.requests[0].url).query))
    assert q["privateExtendedProperty"] == f"aaCrm={CONN}" and q["timeMin"] == "2030-03-01T00:00:00Z"
    assert script.requests[0].headers["Authorization"] == "Bearer at"


@pytest.mark.asyncio
async def test_google_create_is_idempotent_by_event_id() -> None:
    client, script = _google([("POST", "events", _resp(200, {"id": "x"})), ("POST", "events", _resp(409)),
                              ("PUT", "events/aacrm", _resp(200))])  # fmt: skip
    first = await client.create("at", CONN, _event())
    again = await client.create("at", CONN, _event())
    assert first == again == oauth_sync.event_id(CONN, "departure-t1")
    body = script.body(0)
    assert body["id"] == first and body["start"] == {"date": "2030-03-14"} and body["end"] == {"date": "2030-03-15"}
    assert body["extendedProperties"]["private"] == {"aaCrm": str(CONN), "aaCrmKey": "departure-t1",
                                                     "aaCrmHash": _event().digest()}  # fmt: skip
    assert body["transparency"] == "transparent"
    assert script.requests[2].method == "PUT" and script.body(2)["status"] == "confirmed"


@pytest.mark.asyncio
async def test_google_timed_update_delete_and_revoke() -> None:
    client, script = _google([("PUT", "events/e1", _resp(200)), ("DELETE", "events/e1", _resp(204)),
                              ("DELETE", "events/e2", _resp(410)), ("POST", "revoke", _resp(200))])  # fmt: skip
    await client.update("at", CONN, "e1", _event("task-1", all_day=False))
    assert script.body(0)["start"] == {"dateTime": "2030-03-14T02:30:00Z", "timeZone": "UTC"}
    await client.delete("at", "e1")
    await client.delete("at", "e2")  # already gone
    assert await client.revoke({"refresh_token": "rt"}) is True
    assert urllib.parse.parse_qs(script.requests[3].body.decode())["token"] == ["rt"]  # type: ignore[union-attr]
    assert await client.revoke({}) is True


@pytest.mark.parametrize(
    ("status", "body", "error", "code"),
    [
        (401, {}, AuthRevoked, "auth_revoked"),
        (403, {"error": {"errors": [{"reason": "insufficientPermissions"}]}}, AuthRevoked, "permission_missing"),
        (403, {"error": {"errors": [{"reason": "rateLimitExceeded"}]}}, Transient, "rate_limited"),
        (429, {}, Transient, "rate_limited"),
        (400, {}, Rejected, "vendor_rejected"),
        (503, {}, Transient, "unreachable"),
    ],
)
@pytest.mark.asyncio
async def test_failure_classes(status: int, body: dict[str, Any], error: type, code: str) -> None:
    client, _ = _google([("DELETE", "events", _resp(status, body))])
    with pytest.raises(error) as exc:
        await client.delete("at", "e1")
    assert exc.value.code == code  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_google_list_and_write_failures_raise() -> None:
    client, _ = _google([("GET", "events?", _resp(401)), ("POST", "events", _resp(500)), ("PUT", "events", _resp(403))])
    with pytest.raises(AuthRevoked):
        await client.list_ours("at", CONN, None, None)
    with pytest.raises(Transient):
        await client.create("at", CONN, _event())
    with pytest.raises(AuthRevoked):
        await client.update("at", CONN, "e1", _event())


# ── Microsoft Graph ──────────────────────────────────────────────────────────────────────────────


def _graph(answers: list[tuple[str, str, HttpResponse]]) -> tuple[GraphCalendar, Script]:
    script = Script(answers)
    return GraphCalendar(oauth_sync.CAL_MICROSOFT, ("mid", "ms"), HttpClient(script)), script


def _item(event_id: str, key: str, start: str = "2030-03-14T00:00:00.0000000") -> dict[str, Any]:
    return {"id": event_id, "start": {"dateTime": start, "timeZone": "UTC"},
            "singleValueExtendedProperties": [{"id": oauth_sync.GRAPH_KEY, "value": key},
                                              {"id": oauth_sync.GRAPH_HASH, "value": "h"}]}  # fmt: skip


@pytest.mark.asyncio
async def test_graph_lists_ours_in_the_window_and_follows_next_link() -> None:
    page1 = {"value": [_item("g1", "task-1"), _item("old", "task-0", "2029-01-01T00:00:00"), {"id": "x"}],
             "@odata.nextLink": oauth_sync.GRAPH_EVENTS_URL + "?$skip=100"}  # fmt: skip
    page2 = {"value": [_item("g2", "departure-t1", "bad"), _item("g3", "task-9", "2031-01-01T00:00:00")],
             "@odata.nextLink": "https://elsewhere.test/next"}  # fmt: skip
    client, script = _graph([("GET", "events?%24filter", _resp(200, page1)), ("GET", "skip=100", _resp(200, page2))])
    since = dt.datetime(2030, 3, 1, tzinfo=dt.UTC)
    got = await client.list_ours("at", CONN, since, since + dt.timedelta(days=60))
    assert [(r.event_id, r.key) for r in got] == [("g1", "task-1"), ("g2", "departure-t1")]
    assert str(CONN) in urllib.parse.unquote(script.requests[0].url)
    assert len(script.requests) == 2  # a next link to another host is never followed


@pytest.mark.asyncio
async def test_graph_create_update_and_no_revoke_endpoint() -> None:
    client, script = _graph([("POST", "me/events", _resp(201, {"id": "g9"})), ("PATCH", "me/events/g9", _resp(200)),
                             ("POST", "me/events", _resp(201, {})), ("GET", "me/events", _resp(403))])  # fmt: skip
    assert await client.create("at", CONN, _event()) == "g9"
    body = script.body(0)
    assert body["transactionId"] == oauth_sync.event_id(CONN, "departure-t1") and body["isAllDay"] is True
    assert body["start"] == {"dateTime": "2030-03-14T00:00:00", "timeZone": "Asia/Ho_Chi_Minh"}
    assert {p["id"] for p in body["singleValueExtendedProperties"]} == {
        oauth_sync.GRAPH_TAG, oauth_sync.GRAPH_KEY, oauth_sync.GRAPH_HASH}  # fmt: skip
    await client.update("at", CONN, "g9", _event("task-1", all_day=False))
    assert script.body(1)["start"] == {"dateTime": "2030-03-14T02:30:00", "timeZone": "UTC"}
    with pytest.raises(Transient):  # a create answer without an id
        await client.create("at", CONN, _event())
    with pytest.raises(AuthRevoked):
        await client.list_ours("at", CONN, None, None)
    assert await client.revoke({"refresh_token": "rt"}) is False  # Graph has no per-token revoke
