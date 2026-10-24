"""AA-937 (card CRM-09) Python branches: F1 fields, reveal with a reason, the traveller's self view, age bands,
the client directory and profile, edits, health, relationships and the party of a trip.

The SQL, RLS, the inverse trigger and the reader grants run on PostgreSQL in tests/integration/test_crm006_*.py.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

import asyncpg
import pytest

from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.travellers import age_bands, fields, service
from app.travellers.reveal import reveal
from app.travellers.self_reveal import TravellerSession, traveller_self_reveal
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import MEMBER, TENANT, FakeVault, enc, system_ctx, tenant_ctx

T1 = uuid.UUID("00000000-0000-4000-8000-0000000000f1")
T2 = uuid.UUID("00000000-0000-4000-8000-0000000000f2")
TRIP = uuid.UUID("00000000-0000-4000-8000-0000000000e1")
TOKEN = uuid.UUID("00000000-0000-4000-8000-0000000000d1")
PARTY = uuid.UUID("00000000-0000-4000-8000-0000000000d2")
NOW = dt.datetime(2026, 10, 24, 8, 0, tzinfo=dt.UTC)


def traveler_row(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "traveler_id": T1,
        "display_name_enc": enc("traveler", "display_name_enc", "Anna Martin"),
        "given_name_enc": None,
        "family_name_enc": None,
        "email_enc": enc("traveler", "email_enc", "anna@example.test"),
        "phone_enc": None,
        "dob_enc": None,
        "nationality_enc": None,
        "passport_no_enc": None,
        "passport_expiry_enc": None,
        "passport_country_enc": None,
        "emergency_contact_enc": None,
        "profile_token_enc": enc("traveler", "profile_token_enc", str(TOKEN)),
        "preferred_channel": "email",
        "owner_member_id": MEMBER,
        "lead_source": "website_form",
        "referrer_traveler_id": None,
        "archived_at": None,
        "archive_reason": None,
        "created_at": NOW,
    }
    base.update(over)
    return base


def profile_row(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {k: None for k in fields.PROFILE_FIELDS}
    base.update(
        traveler_token=TOKEN,
        interests=["food"],
        must_do_codes=[],
        must_avoid_codes=[],
        dietary_flags=["vegetarian"],
        mobility_flags=[],
        repeat_count=1,
        stage2_completeness=40,
        marketing_ok=False,
        budget_band="2000_3500",
    )
    base.update(over)
    return base


# ── fields ─────────────────────────────────────────────────────────────────────────────────────────
def test_clean_f1_normalises_and_refuses() -> None:
    assert fields.clean_f1("display_name", "  Nguyễn Thị Hoa ") == "Nguyễn Thị Hoa"
    assert fields.clean_f1("email", " Anna@Example.TEST ") == "anna@example.test"
    assert fields.clean_f1("phone", "+84 912 345 678") == "+84912345678"
    assert fields.clean_f1("dob", "1990-04-03") == "1990-04-03"
    assert fields.clean_f1("passport_expiry", "2031-01-01") == "2031-01-01"
    assert fields.clean_f1("nationality", "vn") == "VN"
    assert fields.clean_f1("passport_no", "c 1234-567") == "C1234567"
    assert fields.clean_f1("emergency_contact", "Tom, +61 400 000 000") == "Tom, +61 400 000 000"
    assert fields.clean_f1("given_name", None) is None and fields.clean_f1("phone", "  ") is None
    for field, value in (
        ("display_name", None),
        ("display_name", "x" * 300),
        ("email", "not-an-email"),
        ("phone", "0912"),
        ("dob", "2999-01-01"),
        ("dob", "03/04/1990"),
        ("nationality", "Vietnam"),
        ("passport_no", "!!"),
        ("given_name", 5),
    ):
        with pytest.raises(Invalid) as err:
            fields.clean_f1(field, value)
        assert err.value.code == f"invalid_{field}"
    with pytest.raises(Invalid):
        fields.clean_f1("profile_token", "x")


def test_clean_profile_health_and_mask() -> None:
    assert fields.clean_profile(
        {
            "fitness_band": "4_6h",
            "interests": ["food", "food", "trekking"],
            "altitude_ok": False,
            "residence_country": "au",
            "occasion": None,
        }
    ) == {
        "fitness_band": "4_6h",
        "interests": ["food", "trekking"],
        "altitude_ok": False,
        "residence_country": "AU",
        "occasion": None,
    }
    for bad in (
        {"fitness_band": "marathon"},
        {"interests": ["Food & wine"]},
        {"interests": "food"},
        {"altitude_ok": "yes"},
        {"residence_country": "Australia"},
        {"name": "Anna"},
    ):
        with pytest.raises(Invalid):
            fields.clean_profile(bad)
    assert fields.clean_health(["vegan"], None, ["asthma"]) == {
        "dietary": ["vegan"],
        "mobility": [],
        "medical": ["asthma"],
    }
    with pytest.raises(Invalid):
        fields.clean_health(["pho_lover"], [], [])
    assert fields.mask_passport("C1234567") == "•••• 567" and fields.mask_passport(None) is None


# ── age bands (R32) ────────────────────────────────────────────────────────────────────────────────
def test_age_band_boundaries() -> None:
    dep = dt.date(2027, 3, 12)
    assert age_bands.age_band(dt.date(1990, 3, 12), dep) == "35_44"  # 37 on the day
    assert age_bands.age_band(dt.date(1992, 3, 13), dep) == "25_34"  # 34, birthday the day after
    assert age_bands.age_band(dt.date(2009, 3, 12), dep) == "18_24"
    assert age_bands.age_band(dt.date(2015, 6, 1), dep) == "2_11"
    assert age_bands.age_band(dt.date(2026, 1, 1), dep) == "under_2"
    assert age_bands.age_band(dt.date(1940, 1, 1), dep) == "75_plus"
    assert age_bands.age_band(None, dep) is None and age_bands.age_band(dt.date(1990, 1, 1), None) is None
    assert age_bands.age_band(dt.date(2028, 1, 1), dep) is None
    assert age_bands.BAND_CODES[0] == "under_2" and age_bands.BAND_CODES[-1] == "75_plus"


@pytest.mark.asyncio
async def test_recompute_for_traveler_changes_only_what_moved_and_audits_once() -> None:
    conn = (
        ScriptedConn()
        .on("SELECT dob_enc, profile_token_enc FROM crm.traveler", traveler_row())
        .on(
            "WHERE tp.traveler_token = $1",
            [
                {
                    "trip_party_id": PARTY,
                    "trip_id": TRIP,
                    "age_band_at_departure": "25_34",
                    "start_date": dt.date(2027, 3, 1),
                },
                {
                    "trip_party_id": uuid.uuid4(),
                    "trip_id": uuid.uuid4(),
                    "age_band_at_departure": "35_44",
                    "start_date": dt.date(2027, 3, 1),
                },
            ],
        )
    )
    ctx = tenant_ctx(conn)
    changed = await age_bands.recompute_for_traveler(ctx, FakeVault(), T1, dt.date(1990, 1, 1))
    assert changed == 1 and conn.sql("UPDATE crm.trip_party SET age_band_at_departure") == [(PARTY, "35_44")]
    [event] = ctx.audit_events
    assert (event.action, event.fields, event.reason, len(event.object_ids)) == (
        "age_band_recompute",
        ("dob",),
        "dob_changed",
        2,
    )
    # No traveller or no F2 pointer: nothing to do, nothing audited.
    for row in (None, traveler_row(profile_token_enc=None)):
        ctx = tenant_ctx(ScriptedConn().on("SELECT dob_enc, profile_token_enc FROM crm.traveler", row))
        assert await age_bands.recompute_for_traveler(ctx, FakeVault(), T1, None) == 0 and ctx.audit_events == []


@pytest.mark.asyncio
async def test_recompute_for_trip_matches_each_twin() -> None:
    other = uuid.uuid4()
    conn = (
        ScriptedConn()
        .on(
            "FROM crm.trip_party WHERE trip_id = $1",
            [
                {"trip_party_id": PARTY, "traveler_token": TOKEN, "age_band_at_departure": None},
                {"trip_party_id": uuid.uuid4(), "traveler_token": other, "age_band_at_departure": None},
            ],
        )
        .on(
            "FROM crm.trip_traveler tt JOIN crm.traveler tr",
            [
                {
                    "traveler_id": T1,
                    "dob_enc": enc("traveler", "dob_enc", "2015-06-01"),
                    "profile_token_enc": enc("traveler", "profile_token_enc", str(TOKEN)),
                },
                {"traveler_id": T2, "dob_enc": None, "profile_token_enc": None},
            ],
        )
    )
    ctx = tenant_ctx(conn)
    assert await age_bands.recompute_for_trip(ctx, FakeVault(), TRIP, dt.date(2027, 3, 12)) == 1
    assert conn.sql("UPDATE crm.trip_party SET age_band_at_departure") == [(PARTY, "2_11")]
    assert [e.reason for e in ctx.audit_events] == ["start_date_changed"]
    # The event names the travellers whose DOB was decrypted (T2 has no profile token: not read).
    assert ctx.audit_events[0].object_ids == (str(T1),)
    empty = tenant_ctx(ScriptedConn())
    assert await age_bands.recompute_for_trip(empty, FakeVault(), TRIP, None) == 0 and empty.audit_events == []


@pytest.mark.asyncio
async def test_recompute_party_member() -> None:
    conn = ScriptedConn()
    ctx = tenant_ctx(conn)
    band = await age_bands.recompute_party_member(
        ctx, FakeVault(), PARTY, T1, enc("traveler", "dob_enc", "1990-01-01"), dt.date(2027, 3, 1)
    )
    assert band == "35_44" and conn.sql("UPDATE crm.trip_party") == [(PARTY, "35_44")]
    assert await age_bands.recompute_party_member(ctx, FakeVault(), PARTY, T1, None, dt.date(2027, 3, 1)) is None


# ── reveal(field, reason) ──────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_reveal_needs_a_field_and_a_reason_and_audits_each() -> None:
    conn = ScriptedConn().on("emergency_contact_enc FROM crm.traveler", traveler_row())
    ctx = tenant_ctx(conn)
    got = await reveal(ctx, FakeVault(), T1, "email", "contact_client")
    assert got == {"traveler_id": str(T1), "field": "email", "value": "anna@example.test"}
    assert (await reveal(ctx, FakeVault(), T1, "passport_no", "prepare_booking"))["value"] is None
    assert [(e.action, e.fields, e.reason) for e in ctx.audit_events] == [
        ("reveal", ("email",), "contact_client"),
        ("reveal", ("passport_no",), "prepare_booking"),
    ]
    with pytest.raises(Invalid) as err:
        await reveal(ctx, FakeVault(), T1, "email", "curious")
    assert err.value.code == "reason_required"
    with pytest.raises(Invalid):
        await reveal(ctx, FakeVault(), T1, "display_name_enc", "other")
    with pytest.raises(NotFound):
        await reveal(tenant_ctx(), FakeVault(), T1, "email", "other")


@pytest.mark.asyncio
async def test_reveal_health_codes() -> None:
    hid = uuid.uuid4()
    codes = {"dietary": ["vegan"], "medical": [], "mobility": []}
    conn = (
        ScriptedConn()
        .on("emergency_contact_enc FROM crm.traveler", traveler_row())
        .on("FROM crm.traveler_health", {"health_id": hid, "health_flags_enc": enc("x", "y", json.dumps(codes))})
        .on("purpose = 'health_data' ORDER BY", True)
    )
    assert (await reveal(tenant_ctx(conn), FakeVault(), T1, "health", "fill_forms"))["value"] == codes
    none = ScriptedConn().on("emergency_contact_enc FROM crm.traveler", traveler_row())
    assert (await reveal(tenant_ctx(none), FakeVault(), T1, "health", "other"))["value"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("latest", [False, None])
async def test_reveal_health_needs_the_latest_consent(latest: bool | None) -> None:
    """Design §4.3: health codes are read with consent. A withdrawn (or missing) consent keeps them sealed: 409 and a
    denied event, which tenant_tx() commits even though the request fails."""
    conn = (
        ScriptedConn()
        .on("emergency_contact_enc FROM crm.traveler", traveler_row())
        .on("FROM crm.traveler_health", {"health_id": uuid.uuid4(), "health_flags_enc": enc("x", "y", "{}")})
        .on("purpose = 'health_data' ORDER BY", latest)
    )
    ctx = tenant_ctx(conn)
    with pytest.raises(Conflict) as err:
        await reveal(ctx, FakeVault(), T1, "health", "fill_forms")
    assert err.value.code == "health_consent_withdrawn"
    assert [(e.action, e.outcome, e.reason) for e in ctx.audit_events] == [("reveal", "denied", "consent_withdrawn")]


# ── traveller_self_reveal(session) ─────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_self_reveal_own_record_only_with_masked_passport() -> None:
    row = traveler_row(passport_no_enc=enc("traveler", "passport_no_enc", "C1234567"))
    conn = ScriptedConn().on("JOIN crm.trip_traveler tt ON tt.traveler_id = tr.traveler_id", row)
    ctx = system_ctx(conn)
    session = TravellerSession(tenant_id=TENANT, trip_id=TRIP, traveler_id=T1, access="companion")
    got = await traveller_self_reveal(ctx, FakeVault(), session)
    assert got["display_name"] == "Anna Martin" and got["passport_no_masked"] == "•••• 567"
    assert "passport_no" not in got and "emergency_contact" not in got and "health" not in got
    [event] = ctx.audit_events
    assert (event.actor_kind, event.actor_ref, event.action) == ("traveler", f"traveler:{T1}", "self_view")
    assert conn.sql("JOIN crm.trip_traveler") == [(T1, TRIP)]
    for bad in (
        TravellerSession(tenant_id=uuid.uuid4(), trip_id=TRIP, traveler_id=T1, access="lead"),
        TravellerSession(tenant_id=TENANT, trip_id=TRIP, traveler_id=T1, access="agent"),
    ):
        with pytest.raises(Forbidden):
            await traveller_self_reveal(ctx, FakeVault(), bad)
    with pytest.raises(NotFound):  # not on that trip (or not this agency's traveller)
        await traveller_self_reveal(system_ctx(), FakeVault(), session)
    nopass = ScriptedConn().on("JOIN crm.trip_traveler tt", traveler_row())
    assert (await traveller_self_reveal(system_ctx(nopass), FakeVault(), session))["passport_no_masked"] is None


# ── A-06 directory ─────────────────────────────────────────────────────────────────────────────────
def list_row(tid: uuid.UUID, name: str) -> dict[str, Any]:
    return {
        "traveler_id": tid,
        "display_name_enc": enc("traveler", "display_name_enc", name),
        "owner_member_id": MEMBER,
        "lead_source": "website_form",
        "archived_at": None,
        "created_at": NOW,
        "has_email": True,
        "has_phone": False,
        "trips": 2,
    }


@pytest.mark.asyncio
async def test_directory_lists_names_only_and_writes_one_list_view() -> None:
    conn = ScriptedConn().on("tr.name_token_bidx @>", [list_row(T1, "Anna Martin"), list_row(T2, "Tom Martin")])
    ctx = tenant_ctx(conn)
    got = await service.directory(ctx, FakeVault())
    assert [i["display_name"] for i in got["items"]] == ["Anna Martin", "Tom Martin"]
    assert set(got["items"][0]) == {
        "traveler_id",
        "display_name",
        "owner_member_id",
        "is_mine",
        "lead_source",
        "archived",
        "has_email",
        "has_phone",
        "trips",
        "created_at",
    }
    [event] = ctx.audit_events
    assert (event.action, event.fields, event.reason, event.object_ids) == (
        "list_view",
        ("display_name",),
        "client_list",
        (str(T1), str(T2)),
    )
    assert got["has_more"] is False and got["filter"]["search"] == "none"


@pytest.mark.asyncio
async def test_directory_exact_search_by_email_phone_and_name() -> None:
    rows = [list_row(T1, "Anna Martin"), list_row(T2, "Annabel Smith")]
    conn = ScriptedConn().on("tr.name_token_bidx @>", rows)
    ctx = tenant_ctx(conn, role="agent")
    got = await service.directory(ctx, FakeVault(), q="anna", owner="me")
    # The 4-byte token index matched both; only the confirmed name is shown.
    assert [i["display_name"] for i in got["items"]] == ["Anna Martin"]
    args = conn.calls[-1][1]
    assert args[1] == MEMBER and args[4] == [b"tok1"] and args[2] is None
    assert ctx.audit_events[-1].reason == "client_search" and ctx.audit_events[-1].object_ids == (str(T1),)
    await service.directory(ctx, FakeVault(), q="Anna@Example.test")
    assert conn.calls[-1][1][2] == b"e:anna@example.test"
    await service.directory(ctx, FakeVault(), q="+84 912 345 678")
    assert conn.calls[-1][1][3] == b"p:+84 912 345 678"
    for bad_q, bad_page in (("!!!", 1), (None, 0), (None, 999)):
        with pytest.raises(Invalid):
            await service.directory(ctx, FakeVault(), q=bad_q, page=bad_page)
    nothing = tenant_ctx(ScriptedConn())
    assert (await service.directory(nothing, FakeVault(), archived=True))["items"] == [] and nothing.audit_events == []
    many = ScriptedConn().on("tr.name_token_bidx @>", [list_row(uuid.uuid4(), "A B")] * (service.PAGE_SIZE + 1))
    assert (await service.directory(tenant_ctx(many), FakeVault(), page=2))["has_more"] is True


# ── A-07 profile ───────────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_profile_masks_f1_and_audits_one_view() -> None:
    doc = uuid.uuid4()
    conn = (
        ScriptedConn()
        .on("emergency_contact_enc, profile_token_enc", traveler_row(dob_enc=b"x" * 30))
        .on("FROM crm.traveler_profile WHERE traveler_token", profile_row())
        .on("FROM crm.trip_party WHERE traveler_token", [{"trip_id": TRIP, "age_band_at_departure": "35_44"}])
        .on(
            "FROM crm.traveler_relation r",
            [
                {
                    "relation_id": uuid.uuid4(),
                    "kind": "spouse",
                    "to_traveler_id": T2,
                    "display_name_enc": enc("traveler", "display_name_enc", "Tom Martin"),
                    "trips_together": 2,
                }
            ],
        )
        .on(
            "FROM crm.trip_traveler tt JOIN crm.trip t",
            [
                {
                    "trip_id": TRIP,
                    "title": "Sapa trek",
                    "stage": "inquiry",
                    "waiting_on": "agency",
                    "month": dt.date(2027, 3, 1),
                    "start_date": None,
                    "role": "lead",
                }
            ],
        )
        .on(
            "FROM crm.document WHERE traveler_id",
            [
                {
                    "document_id": doc,
                    "trip_id": None,
                    "kind": "passport_scan",
                    "size_bytes": 10,
                    "content_type": "image/jpeg",
                    "filename_enc": enc("document", "filename_enc", "anna-passport.jpg"),
                    "visible_to_travelers": False,
                    "write_only": True,
                    "created_at": NOW,
                }
            ],
        )
        .on("FROM crm.consent", [{"purpose": "marketing", "value": True, "at": NOW, "source": "form"}])
        .on("FROM crm.traveler_health", True)
    )
    ctx = tenant_ctx(conn)
    got = await service.profile(ctx, FakeVault(), T1)
    t = got["traveler"]
    assert t["display_name"] == "Anna Martin" and t["filled"]["email"] is True and t["filled"]["phone"] is False
    assert t["filled"]["dob"] is True and "anna@example.test" not in json.dumps(got)
    assert t["has_health"] is True and got["profile"]["dietary_flags"] == ["vegetarian"]
    assert got["relations"][0]["display_name"] == "Tom Martin" and got["trips"][0]["age_band_at_departure"] == "35_44"
    assert got["documents"][0]["filename"] == "anna-passport.jpg" and got["consent"]["marketing"]["value"] is True
    [event] = ctx.audit_events
    assert (event.action, event.fields) == ("view", ("display_name", "relation_display_name", "document_filename"))
    # Design §4.4: the other identities shown (a related traveller's name, a file name) are named by id.
    assert event.object_ids == (str(T2), str(doc))
    with pytest.raises(NotFound):
        await service.profile(tenant_ctx(), FakeVault(), T1)
    bare = ScriptedConn().on("emergency_contact_enc, profile_token_enc", traveler_row(profile_token_enc=None))
    got = await service.profile(tenant_ctx(bare), FakeVault(), T1)
    assert got["profile"] is None and got["relations"] == [] and got["traveler"]["has_health"] is False


# ── create / edit ──────────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_create_traveler_encrypts_dedupes_and_allows_no_email() -> None:
    conn = ScriptedConn()
    ctx = tenant_ctx(conn)
    got = await service.create_traveler(ctx, FakeVault(), {"display_name": "Lily Martin"})
    [args] = conn.sql("INSERT INTO crm.traveler (")
    assert args[1] == enc("traveler", "display_name_enc", "Lily Martin") and args[6] is None and args[7] is None
    assert args[13] == "agent_created" and args[12] == MEMBER and got["display_name"] == "Lily Martin"
    assert ctx.audit_events[0].fields == ("display_name",)
    dup = ScriptedConn().on("WHERE email_bidx = $1", T2)
    with pytest.raises(Conflict) as err:
        await service.create_traveler(tenant_ctx(dup), FakeVault(), {"display_name": "Anna", "email": "a@b.test"})
    assert err.value.extra == {"traveler_id": str(T2)}
    for bad in ({"display_name": "A", "preferred_channel": "fax"}, {"display_name": "A", "lead_source": "Web Form"}):
        with pytest.raises(Invalid):
            await service.create_traveler(tenant_ctx(), FakeVault(), bad)


@pytest.mark.asyncio
async def test_update_traveler_fields_indexes_and_dob_recompute(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []

    async def recompute(ctx: Any, vault: Any, traveler_id: Any, dob: Any) -> int:
        calls.append(dob)
        return 1

    monkeypatch.setattr(age_bands, "recompute_for_traveler", recompute)
    row = traveler_row(dob_enc=enc("traveler", "dob_enc", "1990-01-01"))
    conn = ScriptedConn().on("emergency_contact_enc, profile_token_enc", row)
    ctx = tenant_ctx(conn)
    got = await service.update_traveler(
        ctx,
        FakeVault(),
        T1,
        {
            "display_name": "Anna M. Martin",
            "email": None,
            "phone": "+61400000000",
            "dob": "1991-02-02",
            "preferred_channel": "whatsapp",
            "passport_no": "C1234567",
        },
    )
    assert got["updated"] == ["display_name", "dob", "email", "passport_no", "phone", "preferred_channel"]
    assert got["age_bands_changed"] == 1 and calls == [dt.date(1991, 2, 2)]
    [(sql, args)] = [c for c in conn.calls if c[0].startswith("UPDATE crm.traveler SET")]
    assert "email_bidx = " in sql and "name_token_bidx = " in sql and "phone_bidx" in sql and None in args
    # The same DOB again: no recompute (no F5 noise); an unrelated edit: none either.
    calls.clear()
    await service.update_traveler(tenant_ctx(conn), FakeVault(), T1, {"dob": "1990-01-01"})
    await service.update_traveler(tenant_ctx(conn), FakeVault(), T1, {"nationality": "AU"})
    assert calls == []
    for bad in ({}, {"traveler_id": "x"}, {"preferred_channel": "fax"}):
        with pytest.raises(Invalid):
            await service.update_traveler(tenant_ctx(conn), FakeVault(), T1, bad)
    with pytest.raises(NotFound):
        await service.update_traveler(tenant_ctx(), FakeVault(), T1, {"nationality": "AU"})
    dup = ScriptedConn().on("emergency_contact_enc, profile_token_enc", row).on("WHERE phone_bidx = $1", T2)
    with pytest.raises(Conflict):
        await service.update_traveler(tenant_ctx(dup), FakeVault(), T1, {"phone": "+61400000000"})


@pytest.mark.asyncio
async def test_update_profile_creates_the_f2_row_when_missing() -> None:
    conn = (
        ScriptedConn()
        .on("emergency_contact_enc, profile_token_enc", traveler_row(profile_token_enc=None))
        .on("FROM crm.traveler_profile WHERE traveler_token", profile_row(fitness_band="4_6h"))
    )
    ctx = tenant_ctx(conn)
    got = await service.update_profile(ctx, FakeVault(), T1, {"fitness_band": "4_6h", "interests": ["food"]})
    assert got["profile"]["fitness_band"] == "4_6h"
    assert len(conn.sql("INSERT INTO crm.traveler_profile")) == 1 and conn.sql("SET profile_token_enc")
    assert ctx.audit_events[0].object_kind == "traveler_profile" and ctx.audit_events[0].object_id == str(T1)
    with pytest.raises(Invalid):
        await service.update_profile(ctx, FakeVault(), T1, {})
    with pytest.raises(NotFound):
        await service.update_profile(tenant_ctx(), FakeVault(), T1, {"pace": "relaxed"})


@pytest.mark.asyncio
async def test_set_health_needs_consent_and_mirrors_f2() -> None:
    cid = uuid.uuid4()
    conn = ScriptedConn().on("emergency_contact_enc, profile_token_enc", traveler_row()).on("RETURNING consent_id", cid)
    ctx = tenant_ctx(conn)
    got = await service.set_health(
        ctx, FakeVault(), T1, dietary=["vegan"], mobility=["no_stairs"], medical=["asthma"], consent_recorded=True
    )
    assert got["dietary_flags"] == ["vegan"] and conn.sql("INSERT INTO crm.traveler_health")[0][3] == cid
    assert conn.sql("SET dietary_flags = $2") == [(TOKEN, ["vegan"], ["no_stairs"])]
    assert ctx.audit_events[0].fields == ("health",)
    # An existing row and a recorded consent: update.
    hid = uuid.uuid4()
    again = (
        ScriptedConn()
        .on("emergency_contact_enc, profile_token_enc", traveler_row())
        .on("purpose = 'health_data' ORDER BY", {"consent_id": cid, "value": True})
        .on("FROM crm.traveler_health WHERE traveler_id", hid)
    )
    await service.set_health(
        tenant_ctx(again), FakeVault(), T1, dietary=[], mobility=[], medical=[], consent_recorded=False
    )
    assert again.sql("UPDATE crm.traveler_health")[0][0] == hid
    for consent in (None, {"consent_id": cid, "value": False}):
        refused = (
            ScriptedConn()
            .on("emergency_contact_enc, profile_token_enc", traveler_row())
            .on("purpose = 'health_data' ORDER BY", consent)
        )
        with pytest.raises(Invalid) as err:
            await service.set_health(
                tenant_ctx(refused), FakeVault(), T1, dietary=[], mobility=[], medical=[], consent_recorded=False
            )
        assert err.value.code == "consent_required"
    with pytest.raises(NotFound):
        await service.set_health(
            tenant_ctx(), FakeVault(), T1, dietary=[], mobility=[], medical=[], consent_recorded=True
        )


# ── relationships (R7) ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_relations_add_and_remove_keep_the_referrer_in_step() -> None:
    rel = uuid.uuid4()
    conn = ScriptedConn().on("SELECT 1 FROM crm.traveler", 1).on("RETURNING relation_id", rel)
    ctx = tenant_ctx(conn)
    assert (await service.add_relation(ctx, T1, T2, "referrer"))["relation_id"] == str(rel)
    assert conn.sql("SET referrer_traveler_id = $2") == [(T1, T2)]
    await service.add_relation(ctx, T1, T2, "referred")
    assert conn.sql("SET referrer_traveler_id = $2")[-1] == (T2, T1)
    await service.add_relation(ctx, T1, T2, "spouse")
    assert len(conn.sql("SET referrer_traveler_id = $2")) == 2
    for args in ((T1, T2, "cousin"), (T1, T1, "friend")):
        with pytest.raises(Invalid):
            await service.add_relation(ctx, *args)
    with pytest.raises(NotFound):
        await service.add_relation(tenant_ctx(), T1, T2, "friend")
    clash = (
        ScriptedConn()
        .on("SELECT 1 FROM crm.traveler", 1)
        .on("RETURNING relation_id", asyncpg.UniqueViolationError("dup"))
    )
    with pytest.raises(Conflict):
        await service.add_relation(tenant_ctx(clash), T1, T2, "friend")
    for kind, cleared in (("referrer", (T1, T2)), ("referred", (T2, T1)), ("spouse", None)):
        rm = ScriptedConn().on(
            "FROM crm.traveler_relation WHERE relation_id",
            {"relation_id": rel, "from_traveler_id": T1, "to_traveler_id": T2, "kind": kind},
        )
        rctx = tenant_ctx(rm)
        assert (await service.remove_relation(rctx, T1, rel))["removed"] is True
        assert rm.sql("SET deleted_at = now()")[0][1] == f"member:{MEMBER}"
        assert rm.sql("SET referrer_traveler_id = NULL") == ([cleared] if cleared else [])
    with pytest.raises(NotFound):
        await service.remove_relation(tenant_ctx(), T1, rel)


# ── the party of a trip ────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_add_companion_without_email_writes_both_twins() -> None:
    conn = (
        ScriptedConn()
        .on("FROM crm.trip WHERE trip_id = $1", {"trip_id": TRIP, "start_date": dt.date(2027, 3, 12)})
        .on("emergency_contact_enc, profile_token_enc", traveler_row(email_enc=None, dob_enc=None))
        .on("RETURNING trip_party_id", PARTY)
    )
    ctx = tenant_ctx(conn)
    got = await service.add_to_trip(ctx, FakeVault(), TRIP, new={"display_name": "Lily Martin"})
    assert got["role"] == "companion" and got["age_band_at_departure"] is None
    assert len(conn.sql("INSERT INTO crm.trip_traveler")) == 1 and conn.sql("INSERT INTO crm.trip_party") == [
        (TRIP, TOKEN)
    ]
    assert [e.action for e in ctx.audit_events] == ["create", "party_add"]


@pytest.mark.asyncio
async def test_add_existing_client_computes_the_band_and_refuses_twice() -> None:
    row = traveler_row(dob_enc=enc("traveler", "dob_enc", "1990-01-01"))
    conn = (
        ScriptedConn()
        .on("FROM crm.trip WHERE trip_id = $1", {"trip_id": TRIP, "start_date": dt.date(2027, 3, 12)})
        .on("emergency_contact_enc, profile_token_enc", row)
        .on("RETURNING trip_party_id", PARTY)
    )
    ctx = tenant_ctx(conn)
    got = await service.add_to_trip(ctx, FakeVault(), TRIP, traveler_id=T1)
    assert got["age_band_at_departure"] == "35_44" and ctx.audit_events[-1].fields == ("dob",)
    with pytest.raises(Conflict):
        await service.add_to_trip(
            tenant_ctx(conn.on("FROM crm.trip_traveler WHERE trip_id", {"trip_traveler_id": 1, "role": "lead"})),
            FakeVault(),
            TRIP,
            traveler_id=T1,
        )
    with pytest.raises(NotFound):
        await service.add_to_trip(tenant_ctx(), FakeVault(), TRIP, traveler_id=T1)
    trip_only = ScriptedConn().on("FROM crm.trip WHERE trip_id = $1", {"trip_id": TRIP, "start_date": None})
    with pytest.raises(Invalid):
        await service.add_to_trip(tenant_ctx(trip_only), FakeVault(), TRIP)
    with pytest.raises(NotFound):
        await service.add_to_trip(tenant_ctx(trip_only), FakeVault(), TRIP, traveler_id=T1)


@pytest.mark.asyncio
async def test_remove_from_trip_keeps_the_lead() -> None:
    conn = (
        ScriptedConn()
        .on("FROM crm.trip_traveler WHERE trip_id", {"trip_traveler_id": PARTY, "role": "companion"})
        .on("emergency_contact_enc, profile_token_enc", traveler_row())
    )
    ctx = tenant_ctx(conn)
    assert (await service.remove_from_trip(ctx, FakeVault(), TRIP, T1))["removed"] is True
    assert conn.sql("DELETE FROM crm.trip_party") == [(TRIP, TOKEN)] and conn.sql("DELETE FROM crm.trip_traveler")
    lead = ScriptedConn().on("FROM crm.trip_traveler WHERE trip_id", {"trip_traveler_id": PARTY, "role": "lead"})
    with pytest.raises(Conflict):
        await service.remove_from_trip(tenant_ctx(lead), FakeVault(), TRIP, T1)
    with pytest.raises(NotFound):
        await service.remove_from_trip(tenant_ctx(), FakeVault(), TRIP, T1)


# ── trip dates (AA-937 on app.pipeline.update_trip): only a changed start date recomputes ────────────
@pytest.mark.asyncio
async def test_trip_dates_recompute_only_when_the_start_date_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import pipeline
    from tests.unit.test_pipeline import trip_row

    seen: list[Any] = []

    async def recompute(ctx: Any, vault: Any, trip_id: Any, start: Any) -> int:
        seen.append(start)
        return 0

    monkeypatch.setattr(age_bands, "recompute_for_trip", recompute)
    start = dt.date(2027, 3, 12)
    conn = (
        ScriptedConn()
        .on("SELECT start_date, end_date FROM crm.trip", {"start_date": None, "end_date": None})
        .on("UPDATE crm.trip SET", trip_row(start_date=start))
    )
    ctx = tenant_ctx(conn)
    got = await pipeline.update_trip(ctx, TRIP, {"start_date": start, "end_date": dt.date(2027, 3, 20)}, FakeVault())
    assert got["start_date"] == "2027-03-12" and seen == [start]
    assert ctx.audit_events[0].fields == ("end_date", "start_date")
    # An unrelated edit (title, end date, the same start date) does not recompute (card CRM-09 E test).
    same = (
        ScriptedConn()
        .on("SELECT start_date, end_date FROM crm.trip", {"start_date": start, "end_date": None})
        .on("UPDATE crm.trip SET", trip_row())
    )
    await pipeline.update_trip(tenant_ctx(same), TRIP, {"title": "Sapa"})
    await pipeline.update_trip(tenant_ctx(same), TRIP, {"start_date": start, "end_date": None})
    assert seen == [start]
    with pytest.raises(RuntimeError):
        await pipeline.update_trip(tenant_ctx(conn), TRIP, {"start_date": start})
    for changes, code in (
        ({"start_date": "2027-03-12"}, "invalid_start_date"),
        ({"start_date": start, "end_date": dt.date(2027, 3, 1)}, "invalid_dates"),
    ):
        with pytest.raises(Invalid) as err:
            await pipeline.update_trip(tenant_ctx(conn), TRIP, changes, FakeVault())
        assert err.value.code == code
    with pytest.raises(NotFound):
        await pipeline.update_trip(tenant_ctx(), TRIP, {"end_date": start})
