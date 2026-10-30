"""AA-952 (card CRM-23, milestone C-M3 "confirmed") end to end on PostgreSQL with the RDS role shape: the AA lines of an
accepted builder version come from its Booking hold set, and the paid deposit confirms that set with Booking, through
the Booking sync (``job:crm_booking_sync``) against the in-memory Booking of ``tests/aa_fakes.py`` (synthetic data).

- accept -> one AA line per held option (line id = option id = Booking's line_ref), net from Booking's hold answer
  (contract 1.2), sell = the option's sell; the assistant sees the lines but never their money or margin;
- deposit paid -> one confirm for the whole set, every line confirmed, the trip stays booked; later runs send nothing;
- a confirm whose answer was lost is sent again with the same Idempotency-Key: one booking at Booking;
- a hold that expired before the deposit -> lines ``expired`` with a task, no confirm;
- Booking refuses the confirm -> every line ``failed`` with the code and a task; the trip is not moved back;
- Booking still on 1.1 (no quote in the hold answer) -> nothing written until it answers with it.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import Counter
from decimal import Decimal
from typing import Any

import asyncpg
import pytest

from app.booking.sync import BookingSync
from app.ledger import lines
from app.proposals import service
from app.stages import move
from app.tenancy import tenant_tx
from app.vault import Vault
from tests.aa_fakes import booking_client
from tests.integration.test_builder_api import _pid
from tests.integration.test_client_proposal_api import _accept, _hold, _raw, _sent

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _accepted(pool: asyncpg.Pool) -> dict[str, Any]:
    """A sent version with two AA blocks (the day tour 500 and rafting 200 by default), held and accepted."""
    s = await _sent(pool, second_aa=True)
    status, out = await _hold(pool, s)
    assert status == 200 and out["outcome"] == "held"
    assert (await _accept(pool, s, out["page"]))["status"] == "accepted"
    row = (await _raw(pool, s, "SELECT accepted_hold_id FROM crm.proposal WHERE proposal_id = $1", _pid(s["v"])))[0]
    s["hold_id"] = row["accepted_hold_id"]
    return s


async def _sync(pool: asyncpg.Pool, s: dict[str, Any]) -> Counter[str]:
    counts: Counter[str] = Counter()
    await BookingSync(booking_client(s["fake"])).run_agency(pool, s["tenant"], counts, feed=False)
    return counts


async def _aa_lines(pool: asyncpg.Pool, s: dict[str, Any]) -> list[dict[str, Any]]:
    rows = await _raw(pool, s, "SELECT booking_line_id, status, aa_sub_status, aa_result_code, bk_hold_id, "
                      "bk_booking_id, aa_price_hash, title FROM crm.booking_line WHERE trip_id = $1 "
                      "AND supplier_kind = 'aa' ORDER BY booking_line_id", s["trip"])  # fmt: skip
    return [dict(r) for r in rows]


async def _pay_deposit(pool: asyncpg.Pool, s: dict[str, Any]) -> dict[str, Any]:
    sched = await _raw(pool, s, "SELECT schedule_id FROM crm.payment_schedule WHERE proposal_id = $1 "
                       "AND kind = 'deposit'", _pid(s["v"]))  # fmt: skip
    async with tenant_tx(pool, s["who"]["agent"], s["tenant"]) as ctx:
        return await service.mark_paid(ctx, Vault(s["keys"], ctx), sched[0]["schedule_id"], received_on=None,
                                       method="bank_transfer")  # fmt: skip


async def _stage(pool: asyncpg.Pool, s: dict[str, Any]) -> str:
    return (await _raw(pool, s, "SELECT stage FROM crm.trip WHERE trip_id = $1", s["trip"]))[0]["stage"]


async def _tasks(pool: asyncpg.Pool, s: dict[str, Any]) -> set[str]:
    rows = await _raw(pool, s, "SELECT kind FROM crm.task WHERE trip_id = $1 AND status = 'open'", s["trip"])
    return {r["kind"] for r in rows}


async def _commitment(pool: asyncpg.Pool, s: dict[str, Any]) -> list[str]:
    rows = await _raw(pool, s, "SELECT line_status FROM crm.trip_commitment WHERE source_ref = $1", s["hold_id"])
    return [r["line_status"] for r in rows]


async def _due_now(pool: asyncpg.Pool, s: dict[str, Any]) -> None:
    await _raw(pool, s, "UPDATE crm.aa_request SET next_attempt_at = now() WHERE status = 'pending'")


async def test_accept_writes_the_aa_lines_and_the_deposit_confirms_the_set_once(app_pool: asyncpg.Pool) -> None:
    s = await _accepted(app_pool)
    fake = s["fake"]
    fake.confirm_line_status = "confirmed"
    # Accept asked the sync to run for the agency now (one nudge row, ids only).
    jobs = await _raw(app_pool, s, "SELECT payload FROM crm.job WHERE kind = 'crm_booking_sync' AND idempotency_key = "
                      "$1", f"crm_booking_sync:accepted:{_pid(s['v'])}")  # fmt: skip
    assert len(jobs) == 1

    counts = await _sync(app_pool, s)
    assert counts["proposal_lines_held"] == 1 and counts["confirms_queued"] == 0
    made = await _aa_lines(app_pool, s)
    # No client pick: the accepted AA options are the blocks' defaults.
    defaults = await _raw(app_pool, s, "SELECT o.option_id, o.sell_amount FROM crm.option o JOIN crm.option_block b "
                          "ON b.default_option_id = o.option_id WHERE o.proposal_id = $1 AND o.source = 'aa'",
                          _pid(s["v"]))  # fmt: skip
    chosen = {r["option_id"]: r["sell_amount"] for r in defaults}
    # One line per held option, its id the option's (Booking's line_ref), on the accepted hold, held.
    assert {m["booking_line_id"] for m in made} == set(chosen) and len(made) == 2
    assert {(m["status"], m["aa_sub_status"], m["bk_hold_id"]) for m in made} == {("planning", "held", s["hold_id"])}
    assert all(m["aa_price_hash"] == fake.holds[s["hold_id"]]["price_hash"] for m in made)
    # Net from Booking (the held quote's line price, 300), sell = the agency's option sell.
    for m in made:
        async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"]) as ctx:
            view = await lines.line(ctx, m["booking_line_id"])
        assert view["money"]["cost"]["amount"] == "300.00" and view["money"]["cost"]["fx"]["source"] == "aa_quote_lock"
        assert Decimal(view["money"]["sell"]["amount"]) == chosen[m["booking_line_id"]]
        assert view["aa"]["sub_status"] == "held" and view["aa"]["hold_id"] == s["hold_id"]
        # The assistant sees the line, never its money or margin.
        async with tenant_tx(app_pool, s["who"]["assistant"], s["tenant"]) as ctx:
            seen = await lines.line(ctx, m["booking_line_id"])
            trip = await lines.trip_lines(ctx, s["trip"])
        assert "money" not in seen and seen["aa"]["sub_status"] == "held"
        assert "margin" not in str(trip) and "'cost'" not in str(trip)
    assert await _commitment(app_pool, s) == ["held"]
    # A second run writes nothing again (once per proposal).
    assert (await _sync(app_pool, s))["proposal_lines_held"] == 0 and len(await _aa_lines(app_pool, s)) == 2

    paid = await _pay_deposit(app_pool, s)
    assert paid["trip_moved"]["stage"] == "booked"
    counts = await _sync(app_pool, s)
    assert counts["confirms_queued"] == 1 and len(fake.confirm_keys) == 1
    booked = await _aa_lines(app_pool, s)
    assert {(b["status"], b["bk_booking_id"]) for b in booked} == {("confirmed", f"b-{s['hold_id']}")}
    assert await _commitment(app_pool, s) == ["confirmed"]
    audit = await _raw(app_pool, s, "SELECT reason FROM crm.audit_event WHERE action IN ('aa_sync', 'aa_request', "
                       "'aa_lines_from_proposal') ORDER BY at")  # fmt: skip
    reasons = [a["reason"] for a in audit]
    assert "held" in reasons and "confirm_deposit" in reasons and reasons.count("confirm_confirmed") == 2
    assert await _stage(app_pool, s) == "booked"
    # Later runs send nothing more: one confirm for the set, ever.
    await _sync(app_pool, s)
    assert len(fake.confirm_keys) == 1 and len(fake.bookings) == 1


async def test_a_lost_confirm_answer_is_retried_with_the_same_key(app_pool: asyncpg.Pool) -> None:
    s = await _accepted(app_pool)
    fake = s["fake"]
    fake.confirm_line_status = "confirmed"
    await _sync(app_pool, s)
    await _pay_deposit(app_pool, s)
    fake.lose_next_answer = True  # Booking books, the answer is lost
    await _sync(app_pool, s)
    assert len(fake.bookings) == 1 and {b["aa_sub_status"] for b in await _aa_lines(app_pool, s)} == {"held"}
    pending = await _raw(app_pool, s, "SELECT kind, last_error_class FROM crm.aa_request WHERE status = 'pending'")
    assert [(p["kind"], p["last_error_class"]) for p in pending] == [("confirm", "booking_timeout")]
    await _due_now(app_pool, s)
    await _sync(app_pool, s)
    assert len(fake.confirm_keys) == 2 and fake.confirm_keys[0] == fake.confirm_keys[1]
    assert len(fake.bookings) == 1 and {b["status"] for b in await _aa_lines(app_pool, s)} == {"confirmed"}
    assert len(await _raw(app_pool, s, "SELECT 1 FROM crm.aa_request WHERE kind = 'confirm'")) == 1


async def test_a_hold_that_expired_before_the_deposit_is_never_confirmed(app_pool: asyncpg.Pool) -> None:
    s = await _accepted(app_pool)
    fake = s["fake"]
    past = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)).replace(microsecond=0).isoformat()
    fake.holds[s["hold_id"]]["expires_at"] = past
    counts = await _sync(app_pool, s)
    assert counts["proposal_lines_expired"] == 1
    made = await _aa_lines(app_pool, s)
    assert {(m["aa_sub_status"], m["aa_result_code"]) for m in made} == {("expired", "hold_expired")}
    assert "aa_hold_expired" in await _tasks(app_pool, s)
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        view = await lines.line(ctx, made[0]["booking_line_id"])
    assert "requote" in view["aa"]["actions"]  # the agency's way on: a new quote
    await _pay_deposit(app_pool, s)
    counts = await _sync(app_pool, s)
    assert counts["confirms_queued"] == 0 and fake.confirm_keys == []
    assert await _raw(app_pool, s, "SELECT 1 FROM crm.aa_request WHERE kind = 'confirm'") == []
    assert await _stage(app_pool, s) == "booked"


async def test_a_refused_confirm_fails_every_line_and_keeps_the_stage(app_pool: asyncpg.Pool) -> None:
    s = await _accepted(app_pool)
    fake = s["fake"]
    await _sync(app_pool, s)
    await _pay_deposit(app_pool, s)
    fake.confirm_refusal = "invalid"
    await _sync(app_pool, s)
    failed = await _aa_lines(app_pool, s)
    assert {(f["status"], f["aa_sub_status"], f["aa_result_code"]) for f in failed} == {
        ("planning", "failed", "invalid")
    }
    assert await _stage(app_pool, s) == "booked"
    assert "aa_confirm_failed" in await _tasks(app_pool, s)
    done = await _raw(app_pool, s, "SELECT status, result_code FROM crm.aa_request WHERE kind = 'confirm'")
    assert [(d["status"], d["result_code"]) for d in done] == [("failed", "invalid")]
    # Not sent again by itself: the agent decides (a new quote).
    fake.confirm_refusal = None
    await _sync(app_pool, s)
    assert len(fake.confirm_keys) == 1 and fake.bookings == {}


async def test_booking_on_contract_1_1_writes_nothing_until_it_answers_with_the_quote(app_pool: asyncpg.Pool) -> None:
    s = await _accepted(app_pool)
    fake = s["fake"]
    hold = fake.holds[s["hold_id"]]
    saved = {k: hold.pop(k) for k in ("price_hash", "currency", "fx")}
    counts = await _sync(app_pool, s)
    assert counts["proposal_lines_retry"] == 1 and await _aa_lines(app_pool, s) == []
    hold.update(saved)
    assert (await _sync(app_pool, s))["proposal_lines_held"] == 1


async def test_a_hold_booking_does_not_know_gives_failed_lines_and_a_task(app_pool: asyncpg.Pool) -> None:
    s = await _accepted(app_pool)
    s["fake"].holds.pop(s["hold_id"])
    assert (await _sync(app_pool, s))["proposal_lines_failed"] == 1
    made = await _aa_lines(app_pool, s)
    assert len(made) == 2 and {(m["aa_sub_status"], m["aa_result_code"]) for m in made} == {
        ("failed", "hold_not_found")
    }
    assert "aa_lines_failed" in await _tasks(app_pool, s)
    assert all(uuid.UUID(str(m["booking_line_id"])) for m in made)


async def test_lost_before_the_deposit_releases_the_whole_set(app_pool: asyncpg.Pool) -> None:
    """A release acts on the whole Booking hold: every line of the set reads released, nothing stays held."""
    s = await _accepted(app_pool)
    await _sync(app_pool, s)
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        await move(ctx, s["trip"], "lost", lost_reason="went_elsewhere")
    await _due_now(app_pool, s)
    await _sync(app_pool, s)
    released = await _aa_lines(app_pool, s)
    assert {(r["status"], r["aa_sub_status"]) for r in released} == {("cancelled", "released")}
    assert s["fake"].holds[s["hold_id"]]["status"] == "released"
    assert await _raw(app_pool, s, "SELECT 1 FROM crm.aa_request WHERE status = 'pending'") == []
    assert await _commitment(app_pool, s) == ["released"]
