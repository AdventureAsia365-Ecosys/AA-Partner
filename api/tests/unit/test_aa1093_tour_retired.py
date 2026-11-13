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
