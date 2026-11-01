"""AA-958 text-message providers and the webhook signature: the Twilio adapter over a fake HTTPS transport (send for
SMS / WhatsApp / templates, the error codes, the account read, the Content API pages), the deterministic sandbox stub,
which providers an environment gets, and the X-Twilio-Signature HMAC."""

from __future__ import annotations

import base64
import json
import urllib.parse
from typing import Any

import pytest

from app.mail.providers.http import HttpRequest, HttpResponse
from app.messaging import providers, signature
from app.messaging.providers import AuthRevoked, Rejected, SandboxProvider, TextOut, Transient, TwilioProvider
from tests.unit.messaging_fakes import ACCOUNT_SID, AUTH_TOKEN, CLIENT_PHONE, CONTENT_SID

SECRET = {"account_sid": ACCOUNT_SID, "auth_token": AUTH_TOKEN}
SID = "SM" + "c3" * 16


class Transport:
    def __init__(self, *responses: HttpResponse) -> None:
        self.responses = list(responses)
        self.requests: list[HttpRequest] = []

    def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        return self.responses.pop(0)


def ok(data: dict[str, Any], status: int = 201) -> HttpResponse:
    return HttpResponse(status=status, body=json.dumps(data).encode())


def _form(request: HttpRequest) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl((request.body or b"").decode()))


def _out(**over: Any) -> TextOut:
    base: dict[str, Any] = {
        "channel": "sms",
        "to": CLIENT_PHONE,
        "sender": "+61491570157",
        "status_callback": "https://go.crm.example.test/crm/public/v1/hooks/twilio/x/status",
        "reference": "m1",
        "body": "Hello",
    }
    return TextOut(**(base | over))


@pytest.mark.asyncio
async def test_twilio_sends_sms_and_whatsapp() -> None:
    transport = Transport(ok({"sid": SID, "status": "queued"}), ok({"sid": SID, "status": "accepted"}))
    provider = TwilioProvider(transport)
    sent = await provider.send(SECRET, _out())
    assert (sent.sid, sent.status) == (SID, "queued")
    first = transport.requests[0]
    assert first.url.endswith(f"/Accounts/{ACCOUNT_SID}/Messages.json")
    assert (
        first.headers["Authorization"] == "Basic " + base64.b64encode(f"{ACCOUNT_SID}:{AUTH_TOKEN}".encode()).decode()
    )
    assert _form(first) == {
        "To": CLIENT_PHONE,
        "From": "+61491570157",
        "Body": "Hello",
        "StatusCallback": "https://go.crm.example.test/crm/public/v1/hooks/twilio/x/status",
    }
    await provider.send(
        SECRET | {"api_key_sid": "SK" + "1" * 32, "api_key_secret": "s" * 32},
        _out(channel="whatsapp", body=None, content_sid=CONTENT_SID, variables={"1": "Lan"}),
    )
    second = transport.requests[1]
    form = _form(second)
    assert form["To"] == f"whatsapp:{CLIENT_PHONE}" and form["From"] == "whatsapp:+61491570157"
    assert form["ContentSid"] == CONTENT_SID and json.loads(form["ContentVariables"]) == {"1": "Lan"}
    assert "Body" not in form
    assert second.headers["Authorization"].startswith("Basic ") and AUTH_TOKEN not in second.headers["Authorization"]


@pytest.mark.asyncio
async def test_a_messaging_service_sid_is_sent_as_the_service() -> None:
    transport = Transport(ok({"sid": SID, "status": "queued"}))
    await TwilioProvider(transport).send(SECRET, _out(sender="MG" + "2" * 32))
    form = _form(transport.requests[0])
    assert form["MessagingServiceSid"] == "MG" + "2" * 32 and "From" not in form


@pytest.mark.parametrize(
    ("status", "body", "error", "code"),
    [
        (401, {"code": 20003}, AuthRevoked, "auth_revoked"),
        (400, {"code": 20003}, AuthRevoked, "auth_revoked"),
        (429, {}, Transient, "rate_limited"),
        (503, {}, Transient, "unreachable"),
        (400, {"code": 63016}, Rejected, "window_closed"),
        (400, {"code": 21610}, Rejected, "recipient_opted_out"),
        (400, {"code": 21211}, Rejected, "invalid_number"),
        (400, {"code": 99999}, Rejected, "vendor_rejected"),
        (400, {"message": "no code"}, Rejected, "vendor_rejected"),
    ],
)
@pytest.mark.asyncio
async def test_twilio_errors_map_to_codes(status: int, body: dict[str, Any], error: type, code: str) -> None:
    with pytest.raises(error) as caught:
        await TwilioProvider(Transport(ok(body, status))).send(SECRET, _out())
    assert caught.value.code == code


@pytest.mark.asyncio
async def test_twilio_answer_without_a_sid_is_retried() -> None:
    with pytest.raises(Transient):
        await TwilioProvider(Transport(ok({"status": "queued"}))).send(SECRET, _out())


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (ok({"status": "active"}, 200), None),
        (ok({"status": "closed"}, 200), AuthRevoked),
        (ok({"status": "suspended"}, 200), Rejected),
        (ok({}, 401), AuthRevoked),
    ],
)
@pytest.mark.asyncio
async def test_twilio_account_read(response: HttpResponse, error: type | None) -> None:
    transport = Transport(response)
    if error is None:
        await TwilioProvider(transport).check(SECRET)
    else:
        with pytest.raises(error):
            await TwilioProvider(transport).check(SECRET)
    assert transport.requests[0].url.endswith(f"/Accounts/{ACCOUNT_SID}.json")


@pytest.mark.asyncio
async def test_twilio_templates_page_through_the_content_api() -> None:
    page1 = {
        "contents": [
            {
                "sid": CONTENT_SID,
                "friendly_name": "deposit  reminder",
                "language": "en",
                "types": {"twilio/text": {"body": "Hi {{1}}, the deposit for {{2}} is due. {{1}}"}},
                "approval_requests": {"status": "approved", "category": "UTILITY"},
            },
            {"sid": "not-a-sid", "friendly_name": "x", "language": "en"},
            "junk",
            {
                "sid": "HX" + "b2" * 16,
                "friendly_name": "offer",
                "language": "vi",
                "types": {"whatsapp/card": {"title": "t"}},
                "approval_requests": {"status": "weird", "category": "bad category!"},
            },
        ],
        "meta": {"next_page_url": "https://content.twilio.com/v1/ContentAndApprovals?Page=1"},
    }
    page2 = {"contents": [], "meta": {"next_page_url": "https://elsewhere.example/steal"}}
    transport = Transport(ok(page1, 200), ok(page2, 200))
    found = await TwilioProvider(transport).templates(SECRET)
    assert len(transport.requests) == 2
    first, second = found
    assert (first.name, first.approval, first.category, first.variables) == (
        "deposit reminder",
        "approved",
        "UTILITY",
        ("1", "2"),
    )
    assert (second.approval, second.category, second.body) == ("unsubmitted", None, "")
    with pytest.raises(AuthRevoked):
        await TwilioProvider(Transport(ok({}, 401))).templates(SECRET)


@pytest.mark.asyncio
async def test_the_sandbox_is_deterministic_and_sends_nothing() -> None:
    sandbox = SandboxProvider()
    a = await sandbox.send(SECRET, _out(reference="m1"))
    b = await sandbox.send(SECRET, _out(reference="m1"))
    assert a == b and a.sid.startswith("SM") and len(a.sid) == 34 and a.status == "queued"
    with pytest.raises(Rejected):
        await sandbox.send(SECRET, _out(to="+61491570000"))
    with pytest.raises(Transient):
        await sandbox.send(SECRET, _out(to="+61491570001"))
    revoked = SECRET | {"auth_token": "revoked-" + "x" * 20}
    with pytest.raises(AuthRevoked):
        await sandbox.send(revoked, _out())
    with pytest.raises(AuthRevoked):
        await sandbox.check(revoked)
    templates = await sandbox.templates(SECRET)
    assert [t.approval for t in templates] == ["approved", "approved", "pending"]
    assert templates[0].variables == ("1", "2") and templates[0].content_sid.startswith("HX")


def test_sandbox_only_outside_staging_and_production() -> None:
    assert set(providers.build_providers("dev")) == {"twilio", "sandbox"}
    assert set(providers.build_providers("test")) == {"twilio", "sandbox"}
    assert set(providers.build_providers("prod")) == {"twilio"}
    assert set(providers.build_providers("stg")) == {"twilio"}


def test_placeholders_and_render() -> None:
    assert providers.placeholders("Hi {{1}} and {{ name }}, {{1}}") == ("1", "name")
    assert providers.render("Hi {{1}}, {{2}}", {"1": "Lan"}) == "Hi Lan, {{2}}"
    assert providers.address("whatsapp", "+1") == "whatsapp:+1" and providers.address("sms", "+1") == "+1"


def test_signature_matches_twilio_and_refuses_others() -> None:
    url = "https://go.crm.example.test/crm/public/v1/hooks/twilio/" + "ab" * 16
    params = [("To", "+61491570156"), ("From", "whatsapp:+61491570110"), ("Body", "Hi"), ("AccountSid", ACCOUNT_SID)]
    good = signature.sign(AUTH_TOKEN, url, params)
    assert signature.valid(AUTH_TOKEN, url, list(reversed(params)), good)  # order of the form does not matter
    assert not signature.valid(AUTH_TOKEN, url, params + [("Body", "x")], good)
    assert not signature.valid(AUTH_TOKEN, url + "/status", params, good)
    assert not signature.valid("other-token", url, params, good)
    assert not signature.valid(AUTH_TOKEN, url, params, None)
    assert not signature.valid(AUTH_TOKEN, url, params, "x" * 500)
    assert not signature.valid("", url, params, good)
    assert not signature.valid(AUTH_TOKEN, url, params, "ü" + good[1:])


def test_signature_follows_the_documented_algorithm() -> None:
    # Twilio's documented example request (token 12345): URL + the POST parameters sorted by name, HMAC-SHA1, base64.
    url = "https://mycompany.com/myapp.php?foo=1&bar=2"
    params = [
        ("CallSid", "CA1234567890ABCDE"),
        ("Caller", "+12349013030"),
        ("Digits", "1234"),
        ("From", "+12349013030"),
        ("To", "+18005551212"),
    ]
    assert signature.sign("12345", url, params) == "0/KCTR6DLpKmkAf8muzZqo1nDgQ="
