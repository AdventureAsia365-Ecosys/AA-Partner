"""AA-945 send-as providers with fake transports: Gmail / Microsoft Graph over the stdlib HTTP client, SMTP over a fake
server. Failure classes: AuthRevoked (held_reconnect), Rejected (failed), Transient (retried)."""

from __future__ import annotations

import base64
import json
import smtplib
from collections.abc import Callable
from typing import Any

import pytest

from app.mail.providers import AuthRevoked, Rejected, Transient
from app.mail.providers import http as http_module
from app.mail.providers import smtp as smtp_module
from app.mail.providers.http import HttpClient, HttpRequest, HttpResponse
from app.mail.providers.oauth_mail import GOOGLE, MICROSOFT, OAuthMailProvider, id_token_claims
from app.mail.providers.registry import build_providers
from app.mail.providers.smtp import SmtpProvider, connect, raise_if_refused, require_public, validate
from app.settings import Settings

CLIENT = ("client-id", "client-secret")
SECRET = {"refresh_token": "rt-1", "address": "hello@sunshine.example"}


def _id_token(claims: dict[str, Any]) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"h.{body}.s"


class Script:
    """A transport answering by URL substring; records the requests."""

    def __init__(self, answers: dict[str, HttpResponse]) -> None:
        self.answers = answers
        self.requests: list[HttpRequest] = []

    def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        for fragment, response in self.answers.items():
            if fragment in request.url:
                return response
        raise AssertionError(request.url)


def _json(status: int, body: dict[str, Any]) -> HttpResponse:
    return HttpResponse(status, json.dumps(body).encode())


TOKEN_OK = _json(200, {"access_token": "at-1"})


@pytest.mark.asyncio
async def test_gmail_send_refreshes_then_posts_raw() -> None:
    script = Script({"oauth2.googleapis.com": TOKEN_OK, "gmail.googleapis.com": _json(200, {"id": "gm-9"})})
    provider = OAuthMailProvider(GOOGLE, CLIENT, HttpClient(script))
    result = await provider.send(b"raw mime", SECRET, ["lan@example.test"])
    assert result.external_id == "gm-9"
    send = script.requests[1]
    assert send.headers["Authorization"] == "Bearer at-1"
    assert json.loads(send.body or b"{}")["raw"] == base64.urlsafe_b64encode(b"raw mime").decode()
    await provider.check(SECRET)


@pytest.mark.asyncio
async def test_graph_send_posts_mime_and_has_no_id() -> None:
    script = Script({"login.microsoftonline.com": TOKEN_OK, "graph.microsoft.com": HttpResponse(202, b"")})
    provider = OAuthMailProvider(MICROSOFT, CLIENT, HttpClient(script))
    assert (await provider.send(b"raw", SECRET, [])).external_id is None
    assert script.requests[1].body == base64.b64encode(b"raw")
    assert script.requests[1].headers["Content-Type"] == "text/plain"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("token", "send", "error", "code"),
    [
        (_json(400, {"error": "invalid_grant"}), None, AuthRevoked, "auth_revoked"),
        (_json(401, {"error": "invalid_client"}), None, Transient, "token_endpoint"),
        (_json(503, {}), None, Transient, "token_endpoint"),
        (_json(400, {"error": "something"}), None, AuthRevoked, "auth_revoked"),
        (_json(302, {}), None, Transient, "token_endpoint"),
        (TOKEN_OK, HttpResponse(401, b""), AuthRevoked, "auth_revoked"),
        (TOKEN_OK, HttpResponse(403, b""), AuthRevoked, "permission_missing"),
        (TOKEN_OK, HttpResponse(400, b""), Rejected, "provider_rejected"),
        (TOKEN_OK, HttpResponse(500, b""), Transient, "provider_unavailable"),
    ],
)
async def test_failure_classes(token: HttpResponse, send: HttpResponse | None, error: type, code: str) -> None:
    script = Script({"oauth2.googleapis.com": token, "gmail.googleapis.com": send or HttpResponse(200, b"{}")})
    with pytest.raises(error) as err:
        await OAuthMailProvider(GOOGLE, CLIENT, HttpClient(script)).send(b"raw", SECRET, [])
    assert err.value.code == code


@pytest.mark.asyncio
async def test_missing_client_or_refresh_token() -> None:
    with pytest.raises(Transient):
        await OAuthMailProvider(GOOGLE, None, HttpClient(Script({}))).check(SECRET)
    with pytest.raises(AuthRevoked):
        await OAuthMailProvider(GOOGLE, CLIENT, HttpClient(Script({}))).check({"address": "x@y.test"})


@pytest.mark.asyncio
async def test_exchange_code_reads_the_address_from_the_id_token() -> None:
    good = _json(200, {"refresh_token": "rt-2", "id_token": _id_token({"email": "Hello@Sunshine.Example"})})
    provider = OAuthMailProvider(MICROSOFT, CLIENT, HttpClient(Script({"login.microsoftonline.com": good})))
    assert await provider.exchange_code("code", "verifier", "https://p.test/cb") == {
        "refresh_token": "rt-2",
        "address": "hello@sunshine.example",
    }
    for answer, code in (
        (_json(400, {"error": "invalid_grant"}), "oauth_code_rejected"),
        (_json(200, {"refresh_token": "r", "id_token": _id_token({"sub": "x"})}), "oauth_no_address"),
    ):
        p = OAuthMailProvider(GOOGLE, CLIENT, HttpClient(Script({"oauth2.googleapis.com": answer})))
        with pytest.raises(Rejected) as err:
            await p.exchange_code("c", "v", "https://p.test/cb")
        assert err.value.code == code
    assert id_token_claims("bad") == {} and id_token_claims("a.!!!.c") == {}
    assert id_token_claims("a." + base64.urlsafe_b64encode(b"[1]").decode() + ".c") == {}


def test_http_response_json_and_urllib_refuses_plain_http(monkeypatch: pytest.MonkeyPatch) -> None:
    assert HttpResponse(200, b"not json").json() == {} and HttpResponse(200, b"[1]").json() == {}
    with pytest.raises(ValueError):
        http_module.urllib_transport(HttpRequest("GET", "http://x.test", {}, None))

    def boom(*_: Any, **__: Any) -> Any:
        raise http_module.urllib.error.URLError("down")

    monkeypatch.setattr(http_module.urllib.request, "urlopen", boom)
    with pytest.raises(Transient):
        http_module.urllib_transport(HttpRequest("GET", "https://x.test", {}, None))


class FakeServer:
    def __init__(self, *, login: Exception | None = None, send: Exception | None = None, refused: Any = None) -> None:
        self.login_error = login
        self.send_error = send
        self.refused = refused or {}
        self.sent: list[tuple[str, list[str], bytes]] = []
        self.quit_called = False

    def login(self, user: str, password: str) -> None:
        if self.login_error:
            raise self.login_error

    def sendmail(self, sender: str, rcpt: list[str], raw: bytes) -> dict[str, Any]:
        if self.send_error:
            raise self.send_error
        self.sent.append((sender, rcpt, raw))
        return self.refused

    def noop(self) -> tuple[int, bytes]:
        return (250, b"OK")

    def quit(self) -> None:
        self.quit_called = True
        raise smtplib.SMTPServerDisconnected("already closed")


SMTP_SECRET = {
    "host": "smtp.sunshine.example",
    "port": 587,
    "security": "starttls",
    "username": "hello",
    "password": "app-password",
    "address": "hello@sunshine.example",
}


def _factory(server: FakeServer | Exception) -> Callable[[dict[str, Any]], Any]:
    def make(_secret: dict[str, Any]) -> Any:
        if isinstance(server, Exception):
            raise server
        return server

    return make


@pytest.mark.asyncio
async def test_smtp_send_and_check() -> None:
    server = FakeServer()
    provider = SmtpProvider(_factory(server))
    await provider.send(b"raw", SMTP_SECRET, ["lan@example.test"])
    assert server.sent == [("hello@sunshine.example", ["lan@example.test"], b"raw")] and server.quit_called
    await provider.check(SMTP_SECRET)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("server", "error", "code"),
    [
        (FakeServer(login=smtplib.SMTPAuthenticationError(535, b"bad")), AuthRevoked, "auth_revoked"),
        (FakeServer(send=smtplib.SMTPRecipientsRefused({})), Rejected, "provider_rejected"),
        (FakeServer(send=smtplib.SMTPDataError(552, b"too big")), Rejected, "provider_rejected"),
        (FakeServer(send=smtplib.SMTPDataError(451, b"later")), Transient, "smtp_temporary"),
        (FakeServer(send=OSError("reset")), Transient, "smtp_unreachable"),
        (FakeServer(refused={"x@y.test": (550, b"no")}), Rejected, "provider_rejected"),
        (OSError("refused"), Transient, "smtp_unreachable"),
    ],
)
async def test_smtp_failures(server: Any, error: type, code: str) -> None:
    with pytest.raises(error) as err:
        await SmtpProvider(_factory(server)).send(b"raw", SMTP_SECRET, ["lan@example.test"])
    assert err.value.code == code


@pytest.mark.asyncio
async def test_smtp_validation() -> None:
    with pytest.raises(Rejected):
        await SmtpProvider(_factory(FakeServer())).send(b"raw", SMTP_SECRET, [])
    for change, code in (
        ({"host": "bad host"}, "smtp_invalid_host"),
        ({"port": 0}, "smtp_invalid_port"),
        ({"port": 22}, "smtp_invalid_port"),  # only 25 / 465 / 587 / 2525 are dialled
        ({"port": 6379}, "smtp_invalid_port"),
        ({"security": "none"}, "smtp_tls_required"),
        ({"password": ""}, "smtp_missing_password"),
    ):
        with pytest.raises(Rejected) as err:
            validate(SMTP_SECRET | change)
        assert err.value.code == code
    raise_if_refused({})


def test_registry_builds_the_three_kinds() -> None:
    providers = build_providers(Settings(environment="test", db_ssl="disable"))
    assert set(providers) == {"gmail", "microsoft", "smtp"}


def test_smtp_host_must_be_public() -> None:
    """SSRF guard: an agency-named SMTP host never points the task into the VPC or at instance metadata."""
    assert require_public("smtp.example.test", 587, lambda h, p: ["93.184.216.34"]) == "93.184.216.34"
    for private in ("10.2.10.5", "127.0.0.1", "169.254.169.254", "fd00::1", "224.0.0.1"):
        with pytest.raises(Rejected):
            require_public("smtp.example.test", 587, lambda h, p, ip=private: [ip])
    with pytest.raises(Transient):
        require_public("smtp.example.test", 587, lambda h, p: [])

    def fail(h: str, p: int) -> list[str]:
        raise OSError("no such host")

    with pytest.raises(Transient):
        require_public("nope.example.test", 587, fail)


@pytest.mark.parametrize("security", ["starttls", "tls"])
def test_smtp_connects_to_the_vetted_address_not_a_second_dns_answer(
    monkeypatch: pytest.MonkeyPatch, security: str
) -> None:
    """Review S250 (DNS rebinding): the name is resolved once; a resolver that answers public then private never gets
    the socket into the VPC or 169.254.x, because the socket opens to the first, vetted address."""
    answers = iter([["93.184.216.34"], ["169.254.169.254"]])
    resolved: list[str] = []

    def resolver(host: str, port: int) -> list[str]:
        resolved.append(host)
        return next(answers)

    dialled: list[tuple[str, int]] = []

    def create_connection(address: tuple[str, int], *_: Any, **__: Any) -> Any:
        dialled.append(address)
        raise OSError("stop here")

    monkeypatch.setattr(smtp_module.socket, "create_connection", create_connection)
    with pytest.raises(OSError):
        connect(SMTP_SECRET | {"security": security, "port": 465 if security == "tls" else 587}, resolver)
    assert resolved == ["smtp.sunshine.example"]
    assert dialled == [("93.184.216.34", 465 if security == "tls" else 587)]


def test_implicit_tls_keeps_the_host_name_for_sni(monkeypatch: pytest.MonkeyPatch) -> None:
    wrapped: list[str | None] = []

    class Ctx:
        def wrap_socket(self, sock: Any, server_hostname: str | None = None) -> Any:
            wrapped.append(server_hostname)
            raise OSError("stop after the handshake call")

    monkeypatch.setattr(smtp_module.socket, "create_connection", lambda *a, **k: object())
    monkeypatch.setattr(smtp_module.ssl, "create_default_context", Ctx)
    with pytest.raises(OSError):
        connect(SMTP_SECRET | {"security": "tls", "port": 465}, lambda h, p: ["93.184.216.34"])
    assert wrapped == ["smtp.sunshine.example"]


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoints", [GOOGLE, MICROSOFT])
async def test_a_rotated_refresh_token_is_handed_back(endpoints: Any) -> None:
    """Review S250: Microsoft rotates the refresh token on every refresh (90-day lifetime); keeping the first one
    would disconnect every Microsoft 365 mailbox after 90 days."""
    rotated = _json(200, {"access_token": "at-2", "refresh_token": "rt-2"})
    script = Script({"login.microsoftonline.com": rotated, "oauth2.googleapis.com": rotated})
    script.answers |= {"graph.microsoft.com": HttpResponse(202, b""), "gmail.googleapis.com": _json(200, {"id": "g"})}
    provider = OAuthMailProvider(endpoints, CLIENT, HttpClient(script))
    result = await provider.send(b"raw", SECRET, ["lan@example.test"])
    assert result.rotated_secret == SECRET | {"refresh_token": "rt-2"}
    assert await provider.check(SECRET) == SECRET | {"refresh_token": "rt-2"}
    same = Script({"login.microsoftonline.com": _json(200, {"access_token": "a", "refresh_token": "rt-1"})})
    same.answers["oauth2.googleapis.com"] = TOKEN_OK
    assert await OAuthMailProvider(endpoints, CLIENT, HttpClient(same)).check(SECRET) is None
