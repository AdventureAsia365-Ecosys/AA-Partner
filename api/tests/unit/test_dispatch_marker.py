"""AA-993 G4b: the ``crm_dispatch`` send marker (``crm.dispatch_attempt``, crm-023) over a stateful fake.

The fake keeps one dispatch row and its attempts, and answers the worker's SQL by fragment; every statement "commits"
(the real two-transaction split, rollbacks included, is covered on PostgreSQL in
tests/integration/test_crm023_schema.py). The cases are the design's (S252 §2 G4b): a run cancelled after the provider
call returned sends once and the re-run holds the row ``send_uncertain``; a run cancelled before the sender was called
leaves no open attempt, so the re-run sends.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from app.dispatch import worker as dispatch_worker
from app.errors import Forbidden
from app.jobs.kinds import DEPLOY_BLOCKING_KINDS, DISPATCH
from app.jobs.queue import Job
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import TENANT, fake_tx, system_ctx

DISPATCH_ID = uuid.UUID("00000000-0000-4000-8000-0000000000d1")
MESSAGE_ID = uuid.UUID("00000000-0000-4000-8000-0000000000e1")


class Db:
    """One dispatch row (status, gate_result), its attempts and the message's failure, answered by SQL fragment."""

    def __init__(self, **row: Any) -> None:
        self.row: dict[str, Any] = {
            "dispatch_id": DISPATCH_ID,
            "status": "queued",
            "channel": "email",
            "purpose": "transactional",
            "message_ref": None,
            "message_id": MESSAGE_ID,
            "traveler_id": uuid.uuid4(),
            "trip_id": None,
            "archived_at": None,
            "traveler_deleted_at": None,
            "trip_deleted_at": None,
            "marketing_ok": None,
        } | row
        self.gate_result: str | None = None
        self.attempts: dict[uuid.UUID, dict[str, Any]] = {}
        self.message_failure: str | None = None
        self.row_reads = 0
        self.cancel_on_row_read: int | None = None
        self.conn = (
            ScriptedConn()
            .on("FROM crm.dispatch_queue d", self._row)
            .on("SELECT attempt_id FROM crm.dispatch_attempt", self._open)
            .on("INSERT INTO crm.dispatch_attempt", self._insert)
            .on("UPDATE crm.dispatch_attempt SET decided_at", self._close)
            .on("UPDATE crm.dispatch_queue SET status = $2", self._decide)
            .on("UPDATE crm.message SET status = 'failed'", self._fail_message)
        )

    def _row(self, *_: Any) -> Any:
        self.row_reads += 1
        if self.cancel_on_row_read == self.row_reads:
            return asyncio.CancelledError()
        return dict(self.row)

    def _open(self, dispatch_id: uuid.UUID) -> uuid.UUID | None:
        return next((k for k, a in self.attempts.items() if a["outcome"] is None), None)

    def _insert(self, dispatch_id: uuid.UUID, job_attempt: int) -> uuid.UUID:
        assert self._open(dispatch_id) is None, "dispatch_attempt_open_uq: one open attempt per dispatch row"
        attempt_id = uuid.uuid4()
        self.attempts[attempt_id] = {"job_attempt": job_attempt, "outcome": None}
        return attempt_id

    def _close(self, attempt_id: uuid.UUID, outcome: str) -> None:
        if self.attempts[attempt_id]["outcome"] is None:  # WHERE decided_at IS NULL
            self.attempts[attempt_id]["outcome"] = outcome

    def _decide(self, dispatch_id: uuid.UUID, status: str, code: str) -> None:
        self.row["status"], self.gate_result = status, code

    def _fail_message(self, message_id: uuid.UUID, code: str) -> None:
        self.message_failure = code

    def outcomes(self) -> list[str | None]:
        return [a["outcome"] for a in self.attempts.values()]


class Sender:
    """Records each provider call; ``then`` runs after the call returned (raise to cancel or fail)."""

    def __init__(self, outcome: str = "sent", then: BaseException | None = None) -> None:
        self.calls = 0
        self.outcome = outcome
        self.then = then
        self.attempts_seen: list[int] = []

    async def send(self, ctx: Any, dispatch_id: uuid.UUID, channel: str, message_id: Any) -> str:
        self.calls += 1
        self.attempts_seen.append(len(ctx.conn.sql("INSERT INTO crm.dispatch_attempt")))
        if self.then is not None:
            then, self.then = self.then, None
            raise then
        return self.outcome


def _job(attempt: int = 1) -> Job:
    return Job("j", DISPATCH, {"tenant_id": str(TENANT), "dispatch_id": str(DISPATCH_ID)}, attempt, 5)


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> Db:
    state = Db()
    monkeypatch.setattr(dispatch_worker, "system_tx", fake_tx(system_ctx(state.conn)))
    return state


@pytest.mark.asyncio
async def test_a_send_opens_an_attempt_before_the_call_and_closes_it_with_the_decision(db: Db) -> None:
    sender = Sender()
    assert await dispatch_worker.build_handler(sender)(object(), _job()) == {  # type: ignore[arg-type]
        "dispatch_id": str(DISPATCH_ID),
        "status": "sent",
    }
    assert sender.calls == 1 and db.row["status"] == "sent" and db.outcomes() == ["sent"]
    writes = [
        sql.split()[2 if sql.startswith("INSERT") else 1] for sql, _ in db.conn.calls if sql[:6] in ("INSERT", "UPDATE")
    ]
    assert writes == ["crm.dispatch_attempt", "crm.dispatch_queue", "crm.dispatch_attempt"]
    # The attempt was committed before the provider call: the sender saw it.
    assert sender.attempts_seen == [1]
    assert db.conn.sql("INSERT INTO crm.dispatch_attempt") == [(DISPATCH_ID, 1)]


@pytest.mark.asyncio
async def test_cancelled_after_the_provider_returned_sends_once_and_holds_send_uncertain(db: Db) -> None:
    """The design case: a fake sender cancelled after ``send`` returns -> one send, the row ends held/send_uncertain."""
    sender = Sender(then=asyncio.CancelledError())
    handler = dispatch_worker.build_handler(sender)
    with pytest.raises(asyncio.CancelledError):
        await handler(object(), _job())  # type: ignore[arg-type]
    assert sender.calls == 1 and db.row["status"] == "queued" and db.outcomes() == [None]  # left open
    # The worker releases the job; the new task re-claims it with the same attempt number.
    result = await handler(object(), _job())  # type: ignore[arg-type]
    assert result == {"dispatch_id": str(DISPATCH_ID), "status": "held", "reason_code": "send_uncertain"}
    assert sender.calls == 1  # never sent a second time
    assert (db.row["status"], db.gate_result) == ("held", "send_uncertain")
    assert db.outcomes() == ["send_uncertain"] and db.message_failure == "send_uncertain"
    # A third run (a requeue would not pick send_uncertain; a stray job would) skips the decided row.
    assert (await handler(object(), _job()))["status"] == "skipped"  # type: ignore[arg-type]
    assert sender.calls == 1


@pytest.mark.asyncio
async def test_cancelled_before_the_sender_was_called_allows_the_resend(db: Db) -> None:
    """The design case: cancelled before ``send`` -> the attempt is closed not_sent, and the re-run sends."""
    sender = Sender()
    handler = dispatch_worker.build_handler(sender)
    db.cancel_on_row_read = 2  # the second transaction's row lock, after the attempt committed
    with pytest.raises(asyncio.CancelledError):
        await handler(object(), _job())  # type: ignore[arg-type]
    assert sender.calls == 0 and db.outcomes() == ["not_sent"]
    assert (await handler(object(), _job()))["status"] == "sent"  # type: ignore[arg-type]
    assert sender.calls == 1 and db.outcomes() == ["not_sent", "sent"]


@pytest.mark.asyncio
async def test_a_sender_that_raises_closes_the_attempt_retry_so_the_retry_sends(db: Db) -> None:
    sender = Sender(then=ConnectionError("provider unreachable"))
    handler = dispatch_worker.build_handler(sender)
    with pytest.raises(ConnectionError):
        await handler(object(), _job(1))  # type: ignore[arg-type]
    assert db.outcomes() == ["retry"] and db.row["status"] == "queued"
    assert (await handler(object(), _job(2)))["status"] == "sent"  # type: ignore[arg-type]
    assert sender.calls == 2 and db.outcomes() == ["retry", "sent"]
    assert [a["job_attempt"] for a in db.attempts.values()] == [1, 2]


@pytest.mark.asyncio
async def test_the_last_attempt_that_raises_drops_and_closes_send_failed(db: Db) -> None:
    sender = Sender(then=ConnectionError("provider unreachable"))
    result = await dispatch_worker.build_handler(sender)(object(), _job(5))  # type: ignore[arg-type]
    assert result == {"dispatch_id": str(DISPATCH_ID), "status": "dropped", "reason_code": "send_failed"}
    assert db.outcomes() == ["send_failed"] and db.message_failure == "retries_exhausted"


@pytest.mark.asyncio
async def test_a_row_undone_or_archived_between_the_two_transactions_is_not_sent(
    db: Db, monkeypatch: pytest.MonkeyPatch
) -> None:
    sender = Sender()
    handler = dispatch_worker.build_handler(sender)
    reads = {"n": 0}

    def undone(*_: Any) -> dict[str, Any]:
        reads["n"] += 1
        return db.row | ({"status": "cancelled"} if reads["n"] == 2 else {})

    db.conn.on("FROM crm.dispatch_queue d", undone)
    assert (await handler(object(), _job()))["status"] == "skipped"  # type: ignore[arg-type]
    assert sender.calls == 0 and db.outcomes() == ["not_sent"]

    other = Db()
    monkeypatch.setattr(dispatch_worker, "system_tx", fake_tx(system_ctx(other.conn)))
    reads["n"] = 0

    def archived(*_: Any) -> dict[str, Any]:
        reads["n"] += 1
        return other.row | ({"archived_at": "2026-10-28"} if reads["n"] == 2 else {})

    other.conn.on("FROM crm.dispatch_queue d", archived)
    result = await handler(object(), _job())  # type: ignore[arg-type]
    assert result == {"dispatch_id": str(DISPATCH_ID), "status": "dropped", "reason_code": "archived"}
    assert sender.calls == 0 and other.outcomes() == ["not_sent"] and other.message_failure == "archived"


@pytest.mark.asyncio
async def test_an_open_attempt_wins_over_the_gate(db: Db) -> None:
    """An uncertain send is reported as uncertain even when the client was archived since: the message may be out."""
    db.attempts[uuid.uuid4()] = {"job_attempt": 1, "outcome": None}
    db.row["archived_at"] = "2026-10-28"
    result = await dispatch_worker.build_handler(Sender())(object(), _job())  # type: ignore[arg-type]
    assert result["reason_code"] == "send_uncertain" and db.outcomes() == ["send_uncertain"]


@pytest.mark.asyncio
async def test_closing_alone_is_best_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cancelled run whose own close fails (the agency was locked meanwhile) leaves the attempt open: the next run
    holds the row send_uncertain instead of sending, never twice."""

    class Locked:
        def __call__(self, *a: Any, **k: Any) -> Any:
            return self

        async def __aenter__(self) -> Any:
            raise Forbidden("tenant_locked")

        async def __aexit__(self, *_: Any) -> None:  # pragma: no cover — never entered
            return None

    monkeypatch.setattr(dispatch_worker, "system_tx", Locked())
    await dispatch_worker._close_alone(object(), TENANT, uuid.uuid4(), "not_sent")  # type: ignore[arg-type]


def test_crm_dispatch_left_the_deploy_blocking_kinds() -> None:
    """AA-993 G4b: with the committed marker a deploy mid-send cannot send twice, so crm_dispatch no longer blocks."""
    assert DISPATCH not in DEPLOY_BLOCKING_KINDS
