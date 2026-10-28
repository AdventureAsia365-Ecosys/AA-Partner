"""Database.readiness() against a fake pool: every failure is a 503 body, never a leaked DSN."""

from __future__ import annotations

import datetime as dt
from contextlib import asynccontextmanager
from typing import Any

import pytest

import app.db as db_module
from app.cis import feed_state
from app.db import REQUIRED_SCHEMA_VERSION, REQUIRED_SCHEMA_VERSIONS, Database
from app.settings import Settings

DSN = "postgresql://crm_app:super-secret@db.internal:5432/aa_cis"  # gitleaks:allow (fake test DSN)
SETTINGS = Settings(database_url=DSN)


class FakeConn:
    def __init__(self, answers: dict[str, Any]) -> None:
        self._answers = answers

    async def fetchval(self, query: str) -> Any:
        answer = self._answers[query]
        if isinstance(answer, BaseException):
            raise answer
        return answer


class FakePool:
    def __init__(self, answers: dict[str, Any]) -> None:
        self._answers = answers
        self.closed = False

    async def fetchrow(self, query: str) -> Any:
        """crm.membership_feed_state (AA-978), read by the readiness probe when the feed is configured."""
        answer = self._answers[query]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    @asynccontextmanager
    async def acquire(self):
        yield FakeConn(self._answers)

    async def close(self) -> None:
        self.closed = True


VERSIONS_SQL = "SELECT coalesce(array_agg(version ORDER BY version), '{}') FROM crm.v_schema_versions"


def _answers(versions: Any = REQUIRED_SCHEMA_VERSIONS, select_one: Any = 1) -> dict[str, Any]:
    if isinstance(versions, tuple):
        versions = list(versions)
    return {"SELECT 1": select_one, VERSIONS_SQL: versions}


@pytest.fixture
def answers() -> dict[str, Any]:
    """What the fake connection returns per query; tests overwrite entries."""
    return _answers()


@pytest.fixture
def created(monkeypatch: pytest.MonkeyPatch, answers: dict[str, Any]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def fake_create_pool(**kwargs: Any) -> FakePool:
        calls.append(kwargs)
        return FakePool(answers)

    monkeypatch.setattr(db_module.asyncpg, "create_pool", fake_create_pool)
    return calls


@pytest.mark.asyncio
async def test_ready_when_db_answers_and_required_version_registered(created: list[dict[str, Any]]) -> None:
    result = await Database(SETTINGS).readiness()
    assert result.ok
    assert result.body == {
        "status": "ok",
        "db": "ok",
        "schema_version": REQUIRED_SCHEMA_VERSION,
        "registered_keys": sorted(REQUIRED_SCHEMA_VERSIONS),
        "membership_feed": "not_configured",
    }


M2M_JSON = (
    '{"client_id": "abc123", "client_secret": "placeholder", '
    '"token_url": "https://m2m-auth.example.invalid/oauth2/token", "scopes": ["cis.tenant/read"]}'
)
FEED_SETTINGS = Settings(database_url=DSN, cis_api_url="https://api-cis.example.invalid", m2m_client=M2M_JSON)


def _feed_row(failures: int = 0, last_ok: dt.timedelta | None = dt.timedelta(seconds=30)) -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC)
    return {
        "feed_cursor": "c1",
        "failures": failures,
        "last_error_at": now if failures else None,
        "last_ok_at": now - last_ok if last_ok is not None else None,
    }


@pytest.mark.parametrize(
    ("row", "word"),
    [
        (_feed_row(), "ok"),
        (_feed_row(last_ok=None), "pending"),
        (_feed_row(last_ok=dt.timedelta(minutes=11)), "stale"),
        (_feed_row(failures=3), "failing"),
        (RuntimeError("no row"), "unknown"),
    ],
)
@pytest.mark.asyncio
async def test_the_membership_feed_shows_in_readiness_and_never_fails_it(
    created: list[dict[str, Any]], answers: dict[str, Any], row: Any, word: str
) -> None:
    """AA-978 review round 1: a stuck CIS feed shows on /crm/health/ready (one word), and readiness stays 200."""
    answers[feed_state.LOAD_SQL] = row
    result = await Database(FEED_SETTINGS).readiness()
    assert result.ok
    assert result.body["membership_feed"] == word


@pytest.mark.asyncio
async def test_not_ready_when_only_an_older_version_is_registered(
    created: list[dict[str, Any]], answers: dict[str, Any]
) -> None:
    answers.update(_answers(versions=["crm-001"]))
    result = await Database(SETTINGS).readiness()
    assert not result.ok
    assert result.body["schema_version"] == "crm-001"
    assert result.body["required_schema_version"] == REQUIRED_SCHEMA_VERSION
    assert result.body["missing_schema_versions"] == [v for v in REQUIRED_SCHEMA_VERSIONS if v != "crm-001"]


@pytest.mark.asyncio
async def test_not_ready_when_an_older_required_version_is_missing(
    created: list[dict[str, Any]], answers: dict[str, Any]
) -> None:
    # The newest file is in but one before it is not (e.g. crm-002 applied, crm-001 never ran): not ready.
    gap = [v for v in REQUIRED_SCHEMA_VERSIONS if v != "crm-001"]
    answers.update(_answers(versions=gap))
    result = await Database(SETTINGS).readiness()
    assert not result.ok
    assert result.body["schema_version"] == REQUIRED_SCHEMA_VERSION
    assert result.body["missing_schema_versions"] == ["crm-001"]
    assert "crm-001" not in result.body["registered_keys"]  # AA-993 M4a: the gap is visible to the schema gate


@pytest.mark.asyncio
async def test_pool_is_lazy_single_and_requires_ssl(created: list[dict[str, Any]]) -> None:
    db = Database(SETTINGS)
    assert created == []  # nothing at construction time
    await db.readiness()
    await db.readiness()
    assert len(created) == 1
    assert created[0]["ssl"] == "require"
    assert created[0]["min_size"] == 0
    assert created[0]["max_size"] == 3  # crm_app CONNECTION LIMIT 6 = 3 x 2 tasks (Infra connection budget)
    assert created[0]["server_settings"] == {
        "application_name": "crm-api",
        "idle_in_transaction_session_timeout": "120000",
    }
    await db.close()


@pytest.mark.asyncio
async def test_newer_schema_is_ready(created: list[dict[str, Any]], answers: dict[str, Any]) -> None:
    answers.update(_answers(versions=[*REQUIRED_SCHEMA_VERSIONS, "crm-999"]))
    result = await Database(SETTINGS).readiness()
    assert result.ok and result.body["schema_version"] == "crm-999"
    assert result.body["registered_keys"][-1] == "crm-999"


@pytest.mark.asyncio
async def test_not_ready_when_no_crm_version(created: list[dict[str, Any]], answers: dict[str, Any]) -> None:
    answers.update(_answers(versions=[]))
    result = await Database(SETTINGS).readiness()
    assert not result.ok
    assert result.body == {
        "status": "not_ready",
        "db": "ok",
        "schema_version": None,
        "required_schema_version": REQUIRED_SCHEMA_VERSION,
        "missing_schema_versions": list(REQUIRED_SCHEMA_VERSIONS),
        "registered_keys": [],
    }


@pytest.mark.asyncio
async def test_not_ready_when_view_missing(created: list[dict[str, Any]], answers: dict[str, Any]) -> None:
    class UndefinedTableError(Exception):
        pass

    answers.update(_answers(versions=UndefinedTableError("relation does not exist")))
    result = await Database(SETTINGS).readiness()
    assert not result.ok
    assert result.body["db"] == "ok"
    assert result.body["error"] == "UndefinedTableError"


@pytest.mark.asyncio
async def test_select_one_failure_is_db_error(created: list[dict[str, Any]], answers: dict[str, Any]) -> None:
    answers.update(_answers(select_one=TimeoutError()))
    result = await Database(SETTINGS).readiness()
    assert not result.ok
    assert result.body["db"] == "error" and result.body["error"] == "TimeoutError"


@pytest.mark.asyncio
async def test_db_error_does_not_leak_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing_create_pool(**kwargs: Any) -> FakePool:
        raise OSError(f"could not connect to {DSN}")

    monkeypatch.setattr(db_module.asyncpg, "create_pool", failing_create_pool)
    db = Database(SETTINGS)
    result = await db.readiness()
    assert not result.ok
    assert result.body == {"status": "not_ready", "db": "error", "error": "OSError", "schema_version": None}
    assert "super-secret" not in repr(result.body)


@pytest.mark.asyncio
async def test_pool_creation_is_retried_after_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[int] = []

    async def flaky_create_pool(**kwargs: Any) -> FakePool:
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("db down")
        return FakePool(_answers())

    monkeypatch.setattr(db_module.asyncpg, "create_pool", flaky_create_pool)
    db = Database(SETTINGS)
    assert not (await db.readiness()).ok
    assert (await db.readiness()).ok
    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_close_without_pool_is_a_noop() -> None:
    await Database(SETTINGS).close()
