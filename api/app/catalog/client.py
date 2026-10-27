"""The CRM's client of AA-CIS-App ``catalog.http.v1`` (root contract 1.2; AA-976 served it): published AA tours.

Every call asks the aa-m2m pool for ``cis.catalog/read`` only (:class:`app.cis.m2m.TokenProvider`). ``GET /tours`` is
read page by page (``limit`` 50, the cursor CIS issued); ``GET /tours/cis/{source_id}`` sends the stored ETag in
``If-None-Match`` (304 = unchanged) and reads the liveness answers of root ADR 0015 decision 5: 200 served, 404 with a
``reason`` paused (hide, do not mark lines), 404 without a reason never served, 410 retired (lines read "no longer
offered"). Any other failure is :class:`CatalogUnavailable` with a code (the hourly sync backs off and tries again).

AA-951 (contract 1.2, AA-977): ``GET /aa-content?kind=`` lists the latest published version of each AA-authored item
that is not retired (one locale per item, EN / VI paired by ``group_key``); ``GET /aa-content/{id}/versions/{n}``
serves one published version (also after a newer one or the item's retirement). Read by :mod:`app.catalog.content`.
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
_GROUP_KEY = re.compile(r"^[a-z0-9][a-z0-9-]{1,79}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
CONTENT_KINDS = ("prep_pack", "starter_template", "campaign_block")
LOCALES = ("en", "vi")
MAX_CONTENT_BODY = 200_000


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


@dataclass(frozen=True)
class ContentSummary:
    """One item of ``listAaContent`` (contract ``AaContentSummary``)."""

    content_id: str
    kind: str
    version: int
    locale: str
    group_key: str | None
    title: str
    content_hash: str


@dataclass(frozen=True)
class Content:
    """One published version (contract ``AaContent``)."""

    content_id: str
    kind: str
    version: int
    locale: str
    group_key: str | None
    title: str
    body_markdown: str
    applies_to: dict[str, Any]
    published_at: str
    content_hash: str


def _content_head(item: Any) -> tuple[str, str, int, str, str | None, str, str]:
    if not isinstance(item, dict):
        raise CatalogUnavailable("cis_contract")
    cid, kind, version, locale = item.get("content_id"), item.get("kind"), item.get("version"), item.get("locale")
    group, title, digest = item.get("group_key"), item.get("title"), item.get("content_hash")
    if (
        not isinstance(cid, str)
        or not _UUID.match(cid.lower())
        or kind not in CONTENT_KINDS
        or not isinstance(version, int)
        or isinstance(version, bool)
        or version < 1
        or locale not in LOCALES
        or (group is not None and (not isinstance(group, str) or not _GROUP_KEY.match(group)))
        or not isinstance(title, str)
        or not 1 <= len(title) <= 300
        or any(ord(c) < 32 for c in title)
        or not isinstance(digest, str)
        or not 1 <= len(digest) <= 100
    ):
        raise CatalogUnavailable("cis_contract")
    return cid.lower(), kind, version, locale, group, title, digest


def content_summary(item: Any) -> ContentSummary:
    return ContentSummary(*_content_head(item))


def _applies_to(value: Any) -> dict[str, Any]:
    """The contract's ``applies_to``: country codes and / or CIS tours; anything else is a contract break."""
    if not isinstance(value, dict) or set(value) - {"country_codes", "tours"}:
        raise CatalogUnavailable("cis_contract")
    out: dict[str, Any] = {}
    countries = value.get("country_codes")
    if countries is not None:
        if not isinstance(countries, list) or not all(isinstance(c, str) and _COUNTRY.match(c) for c in countries):
            raise CatalogUnavailable("cis_contract")
        out["country_codes"] = sorted(set(countries))
    tours = value.get("tours")
    if tours is not None:
        if not isinstance(tours, list):
            raise CatalogUnavailable("cis_contract")
        keys = []
        for t in tours:
            sid = t.get("source_id") if isinstance(t, dict) else None
            if t.get("source_system") != "cis" or not isinstance(sid, str) or not _SOURCE_ID.match(sid):
                raise CatalogUnavailable("cis_contract")
            keys.append({"source_system": "cis", "source_id": sid})
        out["tours"] = keys
    return out


def content(item: Any) -> Content:
    head = _content_head(item)
    body, published = item.get("body_markdown"), item.get("published_at")
    if (
        not isinstance(body, str)
        or len(body.encode()) > MAX_CONTENT_BODY
        or not isinstance(published, str)
        or not 1 <= len(published) <= 40
    ):
        raise CatalogUnavailable("cis_contract")
    return Content(*head[:6], body, _applies_to(item.get("applies_to")), published, head[6])


class ContentClient(CatalogClient):
    """``catalog.http.v1`` 1.2 AA-authored content (AA-951)."""

    async def list_content(self, kind: str, cursor: str | None) -> tuple[list[ContentSummary], str | None]:
        query = {"kind": kind, "limit": str(PAGE_LIMIT)}
        if cursor:
            query["cursor"] = cursor
        resp = await self._get("/aa-content", query)
        if resp.status != 200:
            raise self._failed(resp.status)
        data = _json(resp.body)
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise CatalogUnavailable("cis_contract")
        next_cursor = data.get("next_cursor")
        if next_cursor is not None and (not isinstance(next_cursor, str) or not 1 <= len(next_cursor) <= 200):
            raise CatalogUnavailable("cis_contract")
        return [content_summary(i) for i in items], next_cursor or None

    async def get_content(self, content_id: str, version: int) -> Content | None:
        """One published version; None when CIS answers 404 (a draft or an unknown version: skipped)."""
        resp = await self._get(f"/aa-content/{urllib.parse.quote(content_id, safe='')}/versions/{int(version)}")
        if resp.status == 404:
            return None
        if resp.status != 200:
            raise self._failed(resp.status)
        return content(_json(resp.body))
