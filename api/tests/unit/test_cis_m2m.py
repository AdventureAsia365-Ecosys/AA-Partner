"""app.cis.m2m and app.cis.http (AA-978): the M2M_CLIENT secret, the token cache, the stdlib HTTPS client."""

from __future__ import annotations

import base64
import json
import threading
import urllib.parse
from collections.abc import Iterator, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.cis import http
from app.cis.http import HttpError, HttpResponse
from app.cis.m2m import CredentialsError, M2mCredentials, TokenError, TokenProvider, parse_credentials

CREDS = M2mCredentials("abc123", "s3cret-value", "https://m2m-auth.example.invalid/oauth2/token")


def _secret(**changes: object) -> str:
    data: dict[str, object] = {
        "client_id": "abc123",
        "client_secret": "s3cret-value",
        "token_url": "https://m2m-auth.example.invalid/oauth2/token",
        "scopes": ["booking.b2b/read", "cis.tenant/read"],
    }
    data.update(changes)
    return json.dumps(data)


class FakeTransport:
    def __init__(self, *responses: HttpResponse | Exception) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

    async def __call__(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout_s: float
    ) -> HttpResponse:
        self.calls.append((method, url, dict(headers), body))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _token_response(value: str, expires_in: int = 21600) -> HttpResponse:
    body = json.dumps({"access_token": value, "expires_in": expires_in, "token_type": "Bearer"}).encode()
    return HttpResponse(200, body)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_the_secret_json_is_parsed() -> None:
    creds = parse_credentials(_secret())
    assert creds == CREDS
    assert "s3cret" not in repr(creds)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("not json", "JSON object"),
        ("[1]", "JSON object"),
        (_secret(client_secret=""), "client_secret"),
        (_secret(client_id=None, token_url=7), "client_id, token_url"),
        (_secret(client_id="has-dash"), "client_id"),
        (_secret(token_url="http://m2m-auth.example.invalid/oauth2/token"), "https"),
    ],
)
def test_a_bad_secret_is_refused_without_echoing_it(raw: str, message: str) -> None:
    with pytest.raises(CredentialsError, match=message) as err:
        parse_credentials(raw)
    assert "s3cret-value" not in str(err.value)


def test_http_token_url_only_on_test_and_local() -> None:
    assert parse_credentials(_secret(token_url="http://127.0.0.1:9/token"), allow_http=True).token_url.startswith(
        "http:"
    )


@pytest.mark.asyncio
async def test_a_token_is_asked_for_one_scope_and_cached_until_five_minutes_before_expiry() -> None:
    transport, clock = FakeTransport(_token_response("tok-1"), _token_response("tok-2")), Clock()
    tokens = TokenProvider(CREDS, transport=transport, clock=clock)
    assert await tokens.token("cis.tenant/read") == "tok-1"
    method, url, headers, body = transport.calls[0]
    assert (method, url) == ("POST", CREDS.token_url)
    assert headers["Authorization"] == "Basic " + base64.b64encode(b"abc123:s3cret-value").decode()
    assert urllib.parse.parse_qs((body or b"").decode()) == {
        "grant_type": ["client_credentials"],
        "scope": ["cis.tenant/read"],
    }
    clock.now += 21600 - 301
    assert await tokens.token("cis.tenant/read") == "tok-1"
    clock.now += 2
    assert await tokens.token("cis.tenant/read") == "tok-2"
    assert len(transport.calls) == 2


@pytest.mark.asyncio
async def test_each_scope_has_its_own_token_and_invalidate_forgets_one() -> None:
    transport = FakeTransport(_token_response("a"), _token_response("b"), _token_response("c"))
    tokens = TokenProvider(CREDS, transport=transport, clock=Clock())
    assert await tokens.token("cis.tenant/read") == "a"
    assert await tokens.token("cis.catalog/read") == "b"
    tokens.invalidate("cis.tenant/read")
    assert await tokens.token("cis.tenant/read") == "c"
    assert await tokens.token("cis.catalog/read") == "b"


@pytest.mark.asyncio
async def test_a_short_token_is_refreshed_half_way() -> None:
    transport, clock = FakeTransport(_token_response("a", 120), _token_response("b", 120)), Clock()
    tokens = TokenProvider(CREDS, transport=transport, clock=clock)
    await tokens.token("s")
    clock.now += 59
    assert await tokens.token("s") == "a"
    clock.now += 2
    assert await tokens.token("s") == "b"


@pytest.mark.parametrize(
    ("response", "error_class"),
    [
        (HttpResponse(400, b'{"error":"invalid_client"}'), "token_unauthorized"),
        (HttpResponse(401, b""), "token_unauthorized"),
        (HttpResponse(503, b""), "token_unavailable"),
        (HttpError("timeout"), "token_unavailable"),
        (HttpResponse(200, b"not json"), "token_invalid"),
        (HttpResponse(200, b"{}"), "token_invalid"),
        (HttpResponse(200, b'{"access_token": "", "expires_in": 60}'), "token_invalid"),
        (HttpResponse(200, b'{"access_token": "t", "token_type": "mac"}'), "token_invalid"),
    ],
)
@pytest.mark.asyncio
async def test_token_errors_carry_a_class_only(response: HttpResponse | Exception, error_class: str) -> None:
    tokens = TokenProvider(CREDS, transport=FakeTransport(response), clock=Clock())
    with pytest.raises(TokenError) as err:
        await tokens.token("cis.tenant/read")
    assert err.value.error_class == error_class
    assert "s3cret" not in str(err.value)


# ── app.cis.http against a local server ────────────────────────────────────────────────────────


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:  # quiet
        pass

    def do_GET(self) -> None:  # noqa: N802 — http.server's name
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "https://elsewhere.example.invalid/")
            self.end_headers()
            return
        if self.path == "/short":  # promises more than it sends: http.client.IncompleteRead on read
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.write(b"x" * 10)
            return
        if self.path == "/big":
            body = b"x" * (http.MAX_BODY_BYTES + 1)
        elif self.path == "/missing":
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"error":"not_found"}')
            return
        else:
            body = json.dumps({"auth": self.headers.get("Authorization")}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def server() -> Iterator[str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.asyncio
async def test_a_get_returns_status_and_body(server: str) -> None:
    resp = await http.urllib_transport("GET", f"{server}/ok", {"Authorization": "Bearer t"}, None, 5)
    assert resp.status == 200 and json.loads(resp.body) == {"auth": "Bearer t"}
    missing = await http.urllib_transport("GET", f"{server}/missing", {}, None, 5)
    assert missing.status == 404 and b"not_found" in missing.body


@pytest.mark.asyncio
async def test_a_redirect_is_returned_not_followed(server: str) -> None:
    resp = await http.urllib_transport("GET", f"{server}/redirect", {"Authorization": "Bearer t"}, None, 5)
    assert resp.status == 302


@pytest.mark.asyncio
async def test_an_oversized_body_and_an_unreachable_host_are_errors(server: str) -> None:
    with pytest.raises(HttpError, match="response_too_large"):
        await http.urllib_transport("GET", f"{server}/big", {}, None, 5)
    with pytest.raises(HttpError, match="unreachable"):
        await http.urllib_transport("GET", "http://127.0.0.1:9/x", {}, None, 2)
    with pytest.raises(HttpError, match="bad_url"):
        await http.urllib_transport("GET", "file:///etc/passwd", {}, None, 2)


@pytest.mark.asyncio
async def test_a_truncated_body_is_an_error(server: str) -> None:
    """Review round 1: a body cut short is a transport error (HttpError), never a partial body or an escaped
    http.client exception (IncompleteRead is no OSError; urllib's read(n) does not raise it at all)."""
    with pytest.raises(HttpError, match="incomplete_body"):
        await http.urllib_transport("GET", f"{server}/short", {}, None, 5)


@pytest.mark.asyncio
async def test_an_http_client_exception_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A chunked body cut short (IncompleteRead) or a bad status line: http.client errors that are no OSError."""
    import http.client as http_client

    def broken(*_args: object, **_kwargs: object) -> None:
        raise http_client.IncompleteRead(b"")

    monkeypatch.setattr(http._OPENER, "open", broken)
    with pytest.raises(HttpError, match="unreachable"):
        await http.urllib_transport("GET", "https://api-cis.example.invalid/x", {}, None, 2)
