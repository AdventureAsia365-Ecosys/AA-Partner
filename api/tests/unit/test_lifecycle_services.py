"""AA-946 services with a scripted connection: the refusal codes and the Python branches of proposals, agreements, the
schedule, portal access, the portal's code / session / Stage 3 / sign steps, the Stage 3 progress and the date ticks.
The SQL, RLS, triggers and encryption run on PostgreSQL in tests/integration/test_crm016_schema.py and
test_lifecycle_api.py."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from typing import Any

import pytest

from app import stages, stages_job
from app.errors import Conflict, Forbidden, Invalid, NotFound, RateLimited, ServiceError, Unavailable
from app.portal import access, progress
from app.portal import public as portal
from app.portal.codes import CODE_MASK
from app.proposals import agreements, service
from app.proposals import public as proposal_page
from app.settings import Settings
from app.tenancy import ClientLink
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import TENANT, FakeVault, enc, fake_tx, system_ctx, tenant_ctx

TRIP = uuid.UUID("00000000-0000-4000-8000-0000000007a1")
LEAD = uuid.UUID("00000000-0000-4000-8000-0000000007b1")
OTHER = uuid.UUID("00000000-0000-4000-8000-0000000007b2")
PROPOSAL = uuid.UUID("00000000-0000-4000-8000-0000000007c1")
SCHEDULE = uuid.UUID("00000000-0000-4000-8000-0000000007d1")
ACCESS = uuid.UUID("00000000-0000-4000-8000-0000000007e1")
LINK = uuid.UUID("00000000-0000-4000-8000-0000000007f1")
DOC = uuid.UUID("00000000-0000-4000-8000-000000000701")
OTP = uuid.UUID("00000000-0000-4000-8000-000000000702")
NOW = dt.datetime(2026, 10, 25, 3, 0, tzinfo=dt.UTC)
TODAY = dt.date(2026, 10, 25)
ECHO = Settings(environment="test", db_ssl="disable", portal_otp_dev_echo=True)
PLAIN = Settings(environment="test", db_ssl="disable")
TOKEN = "t" * 43


@contextmanager
def _raises(cls: type[ServiceError], code: str) -> Iterator[ServiceError]:
    with pytest.raises(cls) as err:
        yield err  # type: ignore[misc]
    assert err.value.code == code, err.value.code


# ── proposals ────────────────────────────────────────────────────────────────────────────────────


def _proposal_row(**over: Any) -> dict[str, Any]:
    row = {
        "proposal_id": PROPOSAL,
        "trip_id": TRIP,
        "version": 1,
        "status": "draft",
        "body_enc": enc("proposal", "body_enc", "Day 1"),
        "currency": "USD",
        "total_amount": Decimal("1000"),
        "deposit_amount": Decimal("300"),
        "balance_due_on": TODAY + dt.timedelta(days=60),
        "terms_doc_id": None,
        "price_hash": None,
        "link_id": None,
        "stage": "proposal",
        "owner_member_id": None,
        "sent_at": None,
        "accepted_at": None,
    }
    row.update(over)
    return row


def test_price_hash_is_stable_and_names_everything_the_client_sees() -> None:
    base = dict(
        proposal_id=PROPOSAL,
        version=1,
        currency="USD",
        total=Decimal("1000"),
        deposit=Decimal("300.0"),
        balance_due_on=TODAY,
        body="Day 1",
        terms_doc_id=DOC,
        terms_sha256=b"\x01" * 32,
    )
    one = service.price_hash(**base)
    assert one == service.price_hash(**{**base, "deposit": Decimal("300.00")}) and len(one) == 32
    for change in ({"total": Decimal("1001")}, {"body": "Day 2"}, {"terms_sha256": b"\x02" * 32}, {"version": 2}):
        assert service.price_hash(**{**base, **change}) != one


@pytest.mark.parametrize(
    ("args", "code"),
    [
        (("usd", Decimal(1), Decimal(1), None), "invalid_currency"),
        (("USD", Decimal(0), Decimal(1), None), "invalid_amount"),
        (("USD", Decimal(1), Decimal(2), None), "deposit_above_total"),
        (("USD", Decimal(2), Decimal(1), None), "balance_due_on_required"),
        (("USD", Decimal(1), Decimal(1), TODAY), "balance_due_on_not_allowed"),
    ],
)
def test_amount_rules(args: tuple, code: str) -> None:
    with _raises(Invalid, code):
        service._check_amounts(*args)


def test_body_rules() -> None:
    assert service._body(" Day 1\r\nDay 2 ") == "Day 1\nDay 2"
    with _raises(Invalid, "body_required"):
        service._body("  ")
    with _raises(Invalid, "body_too_long"):
        service._body("x" * (service.MAX_BODY + 1))


@pytest.mark.asyncio
async def test_create_draft_refusals_and_success() -> None:
    kwargs = dict(body="Day 1", currency="USD", total=Decimal(1000), deposit=Decimal(1000), balance_due_on=None)
    with _raises(NotFound, "trip_not_found"):
        await service.create_draft(tenant_ctx(), FakeVault(), TRIP, terms_doc_id=None, **kwargs)
    booked = ScriptedConn().on("FROM crm.trip WHERE trip_id", {"trip_id": TRIP, "stage": "booked"})
    with _raises(Conflict, "trip_not_open_for_proposal"):
        await service.create_draft(tenant_ctx(booked), FakeVault(), TRIP, terms_doc_id=None, **kwargs)
    taken = (
        ScriptedConn()
        .on("FROM crm.trip WHERE trip_id", {"trip_id": TRIP, "stage": "proposal"})
        .on("status = 'accepted'", True)
    )
    with _raises(Conflict, "proposal_already_accepted"):
        await service.create_draft(tenant_ctx(taken), FakeVault(), TRIP, terms_doc_id=None, **kwargs)
    conn = (
        ScriptedConn()
        .on("FROM crm.trip WHERE trip_id", {"trip_id": TRIP, "stage": "qualifying"})
        .on("status = 'accepted'", False)
        .on("max(version)", 2)
        .on("FROM crm.agreement_doc", None)
    )
    with _raises(NotFound, "terms_not_found"):
        await service.create_draft(tenant_ctx(conn), FakeVault(), TRIP, terms_doc_id=DOC, **kwargs)
    ctx = tenant_ctx(conn)
    out = await service.create_draft(ctx, FakeVault(), TRIP, terms_doc_id=None, **kwargs)
    assert out["version"] == 2 and out["status"] == "draft"
    inserted = conn.sql("INSERT INTO crm.proposal")[0]
    assert inserted[3] == enc("proposal", "body_enc", "Day 1") and inserted[2] == 2
    assert [e.action for e in ctx.audit_events] == ["proposal_draft"]


@pytest.mark.asyncio
async def test_update_draft_only_while_draft() -> None:
    with _raises(NotFound, "proposal_not_found"):
        await service.update_draft(tenant_ctx(), FakeVault(), PROPOSAL, {})
    sent = ScriptedConn().on("FROM crm.proposal p JOIN crm.trip", _proposal_row(status="sent"))
    with _raises(Conflict, "proposal_not_draft"):
        await service.update_draft(tenant_ctx(sent), FakeVault(), PROPOSAL, {"body": "x"})
    conn = ScriptedConn().on("FROM crm.proposal p JOIN crm.trip", _proposal_row())
    out = await service.update_draft(tenant_ctx(conn), FakeVault(), PROPOSAL, {"deposit_amount": Decimal(1000),
                                                                             "balance_due_on": None})  # fmt: skip
    assert out["status"] == "draft"
    args = conn.sql("UPDATE crm.proposal SET body_enc")[0]
    assert args[1] == enc("proposal", "body_enc", "Day 1") and args[4] == Decimal(1000) and args[5] is None


@pytest.mark.asyncio
async def test_send_refusals() -> None:
    with _raises(NotFound, "proposal_not_found"):
        await service.send(tenant_ctx(), FakeVault(), PROPOSAL)
    for row, cls, code in (
        (_proposal_row(status="sent"), Conflict, "proposal_not_draft"),
        (_proposal_row(stage="qualifying"), Conflict, "trip_not_in_proposal"),
        (_proposal_row(), Conflict, "no_lead_traveller"),
    ):
        conn = ScriptedConn().on("FROM crm.proposal p JOIN crm.trip", row)
        with _raises(cls, code):
            await service.send(tenant_ctx(conn), FakeVault(), PROPOSAL)
    draft_terms = (
        ScriptedConn()
        .on("FROM crm.proposal p JOIN crm.trip", _proposal_row(terms_doc_id=DOC))
        .on("role = 'lead'", LEAD)
        .on("FROM crm.agreement_doc", {"doc_id": DOC, "status": "draft", "body_sha256": b"\x00" * 32})
    )
    with _raises(Conflict, "terms_not_published"):
        await service.send(tenant_ctx(draft_terms), FakeVault(), PROPOSAL)


@pytest.mark.asyncio
async def test_send_supersedes_and_returns_the_link_once() -> None:
    previous = uuid.uuid4()
    conn = (
        ScriptedConn()
        .on("FROM crm.proposal p JOIN crm.trip", _proposal_row(terms_doc_id=DOC))
        .on("role = 'lead'", LEAD)
        .on("FROM crm.agreement_doc", {"doc_id": DOC, "status": "published", "body_sha256": b"\x01" * 32})
        .on("status = 'sent' AND deleted_at IS NULL FOR UPDATE", {"proposal_id": previous, "link_id": LINK})
        .on("INSERT INTO crm.client_link", NOW + dt.timedelta(days=14))
        .on("SELECT archived_at, deleted_at FROM crm.traveler", {"archived_at": None, "deleted_at": None})
        .on("INSERT INTO crm.dispatch_queue", {"dispatch_id": uuid.uuid4(), "not_before": NOW})
        .on("SET status = 'sent', price_hash", NOW)
    )
    out = await service.send(tenant_ctx(conn), FakeVault(), PROPOSAL)
    assert out["link_path"].startswith("/c/p/") and len(out["link_path"]) == len("/c/p/") + 43
    assert out["superseded"] == str(previous) and len(out["price_hash"]) == 64
    assert conn.sql("SET status = 'superseded'") == [(previous,)]
    assert conn.sql("SET status = 'revoked'") == [(LINK,)]


@pytest.mark.asyncio
async def test_withdraw() -> None:
    with _raises(NotFound, "proposal_not_found"):
        await service.withdraw(tenant_ctx(), PROPOSAL)
    accepted = ScriptedConn().on("FROM crm.proposal p JOIN crm.trip", _proposal_row(status="accepted"))
    with _raises(Conflict, "proposal_not_open"):
        await service.withdraw(tenant_ctx(accepted), PROPOSAL)
    conn = ScriptedConn().on("FROM crm.proposal p JOIN crm.trip", _proposal_row(status="sent", link_id=LINK))
    assert (await service.withdraw(tenant_ctx(conn), PROPOSAL))["status"] == "withdrawn"
    assert conn.sql("SET status = 'revoked'") == [(LINK,)]


def _schedule_row(**over: Any) -> dict[str, Any]:
    row = {
        "schedule_id": SCHEDULE,
        "trip_id": TRIP,
        "proposal_id": PROPOSAL,
        "kind": "deposit",
        "amount": Decimal("300"),
        "currency": "USD",
        "status": "due",
        "proposal_status": "accepted",
        "stage": "booked",
    }
    row.update(over)
    return row


@pytest.mark.asyncio
async def test_mark_paid_refusals() -> None:
    with _raises(Invalid, "invalid_method"):
        await service.mark_paid(tenant_ctx(), FakeVault(), SCHEDULE, received_on=None, method="crypto")
    with _raises(NotFound, "schedule_not_found"):
        await service.mark_paid(tenant_ctx(), FakeVault(), SCHEDULE, received_on=None, method="cash")
    for row, code in ((_schedule_row(status="paid"), "schedule_not_due"),
                      (_schedule_row(proposal_status="superseded"), "proposal_not_accepted")):  # fmt: skip
        conn = ScriptedConn().on("FROM crm.payment_schedule s", row)
        with _raises(Conflict, code):
            await service.mark_paid(tenant_ctx(conn), FakeVault(), SCHEDULE, received_on=None, method="cash")
    future = ScriptedConn().on("FROM crm.payment_schedule s", _schedule_row()).on("AT TIME ZONE", TODAY)
    with _raises(Invalid, "received_in_future"):
        await service.mark_paid(
            tenant_ctx(future), FakeVault(), SCHEDULE, received_on=TODAY + dt.timedelta(days=1), method="cash"
        )


@pytest.mark.asyncio
async def test_mark_paid_balance_and_a_booked_deposit(monkeypatch: pytest.MonkeyPatch) -> None:
    invited: list[Any] = []

    async def invite_all(_ctx: Any, trip_id: uuid.UUID) -> dict[str, Any]:
        invited.append(trip_id)
        return {"invited": [], "skipped": []}

    monkeypatch.setattr(access, "invite_all", invite_all)
    conn = ScriptedConn().on("FROM crm.payment_schedule s", _schedule_row(kind="balance")).on("AT TIME ZONE", TODAY)
    out = await service.mark_paid(tenant_ctx(conn), FakeVault(), SCHEDULE, received_on=None, method="cash")
    assert out["trip_moved"] is None and out["portal"] is None and invited == []
    payment = conn.sql("INSERT INTO crm.payment")[0]
    assert payment[3:] == (Decimal("300"), "USD", "cash", TODAY)
    assert conn.sql("INSERT INTO crm.trip_commitment")[0][1].startswith("payment:")
    # A deposit on a trip already booked (an override): no move, the travellers are invited.
    conn = ScriptedConn().on("FROM crm.payment_schedule s", _schedule_row()).on("AT TIME ZONE", TODAY)
    out = await service.mark_paid(tenant_ctx(conn), FakeVault(), SCHEDULE, received_on=TODAY, method="bank_transfer")
    assert out["trip_moved"] is None and invited == [TRIP]
    assert conn.sql("kind = $2 AND status = 'open'")  # the collect_deposit task closed


@pytest.mark.asyncio
async def test_mark_paid_moves_a_proposal_trip_to_booked(monkeypatch: pytest.MonkeyPatch) -> None:
    moves: list[tuple] = []

    async def move(_ctx: Any, trip_id: uuid.UUID, to: str, **kw: Any) -> dict[str, Any]:
        moves.append((trip_id, to, kw["reason"]))
        return {"from_stage": "proposal", "stage": to, "waiting_on": "client"}

    async def invite_all(_ctx: Any, _trip: uuid.UUID) -> dict[str, Any]:
        return {"invited": [{"traveler_id": str(LEAD)}], "skipped": []}

    monkeypatch.setattr(stages, "move", move)
    monkeypatch.setattr(access, "invite_all", invite_all)
    conn = ScriptedConn().on("FROM crm.payment_schedule s", _schedule_row(stage="proposal")).on("AT TIME ZONE", TODAY)
    out = await service.mark_paid(tenant_ctx(conn), FakeVault(), SCHEDULE, received_on=None, method="other")
    assert moves == [(TRIP, "booked", "deposit_paid")]
    assert out["trip_moved"] == {"from_stage": "proposal", "stage": "booked", "waiting_on": "client"}


@pytest.mark.asyncio
async def test_void_only_a_due_row() -> None:
    with _raises(NotFound, "schedule_not_found"):
        await service.void(tenant_ctx(), SCHEDULE)
    paid = ScriptedConn().on("FROM crm.payment_schedule s", _schedule_row(status="paid"))
    with _raises(Conflict, "schedule_not_due"):
        await service.void(tenant_ctx(paid), SCHEDULE)
    # A deposit row is never void (review S251: the accepted proposal keeps its way to Booked and to the portal).
    deposit = ScriptedConn().on("FROM crm.payment_schedule s", _schedule_row())
    with _raises(Conflict, "deposit_not_voidable"):
        await service.void(tenant_ctx(deposit), SCHEDULE)
    assert not any("SET status = 'void'" in sql for sql, _ in deposit.calls)
    conn = ScriptedConn().on("FROM crm.payment_schedule s", _schedule_row(kind="balance"))
    assert (await service.void(tenant_ctx(conn), SCHEDULE))["status"] == "void"


@pytest.mark.asyncio
async def test_void_a_row_its_invoice_bills() -> None:
    """AA-952 slice 3: one row, one path. An issued invoice keeps the row (void the invoice first); a draft one is
    voided with the row. The invoice rows are locked before the schedule row (the order of issue / record payment)."""
    draft_id, issued_id = uuid.uuid4(), uuid.uuid4()
    for status in ("open", "payment_claimed", "expired"):
        conn = ScriptedConn().on("FROM crm.payment_schedule s", _schedule_row(kind="balance"))
        conn.on("FROM crm.invoice WHERE schedule_id", [{"invoice_id": issued_id, "status": status}])
        with _raises(Conflict, "invoice_issued_for_row"):
            await service.void(tenant_ctx(conn), SCHEDULE)
        assert not conn.sql("UPDATE crm.payment_schedule SET status = 'void'")
    conn = ScriptedConn().on("FROM crm.payment_schedule s", _schedule_row(kind="balance"))
    conn.on("FROM crm.invoice WHERE schedule_id", [{"invoice_id": draft_id, "status": "draft"}])
    out = await service.void(tenant_ctx(conn), SCHEDULE)
    assert out == {"schedule_id": str(SCHEDULE), "status": "void", "invoices_voided": [str(draft_id)]}
    voided = conn.sql("UPDATE crm.invoice SET status = 'void'")
    assert voided == [(draft_id, f"member:{tenant_ctx().member_id}", service.DRAFT_VOID_REASON)]
    locks = [sql for sql, _ in conn.calls if "FOR UPDATE" in sql]
    assert "crm.invoice" in locks[0] and "crm.payment_schedule" in locks[1] and "crm.invoice" in locks[2]


@pytest.mark.asyncio
async def test_trip_overview() -> None:
    with _raises(NotFound, "trip_not_found"):
        await service.trip_overview(tenant_ctx(), FakeVault(), TRIP)
    sent = _proposal_row(status="sent", price_hash=b"\x01" * 32, sent_at=NOW, terms_doc_id=DOC)
    schedule = {**_schedule_row(), "due_on": TODAY, "paid_at": None}
    payment = {"payment_id": uuid.uuid4(), "schedule_id": SCHEDULE, "invoice_id": None, "amount": Decimal(1),
               "currency": "USD", "method": "cash", "received_on": TODAY}  # fmt: skip
    # AA-952 slice 3: a service-fee invoice's payment has no schedule row.
    fee = {**payment, "payment_id": uuid.uuid4(), "schedule_id": None, "invoice_id": uuid.uuid4()}
    conn = (
        ScriptedConn()
        .on("SELECT trip_id, stage FROM crm.trip", {"trip_id": TRIP, "stage": "proposal"})
        .on("FROM crm.proposal WHERE trip_id", [sent])
        .on("FROM crm.payment_schedule WHERE trip_id", [schedule])
        .on("FROM crm.payment WHERE trip_id", [payment, fee])
    )
    ctx = tenant_ctx(conn)
    out = await service.trip_overview(ctx, FakeVault(), TRIP)
    assert out["payments"][1]["schedule_id"] is None and out["payments"][1]["invoice_id"] == str(fee["invoice_id"])
    assert out["proposals"][0]["body"] == "Day 1" and out["proposals"][0]["price_hash"] == "01" * 32
    assert out["schedule"][0]["amount"] == "300.00" and out["payments"][0]["method"] == "cash"
    assert out["portal"] == {"s3": 0, "waiver_required": False, "travellers": []} and not out["deposit_paid"]


# ── agreements ───────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_agreements() -> None:
    with _raises(Invalid, "invalid_kind"):
        await agreements.create(tenant_ctx(), kind="contract", version_label="v1", body_text="x")
    with _raises(Invalid, "invalid_version_label"):
        await agreements.create(tenant_ctx(), kind="waiver", version_label=" ", body_text="x")
    with _raises(Invalid, "body_required"):
        await agreements.create(tenant_ctx(), kind="waiver", version_label="v1", body_text="")
    with _raises(Invalid, "body_too_long"):
        await agreements.create(tenant_ctx(), kind="waiver", version_label="v1", body_text="x" * 100_001)
    import asyncpg

    taken = ScriptedConn().on("INSERT INTO crm.agreement_doc", asyncpg.UniqueViolationError("dup"))
    with _raises(Conflict, "version_label_taken"):
        await agreements.create(tenant_ctx(taken), kind="waiver", version_label="v1", body_text="x")
    assert (await agreements.create(tenant_ctx(), kind="waiver", version_label="v1", body_text="x"))[
        "status"
    ] == "draft"
    for fn in (agreements.publish,):
        with _raises(NotFound, "agreement_not_found"):
            await fn(tenant_ctx(), DOC)
    with _raises(NotFound, "agreement_not_found"):
        await agreements.update(tenant_ctx(), DOC, {})
    published = ScriptedConn().on("FROM crm.agreement_doc WHERE doc_id", {"doc_id": DOC, "kind": "waiver",
                                                                            "status": "published"})  # fmt: skip
    with _raises(Conflict, "agreement_not_draft"):
        await agreements.update(tenant_ctx(published), DOC, {"body_text": "y"})
    with _raises(Conflict, "agreement_not_draft"):
        await agreements.publish(tenant_ctx(published), DOC)
    draft = ScriptedConn().on("FROM crm.agreement_doc WHERE doc_id", {"doc_id": DOC, "kind": "waiver",
                                                                        "status": "draft"})  # fmt: skip
    assert (await agreements.update(tenant_ctx(draft), DOC, {"version_label": "v2"}))["status"] == "draft"
    clash = (
        ScriptedConn()
        .on("FROM crm.agreement_doc WHERE doc_id", {"doc_id": DOC, "kind": "waiver", "status": "draft"})
        .on("UPDATE crm.agreement_doc SET version_label", asyncpg.UniqueViolationError("dup"))
    )
    with _raises(Conflict, "version_label_taken"):
        await agreements.update(tenant_ctx(clash), DOC, {"version_label": "v1"})
    assert (await agreements.publish(tenant_ctx(draft), DOC))["status"] == "published"
    assert draft.sql("SET status = 'retired'") == [("waiver",)]
    listed = ScriptedConn().on(
        "ORDER BY kind",
        [{"doc_id": DOC, "kind": "waiver", "version_label": "v1", "status": "published", "body_text": "x",
          "body_sha256": b"\x01" * 32, "published_at": NOW, "updated_at": NOW}],
    )  # fmt: skip
    docs = (await agreements.list_docs(tenant_ctx(listed)))["agreements"]
    assert docs[0]["body_sha256"] == "01" * 32 and docs[0]["published_at"] == NOW.isoformat()


# ── progress ─────────────────────────────────────────────────────────────────────────────────────


def _party(traveler: uuid.UUID, role: str, have: list[bool], signed: bool, access_id: Any = None) -> dict[str, Any]:
    return {
        "traveler_id": traveler,
        "role": role,
        "details_done": all(have),
        "details_have": have,
        "access_id": access_id,
        "access_status": "active" if access_id else None,
        "last_sign_in_at": NOW if access_id else None,
        "stage3_submitted_at": None,
        "waiver_signed": signed,
    }


@pytest.mark.asyncio
async def test_progress_counts_details_and_the_waiver() -> None:
    full, part = [True] * 8, [True] * 7 + [False]
    conn = (
        ScriptedConn()
        .on("kind = 'waiver' AND status = 'published'", {"doc_id": DOC})
        .on(
            "FROM crm.trip_traveler tt",
            [_party(LEAD, "lead", full, True, ACCESS), _party(OTHER, "companion", part, False)],
        )
    )
    done = await progress.refresh(system_ctx(conn), TRIP)
    assert done.s3 == 50 and done.waiver_doc_id == DOC
    view = done.view()
    assert view["travellers"][1]["missing"] == ["emergency_contact"] and view["travellers"][1]["portal"] is None
    assert view["travellers"][0]["portal"]["status"] == "active"
    assert conn.sql("jsonb_set") == [(TRIP, 50)]
    no_waiver = ScriptedConn().on("FROM crm.trip_traveler tt", [_party(LEAD, "lead", full, False)])
    assert (await progress.compute(system_ctx(no_waiver), TRIP)).s3 == 100
    assert (await progress.compute(system_ctx(ScriptedConn()), TRIP)).s3 == 0


@pytest.mark.asyncio
async def test_the_stage3_hook() -> None:
    facts = stages.TripFacts(trip_id=TRIP, stage="booked", stage2_link_open=False)
    assert await progress._stage3_hook(system_ctx(ScriptedConn().on("< 100", True)), facts) == "client"
    assert await progress._stage3_hook(system_ctx(ScriptedConn().on("< 100", False)), facts) is None
    later = stages.TripFacts(trip_id=TRIP, stage="pre_departure", stage2_link_open=False)
    assert await progress._stage3_hook(system_ctx(ScriptedConn().on("< 100", True)), later) is None


# ── portal access ────────────────────────────────────────────────────────────────────────────────


def _invite_conn(current: Any = None, stage: str = "booked", role: Any = "lead") -> ScriptedConn:
    return (
        ScriptedConn()
        .on("SELECT stage FROM crm.trip", stage)
        .on("SELECT role FROM crm.trip_traveler", role)
        .on("FROM crm.traveler_access WHERE trip_id = $1 AND traveler_id = $2", current)
        .on("INSERT INTO crm.client_link", NOW + dt.timedelta(days=30))
        .on("SELECT archived_at, deleted_at FROM crm.traveler", {"archived_at": None, "deleted_at": None})
        .on("INSERT INTO crm.dispatch_queue", {"dispatch_id": uuid.uuid4(), "not_before": NOW})
    )


@pytest.mark.asyncio
async def test_invite_refusals_and_reissue() -> None:
    with _raises(NotFound, "trip_not_found"):
        await access.invite(tenant_ctx(), TRIP, LEAD)
    with _raises(Conflict, "trip_closed"):
        await access.invite(tenant_ctx(_invite_conn(stage="cancelled")), TRIP, LEAD)
    with _raises(NotFound, "traveler_not_on_trip"):
        await access.invite(tenant_ctx(_invite_conn(role=None)), TRIP, LEAD)
    conn = _invite_conn()
    first = await access.invite(tenant_ctx(conn), TRIP, LEAD)
    assert first["role"] == "lead" and first["reissued_from"] is None and first["link_path"].startswith("/c/t/")
    assert conn.sql("INSERT INTO crm.traveler_access") and conn.sql("INSERT INTO crm.signature_request")
    again = _invite_conn(current={"access_id": ACCESS, "link_id": LINK, "status": "active"})
    second = await access.invite(system_ctx(again), TRIP, LEAD)  # a job / a payment: origin system
    assert second["reissued_from"] == str(LINK) and second["access_id"] == str(ACCESS)
    assert again.sql("SET link_id = $2") and again.sql("INSERT INTO crm.dispatch_queue")[0][4] == "system"


@pytest.mark.asyncio
async def test_invite_all_skips_who_cannot_be_invited() -> None:
    conn = _invite_conn().on(
        "ORDER BY tt.role = 'lead' DESC, tt.created_at, tt.traveler_id\n", None
    )  # the progress query answers nothing
    conn.on("JOIN crm.traveler t ON t.traveler_id = tt.traveler_id AND t.deleted_at IS NULL WHERE tt.trip_id = $1 "
            "AND tt.deleted_at IS NULL ORDER BY", [{"traveler_id": LEAD}, {"traveler_id": OTHER}])  # fmt: skip
    conn.on("SELECT archived_at, deleted_at FROM crm.traveler",
            lambda tid: {"archived_at": NOW if tid == OTHER else None, "deleted_at": None})  # fmt: skip
    out = await access.invite_all(tenant_ctx(conn), TRIP)
    assert [i["traveler_id"] for i in out["invited"]] == [str(LEAD)]
    assert out["skipped"] == [{"traveler_id": str(OTHER), "code": "traveler_archived"}]


@pytest.mark.asyncio
async def test_revoke_and_trip_of() -> None:
    with _raises(NotFound, "access_not_found"):
        await access.revoke(tenant_ctx(), ACCESS)
    with _raises(NotFound, "access_not_found"):
        await access.trip_of(tenant_ctx(), ACCESS)
    gone = ScriptedConn().on("WHERE access_id = $1 AND deleted_at IS NULL FOR UPDATE", {"status": "revoked"})
    with _raises(Conflict, "already_revoked"):
        await access.revoke(tenant_ctx(gone), ACCESS)
    row = {"access_id": ACCESS, "trip_id": TRIP, "traveler_id": LEAD, "link_id": LINK, "status": "active"}
    conn = ScriptedConn().on("WHERE access_id = $1 AND deleted_at IS NULL FOR UPDATE", row)
    assert (await access.revoke(tenant_ctx(conn), ACCESS))["status"] == "revoked"
    assert conn.sql("SET status = 'revoked', revoked_at") == [(LINK,)]
    assert await access.trip_of(tenant_ctx(ScriptedConn().on("SELECT trip_id FROM crm.traveler_access", TRIP)),
                                ACCESS) == TRIP  # fmt: skip


# ── the portal (traveller side) ─────────────────────────────────────────────────────────────────


def _access_row(**over: Any) -> dict[str, Any]:
    row = {
        "access_id": ACCESS,
        "trip_id": TRIP,
        "traveler_id": LEAD,
        "role": "lead",
        "status": "active",
        "stage3_submitted_at": None,
        "stage": "booked",
        "title": "Laos",
        "start_date": TODAY,
        "end_date": None,
        "owner_member_id": None,
    }
    row.update(over)
    return row


# AA-953: the party as app.portal.person reads it (who reaches whom): the lead alone, with an address and access.
PARTY_ROWS = [
    {"traveler_id": LEAD, "role": "lead", "has_email": True, "display_name_enc": None, "access_id": ACCESS,
     "access_status": "active"},
]  # fmt: skip


def _portal(monkeypatch: pytest.MonkeyPatch, conn: ScriptedConn, module: Any = portal, purpose: str = "portal") -> Any:
    if purpose == "portal" and not any(f == "AS has_email" for f, _ in conn.rules):
        conn.on("AS has_email", PARTY_ROWS)
    link = ClientLink(link_id=LINK, tenant_id=TENANT, trip_id=TRIP, traveler_id=LEAD, purpose=purpose, status="opened")
    ctx = system_ctx(conn)
    monkeypatch.setattr(module, "client_link_tx", fake_tx(ctx, link))
    monkeypatch.setattr(module, "Vault", lambda keys, ctx: FakeVault())
    return ctx


# AA-953: the session row names the access it acts as (the link's own here).
SESSION_ROW = {"otp_id": OTP, "access_id": ACCESS, "traveler_id": LEAD}


def _signed_in(conn: ScriptedConn) -> ScriptedConn:
    return conn.on("FROM crm.traveler_access a JOIN crm.trip t", _access_row()).on("AND session_hash = $2", SESSION_ROW)


def test_small_helpers() -> None:
    assert portal.mask_email("grace@example.test") == "g***@e******.test"
    assert portal.mask_email("a@localhost") == "a***@l********"
    assert portal.code_hash(OTP, "123456") == hashlib.sha256(OTP.bytes + b"123456").digest()
    assert portal.evidence_record(b=1, a="x") == b'{"a":"x","b":1}'


def test_origin_rules() -> None:
    strict = Settings(environment="test", db_ssl="disable", intake_page_origins=("https://go.test",))
    portal.check_origin(strict, "https://go.test")
    with _raises(Forbidden, "origin_not_allowed"):
        portal.check_origin(strict, "https://evil.test")
    with _raises(Unavailable, "intake_not_configured"):
        portal.check_origin(Settings(environment="dev", db_ssl="require"), "https://go.test")


@pytest.mark.asyncio
async def test_request_code_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    with _raises(Invalid, "invalid_purpose"):
        await portal.request_code(object(), object(), ECHO, TOKEN, purpose="x", session=None, origin=None)  # type: ignore[arg-type]
    _portal(monkeypatch, ScriptedConn())
    with _raises(NotFound, "link_not_found"):
        await portal.request_code(object(), object(), ECHO, TOKEN, purpose="sign_in", session=None, origin=None)  # type: ignore[arg-type]
    base = ScriptedConn().on("FROM crm.traveler_access a JOIN crm.trip t", _access_row())
    _portal(monkeypatch, base)
    with _raises(Forbidden, "portal_sign_in_required"):
        await portal.request_code(object(), object(), ECHO, TOKEN, purpose="sign", session=None, origin=None)  # type: ignore[arg-type]
    # A signing code while signing is locked: refused before a code row or a mail (re-review S251).
    locked = _signed_in(ScriptedConn()).on("crm.portal_unlocked", False)
    _portal(monkeypatch, locked)
    with _raises(Forbidden, "stage3_locked"):
        await portal.request_code(object(), object(), ECHO, TOKEN, purpose="sign", session="s", origin=None)  # type: ignore[arg-type]
    assert not locked.sql("INSERT INTO crm.otp_challenge") and not locked.sql("INSERT INTO crm.message")
    busy = ScriptedConn().on("FROM crm.traveler_access a JOIN crm.trip t", _access_row()).on("interval '1 hour'", 5)
    _portal(monkeypatch, busy)
    with pytest.raises(RateLimited):
        await portal.request_code(object(), object(), ECHO, TOKEN, purpose="sign_in", session=None, origin=None)  # type: ignore[arg-type]
    no_mail = ScriptedConn().on("FROM crm.traveler_access a JOIN crm.trip t", _access_row())
    _portal(monkeypatch, no_mail)
    with _raises(Conflict, "no_email_on_file"):
        await portal.request_code(object(), object(), ECHO, TOKEN, purpose="sign_in", session=None, origin=None)  # type: ignore[arg-type]
    echo = (
        ScriptedConn()
        .on("FROM crm.traveler_access a JOIN crm.trip t", _access_row())
        .on("SELECT email_enc FROM crm.traveler", enc("traveler", "email_enc", "grace@example.test"))
    )
    _portal(monkeypatch, echo)
    out = await portal.request_code(object(), object(), ECHO, TOKEN, purpose="sign_in", session=None, origin=None)  # type: ignore[arg-type]
    assert out["sent"] is False and len(out["dev_code"]) == 6 and out["dev_code"].isdigit()
    stored = echo.sql("INSERT INTO crm.otp_challenge")[0]
    assert stored[5] == portal.code_hash(stored[0], out["dev_code"])  # only the hash is stored
    assert stored[7] == "dev_echo"  # the evidence will say the API answer carried this code
    _portal(monkeypatch, echo)
    with _raises(Conflict, "code_delivery_unavailable"):
        await portal.request_code(object(), object(), PLAIN, TOKEN, purpose="sign_in", session=None, origin=None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_the_code_mail_leaves_the_agency_mailbox(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = (
        ScriptedConn()
        .on("FROM crm.traveler_access a JOIN crm.trip t", _access_row())
        .on("SELECT email_enc FROM crm.traveler", enc("traveler", "email_enc", "grace@example.test"))
        .on("FROM crm.channel_account", uuid.uuid4())
        .on("FROM crm.conversation", uuid.uuid4())
        .on("SELECT archived_at, deleted_at FROM crm.traveler", {"archived_at": None, "deleted_at": None})
        .on("INSERT INTO crm.dispatch_queue", {"dispatch_id": uuid.uuid4(), "not_before": NOW})
    )
    _portal(monkeypatch, conn)
    out = await portal.request_code(object(), object(), PLAIN, TOKEN, purpose="sign_in", session=None, origin=None)  # type: ignore[arg-type]
    assert out == {"sent": True, "to_hint": "g***@e******.test", "expires_in_s": 600}
    message = conn.sql("INSERT INTO crm.message")[0]
    body = message[4].decode()
    # Review S251: the stored mail (the trip's Messages tab) shows the code masked; the code waits sealed on the
    # otp_challenge row for the sender, which puts it into the mail at send time.
    assert "valid for 10 minutes" in body and CODE_MASK in body and "dev_code" not in out
    stored = conn.sql("INSERT INTO crm.otp_challenge")[0]
    assert stored[7] == "mailbox"
    otp_id, message_id, sealed = conn.sql("SET message_id = $2, code_enc = $3")[0]
    assert otp_id == stored[0] and message_id == message[0]
    code = sealed.decode().split(":", 2)[2]
    assert sealed.startswith(b"enc:otp_challenge.code_enc:") and code not in body
    assert stored[5] == portal.code_hash(otp_id, code)


@pytest.mark.asyncio
async def test_verify_code_outcomes(monkeypatch: pytest.MonkeyPatch) -> None:
    base = ScriptedConn().on("FROM crm.traveler_access a JOIN crm.trip t", _access_row())
    _portal(monkeypatch, base)
    assert await portal.verify_code(object(), ECHO, TOKEN, "123456", origin=None) == (  # type: ignore[arg-type]
        None,
        {"detail": "code_invalid", "attempts_left": 0},
    )
    old = ScriptedConn().on("FROM crm.traveler_access a JOIN crm.trip t", _access_row())
    old.on("ORDER BY created_at DESC LIMIT 1", {"otp_id": OTP, "code_hash": b"x", "attempts": 0, "expired": True})
    _portal(monkeypatch, old)
    assert (await portal.verify_code(object(), ECHO, TOKEN, "123456", origin=None))[1]["detail"] == "code_expired"  # type: ignore[arg-type]
    good = portal.code_hash(OTP, "123456")
    conn = ScriptedConn().on("FROM crm.traveler_access a JOIN crm.trip t", _access_row())
    conn.on("ORDER BY created_at DESC LIMIT 1", {"otp_id": OTP, "code_hash": good, "attempts": 0, "expired": False})
    conn.on("RETURNING attempts, status", {"attempts": 2, "status": "pending"})
    conn.on("RETURNING session_expires_at", NOW)
    ctx = _portal(monkeypatch, conn)
    session, refusal = await portal.verify_code(object(), ECHO, TOKEN, "12345x", origin=None)  # type: ignore[arg-type]
    assert session is None and refusal == {"detail": "code_invalid", "attempts_left": 3}
    session, answer = await portal.verify_code(object(), ECHO, TOKEN, "123456", origin=None)  # type: ignore[arg-type]
    assert (
        session
        and answer["signed_in"]
        and conn.sql("RETURNING session_expires_at")[0][1] == portal.session_hash(session)
    )
    assert [e.action for e in ctx.audit_events][-1] == "portal_sign_in"


@pytest.mark.asyncio
async def test_home_without_and_with_a_session(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = ScriptedConn().on("FROM crm.traveler_access a JOIN crm.trip t", _access_row()).on("display_name", "Sen")
    _portal(monkeypatch, conn)
    assert await portal.home(object(), TOKEN, None) == {"agency_name": "Sen", "signed_in": False}  # type: ignore[arg-type]
    signed = _signed_in(ScriptedConn()).on("display_name", "Sen")
    signed.on("SELECT doc_id, version_label, body_text", {"doc_id": DOC, "version_label": "v1", "body_text": "Risk",
                                                           "body_sha256": b"\x01" * 32})  # fmt: skip
    signed.on("FROM crm.signature_request WHERE trip_id", {"request_id": uuid.uuid4(), "status": "signed",
                                                           "signed_at": NOW})  # fmt: skip
    deposit = {"kind": "deposit", "amount": Decimal(300), "currency": "USD", "due_on": TODAY, "status": "paid"}
    signed.on("JOIN crm.proposal p ON p.proposal_id = s.proposal_id AND p.status = 'accepted'", [deposit])
    _portal(monkeypatch, signed)
    home = await portal.home(object(), TOKEN, "session")  # type: ignore[arg-type]
    assert home["signed_in"] and home["waiver"]["signed"] and home["payments"][0]["amount"] == "300"
    assert home["stage3"]["locked"] and home["party"] == []
    companion = ScriptedConn().on("FROM crm.traveler_access a JOIN crm.trip t", _access_row(role="companion"))
    companion.on("AND session_hash = $2", SESSION_ROW)
    _portal(monkeypatch, companion)
    home = await portal.home(object(), TOKEN, "session")  # type: ignore[arg-type]
    assert "payments" not in home and home["waiver"] is None


@pytest.mark.asyncio
async def test_stage3_is_locked_before_the_deposit(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _portal(monkeypatch, _signed_in(ScriptedConn()))
    with _raises(Forbidden, "stage3_locked") as err:
        await portal.submit_stage3(object(), object(), ECHO, TOKEN, "session", {"dob": "1984-03-02"}, origin=None)  # type: ignore[arg-type]
    assert err.value.status_code == 403
    assert [(e.action, e.outcome) for e in ctx.audit_events] == [("portal_stage3", "denied")]
    _portal(monkeypatch, ScriptedConn().on("FROM crm.traveler_access a JOIN crm.trip t", _access_row()))
    with _raises(Forbidden, "portal_sign_in_required"):
        await portal.submit_stage3(object(), object(), ECHO, TOKEN, "x" * 101, {}, origin=None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("raw", "fields"),
    [
        ({}, ["body"]),
        ([], ["body"]),
        ({"shoe_size": "42"}, ["shoe_size"]),
        ({"dob": "soon", "nationality": "x"}, ["dob", "nationality"]),
    ],  # fmt: skip
)
@pytest.mark.asyncio
async def test_stage3_answers_are_checked(monkeypatch: pytest.MonkeyPatch, raw: Any, fields: list[str]) -> None:
    _portal(monkeypatch, _signed_in(ScriptedConn()).on("crm.portal_unlocked", True))
    with _raises(Invalid, "invalid_submission") as err:
        await portal.submit_stage3(object(), object(), ECHO, TOKEN, "session", raw, origin=None)  # type: ignore[arg-type]
    assert err.value.extra["fields"] == fields


@pytest.mark.asyncio
async def test_stage3_writes_the_f1_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _signed_in(ScriptedConn()).on("crm.portal_unlocked", True)
    _portal(monkeypatch, conn)
    out = await portal.submit_stage3(
        object(),
        object(),
        ECHO,
        TOKEN,
        "session",
        {"passport_no": "pa 123 456", "dob": "1984-03-02"},
        origin=None,  # type: ignore[arg-type]
    )
    assert out == {"status": "received", "missing": []}
    update = conn.sql("UPDATE crm.traveler SET passport_no_enc")[0]
    assert update[1] == enc("traveler", "passport_no_enc", "PA123456") and update[2].startswith(b"enc:traveler.dob")
    assert conn.sql("SET stage3_submitted_at = now()") == [(ACCESS,)]


# The e-sign v1 signing tests (AA-955) are in tests/unit/test_esign.py.


@pytest.mark.asyncio
async def test_sign_out(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = ScriptedConn().on("FROM crm.traveler_access a JOIN crm.trip t", _access_row())
    _portal(monkeypatch, conn)
    assert await portal.sign_out(object(), TOKEN, "session") == {"signed_in": False}  # type: ignore[arg-type]
    assert conn.sql("status = 'verified'\n") or conn.sql("AND session_hash = $2 AND status = 'verified'")


# ── the proposal page (client side) ─────────────────────────────────────────────────────────────


def _page_row(**over: Any) -> dict[str, Any]:
    row = {
        **_proposal_row(status="sent", price_hash=b"\x01" * 32, terms_doc_id=DOC),
        "title": "Laos",
        "start_date": TODAY,
        "end_date": None,
        "accepted_at": None,
        "kind": "text",  # AA-950: the page of a builder version is tested in test_client_proposal.py
        "view_count": 0,
        "owner_member_id": None,
    }
    row.update(over)
    return row


ACCEPT = {"full_name": "Grace Lee", "accept_terms": True, "price_hash": "01" * 32, "terms_doc_id": str(DOC)}


@pytest.mark.asyncio
async def test_the_proposal_page_read(monkeypatch: pytest.MonkeyPatch) -> None:
    _portal(monkeypatch, ScriptedConn(), proposal_page, "proposal")
    with _raises(NotFound, "link_not_found"):
        await proposal_page.read(object(), object(), TOKEN)  # type: ignore[arg-type]
    conn = ScriptedConn().on("WHERE p.link_id = $1", _page_row()).on("display_name", "Sen")
    conn.on("FROM crm.agreement_doc WHERE doc_id", {"doc_id": DOC, "version_label": "v1", "body_text": "Terms"})
    _portal(monkeypatch, conn, proposal_page, "proposal")
    page = await proposal_page.read(object(), object(), TOKEN)  # type: ignore[arg-type]
    assert page["balance_amount"] == "700.00" and page["terms"]["body_text"] == "Terms" and page["body"] == "Day 1"
    assert page["price_hash"] == "01" * 32 and page["schedule"] == []


@pytest.mark.parametrize(
    ("raw", "fields"),
    [
        ("x", ["body"]),
        ({**ACCEPT, "full_name": " "}, ["full_name"]),
        ({**ACCEPT, "accept_terms": False, "price_hash": "1"}, ["accept_terms", "price_hash"]),
        ({**ACCEPT, "terms_doc_id": 5}, ["terms_doc_id"]),
    ],
)
@pytest.mark.asyncio
async def test_accept_checks_its_body(raw: Any, fields: list[str]) -> None:
    with _raises(Invalid, "invalid_submission") as err:
        await proposal_page.accept(object(), object(), PLAIN, TOKEN, raw, origin=None)  # type: ignore[arg-type]
    assert err.value.extra["fields"] == fields


@pytest.mark.asyncio
async def test_accept_outcomes(monkeypatch: pytest.MonkeyPatch) -> None:
    _portal(monkeypatch, ScriptedConn(), proposal_page, "proposal")
    with _raises(NotFound, "link_not_found"):
        await proposal_page.accept(object(), object(), PLAIN, TOKEN, ACCEPT, origin=None)  # type: ignore[arg-type]
    _portal(monkeypatch, ScriptedConn().on("WHERE p.link_id = $1", _page_row(status="accepted", accepted_at=NOW)),
            proposal_page, "proposal")  # fmt: skip
    assert (await proposal_page.accept(object(), object(), PLAIN, TOKEN, ACCEPT, origin=None))["status"] == (  # type: ignore[arg-type]
        "already_accepted"
    )
    _portal(monkeypatch, ScriptedConn().on("WHERE p.link_id = $1", _page_row()), proposal_page, "proposal")
    with _raises(Conflict, "price_changed"):
        await proposal_page.accept(object(), object(), PLAIN, TOKEN, {**ACCEPT, "price_hash": "02" * 32}, origin=None)  # type: ignore[arg-type]
    with _raises(Conflict, "terms_changed"):
        await proposal_page.accept(object(), object(), PLAIN, TOKEN, {**ACCEPT, "terms_doc_id": None}, origin=None)  # type: ignore[arg-type]
    conn = ScriptedConn().on("WHERE p.link_id = $1", _page_row()).on("RETURNING accepted_at", NOW)
    conn.on("AT TIME ZONE", TODAY)
    conn.on("FROM crm.agreement_doc WHERE doc_id = $1", {"doc_id": DOC, "version_label": "v1", "body_text": "T",
                                                         "body_sha256": b"\x01" * 32})  # fmt: skip
    conn.on("INSERT INTO crm.terms_acceptance", uuid.uuid4())
    _portal(monkeypatch, conn, proposal_page, "proposal")
    out = await proposal_page.accept(
        object(),
        object(),
        PLAIN,
        TOKEN,
        ACCEPT,
        origin=None,
        client_ip="192.0.2.7",
        user_agent="Test/1",  # type: ignore[arg-type]
    )
    assert out == {"status": "accepted", "accepted_at": NOW.isoformat(), "terms_reaccept_required": False}
    # AA-955 (R34): the acceptance pinned to the terms version, its hash and the price hash, with the evidence.
    (acc,) = conn.sql("INSERT INTO crm.terms_acceptance")
    assert acc[6] == "proposal" and acc[7] == DOC and acc[8] == b"\x01" * 32 and acc[9] == b"\x01" * 32
    assert acc[2] == PROPOSAL and acc[4] == LINK and acc[11].startswith(b"enc:terms_acceptance.client_ip_enc")
    record = json.loads(acc[13])
    assert record["source"] == "proposal" and record["price_hash"] == "01" * 32 and "Grace" not in acc[13].decode()
    assert record["client_ip_sha256"] == hashlib.sha256(b"192.0.2.7").hexdigest()
    assert hashlib.sha256(acc[13]).digest() == acc[14]
    rows = conn.sql("INSERT INTO crm.payment_schedule")
    assert [(r[2], r[3]) for r in rows] == [("deposit", Decimal(300)), ("balance", Decimal(700))]
    assert conn.sql("'collect_deposit'") and conn.sql("SET status = 'submitted'") == [(LINK,)]


# ── date ticks and the gates ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_date_ticks(monkeypatch: pytest.MonkeyPatch) -> None:
    complete, missing, soon, no_return = (uuid.uuid4() for _ in range(4))

    async def refresh(_ctx: Any, trip_id: uuid.UUID) -> progress.Progress:
        return progress.Progress(s3=100 if trip_id == complete else 50, waiver_doc_id=None, travellers=())

    monkeypatch.setattr(progress, "refresh", refresh)
    conn = (
        ScriptedConn()
        .on("AT TIME ZONE", TODAY)
        .on(
            "FOR UPDATE SKIP LOCKED",
            [{"trip_id": complete, "stage": "booked"}, {"trip_id": missing, "stage": "booked"}],
        )
        .on("t.end_date IS NULL", [{"trip_id": no_return}])
        .on("$1 + $2::int", [{"trip_id": soon}, {"trip_id": complete}])
    )
    conn.on("DELETE FROM crm.otp_challenge", None)
    conn.on("UPDATE crm.otp_challenge", None)

    async def execute(sql: str, *args: Any) -> str:
        conn._answer(sql, args)
        if "otp_challenge" in sql:
            return "UPDATE 1" if sql.lstrip().startswith("UPDATE") else "DELETE 2"
        return "OK"

    conn.execute = execute  # type: ignore[method-assign]
    counts = await stages_job.run_agency(system_ctx(conn))
    assert counts["booked_to_travelling"] == 2 and counts["urgent_tasks"] == 1
    assert counts["return_date_missing"] == 1 and counts["stage3_due_t21"] == 1 and counts["codes_purged"] == 2
    assert counts["codes_expired"] == 1
    # The stale pending codes expire (and lose code_enc) before the purge.
    expire = next(i for i, (sql, _) in enumerate(conn.calls) if "UPDATE crm.otp_challenge" in sql)
    purge = next(i for i, (sql, _) in enumerate(conn.calls) if "DELETE FROM crm.otp_challenge" in sql)
    assert expire < purge
    reasons = {args[0]: args[3] for args in conn.sql("INSERT INTO crm.trip_stage_history")}
    assert reasons == {complete: "date_rule", missing: "date_rule_gate_unmet"}


@pytest.mark.asyncio
async def test_the_gates_read_the_deposit_and_s3(monkeypatch: pytest.MonkeyPatch) -> None:
    trip = {"trip_id": TRIP, "stage": "proposal", "intake_completeness": {"s1": 0, "s2": 0, "s3": 0}}
    paid = tenant_ctx(ScriptedConn().on("s.status = 'paid'", True))
    assert await stages._check_gate(paid, None, trip, "booked", None) == (None, None)
    with _raises(Invalid, "override_not_needed"):
        await stages._check_gate(paid, None, trip, "booked", "manual_booking")
    unpaid = tenant_ctx(ScriptedConn().on("s.status = 'paid'", False))
    with _raises(Conflict, "gate_unmet"):
        await stages._check_gate(unpaid, None, trip, "booked", None)
    assert await stages._check_gate(unpaid, None, trip, "booked", "manual_booking") == ("booking", "manual_booking")
    booked = {**trip, "stage": "booked", "intake_completeness": {"s1": 0, "s2": 0, "s3": 100}}
    assert await stages._check_gate(unpaid, None, booked, "pre_departure", None) == (None, None)

    async def refresh(_ctx: Any, _trip: uuid.UUID) -> progress.Progress:
        return progress.Progress(s3=40, waiver_doc_id=None, travellers=())

    monkeypatch.setattr(progress, "refresh", refresh)
    with _raises(Conflict, "gate_unmet"):
        await stages._check_gate(unpaid, FakeVault(), booked, "pre_departure", None)  # type: ignore[arg-type]


def test_the_dev_code_echo_is_dev_only() -> None:
    from app.settings import SettingsError, load_settings

    assert load_settings({"ENVIRONMENT": "dev", "PORTAL_OTP_DEV_ECHO": "true"}).portal_otp_dev_echo is True
    assert load_settings({"ENVIRONMENT": "dev"}).portal_otp_dev_echo is False
    for env in ("stg", "prod"):
        with pytest.raises(SettingsError, match="PORTAL_OTP_DEV_ECHO"):
            load_settings({"ENVIRONMENT": env, "PORTAL_OTP_DEV_ECHO": "true"})
