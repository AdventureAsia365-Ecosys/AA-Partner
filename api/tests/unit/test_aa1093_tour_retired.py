"""AA-1093 part 1 (crm-063): the pure parts of the retired-tour fan-out (``app.catalog.retired``) and its wiring in the
catalog sync. The database side (flags, tasks, notes, idempotency, isolation) is
``tests/integration/test_aa1093_tour_retired_db.py``.

- Which served-state moves are transitions (paused alone never is, root ADR 0015 decision 5).
- The job row: an ids-only payload the payload guard accepts, one idempotency key per tour, transition and change
  time, no row for a source id a payload cannot carry; the handler refuses a bad payload for good.
- The run-time check that the tour is still in the transition's state; the task priority rule.
- The sync counts the fan-out rows its state writes queued.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from typing import Any

import pytest

from app.catalog import retired, sync
from app.catalog.client import TourAnswer
from app.jobs.kinds import EXPECTED_SECONDS, TOUR_RETIRED_FANOUT
from app.jobs.payload_guard import check_payload
from app.jobs.queue import Job
from app.jobs.worker import PermanentError
from app.task_kinds import TASK_KINDS

TOUR = "3b7e9d20-5c41-4f8a-a6e2-0d9c8b1f7a34"
AT = dt.datetime(2026, 11, 13, 4, 5, 6, 789012, tzinfo=dt.UTC)


@pytest.mark.parametrize(
    ("old", "new", "made"),
    [
        ("served", "retired", "retired"),
        ("paused", "retired", "retired"),
        ("retired", "served", "reinstated"),
        ("served", "paused", None),
        ("paused", "served", None),
        ("retired", "paused", None),
        ("retired", "retired", None),
        ("served", "served", None),
        (None, "served", None),  # first seen
        (None, "retired", None),  # first seen already retired: no agency item can use it
    ],
)
def test_transitions(old: str | None, new: str, made: str | None) -> None:
    assert retired.transition(old, new) == made


def test_the_job_row_is_ids_only_and_keyed_per_transition() -> None:
    job = retired.fanout_job(TOUR, "retired", AT)
    assert job is not None
    payload, key = job
    check_payload(payload)  # the guard enqueue() applies
    us = retired.changed_us(AT)
    assert payload == {"source_id": TOUR, "purpose": "retired", "changed_at": us}
    assert key == f"crm_tour_retired_fanout:retired:{TOUR}:{us}"
    assert retired.fanout_job(TOUR, "reinstated", AT)[1] != key  # type: ignore[index]
    later = retired.fanout_job(TOUR, "retired", AT + dt.timedelta(microseconds=1))
    assert later is not None and later[1] != key
    assert retired.fanout_job("T-100", "retired", AT) is None  # not a uuid: a payload cannot carry it
    assert retired.fanout_job(TOUR, "paused", AT) is None


def test_changed_us_is_exact() -> None:
    assert retired.changed_us(AT) == 1_794_542_706_789_012
    assert retired.changed_us(dt.datetime(1970, 1, 1, tzinfo=dt.UTC)) == 0


def test_the_kinds_are_registered() -> None:
    assert retired.KIND == TOUR_RETIRED_FANOUT == "crm_tour_retired_fanout"
    assert TOUR_RETIRED_FANOUT in EXPECTED_SECONDS
    assert retired.TASK in TASK_KINDS
    assert retired.raised_key("retired", 12) == "retired:12"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"source_id": "T-100", "purpose": "retired", "changed_at": 1},
        {"source_id": TOUR, "purpose": "paused", "changed_at": 1},
        {"source_id": TOUR, "purpose": "retired", "changed_at": "1"},
        {"source_id": TOUR, "purpose": "retired", "changed_at": True},
        {"source_id": TOUR, "purpose": "retired", "changed_at": 0},
    ],
)
async def test_a_bad_payload_fails_for_good(payload: dict[str, Any]) -> None:
    handler = retired.build_handler(None)
    with pytest.raises(PermanentError):
        await handler(object(), Job("j", retired.KIND, payload, 1, 3))  # type: ignore[arg-type]


def test_the_tour_must_still_be_in_the_transitions_state() -> None:
    us = retired.changed_us(AT)
    gone = {"served_state": "retired", "state_changed_at": AT}
    assert retired._current(gone, "retired", us)
    assert not retired._current(gone, "retired", us + 1)  # retired again later: the later row does the work
    assert not retired._current({"served_state": "served", "state_changed_at": AT}, "retired", us)
    assert retired._current({"served_state": "served", "state_changed_at": AT}, "reinstated", us)
    assert retired._current({"served_state": "paused", "state_changed_at": AT}, "reinstated", us)
    assert not retired._current(gone, "reinstated", us)
    assert not retired._current(None, "retired", us)


@pytest.mark.asyncio
async def test_the_sync_counts_the_rows_its_state_writes_queued(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = {"set_state": "retired", "upsert": "reinstated"}

    async def set_state(_pool: Any, _sid: str, _state: str, _reason: Any) -> str | None:
        return answers["set_state"]

    async def upsert(_pool: Any, _s: Any, _etag: Any, _body: Any) -> str | None:
        return answers["upsert"]

    monkeypatch.setattr(sync.store, "set_state", set_state)
    monkeypatch.setattr(sync.store, "upsert", upsert)
    counts: Counter[str] = Counter()
    await sync._store_answer(object(), TOUR, TourAnswer("retired", None, None, "source_deleted"), counts)  # type: ignore[arg-type]
    body = {"source_system": "cis", "source_id": TOUR, "title": "Pine ridge 2 days", "country_code": "LA",
            "duration_days": 2, "content_hash": "sha256:1"}  # fmt: skip
    await sync._store_answer(object(), TOUR, TourAnswer("served", body, '"e"', None), counts)  # type: ignore[arg-type]
    answers["set_state"] = "unkeyed"
    await sync._store_answer(object(), "T-9", TourAnswer("paused", None, None, None), counts)  # type: ignore[arg-type]
    assert counts == Counter(
        {"retired": 1, "fetched": 1, "paused": 1, "fanout_retired": 1, "fanout_reinstated": 1, "fanout_unkeyed": 1}
    )


# ── one agency and the handler, on a fake connection (the SQL itself runs in the integration test) ──────────────────


class _Conn:
    """Answers the module's SQL texts from fixed rows; records what was written."""

    def __init__(self, items: list[dict[str, Any]], *, tour: dict[str, Any] | None = None) -> None:
        self.items = items
        self.tour = tour
        self.flags: list[tuple[Any, ...]] = []
        self.tasks: list[tuple[Any, ...]] = []
        self.notes: list[tuple[Any, ...]] = []
        self.cleared: list[dict[str, Any]] = []
        self.note_trips: list[dict[str, Any]] = []
        self.view: list[dict[str, Any]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        if sql == retired.ITEMS_SQL:
            return self.items
        if sql == retired.CLEAR_SQL:
            return self.cleared
        if sql == retired.NOTE_TRIPS_SQL:
            return self.note_trips
        if sql == retired.FLAGGED_SQL:
            return [{"trip_id": t} for t in args[0] if t in {"trip-a"}]
        if sql == retired.VIEW_SQL:
            return self.view
        raise AssertionError(sql)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if sql == retired.FLAG_SQL:
            key = (args[0], args[3] or args[4], args[5])
            if key in {(f[0], f[3] or f[4], f[5]) for f in self.flags}:
                return None
            self.flags.append(args)
            return len(self.flags)
        if sql == retired.TASK_SQL:
            if any(t[0] == args[0] for t in self.tasks):
                return None
            self.tasks.append(args)
            return len(self.tasks)
        if sql == retired.LOCALE_SQL:
            return "vi"
        raise AssertionError(sql)

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        assert sql == retired.TOUR_SQL
        return self.tour

    async def execute(self, sql: str, *args: Any) -> None:
        assert sql == retired.NOTE_SQL
        self.notes.append(args)


class _Ctx:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn
        self.tenant_id = "tenant-a"
        self.events: list[tuple[Any, ...]] = []

    def audit(self, *args: Any, **kw: Any) -> None:
        self.events.append((args, kw))


class _Vault:
    async def encrypt(self, table: str, column: str, row_id: Any, text: str) -> bytes:
        assert (table, column) == ("note", "body_enc")
        return text.encode()


def _items() -> list[dict[str, Any]]:
    return [
        {"trip_id": "trip-a", "kind": "open_option", "item_id": "opt-1"},
        {"trip_id": "trip-a", "kind": "held_line", "item_id": "line-1"},
        {"trip_id": "trip-b", "kind": "confirmed_line", "item_id": "line-2"},
    ]


@pytest.mark.asyncio
async def test_raise_flags_once_per_item_and_one_task_per_trip() -> None:
    conn = _Conn(_items())
    ctx = _Ctx(conn)
    counts = await retired.raise_flags(ctx, TOUR, 7)  # type: ignore[arg-type]
    assert counts == Counter({"flag": 3, "task": 2, "trip": 2})
    assert [(f[0], f[2], f[3], f[4], f[5]) for f in conn.flags] == [
        ("trip-a", "open_option", "opt-1", None, "retired:7"),
        ("trip-a", "held_line", None, "line-1", "retired:7"),
        ("trip-b", "confirmed_line", None, "line-2", "retired:7"),
    ]
    assert [(t[0], t[1], t[2]) for t in conn.tasks] == [
        ("trip-a", "aa_tour_discontinued", "urgent"),
        ("trip-b", "aa_tour_discontinued", "normal"),  # booked lines only: information
    ]
    assert ctx.events[0][0][:4] == ("aa_sync", "allowed", "aa_tour", TOUR)
    assert ctx.events[0][1]["object_ids"] == ("trip-a", "trip-b")
    # The same transition again: nothing new, no audit event.
    again = _Ctx(conn)
    assert await retired.raise_flags(again, TOUR, 7) == Counter()  # type: ignore[arg-type]
    assert again.events == []


@pytest.mark.asyncio
async def test_clear_flags_notes_only_trips_left_without_a_flag_and_with_the_task() -> None:
    conn = _Conn([])
    conn.cleared = [{"trip_id": "trip-a"}, {"trip_id": "trip-a"}, {"trip_id": "trip-b"}]
    conn.note_trips = [{"trip_id": "trip-a"}]
    ctx = _Ctx(conn)
    counts = await retired.clear_flags(ctx, TOUR, 9, _Vault(), "Pine ridge 2 days")  # type: ignore[arg-type]
    assert counts == Counter({"cleared_trip": 2, "note": 1})
    (note_id, trip, body) = conn.notes[0]
    assert trip == "trip-a" and "Pine ridge 2 days" in body.decode() and "mở bán lại" in body.decode()
    assert ctx.events[0][1]["reason"] == "tour_reinstated_flags"
    skipped = await retired.clear_flags(_Ctx(conn), TOUR, 9, None, "Pine ridge 2 days")  # type: ignore[arg-type]
    assert skipped["note_skipped"] == 1 and "note" not in skipped
    conn.cleared = []
    assert await retired.clear_flags(_Ctx(conn), TOUR, 9, _Vault(), "x") == Counter()  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_the_reads() -> None:
    conn = _Conn([])
    ctx = _Ctx(conn)
    assert await retired.flagged(ctx, []) == set()  # type: ignore[arg-type]
    assert await retired.flagged(ctx, ["trip-a", "trip-b"]) == {"trip-a"}  # type: ignore[arg-type,list-item]
    conn.view = [
        {"source_id": TOUR, "title": "Pine ridge 2 days", "kind": "held_line", "raised_at": AT, "n": 2},
        {"source_id": TOUR, "title": "Pine ridge 2 days", "kind": "open_option", "raised_at": AT, "n": 1},
    ]
    assert await retired.trip_view(ctx, "trip-a") == [  # type: ignore[arg-type]
        {
            "source_id": TOUR,
            "title": "Pine ridge 2 days",
            "raised_at": AT.isoformat(),
            "items": {"open_option": 1, "held_line": 2, "quoted_line": 0, "confirmed_line": 0},
        }
    ]


class _Agency:
    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id


def _patch_agencies(monkeypatch: pytest.MonkeyPatch, outcomes: dict[str, Any]) -> list[str]:
    """registry.agencies lists ``outcomes``; system_tx yields a fake context or raises the outcome."""
    import contextlib

    opened: list[str] = []

    async def agencies(_pool: Any) -> list[_Agency]:
        return [_Agency(t) for t in outcomes]

    @contextlib.asynccontextmanager
    async def system_tx(_pool: Any, tenant_id: str, *, actor: str) -> Any:
        assert actor == retired.ACTOR
        opened.append(tenant_id)
        outcome = outcomes[tenant_id]
        if isinstance(outcome, BaseException):
            raise outcome
        yield _Ctx(outcome)

    monkeypatch.setattr(retired.registry, "agencies", agencies)
    monkeypatch.setattr(retired, "system_tx", system_tx)
    return opened


@pytest.mark.asyncio
async def test_the_handler_walks_every_agency(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.errors import Forbidden

    us = retired.changed_us(AT)
    gone = {"title": "Pine ridge 2 days", "served_state": "retired", "state_changed_at": AT}
    _patch_agencies(monkeypatch, {"a": _Conn(_items(), tour=gone), "locked": Forbidden("tenant_locked"),
                                  "b": _Conn([], tour=gone)})  # fmt: skip
    handler = retired.build_handler(None)
    payload = {"source_id": TOUR, "purpose": "retired", "changed_at": us}
    out = await handler(object(), Job("j", retired.KIND, payload, 1, 3))  # type: ignore[arg-type]
    check_payload(out, what="result")
    assert out == {"status": "done", "purpose": "retired", "agency_count": 2, "skipped_count": 1, "flag_count": 3,
                   "task_count": 2, "trip_count": 2}  # fmt: skip


@pytest.mark.asyncio
async def test_the_handler_stops_on_a_stale_row_and_retries_a_failed_agency(monkeypatch: pytest.MonkeyPatch) -> None:
    us = retired.changed_us(AT)
    served = {"title": "Pine ridge 2 days", "served_state": "served", "state_changed_at": AT}
    opened = _patch_agencies(monkeypatch, {"a": _Conn(_items(), tour=served), "b": _Conn([], tour=served)})
    handler = retired.build_handler(None)
    payload = {"source_id": TOUR, "purpose": "retired", "changed_at": us}
    assert await handler(object(), Job("j", retired.KIND, payload, 1, 3)) == {  # type: ignore[arg-type]
        "status": "stale", "purpose": "retired"}  # fmt: skip
    assert opened == ["a"]

    gone = {"title": "Pine ridge 2 days", "served_state": "retired", "state_changed_at": AT}
    _patch_agencies(monkeypatch, {"a": RuntimeError("boom"), "b": _Conn([], tour=gone)})
    with pytest.raises(RuntimeError, match="1 agencies failed"):
        await handler(object(), Job("j", retired.KIND, payload, 1, 3))  # type: ignore[arg-type]

    back = {"title": "Pine ridge 2 days", "served_state": "served", "state_changed_at": AT}
    conn = _Conn([], tour=back)
    conn.cleared = [{"trip_id": "trip-a"}]
    _patch_agencies(monkeypatch, {"a": conn})
    out = await handler(object(), Job("j", retired.KIND, {**payload, "purpose": "reinstated"}, 1, 3))  # type: ignore[arg-type]
    assert out["status"] == "done" and out["cleared_trip_count"] == 1
