"""AA-968 (card CRM-36, campaigns R14) end to end on PostgreSQL with the RDS role shape (crm-073), as crm_app through
``tenant_tx()``, the send pass's ``system_tx()``, the public unsubscribe context and the real dispatcher + mail sender
with a fake provider:

- **Feature key**: every write is refused while the ``campaigns`` feature is not on the agency's plan.
- **BYO**: a campaign is scheduled only with one of the agency's own mailboxes (never another member's personal one).
- **d21 gate**: a link outside the AA tour pages / the TripPlanner site holds the draft's gate codes and refuses the
  schedule.
- **Consent (card R)**: clients without a marketing yes are left out of the plan, and a yes withdrawn after the plan
  skips the client on its send day.
- **Daily spread (card E)**: with a mailbox daily limit of 2, four clients go out over two days; the second pass of
  the first day sends nothing more.
- **Unsubscribe + suppression (card R)**: the one-click link records the opt-out, puts the address on the agency's
  list and the mail carries the RFC 8058 headers; a queued marketing mail to it is dropped by the dispatcher; the
  client erased and imported again with a marketing yes stays suppressed; "subscribe again" lifts it.
- **Retired tour**: the CRM fan-out's pull holds a scheduled campaign naming the tour; a tour no longer served holds
  it on its send day.
- **A stopped campaign sends nothing on reconnect**: cancel, the retired-tour pull and a send-day hold take back the
  mails still queued or held for a disconnected mailbox, so the reconnect releases none of them.
- **A disconnected mailbox keeps to the daily limit**: nothing is queued while it is disconnected, and the mails it
  releases on reconnect count against that day's room.
- **BYO at schedule and send time**: a colleague cannot schedule a campaign on another member's personal mailbox, and
  a member who is no longer active takes their mailbox with them (the campaign is held ``no_mailbox``).
- **RLS**: another agency sees none of it.

Invented names, addresses and places only.
"""

from __future__ import annotations

import datetime as dt
import email
import email.policy
import uuid
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from app.auth.route_guard import Caller
from app.campaigns import audience, send, service, unsubscribe
from app.dispatch import queue as dispatch_queue
from app.dispatch import worker as dispatch_worker
from app.documents.store import MemoryDocumentStore
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.jobs.queue import Job
from app.mail import state as mail_state
from app.mail.sender import MailSender
from app.mail.state import MailRuntime
from app.permissions import Action
from app.tags import service as tags
from app.tenancy import system_tx, tenant_tx
from app.vault import Vault
from tests.integration.test_crm010_schema import ISS, _ctx, _erase
from tests.integration.test_mail_db import _scalar
from tests.integration.test_mail_db import _setup as _mail_setup
from tests.integration.test_onboarding_import_api import _import, _preview
from tests.integration.test_portability_api import _download, _export
from tests.unit.mail_fakes import FakeProvider

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

TOUR = "3e7a91c4-58b2-4f0d-a6e1-9c2b7d4f8a16"
CLIENT = "https://go.crm.example.test"
PLANNER = "https://plan.example.test"
LINKS = audience.Links(client_origin=CLIENT, tripplanner_origin=PLANNER)
CLIENTS = (
    "Client ID,Full name,Email,Marketing\n"
    "K-1,Ivo Brandt,ivo.brandt@example.test,yes\n"
    "K-2,Lena Sato,lena.sato@example.test,yes\n"
    "K-3,Omar Field,omar.field@example.test,yes\n"
    "K-4,Pia Lund,pia.lund@example.test,yes\n"
    "K-5,Rui Costa,rui.costa@example.test,no\n"
    "K-6,Sven Holt,,yes\n"
)
BODY = f"Hello {{{{traveller_first_name}}}}, the river valley is calling.\nSee the trip: {CLIENT}/trip/{TOUR}\n"


@pytest_asyncio.fixture(autouse=True)
async def _tour(admin: asyncpg.Connection) -> AsyncIterator[None]:
    """One served AA tour in the platform cache for this file; it never outlives a test (other files count it)."""
    await admin.execute("SET ROLE crm_owner")
    try:
        await admin.execute(
            "INSERT INTO crm.aa_catalog_tour (source_system, source_id, title, served_state, checked_at, "
            "state_changed_at) VALUES ('cis', $1, 'River valley 4 days', 'served', now(), now()) "
            "ON CONFLICT (source_system, source_id) DO UPDATE SET served_state = 'served'",
            TOUR,
        )
    finally:
        await admin.execute("RESET ROLE")
    yield
    await admin.execute("SET ROLE crm_owner")
    try:
        await admin.execute("DELETE FROM crm.aa_catalog_tour WHERE source_system = 'cis' AND source_id = $1", TOUR)
    finally:
        await admin.execute("RESET ROLE")


async def _tour_state(admin: asyncpg.Connection, state: str) -> None:
    await admin.execute("SET ROLE crm_owner")
    try:
        await admin.execute("UPDATE crm.aa_catalog_tour SET served_state = $2 WHERE source_id = $1", TOUR, state)
    finally:
        await admin.execute("RESET ROLE")


async def _allow(pool: asyncpg.Pool, tenant: uuid.UUID) -> None:
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant, "platform:staff-sub-1")
        await conn.execute(
            "INSERT INTO crm.tenant_entitlement (plan, member_limit, allowed_features) VALUES ('core', 4, "
            "ARRAY['campaigns']) ON CONFLICT (tenant_id) DO UPDATE SET allowed_features = EXCLUDED.allowed_features"
        )


async def _world(pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch, *, allow: bool = True) -> dict[str, Any]:
    """An agency with the owner's own Gmail mailbox, six imported clients (four with an e-mail and a marketing yes)
    carrying the client tag "spring_list", and a mailbox daily limit of 2."""
    d = await _mail_setup(pool)
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, d["tenant"], "seed:test")
        await conn.execute("INSERT INTO crm.tenant_settings DEFAULT VALUES ON CONFLICT (tenant_id) DO NOTHING")
    status = await _import(pool, d, "clients", CLIENTS.encode())
    assert status["status"] == "done", status
    async with tenant_tx(pool, d["owner"], d["tenant"]) as ctx:
        vault = Vault(d["keys"], ctx)
        d["mailbox"] = await ctx.conn.fetchval("SELECT channel_account_id FROM crm.channel_account")
        tag = await tags.create(ctx, "client", "Spring list")
        d["clients"] = {}
        for ref in ("K-1", "K-2", "K-3", "K-4", "K-5", "K-6"):
            tid = await ctx.conn.fetchval(
                "SELECT traveler_id FROM crm.traveler WHERE import_ref_bidx = $1",
                await vault.ref_index("traveler", "import_ref", ref),
            )
            assert tid is not None, ref
            d["clients"][ref] = tid
            await tags.put(ctx, uuid.UUID(tag["tag_id"]), traveler_id=tid)
    if allow:
        await _allow(pool, d["tenant"])

    async def two(_ctx: Any) -> int:
        return 2

    monkeypatch.setattr(audience, "daily_limit", two)
    return d


def _fields(d: dict[str, Any], **over: Any) -> dict[str, Any]:
    return {
        "name": "Spring river valley",
        "locale": "en",
        "subject": "Spring on the river, {{traveller_first_name}}",
        "body": BODY,
        "channel_account_id": d["mailbox"],
        "aa_tour_ids": [TOUR],
        "audience_tags": ["spring_list"],
        **over,
    }


async def _create(pool: asyncpg.Pool, d: dict[str, Any], **over: Any) -> dict[str, Any]:
    async with tenant_tx(pool, d["owner"], d["tenant"], action=Action.CAMPAIGN_SEND) as ctx:
        return await service.create(ctx, Vault(d["keys"], ctx), _fields(d, **over), LINKS)


async def _schedule(pool: asyncpg.Pool, d: dict[str, Any], made: dict[str, Any]) -> dict[str, Any]:
    cid = uuid.UUID(made["campaign_id"])
    async with tenant_tx(pool, d["owner"], d["tenant"], action=Action.CAMPAIGN_SEND) as ctx:
        vault = Vault(d["keys"], ctx)
        seen = await service.preview(ctx, vault, cid, LINKS)
        version = await ctx.conn.fetchval("SELECT version FROM crm.campaign WHERE campaign_id = $1", cid)
        out = await service.schedule(ctx, vault, cid, at=None, expected=seen["eligible"], version=version, links=LINKS)
    return {"preview": seen, **out}


async def _tick(pool: asyncpg.Pool, d: dict[str, Any]) -> dict[str, int]:
    async with system_tx(pool, d["tenant"], actor=send.ACTOR) as ctx:
        return dict(await send.run_agency(ctx, Vault(d["keys"], ctx), LINKS))


async def _rows(pool: asyncpg.Pool, d: dict[str, Any], sql: str, *args: Any) -> list[Any]:
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, d["tenant"])
        return list(await conn.fetch(sql, *args))


async def _token(pool: asyncpg.Pool, d: dict[str, Any], traveler: uuid.UUID) -> str:
    async with tenant_tx(pool, d["owner"], d["tenant"]) as ctx:
        row = await ctx.conn.fetchrow(
            "SELECT campaign_recipient_id, unsubscribe_url_enc FROM crm.campaign_recipient WHERE traveler_id = $1 "
            "AND state = 'queued'",
            traveler,
        )
        url = await Vault(d["keys"], ctx).decrypt(
            "campaign_recipient", "unsubscribe_url_enc", row["campaign_recipient_id"], row["unsubscribe_url_enc"],
            purpose="mail",
        )  # fmt: skip
    assert url.startswith(f"{CLIENT}/crm/public/v1/unsubscribe/")
    return url.rsplit("/", 1)[1]


async def test_feature_key_byo_mailbox_and_the_content_gate(
    app_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = await _world(app_pool, monkeypatch, allow=False)
    with pytest.raises(Forbidden, match="not on this agency's plan"):
        await _create(app_pool, d)
    async with tenant_tx(app_pool, d["owner"], d["tenant"]) as ctx:
        assert (await service.listing(ctx))["feature_allowed"] is False
    await _allow(app_pool, d["tenant"])
    # d21: a link that is neither an AA tour page nor the planner holds the gate codes; scheduling is refused.
    made = await _create(app_pool, d, body=BODY + "Book at https://other-agency.example.test/deal\n")
    assert made["gate_codes"] == ["link_not_allowed"]
    with pytest.raises(Invalid) as err:
        await _schedule(app_pool, d, made)
    assert err.value.code == "content_gate"
    # No AA subject at all, and a tour link the campaign does not name.
    bare = await _create(app_pool, d, aa_tour_ids=[], body=f"See {CLIENT}/trip/{TOUR}")
    assert bare["gate_codes"] == ["no_aa_subject", "tour_link_not_listed"]
    # Brand content may not carry a sales link, even to the planner.
    brand = await _create(app_pool, d, aa_tour_ids=[], brand_only=True, body=f"Plan yours: {PLANNER}/sen-vang/plan")
    assert brand["gate_codes"] == ["brand_has_sales_link"]
    # BYO: no mailbox -> refused; another member's personal mailbox cannot be picked.
    no_box = await _create(app_pool, d, channel_account_id=None)
    with pytest.raises(Conflict, match="own mailboxes"):
        await _schedule(app_pool, d, no_box)
    other_member = uuid.uuid4()
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, d["tenant"], "seed:test")
        await conn.execute(
            "INSERT INTO crm.member (member_id, cognito_iss, cognito_sub, role, status) "
            "VALUES ($1, $2, $3, 'agent', 'invited')",
            other_member,
            ISS,
            f"sub-{other_member}",
        )
        await conn.execute("UPDATE crm.member SET status = 'active' WHERE member_id = $1", other_member)
        theirs = await conn.fetchval(
            "INSERT INTO crm.channel_account (member_id, kind, address_enc, address_bidx, status) "
            "VALUES ($1, 'smtp', $2, $3, 'needs_attention') RETURNING channel_account_id",
            other_member,
            b"x" * 40,
            b"y" * 16,
        )
    with pytest.raises(Invalid, match="agency mailbox or your own"):
        await _create(app_pool, d, channel_account_id=theirs)
    # Nor schedule one written on another member's personal mailbox: the agent cannot send as the owner.
    mine = await _create(app_pool, d)  # the owner's own Gmail
    agent = Caller("agency", "partner", "partner-web", ISS, f"sub-{other_member}", ())
    async with tenant_tx(app_pool, agent, d["tenant"], action=Action.CAMPAIGN_SEND) as ctx:
        cid = uuid.UUID(mine["campaign_id"])
        with pytest.raises(Conflict) as err:
            await service.schedule(ctx, Vault(d["keys"], ctx), cid, at=None, expected=4, version=1, links=LINKS)
    assert err.value.code == "invalid_mailbox"
    assert await _scalar(app_pool, d["tenant"], "SELECT state FROM crm.campaign WHERE campaign_id = $1", cid) == "draft"
    # The agent's own mailbox is theirs to schedule; once the agent is disabled, the send pass holds it (BYO).
    async with tenant_tx(app_pool, agent, d["tenant"], action=Action.CAMPAIGN_SEND) as ctx:
        vault = Vault(d["keys"], ctx)
        own = await service.create(ctx, vault, _fields(d, channel_account_id=theirs), LINKS)
        own_id = uuid.UUID(own["campaign_id"])
        seen = await service.preview(ctx, vault, own_id, LINKS)
        out = await service.schedule(ctx, vault, own_id, at=None, expected=seen["eligible"], version=1, links=LINKS)
    assert out["state"] == "scheduled"
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, d["tenant"], "seed:test")
        await conn.execute("UPDATE crm.member SET status = 'disabled' WHERE member_id = $1", other_member)
    assert (await _tick(app_pool, d)).get("held") == 1
    held = await _rows(app_pool, d, "SELECT state, hold_code FROM crm.campaign WHERE campaign_id = $1", own_id)
    assert tuple(held[0]) == ("held", "no_mailbox")


async def test_consent_and_the_daily_spread(app_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch) -> None:
    d = await _world(app_pool, monkeypatch)
    made = await _create(app_pool, d)
    assert made["gate_codes"] == []
    done = await _schedule(app_pool, d, made)
    # Card R: the client without a marketing yes and the one without an address are left out.
    assert done["preview"]["eligible"] == 4 and done["preview"]["excluded"] == {"no_consent": 1, "no_email": 1}
    # Card E: four clients over two days at two a day.
    assert (done["days"], done["daily_limit"]) == (2, 2)
    days = await _rows(app_pool, d, "SELECT send_on, count(*) AS n FROM crm.campaign_recipient GROUP BY 1 ORDER BY 1")
    assert [r["n"] for r in days] == [2, 2] and days[1]["send_on"] - days[0]["send_on"] == dt.timedelta(days=1)
    first = await _tick(app_pool, d)
    assert first.get("queued") == 2 and first.get("waiting") == 1
    assert (await _tick(app_pool, d)).get("queued") is None  # the day's room is used
    queued = await _rows(
        app_pool, d,
        "SELECT q.purpose, q.origin, q.channel, m.channel_account_id, m.sender_kind FROM crm.campaign_recipient r "
        "JOIN crm.dispatch_queue q ON q.dispatch_id = r.dispatch_id JOIN crm.message m ON m.message_id = r.message_id",
    )  # fmt: skip
    assert {tuple(r) for r in queued} == {("marketing", "system", "email", d["mailbox"], "agency_mailbox")}
    # The dispatcher sends them (a mail still waiting would count against the next day's room too).
    assert await _dispatch_all(app_pool, d, FakeProvider(external_id=None)) == ["sent", "sent"]
    # A yes withdrawn after the plan: that client is skipped on the send day.
    later = await _rows(app_pool, d, "SELECT traveler_id FROM crm.campaign_recipient WHERE state = 'planned'")
    withdrawn = later[0]["traveler_id"]
    async with tenant_tx(app_pool, d["owner"], d["tenant"]) as ctx:
        await ctx.conn.execute(
            "INSERT INTO crm.consent (traveler_id, purpose, value, source) VALUES ($1, 'marketing', false, "
            "'agent_recorded')",
            withdrawn,
        )
        tomorrow = await audience.today(ctx) + dt.timedelta(days=1)

    async def next_day(_ctx: Any) -> dt.date:
        return tomorrow

    monkeypatch.setattr(audience, "today", next_day)
    second = await _tick(app_pool, d)
    assert (second.get("queued"), second.get("no_consent"), second.get("finished")) == (1, 1, 1)
    states = await _rows(app_pool, d, "SELECT traveler_id, state, outcome_code FROM crm.campaign_recipient")
    assert {(r["state"], r["outcome_code"]) for r in states if r["traveler_id"] == withdrawn} == {
        ("skipped", "no_consent")
    }
    async with tenant_tx(app_pool, d["owner"], d["tenant"]) as ctx:
        listed = (await service.listing(ctx))["items"]
        assert [(c["state"], c["counts"]["queued"], c["counts"]["skipped"]) for c in listed] == [("sent", 3, 1)]
    # The agency's portability export opens the campaign text under the mail DEK and leaves the unsubscribe tokens out.
    store = MemoryDocumentStore()
    row = await _export(app_pool, d["keys"], store, d)
    zf, _ = await _download(app_pool, d["keys"], store, d, row["export_id"])
    assert "Spring on the river" in zf.read("csv/campaign.csv").decode("utf-8-sig")
    header = zf.read("csv/campaign_recipient.csv").decode("utf-8-sig").splitlines()[0]
    assert "unsubscribe_hash" not in header and "unsubscribe_url" not in header and "send_on" in header
    # RLS: another agency sees nothing and cannot open it.
    other = await _mail_setup(app_pool)
    async with tenant_tx(app_pool, other["owner"], other["tenant"]) as ctx:
        assert (await service.listing(ctx))["items"] == []
        with pytest.raises(NotFound):
            await service.detail(ctx, Vault(other["keys"], ctx), uuid.UUID(made["campaign_id"]))
        assert await ctx.conn.fetchval("SELECT count(*) FROM crm.campaign_recipient") == 0


async def test_unsubscribe_suppression_and_the_reimport(
    app_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = await _world(app_pool, monkeypatch)
    await _schedule(app_pool, d, await _create(app_pool, d))
    assert (await _tick(app_pool, d)).get("queued") == 2
    queued_sql = "SELECT traveler_id FROM crm.campaign_recipient WHERE state = 'queued' ORDER BY traveler_id"
    sent_to = [r["traveler_id"] for r in await _rows(app_pool, d, queued_sql)]
    leaver, stayer = sent_to
    # The real sender with a fake Gmail: the mail carries the RFC 8058 headers and the unsubscribe page link.
    provider = FakeProvider()
    handler = dispatch_worker.build_handler(MailSender(MailRuntime(keys=d["keys"], providers={"gmail": provider})))
    stayer_dispatch = (await _rows(app_pool, d, "SELECT dispatch_id FROM crm.campaign_recipient WHERE traveler_id = $1",
                                   stayer))[0]["dispatch_id"]  # fmt: skip
    job = Job("j", "crm_dispatch", {"tenant_id": str(d["tenant"]), "dispatch_id": str(stayer_dispatch)}, 1, 5)
    assert (await handler(app_pool, job))["status"] == "sent"
    msg = email.message_from_bytes(provider.sent[0][0], policy=email.policy.default)
    assert msg["List-Unsubscribe"].startswith(f"<{CLIENT}/crm/public/v1/unsubscribe/")
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    text = msg.get_content()
    assert f"{CLIENT}/c/u/" in text and "Unsubscribe:" in text and "{{" not in text
    # The client clicks unsubscribe (twice: nothing new the second time).
    token = await _token(app_pool, d, leaver)
    assert await unsubscribe.state(app_pool, token) == {"agency": "Sen Vàng Travel", "subscribed": True}
    for _ in range(2):
        assert (await unsubscribe.unsubscribe(app_pool, d["keys"], token))["subscribed"] is False
    assert await _scalar(app_pool, d["tenant"], "SELECT count(*) FROM crm.campaign_suppression") == 1
    assert await _scalar(
        app_pool, d["tenant"],
        "SELECT value FROM crm.consent WHERE traveler_id = $1 AND purpose = 'marketing' ORDER BY at DESC LIMIT 1",
        leaver,
    ) is False  # fmt: skip
    with pytest.raises(NotFound):
        await unsubscribe.state(app_pool, "x" * 43)
    # A yes recorded again by hand: the queued marketing mail is still dropped by the dispatcher (the address list).
    async with tenant_tx(app_pool, d["owner"], d["tenant"]) as ctx:
        await ctx.conn.execute(
            "INSERT INTO crm.consent (traveler_id, purpose, value, source) VALUES ($1, 'marketing', true, "
            "'agent_recorded')",
            leaver,
        )
    leaver_dispatch = (await _rows(app_pool, d, "SELECT dispatch_id FROM crm.campaign_recipient WHERE traveler_id = $1",
                                   leaver))[0]["dispatch_id"]  # fmt: skip
    job = Job("j2", "crm_dispatch", {"tenant_id": str(d["tenant"]), "dispatch_id": str(leaver_dispatch)}, 1, 5)
    assert await handler(app_pool, job) == {
        "dispatch_id": str(leaver_dispatch), "status": "dropped", "reason_code": "no_consent"}  # fmt: skip
    assert len(provider.sent) == 1
    # The client is erased, then imported again with a marketing yes: the yes is not recorded and they stay out.
    async with tenant_tx(app_pool, d["owner"], d["tenant"]) as ctx:
        blob = await ctx.conn.fetchval("SELECT profile_token_enc FROM crm.traveler WHERE traveler_id = $1", leaver)
        profile = await Vault(d["keys"], ctx).decrypt("traveler", "profile_token_enc", leaver, blob)
    refs = {v: k for k, v in d["clients"].items()}
    await _erase(app_pool, d["tenant"], "traveler", leaver, uuid.UUID(profile))
    assert await _scalar(app_pool, d["tenant"], "SELECT count(*) FROM crm.campaign_suppression") == 1
    emails = {"K-1": "ivo.brandt@example.test", "K-2": "lena.sato@example.test", "K-3": "omar.field@example.test",
              "K-4": "pia.lund@example.test"}  # fmt: skip
    address = emails[refs[leaver]]
    again = f"Client ID,Full name,Email,Marketing\nR-9,Returning Guest,{address},yes\n".encode()
    seen = await _preview(app_pool, d, "clients", again)
    assert "marketing_suppressed" in str(seen)
    assert (await _import(app_pool, d, "clients", again))["status"] == "done"
    async with tenant_tx(app_pool, d["owner"], d["tenant"]) as ctx:
        vault = Vault(d["keys"], ctx)
        back = await ctx.conn.fetchval(
            "SELECT traveler_id FROM crm.traveler WHERE import_ref_bidx = $1",
            await vault.ref_index("traveler", "import_ref", "R-9"),
        )
        assert await ctx.conn.fetchval("SELECT count(*) FROM crm.consent WHERE traveler_id = $1", back) == 0
        # Even with a yes recorded later (an inquiry form), the address stays suppressed for campaigns.
        await ctx.conn.execute(
            "INSERT INTO crm.consent (traveler_id, purpose, value, source) VALUES ($1, 'marketing', true, 'form')", back
        )
        found = await audience.audience(ctx, [])
        assert back not in found.eligible and found.excluded.get("suppressed") == 1
        queued = await dispatch_queue.enqueue_message(
            ctx, traveler_id=back, channel="email", purpose="marketing", origin="system"
        )
    job = Job("j3", "crm_dispatch", {"tenant_id": str(d["tenant"]), "dispatch_id": queued["dispatch_id"]}, 1, 5)
    assert (await handler(app_pool, job))["reason_code"] == "no_consent"
    # "Subscribe again" is the client's own act on the page: it lifts the suppression.
    stayer_token = await _token(app_pool, d, stayer)
    await unsubscribe.unsubscribe(app_pool, d["keys"], stayer_token)
    assert await _scalar(app_pool, d["tenant"], "SELECT count(*) FROM crm.campaign_suppression") == 2
    assert (await unsubscribe.resubscribe(app_pool, d["keys"], stayer_token))["subscribed"] is True
    assert await _scalar(app_pool, d["tenant"], "SELECT count(*) FROM crm.campaign_suppression") == 1
    assert await unsubscribe.state(app_pool, stayer_token) == {"agency": "Sen Vàng Travel", "subscribed": True}


async def test_a_retired_tour_pulls_scheduled_campaigns(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = await _world(app_pool, monkeypatch)
    pulled = await _create(app_pool, d)
    await _schedule(app_pool, d, pulled)
    # The fan-out of a retirement holds it at once (the same run again holds nothing new).
    async with system_tx(app_pool, d["tenant"], actor="job:crm_tour_retired_fanout") as ctx:
        assert await send.pull_retired(ctx, TOUR) == 1
        assert await send.pull_retired(ctx, TOUR) == 0
    rows = await _rows(app_pool, d, "SELECT state, hold_code FROM crm.campaign")
    assert [tuple(r) for r in rows] == [("held", "aa_tour_retired")]
    assert await _tick(app_pool, d) == {}  # nothing live, nothing queued
    # Edited onto brand content without a sales link it can be scheduled again.
    cid = uuid.UUID(pulled["campaign_id"])
    async with tenant_tx(app_pool, d["owner"], d["tenant"], action=Action.CAMPAIGN_SEND) as ctx:
        vault = Vault(d["keys"], ctx)
        row = await service.detail(ctx, vault, cid)
        changed = await service.update(
            ctx, vault, cid, _fields(d, aa_tour_ids=[], brand_only=True, body="Thank you for travelling with us."),
            row["version"], LINKS,
        )  # fmt: skip
    assert changed["gate_codes"] == []
    assert (await _schedule(app_pool, d, {"campaign_id": str(cid)}))["state"] == "scheduled"
    # A tour that is no longer served (the fan-out not run yet) holds a campaign naming it on its send day.
    second = await _create(app_pool, d, name="Autumn river valley")
    await _schedule(app_pool, d, second)
    await _tour_state(admin, "paused")
    out = await _tick(app_pool, d)
    assert out.get("held") == 1
    held = await _rows(app_pool, d, "SELECT hold_code, gate_codes FROM crm.campaign WHERE campaign_id = $1",
                       uuid.UUID(second["campaign_id"]))  # fmt: skip
    assert (held[0]["hold_code"], list(held[0]["gate_codes"])) == ("aa_tour_retired", ["aa_tour_not_served"])
    # Cancel: the planned clients are skipped, nothing is left to send.
    async with tenant_tx(app_pool, d["owner"], d["tenant"], action=Action.CAMPAIGN_SEND) as ctx:
        version = await ctx.conn.fetchval("SELECT version FROM crm.campaign WHERE campaign_id = $1",
                                          uuid.UUID(second["campaign_id"]))  # fmt: skip
        gone = await service.cancel(ctx, uuid.UUID(second["campaign_id"]), version)
    assert gone["state"] == "cancelled" and gone["skipped"] == 4


async def _mailbox(pool: asyncpg.Pool, d: dict[str, Any], *, connected: bool) -> int:
    """The owner's Gmail goes disconnected (a revoked token) or connected again (its held mails go back to the
    queue); returns how many went back."""
    async with system_tx(pool, d["tenant"], actor="job:crm_mailbox_check") as ctx:
        credential = await ctx.conn.fetchval(
            "SELECT credential_id FROM crm.channel_account WHERE channel_account_id = $1", d["mailbox"]
        )
        if connected:
            return await mail_state.mark_connected(ctx, d["mailbox"], credential)
        await mail_state.mark_disconnected(ctx, d["mailbox"], credential, "auth_revoked")
        return 0


async def _dispatch_all(pool: asyncpg.Pool, d: dict[str, Any], provider: FakeProvider) -> list[str]:
    """Run the real dispatcher + mail sender on every campaign mail; returns each row's outcome."""
    handler = dispatch_worker.build_handler(MailSender(MailRuntime(keys=d["keys"], providers={"gmail": provider})))
    out = []
    for r in await _rows(pool, d, "SELECT dispatch_id FROM crm.campaign_recipient WHERE dispatch_id IS NOT NULL "
                                  "ORDER BY dispatch_id"):  # fmt: skip
        job = Job("j", "crm_dispatch", {"tenant_id": str(d["tenant"]), "dispatch_id": str(r["dispatch_id"])}, 1, 5)
        result = await handler(pool, job)
        out.append(result.get("reason_code") or result["status"])
    return out


async def test_a_stopped_campaign_sends_nothing_when_the_mailbox_reconnects(
    app_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = await _world(app_pool, monkeypatch)
    provider = FakeProvider(external_id=None)
    # Cancel: two mails queued, the mailbox drops, both wait held_reconnect; the cancel takes them back.
    first = await _create(app_pool, d)
    await _schedule(app_pool, d, first)
    assert (await _tick(app_pool, d)).get("queued") == 2
    await _mailbox(app_pool, d, connected=False)
    assert await _dispatch_all(app_pool, d, provider) == ["held_reconnect", "held_reconnect"]
    cid = uuid.UUID(first["campaign_id"])
    async with tenant_tx(app_pool, d["owner"], d["tenant"], action=Action.CAMPAIGN_SEND) as ctx:
        version = await ctx.conn.fetchval("SELECT version FROM crm.campaign WHERE campaign_id = $1", cid)
        gone = await service.cancel(ctx, cid, version)
    assert (gone["skipped"], gone["undone"]) == (2, 2)
    stopped = await _rows(
        app_pool, d,
        "SELECT r.state, r.outcome_code, r.message_id IS NULL AS no_message, q.status, q.gate_result "
        "FROM crm.campaign_recipient r LEFT JOIN crm.dispatch_queue q ON q.dispatch_id = r.dispatch_id "
        "WHERE r.campaign_id = $1 AND r.dispatch_id IS NOT NULL", cid,
    )  # fmt: skip
    assert {tuple(r) for r in stopped} == {("skipped", "cancelled", True, "cancelled", "undone")}
    assert await _scalar(app_pool, d["tenant"], "SELECT count(*) FROM crm.message WHERE status = 'failed' "
                                                "AND failure_code = 'campaign_stopped'") == 2  # fmt: skip
    assert await _mailbox(app_pool, d, connected=True) == 0  # nothing of the campaign goes back to the queue
    assert await _dispatch_all(app_pool, d, provider) == ["skipped", "skipped"] and provider.sent == []
    # The retired-tour pull: the same, and the clients go back to the plan for a later schedule.
    second = await _create(app_pool, d, name="Autumn river valley")
    await _schedule(app_pool, d, second)
    assert (await _tick(app_pool, d)).get("queued") == 2
    await _mailbox(app_pool, d, connected=False)
    held = await _dispatch_all(app_pool, d, provider)
    assert held.count("held_reconnect") == 2
    async with system_tx(app_pool, d["tenant"], actor="job:crm_tour_retired_fanout") as ctx:
        assert await send.pull_retired(ctx, TOUR) == 1
    sid = uuid.UUID(second["campaign_id"])
    states = await _rows(app_pool, d, "SELECT state, count(*) AS n FROM crm.campaign_recipient WHERE campaign_id = $1 "
                                      "GROUP BY 1", sid)  # fmt: skip
    assert {r["state"]: r["n"] for r in states} == {"planned": 4}
    assert await _mailbox(app_pool, d, connected=True) == 0
    assert set(await _dispatch_all(app_pool, d, provider)) == {"skipped"} and provider.sent == []


async def test_a_disconnected_mailbox_keeps_to_the_daily_limit(
    app_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = await _world(app_pool, monkeypatch)
    await _schedule(app_pool, d, await _create(app_pool, d))  # four clients, two a day
    assert (await _tick(app_pool, d)).get("queued") == 2
    await _mailbox(app_pool, d, connected=False)
    provider = FakeProvider(external_id=None)
    assert await _dispatch_all(app_pool, d, provider) == ["held_reconnect", "held_reconnect"]
    async with tenant_tx(app_pool, d["owner"], d["tenant"]) as ctx:
        today = await audience.today(ctx)
    for offset in (1, 2):  # two send days while disconnected: nothing more is queued, the campaign stays live
        day = today + dt.timedelta(days=offset)

        async def fixed(_ctx: Any, day: dt.date = day) -> dt.date:
            return day

        monkeypatch.setattr(audience, "today", fixed)
        assert await _tick(app_pool, d) == {"mailbox_disconnected": 1}
    assert (
        await _scalar(app_pool, d["tenant"], "SELECT count(*) FROM crm.campaign_recipient WHERE state = 'queued'") == 2
    )
    assert await _scalar(app_pool, d["tenant"], "SELECT state FROM crm.campaign") == "sending"
    # Reconnected on the third day: the two held mails go back to the queue and fill that day's room.
    assert await _mailbox(app_pool, d, connected=True) == 2
    assert (await _tick(app_pool, d)).get("queued") is None
    assert await _dispatch_all(app_pool, d, provider) == ["sent", "sent"] and len(provider.sent) == 2
    # Sent that day (queued an earlier day): they still count against the day they left.
    async with tenant_tx(app_pool, d["owner"], d["tenant"]) as ctx:
        await ctx.conn.execute("UPDATE crm.campaign_recipient SET queued_at = queued_at - interval '2 days' "
                               "WHERE state = 'queued'")  # fmt: skip
        assert await audience.day_room(ctx, d["mailbox"], today, 2) == 0
        assert await audience.day_room(ctx, d["mailbox"], today + dt.timedelta(days=1), 2) == 2
