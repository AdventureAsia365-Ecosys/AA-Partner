"""S263 (AA-939 follow-up): the partner-account clean-up (app.members.accounts) on a scripted database and a fake
partner pool: who is deleted (never signed in, no member, no live invitation, no studio link, ended long enough ago),
who is kept and why, the cap, the dry run, the audit events, the job handler, the daily schedule, and the Cognito calls
of the directory."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest

from app.errors import Conflict, Unavailable
from app.jobs.kinds import PARTNER_ACCOUNTS
from app.jobs.queue import Job
from app.jobs.worker import PermanentError
from app.members import accounts
from app.members.directory import (
    CHANGED_SINCE,
    DELETED,
    FORCE_CHANGE_PASSWORD,
    GONE,
    SIGNED_IN,
    AccountState,
    CognitoDirectory,
    MemoryDirectory,
    delete_verdict,
)
from tests.tokens import PARTNER_ISSUER
from tests.unit.fakes import ScriptedConn

NOW = dt.datetime(2026, 11, 1, 3, 0, tzinfo=dt.UTC)
LONG_AGO = NOW - dt.timedelta(days=30)
RECENT = NOW - dt.timedelta(days=2)
AGENCY_A = uuid.UUID("00000000-0000-4000-8000-0000000000a1")
AGENCY_B = uuid.UUID("00000000-0000-4000-8000-0000000000b2")
OTHER_ISSUER = "https://cognito-idp.local.test/other_pool"


def inv(
    sub: str,
    status: str = "revoked",
    *,
    updated: dt.datetime = LONG_AGO,
    expires: dt.datetime | None = None,
    deleted: dt.datetime | None = None,
    iss: str = PARTNER_ISSUER,
) -> dict[str, Any]:
    return {
        "invitation_id": uuid.uuid4(),
        "cognito_iss": iss,
        "cognito_sub": sub,
        "status": status,
        "expires_at": expires or (updated + dt.timedelta(days=7)),
        "updated_at": updated,
        "deleted_at": deleted,
    }


class FakeDb(ScriptedConn):
    """One scripted pool: per-agency answers keyed by the ``app.tenant_id`` the context set."""

    def __init__(
        self,
        agencies: dict[uuid.UUID, dict[str, Any]],
        studio: list[dict[str, Any]] | None = None,
        *,
        cleanup_ready: bool = True,
    ) -> None:
        super().__init__()
        self.agencies = agencies
        self.tenant: uuid.UUID | None = None
        self.on("FROM crm.audit_export_agencies()", lambda: [_registry(t) for t in agencies])
        self.on("SELECT 1 FROM crm.tenant WHERE tenant_id = $1", lambda t: 1 if t in agencies else None)
        self.on("SELECT deleted_at IS NOT NULL AS trashed", lambda t: agencies[t].get("trashed", False))
        self.on("FROM crm.member_invitation WHERE cognito_sub IS NOT NULL", lambda: agencies[self.tenant]["inv"])
        self.on("FROM crm.member WHERE status IN ('active', 'invited')", lambda: agencies[self.tenant].get("mem", []))
        self.on("FROM crm.v_schema_versions WHERE version = $1", lambda v: cleanup_ready and v == "crm-039")
        self.on("FROM crm.studio_invitation WHERE cognito_sub IS NOT NULL", lambda: studio or [])

    async def execute(self, sql: str, *args: Any) -> str:
        if "set_config('app.tenant_id'" in sql:
            self.tenant = uuid.UUID(args[0])
        return await super().execute(sql, *args)

    def audits(self) -> list[tuple[Any, ...]]:
        return self.sql("INSERT INTO crm.audit_event")


def _registry(tenant_id: uuid.UUID) -> dict[str, Any]:
    return {"tenant_id": tenant_id, "exported_until": NOW, "verified_until": None, "last_seq": 0, "registered_at": NOW}


def pool_dir(*subs: tuple[str, str], modified: dt.datetime = LONG_AGO - dt.timedelta(days=1)) -> MemoryDirectory:
    directory = MemoryDirectory(PARTNER_ISSUER)
    for name, status in subs:
        directory.accounts[f"{name}@agency.example"] = {"sub": name, "status": status, "modified": modified}
    return directory


async def run(db: FakeDb, directory: Any, **kw: Any) -> dict[str, Any]:
    return await accounts.sweep(db, directory, now=NOW, **({"dry_run": False} | kw))  # type: ignore[arg-type]


# ── who goes ──────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_never_signed_in_account_whose_invitations_ended_long_ago_is_deleted_and_recorded() -> None:
    first, second = inv("s-old"), inv("s-old", "expired")
    db = FakeDb({AGENCY_A: {"inv": [first]}, AGENCY_B: {"inv": [second]}})
    directory = pool_dir(("s-old", FORCE_CHANGE_PASSWORD))
    result = await run(db, directory)
    assert result == {"dry_run": False, "candidate_count": 1, "deleted_count": 1}
    assert directory.deleted == ["s-old"]
    rows = db.audits()
    assert len(rows) == 2  # one event in each agency whose invitation it was
    flat = [" ".join(str(v) for v in row) for row in rows]
    assert all("partner_account_delete" in f and "never_signed_in" in f and "member_invitation" in f for f in flat)
    assert any(str(first["invitation_id"]) in f for f in flat) and any(str(second["invitation_id"]) in f for f in flat)
    assert not any("s-old" in f or "@" in f for f in flat)  # never the subject or the address


@pytest.mark.asyncio
async def test_a_confirmed_account_is_never_deleted() -> None:
    db = FakeDb({AGENCY_A: {"inv": [inv("s-pw")]}})
    directory = pool_dir(("s-pw", "CONFIRMED"))
    assert await run(db, directory) == {"dry_run": False, "candidate_count": 1, "kept_signed_in_count": 1}
    assert directory.deleted == [] and db.audits() == []


@pytest.mark.parametrize(
    ("agencies", "studio", "kept"),
    [
        # an active member elsewhere
        ({AGENCY_A: {"inv": [inv("s")]}, AGENCY_B: {"inv": [], "mem": [{"cognito_iss": PARTNER_ISSUER,
                                                                        "cognito_sub": "s"}]}}, [], "member"),
        # a live (claimed, not expired) invitation in another agency
        ({AGENCY_A: {"inv": [inv("s")]}, AGENCY_B: {"inv": [inv("s", "claimed", expires=NOW + dt.timedelta(days=3))]}},
         [], "live_invitation"),
        # an accepted invitation (the person signed in once)
        ({AGENCY_A: {"inv": [inv("s"), inv("s", "accepted")]}}, [], "accepted_invitation"),
        # ended two days ago: the grace window
        ({AGENCY_A: {"inv": [inv("s", updated=RECENT)]}}, [], "grace"),
        # a studio claim still open
        ({AGENCY_A: {"inv": [inv("s")]}}, [{"status": "claimed", "expires_at": NOW + dt.timedelta(days=1)}],
         "studio_invitation"),
        # a studio membership
        ({AGENCY_A: {"inv": [inv("s")]}}, [{"status": "activated"}], "studio_member"),
        # a studio invitation that ended two days ago
        ({AGENCY_A: {"inv": [inv("s")]}}, [{"status": "expired", "updated_at": RECENT}], "grace"),
    ],
)  # fmt: skip
@pytest.mark.asyncio
async def test_an_account_something_still_needs_is_kept(
    agencies: dict[uuid.UUID, dict[str, Any]], studio: list[dict[str, Any]], kept: str
) -> None:
    studio_rows = [
        {
            "studio_invitation_id": uuid.uuid4(),
            "cognito_iss": PARTNER_ISSUER,
            "cognito_sub": "s",
            "status": "revoked",
            "expires_at": LONG_AGO,
            "updated_at": LONG_AGO,
        }
        | row
        for row in studio
    ]
    db = FakeDb(agencies, studio_rows)
    directory = pool_dir(("s", FORCE_CHANGE_PASSWORD))
    assert await run(db, directory) == {"dry_run": False, "candidate_count": 1, f"kept_{kept}_count": 1}
    assert directory.deleted == [] and db.audits() == []


@pytest.mark.asyncio
async def test_a_trashed_test_agency_needs_no_grace_and_ended_studio_links_do_not_keep() -> None:
    db = FakeDb(
        {AGENCY_A: {"inv": [inv("s", updated=NOW - dt.timedelta(minutes=5))], "trashed": True}},
        [
            {
                "studio_invitation_id": uuid.uuid4(),
                "cognito_iss": PARTNER_ISSUER,
                "cognito_sub": "s",
                "status": "claimed",
                "expires_at": LONG_AGO,  # a claim that lapsed long ago
                "updated_at": LONG_AGO,
            }
        ],
    )
    directory = pool_dir(("s", FORCE_CHANGE_PASSWORD), modified=NOW - dt.timedelta(hours=1))
    assert (await run(db, directory))["deleted_count"] == 1


@pytest.mark.asyncio
async def test_an_account_changed_after_its_invitations_ended_is_kept() -> None:
    """A claim racing the clean-up (its RESEND changes the account) keeps it."""
    db = FakeDb({AGENCY_A: {"inv": [inv("s")]}})
    directory = pool_dir(("s", FORCE_CHANGE_PASSWORD), modified=NOW - dt.timedelta(minutes=1))
    assert await run(db, directory) == {"dry_run": False, "candidate_count": 1, f"kept_{CHANGED_SINCE}_count": 1}
    assert directory.deleted == []


@pytest.mark.asyncio
async def test_expired_but_unmarked_and_trashed_invitations_count_as_ended() -> None:
    db = FakeDb(
        {
            AGENCY_A: {"inv": [inv("s", "claimed", expires=LONG_AGO)]},
            AGENCY_B: {"inv": [inv("s", "claimed", expires=NOW + dt.timedelta(days=9), deleted=LONG_AGO)]},
        }
    )
    directory = pool_dir(("s", FORCE_CHANGE_PASSWORD))
    assert (await run(db, directory))["deleted_count"] == 1


# ── what is not a candidate ──────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_only_partner_pool_identities_of_crm_invitations_are_candidates() -> None:
    db = FakeDb(
        {AGENCY_A: {"inv": [inv("s-other", iss=OTHER_ISSUER)]}},
        [
            {
                "studio_invitation_id": uuid.uuid4(),
                "cognito_iss": PARTNER_ISSUER,
                "cognito_sub": "s-studio-only",
                "status": "expired",
                "expires_at": LONG_AGO,
                "updated_at": LONG_AGO,
            }
        ],
    )
    directory = pool_dir(("s-other", FORCE_CHANGE_PASSWORD), ("s-studio-only", FORCE_CHANGE_PASSWORD))
    assert await run(db, directory) == {"dry_run": False, "candidate_count": 0}
    assert directory.deleted == []


@pytest.mark.asyncio
async def test_the_agency_run_takes_only_that_agencys_identities() -> None:
    db = FakeDb({AGENCY_A: {"inv": [inv("s-a")], "trashed": True}, AGENCY_B: {"inv": [inv("s-b")]}})
    directory = pool_dir(("s-a", FORCE_CHANGE_PASSWORD), ("s-b", FORCE_CHANGE_PASSWORD))
    assert (await run(db, directory, tenant_id=AGENCY_A))["deleted_count"] == 1
    assert directory.deleted == ["s-a"]


# ── dry run, cap, errors ─────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_the_dry_run_counts_and_changes_nothing() -> None:
    db = FakeDb({AGENCY_A: {"inv": [inv("s-1"), inv("s-2"), inv("s-3")]}})
    directory = pool_dir(("s-1", FORCE_CHANGE_PASSWORD), ("s-2", "CONFIRMED"))
    result = await run(db, directory, dry_run=True)
    assert result == {
        "dry_run": True,
        "candidate_count": 3,
        "would_delete_count": 1,
        "kept_signed_in_count": 1,
        "already_gone_count": 1,
    }
    assert directory.deleted == [] and db.audits() == []


@pytest.mark.asyncio
async def test_the_cap_holds_per_run() -> None:
    subs = [f"s-{i}" for i in range(4)]
    db = FakeDb({AGENCY_A: {"inv": [inv(s) for s in subs]}})
    directory = pool_dir(*[(s, FORCE_CHANGE_PASSWORD) for s in subs])
    result = await run(db, directory, limit=2)
    assert (result["deleted_count"], result["capped_count"]) == (2, 2)
    assert len(directory.deleted) == 2
    # a limit above the hard cap is cut to it
    big = [f"b-{i}" for i in range(accounts.MAX_DELETES_PER_RUN + 3)]
    db = FakeDb({AGENCY_A: {"inv": [inv(s) for s in big]}})
    directory = pool_dir(*[(s, FORCE_CHANGE_PASSWORD) for s in big])
    result = await run(db, directory, limit=10_000)
    assert (result["deleted_count"], result["capped_count"]) == (accounts.MAX_DELETES_PER_RUN, 3)


class Flaky(MemoryDirectory):
    async def delete_never_signed_in(self, sub: str, *, untouched_since: dt.datetime) -> str:
        if sub == "s-bad":
            raise RuntimeError("throttled")
        return await super().delete_never_signed_in(sub, untouched_since=untouched_since)


@pytest.mark.asyncio
async def test_one_cognito_error_does_not_stop_the_others_and_records_nothing() -> None:
    db = FakeDb({AGENCY_A: {"inv": [inv("s-bad"), inv("s-good")]}})
    directory = Flaky(PARTNER_ISSUER)
    for s in ("s-bad", "s-good"):
        directory.accounts[s] = {"sub": s, "status": FORCE_CHANGE_PASSWORD, "modified": LONG_AGO - dt.timedelta(1)}
    result = await run(db, directory)
    assert (result["deleted_count"], result["failed_count"]) == (1, 1)
    assert len(db.audits()) == 1


@pytest.mark.asyncio
async def test_the_job_fails_for_a_retry_when_an_account_failed() -> None:
    db = FakeDb({AGENCY_A: {"inv": [inv("s-bad")]}})
    directory = Flaky(PARTNER_ISSUER)
    directory.accounts["s-bad"] = {"sub": "s-bad", "status": FORCE_CHANGE_PASSWORD, "modified": LONG_AGO}
    with pytest.raises(accounts.CleanupIncomplete, match="failed_count=1"):
        await accounts.build_handler(directory, dry_run=False)(db, job({"purpose": "daily_schedule"}))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_studio_read_before_crm039_stops_the_run() -> None:
    db = FakeDb({AGENCY_A: {"inv": [inv("s")]}}, cleanup_ready=False)
    directory = pool_dir(("s", FORCE_CHANGE_PASSWORD))
    with pytest.raises(Unavailable) as err:
        await run(db, directory)
    assert err.value.code == "cleanup_not_ready" and directory.deleted == []


@pytest.mark.asyncio
async def test_a_registry_row_without_its_agency_is_skipped() -> None:
    db = FakeDb({AGENCY_A: {"inv": [inv("s")]}})
    db.on("SELECT 1 FROM crm.tenant WHERE tenant_id = $1", None)
    assert await run(db, pool_dir(("s", FORCE_CHANGE_PASSWORD))) == {"dry_run": False, "candidate_count": 0}


@pytest.mark.asyncio
async def test_an_audit_failure_in_a_second_agency_still_counts_the_delete() -> None:
    db = FakeDb({AGENCY_A: {"inv": [inv("s")]}, AGENCY_B: {"inv": [inv("s")]}})
    seen: list[uuid.UUID] = []

    def exists(t: uuid.UUID) -> int | None:
        seen.append(t)
        return None if len(seen) > 3 else 1  # the two reads and the delete's own context, then the agency is gone

    db.on("SELECT 1 FROM crm.tenant WHERE tenant_id = $1", exists)
    directory = pool_dir(("s", FORCE_CHANGE_PASSWORD))
    assert (await run(db, directory))["deleted_count"] == 1
    assert len(db.audits()) == 1


# ── pure rules ───────────────────────────────────────────────────────────────────────────────────────────────────
def test_decide_keeps_when_nothing_ended() -> None:
    assert accounts.decide([], 0, now=NOW, grace=dt.timedelta(days=7)) == ("unknown", None)


@pytest.mark.parametrize(
    ("state", "verdict"),
    [
        (None, GONE),
        (AccountState("CONFIRMED", LONG_AGO), SIGNED_IN),
        (AccountState("RESET_REQUIRED", LONG_AGO), SIGNED_IN),
        (AccountState(FORCE_CHANGE_PASSWORD, None), CHANGED_SINCE),
        (AccountState(FORCE_CHANGE_PASSWORD, NOW), CHANGED_SINCE),
        (AccountState(FORCE_CHANGE_PASSWORD, LONG_AGO), None),
    ],
)
def test_the_directory_verdict(state: AccountState | None, verdict: str | None) -> None:
    assert delete_verdict(state, RECENT) == verdict


# ── the job and its schedule ─────────────────────────────────────────────────────────────────────────────────────
def job(payload: dict[str, Any]) -> Job:
    return Job(id="j1", kind=PARTNER_ACCOUNTS, payload=payload, attempt=1, max_attempts=3)


@pytest.mark.asyncio
async def test_the_handler_runs_both_purposes_with_the_configured_dry_run() -> None:
    db = FakeDb({AGENCY_A: {"inv": [inv("s-a")], "trashed": True}, AGENCY_B: {"inv": [inv("s-b")]}})
    directory = pool_dir(("s-a", FORCE_CHANGE_PASSWORD), ("s-b", FORCE_CHANGE_PASSWORD))
    dry = accounts.build_handler(directory, dry_run=True)
    got = await dry(db, job({"purpose": "daily_schedule"}))  # type: ignore[arg-type]
    assert got["purpose"] == "daily_schedule" and got["dry_run"] is True and got["would_delete_count"] == 2
    real = accounts.build_handler(directory, dry_run=False)
    got = await real(db, job({"purpose": "agency_delete", "tenant_id": str(AGENCY_A)}))  # type: ignore[arg-type]
    assert (got["purpose"], got["candidate_count"], got["deleted_count"]) == ("agency_delete", 1, 1)
    assert directory.deleted == ["s-a"]


@pytest.mark.parametrize(
    "payload",
    [{}, {"purpose": "other"}, {"purpose": "agency_delete"}, {"purpose": "agency_delete", "tenant_id": "x"}],
)
@pytest.mark.asyncio
async def test_the_handler_refuses_a_bad_payload(payload: dict[str, Any]) -> None:
    with pytest.raises(PermanentError):
        await accounts.build_handler(MemoryDirectory(), dry_run=True)(FakeDb({}), job(payload))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_the_handler_needs_the_directory() -> None:
    with pytest.raises(PermanentError, match="directory_not_configured"):
        await accounts.build_handler(None, dry_run=True)(FakeDb({}), job({"purpose": "daily_schedule"}))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_the_daily_schedule_queues_one_run_from_its_hour() -> None:
    clock = [NOW.replace(hour=2)]
    conn = ScriptedConn().on("INSERT INTO crm.job", "job-1")
    schedule = accounts.DailyPartnerAccountSchedule(3, lambda: clock[0])
    assert await schedule(conn) is None  # type: ignore[arg-type]
    clock[0] = NOW.replace(hour=4)
    assert await schedule(conn) == "job-1"  # type: ignore[arg-type]
    assert await schedule(conn) is None  # type: ignore[arg-type]  # once per day
    (args,) = conn.sql("INSERT INTO crm.job")
    assert args[0] == PARTNER_ACCOUNTS and '"daily_schedule"' in args[1]
    assert args[2] == accounts.schedule_key(NOW.date())
    with pytest.raises(ValueError):
        accounts.DailyPartnerAccountSchedule(24)


# ── the Cognito side ─────────────────────────────────────────────────────────────────────────────────────────────
class FakeIdp:
    class exceptions:  # noqa: N801 — boto3 shape
        class UserNotFoundException(Exception):
            pass

    def __init__(self, users: list[dict[str, Any]], *, vanish: bool = False) -> None:
        self.users = users
        self.vanish = vanish
        self.deleted: list[str] = []
        self.filters: list[str] = []

    def list_users(self, **kw: Any) -> dict[str, Any]:
        self.filters.append(kw["Filter"])
        return {"Users": self.users}

    def admin_delete_user(self, **kw: Any) -> None:
        if self.vanish:
            raise self.exceptions.UserNotFoundException()
        self.deleted.append(kw["Username"])


def user(status: str, modified: Any = LONG_AGO) -> dict[str, Any]:
    return {"Username": "internal-name", "UserStatus": status, "UserLastModifiedDate": modified}


@pytest.mark.asyncio
async def test_cognito_deletes_only_an_old_never_signed_in_account() -> None:
    pool_id = "us-west-1_TESTPARTN"
    idp = FakeIdp([user(FORCE_CHANGE_PASSWORD)])
    directory = CognitoDirectory(pool_id, PARTNER_ISSUER, client=idp)
    assert await directory.delete_never_signed_in("sub-1", untouched_since=RECENT) == DELETED
    assert idp.deleted == ["internal-name"] and idp.filters == ['sub = "sub-1"']
    for users, outcome in (
        ([user("CONFIRMED")], SIGNED_IN),
        ([user(FORCE_CHANGE_PASSWORD, NOW)], CHANGED_SINCE),
        ([user(FORCE_CHANGE_PASSWORD, "not a date")], CHANGED_SINCE),
        ([], GONE),
    ):
        idp = FakeIdp(users)
        assert (
            await CognitoDirectory(pool_id, PARTNER_ISSUER, client=idp).delete_never_signed_in(
                "sub-1", untouched_since=RECENT
            )
            == outcome
        )
        assert idp.deleted == []
    vanished = FakeIdp([user(FORCE_CHANGE_PASSWORD)], vanish=True)
    assert (
        await CognitoDirectory(pool_id, PARTNER_ISSUER, client=vanished).delete_never_signed_in(
            "sub-1", untouched_since=RECENT
        )
        == GONE
    )
    twins = FakeIdp([user(FORCE_CHANGE_PASSWORD), user(FORCE_CHANGE_PASSWORD)])
    with pytest.raises(Conflict):
        await CognitoDirectory(pool_id, PARTNER_ISSUER, client=twins).delete_never_signed_in(
            "sub-1", untouched_since=RECENT
        )
    state = await CognitoDirectory(pool_id, PARTNER_ISSUER, client=FakeIdp([user("CONFIRMED")])).account_state("s")
    assert state == AccountState("CONFIRMED", LONG_AGO)
    assert await CognitoDirectory(pool_id, PARTNER_ISSUER, client=FakeIdp([])).account_state("s") is None


@pytest.mark.asyncio
async def test_the_memory_directory_follows_the_same_rules() -> None:
    directory = MemoryDirectory(PARTNER_ISSUER)
    claimed = await directory.create_or_find("new@agency.example")
    assert (await directory.account_state(claimed.sub)).status == FORCE_CHANGE_PASSWORD  # type: ignore[union-attr]
    # just claimed: changed after any earlier end
    assert await directory.delete_never_signed_in(claimed.sub, untouched_since=RECENT) == CHANGED_SINCE
    assert await directory.delete_never_signed_in("nobody", untouched_since=RECENT) == GONE
