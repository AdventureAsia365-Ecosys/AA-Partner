"""Twilio webhook signatures (AA-958; design S6 "HMAC checked before anything is stored", card R "invalid signature ->
403, dropped").

Twilio signs each webhook with the account's Auth Token: ``X-Twilio-Signature`` = base64(HMAC-SHA1(token, URL +
every POST parameter's name and value, sorted by name)). The URL is the one Twilio called, which is the agency's
webhook URL as the CRM printed it (``Settings.messaging_hooks_origin()`` + the path), so the check never trusts the
Host header of the request. Compared in constant time. The same function signs a request in the tests and in
``scripts/messaging_hook_probe.py`` (the Dev live check against the sandbox provider).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Iterable

MAX_SIGNATURE = 200


def sign(token: str, url: str, params: Iterable[tuple[str, str]]) -> str:
    data = url + "".join(name + value for name, value in sorted(params))
    digest = hmac.new(token.encode("utf-8"), data.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def valid(token: str, url: str, params: Iterable[tuple[str, str]], signature: str | None) -> bool:
    if not token or not signature or len(signature) > MAX_SIGNATURE:
        return False
    return hmac.compare_digest(sign(token, url, params).encode("ascii"), signature.encode("ascii", "replace"))
