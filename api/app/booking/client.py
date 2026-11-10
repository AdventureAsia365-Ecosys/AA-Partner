"""The CRM's client of AA-Booking ``booking.b2b.v1`` (AA-947, card CRM-19 "M2M client"; design v3 §5.1, §5.2).

Server to server: every call carries an aa-m2m access token for exactly the scope it needs (``booking.b2b/read``,
``/quote``, ``/hold``, ``/book``; :class:`app.cis.m2m.TokenProvider`, one cached token per scope) and the agency's id in
``X-AA-Tenant-Id`` (Booking checks it against its ``b2b_partners`` row). No personal data is ever sent: pax are counts.

Two kinds of failure, both codes only (never a body, a token or a URL in a message):

- :class:`BookingUnavailable` (**transient**, the outage queue retries it): no HTTP answer (``booking_timeout``,
  ``booking_unreachable``, ...), a 5xx / 408, a 401 (the cached token is dropped and asked for again on the next call),
  a 429 that is not ``rehold_limit``, a token the pool would not give (``token_unavailable`` / ``token_unauthorized``),
  or an answer that breaks the contract (``booking_contract``).
- :class:`BookingRefusal` (**final**): Booking answered with a contract ``Problem``: ``price_changed``,
  ``hold_expired``, ``sold_out``, ``rehold_limit``, ``extend_limit`` (AA-952 :meth:`BookingClient.extend`),
  ``tour_not_served``, ``invalid``,
  ``unknown_partner``, ``forbidden``, ``not_found``; ``code`` is the contract's ``error`` and ``problem`` the parsed
  body (``price_hash``, ``tour_state``, ``retry_after_s``, ...).

AA-1030 (contract 1.4): :meth:`BookingClient.tariff` / :meth:`BookingClient.tariff_csv` read the agency's own tariff;
the CSV variant goes through the same :meth:`BookingClient._send` (token, 401, 5xx / 408, 429, refusals) and returns
Booking's file bytes unchanged. Both are ``bulk`` calls: the whole tariff is one answer (up to Booking's 6000 rows),
so they allow :data:`app.cis.http.BULK_MAX_BODY_BYTES` and at least :data:`BULK_TIMEOUT_S`.

The transport is :mod:`app.cis.http` (no redirects, bounded body, a timeout per call).
"""

from __future__ import annotations

import datetime as dt
import json
import re
import urllib.parse
import uuid
from dataclasses import dataclass
from typing import Any

from app.cis.http import (
    DEFAULT_TIMEOUT_S,
    HttpError,
    HttpResponse,
    Transport,
    bulk_urllib_transport,
    urllib_transport,
)
from app.cis.m2m import TokenError, TokenProvider

PREFIX = "/booking/b2b/v1"
BULK_TIMEOUT_S = 25.0  # AA-1030: a whole tariff; under the 29 s API Gateway limit in front of Booking
CONTRACT = "booking.b2b.v1"
READ = "booking.b2b/read"
QUOTE = "booking.b2b/quote"
HOLD = "booking.b2b/hold"
BOOK = "booking.b2b/book"
_CODE = re.compile(r"^[a-z][a-z_]{0,39}$")
_PATH_ID = re.compile(r"^[A-Za-z0-9:_.-]{1,64}$")


class BookingUnavailable(Exception):
    """Booking did not give a usable answer; retry later. ``error_class`` is a code."""

    def __init__(self, error_class: str) -> None:
        super().__init__(error_class)
        self.error_class = error_class


class BookingRefusal(Exception):
    """Booking refused the call with a contract Problem (a final answer for this request)."""

    def __init__(self, status: int, code: str, problem: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.problem = problem or {}


@dataclass(frozen=True)
class BookingConfig:
    """``base_url`` = ``BOOKING_API_URL`` (the Booking API origin, no path, no trailing slash)."""

    base_url: str
    tokens: TokenProvider
    transport: Transport = urllib_transport
    timeout_s: float = DEFAULT_TIMEOUT_S


def _problem(body: bytes) -> dict[str, Any]:
    try:
        data = json.loads(body)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _path_id(value: str) -> str:
    if not isinstance(value, str) or not _PATH_ID.match(value):
        raise ValueError("a Booking id has 1 to 64 letters, digits or : _ . -")
    return urllib.parse.quote(value, safe=":_.-")


def _tariff_query(currency: str, source_id: str | None, fmt: str) -> dict[str, str]:
    query = {"currency": currency, "format": fmt}
    if source_id:
        query["source_id"] = source_id
    return query


def _header(resp: HttpResponse, name: str) -> str:
    for key, value in resp.headers.items():
        if key.lower() == name:
            return value
    return ""


_ATTACHMENT_NAME = re.compile(r'filename="?([A-Za-z0-9][A-Za-z0-9._-]{0,150}\.csv)"?(;|$)')


def _attachment_name(disposition: str) -> str | None:
    """The plain ``filename`` of a ``Content-Disposition`` (letters, digits, ``.``, ``_``, ``-``, ending ``.csv``), so
    it can be sent on as it is; anything else is None (the CRM then names the file itself)."""
    match = _ATTACHMENT_NAME.search(disposition.strip())
    return match.group(1) if match else None


class BookingClient:
    def __init__(self, config: BookingConfig) -> None:
        self._config = config

    async def _send(
        self,
        method: str,
        path: str,
        scope: str,
        tenant_id: uuid.UUID,
        *,
        body: dict[str, Any] | None = None,
        query: dict[str, str] | None = None,
        idempotency_key: str | None = None,
        accept: str = "application/json",
        bulk: bool = False,
    ) -> HttpResponse:
        """One Booking call: the scope's token, the agency header, and every non-2xx answer mapped to
        :class:`BookingUnavailable` / :class:`BookingRefusal`. A 2xx response comes back as it is. ``bulk``: a whole
        list in one answer (the tariff): the bigger body cap of the default transport and at least
        :data:`BULK_TIMEOUT_S` (a transport passed in by a test is used as it is)."""
        config = self._config
        transport, timeout_s = config.transport, config.timeout_s
        if bulk:
            timeout_s = max(timeout_s, BULK_TIMEOUT_S)
            if transport is urllib_transport:
                transport = bulk_urllib_transport
        try:
            token = await config.tokens.token(scope)
        except TokenError as exc:
            raise BookingUnavailable(exc.error_class) from None
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": accept,
            "X-AA-Tenant-Id": str(tenant_id),
        }
        raw = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            raw = json.dumps(body, separators=(",", ":")).encode()
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        url = f"{config.base_url}{PREFIX}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        try:
            resp = await transport(method, url, headers, raw, timeout_s)
        except HttpError as exc:
            raise BookingUnavailable(f"booking_{exc.error_class}") from None
        status = resp.status
        if status == 401:
            config.tokens.invalidate(scope)  # revoked or rotated: the next call asks the pool again
            raise BookingUnavailable("booking_unauthorized")
        if status >= 500 or status == 408:
            raise BookingUnavailable("booking_unavailable")
        if 200 <= status < 300:
            return resp
        problem = _problem(resp.body)
        code = problem.get("error")
        code = code if isinstance(code, str) and _CODE.match(code) else f"http_{status}"
        if status == 429 and code != "rehold_limit":
            raise BookingUnavailable("booking_throttled")
        raise BookingRefusal(status, code, problem)

    async def _call(
        self,
        method: str,
        path: str,
        scope: str,
        tenant_id: uuid.UUID,
        *,
        body: dict[str, Any] | None = None,
        query: dict[str, str] | None = None,
        idempotency_key: str | None = None,
        bulk: bool = False,
    ) -> dict[str, Any]:
        resp = await self._send(
            method, path, scope, tenant_id, body=body, query=query, idempotency_key=idempotency_key, bulk=bulk
        )
        if resp.status == 204 or not resp.body:
            return {}
        data = _problem(resp.body)
        if not data:
            raise BookingUnavailable("booking_contract")
        return data

    # ── reads ────────────────────────────────────────────────────────────────────────────────────

    async def products(self, tenant_id: uuid.UUID, *, cursor: str | None = None) -> dict[str, Any]:
        query = {"cursor": cursor} if cursor else None
        return await self._call("GET", "/products", READ, tenant_id, query=query)

    async def departures(
        self, tenant_id: uuid.UUID, source_id: str, *, date_from: str | None = None, date_to: str | None = None
    ) -> dict[str, Any]:
        query = {"source_system": "cis", "source_id": source_id}
        if date_from:
            query["from"] = date_from
        if date_to:
            query["to"] = date_to
        return await self._call("GET", "/departures", READ, tenant_id, query=query)

    async def price_parity(self, tenant_id: uuid.UUID, source_id: str, currency: str) -> dict[str, Any]:
        query = {"source_system": "cis", "source_id": source_id, "currency": currency}
        return await self._call("GET", "/price-parity", READ, tenant_id, query=query)

    async def fx_rates(self, tenant_id: uuid.UUID, on: dt.date) -> dict[str, Any]:
        """S270 (contract 1.0 ``GET /fx-rates``): the FX rates Booking has approved in force on ``on`` (read with
        :func:`app.booking.contract.fx_rates`); the AA prices page offers only the currencies that have one."""
        return await self._call("GET", "/fx-rates", READ, tenant_id, query={"date": on.isoformat()})

    async def tariff(self, tenant_id: uuid.UUID, *, currency: str, source_id: str | None = None) -> dict[str, Any]:
        """AA-1030 (contract 1.4): the agency's own tariff as JSON (read with :func:`app.booking.contract.tariff`)."""
        query = _tariff_query(currency, source_id, "json")
        return await self._call("GET", "/tariff", READ, tenant_id, query=query, bulk=True)

    async def tariff_csv(
        self, tenant_id: uuid.UUID, *, currency: str, source_id: str | None = None
    ) -> tuple[bytes, str | None]:
        """AA-1030: the same tariff as Booking's CSV file (its columns, ``tariff.CSV_COLUMNS``; the CRM never builds the
        file again) and the file name of its ``Content-Disposition`` (None when absent or not a plain file name). The
        failures are those of every call; a 2xx that is not a CSV file is ``booking_contract``."""
        query = _tariff_query(currency, source_id, "csv")
        resp = await self._send("GET", "/tariff", READ, tenant_id, query=query, accept="text/csv", bulk=True)
        if "text/csv" not in _header(resp, "content-type").lower():
            raise BookingUnavailable("booking_contract")
        return resp.body, _attachment_name(_header(resp, "content-disposition"))

    async def invoices(self, tenant_id: uuid.UUID, *, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        """AA-1068 (contract 1.5 ``GET /invoices``): one page of the agency's own AA invoices, newest first (read with
        :func:`app.booking.contract.agency_invoice_page`). Booking answers ``no-store``: the CRM keeps only the copy
        of ``crm.aa_invoice_cache``."""
        query = {"limit": str(limit), "offset": str(offset)}
        return await self._call("GET", "/invoices", READ, tenant_id, query=query)

    async def invoice(self, tenant_id: uuid.UUID, number: str) -> dict[str, Any]:
        """AA-1068 (contract 1.5 ``GET /invoices/{number}``): one invoice with its lines and approved payments."""
        return await self._call("GET", f"/invoices/{_path_id(number)}", READ, tenant_id)

    async def billing_status(self, tenant_id: uuid.UUID) -> dict[str, Any]:
        """AA-1068 (contract 1.5 ``GET /billing-status``): the agency's credit status with AA (holds paused while
        past_due) and the last day its software fee is paid for."""
        return await self._call("GET", "/billing-status", READ, tenant_id)

    async def get_hold(self, tenant_id: uuid.UUID, hold_id: str) -> dict[str, Any]:
        return await self._call("GET", f"/holds/{_path_id(hold_id)}", READ, tenant_id)

    async def events(self, tenant_id: uuid.UUID, *, after: str | None, limit: int) -> dict[str, Any]:
        query = {"limit": str(limit)}
        if after:
            query["after"] = after
        return await self._call("GET", "/events", READ, tenant_id, query=query)

    # ── writes ───────────────────────────────────────────────────────────────────────────────────

    async def quote(self, tenant_id: uuid.UUID, body: dict[str, Any]) -> dict[str, Any]:
        return await self._call("POST", "/quotes", QUOTE, tenant_id, body=body)

    async def hold(self, tenant_id: uuid.UUID, body: dict[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        return await self._call("POST", "/holds", HOLD, tenant_id, body=body, idempotency_key=idempotency_key)

    async def release(self, tenant_id: uuid.UUID, hold_id: str) -> None:
        await self._call("DELETE", f"/holds/{_path_id(hold_id)}", HOLD, tenant_id)

    async def extend(self, tenant_id: uuid.UUID, hold_id: str, reason: str, *, idempotency_key: str) -> dict[str, Any]:
        """AA-952 (Fix A8): one extension of an active hold (Booking decides the length; a second is 409
        ``extend_limit``, a retry with the same key returns the hold). ``reason``: ``payment_claimed`` /
        ``client_recheck``."""
        path = f"/holds/{_path_id(hold_id)}/extend"
        return await self._call("POST", path, HOLD, tenant_id, body={"reason": reason}, idempotency_key=idempotency_key)

    async def confirm(
        self, tenant_id: uuid.UUID, hold_id: str, body: dict[str, Any], *, idempotency_key: str
    ) -> dict[str, Any]:
        path = f"/holds/{_path_id(hold_id)}/confirm"
        return await self._call("POST", path, BOOK, tenant_id, body=body, idempotency_key=idempotency_key)

    async def cancel_booking(
        self, tenant_id: uuid.UUID, booking_id: str, reason_code: str, *, dry_run: bool, idempotency_key: str
    ) -> dict[str, Any]:
        """AA-957 (C12): cancel a confirmed AA booking, or with ``dry_run`` only read the fee its cancellation policy
        charges today (``CancellationResult``). Booking answers a repeat with the stored cancellation."""
        path = f"/bookings/{_path_id(booking_id)}/cancel"
        query = {"dry_run": "true"} if dry_run else None
        return await self._call(
            "POST", path, BOOK, tenant_id, body={"reason_code": reason_code}, query=query,
            idempotency_key=idempotency_key,
        )  # fmt: skip
