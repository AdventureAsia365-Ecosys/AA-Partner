"""The F3 placeholder map and the known values of a trip (AA-938; crm-009, design v3 §4.5)."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from datetime import UTC, datetime

import pytest

from app.errors import Forbidden, Gone
from app.privacy import purge
from app.privacy.known import known_values_for_trip, split_contact
from app.privacy.placeholder_store import restore_placeholders, seal_map, store_map
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import FakeVault, enc, system_ctx, tenant_ctx

pytestmark = pytest.mark.asyncio
MAP_ID = uuid.UUID("00000000-0000-4000-8000-0000000000aa")
TRIP = uuid.UUID("00000000-0000-4000-8000-0000000000b1")
LEAD = uuid.UUID("00000000-0000-4000-8000-0000000000c1")
MATE = uuid.UUID("00000000-0000-4000-8000-0000000000c2")
SPOUSE = uuid.UUID("00000000-0000-4000-8000-0000000000c3")


class RecordingVault(FakeVault):
    def __init__(self) -> None:
        super().__init__()
        self.sealed: list[tuple] = []

    async def encrypt(self, table, column, row_id, value, **kw):  # noqa: ANN001, ANN003, ANN201
        self.sealed.append((table, column, row_id, kw.get("purpose")))
        return await super().encrypt(table, column, row_id, value)


async def test_seal_uses_the_scrub_dek_and_store_writes_the_row() -> None:
    vault = RecordingVault()
    sealed = await seal_map(vault, {"<PERSON_1>": "Nguyễn Thị Hoa"})
    assert vault.sealed == [("placeholder_map", "map_enc", sealed.placeholder_map_id, "scrub")]
    assert sealed.count == 1 and "Hoa" not in repr(sealed)
    c = ScriptedConn()
    await store_map(system_ctx(c), sealed, "00000000-0000-4000-8000-000000000001", "f3-2")
    [args] = c.sql("INSERT INTO crm.placeholder_map")
    assert args[0] == sealed.placeholder_map_id and args[3:] == (1, "f3-2")
    with pytest.raises(ValueError):
        await seal_map(vault, {})


async def test_restore_only_in_a_member_session_audited_and_refused_when_expired() -> None:
    blob = enc("placeholder_map", "map_enc", json.dumps({"<PERSON_1>": "Nguyễn Thị Hoa"}))
    ctx = tenant_ctx(ScriptedConn().on("FROM crm.placeholder_map", blob))
    out = await restore_placeholders(ctx, FakeVault(), MAP_ID, "Chào <PERSON_1>, <PHONE_1>")
    assert out == "Chào Nguyễn Thị Hoa, <PHONE_1>"
    [event] = ctx.audit_events
    assert (event.action, event.outcome, event.object_id) == ("placeholder_restore", "allowed", str(MAP_ID))

    expired = tenant_ctx(ScriptedConn())
    with pytest.raises(Gone) as info:
        await restore_placeholders(expired, FakeVault(), MAP_ID, "x")
    assert info.value.code == "placeholder_map_expired"
    assert [(e.action, e.outcome, e.reason) for e in expired.audit_events] == [
        ("placeholder_restore", "denied", "expired")
    ]
    [(sql, _)] = expired.conn.calls
    assert "expires_at > now()" in sql and "deleted_at IS NULL" in sql

    with pytest.raises(Forbidden):
        await restore_placeholders(system_ctx(ScriptedConn()), FakeVault(), MAP_ID, "x")  # type: ignore[arg-type]


async def test_known_values_of_a_trip_and_one_audit_event() -> None:
    rows = [
        {
            "traveler_id": LEAD,
            "role": "lead",
            "display_name_enc": enc("traveler", "display_name_enc", "Nguyễn Thị Hoa"),
            "given_name_enc": enc("traveler", "given_name_enc", "Hoa"),
            "family_name_enc": enc("traveler", "family_name_enc", "Nguyễn"),
            "email_enc": enc("traveler", "email_enc", "hoa@example.test"),
            "phone_enc": enc("traveler", "phone_enc", "+84912345678"),
            "passport_no_enc": enc("traveler", "passport_no_enc", "C1234567"),
            "dob_enc": enc("traveler", "dob_enc", "1985-03-12"),
            "emergency_contact_enc": enc("traveler", "emergency_contact_enc", "Trần Văn Nam 0987 654 321"),
        },
        {
            "traveler_id": MATE,
            "role": "companion",
            "display_name_enc": enc("traveler", "display_name_enc", "Lê Minh"),
            "given_name_enc": None,
            "family_name_enc": None,
            "email_enc": None,
            "phone_enc": None,
            "passport_no_enc": None,
            "dob_enc": enc("traveler", "dob_enc", "not a date"),
            "emergency_contact_enc": None,
        },
    ]
    rows.append(
        {
            "traveler_id": SPOUSE,
            "role": "related",
            "display_name_enc": enc("traveler", "display_name_enc", "Phạm Quang Khải"),
            **dict.fromkeys(
                ("given_name_enc", "family_name_enc", "email_enc", "phone_enc", "passport_no_enc", "dob_enc"), None
            ),
            "emergency_contact_enc": None,
        }
    )
    conn = ScriptedConn().on("FROM crm.trip_traveler", rows).on("FROM crm.trip WHERE", ["vietnam", "laos"])
    ctx = tenant_ctx(conn)
    known = await known_values_for_trip(ctx, FakeVault(), TRIP)
    # Option B (S250): the trip's destination codes, read in the same context, nothing opened through the vault.
    assert known.destinations == ("vietnam", "laos") and known.count == 4
    [destinations_args] = conn.sql("SELECT destinations FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL")
    assert destinations_args == (TRIP,)
    lead, ec, mate, spouse = known.persons
    assert (spouse.role, spouse.display_name) == ("related", "Phạm Quang Khải")
    [(sql, _)] = ctx.conn.calls[:1]
    assert "crm.traveler_relation" in sql and "NOT IN (SELECT traveler_id FROM party)" in sql
    assert (lead.role, lead.display_name, lead.emails, lead.phones, lead.passports, lead.dobs) == (
        "traveller",
        "Nguyễn Thị Hoa",
        ("hoa@example.test",),
        ("+84912345678",),
        ("C1234567",),
        (dt.date(1985, 3, 12),),
    )
    assert (ec.role, ec.display_name, ec.phones) == ("emergency_contact", "Trần Văn Nam", ("0987 654 321",))
    assert (mate.role, mate.display_name, mate.dobs) == ("companion", "Lê Minh", ())
    [event] = ctx.audit_events
    assert (event.action, event.object_kind, event.object_id) == ("scrub_known_values", "trip", str(TRIP))
    assert event.object_ids == (str(LEAD), str(MATE), str(SPOUSE)) and "dob" in event.fields
    empty = tenant_ctx(ScriptedConn())
    nothing = await known_values_for_trip(empty, FakeVault(), TRIP)
    assert nothing.persons == () and nothing.destinations == () and empty.audit_events == []


async def test_split_contact() -> None:
    assert split_contact("Trần Văn Nam - 0987 654 321") == ("Trần Văn Nam", ("0987 654 321",))
    assert split_contact("+84 912 345 678") == (None, ("+84 912 345 678",))
    assert split_contact("Mum (Anna Smith)") == ("Mum Anna Smith", ())


class PurgePool:
    def __init__(self) -> None:
        self.conn = ScriptedConn()

    def acquire(self):  # noqa: ANN201
        conn = self.conn

        class _Acquire:
            async def __aenter__(self):  # noqa: ANN204
                return conn

            async def __aexit__(self, *exc):  # noqa: ANN002, ANN204
                return False

        return _Acquire()


async def test_purge_sets_the_scope_then_deletes_without_a_where_clause() -> None:
    pool = PurgePool()

    async def execute(sql: str, *args: object) -> str:
        pool.conn._answer(sql, args)
        return "DELETE 3" if sql.startswith("DELETE") else "SELECT 1"

    pool.conn.execute = execute  # type: ignore[method-assign]
    assert await purge.purge_expired_maps(pool, None) == {"deleted_count": 3}  # type: ignore[arg-type]
    assert [sql for sql, _ in pool.conn.calls] == [purge.SCOPE_SQL, "DELETE FROM crm.placeholder_map"]


async def test_hourly_hook_queues_one_due_now_run_per_utc_hour() -> None:
    from tests.unit.test_job_handlers import FakeEnqueuePool

    clock = {"now": datetime(2026, 10, 24, 4, 5, tzinfo=UTC)}
    hook = purge.HourlyPlaceholderPurge(now=lambda: clock["now"])
    pool = FakeEnqueuePool()
    assert await hook(pool) == "job-1"
    kind, payload, key, max_attempts, created_by, delay = pool.inserts[0]
    assert (kind, key, max_attempts, created_by, delay) == (
        "crm_placeholder_purge",
        "crm_placeholder_purge:schedule:2026-10-24T04",
        3,
        "scheduler:crm-placeholder-purge",
        0.0,
    )
    assert json.loads(payload) == {"purpose": "hourly_schedule"} and pool.prunes == [("crm_placeholder_purge", 2)]
    assert await hook(pool) is None and len(pool.inserts) == 1  # same hour
    clock["now"] = datetime(2026, 10, 24, 5, 0, tzinfo=UTC)
    await hook(pool)
    assert len(pool.inserts) == 2


async def test_place_placeholders_round_trip_through_the_sealed_map() -> None:
    # Option C (AA-938, S249): <PLACE_n> lives in the same sealed map and is restored like any placeholder.
    from app.privacy.scrubber import Detection, PiiScrubber

    class Finds:
        name = "fake"

        async def detect(self, text):  # noqa: ANN001, ANN202
            return [Detection("ADDRESS", "Hà Giang"), Detection("NAME", "Kim Liên")]

    scrubbed = await PiiScrubber(Finds()).scrub("Khách Kim Liên muốn đi Hà Giang.")
    assert scrubbed.text == "Khách <PERSON_1> muốn đi <PLACE_1>."
    vault = RecordingVault()
    sealed = await seal_map(vault, scrubbed.placeholders)
    blob = enc("placeholder_map", "map_enc", json.dumps(scrubbed.placeholders, ensure_ascii=False))
    ctx = tenant_ctx(ScriptedConn().on("FROM crm.placeholder_map", blob))
    out = await restore_placeholders(ctx, FakeVault(), sealed.placeholder_map_id, "Chào <PERSON_1>, tour <PLACE_1> ạ.")
    assert out == "Chào Kim Liên, tour Hà Giang ạ."
