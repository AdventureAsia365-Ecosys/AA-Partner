"""AA-952 (card CRM-23, C-M3 "confirmed"): the pure parts of the accepted proposal's AA lines and their confirm: the
contract 1.2 hold reader, the confirm answer of a whole set, a new line's state from Booking's hold answer, and the
sync step's Booking failures (an outage stops it, a 1.1 answer writes nothing, an unknown hold writes failed lines).
The SQL is covered by ``tests/integration/test_proposal_confirm_api.py``."""

from __future__ import annotations

import datetime as dt
import uuid
from collections import Counter
from decimal import Decimal
from typing import Any

import pytest

from app.booking import contract, proposal_lines, sync
from app.booking.sync import BookingSync
from tests.aa_fakes import FakeBooking, booking_client
from tests.unit.slice_fakes import TENANT, fake_tx, system_ctx

HOLD = {
    "hold_id": "h1", "quote_id": "q1", "status": "active", "expires_at": "2027-01-01T00:00:00+00:00",
    "extended_count": 0, "price_hash": "b" * 64, "currency": "USD",
    "fx": {"base_currency": "USD", "quote_currency": "USD", "rate_per_usd": "1", "rate_date": "2026-10-25",
           "source": "test"},
    "lines": [{"line_ref": "o1", "line_status": "available", "b2b_price": {"amount": "300.00", "currency": "USD"}},
              {"line_ref": "o2", "line_status": "on_request", "b2b_price": {"amount": "120.50", "currency": "USD"}}],
}  # fmt: skip


def test_held_set_reads_the_quote_of_a_1_2_hold_answer() -> None:
    held = contract.held_set(HOLD)
    assert held.hold_id == "h1" and held.quote_id == "q1" and held.price_hash == "b" * 64
    assert held.rate_per_usd == Decimal("1") and held.rate_date == dt.date(2026, 10, 25)
    assert held.lines["o1"].b2b_price == Decimal("300.00") and held.lines["o2"].line_status == "on_request"
    assert held.expires_at == dt.datetime(2027, 1, 1, tzinfo=dt.UTC)


@pytest.mark.parametrize(
    "broken",
    [
        {k: v for k, v in HOLD.items() if k != "price_hash"},  # a 1.1 answer
        {k: v for k, v in HOLD.items() if k != "fx"},
        {**HOLD, "status": "gone"},
        {**HOLD, "fx": {**HOLD["fx"], "rate_per_usd": "0"}},
        {**HOLD, "lines": [{"line_ref": "o1", "b2b_price": {"amount": "1.00", "currency": "EUR"}}]},
        {**HOLD, "lines": [{"line_ref": "o1"}]},
        {**HOLD, "lines": ["o1"]},
    ],
)
def test_held_set_refuses_what_it_cannot_write_from(broken: dict[str, Any]) -> None:
    with pytest.raises(contract.ContractError):
        contract.held_set(broken)


def test_booking_lines_answers_every_line_of_the_set() -> None:
    answer = {"booking_id": "b1", "hold_id": "h1", "trip_case_code": "TC-1",
              "lines": [{"line_ref": "o1", "status": "confirmed"}, {"line_ref": "o2", "status": "on_request"},
                        {"line_ref": "o3", "status": "weird"}, "junk"]}  # fmt: skip
    assert contract.booking_lines(answer) == {"o1": "confirmed", "o2": "on_request"}
    with pytest.raises(contract.ContractError):
        contract.booking_lines(None)


def test_a_new_line_reads_booking_s_hold() -> None:
    held = contract.held_set(HOLD)
    assert proposal_lines.line_state(held, "o1") == ("held", None)
    assert proposal_lines.line_state(held, "o9") == ("failed", "not_in_hold")
    assert proposal_lines.line_state(None, "o1") == ("failed", "hold_not_found")
    for status, code in (("expired", "hold_expired"), ("released", "hold_released")):
        assert proposal_lines.line_state(contract.held_set({**HOLD, "status": status}), "o1") == ("expired", code)
    assert proposal_lines.line_state(contract.held_set({**HOLD, "status": "confirmed"}), "o1") == ("held", None)


@pytest.mark.asyncio
async def test_the_sync_step_handles_booking_s_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = system_ctx()
    monkeypatch.setattr(sync, "system_tx", fake_tx(ctx))
    due = [{"proposal_id": uuid.uuid4(), "accepted_hold_id": h} for h in ("h1", "h404", "h2")]
    written: list[tuple[Any, Any]] = []

    async def due_sets(_ctx: Any) -> list[dict[str, Any]]:
        return due

    async def write_lines(_ctx: Any, proposal_id: Any, held: Any) -> dict[str, Any]:
        written.append((proposal_id, held))
        return {"outcome": "held" if held else "failed"}

    async def queue_confirms(_ctx: Any) -> list[Any]:
        return [uuid.uuid4()]

    monkeypatch.setattr(proposal_lines, "due_sets", due_sets)
    monkeypatch.setattr(proposal_lines, "write_lines", write_lines)
    monkeypatch.setattr(proposal_lines, "queue_confirms", queue_confirms)
    fake = FakeBooking()
    fake.holds["h1"] = dict(HOLD)
    fake.holds["h2"] = {k: v for k, v in HOLD.items() if k != "fx"} | {"hold_id": "h2"}  # still on 1.1
    counts: Counter[str] = Counter()
    assert await BookingSync(booking_client(fake)).proposal_sets(object(), TENANT, counts) is None  # type: ignore[arg-type]
    assert [w[1].hold_id if w[1] else None for w in written] == ["h1", None]  # h404 unknown: failed lines
    assert counts["proposal_lines_held"] == 1 and counts["proposal_lines_failed"] == 1
    assert counts["proposal_lines_retry"] == 1 and counts["confirms_queued"] == 1
    # Booking gives no answer: the step stops and tells the run (no confirm queued, nothing written).
    written.clear()
    fake.down = True
    out = await BookingSync(booking_client(fake)).proposal_sets(object(), TENANT, Counter())  # type: ignore[arg-type]
    assert out == "booking_timeout" and written == []
    # A refusal other than not_found (an agency Booking does not know) is tried again next run.
    fake.down = False
    fake.holds["h1"] = {}

    class Forbidding(FakeBooking):
        async def __call__(self, method: str, url: str, *a: Any) -> Any:
            if "/holds/" in url:
                return type(await super().__call__(method, url, *a))(403, b'{"error": "unknown_partner"}')
            return await super().__call__(method, url, *a)

    counts = Counter()
    await BookingSync(booking_client(Forbidding())).proposal_sets(object(), TENANT, counts)  # type: ignore[arg-type]
    assert counts["proposal_lines_refused"] == 3 and written == []


@pytest.mark.asyncio
async def test_one_broken_proposal_does_not_stop_the_agency(monkeypatch: pytest.MonkeyPatch) -> None:
    """A proposal whose lines cannot be written (no FX rate yet, a CHECK) is counted and skipped: the other proposals,
    the confirms, the request queue and the feed of the agency still run. A locked agency still stops the run."""
    ctx = system_ctx()
    monkeypatch.setattr(sync, "system_tx", fake_tx(ctx))
    due = [{"proposal_id": uuid.uuid4(), "accepted_hold_id": "h1"} for _ in range(3)]
    broken = due[1]["proposal_id"]
    written: list[Any] = []

    async def due_sets(_ctx: Any) -> list[dict[str, Any]]:
        return due

    async def write_lines(_ctx: Any, proposal_id: Any, held: Any) -> dict[str, Any]:
        if proposal_id == broken:
            raise KeyError("fx")
        written.append(proposal_id)
        return {"outcome": "held"}

    async def queue_confirms(_ctx: Any) -> list[Any]:
        raise RuntimeError("check_violation")

    monkeypatch.setattr(proposal_lines, "due_sets", due_sets)
    monkeypatch.setattr(proposal_lines, "write_lines", write_lines)
    monkeypatch.setattr(proposal_lines, "queue_confirms", queue_confirms)
    fake = FakeBooking()
    fake.holds["h1"] = dict(HOLD)
    client = booking_client(fake)
    counts: Counter[str] = Counter()
    assert await BookingSync(client).proposal_sets(object(), TENANT, counts) is None  # type: ignore[arg-type]
    assert written == [due[0]["proposal_id"], due[2]["proposal_id"]]
    assert counts["proposal_lines_held"] == 2 and counts["proposal_lines_error"] == 1 and counts["confirms_error"] == 1

    # run_agency goes on to the request queue (and the feed) after the broken proposal.
    reached: list[str] = []

    async def no_requests(*_a: Any, **_k: Any) -> list[Any]:
        reached.append("requests")
        return []

    async def feed(*_a: Any, **_k: Any) -> None:
        reached.append("feed")

    monkeypatch.setattr(ctx.conn, "fetch", no_requests, raising=False)
    monkeypatch.setattr(BookingSync, "pull_feed", feed)
    await BookingSync(client).run_agency(object(), TENANT, Counter(), feed=True)  # type: ignore[arg-type]
    assert reached == ["requests", "feed"]

    async def locked(_ctx: Any, proposal_id: Any, held: Any) -> dict[str, Any]:
        raise sync.Forbidden("agency_locked")

    monkeypatch.setattr(proposal_lines, "write_lines", locked)
    with pytest.raises(sync.Forbidden):
        await BookingSync(client).proposal_sets(object(), TENANT, Counter())  # type: ignore[arg-type]
