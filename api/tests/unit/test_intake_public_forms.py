"""Public intake (card CRM-08 R test: honeypot or no consent -> no row) and forms admin, Python branches (AA-936)."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from app.errors import Forbidden, Invalid, NotFound, Unavailable
from app.intake import forms, public
from app.settings import Settings
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import (
    DEFINITION_ID,
    FORM_ID,
    MEMBER,
    FakeVault,
    fake_tx,
    intake_form,
    system_ctx,
    tenant_ctx,
)

pytestmark = pytest.mark.asyncio

TEST = Settings(environment="test", db_ssl="disable")
PAGE = "https://go-dev.example.test"
DEV = Settings(environment="dev", intake_page_origins=(PAGE,))


def body(**over: Any) -> dict[str, Any]:
    base = {
        "destinations": ["laos"],
        "adults": 1,
        "budget_band": "under_1000",
        "name": "An",
        "email": "an@x.test",
        "consent_contact": True,
        "client_submission_id": str(uuid.uuid4()),
    }
    base.update(over)
    return base


@pytest.fixture
def intake(monkeypatch: pytest.MonkeyPatch) -> ScriptedConn:
    conn = ScriptedConn().on("INSERT INTO crm.form_submission", uuid.uuid4())
    monkeypatch.setattr(public, "intake_tx", fake_tx(system_ctx(conn), intake_form()))
    monkeypatch.setattr(public, "Vault", lambda keys, ctx: FakeVault())
    return conn


async def test_r_honeypot_and_no_consent_write_nothing(intake: ScriptedConn, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="app.intake.public")
    trapped = await public.submit(None, None, TEST, "k" * 22, body(website="http://spam"), origin=None)  # type: ignore[arg-type]
    assert trapped.stored is False and trapped.dropped == "honeypot"
    with pytest.raises(Invalid) as exc:
        await public.submit(None, None, TEST, "k" * 22, body(consent_contact=False), origin=None)  # type: ignore[arg-type]
    assert exc.value.code == "consent_required"
    with pytest.raises(Invalid) as bad:
        await public.submit(None, None, TEST, "k" * 22, body(email="x"), origin=None)  # type: ignore[arg-type]
    assert bad.value.extra == {"fields": ["email"]}
    assert intake.calls == []  # not one statement ran: no row, no job
    assert "spam" not in caplog.text and "an@x.test" not in caplog.text and "reason=honeypot" in caplog.text


async def test_stored_submission_queues_one_job_in_the_same_transaction(intake: ScriptedConn) -> None:
    result = await public.submit(None, None, TEST, "k" * 22, body(), origin=None)  # type: ignore[arg-type]
    assert result.stored
    [insert] = intake.sql("INSERT INTO crm.form_submission")
    assert insert[3].startswith(b"enc:form_submission.payload_enc:") and insert[4].startswith("page:")
    [job] = intake.sql("INSERT INTO crm.job")
    assert job[0] == "crm_intake_process" and set(json.loads(job[1])) == {"tenant_id", "submission_id"}
    intake.on("INSERT INTO crm.form_submission", None)  # the same page submission again
    again = await public.submit(None, None, TEST, "k" * 22, body(), origin=None)  # type: ignore[arg-type]
    assert again.dropped == "duplicate" and len(intake.sql("INSERT INTO crm.job")) == 1


async def test_origin_rules(intake: ScriptedConn) -> None:
    with pytest.raises(Unavailable):
        await public.submit(None, None, Settings(environment="dev"), "k" * 22, body(), origin=PAGE)  # type: ignore[arg-type]
    with pytest.raises(Forbidden):
        await public.submit(None, None, DEV, "k" * 22, body(), origin="https://evil.example")  # type: ignore[arg-type]
    with pytest.raises(Forbidden):
        await public.submit(None, None, DEV, "k" * 22, body(), origin=None)  # type: ignore[arg-type]
    assert (await public.submit(None, None, DEV, "k" * 22, body(), origin=PAGE + "/")).stored  # type: ignore[arg-type]
    assert (await public.submit(None, None, DEV, "k" * 22, body(), origin="https://www.agency.example")).stored  # type: ignore[arg-type]
    assert public.origin_allowed(None, TEST, ()) is True


async def test_read_form(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = ScriptedConn().on(
        "FROM crm.form_definition d",
        {"schema": '{"schema_key": "stage1.v1"}', "display_name": "Sen", "default_locale": "vi-VN"},
    )
    monkeypatch.setattr(public, "intake_tx", fake_tx(system_ctx(conn), intake_form()))
    shown = await public.read_form(None, "k" * 22)  # type: ignore[arg-type]
    assert shown == {
        "schema_key": "stage1.v1",
        "honeypot_field": "website",
        "agency_name": "Sen",
        "default_locale": "vi-VN",
        "allowed_origins": ["https://www.agency.example"],
        "privacy_notice_url": None,
    }


def form_row(**over: Any) -> dict[str, Any]:
    base = {
        "intake_form_id": FORM_ID,
        "public_key": "p" * 22,
        "label": "Website",
        "allowed_origins": [],
        "honeypot_field": "website",
        "default_owner_member_id": None,
        "lead_source": "website_form",
        "privacy_notice_url": None,
        "status": "active",
        "created_at": datetime(2026, 10, 24, tzinfo=UTC),
    }
    base.update(over)
    return base


async def test_normalisers() -> None:
    assert forms.normalise_origins(None) == []
    assert forms.normalise_origins(["https://A.example/", "https://a.example"]) == ["https://a.example"]
    for bad in (["http://a.example"], ["https://a.example/path"], [5], "https://a.example", ["https://a"] * 11):
        with pytest.raises(Invalid):
            forms.normalise_origins(bad)
    assert (
        forms.normalise_privacy_url("") is None
        and forms.normalise_privacy_url("https://a.example/p") == "https://a.example/p"
    )
    for bad in ("http://a", "https://a b", 5):
        with pytest.raises(Invalid):
            forms.normalise_privacy_url(bad)
    with pytest.raises(Invalid):
        forms.normalise_label("  ")


async def test_create_list_update_forms() -> None:
    conn = (
        ScriptedConn()
        .on("SELECT count(*) FROM crm.intake_form", 0)
        .on("SELECT form_definition_id FROM crm.form_definition", None)
        .on("INSERT INTO crm.form_definition", DEFINITION_ID)
        .on("INSERT INTO crm.intake_form", form_row())
        .on("FROM crm.member WHERE member_id", 1)
    )
    ctx = tenant_ctx(conn)
    made = await forms.create_form(ctx, label=" Website ", default_owner_member_id=MEMBER)
    args = conn.sql("INSERT INTO crm.intake_form")[0]
    assert made.intake_form_id == str(FORM_ID) and len(args[0]) == 22 and args[1] == "Website"
    assert args[4] in forms.HONEYPOT_NAMES and args[2] == DEFINITION_ID
    conn.on("SELECT count(*) FROM crm.intake_form", forms.MAX_FORMS)
    with pytest.raises(Invalid):
        await forms.create_form(ctx, label="x")
    conn.on("SELECT count(*) FROM crm.intake_form", 0)
    with pytest.raises(Invalid):
        await forms.create_form(ctx, label="x", lead_source="Bad Source")
    conn.on("FROM crm.member WHERE member_id", None)
    with pytest.raises(Invalid):
        await forms.create_form(ctx, label="x", default_owner_member_id=MEMBER)
    conn.on("FROM crm.intake_form WHERE deleted_at", [form_row()])
    assert [f.label for f in await forms.list_forms(ctx)] == ["Website"]

    uconn = ScriptedConn().on("FOR UPDATE", form_row()).on("UPDATE crm.intake_form", form_row(status="disabled"))
    uctx = tenant_ctx(uconn)
    assert (await forms.update_form(uctx, FORM_ID, {"status": "active"})).status == "active"  # nothing changed
    changed = await forms.update_form(
        uctx,
        FORM_ID,
        {
            "status": "disabled",
            "label": "Blog",
            "allowed_origins": [],
            "privacy_notice_url": None,
            "default_owner_member_id": None,
        },
    )
    assert changed.status == "disabled" and "status = $2" in uconn.calls[-1][0]
    with pytest.raises(Invalid):
        await forms.update_form(uctx, FORM_ID, {"status": "gone"})
    with pytest.raises(Invalid):
        await forms.update_form(uctx, FORM_ID, {"default_owner_member_id": str(MEMBER)})
    with pytest.raises(NotFound):
        await forms.update_form(tenant_ctx(), FORM_ID, {})


async def test_definition_found_or_raced() -> None:
    found = ScriptedConn().on("SELECT form_definition_id", DEFINITION_ID)
    assert await forms.ensure_stage1_definition(tenant_ctx(found)) == DEFINITION_ID
    raced = ScriptedConn().on("INSERT INTO crm.form_definition", None)
    assert await forms.ensure_stage1_definition(tenant_ctx(raced)) is None
