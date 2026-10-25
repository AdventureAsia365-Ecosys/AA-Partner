"""app.cis.membership_feed without a database (AA-978): configuration, backoff, the HTTP error classes, the cursor
moves, and the pure decisions. The rules applied to agencies and members run on PostgreSQL in
``tests/integration/test_crm013_membership_feed.py``."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping
from typing import Any

import asyncpg
import pytest

from app.cis import feed_state
from app.cis import membership_feed as feed
from app.cis.http import HttpError, HttpResponse
from app.cis.m2m import M2mCredentials, TokenError, TokenProvider
from app.jobs.payload_guard import check_payload
from app.jobs.queue import Job

NOW = dt.datetime(2026, 10, 24, 12, 0, tzinfo=dt.UTC)
BASE = "https://api-cis.example.invalid"


class FakePool:
    """Answers the crm.membership_feed_state statements of app.cis.feed_state."""

    def __init__(self, *, cursor: str | None = None, failures: int = 0, last_error_at: dt.datetime | None = None):
        self.state: dict[str, Any] = {
            "feed_cursor": cursor,
            "failures": failures,
            "last_error_at": last_error_at,
            "last_ok_at": None,
        }
        self.calls: list[str] = []
        self.moved_elsewhere = False

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any]:
        self.calls.append("load")
        return dict(self.state)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if "SET feed_cursor" in sql:
            self.calls.append(f"advance:{args[0]}->{args[1]}")
            if self.moved_elsewhere or args[0] != self.state["feed_cursor"]:
                return None
            self.state["feed_cursor"] = args[1]
            return 1
        if "last_error_class" in sql:
            self.calls.append(f"fail:{args[0]}")
            self.state["failures"] += 1
            return self.state["failures"]
        raise AssertionError(sql)

    async def execute(self, sql: str, *args: Any) -> str:
        self.calls.append("succeed")
        return "UPDATE 1"


class FakeTokens(TokenProvider):
    def __init__(self, error: str | None = None) -> None:
        super().__init__(M2mCredentials("abc123", "x", "https://m2m-auth.example.invalid/oauth2/token"))
        self.error = error
        self.invalidated: list[str] = []
        self.scopes: list[str] = []

    async def token(self, scope: str) -> str:
        self.scopes.append(scope)
        if self.error:
            raise TokenError(self.error)
        return "tok"

    def invalidate(self, scope: str) -> None:
        self.invalidated.append(scope)


class Transport:
    def __init__(self, *responses: HttpResponse | Exception) -> None:
        self.responses = list(responses)
        self.urls: list[str] = []
        self.headers: list[dict[str, str]] = []

    async def __call__(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout_s: float
    ) -> HttpResponse:
        assert method == "GET" and body is None
        self.urls.append(url)
        self.headers.append(dict(headers))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _page(items: list[Any], next_cursor: str) -> HttpResponse:
    return HttpResponse(200, json.dumps({"items": items, "next_cursor": next_cursor}).encode())


def _feed(transport: Transport, tokens: FakeTokens | None = None) -> feed.MembershipFeed:
    config = feed.FeedConfig(base_url=BASE, tokens=tokens or FakeTokens(), transport=transport)
    return feed.MembershipFeed(config, None, now=lambda: NOW)


JOB = Job(id="00000000-0000-4000-8000-000000000001", kind="crm_membership_pull", payload={}, attempt=1, max_attempts=3)


@pytest.mark.asyncio
async def test_not_configured_does_nothing_and_warns_once(caplog: pytest.LogCaptureFixture) -> None:
    pool = FakePool()
    job = feed.MembershipFeed(None, None)
    for _ in range(2):
        result = await job(pool, JOB)  # type: ignore[arg-type]
        assert result["status"] == "not_configured"
        check_payload(result, what="result")
    assert pool.calls == []
    assert sum("not_configured" in r.message for r in caplog.records) == 1


@pytest.mark.asyncio
async def test_after_failures_the_run_waits_its_backoff() -> None:
    pool = FakePool(failures=2, last_error_at=NOW - dt.timedelta(seconds=30))  # 40 s after 2 failures
    transport = Transport()
    assert (await _feed(transport)(pool, JOB))["status"] == "backoff"  # type: ignore[arg-type]
    assert transport.urls == []
    pool = FakePool(failures=2, last_error_at=NOW - dt.timedelta(seconds=41))
    transport = Transport(_page([], ""))
    assert (await _feed(transport)(pool, JOB))["status"] == "ok"  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_an_empty_feed_records_a_success_and_asks_for_the_tenant_scope_only() -> None:
    pool, transport, tokens = FakePool(), Transport(_page([], "")), FakeTokens()
    result = await _feed(transport, tokens)(pool, JOB)  # type: ignore[arg-type]
    assert result == {
        "status": "ok",
        "applied_count": 0,
        "ignored_count": 0,
        "rejected_count": 0,
        "replayed_count": 0,
        "unrecorded_count": 0,
        "page_count": 1,
        "signed_out_count": 0,
    }
    assert tokens.scopes == ["cis.tenant/read"]
    assert transport.urls == [f"{BASE}/tenant/membership/v1/events?limit=200"]
    assert transport.headers[0]["Authorization"] == "Bearer tok"
    assert pool.calls == ["load", "succeed"]


@pytest.mark.asyncio
async def test_the_cursor_moves_from_the_value_read_and_pages_until_a_short_one() -> None:
    full = [{"no": "ids"}] * feed.PAGE_LIMIT  # unrecorded: no event / tenant id (no database needed)
    pool = FakePool(cursor="c0")
    transport = Transport(_page(full, "c1"), _page([{"x": 1}], "c2"))
    result = await _feed(transport)(pool, JOB)  # type: ignore[arg-type]
    assert result["page_count"] == 2 and result["unrecorded_count"] == feed.PAGE_LIMIT + 1
    assert transport.urls[0].endswith("events?limit=200&after=c0")
    assert transport.urls[1].endswith("after=c1")
    assert pool.calls == ["load", "advance:c0->c1", "advance:c1->c2"]
    assert pool.state["feed_cursor"] == "c2"


@pytest.mark.asyncio
async def test_a_cursor_moved_by_another_run_stops_this_one() -> None:
    pool = FakePool(cursor="c0")
    pool.moved_elsewhere = True
    transport = Transport(_page([{}] * feed.PAGE_LIMIT, "c1"), _page([], "c2"))
    result = await _feed(transport)(pool, JOB)  # type: ignore[arg-type]
    assert result["status"] == "ok" and result["page_count"] == 1
    assert len(transport.urls) == 1


@pytest.mark.asyncio
async def test_at_most_max_pages_per_run() -> None:
    pages = [_page([{}] * feed.PAGE_LIMIT, f"c{i}") for i in range(1, feed.MAX_PAGES + 2)]
    pool = FakePool()
    result = await _feed(Transport(*pages))(pool, JOB)  # type: ignore[arg-type]
    assert result["page_count"] == feed.MAX_PAGES


@pytest.mark.parametrize(
    ("response", "error_class"),
    [
        (HttpResponse(401, b""), "cis_unauthorized"),
        (HttpResponse(403, b""), "cis_forbidden"),
        (HttpResponse(404, b""), "cis_not_found"),
        (HttpResponse(429, b""), "cis_throttled"),
        (HttpResponse(503, b""), "cis_unavailable"),
        (HttpResponse(302, b""), "cis_unexpected_status"),
        (HttpError("timeout"), "cis_timeout"),
        (HttpError("unreachable"), "cis_unreachable"),
        (HttpResponse(200, b"<html>"), "cis_contract"),
        (HttpResponse(200, b'{"items": []}'), "cis_contract"),
    ],
)
@pytest.mark.asyncio
async def test_a_cis_failure_is_recorded_and_the_job_still_succeeds(
    response: HttpResponse | Exception, error_class: str
) -> None:
    pool, tokens = FakePool(cursor="c0"), FakeTokens()
    result = await _feed(Transport(response), tokens)(pool, JOB)  # type: ignore[arg-type]
    assert result["status"] == "error" and result["reason_code"] == error_class
    check_payload(result, what="result")
    assert pool.calls == ["load", f"fail:{error_class}"]
    assert pool.state["feed_cursor"] == "c0"
    assert tokens.invalidated == (["cis.tenant/read"] if error_class == "cis_unauthorized" else [])


@pytest.mark.asyncio
async def test_a_token_failure_is_recorded() -> None:
    pool = FakePool()
    result = await _feed(Transport(), FakeTokens(error="token_unauthorized"))(pool, JOB)  # type: ignore[arg-type]
    assert (result["status"], result["reason_code"]) == ("error", "token_unauthorized")
    assert pool.calls == ["load", "fail:token_unauthorized"]


class Directory:
    issuer = "https://issuer.example.invalid"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.signed_out: list[str] = []

    async def global_sign_out(self, sub: str) -> None:
        if self.fail:
            raise RuntimeError("cognito down")
        self.signed_out.append(sub)


@pytest.mark.asyncio
async def test_sign_outs_are_best_effort() -> None:
    counts = feed.RunCounts(sign_outs=["s1", "s2"])
    ok = Directory()
    await feed.MembershipFeed(None, ok)._sign_out(counts)  # type: ignore[arg-type]
    assert ok.signed_out == ["s1", "s2"] and counts.signed_out == 2
    counts = feed.RunCounts(sign_outs=["s1"])
    await feed.MembershipFeed(None, Directory(fail=True))._sign_out(counts)  # type: ignore[arg-type]
    assert counts.signed_out == 0
    await feed.MembershipFeed(None, None)._sign_out(counts)


def test_backoff_doubles_up_to_five_minutes() -> None:
    assert [feed.backoff_seconds(n) for n in (0, 1, 2, 3, 4, 5, 50)] == [0, 20, 40, 80, 160, 300, 300]


@pytest.mark.parametrize(
    ("event_type", "status", "source", "expected"),
    [
        ("tenant.locked", "active", None, ("applied", "agency_locked", "lock")),
        ("tenant.deleted", "active", None, ("applied", "agency_locked", "lock")),
        ("tenant.locked", "locked", "aa_ops", ("ignored", "already_locked", None)),
        ("tenant.unlocked", "locked", "cis_tenant", ("applied", "agency_unlocked", "unlock")),
        ("tenant.unlocked", "locked", "aa_ops", ("ignored", "lock_not_from_cis_tenant", None)),
        ("tenant.unlocked", "locked", "cis_membership", ("ignored", "lock_not_from_cis_tenant", None)),
        ("tenant.unlocked", "locked", None, ("ignored", "lock_not_from_cis_tenant", None)),  # locked before crm-013
        ("tenant.unlocked", "active", None, ("ignored", "not_locked", None)),
    ],
)
def test_lifecycle_decisions(event_type: str, status: str, source: str | None, expected: tuple[str, ...]) -> None:
    """tenant.unlocked lifts only CIS's own lifecycle lock (contract text: AA and last-owner locks stay)."""
    assert feed.lifecycle_decision(event_type, status, source) == expected
    assert expected[1] in feed.REASONS


def test_the_events_url_quotes_the_cursor() -> None:
    assert feed.events_url(BASE, "a/b+c") == f"{BASE}/tenant/membership/v1/events?limit=200&after=a%2Fb%2Bc"


# ── review round 1: one event never blocks or crashes the feed silently ──────────────────────────


def _broken_process(error: BaseException):
    async def process(*_args: Any) -> None:
        raise error

    return process


@pytest.mark.parametrize(
    "error",
    [
        asyncpg.exceptions.DeadlockDetectedError("x"),  # systemic: 40P01
        asyncpg.exceptions.InsufficientPrivilegeError("x"),  # systemic: a grant is missing, not this event's fault
        asyncpg.exceptions.DataError("invalid input for query argument $1"),  # 22000 from asyncpg: a bug here
        TypeError("unhashable type: 'list'"),  # a bug: never an unhandled job failure
        asyncpg.exceptions.InterfaceError("connection is closed"),  # not a PostgresError
    ],
)
@pytest.mark.asyncio
async def test_an_apply_failure_keeps_the_cursor_and_backs_off(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, error: BaseException
) -> None:
    monkeypatch.setattr(feed, "process_event", _broken_process(error))
    pool = FakePool(cursor="c0")
    result = await _feed(Transport(_page([{"event_id": "e"}], "c1")))(pool, JOB)  # type: ignore[arg-type]
    assert (result["status"], result["reason_code"]) == ("error", "apply_error")
    check_payload(result, what="result")
    assert pool.calls == ["load", "fail:apply_error"]
    assert pool.state["feed_cursor"] == "c0"
    assert any("crm_membership_event_apply_failed" in r.message for r in caplog.records)
    assert "unhashable" not in caplog.text and "connection is closed" not in caplog.text  # class codes only


@pytest.mark.asyncio
async def test_a_stuck_feed_logs_an_error_line_for_the_alarm(caplog: pytest.LogCaptureFixture) -> None:
    pool = FakePool(failures=feed.STUCK_FAILURES - 2, last_error_at=NOW - dt.timedelta(hours=1))
    await _feed(Transport(HttpResponse(503, b"")))(pool, JOB)  # type: ignore[arg-type]
    assert not [r for r in caplog.records if "crm_membership_feed_stuck" in r.message]
    pool.state["last_error_at"] = NOW - dt.timedelta(hours=1)
    await _feed(Transport(HttpResponse(503, b"")))(pool, JOB)  # type: ignore[arg-type]
    stuck = [r for r in caplog.records if "crm_membership_feed_stuck" in r.message]
    assert len(stuck) == 1 and stuck[0].levelname == "ERROR"
    assert "error=cis_unavailable" in stuck[0].getMessage()


@pytest.mark.parametrize(
    ("failures", "last_ok", "word"),
    [
        (0, dt.timedelta(seconds=30), "ok"),
        (0, None, "pending"),
        (0, dt.timedelta(minutes=11), "stale"),
        (2, dt.timedelta(seconds=30), "failing"),
    ],
)
def test_the_health_word(failures: int, last_ok: dt.timedelta | None, word: str) -> None:
    state = feed_state.FeedState("c1", failures, None, NOW - last_ok if last_ok else None)
    assert feed_state.health_word(state, NOW) == word


@pytest.mark.asyncio
async def test_health_is_not_configured_without_a_database_call() -> None:
    pool = FakePool()
    assert await feed_state.health(pool, configured=False) == "not_configured"  # type: ignore[arg-type]
    assert pool.calls == []


def test_refusal_classes_are_event_specific_only() -> None:
    """CRM guards and integrity errors belong to one event; privilege, schema, data and transient errors do not."""
    assert feed.REFUSAL_CLASSES == {"CR", "23"}
    assert "apply_refused" in feed.REASONS
