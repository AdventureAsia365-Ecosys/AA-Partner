"""AA-968 (card CRM-36) routes, service, send pass and unsubscribe on a scripted connection: each route reaches its
helper with the validated body and the role matrix refuses before any read; the service's refusals (feature key, BYO
mailbox, version, gate, audience) and its writes; the send pass's holds and its per-client checks; the public
unsubscribe. SQL, RLS and the real encryption are tests/integration/test_aa968_campaigns_db.py. Invented names only."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.campaigns import audience, send, service, unsubscribe
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.jobs.queue import Job
from app.main import create_app
from app.routers import campaigns as campaigns_router
from app.routers import public as public_router
from tests.tokens import LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import MEMBER, OTHER_MEMBER, TENANT, FakeVault, fake_tx, system_ctx, tenant_ctx

CAMPAIGN = uuid.UUID("00000000-0000-4000-8000-0000000c4a01")
MAILBOX = uuid.UUID("00000000-0000-4000-8000-0000000c4a02")
CLIENT_A = uuid.UUID("00000000-0000-4000-8000-0000000c4a03")
CLIENT_B = uuid.UUID("00000000-0000-4000-8000-0000000c4a04")
RECIPIENT = uuid.UUID("00000000-0000-4000-8000-0000000c4a05")
TOUR = "3e7a91c4-58b2-4f0d-a6e1-9c2b7d4f8a16"
ORIGIN = "https://go.crm.example.test"
LINKS = audience.Links(client_origin=ORIGIN, tripplanner_origin=None)
TODAY = dt.date(2027, 3, 1)
TOKEN = "t" * 43


# ── routes ───────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def pools() -> LocalPools:
    return LocalPools()


async def _pool(_request: Any) -> object:
    return object()


def _client(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, role: str) -> TestClient:
    settings = pools.settings()
    app = create_app(settings, verifier=pools.verifier(settings), keys=object())
    monkeypatch.setattr(campaigns_router, "request_pool", _pool)
    monkeypatch.setattr(campaigns_router, "tenant_tx", fake_tx(tenant_ctx(ScriptedConn(), role=role)))
    monkeypatch.setattr(campaigns_router, "Vault", lambda keys, ctx: FakeVault())
    monkeypatch.setattr(campaigns_router, "require_keys", lambda keys: keys)
    return TestClient(app)


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-AA-Tenant-Id": str(TENANT)}


def _stubs(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    calls: list[tuple[str, Any]] = []

    def stub(name: str) -> Any:
        async def fn(*args: Any, **kwargs: Any) -> dict[str, Any]:
            calls.append((name, {"args": args[1:], **kwargs}))
            return {"ok": name}

        return fn

    for name in ("listing", "options", "tours", "block", "create", "detail", "update", "delete", "preview",
                 "schedule", "cancel"):  # fmt: skip
        monkeypatch.setattr(service, name, stub(name))
    return calls


@pytest.fixture
def client(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    with _client(pools, monkeypatch, "owner") as c:
        yield c


def test_routes_reach_their_helpers(client: TestClient, pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stubs(monkeypatch)
    h = _headers(pools.partner_web())
    body = {"name": "Spring", "subject": "Hi", "body": "Hello", "aa_tour_ids": [TOUR], "audience_tags": ["vip"]}
    responses = [
        client.get("/crm/v1/campaigns", headers=h),
        client.get("/crm/v1/campaigns/options", headers=h),
        client.get("/crm/v1/campaigns/tours?q=river", headers=h),
        client.get(f"/crm/v1/campaigns/blocks/{CAMPAIGN}", headers=h),
        client.post("/crm/v1/campaigns", json=body, headers=h),
        client.get(f"/crm/v1/campaigns/{CAMPAIGN}", headers=h),
        client.patch(f"/crm/v1/campaigns/{CAMPAIGN}", json={**body, "version": 3}, headers=h),
        client.delete(f"/crm/v1/campaigns/{CAMPAIGN}", headers=h),
        client.post(f"/crm/v1/campaigns/{CAMPAIGN}/preview", headers=h),
        client.post(f"/crm/v1/campaigns/{CAMPAIGN}/schedule", json={"expected": 4, "version": 2}, headers=h),
        client.post(f"/crm/v1/campaigns/{CAMPAIGN}/cancel", json={"version": 2}, headers=h),
    ]
    assert [r.status_code for r in responses] == [200, 200, 200, 200, 201] + [200] * 6
    names = [c[0] for c in calls]
    assert names == ["listing", "options", "tours", "block", "create", "detail", "update", "delete", "preview",
                     "schedule", "cancel"]  # fmt: skip
    assert calls[2][1]["args"] == ("river",)
    assert calls[4][1]["args"][1]["aa_tour_ids"] == [TOUR] and calls[4][1]["args"][2] == audience.Links(
        client_origin=None, tripplanner_origin=None
    )
    assert calls[6][1]["args"][3] == 3 and "version" not in calls[6][1]["args"][2]
    assert calls[9][1]["expected"] == 4 and calls[9][1]["version"] == 2 and calls[9][1]["at"] is None
    bad = [
        client.post("/crm/v1/campaigns", json={**body, "extra": 1}, headers=h),
        client.post(f"/crm/v1/campaigns/{CAMPAIGN}/schedule", json={"expected": -1, "version": 1}, headers=h),
        client.post(f"/crm/v1/campaigns/{CAMPAIGN}/cancel", json={}, headers=h),
    ]
    assert [r.status_code for r in bad] == [422] * 3


@pytest.mark.parametrize(("role", "code"), [("agent", 200), ("assistant", 403)])
def test_roles(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, role: str, code: int) -> None:
    _stubs(monkeypatch)
    with _client(pools, monkeypatch, role) as c:
        h = _headers(pools.partner_web())
        assert c.get("/crm/v1/campaigns", headers=h).status_code == code
        assert c.post(f"/crm/v1/campaigns/{CAMPAIGN}/preview", headers=h).status_code == code


def test_public_unsubscribe_routes(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def state(_pool: Any, token: str) -> dict[str, Any]:
        calls.append(f"state:{token}")
        return {"agency": "Sen Vang", "subscribed": True}

    async def unsub(_pool: Any, _keys: Any, token: str) -> dict[str, Any]:
        calls.append(f"unsub:{token}")
        return {"agency": "Sen Vang", "subscribed": False}

    async def resub(_pool: Any, _keys: Any, token: str) -> dict[str, Any]:
        calls.append(f"resub:{token}")
        return {"agency": "Sen Vang", "subscribed": True}

    monkeypatch.setattr(unsubscribe, "state", state)
    monkeypatch.setattr(unsubscribe, "unsubscribe", unsub)
    monkeypatch.setattr(unsubscribe, "resubscribe", resub)
    monkeypatch.setattr(public_router, "request_pool", _pool)
    monkeypatch.setattr(public_router, "require_keys", lambda keys: keys)
    settings = pools.settings()
    with TestClient(create_app(settings, verifier=pools.verifier(settings), keys=object())) as c:
        got = c.get(f"/crm/public/v1/unsubscribe/{TOKEN}")
        assert got.status_code == 200 and got.headers["cache-control"] == "no-store"
        assert c.post(f"/crm/public/v1/unsubscribe/{TOKEN}", content=b"List-Unsubscribe=One-Click").json() == {
            "agency": "Sen Vang", "subscribed": False}  # fmt: skip
        assert c.post(f"/crm/public/v1/unsubscribe/{TOKEN}/resubscribe").status_code == 200
        assert c.get("/crm/public/v1/unsubscribe/short").status_code == 422
    assert calls == [f"state:{TOKEN}", f"unsub:{TOKEN}", f"resub:{TOKEN}"]


# ── service ──────────────────────────────────────────────────────────────────────────────────────


def _campaign(**over: Any) -> dict[str, Any]:
    base = {
        "campaign_id": CAMPAIGN,
        "name": "Spring",
        "locale": "en",
        "subject_enc": b"enc:campaign.subject_enc:Spring on the river",
        "body_enc": f"enc:campaign.body_enc:See {ORIGIN}/trip/{TOUR}".encode(),
        "channel_account_id": MAILBOX,
        "aa_tour_ids": [TOUR],
        "aa_content_id": None,
        "brand_only": False,
        "audience_tags": [],
        "state": "draft",
        "hold_code": None,
        "gate_codes": [],
        "scheduled_at": None,
        "started_at": None,
        "finished_at": None,
        "daily_limit": None,
        "recipient_count": 0,
        "excluded": "{}",
        "version": 2,
        "created_at": dt.datetime(2027, 2, 1, tzinfo=dt.UTC),
        "created_by_member_id": MEMBER,
    }
    return {**base, **over}


def _audience_row(tid: uuid.UUID, **over: Any) -> dict[str, Any]:
    return {"traveler_id": tid, "email_bidx": b"x" * 16, "has_email": True, "marketing": True, "suppressed": False,
            **over}  # fmt: skip


def _conn(*, allowed: bool = True, campaign: dict[str, Any] | None = None, people: list[Any] | None = None) -> Any:
    conn = ScriptedConn()
    conn.on("allowed_features", allowed)
    conn.on("FROM crm.campaign WHERE campaign_id", campaign or _campaign())
    conn.on("FROM crm.channel_account WHERE channel_account_id", {"channel_account_id": MAILBOX, "member_id": None})
    conn.on("FROM crm.aa_catalog_tour WHERE source_system = 'cis' AND source_id = ANY",
            [{"source_id": TOUR, "served_state": "served"}])  # fmt: skip
    conn.on("FROM crm.traveler t WHERE t.deleted_at IS NULL", people if people is not None else [])
    conn.on("SELECT (now() AT TIME ZONE", TODAY)
    conn.on("SELECT ($1::timestamptz AT TIME ZONE", TODAY)
    conn.on("UPDATE crm.campaign SET state = 'scheduled'", 3)
    conn.on("UPDATE crm.campaign SET state = 'cancelled'", 3)
    conn.on("UPDATE crm.campaign SET name", 3)
    return conn


@pytest.fixture
def limits(monkeypatch: pytest.MonkeyPatch) -> None:
    async def two(_ctx: Any) -> int:
        return 2

    async def cap(_ctx: Any) -> int:
        return 3

    monkeypatch.setattr(audience, "daily_limit", two)
    monkeypatch.setattr(audience, "max_recipients", cap)


def _fields(**over: Any) -> dict[str, Any]:
    return {
        "name": "  Spring   river ",
        "subject": "Spring",
        "body": f"See {ORIGIN}/trip/{TOUR}",
        "aa_tour_ids": [TOUR],
        "channel_account_id": str(MAILBOX),
        **over,
    }


@pytest.mark.asyncio
async def test_create_needs_the_feature_and_checks_fields() -> None:
    ctx = tenant_ctx(_conn(allowed=False))
    with pytest.raises(Forbidden, match="not on this agency's plan"):
        await service.create(ctx, FakeVault(), _fields(), LINKS)
    ctx = tenant_ctx(_conn())
    made = await service.create(ctx, FakeVault(), _fields(), LINKS)
    assert made["gate_codes"] == [] and made["state"] == "draft"
    insert = ctx.conn.sql("INSERT INTO crm.campaign")[0]
    assert insert[1] == "Spring river" and insert[6] == [TOUR] and insert[10] == [] and insert[11] == MEMBER
    for bad, code in (
        ({"name": ""}, "invalid_name"),
        ({"locale": "fr"}, "invalid_locale"),
        ({"subject": "x" * 400}, "invalid_subject"),
        ({"body": "x" * 30_000}, "invalid_body"),
        ({"aa_tour_ids": ["bad\x00id"]}, "invalid_aa_tour_ids"),
        ({"audience_tags": ["Bad Tag"]}, "invalid_audience_tags"),
    ):
        with pytest.raises(Invalid) as err:
            await service.create(tenant_ctx(_conn()), FakeVault(), _fields(**bad), LINKS)
        assert err.value.code == code


@pytest.mark.asyncio
async def test_refs_are_the_agencys_own() -> None:
    conn = _conn().on("FROM crm.channel_account WHERE channel_account_id", {"member_id": OTHER_MEMBER})
    with pytest.raises(Invalid, match="agency mailbox or your own"):
        await service.create(tenant_ctx(conn), FakeVault(), _fields(), LINKS)
    conn = _conn().on("SELECT array_agg(code) FROM crm.tag", ["vip"])
    with pytest.raises(Invalid) as err:
        await service.create(tenant_ctx(conn), FakeVault(), _fields(audience_tags=["vip", "gone"]), LINKS)
    assert err.value.code == "tag_not_found"
    with pytest.raises(Invalid) as err:
        await service.create(tenant_ctx(_conn()), FakeVault(), _fields(aa_content_id=str(CAMPAIGN)), LINKS)
    assert err.value.code == "aa_content_not_found"


@pytest.mark.asyncio
async def test_update_and_delete_follow_the_state() -> None:
    with pytest.raises(Conflict, match="reload"):
        await service.update(tenant_ctx(_conn()), FakeVault(), CAMPAIGN, _fields(), 1, LINKS)
    with pytest.raises(Conflict) as err:
        await service.update(tenant_ctx(_conn(campaign=_campaign(state="sent"))), FakeVault(), CAMPAIGN, _fields(), 2,
                             LINKS)  # fmt: skip
    assert err.value.code == "campaign_not_editable"
    out = await service.update(tenant_ctx(_conn()), FakeVault(), CAMPAIGN, _fields(), 2, LINKS)
    assert out["version"] == 3
    with pytest.raises(Conflict):
        await service.delete(tenant_ctx(_conn(campaign=_campaign(state="scheduled"))), CAMPAIGN)
    assert (await service.delete(tenant_ctx(_conn()), CAMPAIGN))["deleted"] is True
    with pytest.raises(NotFound):
        await service.delete(tenant_ctx(_conn().on("FROM crm.campaign WHERE campaign_id", None)), CAMPAIGN)


@pytest.mark.asyncio
@pytest.mark.usefixtures("limits")
async def test_schedule_refusals_and_the_plan() -> None:
    people = [_audience_row(CLIENT_A), _audience_row(CLIENT_B), _audience_row(RECIPIENT, marketing=None)]

    async def call(conn: Any, expected: int = 2, links: audience.Links = LINKS) -> dict[str, Any]:
        return await service.schedule(tenant_ctx(conn), FakeVault(), CAMPAIGN, at=dt.datetime(2027, 3, 1, 8),
                                      expected=expected, version=2, links=links)  # fmt: skip

    with pytest.raises(Conflict, match="campaign links"):
        await call(_conn(people=people), links=audience.Links(None, None))
    with pytest.raises(Conflict) as err:
        await call(_conn(campaign=_campaign(channel_account_id=None), people=people))
    assert err.value.code == "mailbox_required"
    gated = _campaign(body_enc=b"enc:campaign.body_enc:Deals at https://other.example.test/x")
    with pytest.raises(Invalid) as err:
        await call(_conn(campaign=gated, people=people))
    assert err.value.code == "content_gate" and err.value.extra["codes"] == ["link_not_allowed"]
    with pytest.raises(Conflict) as err:
        await call(_conn(people=[]))
    assert err.value.code == "audience_empty"
    many = [_audience_row(uuid.UUID(int=i + 1)) for i in range(4)]
    with pytest.raises(Conflict) as err:
        await call(_conn(people=many), expected=4)
    assert err.value.code == "audience_over_cap"
    with pytest.raises(Conflict) as err:
        await call(_conn(people=people), expected=5)
    assert err.value.code == "count_changed"
    conn = _conn(people=people)
    out = await call(conn)
    assert (out["eligible"], out["excluded"], out["days"], out["daily_limit"]) == (2, {"no_consent": 1}, 1, 2)
    [(cid, ids, days)] = conn.sql("INSERT INTO crm.campaign_recipient")
    assert cid == CAMPAIGN and ids == [CLIENT_A, CLIENT_B] and days == [TODAY, TODAY]


@pytest.mark.asyncio
@pytest.mark.usefixtures("limits")
async def test_preview_listing_detail_options() -> None:
    conn = _conn(people=[_audience_row(CLIENT_A), _audience_row(CLIENT_B, suppressed=True)])
    seen = await service.preview(tenant_ctx(conn), FakeVault(), CAMPAIGN, LINKS)
    assert (seen["eligible"], seen["excluded"], seen["days"], seen["over_cap"]) == (1, {"suppressed": 1}, 1, False)
    listed = _campaign(planned=1, queued=2, skipped=0, sent=2, unsubscribed=1)
    conn = _conn().on("FROM crm.campaign c WHERE c.deleted_at IS NULL", [listed])
    out = await service.listing(tenant_ctx(conn))
    assert out["feature_allowed"] is True and out["items"][0]["counts"]["unsubscribed"] == 1
    conn = _conn().on("SELECT source_id, title, served_state", [{"source_id": TOUR, "title": "River", "served_state":
                                                                  "served"}])  # fmt: skip
    one = await service.detail(tenant_ctx(conn), FakeVault(), CAMPAIGN)
    assert one["subject"] == "Spring on the river" and one["aa_tours"][0]["title"] == "River"
    conn = _conn().on("FROM crm.channel_account WHERE deleted_at IS NULL", [
        {"channel_account_id": MAILBOX, "member_id": None, "kind": "gmail", "status": "connected",
         "address_enc": b"enc:channel_account.address_enc:hello@agency.example.test"}])  # fmt: skip
    opts = await service.options(tenant_ctx(conn), FakeVault(), LINKS)
    assert opts["mailboxes"][0]["scope"] == "agency" and opts["daily_limit"] == 2 and opts["max_recipients"] == 3
    with pytest.raises(NotFound):
        await service.block(tenant_ctx(_conn()), CAMPAIGN)
    conn = _conn().on(
        "FROM crm.aa_content WHERE content_id", {"locale": "en", "title": "Spring", "body_markdown": "Hi"}
    )
    assert (await service.block(tenant_ctx(conn), CAMPAIGN))["body"] == "Hi"
    conn = _conn()
    await service.tours(tenant_ctx(conn), "50%_off")
    assert conn.sql("FROM crm.aa_catalog_tour WHERE source_system = 'cis' AND served_state")[0] == ("50\\%\\_off",)


@pytest.mark.asyncio
async def test_cancel_skips_the_plan_and_undoes_what_is_still_queued(monkeypatch: pytest.MonkeyPatch) -> None:
    undone: list[Any] = []

    async def undo(_ctx: Any, dispatch_id: Any) -> dict[str, Any]:
        if dispatch_id == "gone":
            raise Conflict("already_dispatched")
        undone.append(dispatch_id)
        return {}

    monkeypatch.setattr(service.dispatch, "undo", undo)
    conn = _conn(campaign=_campaign(state="sending")).on(
        "r.state = 'queued' AND d.status = 'queued'", [{"dispatch_id": "d-1"}, {"dispatch_id": "gone"}]
    )
    out = await service.cancel(tenant_ctx(conn), CAMPAIGN, 2)
    assert (out["state"], out["undone"], undone) == ("cancelled", 1, ["d-1"])
    with pytest.raises(Conflict) as err:
        await service.cancel(tenant_ctx(_conn(campaign=_campaign(state="sent"))), CAMPAIGN, 2)
    assert err.value.code == "campaign_not_cancellable"


# ── send pass ────────────────────────────────────────────────────────────────────────────────────


def _due(tid: uuid.UUID, **over: Any) -> dict[str, Any]:
    return {"campaign_recipient_id": uuid.uuid5(uuid.NAMESPACE_URL, str(tid)), "traveler_id": tid, "gone": False,
            "archived": False, "has_email": True, "email_bidx": b"x" * 16, "given_name_enc": None,
            "display_name_enc": b"enc:traveler.display_name_enc:Ivo Brandt", "marketing": True, "suppressed": False,
            **over}  # fmt: skip


def _send_conn(due: list[Any], *, left: int = 0, allowed: bool = True, queued_today: int = 0) -> Any:
    conn = _conn(allowed=allowed)
    conn.on("FROM crm.campaign_recipient r JOIN crm.traveler t", due)
    conn.on("SELECT count(*)::int FROM crm.campaign_recipient WHERE campaign_id", left)
    conn.on("r.state = 'queued' AND r.deleted_at IS NULL AND (r.queued_at", queued_today)
    conn.on("crm.conversation", uuid.UUID(int=99))
    return conn


@pytest.fixture
def queued(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    async def enqueue(_ctx: Any, **kw: Any) -> dict[str, Any]:
        seen.append(kw)
        return {"dispatch_id": str(uuid.UUID(int=len(seen))), "not_before": "now"}

    monkeypatch.setattr(send.dispatch, "enqueue_message", enqueue)
    return seen


@pytest.mark.asyncio
async def test_the_send_pass_holds_a_campaign_that_may_not_go(queued: list[Any]) -> None:
    row = _campaign(state="scheduled")
    cases = [
        (_send_conn([], allowed=False), LINKS, row, "feature_off"),
        (_send_conn([]), audience.Links(None, None), row, "not_configured"),
        (_send_conn([]).on("FROM crm.channel_account WHERE channel_account_id", None), LINKS, row, "no_mailbox"),
        (_send_conn([]).on("source_id = ANY", [{"source_id": TOUR, "served_state": "retired"}]), LINKS, row,
         "aa_tour_retired"),
        (_send_conn([]), LINKS, _campaign(state="scheduled", body_enc=b"enc:campaign.body_enc:www.other.example.test"),
         "content_gate"),
    ]  # fmt: skip
    for conn, links, campaign, code in cases:
        ctx = system_ctx(conn)
        assert (await send.run_campaign(ctx, FakeVault(), campaign, links, limit=2, agency="Sen Vang"))["held"] == 1
        assert conn.sql("UPDATE crm.campaign SET state = 'held'")[0][1] == code
    assert queued == []


@pytest.mark.asyncio
async def test_todays_clients_are_checked_again_and_queued(queued: list[Any]) -> None:
    due = [
        _due(CLIENT_A),
        _due(CLIENT_B, marketing=False),
        _due(RECIPIENT, suppressed=True),
        _due(MEMBER, archived=True),
        _due(OTHER_MEMBER, has_email=False),
    ]
    conn = _send_conn(due, left=0)
    counts = await send.run_campaign(system_ctx(conn), FakeVault(), _campaign(state="scheduled"), LINKS, limit=5,
                                     agency="Sen Vang")  # fmt: skip
    assert dict(counts) == {"queued": 1, "no_consent": 1, "suppressed": 1, "archived": 1, "no_email": 1, "finished": 1}
    assert queued == [{"traveler_id": CLIENT_A, "channel": "email", "purpose": "marketing", "origin": "system",
                       "message_id": queued[0]["message_id"]}]  # fmt: skip
    [(message_id, _conv, mailbox, subject, body)] = conn.sql("INSERT INTO crm.message")
    assert mailbox == MAILBOX and subject.startswith(b"enc:message.subject_enc:Spring on the river")
    assert f"{ORIGIN}/c/u/".encode() in body and b"Sen Vang" in body
    [(_rid, mid, _did, token_hash, url)] = conn.sql("SET state = 'queued'")
    assert mid == message_id and len(token_hash) == 32 and f"{ORIGIN}/crm/public/v1/unsubscribe/".encode() in url
    assert conn.sql("SET state = 'sent'")


@pytest.mark.asyncio
async def test_the_mailbox_room_of_the_day(queued: list[Any]) -> None:
    conn = _send_conn([_due(CLIENT_A)], left=3, queued_today=2)
    counts = await send.run_campaign(system_ctx(conn), FakeVault(), _campaign(state="scheduled"), LINKS, limit=2,
                                     agency="Sen Vang")  # fmt: skip
    assert dict(counts) == {"waiting": 1} and queued == [] and not conn.sql("JOIN crm.traveler t")
    assert conn.sql("SET state = 'sending'")


@pytest.mark.asyncio
async def test_the_handler_walks_every_agency(monkeypatch: pytest.MonkeyPatch, queued: list[Any]) -> None:
    from app.audit_export.registry import Agency

    outcomes: dict[str, Any] = {
        "a": _send_conn([_due(CLIENT_A)]).on("WHERE state IN ('scheduled', 'sending') AND scheduled_at",
                                             [_campaign(state="scheduled")]),
        "locked": Forbidden("tenant_locked"),
        "broken": Invalid("boom"),
        "quiet": _send_conn([]),
    }  # fmt: skip

    async def agencies(_pool: Any) -> list[Any]:
        return [Agency(tenant_id=k, exported_until=dt.datetime(2027, 1, 1, tzinfo=dt.UTC), verified_until=None,  # type: ignore[arg-type]
                       last_seq=0) for k in outcomes]  # fmt: skip

    @asynccontextmanager
    async def system_tx(_pool: Any, tenant_id: Any, *, actor: str) -> Any:
        assert actor == send.ACTOR
        outcome = outcomes[tenant_id]
        if isinstance(outcome, BaseException):
            raise outcome
        yield system_ctx(outcome)

    async def limit(_ctx: Any) -> int:
        return 5

    monkeypatch.setattr(send.registry, "agencies", agencies)
    monkeypatch.setattr(send, "system_tx", system_tx)
    monkeypatch.setattr(send, "Vault", lambda keys, ctx: FakeVault())
    monkeypatch.setattr(audience, "daily_limit", limit)
    handler = send.build_handler(object(), LINKS)  # type: ignore[arg-type]
    out = await handler(object(), Job("j", send.CAMPAIGN_TICK, {}, 1, 3))  # type: ignore[arg-type]
    assert out == {"agency_count": 2, "skipped_count": 1, "failed_count": 1, "finished_count": 1, "queued_count": 1}
    assert await send.build_handler(None, LINKS)(object(), Job("j", send.CAMPAIGN_TICK, {}, 1, 3)) == {  # type: ignore[arg-type]
        "agency_count": 0, "reason_code": "key_provider_not_configured"}  # fmt: skip


@pytest.mark.asyncio
async def test_a_failing_campaign_leaves_the_others(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _send_conn([]).on("WHERE state IN ('scheduled', 'sending') AND scheduled_at",
                             [_campaign(state="scheduled"), _campaign(state="scheduled")])  # fmt: skip
    calls = {"n": 0}

    async def flaky(*_a: Any, **_k: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise Invalid("boom")
        from collections import Counter

        return Counter({"waiting": 1})

    async def limit(_ctx: Any) -> int:
        return 5

    monkeypatch.setattr(send, "run_campaign", flaky)
    monkeypatch.setattr(audience, "daily_limit", limit)
    out = await send.run_agency(system_ctx(conn), FakeVault(), LINKS)  # type: ignore[arg-type]
    assert dict(out) == {"failed": 1, "waiting": 1}


@pytest.mark.asyncio
async def test_pull_retired_and_the_hourly_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = ScriptedConn().on("UPDATE crm.campaign SET state = 'held', hold_code = 'aa_tour_retired'",
                             [{"campaign_id": CAMPAIGN}])  # fmt: skip
    ctx = system_ctx(conn)
    assert await send.pull_retired(ctx, TOUR) == 1
    assert ctx.audit_events[0].reason == "aa_tour_retired"
    enqueued: list[str] = []

    async def enqueue(_pool: Any, kind: str, _payload: Any, *, idempotency_key: str, **_: Any) -> str:
        enqueued.append(idempotency_key)
        return "job-1"

    async def prune(*_a: Any) -> int:
        return 0

    monkeypatch.setattr(send.queue, "enqueue", enqueue)
    monkeypatch.setattr(send.queue, "prune", prune)
    hook = send.HourlyCampaignTick(lambda: dt.datetime(2027, 3, 1, 8, 30, tzinfo=dt.UTC))
    assert await hook(object()) == "job-1"  # type: ignore[arg-type]
    assert await hook(object()) is None  # type: ignore[arg-type]
    assert enqueued == ["crm_campaign_tick:schedule:2027-03-01T08"]


# ── unsubscribe ──────────────────────────────────────────────────────────────────────────────────


def _found(**over: Any) -> Any:
    from app.tenancy import CampaignRecipient

    base = {"campaign_recipient_id": RECIPIENT, "campaign_id": CAMPAIGN, "traveler_id": CLIENT_A, "tenant_id": TENANT,
            "unsubscribed_at": None}  # fmt: skip
    return CampaignRecipient(**{**base, **over})


def _unsub(monkeypatch: pytest.MonkeyPatch, conn: Any, **found: Any) -> Any:
    ctx = system_ctx(conn)

    @asynccontextmanager
    async def tx(_pool: Any, token_hash: str) -> Any:
        assert len(token_hash) == 64
        yield ctx, _found(**found)

    monkeypatch.setattr(unsubscribe, "campaign_unsubscribe_tx", tx)
    monkeypatch.setattr(unsubscribe, "Vault", lambda keys, ctx: FakeVault())
    return ctx


def _client_conn(*, consent: Any, suppressed: bool) -> Any:
    conn = ScriptedConn()
    profile = f"enc:traveler.profile_token_enc:{uuid.UUID(int=7)}".encode()
    conn.on(
        "SELECT email_bidx, profile_token_enc FROM crm.traveler",
        {"email_bidx": b"x" * 16, "profile_token_enc": profile},
    )
    conn.on("SELECT display_name FROM crm.tenant", "Sen Vang")
    conn.on("SELECT c.value FROM crm.consent", consent)
    conn.on("SELECT 1 FROM crm.campaign_suppression", 1 if suppressed else None)
    return conn


@pytest.mark.asyncio
async def test_unsubscribe_records_the_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _client_conn(consent=True, suppressed=False)
    ctx = _unsub(monkeypatch, conn)
    assert await unsubscribe.state(object(), TOKEN) == {"agency": "Sen Vang", "subscribed": True}  # type: ignore[arg-type]
    assert await unsubscribe.unsubscribe(object(), object(), TOKEN) == {"agency": "Sen Vang", "subscribed": False}  # type: ignore[arg-type]
    assert conn.sql("INSERT INTO crm.consent")[0] == (CLIENT_A, False)
    assert conn.sql("UPDATE crm.traveler_profile")[0] == (uuid.UUID(int=7), False)
    assert conn.sql("INSERT INTO crm.campaign_suppression")[0] == (b"x" * 16, CAMPAIGN)
    assert conn.sql("SET unsubscribed_at")[0] == (RECIPIENT,)
    assert [e.action for e in ctx.audit_events] == ["campaign_unsubscribe"]
    # Already out: no new consent row, no new audit event.
    conn = _client_conn(consent=False, suppressed=True)
    ctx = _unsub(monkeypatch, conn, unsubscribed_at=dt.datetime(2027, 3, 1, tzinfo=dt.UTC))
    await unsubscribe.unsubscribe(object(), object(), TOKEN)  # type: ignore[arg-type]
    assert not conn.sql("INSERT INTO crm.consent") and ctx.audit_events == []


@pytest.mark.asyncio
async def test_resubscribe_is_the_clients_own_yes(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _client_conn(consent=False, suppressed=True)
    ctx = _unsub(monkeypatch, conn)
    assert (await unsubscribe.resubscribe(object(), object(), TOKEN))["subscribed"] is True  # type: ignore[arg-type]
    assert conn.sql("DELETE FROM crm.campaign_suppression")[0] == (b"x" * 16,)
    assert conn.sql("INSERT INTO crm.consent")[0] == (CLIENT_A, True)
    assert [e.action for e in ctx.audit_events] == ["campaign_resubscribe"]
    conn = _client_conn(consent=True, suppressed=False)
    ctx = _unsub(monkeypatch, conn)
    await unsubscribe.resubscribe(object(), object(), TOKEN)  # type: ignore[arg-type]
    assert not conn.sql("DELETE FROM crm.campaign_suppression") and ctx.audit_events == []
