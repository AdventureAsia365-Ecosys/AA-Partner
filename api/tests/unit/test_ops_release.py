"""AA-956 (card CRM-26) without a database: route classes and groups, the manifest CSV, the purge date, the job
schedule, the Booking incident admin link and the ``BOOKING_ADMIN_URL`` setting.

- **R**: staff without ``crm-ops`` -> 403 (``crm-support`` and any other WORKFORCE group); a ``crm-staff`` token on
  the agency routes and a partner token on the staff routes are refused by the class guard.
- **E**: dates moved -> purge date recomputed (``purge_on`` follows ``end_date``; the integration test drives it).
- Every staff route is a GET (the staff class stays read-only, AA-971).
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.audit_export import registry
from app.errors import Invalid
from app.jobs.kinds import OPS_RELEASE_SYNC
from app.jobs.payload_guard import check_payload
from app.jobs.worker import PermanentError
from app.main import create_app
from app.ops_release import job, service, staff
from app.settings import SettingsError, load_settings
from tests.tokens import LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import tenant_ctx

TENANT = "00000000-0000-4000-8000-000000000001"
TRIP = "00000000-0000-4000-8000-0000000000aa"
STAFF_PATHS = (
    "/crm/staff/v1/ops-releases",
    f"/crm/staff/v1/agencies/{TENANT}/trips/{TRIP}/ops-release",
    f"/crm/staff/v1/agencies/{TENANT}/trips/{TRIP}/ops-release/manifest",
)


@pytest.fixture(scope="module")
def pools() -> LocalPools:
    return LocalPools()


@pytest.fixture
def client(pools: LocalPools) -> Iterator[TestClient]:
    settings = pools.settings()
    with TestClient(create_app(settings, verifier=pools.verifier(settings))) as c:
        yield c


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ── routes ─────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", STAFF_PATHS)
@pytest.mark.parametrize("groups", [["crm-support"], ["cis-admin"], []])
def test_staff_without_crm_ops_is_403(client: TestClient, pools: LocalPools, path: str, groups: list[str]) -> None:
    res = client.get(path, headers=_bearer(pools.crm_staff(groups)))
    assert res.status_code == 403, (path, groups)


@pytest.mark.parametrize("path", STAFF_PATHS)
def test_crm_ops_passes_the_guards(client: TestClient, pools: LocalPools, path: str) -> None:
    """Past the class and group guards the route needs the database (none configured here): 503, not 401 / 403."""
    res = client.get(path, headers=_bearer(pools.crm_staff(["crm-ops"])))
    assert (res.status_code, res.json()["detail"]) == (503, "database_not_configured")


@pytest.mark.parametrize("path", STAFF_PATHS)
def test_a_partner_token_is_refused_on_the_staff_routes(client: TestClient, pools: LocalPools, path: str) -> None:
    assert client.get(path, headers=_bearer(pools.partner_web())).status_code == 403


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", f"/crm/v1/trips/{TRIP}/ops-release"), ("POST", f"/crm/v1/trips/{TRIP}/ops-release/withdraw")],
)
def test_a_staff_token_is_refused_on_the_agency_routes(
    client: TestClient, pools: LocalPools, method: str, path: str
) -> None:
    res = client.request(method, path, headers={**_bearer(pools.crm_staff(["crm-ops"])), "X-AA-Tenant-Id": TENANT})
    assert (res.status_code, res.json()["detail"]) == (403, "wrong_audience")


def test_every_ops_release_staff_route_is_a_get(client: TestClient) -> None:
    spec = client.app.openapi()
    paths = spec["paths"].items()
    ours = [(m.upper(), p) for p, ops in paths if "ops-release" in p and "/crm/staff/" in p for m in ops]
    assert len(ours) == len(STAFF_PATHS)
    assert {m for m, _ in ours} == {"GET"}


def test_an_unknown_manifest_field_is_422(client: TestClient, pools: LocalPools) -> None:
    res = client.get(STAFF_PATHS[2] + "?fields=phone", headers=_bearer(pools.crm_staff(["crm-ops"])))
    assert res.status_code == 422


# ── the manifest ───────────────────────────────────────────────────────────────────────────────────

DOC: dict[str, Any] = {
    "v": 1,
    "travellers": [
        {
            "name": "Lotus Mai",
            "given_name": "Mai",
            "family_name": "Lotus",
            "age_band": "adult",
            "dietary": ["vegetarian"],
            "mobility": [],
            "arrival": {
                "flight_no": "ZZ101",
                "airport": "HAN",
                "sched_local": "2026-12-01T22:40",
                "tz": "Asia/Bangkok",
            },
            "departure": None,
        },
        {"name": "=HYPERLINK(1)", "age_band": "child", "dietary": [], "mobility": ["wheelchair"], "arrival": None},
    ],
}


def test_manifest_has_the_watermark_on_every_row_and_only_the_picked_fields() -> None:
    text, rows = staff.manifest_csv(DOC, ("name", "dietary", "arrival_flight"), "AAOPS-0123456789")
    lines = text.strip().split("\r\n")
    assert rows == 2
    assert lines[0] == "watermark,no,name,dietary,arrival_flight"
    assert lines[1] == "AAOPS-0123456789,1,Mai Lotus,vegetarian,ZZ101 HAN 2026-12-01T22:40 Asia/Bangkok"
    assert all(line.startswith("AAOPS-0123456789,") for line in lines[1:])
    assert "age_band" not in lines[0] and "insurance" not in text


def test_manifest_cells_never_start_a_formula() -> None:
    text, _ = staff.manifest_csv(DOC, ("name",), "AAOPS-0123456789")
    assert ",'=HYPERLINK(1)" in text


def test_manifest_fields_default_and_unknown() -> None:
    assert staff.pick_fields([]) == staff.MANIFEST_DEFAULT
    assert staff.pick_fields(["departure_flight", "name"]) == ("name", "departure_flight")
    with pytest.raises(Invalid):
        staff.pick_fields(["phone"])


def test_watermark_fits_the_crm034_check() -> None:
    import re

    marks = {staff.watermark() for _ in range(20)}
    assert len(marks) == 20
    assert all(re.fullmatch(r"[A-Z0-9-]{8,40}", m) for m in marks)


# ── purge date, incident link, settings ──────────────────────────────────────────────────────────────


def test_purge_date_follows_the_return_date() -> None:
    assert service.purge_on(dt.date(2026, 12, 10)) == dt.date(2027, 3, 10)
    assert service.purge_on(dt.date(2026, 12, 20)) == dt.date(2027, 3, 20)  # dates moved -> purge moved
    assert service.purge_on(None) is None


def test_incident_admin_link_is_the_booking_b2b_desk() -> None:
    assert staff.incident_admin_url("https://booking.example.invalid") == "https://booking.example.invalid/b2b-desk"
    assert staff.incident_admin_url("") is None


def test_booking_admin_url_setting() -> None:
    assert load_settings({}).booking_admin_url == ""
    got = load_settings({"BOOKING_ADMIN_URL": "https://Booking.example.invalid/"})
    assert got.booking_admin_url == "https://booking.example.invalid"
    for bad in ("http://booking.example.invalid", "https://booking.example.invalid/b2b-desk"):
        with pytest.raises(SettingsError, match="BOOKING_ADMIN_URL"):
            load_settings({"BOOKING_ADMIN_URL": bad})


# ── the job ────────────────────────────────────────────────────────────────────────────────────────


def test_schedule_is_one_row_per_utc_hour() -> None:
    at = dt.datetime(2026, 12, 1, 7, 59, tzinfo=dt.UTC)
    assert job.schedule_key(at) == f"{OPS_RELEASE_SYNC}:schedule:2026-12-01T07"
    assert job.schedule_key(at + dt.timedelta(minutes=1)) == f"{OPS_RELEASE_SYNC}:schedule:2026-12-01T08"


def test_nudge_payload_is_ids_only() -> None:
    check_payload({"tenant_id": TENANT, "trip_id": TRIP})
    assert job._ids({"tenant_id": TENANT, "trip_id": TRIP}) == (uuid.UUID(TENANT), uuid.UUID(TRIP))
    assert job._ids({"purpose": "hourly_schedule"}) is None
    with pytest.raises(PermanentError):
        job._ids({"tenant_id": "x", "trip_id": TRIP})


@pytest.mark.asyncio
async def test_without_the_key_provider_the_run_finishes_instead_of_retrying() -> None:
    from app.jobs.queue import Job

    handler = job.build_handler(None)
    out = await handler(
        None, Job(id="1", kind=OPS_RELEASE_SYNC, payload={"purpose": "hourly_schedule"}, attempt=1, max_attempts=3)
    )  # type: ignore[arg-type]
    assert out == {"agency_count": 0, "reason_code": "key_provider_not_configured"}
    check_payload(out)


# ── AA ops notices after the commit, one failing trip (review fixes) ────────────────────────────────


class _Tx:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Conn:
    def transaction(self) -> _Tx:
        return _Tx()


def _ctx() -> Any:
    from app.tenancy import SystemContext

    return SystemContext(conn=_Conn(), tenant_id=uuid.UUID(TENANT), actor=service.ACTOR)  # type: ignore[arg-type]


def _lines(caplog: pytest.LogCaptureFixture, event: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith(event + " ")]


def test_the_notice_line_is_the_one_the_infra_alarm_matches(caplog: pytest.LogCaptureFixture) -> None:
    """AA-CIS-Infra crm_ops_release_alarms.tf matches the quoted event names; renaming one silences its alarm."""
    assert (service.WITHDRAWN_EVENT, service.T21_EVENT) == (
        "crm_ops_release_withdrawn",
        "crm_ops_release_incomplete_t21",
    )
    caplog.set_level(logging.INFO, logger="app.ops_release.service")
    release = uuid.UUID(TRIP)
    notices: service.Notices = [service._withdrawn(_ctx(), release, "trip_cancelled")]
    assert not _lines(caplog, service.WITHDRAWN_EVENT)  # queued: nothing written before the commit
    service.emit(notices)
    assert _lines(caplog, service.WITHDRAWN_EVENT) == [
        f"crm_ops_release_withdrawn tenant_id={TENANT} ops_release_id={TRIP} reason=trip_cancelled"
    ]
    assert notices == []  # written once


@pytest.mark.asyncio
async def test_item_tx_puts_the_context_back_but_keeps_denials() -> None:
    ctx = _ctx()
    ctx.audit("ops_release_build", "allowed", "trip")
    ctx.dek_memo[("vault", 1)] = b"k" * 32
    ctx.dek_current["vault"] = 1
    with pytest.raises(RuntimeError):
        async with service.item_tx(ctx):
            ctx.audit("ops_release_create", "allowed", "trip", TRIP)
            ctx.audit("decrypt", "denied", "ops_release", TRIP)
            ctx.dek_memo[("ops_release", 1)] = b"x" * 32  # a DEK whose row the savepoint rolled back
            ctx.dek_current["ops_release"] = 1
            raise RuntimeError("bad stored value")
    assert [(e.action, e.outcome) for e in ctx.audit_events] == [
        ("ops_release_build", "allowed"),
        ("decrypt", "denied"),
    ]
    assert ctx.dek_memo == {("vault", 1): b"k" * 32} and ctx.dek_current == {"vault": 1}
    async with service.item_tx(ctx):  # success keeps what the item queued
        ctx.audit("ops_release_create", "allowed", "trip", TRIP)
    assert ctx.audit_events[-1].action == "ops_release_create"


@pytest.mark.asyncio
async def test_one_failing_trip_keeps_the_others_and_drops_its_notices(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bad, good = uuid.uuid4(), uuid.uuid4()

    async def today(_ctx: Any) -> dt.date:
        return dt.date(2026, 12, 1)

    async def sync_trip(ctx: Any, _vault: Any, trip_id: uuid.UUID, _on: dt.date, notices: Any = None) -> str:
        service._notify(notices, service._withdrawn(ctx, trip_id, "aa_lines_cancelled"))
        if trip_id == bad:
            raise ValueError("Invalid isoformat string")
        return "withdrawn"

    async def purge_due(_ctx: Any, _on: dt.date) -> int:
        return 1

    monkeypatch.setattr(service, "today", today)
    monkeypatch.setattr(service, "sync_trip", sync_trip)
    monkeypatch.setattr(service, "purge_due", purge_due)
    caplog.set_level(logging.INFO, logger="app.ops_release")
    notices: service.Notices = []
    counts = await job.run_agency(_ctx(), None, [bad, good], notices=notices)  # type: ignore[arg-type]
    assert dict(counts) == {"failed": 1, "withdrawn": 1, "purged": 1}
    assert [dict(n.fields)["ops_release_id"] for n in notices] == [good]
    assert not _lines(caplog, service.WITHDRAWN_EVENT)  # the job writes them after the agency's commit
    failed = [r.getMessage() for r in caplog.records if "ops_release_trip_failed" in r.getMessage()]
    assert failed == [f"ops_release_trip_failed tenant_id={TENANT} trip_id={bad} error_class=ValueError"]


class _Ctx:
    def __init__(self, fail_commit: bool) -> None:
        self.fail_commit = fail_commit

    async def __aenter__(self) -> Any:
        return _ctx()

    async def __aexit__(self, exc_type: object, *_: object) -> bool:
        if exc_type is None and self.fail_commit:
            raise ConnectionError("commit failed")
        return False


@pytest.mark.parametrize("fail_commit", [False, True])
@pytest.mark.parametrize("route", ["withdraw", "cancel"])
def test_the_routes_tell_aa_ops_only_after_the_commit(
    client: TestClient,
    pools: LocalPools,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    route: str,
    fail_commit: bool,
) -> None:
    """C12 step 6: a trip cancel and the agency's own withdrawal each write ``crm_ops_release_withdrawn`` (the Infra
    alarm), and only once the transaction committed."""
    from app import stages
    from app.routers import ops_release as ops_router
    from app.routers import pipeline as pipeline_router

    reason = "trip_cancelled" if route == "cancel" else "agency_withdrawn"

    async def withdraw(ctx: Any, trip_id: uuid.UUID, why: str, *, notices: Any = None) -> dict[str, Any]:
        service._notify(notices, service._withdrawn(ctx, trip_id, why))
        return {"trip_id": str(trip_id)}

    async def pool(_request: Any) -> object:
        return object()

    def tx(*_a: Any, **_k: Any) -> _Ctx:
        return _Ctx(fail_commit)

    for module in (ops_router, pipeline_router):
        monkeypatch.setattr(module, "request_pool", pool)
        monkeypatch.setattr(module, "tenant_tx", tx)
    monkeypatch.setattr(service, "withdraw", withdraw)
    if route == "cancel":

        async def staged_cancel(ctx: Any, trip_id: uuid.UUID, **kw: Any) -> dict[str, Any]:
            await withdraw(ctx, trip_id, "trip_cancelled", notices=kw["notices"])
            return {"stage": "cancelled"}

        monkeypatch.setattr(stages, "cancel", staged_cancel)
        path, body = f"/crm/v1/trips/{TRIP}/cancel?dry_run=false", {"cancel_reason_code": stages.CANCEL_REASONS[0]}
    else:
        path, body = f"/crm/v1/trips/{TRIP}/ops-release/withdraw", None
    caplog.set_level(logging.INFO, logger="app.ops_release.service")
    headers = {**_bearer(pools.partner_web()), "X-AA-Tenant-Id": TENANT}
    if fail_commit:
        with pytest.raises(ConnectionError):
            client.post(path, json=body, headers=headers)
        assert not _lines(caplog, service.WITHDRAWN_EVENT)
    else:
        assert client.post(path, json=body, headers=headers).status_code == 200
        assert _lines(caplog, service.WITHDRAWN_EVENT) == [
            f"crm_ops_release_withdrawn tenant_id={TENANT} ops_release_id={TRIP} reason={reason}"
        ]


@pytest.mark.asyncio
async def test_the_board_visits_every_registered_agency(monkeypatch: pytest.MonkeyPatch) -> None:
    """The registry lists every agency ever registered (offboarded and trashed too) in tenant_id order; the board once
    stopped at the 500th, so an agency whose id sorted later lost its releases and withdrawal notices on S-02."""
    ids = sorted(uuid.uuid4() for _ in range(700))
    visited: list[uuid.UUID] = []

    async def agencies(_pool: Any) -> list[Any]:
        return [registry.Agency(tenant_id=i, exported_until=dt.datetime.now(dt.UTC), verified_until=None, last_seq=0)
                for i in ids]  # fmt: skip

    class _Tx:
        def __init__(self, _pool: Any, _caller: Any, tenant_id: uuid.UUID) -> None:
            self.tenant_id = tenant_id

        async def __aenter__(self) -> Any:
            return self.tenant_id

        async def __aexit__(self, *_: object) -> bool:
            return False

    async def agency_board(tenant_id: uuid.UUID, _horizon: int) -> list[dict[str, Any]]:
        visited.append(tenant_id)
        return [{"start_date": None, "agency_name": "A", "trip_id": str(tenant_id)}]

    monkeypatch.setattr(registry, "agencies", agencies)
    monkeypatch.setattr(staff, "ops_tx", _Tx)
    monkeypatch.setattr(staff, "_agency_board", agency_board)
    got = await staff.board(None, None)  # type: ignore[arg-type]
    assert visited == ids and len(got["items"]) == len(ids)


# ── S262 AA-956 live evidence: the receipt said 4 views while the panel listed 3 ─────────────────────────────────────


def _purged_release() -> dict[str, Any]:
    at = dt.datetime(2026, 10, 31, 21, 4, tzinfo=dt.UTC)
    receipt = '{"manifest_exports": 4, "payload_version": 1, "reason": "withdrawn", "travellers": 1, "views": 4}'
    return {
        "ops_release_id": uuid.uuid4(), "trip_id": uuid.uuid4(), "payload_enc": None, "payload_version": 1,
        "fields_released": ["name"], "completeness": "{}", "complete": True, "traveller_count": 1,
        "departure_ids": [], "bk_booking_ids": [], "start_date": None, "end_date": None, "released_at": at,
        "refreshed_at": at, "purge_at": dt.date(2027, 2, 1), "t21_alerted_at": None, "withdrawn_at": at,
        "withdraw_reason": "trip_cancelled", "purged_at": at, "purge_reason": "withdrawn",
        "deletion_receipt": receipt, "view_count": 4, "last_viewed_at": at,
    }  # fmt: skip


def _activity(n: int) -> list[dict[str, Any]]:
    at = dt.datetime(2026, 10, 31, 20, 0, tzinfo=dt.UTC)
    return [
        {"at": at - dt.timedelta(minutes=i), "actor_kind": "aa_ops", "action": "ops_release_view", "reason": None}
        for i in range(n)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lines", "shown", "more"), [(9, 9, False), (service.MAX_ACTIVITY + 1, service.MAX_ACTIVITY, True)]
)
async def test_agency_view_returns_every_activity_line_up_to_the_cap_and_says_when_older_ones_exist(
    lines: int, shown: int, more: bool
) -> None:
    conn = (
        ScriptedConn()
        .on("SELECT 1 FROM crm.trip", 1)
        .on("FROM crm.ops_release WHERE trip_id", _purged_release())
        .on("AT TIME ZONE", dt.date(2026, 11, 1))
        .on("FROM crm.booking_line", 0)
        .on("FROM crm.audit_event", lambda trip, limit: _activity(lines)[:limit])
    )
    view = await service.agency_view(tenant_ctx(conn, role="agent"), uuid.uuid4())
    assert len(view["activity"]) == shown and view["activity_more"] is more
    assert conn.sql("FROM crm.audit_event")[0][1] == service.MAX_ACTIVITY + 1
    assert view["release"]["deletion_receipt"]["views"] == 4 and view["release"]["status"] == "purged"


@pytest.mark.asyncio
async def test_agency_view_gives_an_assistant_no_activity() -> None:
    conn = ScriptedConn().on("SELECT 1 FROM crm.trip", 1).on("FROM crm.booking_line", 0)
    view = await service.agency_view(tenant_ctx(conn, role="assistant"), uuid.uuid4())
    assert view["activity"] is None and view["activity_more"] is False and view["release"] is None
    assert conn.sql("FROM crm.audit_event") == []
