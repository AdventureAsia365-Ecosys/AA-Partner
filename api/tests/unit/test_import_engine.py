"""AA-962 (card CRM-31): the import engine's row outcomes for the four files (clients, relationships, trips, notes), in
a dry run and when applying, on a scripted connection. The SQL itself (blind-index lookups, RLS, the inserts) is
covered by tests/integration/test_onboarding_import_api.py; these drive the Python branches around it. Invented data
only."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import asyncpg
import pytest

from app.errors import Invalid
from app.imports import engine
from app.imports.formats import Row
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import MEMBER, FakeVault, enc, tenant_ctx

pytestmark = pytest.mark.asyncio

KNOWN = uuid.UUID("00000000-0000-4000-8000-000000000101")
OTHER = uuid.UUID("00000000-0000-4000-8000-000000000102")
THIRD = uuid.UUID("00000000-0000-4000-8000-000000000103")
TRIP = uuid.UUID("00000000-0000-4000-8000-000000000201")

BY_REF = "FROM unnest($1::bytea[]) AS k(k)"
BY_EMAIL = "WHERE email_bidx = ANY"
BY_PHONE = "WHERE phone_bidx = ANY"
BY_NAME = "SELECT DISTINCT name_bidx"
TRIP_BY_REF = "FROM crm.trip WHERE import_ref_bidx"
NOTE_BY_REF = "FROM crm.note WHERE import_ref_bidx"
RELATIONS = "FROM crm.traveler_relation WHERE deleted_at IS NULL"


class RefVault(FakeVault):
    """FakeVault plus the import ref index: ``r:<table>:<ref>``."""

    async def ref_index(self, table: str, column: str, value: str) -> bytes:
        return f"r:{table}:{value}".encode()


def rows(*cells: dict[str, str]) -> list[Row]:
    return [Row(number, c) for number, c in enumerate(cells, start=2)]


def answers(mapping: dict[bytes, uuid.UUID]) -> Any:
    def answer(keys: list[bytes]) -> list[dict[str, Any]]:
        return [{"k": k, "id": mapping[k]} for k in keys if k in mapping]

    return answer


def outcomes(results: list[engine.Result]) -> list[tuple[int, str, tuple[str, ...]]]:
    return [(r.row, r.outcome, r.codes) for r in results]


# ── clients ────────────────────────────────────────────────────────────────────────────────────────
CLIENT_ROWS = rows(
    {
        "client_ref": "C-1",
        "full_name": "Ana Moss",
        "email": "ana@example.test",
        "dob": "02/01/1990",
        "nationality": "au",
        "channel": "Email",
        "marketing": "yes",
    },
    {"client_ref": "C-1", "name": "Ana Moss Again", "email": "other@example.test"},
    {"client_ref": "C-2", "name": "Ben Moss", "email": "ANA@example.test"},
    {"client_ref": "C-3", "name": "Cy Known", "email": "known@example.test"},
    {"client_ref": "C-OLD", "name": "Dee Old"},
    {"client_ref": "C-9", "name": "", "email": "not-an-email", "channel": "fax", "marketing": "maybe", "dob": "May"},
    {"client_ref": "C-4", "name": "Eli Fern", "mobile": "0412 345 678", "family_name": "Fern"},
    {"client_ref": "C-5", "name": "Flo Fern", "phone": "+61 412 345 678"},
    {"client_ref": "C-6", "name": "Gus Pine", "phone": "+61 400 000 111"},
)


def client_conn() -> ScriptedConn:
    return ScriptedConn(
        [
            (BY_REF, answers({b"r:traveler:C-OLD": OTHER})),
            (BY_EMAIL, answers({b"e:known@example.test": KNOWN})),
            (BY_PHONE, answers({b"p:+61400000111": THIRD})),
            (BY_NAME, lambda keys: [{"k": k} for k in keys]),
        ]
    )


async def test_clients_dry_run_classifies_every_row_and_writes_nothing() -> None:
    conn = client_conn()
    results = await engine.run(
        tenant_ctx(conn), RefVault(), "clients", CLIENT_ROWS, apply=False, member_id=MEMBER, region="AU"
    )
    assert outcomes(results) == [
        (2, "create", ("possible_duplicate",)),  # the fake name index is one value for every name
        (3, "duplicate_in_file", ("duplicate_client_ref",)),
        (4, "match", ("duplicate_contact",)),
        (5, "match", ()),
        (6, "already_imported", ()),
        (
            7,
            "invalid",
            ("invalid_name", "invalid_email", "invalid_dob", "invalid_preferred_channel", "invalid_marketing_consent"),
        ),
        (8, "create", ("possible_duplicate",)),
        (9, "match", ("duplicate_contact",)),
        (10, "match", ()),
    ]
    assert results[3].object_id == KNOWN and results[4].object_id == OTHER and results[8].object_id == THIRD
    assert results[2].object_id is None  # a dry run creates no client for the earlier row to point to
    assert not conn.sql("INSERT") and not conn.sql("pg_advisory_xact_lock") and not conn.sql("UPDATE")


async def test_clients_apply_locks_inserts_links_refs_and_audits_the_created_ids() -> None:
    conn = client_conn()
    ctx = tenant_ctx(conn)
    results = await engine.run(ctx, RefVault(), "clients", CLIENT_ROWS, apply=True, member_id=MEMBER, region="AU")
    created = [r.object_id for r in results if r.outcome == "create"]
    assert len(created) == 2 and all(created)
    # the per-agency import lock, then the intake dedupe locks: every e-mail before any phone
    locks = [args for flat, args in conn.calls if "pg_advisory_xact_lock" in flat]
    assert len(locks[0]) == 1
    kinds = [a[0] for a in locks[1:]]
    assert kinds == sorted(kinds) and set(kinds) == {"crm_intake_email", "crm_intake_phone"}
    inserts = conn.sql("INSERT INTO crm.traveler (")
    assert [a[0] for a in inserts] == created
    first = inserts[0]
    assert first[1] == enc("traveler", "display_name_enc", "Ana Moss")
    assert first[7] == b"e:ana@example.test" and first[10] == "email" and first[12] == MEMBER
    assert first[13] == enc("traveler", "dob_enc", "1990-01-02") and first[14] == enc(
        "traveler", "nationality_enc", "AU"
    )
    assert inserts[1][9] == b"p:+61412345678" and inserts[1][5] == enc("traveler", "family_name_enc", "Fern")
    # marketing consent for the new client only; profile rows for both
    assert conn.sql("INSERT INTO crm.consent") == [(created[0], True)]
    assert [a[1] for a in conn.sql("INSERT INTO crm.traveler_profile")] == [True, False]
    # matched rows point their ref at the client: the earlier row's new client, the known one, the phone match
    linked = conn.sql("UPDATE crm.traveler SET import_ref_bidx")
    assert (created[0], b"r:traveler:C-2") in linked and (KNOWN, b"r:traveler:C-3") in linked
    assert (created[1], b"r:traveler:C-5") in linked and (THIRD, b"r:traveler:C-6") in linked
    (event,) = ctx.audit_events
    assert event.action == "import" and event.object_kind == "traveler"
    assert set(event.object_ids) == {str(c) for c in created}


async def test_an_unknown_entity_is_refused() -> None:
    with pytest.raises(Invalid) as err:
        await engine.run(tenant_ctx(), RefVault(), "suppliers", [], apply=False, member_id=None)
    assert err.value.code == "invalid_entity"


async def test_empty_client_file_makes_no_lookup() -> None:
    conn = ScriptedConn()
    assert await engine.run(tenant_ctx(conn), RefVault(), "clients", [], apply=False, member_id=None) == []
    assert conn.calls == []


# ── relationships ──────────────────────────────────────────────────────────────────────────────────
A, B, C, D = (uuid.UUID(f"00000000-0000-4000-8000-0000000003{i:02d}") for i in range(1, 5))
CLIENT_REFS = {f"r:traveler:{ref}".encode(): tid for ref, tid in (("A", A), ("B", B), ("C", C), ("D", D), ("A2", A))}


def relation_conn(conflict: tuple[uuid.UUID, uuid.UUID] | None = None) -> ScriptedConn:
    def insert(a: uuid.UUID, b: uuid.UUID, kind: str) -> Any:
        if conflict == (a, b):
            return asyncpg.UniqueViolationError("duplicate key")
        return uuid.uuid4()

    return ScriptedConn(
        [
            (BY_REF, answers(CLIENT_REFS)),
            (RELATIONS, [{"from_traveler_id": C, "to_traveler_id": D, "kind": "referrer"}]),
            ("INSERT INTO crm.traveler_relation", insert),
        ]
    )


RELATION_ROWS = rows(
    {"client_ref": "A", "related_ref": "B", "kind": "Spouse"},
    {"client_ref": "B", "related_client_ref": "A", "relation": "spouse"},  # same pair, other way round
    {"client_ref": "A", "related_client_ref": "A2", "relation": "friend"},  # one client under two refs
    {"client_ref": "X", "related_client_ref": "Y", "relation": "friend"},
    {"client_ref": "A", "related_client_ref": "B", "relation": "boss"},
    {"client_ref": "D", "related_client_ref": "C", "relation": "friend"},  # already in the agency
    {"client_ref": "C", "related_client_ref": "A", "relation": "referrer"},  # C already has a referrer
    {"client_ref": "A", "related_client_ref": "C", "relation": "referrer"},  # C referred A
    {"client_ref": "B", "related_client_ref": "D", "relation": "referred"},  # B referred D
    {"client_ref": "B", "related_client_ref": "C", "relation": "referrer"},  # B gets a referrer
    {"client_ref": "D", "related_client_ref": "A", "relation": "referrer"},  # D got B as referrer two rows above
)


async def test_relationships_dry_run() -> None:
    conn = relation_conn()
    results = await engine.run(
        tenant_ctx(conn), RefVault(), "relationships", RELATION_ROWS, apply=False, member_id=None
    )
    assert outcomes(results) == [
        (2, "create", ()),
        (3, "duplicate_in_file", ("duplicate_relationship",)),
        (4, "invalid", ("same_client",)),
        (5, "invalid", ("unknown_client_ref", "unknown_related_client_ref")),
        (6, "invalid", ("invalid_relation",)),
        (7, "already_imported", ()),
        (8, "invalid", ("referrer_exists",)),
        (9, "create", ()),
        (10, "create", ()),
        (11, "create", ()),
        (12, "invalid", ("referrer_exists",)),
    ]
    assert not conn.sql("INSERT")


async def test_relationships_apply_sets_referrers_and_reports_a_conflict() -> None:
    conn = relation_conn(conflict=(B, C))
    ctx = tenant_ctx(conn)
    results = await engine.run(ctx, RefVault(), "relationships", RELATION_ROWS, apply=True, member_id=None)
    assert results[0].object_id is not None
    assert (results[9].outcome, results[9].codes) == ("invalid", ("relation_conflict",))
    assert conn.sql("SET referrer_traveler_id") == [(A, C), (D, B)]
    assert ctx.audit_events[0].object_kind == "traveler_relation" and len(ctx.audit_events[0].object_ids) == 3


async def test_relationships_without_known_clients_skip_the_relation_lookup() -> None:
    conn = ScriptedConn()
    results = await engine.run(
        tenant_ctx(conn),
        RefVault(),
        "relationships",
        rows({"client_ref": "Q", "related_client_ref": "R", "relation": "friend"}),
        apply=False,
        member_id=None,
    )
    assert results[0].outcome == "invalid" and not conn.sql(RELATIONS)


# ── trips ──────────────────────────────────────────────────────────────────────────────────────────
def trip_conn() -> ScriptedConn:
    sources = {A: {"dob_enc": None, "profile_token_enc": enc("traveler", "profile_token_enc", str(uuid.UUID(int=7)))}}
    return ScriptedConn(
        [
            (TRIP_BY_REF, answers({b"r:trip:T-OLD": TRIP})),
            (BY_REF, answers(CLIENT_REFS)),
            (
                "SELECT dob_enc, profile_token_enc",
                lambda tid: sources.get(tid, {"dob_enc": None, "profile_token_enc": None}),
            ),
            ("RETURNING trip_party_id", lambda *_: uuid.uuid4()),
        ]
    )


TRIP_ROWS = rows(
    {
        "trip_ref": "T-1",
        "client_ref": "A",
        "title": "Coast walk",
        "start": "2020-03-01",
        "end": "2020-03-09",
        "companions": "B; A2",
    },
    {"trip_ref": "T-1", "client_ref": "A", "title": "Coast walk"},
    {"trip_ref": "T-OLD", "client_ref": "A", "title": "Old trip"},
    {"trip_ref": "T-2", "client_ref": "Z", "title": "Lost lead", "companion_refs": "Y"},
    {"trip_ref": "T-3", "client_ref": "A", "title": "Backwards", "start_date": "2024-05-09", "end_date": "2024-05-01"},
    {"trip_ref": "T-4", "client_ref": "A", "title": "Bad", "status": "booked", "adults": "0"},
    {"trip_ref": "T-5", "lead_client_ref": "B", "trip_name": "Next year", "start_date": "2999-01-01"},
    {"trip_ref": "T-6", "client_ref": "C", "title": "Gone cold", "stage": "lost", "adults": "3"},
    {"trip_ref": "T-7", "client_ref": "D", "title": "Called off", "status": "canceled"},
)


async def test_trips_dry_run() -> None:
    results = await engine.run(tenant_ctx(trip_conn()), RefVault(), "trips", TRIP_ROWS, apply=False, member_id=MEMBER)
    assert outcomes(results) == [
        (2, "create", ()),
        (3, "duplicate_in_file", ("duplicate_trip_ref",)),
        (4, "already_imported", ()),
        (5, "invalid", ("unknown_client_ref", "unknown_companion_ref")),
        (6, "invalid", ("invalid_end_date",)),
        (7, "invalid", ("invalid_status", "invalid_adults")),
        (8, "create", ()),
        (9, "create", ()),
        (10, "create", ()),
    ]
    assert results[2].object_id == TRIP


async def test_trips_apply_writes_stage_party_and_history() -> None:
    conn = trip_conn()
    ctx = tenant_ctx(conn)
    await engine.run(ctx, RefVault(), "trips", TRIP_ROWS, apply=True, member_id=MEMBER)
    trips = conn.sql("INSERT INTO crm.trip (")
    # (stage, waiting_on, adults, lost reason, cancel reason) per created trip
    assert [(t[2], t[3], t[6], t[8], t[9]) for t in trips] == [
        ("completed", "none", 2, None, None),  # past dates; lead A and companion B (A2 is A again)
        ("inquiry", "agency", 1, None, None),
        ("lost", "none", 3, "other", None),
        ("cancelled", "none", 1, None, "other"),
    ]
    assert [a[1] for a in conn.sql("INSERT INTO crm.trip_stage_history")] == [
        "completed",
        "inquiry",
        "lost",
        "cancelled",
    ]
    party = conn.sql("INSERT INTO crm.trip_traveler")
    assert [(p[1], p[2]) for p in party[:2]] == [(A, "lead"), (B, "companion")]
    # A had a profile token (decrypted); B had none: an empty profile and its sealed pointer are written
    tokens = [a[1] for a in conn.sql("INSERT INTO crm.trip_party")]
    assert tokens[0] == uuid.UUID(int=7)
    assert conn.sql("INSERT INTO crm.traveler_profile")[0][0] == tokens[1]
    assert conn.sql("UPDATE crm.traveler SET profile_token_enc")[0][0] == B
    assert ctx.audit_events[0].object_kind == "trip"


# ── notes ──────────────────────────────────────────────────────────────────────────────────────────
NOTE_ROWS = rows(
    {"client_ref": "A", "text": "Prefers\r\nwindow seats", "created": "2024-02-03"},
    {"client_ref": "A", "text": "Prefers\r\nwindow seats", "created": "2024-02-03"},
    {"note_ref": "N-OLD", "client_ref": "A", "text": "Old"},
    {"trip_ref": "T-1", "body": "Trip note", "note_id": "N-2"},
    {"client_ref": "Q", "trip_ref": "T-9", "text": "Nobody"},
    {"text": "About nobody", "client_ref": " "},
    {"client_ref": "A", "text": " ", "date": "soon"},
    {"client_ref": "A", "note_ref": "x" * 101, "text": "Too long a ref"},
)


def note_conn() -> ScriptedConn:
    return ScriptedConn(
        [
            (NOTE_BY_REF, answers({b"r:note:N-OLD": KNOWN})),
            (TRIP_BY_REF, answers({b"r:trip:T-1": TRIP})),
            (BY_REF, answers(CLIENT_REFS)),
        ]
    )


async def test_notes_dry_run() -> None:
    results = await engine.run(tenant_ctx(note_conn()), RefVault(), "notes", NOTE_ROWS, apply=False, member_id=None)
    assert outcomes(results) == [
        (2, "create", ()),
        (3, "duplicate_in_file", ("duplicate_note",)),
        (4, "already_imported", ()),
        (5, "create", ()),
        (6, "invalid", ("unknown_client_ref", "unknown_trip_ref")),
        (7, "invalid", ("missing_subject",)),
        (8, "invalid", ("invalid_text", "invalid_date")),
        (9, "invalid", ("invalid_ref",)),
    ]


async def test_notes_apply_seals_the_body_and_keeps_the_date() -> None:
    conn = note_conn()
    ctx = tenant_ctx(conn)
    await engine.run(ctx, RefVault(), "notes", NOTE_ROWS, apply=True, member_id=None)
    first, second = conn.sql("INSERT INTO crm.note")
    assert first[1] is None and first[2] == A and first[3] == enc("note", "body_enc", "Prefers\nwindow seats")
    assert first[5] == dt.datetime(2024, 2, 3, 12, tzinfo=dt.UTC)
    assert second[1] == TRIP and second[2] is None and second[4] == b"r:note:N-2" and second[5] is None
    assert ctx.audit_events[0].object_kind == "note"
