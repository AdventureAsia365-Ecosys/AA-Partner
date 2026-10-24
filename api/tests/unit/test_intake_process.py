"""``crm_intake_process`` branches (AA-936): new, attach (card E test), repeat client, skip, failures.

Real dedupe through blind indexes on PostgreSQL: tests/integration/test_crm005_intake.py.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from app.errors import Forbidden
from app.intake import process
from app.jobs.queue import Job
from app.jobs.worker import PermanentError
from app.vault import DecryptionDenied
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import FORM_ID, MEMBER, TENANT, FakeVault, enc, fake_tx, system_ctx

pytestmark = pytest.mark.asyncio

SUB = uuid.UUID("00000000-0000-4000-8000-0000000005b1")
KNOWN = uuid.UUID("00000000-0000-4000-8000-0000000005b2")
OPEN_TRIP = uuid.UUID("00000000-0000-4000-8000-0000000005b3")


def answers(**over: Any) -> dict[str, Any]:
    base = {
        "destinations": ["vietnam"],
        "adults": 2,
        "budget_band": "not_sure",
        "name": "Hoa",
        "email": "hoa@x.test",
        "consent_contact": True,
        "month": None,
        "start_date": "2027-03-01",
        "end_date": "2027-03-11",
        "date_flex_days": 0,
        "nights": 10,
        "child_ages": [],
        "party_type": None,
        "trip_style": None,
        "phone": "+84912345678",
        "preferred_channel": None,
        "heard_from": None,
        "message": "Hi",
        "companions": ["Nam"],
        "consent_marketing": True,
        "locale": "en",
        "client_submission_id": str(uuid.uuid4()),
    }
    base.update(over)
    return base


def submission(status: str = "received", **over: Any) -> dict[str, Any]:
    payload = json.dumps(answers(**over))
    return {
        "submission_id": SUB,
        "intake_form_id": FORM_ID,
        "status": status,
        "default_owner_member_id": MEMBER,
        "lead_source": "website_form",
        "default_locale": "en-AU",
        "payload_enc": enc("form_submission", "payload_enc", payload),
    }


async def test_new_traveller_trip_party_task_note_consent() -> None:
    conn = ScriptedConn().on("FROM crm.form_submission s", submission())
    ctx = system_ctx(conn)
    out = await process.process_submission(ctx, FakeVault(), SUB)
    assert out.status == "created" and out.trip_id and out.traveler_id
    assert len(conn.sql("INSERT INTO crm.traveler (traveler_id, display_name_enc, email_enc")) == 1
    # Dedupe runs under the agency's e-mail then phone advisory locks (no unique index on the blind indexes).
    assert [a[:2] for a in conn.sql("pg_advisory_xact_lock")] == [
        ("crm_intake_email", str(TENANT)),
        ("crm_intake_phone", str(TENANT)),
    ]
    assert conn.calls.index(next(c for c in conn.calls if "pg_advisory_xact_lock" in c[0])) < conn.calls.index(
        next(c for c in conn.calls if "WHERE email_bidx" in c[0])
    )
    assert len(conn.sql("INSERT INTO crm.traveler (traveler_id, display_name_enc, name_bidx")) == 1  # companion
    assert [a[2] for a in conn.sql("INSERT INTO crm.trip_traveler")] == ["lead", "companion"]
    assert len(conn.sql("INSERT INTO crm.trip_party")) == 2 and len(conn.sql("INSERT INTO crm.task")) == 1
    assert conn.sql("INSERT INTO crm.note")[0][3] == "inquiry_message"
    assert [(a[1], a[2]) for a in conn.sql("INSERT INTO crm.consent")] == [("contact", True), ("marketing", True)]
    trip_args = conn.sql("INSERT INTO crm.trip (")[0]
    assert trip_args[1] == "Vietnam, 10 nights" and str(trip_args[2]) == "2027-03-01"
    assert conn.sql("SET status = 'processed'")[0][1] == "created"
    assert ctx.audit_events[-1].reason == "new_inquiry"


async def test_e_repeat_email_with_an_open_inquiry_attaches() -> None:
    token = uuid.uuid4()
    conn = (
        ScriptedConn()
        .on("FROM crm.form_submission s", submission(message=None, phone=None, consent_marketing=False))
        .on(
            "WHERE email_bidx",
            {"traveler_id": KNOWN, "profile_token_enc": enc("traveler", "profile_token_enc", str(token))},
        )
        .on("FROM crm.trip t", {"trip_id": OPEN_TRIP, "owner_member_id": None})
    )
    ctx = system_ctx(conn)
    out = await process.process_submission(ctx, FakeVault(), SUB)
    assert (out.status, out.trip_id, out.traveler_id) == ("attached", OPEN_TRIP, KNOWN)
    assert conn.sql("INSERT INTO crm.trip (") == [] and conn.sql("INSERT INTO crm.traveler") == []
    assert conn.sql("last_inquiry_at = now()") == [(OPEN_TRIP,)]
    assert conn.sql("INSERT INTO crm.note") == []  # no message, no note
    assert [a[0] for a in conn.sql("pg_advisory_xact_lock")] == ["crm_intake_email"]  # no phone, one lock
    # The consent ledger and the F2 flag agree: the latest marketing row (False here) drives marketing_ok.
    assert [(a[1], a[2]) for a in conn.sql("INSERT INTO crm.consent")] == [("contact", True), ("marketing", False)]
    assert conn.sql("SET marketing_ok = $2") == [(token, False)]
    assert ctx.audit_events[-1].action == "intake_attached"


async def test_known_by_phone_without_open_trip_is_a_repeat_client() -> None:
    token = uuid.uuid4()
    conn = (
        ScriptedConn()
        .on("FROM crm.form_submission s", submission(companions=[]))
        .on(
            "WHERE phone_bidx",
            {"traveler_id": KNOWN, "profile_token_enc": enc("traveler", "profile_token_enc", str(token))},
        )
    )
    ctx = system_ctx(conn)
    out = await process.process_submission(ctx, FakeVault(), SUB)
    assert out.status == "created" and out.traveler_id == KNOWN
    bump = conn.sql("repeat_count = repeat_count + 1")[0]
    assert bump[0] == token and bump[3] is True  # marketing_ok = this submission's marketing row, not OR-ed
    assert conn.sql("INSERT INTO crm.traveler") == []
    assert ctx.audit_events[-1].reason == "repeat_client"


async def test_processed_or_missing_submission_is_skipped() -> None:
    assert (await process.process_submission(system_ctx(), FakeVault(), SUB)).status == "skipped"
    conn = ScriptedConn().on("FROM crm.form_submission s", submission(status="processed"))
    assert (await process.process_submission(system_ctx(conn), FakeVault(), SUB)).result() == {
        "status": "skipped",
        "trip_id": None,
        "traveler_id": None,
    }


async def test_handler_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    job = Job("j1", "crm_intake_process", {"tenant_id": str(TENANT), "submission_id": str(SUB)}, 1, 5)
    with pytest.raises(PermanentError):
        await process.build_handler(object())(None, Job("j", "crm_intake_process", {}, 1, 5))  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        await process.build_handler(None)(None, job)  # type: ignore[arg-type]

    conn = ScriptedConn()
    monkeypatch.setattr(process, "system_tx", fake_tx(system_ctx(conn)))
    monkeypatch.setattr(process, "Vault", lambda keys, ctx: FakeVault())

    async def done(ctx, vault, sid):  # noqa: ANN001, ANN202
        return process.Outcome("created", OPEN_TRIP, KNOWN)

    monkeypatch.setattr(process, "process_submission", done)
    assert (await process.build_handler(object())(None, job))["status"] == "created"  # type: ignore[arg-type]

    async def denied(ctx, vault, sid):  # noqa: ANN001, ANN202
        raise DecryptionDenied("integrity_check_failed")

    monkeypatch.setattr(process, "process_submission", denied)
    with pytest.raises(PermanentError) as exc:
        await process.build_handler(object())(None, job)  # type: ignore[arg-type]
    assert exc.value.code == "DecryptionDenied" and conn.sql("SET status = 'failed'")[0] == (SUB, "DecryptionDenied")

    async def locked(ctx, vault, sid):  # noqa: ANN001, ANN202
        raise Forbidden("tenant_locked")

    monkeypatch.setattr(process, "process_submission", locked)
    with pytest.raises(PermanentError) as exc:
        await process.build_handler(object())(None, job)  # type: ignore[arg-type]
    assert exc.value.code == "tenant_locked"
