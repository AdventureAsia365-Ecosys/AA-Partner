"""Posts a signed Twilio-shaped webhook to an agency's SMS / WhatsApp hook URL (AA-958, card CRM-28): the Dev live check
of the inbound path and of the status callbacks against a ``sandbox`` connection, at $0 (nothing reaches Twilio).

The request is what Twilio itself would send: form-encoded parameters (``AccountSid``, ``MessageSid``, ``From``,
``To``, ``Body`` for an inbound message; ``MessageSid``, ``MessageStatus``, ``ErrorCode`` for a status callback) and
``X-Twilio-Signature`` computed with :func:`app.messaging.signature.sign` over the exact URL Settings > Channels shows.
``--bad-signature`` sends a wrong signature instead (card R: the API answers 403 ``invalid_signature`` and stores
nothing).

Usage (from ``api/``; the Auth Token typed into the sandbox connection is read from the environment, never argv)::

    PROBE_AUTH_TOKEN=... python3 scripts/messaging_hook_probe.py inbound --url <hook URL> --account-sid AC... \\
        --from whatsapp:+61491570110 --to whatsapp:+61491570156 --body "Is the kayak OK for kids?"
    PROBE_AUTH_TOKEN=... python3 scripts/messaging_hook_probe.py status --url <hook URL>/status --account-sid AC... \\
        --sid SM... --status delivered

Prints one line: the HTTP status and the response body (an empty TwiML ``<Response>`` on success). Exit code 0 when
the status is the expected one (``--expect``, default 200), else 1. Standard library only.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.messaging.signature import sign  # noqa: E402

TOKEN_ENV = "PROBE_AUTH_TOKEN"  # noqa: S105 — the name of the variable, not a token
TIMEOUT_SECONDS = 20

Opener = Callable[[urllib.request.Request], tuple[int, str]]


def params_for(args: argparse.Namespace) -> list[tuple[str, str]]:
    """The form parameters Twilio would post for this probe."""
    if args.kind == "inbound":
        out = [
            ("AccountSid", args.account_sid),
            ("MessageSid", args.sid or "SM" + secrets.token_hex(16)),
            ("From", args.sender),
            ("To", args.to),
            ("Body", args.body),
            ("NumMedia", "0"),
        ]
        if args.opt_out_type:
            out.append(("OptOutType", args.opt_out_type))
        return out
    out = [("AccountSid", args.account_sid), ("MessageSid", args.sid or ""), ("MessageStatus", args.status)]
    if args.error_code:
        out.append(("ErrorCode", args.error_code))
    return out


def build_request(
    url: str, token: str, params: list[tuple[str, str]], *, bad_signature: bool
) -> urllib.request.Request:
    signature = sign(token, url, params)
    if bad_signature:
        signature = sign(token + "-wrong", url, params)
    return urllib.request.Request(  # noqa: S310 — parse_args accepts https URLs only
        url,
        data=urllib.parse.urlencode(params).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "X-Twilio-Signature": signature},
    )


def _open(request: urllib.request.Request) -> tuple[int, str]:  # pragma: no cover - network
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310 — https only
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("kind", choices=("inbound", "status"))
    parser.add_argument("--url", required=True, help="the hook URL exactly as Settings > Channels shows it")
    parser.add_argument("--account-sid", required=True)
    parser.add_argument("--sid", default="", help="MessageSid (inbound: random when empty; status: required)")
    parser.add_argument("--from", dest="sender", default="", help="inbound: whatsapp:+E164 or +E164")
    parser.add_argument("--to", default="", help="inbound: the agency's sender")
    parser.add_argument("--body", default="", help="inbound: the text")
    parser.add_argument("--opt-out-type", default="", choices=("", "STOP", "START"))
    parser.add_argument("--status", default="delivered", help="status: queued | sent | delivered | read | failed ...")
    parser.add_argument("--error-code", default="")
    parser.add_argument("--bad-signature", action="store_true", help="sign with a wrong token (expect 403)")
    parser.add_argument("--expect", type=int, default=0, help="the expected HTTP status (default 200, 403 if bad)")
    args = parser.parse_args(argv)
    if not args.url.startswith("https://"):
        parser.error("--url must be the https hook URL")
    if args.kind == "inbound" and not (args.sender and args.to):
        parser.error("inbound needs --from and --to")
    if args.kind == "status" and not args.sid:
        parser.error("status needs --sid")
    if not args.expect:
        args.expect = 403 if args.bad_signature else 200
    return args


def main(argv: Sequence[str] | None = None, *, opener: Opener = _open, environ: dict[str, str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    token = (environ if environ is not None else dict(os.environ)).get(TOKEN_ENV, "")
    if not token:
        print(f"FAIL: set {TOKEN_ENV} to the Auth Token of the sandbox connection")
        return 1
    status, body = opener(build_request(args.url, token, params_for(args), bad_signature=args.bad_signature))
    print(f"{status} {body.strip()[:200]}")
    return 0 if status == args.expect else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
