"""The CRM's client of AA-CIS-App ``catalog.http.v1`` (root contract 1.2; AA-976 served it): published AA tours.

Every call asks the aa-m2m pool for ``cis.catalog/read`` only (:class:`app.cis.m2m.TokenProvider`). ``GET /tours`` is
read page by page (``limit`` 50, the cursor CIS issued); ``GET /tours/cis/{source_id}`` sends the stored ETag in
``If-None-Match`` (304 = unchanged) and reads the liveness answers of root ADR 0015 decision 5: 200 served, 404 with a
``reason`` paused (hide, do not mark lines), 404 without a reason never served, 410 retired (lines read "no longer
offered"). Any other failure is :class:`CatalogUnavailable` with a code (the hourly sync backs off and tries again).
"""

from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any

from app.cis.http import DEFAULT_TIMEOUT_S, HttpError, Transport, urllib_transport
from app.cis.m2m import TokenError, TokenProvider

SCOPE = "cis.catalog/read"
PREFIX = "/catalog/v1"
PAGE_LIMIT = 50
_REASON = re.compile(r"^[a-z][a-z_]{0,63}$")
_SOURCE_ID = re.compile(r"^[^\x00-\x1f]{1,128}$")
_COUNTRY = re.compile(r"^[A-Z]{2}$")


class CatalogUnavailable(Exception):
    def __init__(self, error_class: str) -> None:
        super().__init__(error_class)
        self.error_class = error_class


@dataclass(frozen=True)
class CatalogConfig:
    """``base_url`` = ``CIS_API_URL`` (the CIS API origin)."""

    base_url: str
    tokens: TokenProvider
    transport: Transport = urllib_transport
    timeout_s: float = DEFAULT_TIMEOUT_S


@dataclass(frozen=True)
class Summary:
    source_id: str
    title: str
    country_code: str
    duration_days: int
    content_hash: str


@dataclass(frozen=True)
class TourAnswer:
    """``state``: served (``body`` + ``etag``), not_modified, paused / retired (``reason``), unknown (never served)."""

    state: str
    body: dict[str, Any] | None = None
    etag: str | None = None
    reason: str | None = None


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        raise CatalogUnavailable("cis_contract") from None


def summary(item: Any) -> Summary:
    if not isinstance(item, dict):
        raise CatalogUnavailable("cis_contract")
    source_id, title = item.get("source_id"), item.get("title")
    country, days, digest = item.get("country_code"), item.get("duration_days"), item.get("content_hash")
    if (
        item.get("source_system") != "cis"
        or not isinstance(source_id, str)
        or not _SOURCE_ID.match(source_id)
        or not isinstance(title, str)
        or not 1 <= len(title) <= 300
        or not isinstance(country, str)
        or not _COUNTRY.match(country)
        or not isinstance(days, int)
        or isinstance(days, bool)
        or not 1 <= days <= 366
        or not isinstance(digest, str)
        or not 1 <= len(digest) <= 100
    ):
        raise CatalogUnavailable("cis_contract")
    return Summary(source_id, title, country, days, digest)


class CatalogClient:
    def __init__(self, config: CatalogConfig) -> None:
        self._config = config

    async def _get(self, path: str, query: dict[str, str] | None = None, etag: str | None = None) -> Any:
        config = self._config
        try:
            token = await config.tokens.token(SCOPE)
        except TokenError as exc:
            raise CatalogUnavailable(exc.error_class) from None
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if etag:
            headers["If-None-Match"] = etag
        url = f"{config.base_url}{PREFIX}{path}" + (f"?{urllib.parse.urlencode(query)}" if query else "")
        try:
            return await config.transport("GET", url, headers, None, config.timeout_s)
        except HttpError as exc:
            raise CatalogUnavailable(f"cis_{exc.error_class}") from None

    def _failed(self, status: int) -> CatalogUnavailable:
        if status == 401:
            self._config.tokens.invalidate(SCOPE)
            return CatalogUnavailable("cis_unauthorized")
        if status == 403:
            return CatalogUnavailable("cis_forbidden")
        if status >= 500:
            return CatalogUnavailable("cis_unavailable")
        return CatalogUnavailable("cis_unexpected_status")

    async def list_tours(self, cursor: str | None) -> tuple[list[Summary], str | None]:
        query = {"limit": str(PAGE_LIMIT)}
        if cursor:
            query["cursor"] = cursor
        resp = await self._get("/tours", query)
        if resp.status != 200:
            raise self._failed(resp.status)
        data = _json(resp.body)
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise CatalogUnavailable("cis_contract")
        next_cursor = data.get("next_cursor")
        if next_cursor is not None and (not isinstance(next_cursor, str) or not 1 <= len(next_cursor) <= 200):
            raise CatalogUnavailable("cis_contract")
        return [summary(i) for i in items], next_cursor or None

    async def get_tour(self, source_id: str, etag: str | None = None) -> TourAnswer:
        resp = await self._get(f"/tours/cis/{urllib.parse.quote(source_id, safe='')}", etag=etag)
        if resp.status == 304:
            return TourAnswer("not_modified", etag=etag)
        if resp.status in (404, 410):
            problem = _json(resp.body) if resp.body else {}
            reason = problem.get("reason") if isinstance(problem, dict) else None
            reason = reason if isinstance(reason, str) and _REASON.match(reason) else None
            if resp.status == 410:
                return TourAnswer("retired", reason=reason)
            return TourAnswer("paused" if reason else "unknown", reason=reason)
        if resp.status != 200:
            raise self._failed(resp.status)
        body = _json(resp.body)
        if not isinstance(body, dict):
            raise CatalogUnavailable("cis_contract")
        summary(body)  # the fields the cache keeps are valid
        tag = {k.lower(): v for k, v in resp.headers.items()}.get("etag")
        return TourAnswer("served", body=body, etag=tag if isinstance(tag, str) and len(tag) <= 100 else None)
