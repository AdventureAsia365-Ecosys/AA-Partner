"""scripts/messaging_hook_probe.py (AA-958): the Dev live check signs the request the way the webhook verifies it, sends
a wrong signature on request, and reads the token from the environment only."""

from __future__ import annotations

import importlib.util
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from app.messaging import signature

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "messaging_hook_probe.py"
_spec = importlib.util.spec_from_file_location("messaging_hook_probe", SCRIPT)
assert _spec and _spec.loader
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

URL = "https://go.crm.example.test/crm/public/v1/hooks/twilio/" + "ab" * 16
ACCOUNT = "AC" + "0" * 32
TOKEN = "t" * 24


def _capture(status: int = 200, body: str = "<Response></Response>"):
    seen: list[urllib.request.Request] = []

    def opener(request: urllib.request.Request) -> tuple[int, str]:
        seen.append(request)
        return status, body

    return seen, opener


def _form(request: urllib.request.Request) -> list[tuple[str, str]]:
    assert isinstance(request.data, bytes)
    return urllib.parse.parse_qsl(request.data.decode("utf-8"), keep_blank_values=True)


def test_inbound_is_signed_like_twilio(capsys: pytest.CaptureFixture[str]) -> None:
    seen, opener = _capture()
    argv = ["inbound", "--url", URL, "--account-sid", ACCOUNT, "--from", "whatsapp:+61491570110"]
    argv += ["--to", "whatsapp:+61491570156", "--body", "Is the kayak OK for kids?"]
    assert probe.main(argv, opener=opener, environ={probe.TOKEN_ENV: TOKEN}) == 0
    request = seen[0]
    params = _form(request)
    assert dict(params)["AccountSid"] == ACCOUNT
    assert dict(params)["MessageSid"].startswith("SM") and len(dict(params)["MessageSid"]) == 34
    assert signature.valid(TOKEN, URL, params, request.get_header("X-twilio-signature"))
    assert request.get_method() == "POST"
    assert capsys.readouterr().out.startswith("200 ")


def test_status_callback_and_opt_out_parameters() -> None:
    seen, opener = _capture()
    argv = ["status", "--url", URL + "/status", "--account-sid", ACCOUNT, "--sid", "SM" + "c" * 32]
    argv += ["--status", "undelivered", "--error-code", "30003"]
    assert probe.main(argv, opener=opener, environ={probe.TOKEN_ENV: TOKEN}) == 0
    params = dict(_form(seen[0]))
    assert params == {
        "AccountSid": ACCOUNT,
        "MessageSid": "SM" + "c" * 32,
        "MessageStatus": "undelivered",
        "ErrorCode": "30003",
    }
    argv = ["inbound", "--url", URL, "--account-sid", ACCOUNT, "--from", "+61491570110", "--to", "+61491570156"]
    argv += ["--body", "STOP", "--opt-out-type", "STOP"]
    assert probe.main(argv, opener=opener, environ={probe.TOKEN_ENV: TOKEN}) == 0
    assert dict(_form(seen[1]))["OptOutType"] == "STOP"


def test_bad_signature_expects_403() -> None:
    seen, opener = _capture(403, '{"detail":"invalid_signature"}')
    argv = ["inbound", "--url", URL, "--account-sid", ACCOUNT, "--from", "+61491570110", "--to", "+61491570156"]
    assert probe.main([*argv, "--bad-signature"], opener=opener, environ={probe.TOKEN_ENV: TOKEN}) == 0
    assert not signature.valid(TOKEN, URL, _form(seen[0]), seen[0].get_header("X-twilio-signature"))
    # A 403 where a 200 was expected is a failure.
    assert probe.main(argv, opener=opener, environ={probe.TOKEN_ENV: TOKEN}) == 1


def test_needs_the_token_in_the_environment(capsys: pytest.CaptureFixture[str]) -> None:
    seen, opener = _capture()
    argv = ["status", "--url", URL + "/status", "--account-sid", ACCOUNT, "--sid", "SM" + "c" * 32]
    assert probe.main(argv, opener=opener, environ={}) == 1
    assert not seen
    assert probe.TOKEN_ENV in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [
        ["inbound", "--url", "http://plain.example.test/x", "--account-sid", ACCOUNT, "--from", "+1", "--to", "+2"],
        ["inbound", "--url", URL, "--account-sid", ACCOUNT],
        ["status", "--url", URL, "--account-sid", ACCOUNT],
    ],
)
def test_refuses_incomplete_arguments(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        probe.parse_args(argv)
