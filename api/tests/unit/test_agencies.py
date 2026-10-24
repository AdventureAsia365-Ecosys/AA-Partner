"""Agency provisioning rules (AA-935, card CRM-07): the synthetic-only rule (R test: 422), name normalisation (E test:
Vietnamese diacritics), member validation, converging writes. SQL behaviour itself: tests/integration."""

from __future__ import annotations

import unicodedata
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agencies import (
    LOCALES,
    AgencySpec,
    MemberSpec,
    check_environment,
    normalise_name,
    provision_agency,
    validate_agency,
    validate_member,
)
from app.errors import Conflict, Invalid, ServiceError, service_error_handler
from app.tenancy import provisioning_tx
from tests.tokens import EXTERNAL_ISSUER
from tests.unit.fakes import ScriptedConn

TENANT = uuid.UUID("00000000-0000-4000-8000-0000000000c1")
MEMBER = uuid.UUID("00000000-0000-4000-8000-0000000000d1")
SEN_VANG = "Lữ hành Sen Vàng"


def spec(**kw: object) -> AgencySpec:
    values: dict[str, object] = {"tenant_id": TENANT, "display_name": SEN_VANG, "default_locale": "vi-VN"}
    values.update(kw)
    return AgencySpec(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("environment", ["dev", "test", "local"])
def test_dev_refuses_a_non_synthetic_agency_with_422(environment: str) -> None:
    with pytest.raises(Invalid) as info:
        validate_agency(spec(is_synthetic=False), environment)
    assert (info.value.status_code, info.value.code) == (422, "synthetic_required")


def test_prod_refuses_a_synthetic_agency_and_stg_takes_either() -> None:
    with pytest.raises(Invalid, match="production") as info:
        check_environment("prod", True)
    assert info.value.code == "synthetic_not_allowed"
    check_environment("prod", False)
    check_environment("stg", True)
    check_environment("stg", False)


def test_the_422_reaches_http_as_the_service_error_body() -> None:
    app = FastAPI()
    app.add_exception_handler(ServiceError, service_error_handler)

    @app.post("/create")
    def create() -> None:
        validate_agency(spec(is_synthetic=False), "dev")

    r = TestClient(app).post("/create")
    assert r.status_code == 422
    assert r.json()["detail"] == "synthetic_required"


def test_vietnamese_name_is_nfc_and_round_trips() -> None:
    decomposed = unicodedata.normalize("NFD", SEN_VANG)
    assert decomposed != SEN_VANG
    stored = normalise_name(f"  {decomposed}  ")
    assert stored == SEN_VANG
    assert stored.encode("utf-8").decode("utf-8") == SEN_VANG
    assert normalise_name("Southern   Cross  Journeys") == "Southern Cross Journeys"


@pytest.mark.parametrize("bad", ["", "   ", "x" * 121, "Bad\x00name", "tab\tname"])
def test_bad_names_are_refused(bad: str) -> None:
    with pytest.raises(Invalid, match="agency name") as info:
        normalise_name(bad)
    assert info.value.code == "invalid_display_name"


def test_locale_and_tenant_id_are_checked() -> None:
    assert LOCALES == ("en-AU", "en-GB", "en-US", "vi-VN")
    with pytest.raises(Invalid) as info:
        validate_agency(spec(default_locale="fr-FR"), "dev")
    assert info.value.code == "invalid_locale"
    with pytest.raises(Invalid) as info:
        validate_agency(spec(tenant_id=str(TENANT)), "dev")
    assert info.value.code == "invalid_tenant_id"


def test_member_validation() -> None:
    ok = validate_member(MemberSpec(EXTERNAL_ISSUER, "  sub-1  ", "agent"))
    assert ok.cognito_sub == "sub-1"
    for bad, code in [
        (MemberSpec(EXTERNAL_ISSUER, "s", "admin"), "invalid_role"),
        (MemberSpec("http://issuer/x", "s", "owner"), "invalid_issuer"),
        (MemberSpec(EXTERNAL_ISSUER, "", "owner"), "invalid_subject"),
        (MemberSpec(EXTERNAL_ISSUER, "x" * 129, "owner"), "invalid_subject"),
    ]:
        with pytest.raises(Invalid) as info:
            validate_member(bad)
        assert info.value.code == code


@pytest.mark.asyncio
async def test_create_sets_the_new_tenant_then_inserts() -> None:
    conn = ScriptedConn().on("INSERT INTO crm.tenant", TENANT).on("INSERT INTO crm.member", MEMBER)
    result = await provision_agency(
        conn,
        spec(),
        (MemberSpec(EXTERNAL_ISSUER, "sub-owner", "owner"),),
        environment="dev",
        actor="seed:test",
    )
    assert (result.created, result.updated, result.members_added) == (True, False, 1)
    flats = [flat for flat, _ in conn.calls]
    assert flats[0].startswith("SELECT set_config('app.tenant_id'") and conn.calls[0][1] == (str(TENANT),)
    assert conn.sql("'app.actor'") == [("seed:test",)]
    assert conn.sql("INSERT INTO crm.tenant")[0] == (TENANT, SEN_VANG, "vi-VN", True)
    assert conn.sql("INSERT INTO crm.member")[0] == (EXTERNAL_ISSUER, "sub-owner", "owner")
    # AA-940 (design §4.9 "role changes"): the new agency and the member given a role are on the agency audit, as
    # the seeder (actor kind system), after the writes and in the same transaction.
    events = conn.sql("INSERT INTO crm.audit_event")
    assert [e[:3] for e in events] == [("system", "seed:test", "agency_create"), ("system", "seed:test", "member_add")]
    assert events[0][4:6] == ("agency", str(TENANT)) and events[1][4:7] == ("member", str(MEMBER), ["role"])
    assert flats[-1].startswith("INSERT INTO crm.audit_event")


@pytest.mark.asyncio
async def test_existing_agency_converges_and_roles_follow_the_spec() -> None:
    conn = (
        ScriptedConn()
        .on(
            "SELECT display_name, default_locale, is_synthetic",
            {"display_name": "Old", "default_locale": "en-AU", "is_synthetic": True},
        )
        .on("UPDATE crm.member SET role", MEMBER)
    )
    calls: list[str] = []

    async def execute(sql: str, *args: object) -> str:
        conn._answer(sql, args)
        calls.append(sql)
        return "UPDATE 1"

    conn.execute = execute  # type: ignore[method-assign]
    result = await provision_agency(
        conn, spec(), (MemberSpec(EXTERNAL_ISSUER, "sub-a", "assistant"),), environment="dev", actor="seed:test"
    )
    assert (result.created, result.updated, result.members_added, result.members_updated) == (False, True, 0, 1)
    assert conn.sql("UPDATE crm.tenant SET display_name")[0] == (TENANT, SEN_VANG, "vi-VN")
    # The L1 console (a platform: actor) acts as AA ops; the role change names the member, never the role value.
    platform = (
        ScriptedConn()
        .on(
            "SELECT display_name, default_locale, is_synthetic",
            {"display_name": SEN_VANG, "default_locale": "vi-VN", "is_synthetic": True},
        )
        .on("UPDATE crm.member SET role", MEMBER)
    )
    await provision_agency(
        platform, spec(), (MemberSpec(EXTERNAL_ISSUER, "sub-a", "owner"),), environment="dev", actor="platform:l1"
    )
    events = platform.sql("INSERT INTO crm.audit_event")
    assert events == [
        ("aa_ops", "platform:l1", "member_role_change", "allowed", "member", str(MEMBER), ["role"], None, None, [])
    ]


@pytest.mark.asyncio
async def test_unchanged_agency_writes_nothing_and_the_flag_never_flips() -> None:
    same = {"display_name": SEN_VANG, "default_locale": "vi-VN", "is_synthetic": True}
    conn = ScriptedConn().on("SELECT display_name, default_locale, is_synthetic", same)
    result = await provision_agency(conn, spec(), environment="dev", actor="seed:test")
    assert (result.created, result.updated) == (False, False)
    assert conn.sql("UPDATE crm.tenant") == [] and conn.sql("INSERT INTO crm.audit_event") == []
    flipped = ScriptedConn().on("SELECT display_name, default_locale, is_synthetic", {**same, "is_synthetic": False})
    with pytest.raises(Conflict, match="never changes"):
        await provision_agency(flipped, spec(), environment="stg", actor="seed:test")
    gone = ScriptedConn()
    with pytest.raises(Conflict) as info:
        await provision_agency(gone, spec(), environment="dev", actor="seed:test")
    assert info.value.code == "tenant_unavailable"


@pytest.mark.asyncio
async def test_validation_happens_before_any_sql() -> None:
    conn = ScriptedConn()
    with pytest.raises(Invalid):
        await provision_agency(conn, spec(is_synthetic=False), environment="dev", actor="seed:test")
    with pytest.raises(Invalid):
        await provision_agency(
            conn, spec(), (MemberSpec(EXTERNAL_ISSUER, "s", "boss"),), environment="dev", actor="seed:test"
        )
    assert conn.calls == []


@pytest.mark.asyncio
async def test_provisioning_tx_checks_its_inputs() -> None:
    with pytest.raises(TypeError):
        async with provisioning_tx(ScriptedConn(), str(TENANT), actor="seed:x"):  # type: ignore[arg-type]
            pass
    with pytest.raises(ValueError, match="provisioning actor"):
        async with provisioning_tx(ScriptedConn(), TENANT, actor="member:1"):
            pass
