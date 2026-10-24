"""crm-005 Slice 1 on PostgreSQL 15 with the RDS role shape, as crm_app: intake -> crm_intake_process -> board.

Card CRM-08:
- **R**: a submission with the honeypot filled, or without the consent box, creates no row (and no job); the drop is
  counted only (no payload in the log).
- **E**: the same e-mail submitting again while its Inquiry is open attaches to that trip (C47 in R6): no second trip,
  no second traveller.
- Done when (lane draft): form submit -> card on the board; viewing the name writes an audit event; F1 ciphertext
  only (no plaintext e-mail, name or message in any row).
Plus: the public form lookup policy, stage moves along the DB edges, assignment rules, the LLM gateway order and log.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from decimal import Decimal
from typing import Any

import asyncpg
import pytest

from app.auth.route_guard import Caller
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.intake import forms as intake_forms
from app.intake import public as intake
from app.intake.process import build_handler
from app.jobs.queue import Job
from app.jobs.worker import PermanentError
from app.llm.gateway import LlmGateway, StubProvider, StubScrubber, stub_gateway
from app.llm.usage import llm_usage
from app.pipeline import BoardFilter, assign, board, move_stage, set_task_status, trip_detail
from app.settings import Settings
from app.tenancy import intake_tx, system_tx, tenant_tx
from app.travellers.reveal import reveal
from app.vault import KeyProvider, Vault
from app.vault.wrapping import LocalKeyWrapper

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ISS = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_TESTPARTN"
SETTINGS = Settings(environment="test", db_ssl="disable")
PAGE = "https://go-test.example.test"
SETTINGS_WITH_ORIGIN = Settings(environment="test", db_ssl="disable", intake_page_origins=(PAGE,))


def caller(sub: str) -> Caller:
    return Caller("agency", "partner", "partner-web", ISS, sub, ())


def _keys() -> KeyProvider:
    return KeyProvider({"local": LocalKeyWrapper({"k1": os.urandom(32)}, "k1")}, "local")


async def _agency(pool: asyncpg.Pool, *roles: str) -> tuple[uuid.UUID, list[tuple[Caller, uuid.UUID]]]:
    tenant_id = uuid.uuid4()
    people: list[tuple[Caller, uuid.UUID]] = []
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        await conn.execute("SELECT set_config('app.actor', 'seed:test', true)")  # crm-008: crm_app names its actor
        await conn.execute("INSERT INTO crm.tenant (tenant_id, display_name) VALUES ($1, 'Sen Vàng Travel')", tenant_id)
        for role in roles or ("owner",):
            sub = f"sub-{uuid.uuid4()}"
            member_id = await conn.fetchval(
                "INSERT INTO crm.member (cognito_iss, cognito_sub, role, status) VALUES ($1, $2, $3, 'invited') "
                "RETURNING member_id",
                ISS,
                sub,
                role,
            )
            await conn.execute("UPDATE crm.member SET status = 'active' WHERE member_id = $1", member_id)
            people.append((caller(sub), member_id))
    return tenant_id, people


async def _form(pool: asyncpg.Pool, tenant_id: uuid.UUID, who: Caller, **kw: Any) -> intake_forms.FormView:
    async with tenant_tx(pool, who, tenant_id) as ctx:
        return await intake_forms.create_form(ctx, label="Website", **kw)


def answers(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "destinations": ["vietnam", "laos"],
        "month": "2027-03",
        "nights": 10,
        "adults": 2,
        "child_ages": [7],
        "party_type": "family",
        "budget_band": "2000_3500",
        "trip_style": "balanced",
        "name": "Nguyễn Thị Hoa",
        "email": "Hoa.Nguyen@Example.test",
        "phone": "+84912345678",
        "preferred_channel": "whatsapp",
        "message": "We would love Sapa and a slow boat in Laos.",
        "companions": ["Trần Văn Nam"],
        "consent_contact": True,
        "consent_marketing": False,
        "client_submission_id": str(uuid.uuid4()),
    }
    base.update(over)
    return base


async def _count(pool: asyncpg.Pool, tenant_id: uuid.UUID, table: str) -> int:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        return int(await conn.fetchval(f"SELECT count(*) FROM crm.{table}"))  # noqa: S608 — test, fixed names


async def _owner_fetch(admin: asyncpg.Connection, tenant_id: uuid.UUID, sql: str, *args: Any) -> list[asyncpg.Record]:
    """Read as crm_owner in the agency's context (FORCE RLS binds the master and the owner too)."""
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE crm_owner")
        await admin.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        return await admin.fetch(sql, *args)


async def _jobs(pool: asyncpg.Pool, submission_id: str | None = None) -> list[asyncpg.Record]:
    async with pool.acquire() as conn:
        if submission_id is None:
            return await conn.fetch("SELECT id, payload FROM crm.job WHERE kind = 'crm_intake_process'")
        return await conn.fetch(
            "SELECT id, payload FROM crm.job WHERE kind = 'crm_intake_process' AND payload->>'submission_id' = $1",
            submission_id,
        )


async def _process_all(pool: asyncpg.Pool, keys: KeyProvider, *tenants: uuid.UUID) -> list[dict[str, Any]]:
    """Run the agencies' queued intake jobs through the real handler, then end them (the worker's part)."""
    handler = build_handler(keys)
    out = []
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id::text, payload FROM crm.job WHERE kind = 'crm_intake_process' AND status = 'queued' "
            "AND payload->>'tenant_id' = ANY($1::text[]) ORDER BY created_at, id",
            [str(t) for t in tenants],
        )
    for r in rows:
        payload = json.loads(r["payload"]) if isinstance(r["payload"], str) else dict(r["payload"])
        out.append(await handler(pool, Job(r["id"], "crm_intake_process", payload, 1, 5)))
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE crm.job SET status = 'cancelled', finished_at = now() WHERE id = $1::uuid", r["id"]
            )
    return out


async def test_crm005_is_registered_and_the_lookup_needs_the_key(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-005'") == 1
    tenant_id, [(owner, _)] = await _agency(app_pool)
    form = await _form(app_pool, tenant_id, owner)
    async with app_pool.acquire() as conn, conn.transaction():
        # No tenant, no key: nothing. The key opens exactly that one active form, and nothing else of the agency.
        assert await conn.fetchval("SELECT count(*) FROM crm.intake_form") == 0
        await conn.execute("SELECT set_config('app.intake_key', $1, true)", form.public_key)
        assert await conn.fetchval("SELECT count(*) FROM crm.intake_form") == 1
        assert await conn.fetchval("SELECT count(*) FROM crm.form_definition") == 0
    async with intake_tx(app_pool, form.public_key) as (ctx, found):
        assert found.tenant_id == tenant_id and found.honeypot_field == form.__dict__.get(
            "honeypot_field", found.honeypot_field
        )
    with pytest.raises(NotFound):
        async with intake_tx(app_pool, "x" * 22):
            pass


async def test_the_lookup_key_grants_no_write(app_pool: asyncpg.Pool) -> None:
    tenant_id, [(owner, _)] = await _agency(app_pool)
    form = await _form(app_pool, tenant_id, owner)
    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.intake_key', $1, true)", form.public_key)
        status = await conn.execute("UPDATE crm.intake_form SET label = 'Hacked'")
        assert status == "UPDATE 0"


async def test_r_honeypot_or_no_consent_leaves_no_row(app_pool: asyncpg.Pool, caplog: pytest.LogCaptureFixture) -> None:
    keys = _keys()
    tenant_id, [(owner, _)] = await _agency(app_pool)
    form = await _form(app_pool, tenant_id, owner)
    async with intake_tx(app_pool, form.public_key) as (_, found):
        trap = found.honeypot_field
    before = dict(intake.DROPS)
    caplog.set_level(logging.INFO, logger="app.intake.public")
    result = await intake.submit(
        app_pool, keys, SETTINGS, form.public_key, answers(**{trap: "http://spam"}), origin=None
    )
    assert result.stored is False and result.dropped == "honeypot"
    with pytest.raises(Invalid) as no_consent:
        await intake.submit(app_pool, keys, SETTINGS, form.public_key, answers(consent_contact=False), origin=None)
    assert no_consent.value.code == "consent_required"
    with pytest.raises(Invalid):
        await intake.submit(app_pool, keys, SETTINGS, form.public_key, answers(consent_contact=None), origin=None)
    assert await _count(app_pool, tenant_id, "form_submission") == 0
    assert await _count(app_pool, tenant_id, "traveler") == 0
    assert await _jobs(app_pool) == [] or all(
        json.loads(j["payload"]).get("tenant_id") != str(tenant_id) for j in await _jobs(app_pool)
    )
    assert intake.DROPS["honeypot"] == before.get("honeypot", 0) + 1
    assert intake.DROPS["no_consent"] == before.get("no_consent", 0) + 2
    text = caplog.text
    assert "intake_dropped reason=honeypot" in text and "intake_dropped reason=no_consent" in text
    assert "Hoa" not in text and "example.test" not in text and "spam" not in text  # count only, no payload


async def test_invalid_answers_name_the_fields_only(app_pool: asyncpg.Pool) -> None:
    tenant_id, [(owner, _)] = await _agency(app_pool)
    form = await _form(app_pool, tenant_id, owner)
    with pytest.raises(Invalid) as bad:
        await intake.submit(
            app_pool, _keys(), SETTINGS, form.public_key, answers(email="not-an-email", adults=0), origin=None
        )
    assert bad.value.code == "invalid_submission" and bad.value.extra["fields"] == ["adults", "email"]
    assert await _count(app_pool, tenant_id, "form_submission") == 0


async def test_origin_is_checked_when_configured(app_pool: asyncpg.Pool) -> None:
    tenant_id, [(owner, _)] = await _agency(app_pool)
    form = await _form(app_pool, tenant_id, owner, allowed_origins=["https://www.agency.example"])
    keys = _keys()
    with pytest.raises(Forbidden):
        await intake.submit(
            app_pool, keys, SETTINGS_WITH_ORIGIN, form.public_key, answers(), origin="https://evil.example"
        )
    with pytest.raises(Forbidden):
        await intake.submit(app_pool, keys, SETTINGS_WITH_ORIGIN, form.public_key, answers(), origin=None)
    assert (await intake.submit(app_pool, keys, SETTINGS_WITH_ORIGIN, form.public_key, answers(), origin=PAGE)).stored
    assert (
        await intake.submit(
            app_pool, keys, SETTINGS_WITH_ORIGIN, form.public_key, answers(), origin="https://www.agency.example"
        )
    ).stored


async def test_submit_process_board_and_ciphertext_only(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    keys = _keys()
    tenant_id, [(owner, owner_id)] = await _agency(app_pool)
    form = await _form(app_pool, tenant_id, owner, default_owner_member_id=owner_id)
    body = answers()
    assert (await intake.submit(app_pool, keys, SETTINGS, form.public_key, body, origin=None)).stored
    # A retried POST (same page submission id) stores nothing new and queues nothing new.
    assert not (await intake.submit(app_pool, keys, SETTINGS, form.public_key, body, origin=None)).stored
    assert await _count(app_pool, tenant_id, "form_submission") == 1
    [result] = [r for r in await _process_all(app_pool, keys, tenant_id) if r["status"] != "skipped"]
    assert result["status"] == "created"
    trip_id = uuid.UUID(result["trip_id"])
    for table, n in (
        ("traveler", 2),
        ("traveler_profile", 2),
        ("trip", 1),
        ("trip_traveler", 2),
        ("trip_party", 2),
        ("task", 1),
        ("note", 1),
        ("consent", 2),
        ("trip_stage_history", 1),
    ):
        assert await _count(app_pool, tenant_id, table) == n, table

    # Ciphertext only: no plaintext identity value or message anywhere in the agency's rows (Done-when).
    dump = []
    for table in ("traveler", "form_submission", "note", "trip", "traveler_profile", "consent", "task", "audit_event"):
        rows = await _owner_fetch(admin, tenant_id, f"SELECT * FROM crm.{table}")  # noqa: S608
        assert rows, table  # the dump is not vacuous
        dump += [repr(dict(r)) for r in rows]
    blob = "\n".join(dump)
    for secret in ("Hoa", "Nguyễn", "example.test", "912345678", "Sapa", "Trần"):
        assert secret not in blob, secret

    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        view = await board(ctx, Vault(keys, ctx), BoardFilter())
        inquiry = next(s for s in view["stages"] if s["stage"] == "inquiry")
        [card] = inquiry["cards"]
        assert card["trip_id"] == str(trip_id) and card["lead_name"] == "Nguyễn Thị Hoa"
        assert card["waiting_on"] == "agency" and card["is_mine"] and card["companions"] == 1
        assert card["title"] == "Vietnam + Laos, 10 nights" and card["next_due_at"] is not None
        assert view["waiting_on_counts"]["agency"] == 1 and view["total"] == 1
        assert "email" not in json.dumps(card) and "+849" not in json.dumps(card)
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        detail = await trip_detail(ctx, Vault(keys, ctx), trip_id)
        assert [p["role"] for p in detail["party"]] == ["lead", "companion"]
        assert detail["notes"][0]["kind"] == "inquiry_message" and "Sapa" in detail["notes"][0]["body"]
        assert detail["tasks"][0]["kind"] == "reply_inquiry" and detail["tasks"][0]["assignee_member_id"] == str(
            owner_id
        )
        assert detail["submissions"][0]["answers"]["budget_band"] == "2000_3500"
        assert "email" not in detail["submissions"][0]["answers"]
        lead = detail["party"][0]["traveler_id"]
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        revealed = await reveal(ctx, Vault(keys, ctx), uuid.UUID(lead), "email", "contact_client")
        assert revealed["value"] == "hoa.nguyen@example.test"
    events = await _owner_fetch(
        admin, tenant_id, "SELECT action, object_kind, fields, actor_kind FROM crm.audit_event ORDER BY at"
    )
    actions = [(e["action"], e["object_kind"]) for e in events]
    assert ("intake_received", "form_submission") in actions and ("intake_created", "trip") in actions
    assert ("list_view", "pipeline") in actions and ("view", "trip") in actions and ("reveal", "traveler") in actions
    assert [e["fields"] for e in events if e["action"] == "reveal"] == [["email"]]


async def test_e_repeat_email_attaches_to_the_open_inquiry(app_pool: asyncpg.Pool) -> None:
    keys = _keys()
    tenant_id, [(owner, owner_id)] = await _agency(app_pool)
    form = await _form(app_pool, tenant_id, owner)
    await intake.submit(app_pool, keys, SETTINGS, form.public_key, answers(companions=[]), origin=None)
    [first] = await _process_all(app_pool, keys, tenant_id)
    # The same person again, another spelling of the e-mail, a new message, while the Inquiry is open.
    again = answers(email="  hoa.nguyen@EXAMPLE.test ", message="Also Hanoi, please.", companions=[], nights=12)
    await intake.submit(app_pool, keys, SETTINGS, form.public_key, again, origin=None)
    [second] = await _process_all(app_pool, keys, tenant_id)
    assert first["status"] == "created" and second["status"] == "attached"
    assert second["trip_id"] == first["trip_id"] and second["traveler_id"] == first["traveler_id"]
    assert await _count(app_pool, tenant_id, "trip") == 1
    assert await _count(app_pool, tenant_id, "traveler") == 1
    assert await _count(app_pool, tenant_id, "note") == 2
    assert await _count(app_pool, tenant_id, "task") == 1  # the open reply task is not duplicated
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        detail = await trip_detail(ctx, Vault(keys, ctx), uuid.UUID(first["trip_id"]))
        assert [s["outcome"] for s in detail["submissions"]] == ["created", "attached"]
        # Done reply task + a repeat inquiry: the reply task opens again.
        await set_task_status(ctx, uuid.UUID(detail["tasks"][0]["task_id"]), "done")
    await intake.submit(app_pool, keys, SETTINGS, form.public_key, answers(companions=[]), origin=None)
    [third] = await _process_all(app_pool, keys, tenant_id)
    assert third["status"] == "attached"
    assert await _count(app_pool, tenant_id, "task") == 2

    # Once the trip is lost, the next inquiry is a new trip for the same traveller (repeat client).
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        await move_stage(ctx, uuid.UUID(first["trip_id"]), "lost", lost_reason="postponed")
    await intake.submit(app_pool, keys, SETTINGS, form.public_key, answers(companions=[]), origin=None)
    [fourth] = await _process_all(app_pool, keys, tenant_id)
    assert fourth["status"] == "created" and fourth["trip_id"] != first["trip_id"]
    assert fourth["traveler_id"] == first["traveler_id"]
    assert await _count(app_pool, tenant_id, "traveler") == 1
    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        assert await conn.fetchval("SELECT max(repeat_count) FROM crm.traveler_profile") == 1
    del owner_id


async def test_two_jobs_for_the_same_new_person_at_once_make_one_traveller(app_pool: asyncpg.Pool) -> None:
    """Two workers overlapping (rolling deploy): the advisory lock serialises the dedupe lookup, so the second job
    attaches to the trip the first one created instead of creating a twin traveller and trip (R6 / C47)."""
    keys = _keys()
    tenant_id, [(owner, _)] = await _agency(app_pool)
    form = await _form(app_pool, tenant_id, owner)
    await intake.submit(app_pool, keys, SETTINGS, form.public_key, answers(companions=[]), origin=None)
    await intake.submit(
        app_pool, keys, SETTINGS, form.public_key, answers(companions=[], consent_marketing=True), origin=None
    )
    async with app_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id::text, payload FROM crm.job WHERE kind = 'crm_intake_process' AND status = 'queued' "
            "AND payload->>'tenant_id' = $1 ORDER BY created_at, id",
            str(tenant_id),
        )
    handler = build_handler(keys)
    jobs = [
        Job(
            r["id"],
            "crm_intake_process",
            json.loads(r["payload"]) if isinstance(r["payload"], str) else dict(r["payload"]),
            1,
            5,
        )
        for r in rows
    ]
    results = await asyncio.gather(*(handler(app_pool, j) for j in jobs))
    async with app_pool.acquire() as conn:
        await conn.execute(
            "UPDATE crm.job SET status = 'cancelled', finished_at = now() WHERE id = ANY($1::uuid[])",
            [j.id for j in jobs],
        )
    assert sorted(r["status"] for r in results) == ["attached", "created"]
    assert results[0]["traveler_id"] == results[1]["traveler_id"] and results[0]["trip_id"] == results[1]["trip_id"]
    assert await _count(app_pool, tenant_id, "traveler") == 1
    assert await _count(app_pool, tenant_id, "trip") == 1
    # The F2 flag follows the latest marketing consent row (no OR-ing, attach path included).
    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        # The attaching job ran second (it saw the first job's trip): its marketing row is the latest one.
        latest = await conn.fetchval(
            "SELECT c.value FROM crm.consent c JOIN crm.form_submission s ON s.submission_id = c.submission_id "
            "WHERE c.purpose = 'marketing' AND s.outcome = 'attached'"
        )
        flag = await conn.fetchval("SELECT marketing_ok FROM crm.traveler_profile")
    assert flag is latest


async def test_dedupe_by_phone_and_per_agency(app_pool: asyncpg.Pool) -> None:
    keys = _keys()
    a, [(owner_a, _)] = await _agency(app_pool)
    b, [(owner_b, _)] = await _agency(app_pool)
    form_a = await _form(app_pool, a, owner_a)
    form_b = await _form(app_pool, b, owner_b)
    await intake.submit(app_pool, keys, SETTINGS, form_a.public_key, answers(companions=[]), origin=None)
    await intake.submit(app_pool, keys, SETTINGS, form_b.public_key, answers(companions=[]), origin=None)
    results = await _process_all(app_pool, keys, a, b)
    assert [r["status"] for r in results] == ["created", "created"]  # the same e-mail in two agencies: no link
    phone_only = answers(email="other@example.test", companions=[])
    await intake.submit(app_pool, keys, SETTINGS, form_a.public_key, phone_only, origin=None)
    [by_phone] = await _process_all(app_pool, keys, a)
    assert by_phone["status"] == "attached" and by_phone["trip_id"] == results[0]["trip_id"]
    async with tenant_tx(app_pool, owner_b, b) as ctx:
        view = await board(ctx, Vault(keys, ctx), BoardFilter())
        assert sum(s["count"] for s in view["stages"]) == 1
        with pytest.raises(NotFound):
            await trip_detail(ctx, Vault(keys, ctx), uuid.UUID(results[0]["trip_id"]))


async def test_job_is_idempotent_and_refuses_a_locked_agency(app_pool: asyncpg.Pool) -> None:
    keys = _keys()
    tenant_id, [(owner, _)] = await _agency(app_pool)
    form = await _form(app_pool, tenant_id, owner)
    await intake.submit(app_pool, keys, SETTINGS, form.public_key, answers(), origin=None)
    [row] = [r for r in await _jobs(app_pool) if json.loads(r["payload"])["tenant_id"] == str(tenant_id)]
    payload = json.loads(row["payload"])
    handler = build_handler(keys)
    job = Job(str(row["id"]), "crm_intake_process", payload, 1, 5)
    assert (await handler(app_pool, job))["status"] == "created"
    assert (await handler(app_pool, job))["status"] == "skipped"
    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        await conn.execute("UPDATE crm.tenant SET status = 'locked'")
    with pytest.raises(PermanentError):
        await handler(app_pool, job)
    with pytest.raises(PermanentError):
        await handler(app_pool, Job("x", "crm_intake_process", {"tenant_id": "nope"}, 1, 5))
    with pytest.raises(NotFound):  # a locked agency's form is not served
        await intake.read_form(app_pool, form.public_key)
    async with app_pool.acquire() as conn:
        await conn.execute("UPDATE crm.job SET status = 'cancelled', finished_at = now() WHERE id = $1", row["id"])


async def test_a_payload_that_does_not_open_fails_the_submission(app_pool: asyncpg.Pool) -> None:
    keys = _keys()
    tenant_id, [(owner, _)] = await _agency(app_pool)
    form = await _form(app_pool, tenant_id, owner)
    await intake.submit(app_pool, keys, SETTINGS, form.public_key, answers(), origin=None)
    [row] = [r for r in await _jobs(app_pool) if json.loads(r["payload"])["tenant_id"] == str(tenant_id)]
    other_keys = _keys()  # another KEK: the stored DEK does not unwrap
    with pytest.raises(PermanentError):
        await build_handler(other_keys)(
            app_pool, Job(str(row["id"]), "crm_intake_process", json.loads(row["payload"]), 1, 5)
        )
    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        assert await conn.fetchval("SELECT status FROM crm.form_submission") == "failed"
        assert await conn.fetchval("SELECT failure_class FROM crm.form_submission") is not None
        await conn.execute("UPDATE crm.job SET status = 'cancelled', finished_at = now() WHERE id = $1", row["id"])


async def test_stage_moves_follow_the_db_edges(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    keys = _keys()
    tenant_id, [(owner, _)] = await _agency(app_pool)
    form = await _form(app_pool, tenant_id, owner)
    await intake.submit(app_pool, keys, SETTINGS, form.public_key, answers(), origin=None)
    [r] = await _process_all(app_pool, keys, tenant_id)
    trip_id = uuid.UUID(r["trip_id"])
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        moved = await move_stage(ctx, trip_id, "qualifying", lost_reason=None)
        assert moved["allowed_moves"] == ["inquiry", "proposal", "lost"]
    with pytest.raises(Conflict) as refused:
        async with tenant_tx(app_pool, owner, tenant_id) as ctx:
            await move_stage(ctx, trip_id, "proposal", lost_reason=None)  # CRM-15 (AA-943): the Stage 2 gate
    assert refused.value.code == "stage2_incomplete"
    with pytest.raises(Conflict) as refused:
        async with tenant_tx(app_pool, owner, tenant_id) as ctx:
            await move_stage(ctx, trip_id, "booked", lost_reason=None)
    assert refused.value.code == "transition_not_allowed"
    with pytest.raises(Invalid):
        async with tenant_tx(app_pool, owner, tenant_id) as ctx:
            await move_stage(ctx, trip_id, "lost", lost_reason=None)
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        await move_stage(ctx, trip_id, "lost", lost_reason="went_elsewhere")
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:  # one move per request, as the web does
        await move_stage(ctx, trip_id, "inquiry", lost_reason=None, undo=True)
    history = await _owner_fetch(
        admin,
        tenant_id,
        "SELECT from_stage, to_stage, reason_code FROM crm.trip_stage_history WHERE trip_id = $1 ORDER BY at",
        trip_id,
    )
    assert [(h["from_stage"], h["to_stage"], h["reason_code"]) for h in history] == [
        (None, "inquiry", "intake"),
        ("inquiry", "qualifying", "agent_move"),
        ("qualifying", "lost", "lost_went_elsewhere"),
        ("lost", "inquiry", "undo"),
    ]
    [trip] = await _owner_fetch(admin, tenant_id, "SELECT lost_reason_code FROM crm.trip WHERE trip_id = $1", trip_id)
    assert trip["lost_reason_code"] is None
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with app_pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
            await conn.execute("UPDATE crm.trip_stage_history SET reason_code = 'x'")


async def test_assignment_rules(app_pool: asyncpg.Pool) -> None:
    keys = _keys()
    tenant_id, [(owner, owner_id), (agent, agent_id), (assistant, assistant_id)] = await _agency(
        app_pool, "owner", "agent", "assistant"
    )
    form = await _form(app_pool, tenant_id, owner)  # no default owner: the trip starts unassigned
    await intake.submit(app_pool, keys, SETTINGS, form.public_key, answers(), origin=None)
    [r] = await _process_all(app_pool, keys, tenant_id)
    trip_id = uuid.UUID(r["trip_id"])
    with pytest.raises(Forbidden):
        async with tenant_tx(app_pool, agent, tenant_id) as ctx:
            await assign(ctx, trip_id, assistant_id)  # an agent cannot hand a trip to someone else
    async with tenant_tx(app_pool, agent, tenant_id) as ctx:
        taken = await assign(ctx, trip_id, agent_id)  # but may take an unassigned one
        assert taken["is_mine"]
    with pytest.raises(Forbidden):
        async with tenant_tx(app_pool, assistant, tenant_id) as ctx:
            await assign(ctx, trip_id, assistant_id)  # no longer unassigned
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        await assign(ctx, trip_id, owner_id)  # the owner reassigns
        view = await board(ctx, Vault(keys, ctx), BoardFilter(owner="me"))
        assert sum(s["count"] for s in view["stages"]) == 1
        detail = await trip_detail(ctx, Vault(keys, ctx), trip_id)
        assert detail["tasks"][0]["assignee_member_id"] == str(owner_id)


async def test_llm_gateway_order_and_log(app_pool: asyncpg.Pool) -> None:
    tenant_id, _ = await _agency(app_pool)

    class RealProvider(StubProvider):
        name = "bedrock"
        model = "us.example-model-v1"

    async with system_tx(app_pool, tenant_id, actor="job:crm_test") as ctx:
        off = await stub_gateway().call(ctx, stage="crm_test_reply", role="writer", prompt="Hello there")
        assert (off.outcome, off.refusal_code) == ("refused", "ai_disabled")  # default: AI off (D12)
        unscrubbed = await LlmGateway(StubScrubber(), RealProvider()).call(
            ctx, stage="crm_test_reply", role="writer", prompt="Hello"
        )
        assert unscrubbed.refusal_code == "scrubber_unavailable"
        await ctx.conn.execute("INSERT INTO crm.tenant_settings (ai_enabled, ai_budget_usd) VALUES (true, 2)")
        ok = await stub_gateway().call(ctx, stage="crm_test_reply", role="writer", prompt="Hello there")
        assert ok.outcome == "ok" and ok.text and ok.budget_used_pct == 0.0
        await ctx.conn.execute("UPDATE crm.tenant_settings SET ai_budget_usd = 0")
        broke = await stub_gateway().call(ctx, stage="crm_test_reply", role="writer", prompt="Hello")
        assert broke.refusal_code == "budget_exhausted"
        rows = await ctx.conn.fetch(
            "SELECT outcome, refusal_code, scrub_receipt FROM crm.llm_call ORDER BY at, llm_call_id"
        )
        assert sorted(r["outcome"] for r in rows) == ["ok", "refused", "refused", "refused"]
        assert sorted(r["refusal_code"] or "" for r in rows) == [
            "",
            "ai_disabled",
            "budget_exhausted",
            "scrubber_unavailable",
        ]
        assert "Hello" not in repr([dict(r) for r in rows])  # no text in the log
    from datetime import UTC, datetime

    today = datetime.now(UTC).date()
    usage = await llm_usage(app_pool, today, today)
    mine = [u for u in usage if u["stage"] == "crm_test_reply"]
    assert sum(u["calls"] for u in mine) >= 4 and sum(u["failed_calls"] for u in mine) >= 3
    assert all(Decimal(u["cost_usd"]) == 0 for u in mine)


async def test_forms_admin_rules(app_pool: asyncpg.Pool) -> None:
    tenant_id, [(owner, owner_id)] = await _agency(app_pool)
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        form = await intake_forms.create_form(ctx, label="Blog", allowed_origins=["https://Blog.Agency.example/"])
        assert form.allowed_origins == ["https://blog.agency.example"] and len(form.public_key) == 22
        with pytest.raises(Invalid):
            await intake_forms.create_form(ctx, label="Bad", allowed_origins=["http://plain.example"])
        off = await intake_forms.update_form(ctx, uuid.UUID(form.intake_form_id), {"status": "disabled"})
        assert off.status == "disabled"
        again = await intake_forms.update_form(
            ctx, uuid.UUID(form.intake_form_id), {"status": "active", "default_owner_member_id": str(owner_id)}
        )
        assert again.default_owner_member_id == str(owner_id)
        await intake_forms.update_form(ctx, uuid.UUID(form.intake_form_id), {"status": "disabled"})
    with pytest.raises(NotFound):  # a disabled form is not served
        await intake.read_form(app_pool, form.public_key)
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        await intake_forms.update_form(ctx, uuid.UUID(form.intake_form_id), {"status": "active"})
    shown = await intake.read_form(app_pool, form.public_key)
    assert shown["schema_key"] == "stage1.v1" and shown["agency_name"] == "Sen Vàng Travel"
    assert shown["allowed_origins"] == ["https://blog.agency.example"] and shown["honeypot_field"]


async def test_seeder_stores_synthetic_inquiries_through_the_intake_path(
    app_pool: asyncpg.Pool, migrated_app_dsn: str
) -> None:
    import base64
    import io

    from app import seed
    from app.seed.catalogue import BY_KEY
    from app.vault import build_key_provider
    from tests.tokens import LocalPools

    settings = LocalPools().settings(
        database_url=migrated_app_dsn,
        cognito_external_issuer=ISS,
        cognito_partner_issuer="",
        key_provider="local",
        local_kek_b64=base64.b64encode(os.urandom(32)).decode(),
    )
    argv = ["--apply", "--travellers", "--agency", "harbour-lights"]
    out = io.StringIO()
    assert await seed.main(argv, out, settings) == 0, out.getvalue()
    line = json.loads(out.getvalue().splitlines()[0])
    assert line["travellers"]["written"] == BY_KEY["harbour-lights"].traveller_count == 12
    assert line["intake_form"]["public_key"] and "@" not in out.getvalue()
    again = io.StringIO()
    assert await seed.main(argv, again, settings) == 0
    second = json.loads(again.getvalue().splitlines()[0])
    assert second["travellers"]["written"] == 0 and second["intake_form"]["created"] is False
    tenant_id = BY_KEY["harbour-lights"].tenant_id
    results = await _process_all(app_pool, build_key_provider(settings), tenant_id)
    assert [r["status"] for r in results] == ["created"] * 12
    assert await _count(app_pool, tenant_id, "traveler") == 12
    io_refused = io.StringIO()
    assert await seed.main(["--travellers"], io_refused, settings) == 2
