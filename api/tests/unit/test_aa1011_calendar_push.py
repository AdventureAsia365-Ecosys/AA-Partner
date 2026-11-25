"""AA-1011 part 3: the calendar push job (:mod:`app.calendar.push`), the connection service
(:mod:`app.calendar.connections`) and the routes, on scripted SQL and a fake calendar client (no network). The real
SQL, the vault, RLS and the token removal run on PostgreSQL in tests/integration/test_aa1011_calendar_db.py."""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import platform_settings as ps
from app.calendar import connections, oauth_sync, push
from app.calendar.oauth_sync import PushEvent, RemoteEvent
from app.errors import Conflict, Forbidden, NotFound
from app.jobs.payload_guard import check_payload
from app.jobs.queue import Job
from app.mail.providers import AuthRevoked, Rejected, Transient
from app.mail.state import MailRuntime
from app.main import create_app
from app.routers import calendar as router
from tests.tokens import LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.mail_fakes import MailVault, secret_blob
from tests.unit.slice_fakes import MEMBER, OTHER_MEMBER, TENANT, fake_tx, system_ctx, tenant_ctx

CONN = uuid.UUID("00000000-0000-4000-8000-00000000c011")
CRED = uuid.UUID("00000000-0000-4000-8000-00000000c012")
TRIP = "00000000-0000-4000-8000-00000000a001"
NOW = dt.datetime(2030, 3, 10, 3, 0, tzinfo=dt.UTC)
REFRESH = "rt-secret-value-0001"


def _event(key: str, day: dt.date = dt.date(2030, 3, 20)) -> PushEvent:
    return PushEvent(key, "Departure", "d", True, day, None, None, "UTC")


# ── plan ─────────────────────────────────────────────────────────────────────────────────────────


def test_plan_creates_updates_and_deletes() -> None:
    keep, change, new = _event("departure-a"), _event("departure-b"), _event("task-c")
    remote = [
        RemoteEvent("r1", "departure-a", keep.digest()),
        RemoteEvent("r2", "departure-b", "old"),
        RemoteEvent("r3", "task-gone", "x"),
        RemoteEvent("r4", "departure-a", keep.digest()),  # a second copy of one key
    ]
    got = push.plan([keep, change, new], remote)
    assert [e.key for e in got.creates] == ["task-c"]
    assert got.updates == [("r2", change)]
    assert got.deletes == ["r3", "r4"] and got.size == 4


def test_to_push_writes_no_title_and_links_back() -> None:
    tz = dt.UTC
    departure = {"kind": "departure", "id": f"departure-{TRIP}", "date": dt.date(2030, 3, 20), "all_day": True,
                 "trip_id": TRIP, "title": "Mr Example family trip"}  # fmt: skip
    pushed = push.to_push(departure, tz, "https://partner.test")
    assert pushed.summary == "Departure · trip 00000000" and "Example" not in pushed.summary + pushed.description
    assert pushed.description.endswith(f"/pipeline/{TRIP}")
    at = dt.datetime(2030, 3, 20, 9, tzinfo=tz)
    task = {"kind": "task", "id": "task-1", "date": at.date(), "starts_at": at, "all_day": False, "trip_id": None,
            "task_kind": "call_client"}  # fmt: skip
    pushed = push.to_push(task, tz, "https://partner.test")
    assert pushed.summary == "Task due: call client" and pushed.ends_at == at + dt.timedelta(minutes=15)
    assert pushed.description.endswith("/tasks")
    assert push.to_push(task, tz, "").description == "From your partner CRM"


# ── sync_connection ──────────────────────────────────────────────────────────────────────────────


class FakeCalendar:
    def __init__(self, remote: list[RemoteEvent] | None = None, error: Exception | None = None,
                 rotated: dict[str, Any] | None = None, configured: bool = True) -> None:  # fmt: skip
        self.remote = remote or []
        self.error = error
        self.rotated = rotated
        self.configured = configured
        self.calls: list[tuple[str, Any]] = []
        self.revoked: list[dict[str, Any]] = []

    async def access_token(self, secret: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        self.calls.append(("token", secret["refresh_token"]))
        return "at", self.rotated

    async def list_ours(self, token: str, connection_id: Any, since: Any, until: Any) -> list[RemoteEvent]:
        self.calls.append(("list", (since is None, until is None)))
        if self.error:
            raise self.error
        return list(self.remote)

    async def create(self, token: str, connection_id: Any, event: PushEvent) -> str:
        self.calls.append(("create", event.key))
        return "new"

    async def update(self, token: str, connection_id: Any, external: str, event: PushEvent) -> None:
        self.calls.append(("update", external))

    async def delete(self, token: str, external: str) -> None:
        self.calls.append(("delete", external))

    async def revoke(self, secret: dict[str, Any]) -> bool:
        self.revoked.append(secret)
        return True


def _row(status: str = "active", member_active: bool = True, scope: str = "mine") -> dict[str, Any]:
    return {"calendar_connection_id": CONN, "member_id": MEMBER, "provider": "google", "scope": scope,
            "status": status, "credential_id": CRED, "member_active": member_active,
            "secret_enc": secret_blob(refresh_token=REFRESH, address="lan@example.test")}  # fmt: skip


def _setup(monkeypatch: pytest.MonkeyPatch, row: dict[str, Any] | None, items: list[dict[str, Any]] | None = None):  # noqa: ANN202
    conn = ScriptedConn()
    conn.on("JOIN crm.integration_credential ic", row)
    conn.on("SELECT coalesce((SELECT timezone", "UTC")
    monkeypatch.setattr(push, "system_tx", fake_tx(system_ctx(conn)))
    monkeypatch.setattr(push, "Vault", lambda keys, ctx: MailVault())
    seen: dict[str, Any] = {}

    async def fake_events(ctx: Any, *, start: dt.date, end: dt.date, member_id: Any, tz: Any) -> list[dict[str, Any]]:
        seen.update(start=start, end=end, member_id=member_id)
        return list(items or [])

    monkeypatch.setattr(push.calendar_events, "events", fake_events)
    return conn, seen


def _runtime(client: Any) -> push.Runtime:
    return push.Runtime(keys=object(), clients={"google": client}, web_origin="https://partner.test")  # type: ignore[arg-type]


DEPARTURE = {"kind": "departure", "id": "departure-a", "date": dt.date(2030, 3, 20), "all_day": True, "trip_id": TRIP}
RETURN = {"kind": "return", "id": "return-a", "date": dt.date(2030, 3, 25), "all_day": True, "trip_id": TRIP}


@pytest.mark.asyncio
async def test_push_writes_the_missing_events_in_the_window_and_records_the_result(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    conn, seen = _setup(monkeypatch, _row(), [DEPARTURE, RETURN])
    client = FakeCalendar(
        remote=[RemoteEvent("r9", "task-gone", "x")], rotated={"refresh_token": "rt-2", "address": "a"}
    )
    caplog.set_level(logging.DEBUG)
    out = await push.sync_connection(conn, _runtime(client), TENANT, CONN, now=NOW)  # type: ignore[arg-type]
    assert out == {"outcome": "pushed", "left_count": 0, "created_count": 1, "deleted_count": 1}
    assert ("create", "departure-a") in client.calls and ("delete", "r9") in client.calls
    assert not any(c == ("create", "return-a") for c in client.calls)  # only departures and tasks
    assert seen == {"start": dt.date(2030, 3, 3), "end": dt.date(2030, 7, 8), "member_id": MEMBER}
    assert (
        conn.sql("UPDATE crm.integration_credential SET secret_enc")[0][1]
        == b"secret:" + b'{"refresh_token": "rt-2", "address": "a"}'
    )
    assert conn.sql("UPDATE crm.calendar_connection")[0] == (CONN, None, 1)
    assert REFRESH not in caplog.text and "rt-2" not in caplog.text


@pytest.mark.asyncio
async def test_agency_scope_and_the_batch_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    items = [dict(DEPARTURE, id=f"departure-{i}") for i in range(5)]
    conn, seen = _setup(monkeypatch, _row(scope="agency"), items)
    real_get = ps.get
    monkeypatch.setattr(push.ps, "get", lambda key, *a: 2 if key == ps.CALENDAR_PUSH_BATCH_SIZE else real_get(key))
    out = await push.sync_connection(conn, _runtime(FakeCalendar()), TENANT, CONN, now=NOW)  # type: ignore[arg-type]
    assert out["created_count"] == 2 and out["left_count"] == 3 and seen["member_id"] is None


@pytest.mark.asyncio
async def test_a_disconnect_removes_every_event_revokes_and_deletes_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    conn, _ = _setup(monkeypatch, _row(status="revoked"))
    client = FakeCalendar(remote=[RemoteEvent("r1", "departure-a", "h"), RemoteEvent("r2", "task-1", "h")])
    out = await push.sync_connection(conn, _runtime(client), TENANT, CONN, now=NOW)  # type: ignore[arg-type]
    assert out["outcome"] == "removed" and out["deleted_count"] == 2
    assert ("list", (True, True)) in client.calls  # every date, not only the window
    assert client.revoked and client.revoked[0]["refresh_token"] == REFRESH
    assert conn.sql("DELETE FROM crm.integration_credential") == [(CRED,)]


@pytest.mark.asyncio
async def test_a_member_who_left_is_cleaned_up_like_a_disconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    conn, _ = _setup(monkeypatch, _row(member_active=False))
    out = await push.sync_connection(conn, _runtime(FakeCalendar()), TENANT, CONN, now=NOW)  # type: ignore[arg-type]
    assert out["outcome"] == "removed" and conn.sql("DELETE FROM crm.integration_credential") == [(CRED,)]


@pytest.mark.asyncio
async def test_a_removal_retries_an_outage_until_its_last_attempt_then_deletes_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, _ = _setup(monkeypatch, _row(status="revoked"))
    client = FakeCalendar(error=Transient("unreachable"))
    with pytest.raises(Transient):
        await push.sync_connection(conn, _runtime(client), TENANT, CONN, now=NOW, final_attempt=False)  # type: ignore[arg-type]
    assert conn.sql("DELETE FROM crm.integration_credential") == []
    out = await push.sync_connection(conn, _runtime(client), TENANT, CONN, now=NOW, final_attempt=True)  # type: ignore[arg-type]
    assert out == {"outcome": "removed_with_error", "reason_code": "unreachable"}
    assert conn.sql("DELETE FROM crm.integration_credential") == [(CRED,)]
    conn2, _ = _setup(monkeypatch, _row(status="revoked"))
    out = await push.sync_connection(conn2, _runtime(FakeCalendar(error=AuthRevoked("auth_revoked"))), TENANT, CONN,
                                     now=NOW, final_attempt=False)  # type: ignore[arg-type]  # fmt: skip
    assert out["outcome"] == "removed_with_error" and conn2.sql("DELETE FROM crm.integration_credential") == [(CRED,)]


@pytest.mark.parametrize(
    ("error", "outcome", "code"),
    [
        (AuthRevoked("auth_revoked"), "auth_failed", "auth_revoked"),
        (AuthRevoked("permission_missing"), "auth_failed", "permission_missing"),
        (Transient("token_endpoint"), "failed", "unreachable"),
        (Rejected("provider_rejected"), "failed", "vendor_rejected"),
    ],
)
@pytest.mark.asyncio
async def test_a_refused_push_keeps_the_connection_with_its_failure_class(
    monkeypatch: pytest.MonkeyPatch, error: Exception, outcome: str, code: str
) -> None:
    conn, _ = _setup(monkeypatch, _row(), [DEPARTURE])
    out = await push.sync_connection(conn, _runtime(FakeCalendar(error=error)), TENANT, CONN, now=NOW)  # type: ignore[arg-type]
    assert out == {"outcome": outcome, "reason_code": code}
    assert conn.sql("UPDATE crm.calendar_connection")[0] == (CONN, code, None)
    assert conn.sql("DELETE FROM crm.integration_credential") == []


@pytest.mark.asyncio
async def test_gone_and_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    conn, _ = _setup(monkeypatch, None)
    assert await push.sync_connection(conn, _runtime(FakeCalendar()), TENANT, CONN, now=NOW) == {"outcome": "gone"}  # type: ignore[arg-type]
    conn, _ = _setup(monkeypatch, _row())
    off = FakeCalendar(configured=False)
    assert (await push.sync_connection(conn, _runtime(off), TENANT, CONN, now=NOW))["outcome"] == "not_configured"  # type: ignore[arg-type]
    conn, _ = _setup(monkeypatch, _row(status="revoked"))
    out = await push.sync_connection(conn, _runtime(off), TENANT, CONN, now=NOW)  # type: ignore[arg-type]
    assert out["outcome"] == "removed_not_configured" and conn.sql("DELETE FROM crm.integration_credential")


# ── the handler, the queue rows, the schedule ────────────────────────────────────────────────────


def _job(payload: dict[str, Any], attempt: int = 1, max_attempts: int = 3) -> Job:
    return Job(id="1", kind="crm_calendar_push", payload=payload, attempt=attempt, max_attempts=max_attempts)


@pytest.mark.asyncio
async def test_handler_runs_one_connection_or_sweeps_every_agency(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []

    async def fake_sync(pool: Any, runtime: Any, tenant: Any, connection: Any, *, now: Any, final_attempt: bool = True):  # noqa: ANN202
        calls.append((tenant, connection, final_attempt))
        if connection == CONN:
            return {"outcome": "pushed"}
        if connection == CRED:
            raise Forbidden("tenant_locked")
        raise RuntimeError("boom")

    monkeypatch.setattr(push, "sync_connection", fake_sync)
    handler = push.build_handler(_runtime(FakeCalendar()))
    one = {"tenant_id": str(TENANT), "calendar_connection_id": str(CONN), "mode": "connect"}
    assert await handler(object(), _job(one, attempt=3)) == {"status": "ok", "outcome": "pushed"}  # type: ignore[arg-type]
    assert calls[-1] == (TENANT, CONN, True)
    locked = dict(one, calendar_connection_id=str(CRED))
    assert (await handler(object(), _job(locked)))["outcome"] == "skipped_agency"  # type: ignore[arg-type]

    class Agency:
        tenant_id = TENANT

    async def agencies(pool: Any) -> list[Any]:
        return [Agency(), type("Gone", (), {"tenant_id": uuid.uuid4()})()]

    async def ids(pool: Any, tenant: Any) -> list[uuid.UUID]:
        if tenant != TENANT:
            raise NotFound("tenant_not_found")
        return [CONN, CRED, uuid.UUID(int=7)]

    monkeypatch.setattr(push.registry, "agencies", agencies)
    monkeypatch.setattr(push, "_connections", ids)
    out = await handler(object(), _job({"purpose": "slot_schedule"}, max_attempts=1))  # type: ignore[arg-type]
    assert out == {"status": "partial", "pushed_count": 1, "skipped_agency_count": 1, "error_count": 1,
                   "reason_code": "RuntimeError"}  # fmt: skip
    check_payload(out)
    assert await push.build_handler(push.Runtime(keys=None, clients={}))(object(), _job({})) == {  # type: ignore[arg-type]
        "status": "not_configured"
    }


@pytest.mark.asyncio
async def test_queue_rows_hold_ids_only_and_the_slot_is_queued_once() -> None:
    conn = ScriptedConn()
    await push.enqueue_connection(conn, TENANT, CONN, "disconnect")
    (kind, payload, key, attempts, by, _delay) = conn.sql("INSERT INTO crm.job")[0]
    assert kind == "crm_calendar_push" and key.startswith(f"crm_calendar_push:{CONN}:disconnect:")
    assert attempts == ps.get(ps.CALENDAR_PUSH_MAX_ATTEMPTS) == 3 and "secret" not in payload
    with pytest.raises(ValueError):
        await push.enqueue_connection(conn, TENANT, CONN, "sync")
    hook = push.HourlyCalendarPush(lambda: NOW)
    await hook(conn)  # type: ignore[arg-type]
    assert await hook(conn) is None  # type: ignore[arg-type]
    slots = [args for args in conn.sql("INSERT INTO crm.job") if args[2].startswith("crm_calendar_push:slot:")]
    assert len(slots) == 1 and slots[0][3] == 1


# ── the connection service ───────────────────────────────────────────────────────────────────────

ROW = {"calendar_connection_id": CONN, "member_id": MEMBER, "provider": "google", "scope": "mine", "status": "active",
       "address_enc": b"enc:calendar_connection.address_enc:lan@example.test", "failure_class": None,
       "last_sync_at": NOW, "last_ok_at": NOW, "event_count": 4, "revoked_at": None, "created_at": NOW}  # fmt: skip


@pytest.mark.asyncio
async def test_view_never_returns_a_token() -> None:
    conn = ScriptedConn().on("WHERE c.member_id = $1", [ROW])
    out = await connections.view(tenant_ctx(conn), MailVault(), {"google": FakeCalendar()})
    assert out["providers"] == {"google": True, "microsoft": False} and out["kinds"] == ["departure", "task"]
    (item,) = out["connections"]
    assert item["address"] == "lan@example.test" and item["event_count"] == 4
    assert not any("secret" in k or k.endswith("_enc") for k in item)


@pytest.mark.asyncio
async def test_connect_seals_the_token_and_queues_a_push() -> None:
    conn = ScriptedConn().on("WHERE c.calendar_connection_id = $1", ROW)
    ctx = tenant_ctx(conn, role="agent")
    out = await connections.connect(ctx, MailVault(), provider="google", scope="mine",
                                    secret={"refresh_token": REFRESH, "address": "Lan@Example.test"})  # fmt: skip
    (cred_id, provider, member, sealed) = conn.sql("INSERT INTO crm.integration_credential")[0]
    assert provider == "google_calendar" and member == MEMBER and sealed.startswith(b"secret:")
    inserted = conn.sql("INSERT INTO crm.calendar_connection")[0]
    assert inserted[2:5] == ("google", "mine", cred_id) and b"lan@example.test" in inserted[5]
    assert conn.sql("INSERT INTO crm.job")[0][0] == "crm_calendar_push"
    assert out["connection"]["calendar_connection_id"] == str(CONN)
    assert [a.action for a in ctx.audit_events] == ["calendar_connect"]


@pytest.mark.asyncio
async def test_reconnect_reuses_the_rows_and_a_pending_disconnect_is_409() -> None:
    conn = ScriptedConn().on("WHERE c.calendar_connection_id = $1", ROW)
    conn.on("WHERE member_id = $1 AND provider = $2", {"calendar_connection_id": CONN, "status": "active",
                                                        "credential_id": CRED})  # fmt: skip
    conn.on("SELECT integration_credential_id FROM crm.integration_credential", CRED)
    await connections.connect(tenant_ctx(conn), MailVault(), provider="google", scope="agency",
                              secret={"refresh_token": "rt-3", "address": "a@example.test"})  # fmt: skip
    assert conn.sql("UPDATE crm.integration_credential")[0][0] == CRED
    assert conn.sql("UPDATE crm.calendar_connection SET scope")[0][:2] == (CONN, "agency")
    assert conn.sql("INSERT INTO crm.calendar_connection") == []
    revoked = {"calendar_connection_id": CONN, "status": "revoked", "credential_id": CRED}
    pending = ScriptedConn().on("WHERE member_id = $1 AND provider = $2", revoked)
    with pytest.raises(Conflict, match="cleaned up"):
        await connections.connect(tenant_ctx(pending), MailVault(), provider="google", scope="mine",
                                  secret={"refresh_token": "x", "address": "a@example.test"})  # fmt: skip


@pytest.mark.asyncio
async def test_disconnect_own_or_owner_only() -> None:
    conn = ScriptedConn().on("WHERE c.calendar_connection_id = $1", dict(ROW, status="revoked"))
    conn.on("FOR UPDATE", {"member_id": MEMBER, "status": "active"})
    ctx = tenant_ctx(conn, role="agent")
    out = await connections.disconnect(ctx, MailVault(), CONN)
    assert out["connection"]["status"] == "revoked"
    assert conn.sql("SET status = 'revoked'")[0] == (CONN, f"member:{MEMBER}")
    assert conn.sql("INSERT INTO crm.job")[0][1].count("disconnect") == 1
    other = ScriptedConn().on("FOR UPDATE", {"member_id": MEMBER, "status": "active"})
    with pytest.raises(Forbidden):
        await connections.disconnect(tenant_ctx(other, role="agent", member=OTHER_MEMBER), MailVault(), CONN)
    owner = ScriptedConn().on("FOR UPDATE", {"member_id": MEMBER, "status": "active"}).on(
        "WHERE c.calendar_connection_id = $1", ROW)  # fmt: skip
    await connections.disconnect(tenant_ctx(owner, role="owner", member=OTHER_MEMBER), MailVault(), CONN)
    assert owner.sql("SET status = 'revoked'")
    with pytest.raises(NotFound):
        await connections.disconnect(tenant_ctx(ScriptedConn()), MailVault(), CONN)


# ── routes ───────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def pools() -> LocalPools:
    return LocalPools()


async def _pool(_request: Any) -> object:
    return object()


def test_routes_reach_their_helpers(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = pools.settings()
    runtime = MailRuntime(keys=object(), calendars={"google": FakeCalendar()})  # type: ignore[arg-type]
    app = create_app(settings, verifier=pools.verifier(settings), keys=object(), mail_runtime=runtime)
    monkeypatch.setattr(router, "request_pool", _pool)
    monkeypatch.setattr(router, "tenant_tx", fake_tx(tenant_ctx(ScriptedConn(), role="assistant")))
    monkeypatch.setattr(router, "Vault", lambda keys, ctx: MailVault())
    monkeypatch.setattr(router, "require_keys", lambda keys: keys)
    calls: list[str] = []

    def stub(name: str) -> Any:
        async def fn(*args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            if name == "complete":
                return oauth_sync.Completed("google", "mine", {"refresh_token": "x", "address": "a@example.test"})
            return {"ok": name}

        return fn

    for module, name in ((connections, "view"), (connections, "connect"), (connections, "disconnect"),
                         (oauth_sync, "start"), (oauth_sync, "complete")):  # fmt: skip
        monkeypatch.setattr(module, name, stub(name))
    headers = {"Authorization": f"Bearer {pools.partner_web()}", "X-AA-Tenant-Id": str(TENANT)}
    with TestClient(app) as client:
        assert client.get("/crm/v1/calendar/connections", headers=headers).json() == {"ok": "view"}
        body = {"provider": "google", "scope": "mine"}
        assert client.post("/crm/v1/calendar/connections/oauth/start", json=body, headers=headers).status_code == 200
        done = client.post("/crm/v1/calendar/connections/oauth/complete", json={"code": "c", "state": "cal.x"},
                           headers=headers)  # fmt: skip
        assert done.status_code == 201 and done.json() == {"ok": "connect"}
        assert client.post(f"/crm/v1/calendar/connections/{CONN}/disconnect", headers=headers).status_code == 200
        bad = client.post("/crm/v1/calendar/connections/oauth/start", json={"provider": "apple"}, headers=headers)
        assert bad.status_code == 422
    assert calls == ["view", "start", "complete", "connect", "disconnect"]
