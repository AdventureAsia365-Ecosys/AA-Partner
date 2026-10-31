"""AA-962 (card CRM-31 "merge screen", design v3 R6): the duplicate candidates page and the merge on a scripted
connection: the blockers and the legal hold, the trip party moves, the relationships re-made, the health codes, the F1
fill and the import refs the kept client takes over. The SQL is covered by
tests/integration/test_onboarding_import_api.py. Invented data only."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

import asyncpg
import pytest

from app.errors import Conflict, Invalid, NotFound
from app.travellers import merge
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import MEMBER, FakeVault, enc, tenant_ctx

KEEP = uuid.UUID("00000000-0000-4000-8000-000000000a01")
GO = uuid.UUID("00000000-0000-4000-8000-000000000a02")
FRIEND = uuid.UUID("00000000-0000-4000-8000-000000000a03")
SHARED = uuid.UUID("00000000-0000-4000-8000-000000000a04")
BUSY = uuid.UUID("00000000-0000-4000-8000-000000000a05")
NEW_REL = uuid.UUID("00000000-0000-4000-8000-000000000a06")
TRIP_BOTH = uuid.UUID("00000000-0000-4000-8000-000000000b01")
TRIP_OTHER = uuid.UUID("00000000-0000-4000-8000-000000000b02")
TOKEN = uuid.UUID("00000000-0000-4000-8000-000000000c01")
HEALTH = uuid.UUID("00000000-0000-4000-8000-000000000d01")
CONSENT = uuid.UUID("00000000-0000-4000-8000-000000000d02")
CREATED = dt.datetime(2026, 1, 2, 3, 4, tzinfo=dt.UTC)


def traveler(traveler_id: uuid.UUID, **over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {column: None for column in merge.FILL_COLUMNS}
    row.update(
        traveler_id=traveler_id,
        display_name_enc=enc("traveler", "display_name_enc", "Kim Lake"),
        email_bidx=None,
        phone_bidx=None,
        profile_token_enc=None,
        import_ref_bidx=None,
        import_ref_alias_bidx=None,
    )
    row.update(over)
    return row


# ── candidates ─────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_candidates_show_names_and_flags_and_audit_the_ids_once() -> None:
    pair = {
        "a_id": KEEP,
        "b_id": GO,
        "a_name": enc("traveler", "display_name_enc", "Kim Lake"),
        "b_name": enc("traveler", "display_name_enc", "Kim  Lake"),
        "same_name": True,
        "same_email": False,
        "same_phone": True,
        "a_created": CREATED,
        "b_created": CREATED,
        "a_source": "import",
        "b_source": "website_form",
        "a_trips": 2,
        "b_trips": 0,
    }
    conn = ScriptedConn([("AS a_id", [pair]), ("SELECT count(*) FROM crm.traveler a", 3)])
    ctx = tenant_ctx(conn)
    out = await merge.candidates(ctx, FakeVault())
    assert out["total"] == 3 and out["has_more"] is True
    (shown,) = out["pairs"]
    assert shown["matched_on"] == ["name", "phone"]
    assert [c["display_name"] for c in shown["clients"]] == ["Kim Lake", "Kim  Lake"]
    assert shown["clients"][0]["created_at"] == CREATED.isoformat() and shown["clients"][1]["trips"] == 0
    (event,) = ctx.audit_events
    assert event.action == "list_view" and event.object_ids == (str(KEEP), str(GO))
    assert await merge.candidate_count(ctx) == 3


@pytest.mark.asyncio
async def test_no_candidates_no_audit() -> None:
    ctx = tenant_ctx(ScriptedConn())
    assert await merge.candidates(ctx, FakeVault()) == {"pairs": [], "total": 0, "has_more": False}
    assert ctx.audit_events == [] and await merge.candidate_count(ctx) == 0


# ── refusals ───────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_the_same_client_twice_is_refused() -> None:
    with pytest.raises(Invalid) as err:
        await merge.merge(tenant_ctx(), FakeVault(), KEEP, KEEP)
    assert err.value.code == "same_traveler"


@pytest.mark.asyncio
async def test_a_missing_client_is_not_found() -> None:
    conn = ScriptedConn([("FOR UPDATE", lambda tid: traveler(tid) if tid == KEEP else None)])
    with pytest.raises(NotFound):
        await merge.merge(tenant_ctx(conn), FakeVault(), KEEP, GO)


@pytest.mark.asyncio
async def test_a_legal_hold_on_either_client_refuses_the_merge() -> None:
    conn = ScriptedConn([("FOR UPDATE", traveler), ("crm.legal_hold_blocks", lambda tid: tid == GO)])
    ctx = tenant_ctx(conn)
    with pytest.raises(Conflict) as err:
        await merge.merge(ctx, FakeVault(), KEEP, GO)
    assert err.value.code == "legal_hold"
    assert [(e.outcome, e.object_id, e.reason) for e in ctx.audit_events] == [("denied", str(GO), "legal_hold")]


@pytest.mark.asyncio
@pytest.mark.parametrize(("keep_blockers", "swap"), [([], True), (["signatures"], False)])
async def test_blockers_on_the_client_that_goes_refuse_and_say_whether_a_swap_works(
    keep_blockers: list[str], swap: bool
) -> None:
    blockers = {GO: ["client_links", "portal_access"], KEEP: keep_blockers}
    conn = ScriptedConn([("FOR UPDATE", traveler), ("array_remove(ARRAY[", lambda tid: blockers[tid])])
    with pytest.raises(Conflict) as err:
        await merge.merge(tenant_ctx(conn), FakeVault(), KEEP, GO)
    assert err.value.code == "merge_blocked"
    assert err.value.extra == {"blockers": ["client_links", "portal_access"], "swap_possible": swap}
    assert not conn.sql("UPDATE crm.") and not conn.sql("DELETE")


# ── the merge ──────────────────────────────────────────────────────────────────────────────────────
def merge_conn(*, keep_health: bool = False, consent: bool | None = True) -> ScriptedConn:
    rows = {
        KEEP: traveler(KEEP, import_ref_bidx=b"ref-1", dob_enc=None),
        GO: traveler(
            GO,
            email_enc=enc("traveler", "email_enc", "kim@example.test"),
            email_bidx=b"e:kim",
            dob_enc=enc("traveler", "dob_enc", "1988-04-05"),
            passport_no_enc=enc("traveler", "passport_no_enc", "X1234567"),
            profile_token_enc=enc("traveler", "profile_token_enc", str(TOKEN)),
            import_ref_bidx=b"ref-2",
            import_ref_alias_bidx=[b"ref-1", b"ref-3"],
        ),
    }
    rows[KEEP]["passport_no_enc"] = enc("traveler", "passport_no_enc", "K7654321")  # the kept value wins
    party = {
        KEEP: [{"trip_traveler_id": uuid.uuid4(), "trip_id": TRIP_BOTH, "role": "companion"}],
        GO: [
            {"trip_traveler_id": uuid.uuid4(), "trip_id": TRIP_BOTH, "role": "lead"},
            {"trip_traveler_id": uuid.uuid4(), "trip_id": TRIP_OTHER, "role": "companion"},
        ],
    }
    relations = [
        {"relation_id": uuid.uuid4(), "to_traveler_id": KEEP, "kind": "spouse"},
        {"relation_id": uuid.uuid4(), "to_traveler_id": SHARED, "kind": "friend"},
        {"relation_id": uuid.uuid4(), "to_traveler_id": BUSY, "kind": "referred"},
        {"relation_id": uuid.uuid4(), "to_traveler_id": FRIEND, "kind": "referrer"},
        {"relation_id": uuid.uuid4(), "to_traveler_id": NEW_REL, "kind": "referred"},
    ]
    health = {
        "health_id": HEALTH,
        "health_flags_enc": enc("traveler_health", "health_flags_enc", json.dumps({"dietary": ["vegan"]})),
    }

    def insert_relation(a: uuid.UUID, b: uuid.UUID, kind: str) -> Any:
        return asyncpg.UniqueViolationError("referrer exists") if b == BUSY else None

    def dob(tid: uuid.UUID) -> Any:
        return enc("traveler", "dob_enc", "1988-04-05") if tid == KEEP else None

    return ScriptedConn(
        [
            ("FOR UPDATE", lambda tid: rows[tid]),
            ("crm.legal_hold_blocks", False),
            ("array_remove(ARRAY[", []),
            ("SELECT trip_traveler_id, trip_id, role", lambda tid: party[tid]),
            ("SELECT to_traveler_id FROM crm.traveler_relation", [{"to_traveler_id": SHARED}]),
            ("SELECT relation_id, to_traveler_id, kind", relations),
            ("INSERT INTO crm.traveler_relation", insert_relation),
            ("FROM crm.traveler_health", lambda tid: (keep_health and tid == KEEP) or (tid == GO and health) or None),
            ("purpose = 'health_data'", None if consent is None else {"consent_id": CONSENT, "value": consent}),
            ("SELECT dob_enc FROM crm.traveler", dob),
        ]
    )


@pytest.fixture
def lifecycle(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    calls: list[tuple[str, Any]] = []

    async def days(_ctx: Any) -> int:
        return 14

    async def to_trash(_ctx: Any, kind: str, object_id: uuid.UUID) -> dict[str, Any]:
        calls.append(("trash", (kind, object_id)))
        return {"purge_after": "2026-02-01T00:00:00+00:00"}

    async def bands(_ctx: Any, _vault: Any, traveler_id: uuid.UUID, day: dt.date | None) -> int:
        calls.append(("bands", (traveler_id, day)))
        return 1

    monkeypatch.setattr(merge, "trash_days", days)
    monkeypatch.setattr(merge.trash, "move_to_trash", to_trash)
    monkeypatch.setattr(merge.age_bands, "recompute_for_traveler", bands)
    return calls


@pytest.mark.asyncio
async def test_merge_moves_everything_to_the_kept_client(lifecycle: list[tuple[str, Any]]) -> None:
    conn = merge_conn()
    ctx = tenant_ctx(conn)
    out = await merge.merge(ctx, FakeVault(), KEEP, GO)
    assert out == {
        "traveler_id": str(KEEP),
        "merged_traveler_id": str(GO),
        "trips_moved": 2,
        "relations_remade": 2,
        "health_moved": True,
        "fields_filled": ["email", "dob"],
        "purge_after": "2026-02-01T00:00:00+00:00",
    }
    # the kept client had no profile: one is made and its pointer sealed
    (empty,) = conn.sql("INSERT INTO crm.traveler_profile (traveler_token)")
    keep_token = empty[0]
    assert conn.sql("UPDATE crm.traveler SET profile_token_enc")[0][0] == KEEP
    # a trip both were on: the other's party rows go and the kept client takes the lead; the other trip moves
    assert conn.sql("DELETE FROM crm.trip_party") == [(TRIP_BOTH, TOKEN)]
    assert conn.sql("SET role = 'lead' WHERE trip_id = $1 AND traveler_id") == [(TRIP_BOTH, KEEP)]
    assert conn.sql("SET role = 'lead' WHERE trip_id = $1 AND traveler_token") == [(TRIP_BOTH, keep_token)]
    assert conn.sql("UPDATE crm.trip_party SET traveler_token") == [(TRIP_OTHER, TOKEN, keep_token)]
    assert len(conn.sql("WHERE traveler_id = $1 AND client_link_id IS NULL")) == 1
    # every relation of the other client is trashed; only new targets are re-made (a referrer conflict is skipped)
    assert len(conn.sql("UPDATE crm.traveler_relation SET deleted_at")) == 5
    assert conn.sql("INSERT INTO crm.traveler_relation") == [
        (KEEP, BUSY, "referred"),
        (KEEP, FRIEND, "referrer"),
        (KEEP, NEW_REL, "referred"),
    ]
    assert conn.sql("SET referrer_traveler_id = $2 WHERE traveler_id = $1 AND") == [(KEEP, FRIEND), (NEW_REL, KEEP)]
    # consents copied; health codes sealed again for the kept client and mirrored on its profile
    assert conn.sql("INSERT INTO crm.consent") == [(GO, KEEP)]
    (health,) = conn.sql("INSERT INTO crm.traveler_health")
    assert health[1] == KEEP and health[3] == CONSENT
    assert conn.sql("UPDATE crm.traveler_profile SET dietary_flags") == [(keep_token, ["vegan"], [])]
    # F1 fill (passport kept as it was) and the import refs: own ref-1 kept, ref-2 and ref-3 become aliases
    (fill,) = conn.sql("UPDATE crm.traveler SET email_enc")
    assert fill == (
        KEEP,
        enc("traveler", "email_enc", "kim@example.test"),
        b"e:kim",
        enc("traveler", "dob_enc", "1988-04-05"),
        b"ref-1",
        [b"ref-2", b"ref-3"],
    )
    assert lifecycle == [("bands", (KEEP, dt.date(1988, 4, 5))), ("trash", ("traveler", GO))]
    (event,) = ctx.audit_events
    assert event.action == "merge" and event.fields == ("email", "dob") and event.object_ids == (str(GO),)
    assert conn.sql("UPDATE crm.traveler_relation SET deleted_at")[0][1:] == (f"member:{MEMBER}", 14)


@pytest.mark.asyncio
@pytest.mark.parametrize(("keep_health", "consent"), [(True, True), (False, None), (False, False)])
async def test_health_stays_when_the_kept_client_has_codes_or_no_consent(
    lifecycle: list[tuple[str, Any]], keep_health: bool, consent: bool | None
) -> None:
    conn = merge_conn(keep_health=keep_health, consent=consent)
    out = await merge.merge(tenant_ctx(conn), FakeVault(), KEEP, GO)
    assert out["health_moved"] is False and not conn.sql("INSERT INTO crm.traveler_health")


@pytest.mark.asyncio
async def test_a_bare_merge_writes_no_fill_and_recomputes_no_bands(lifecycle: list[tuple[str, Any]]) -> None:
    token = enc("traveler", "profile_token_enc", str(TOKEN))
    rows = {KEEP: traveler(KEEP, profile_token_enc=token), GO: traveler(GO, profile_token_enc=token)}
    conn = ScriptedConn([("FOR UPDATE", lambda tid: rows[tid])])
    out = await merge.merge(tenant_ctx(conn), FakeVault(), KEEP, GO)
    assert out["trips_moved"] == 0 and out["fields_filled"] == [] and out["relations_remade"] == 0
    assert not conn.sql("UPDATE crm.traveler SET email_enc") and not conn.sql("INSERT INTO crm.traveler_profile")
    assert lifecycle == [("trash", ("traveler", GO))]


def test_merged_refs() -> None:
    def refs(own: bytes | None, aliases: list[bytes] | None) -> dict[str, Any]:
        return {"import_ref_bidx": own, "import_ref_alias_bidx": aliases}

    assert merge._merged_refs(refs(None, None), refs(None, None)) is None
    assert merge._merged_refs(refs(b"a", [b"b"]), refs(b"b", [b"a"])) is None
    assert merge._merged_refs(refs(None, None), refs(b"x", [b"y"])) == (b"x", [b"y"])
    assert merge._merged_refs(refs(b"a", None), refs(None, [b"z", b"z"])) == (b"a", [b"z"])


@pytest.mark.asyncio
async def test_dob_of_a_client_without_one_is_none() -> None:
    assert await merge._dob(tenant_ctx(ScriptedConn()), FakeVault(), KEEP) is None
