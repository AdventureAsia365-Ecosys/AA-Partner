"""AA-1093 stage 2 on PostgreSQL 15 with the RDS role shape: ``crm_tenant_erase``, the CRM step of the CIS tenant
erasure saga, against a fake CIS (the feed and the reports route of ``tenant.erasure.v1``).

- a locked agency with clients, a trip, a document and keys: DRY RUN (plan + hash, nothing written but the record and
  the audit), an apply refused by the kill switch, then a new round: DRY RUN, apply (owner requests cancelled, every
  tenant table erased, keys shredded, name replaced, studio invitations of the tenant deleted, objects last), verify
  (census 0) — each report posted once, a 503 posts the same report again;
- a plan that changed after the approval is refused and erases nothing; a legal hold blocks the DRY RUN;
- the pilot is refused in every mode; on Dev a tenant that is not synthetic is never applied;
- a tenant with no CRM agency: its studio invitations only;
- an agency the owner already erased (AA-942): the apply's entries equal the DRY RUN's, the name is replaced;
- a bucket error at the end of an apply rolls the whole apply back and the next run redoes it;
- two runs at once: the request's work lock serialises them, the second makes and posts nothing.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import os
import uuid
from typing import Any

import asyncpg
import pytest

from app.cis.http import HttpResponse
from app.cis.membership_feed import FeedConfig
from app.documents import service as documents
from app.documents.store import MemoryDocumentStore
from app.errors import Unavailable
from app.jobs.queue import Job
from app.lifecycle import offboarding, policy, saga_rules, saga_store, tenant_erase
from app.tenancy import tenant_tx
from app.vault import Vault
from tests.integration.test_crm005_intake import _keys, _owner_fetch
from tests.integration.test_crm010_lifecycle import JPEG, _inquiry, _jobs, _sql

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

BASE = "https://cis.example.test"
NOW = dt.datetime(2026, 11, 15, 3, 0, tzinfo=dt.UTC)
INVITER = "platform:staff-sub-1"


class Tokens:
    async def token(self, scope: str) -> str:
        assert scope == "cis.tenant/erasure"
        return "test-token"

    def invalidate(self, scope: str) -> None:
        pass


class FakeCis:
    """The CIS side: one feed item per request (the newest state) and the reports it received."""

    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.reports: list[dict[str, Any]] = []
        self.down = False
        self._n = 0

    def request(self, rid: uuid.UUID, tenant: uuid.UUID, **kw: Any) -> None:
        self._n += 1
        item = {
            "request_id": str(rid),
            "tenant_id": str(tenant),
            "phase": kw.get("phase", "dry_run"),
            "round": kw.get("round", 2),
            "opened_at": "2026-08-01T00:00:00Z",
            "approval_expires_at": kw.get("expires"),
            "is_pilot": kw.get("is_pilot", False),
            "is_synthetic": kw.get("is_synthetic", True),
            "steps": [
                {
                    "app": "crm",
                    "status": kw.get("status", "active"),
                    "not_before": "2026-11-01T00:00:00Z",
                    "approved_plan_hash": kw.get("approved"),
                }
            ],
            "updated_at": f"2026-11-15T02:{self._n:02d}:00Z",
            "cursor": f"c{self._n}",
        }
        self.items[str(rid)] = item

    def mine(self, rid: uuid.UUID) -> list[dict[str, Any]]:
        return [r for r in self.reports if r["request_id"] == str(rid)]

    async def __call__(self, method: str, url: str, headers: Any, body: bytes | None, _timeout: float) -> HttpResponse:
        assert headers["Authorization"] == "Bearer test-token"
        if method == "GET":
            assert url.startswith(f"{BASE}/tenant/v1/erasure-requests?")
            page = {"items": list(self.items.values()), "next_cursor": f"c{self._n}"}
            return HttpResponse(200, json.dumps(page).encode())
        assert method == "POST" and url.startswith(f"{BASE}/tenant/v1/erasure-requests/") and body is not None
        if self.down:
            return HttpResponse(503, b'{"error": "unavailable", "message": "later"}')
        report = json.loads(body)
        assert set(report) == set(saga_rules.REPORT_FIELDS) and report["app"] == "crm"
        same = [r for r in self.reports if r["report_id"] == report["report_id"]]
        if same:
            assert same[0] == report  # the same report id always carries the same body
            return HttpResponse(200, b'{"report_id": "x", "created": false, "phase": "dry_run"}')
        self.reports.append(report)
        return HttpResponse(201, b'{"report_id": "x", "created": true, "phase": "dry_run"}')


def _handler(cis: FakeCis, keys: Any, store: MemoryDocumentStore, *, apply: bool, env: str = "dev"):
    return tenant_erase.TenantErase(
        FeedConfig(BASE, Tokens(), cis),  # type: ignore[arg-type]
        keys=keys,
        store=store,
        apply_enabled=apply,
        environment=env,
        now=lambda: NOW,
    )


def _job() -> Job:
    return Job(str(uuid.uuid4()), "crm_tenant_erase", {"purpose": "slot_schedule"}, 1, 3)


async def _invitation(pool: asyncpg.Pool, cis_tenant: uuid.UUID) -> None:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.actor', $1, true)", INVITER)
        await conn.execute(
            "INSERT INTO crm.studio_invitation (cis_tenant_id, cis_invitation_id, email_enc, token_hash, expires_at) "
            "VALUES ($1, $2, $3, $4, now() + interval '7 days')",
            cis_tenant,
            uuid.uuid4(),
            os.urandom(64),
            hashlib.sha256(os.urandom(16)).digest(),
        )


async def _invitations(admin: asyncpg.Connection, cis_tenant: uuid.UUID) -> int:
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE crm_owner")
        await admin.execute("SELECT set_config('app.actor', $1, true)", INVITER)
        return int(
            await admin.fetchval("SELECT count(*) FROM crm.studio_invitation WHERE cis_tenant_id = $1", cis_tenant)
        )


async def _lock(pool: asyncpg.Pool, tenant: uuid.UUID) -> None:
    """CIS stopped serving the tenant at T0: the account feed locked the agency (lock source cis_tenant)."""
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
        await conn.execute("SELECT set_config('app.actor', 'platform:feed-tenant-account', true)")
        await conn.execute(
            "UPDATE crm.tenant SET status = 'locked', lock_source = 'cis_tenant' WHERE tenant_id = $1", tenant
        )


async def _agency_with_data(
    pool: asyncpg.Pool, keys: Any, store: MemoryDocumentStore, *, lock: bool = True
) -> dict[str, Any]:
    a = await _inquiry(pool, keys)
    async with tenant_tx(pool, a["owner"], a["tenant"]) as ctx:
        await documents.upload(
            ctx, Vault(keys, ctx), store, traveler_id=a["lead"], trip_id=a["trip"], kind="other",
            filename="ticket.jpg", content_type="image/jpeg", content=JPEG,
        )  # fmt: skip
    await _invitation(pool, a["tenant"])
    if lock:
        await _lock(pool, a["tenant"])
    return a


async def _record(admin: asyncpg.Connection, rid: uuid.UUID) -> list[asyncpg.Record]:
    return await admin.fetch(
        "SELECT round, mode, outcome, error_code, post_outcome, post_attempts FROM crm.tenant_erasure_report "
        "WHERE request_id = $1 ORDER BY round, mode",
        rid,
    )


async def test_dry_run_then_a_refused_apply_then_a_full_round(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    keys, store, cis = _keys(), MemoryDocumentStore(), FakeCis()
    a = await _agency_with_data(app_pool, keys, store)
    other = await _agency_with_data(app_pool, keys, store)
    tenant, rid = a["tenant"], uuid.uuid4()

    # Round 2 DRY RUN: the plan and its hash, nothing erased.
    cis.request(rid, tenant)
    result = await _handler(cis, keys, store, apply=False)(app_pool, _job())
    assert result["status"] == "ok" and result["reports_count"] == 1 and result["posted_count"] == 1
    [dry] = cis.mine(rid)
    assert dry["mode"] == "dry_run" and dry["status"] == "ok" and dry["round"] == 2 and dry["census"] is None
    tables = {(t["table"], t["action"]): t["rows"] for t in dry["tables"]}
    assert tables[("crm.traveler", "delete")] == 2 and tables[("crm.trip", "delete")] == 1
    assert tables[("crm.studio_invitation", "delete")] == 1 and tables[("crm.tenant", "anonymise")] == 1
    assert tables[("crm.tenant_key", "shred")] >= 1
    assert dry["plan_hash"] == saga_rules.plan_hash(tenant, 2, dry["tables"])
    assert len(await _sql(app_pool, tenant, "SELECT 1 FROM crm.traveler")) == 2
    # The same run again posts nothing new (one report per request, round and mode).
    assert (await _handler(cis, keys, store, apply=False)(app_pool, _job()))["reports_count"] == 0
    assert len(cis.mine(rid)) == 1

    # Approved, but the CRM kill switch is off: refused, nothing erased.
    expires = "2026-11-16T03:00:00Z"
    cis.request(rid, tenant, phase="apply", approved=dry["plan_hash"], expires=expires)
    await _handler(cis, keys, store, apply=False)(app_pool, _job())
    refused = cis.mine(rid)[-1]
    assert (refused["mode"], refused["status"], refused["error_code"]) == ("apply", "refused", "apply_disabled")
    assert len(await _sql(app_pool, tenant, "SELECT 1 FROM crm.traveler")) == 2

    # CIS starts round 3: DRY RUN, approval, apply and verify (the verify in the same run as the apply).
    cis.request(rid, tenant, round=3)
    await _handler(cis, keys, store, apply=True)(app_pool, _job())
    dry3 = cis.mine(rid)[-1]
    assert dry3["round"] == 3 and dry3["plan_hash"] != dry["plan_hash"]  # the round is bound into the hash
    cis.request(rid, tenant, round=3, phase="apply", approved=dry3["plan_hash"], expires=expires)
    cis.down = True  # the apply's report is recorded with the erase; CIS gets it on the next run
    await _handler(cis, keys, store, apply=True)(app_pool, _job())
    assert [r["mode"] for r in cis.mine(rid)] == ["dry_run", "apply", "dry_run"]
    record = {(r["round"], r["mode"]): r for r in await _record(admin, rid)}
    assert record[(3, "apply")]["outcome"] == "ok" and record[(3, "apply")]["post_outcome"] is None
    cis.down = False
    await _handler(cis, keys, store, apply=True)(app_pool, _job())
    applied, verified = cis.mine(rid)[-2:]
    assert (applied["mode"], applied["status"], applied["plan_hash"]) == ("apply", "ok", dry3["plan_hash"])
    assert (verified["mode"], verified["status"], verified["plan_hash"]) == ("verify", "ok", None)
    assert verified["census"]["pii_rows_found"] == 0 and verified["census"]["pii_columns_checked"] > 20
    assert all(r["post_outcome"] == "accepted" for r in await _record(admin, rid))

    [agency] = await _owner_fetch(admin, tenant, "SELECT status, display_name, lock_source FROM crm.tenant")
    assert agency["status"] == "offboarded" and agency["display_name"] == saga_rules.erased_name(tenant)
    for table in ("traveler", "trip", "member", "consent", "document", "form_submission"):
        assert await _owner_fetch(admin, tenant, f"SELECT 1 FROM crm.{table}") == [], table  # noqa: S608
    keys_left = await _owner_fetch(admin, tenant, "SELECT wrapped_dek, destroyed_at FROM crm.tenant_key")
    assert keys_left and all(k["wrapped_dek"] is None and k["destroyed_at"] is not None for k in keys_left)
    assert not [k for k in store.objects if k.startswith(f"tenants/{tenant}/")], "documents erased"
    assert await _invitations(admin, tenant) == 0
    actions = {
        r["action"]
        for r in await _owner_fetch(
            admin, tenant, "SELECT action FROM crm.audit_event WHERE action LIKE 'tenant_erasure_%'"
        )
    }
    assert {"tenant_erasure_dry_run", "tenant_erasure_apply"} <= actions  # the audit stays (keep list)
    # Another agency is untouched.
    assert len(await _sql(app_pool, other["tenant"], "SELECT 1 FROM crm.traveler")) == 2
    assert await _invitations(admin, other["tenant"]) == 1
    # Done: nothing more to report.
    cis.request(rid, tenant, round=3, phase="done", status="done")
    assert (await _handler(cis, keys, store, apply=True)(app_pool, _job()))["reports_count"] == 0


async def test_a_plan_that_changed_after_the_approval_erases_nothing(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    keys, store, cis = _keys(), MemoryDocumentStore(), FakeCis()
    a = await _agency_with_data(app_pool, keys, store)
    rid = uuid.uuid4()
    cis.request(rid, a["tenant"])
    await _handler(cis, keys, store, apply=True)(app_pool, _job())
    [dry] = cis.mine(rid)
    await _invitation(app_pool, a["tenant"])  # a new row after the DRY RUN
    cis.request(rid, a["tenant"], phase="apply", approved=dry["plan_hash"], expires="2026-11-16T03:00:00Z")
    await _handler(cis, keys, store, apply=True)(app_pool, _job())
    refused = cis.mine(rid)[-1]
    assert (refused["status"], refused["error_code"]) == ("refused", "plan_changed")
    assert refused["plan_hash"] != dry["plan_hash"]
    assert len(await _sql(app_pool, a["tenant"], "SELECT 1 FROM crm.traveler")) == 2
    assert await _invitations(admin, a["tenant"]) == 2


async def test_guards_hold_pilot_and_synthetic(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    keys, store, cis = _keys(), MemoryDocumentStore(), FakeCis()
    a = await _agency_with_data(app_pool, keys, store, lock=False)
    # A legal hold blocks the DRY RUN (no hash to approve).
    async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
        hold = await policy.place_hold(ctx, "traveler", a["lead"], "dispute")
    rid = uuid.uuid4()
    cis.request(rid, a["tenant"])
    await _handler(cis, keys, store, apply=True)(app_pool, _job())
    [blocked] = cis.mine(rid)
    assert (blocked["status"], blocked["error_code"], blocked["plan_hash"]) == ("blocked", "legal_hold", None)
    assert blocked["blockers"] == [{"code": "legal_hold", "count": 1}]
    async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
        await policy.release_hold(ctx, uuid.UUID(hold["hold_id"]))
    await _lock(app_pool, a["tenant"])

    # The pilot is refused in every mode, the DRY RUN included.
    pilot = uuid.uuid4()
    cis.request(pilot, a["tenant"], is_pilot=True)
    await _handler(cis, keys, store, apply=True)(app_pool, _job())
    assert [(r["status"], r["error_code"]) for r in cis.mine(pilot)] == [("refused", "pilot_refused")]

    # On Dev a tenant CIS does not call synthetic gets a DRY RUN, never an apply.
    real = uuid.uuid4()
    cis.request(real, a["tenant"], is_synthetic=False)
    await _handler(cis, keys, store, apply=True)(app_pool, _job())
    [dry] = cis.mine(real)
    assert dry["status"] == "ok"
    cis.request(real, a["tenant"], is_synthetic=False, phase="apply", approved=dry["plan_hash"],
                expires="2026-11-16T03:00:00Z")  # fmt: skip
    await _handler(cis, keys, store, apply=True)(app_pool, _job())
    assert (cis.mine(real)[-1]["status"], cis.mine(real)[-1]["error_code"]) == ("refused", "not_synthetic")
    # An expired approval is refused too.
    late = uuid.uuid4()
    cis.request(late, a["tenant"])
    await _handler(cis, keys, store, apply=True)(app_pool, _job())
    cis.request(
        late, a["tenant"], phase="apply", approved=cis.mine(late)[0]["plan_hash"], expires="2026-11-15T02:00:00Z"
    )
    await _handler(cis, keys, store, apply=True)(app_pool, _job())
    assert cis.mine(late)[-1]["error_code"] == "approval_expired"
    assert len(await _sql(app_pool, a["tenant"], "SELECT 1 FROM crm.traveler")) == 2


async def test_a_tenant_without_an_agency_erases_its_studio_invitations(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    keys, store, cis = _keys(), MemoryDocumentStore(), FakeCis()
    tenant, rid = uuid.uuid4(), uuid.uuid4()
    await _invitation(app_pool, tenant)
    cis.request(rid, tenant)
    await _handler(cis, keys, store, apply=True)(app_pool, _job())
    [dry] = cis.mine(rid)
    assert dry["status"] == "ok" and dry["tables"] == [
        {"table": "crm.studio_invitation", "action": "delete", "rows": 1, "digest": None}
    ]
    cis.request(rid, tenant, phase="apply", approved=dry["plan_hash"], expires="2026-11-16T03:00:00Z")
    await _handler(cis, keys, store, apply=True)(app_pool, _job())
    applied, verified = cis.mine(rid)[1:]
    assert applied["status"] == "ok" and verified["status"] == "ok"
    assert verified["census"]["pii_rows_found"] == 0
    assert await _invitations(admin, tenant) == 0


async def test_an_agency_the_owner_erased_applies_the_entries_of_its_plan(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    """Review S281: the owner's own erase (AA-942) leaves the agency offboarded with its name. The saga's DRY RUN and
    apply then give the same entries (all erase counts 0, no live key, the name replaced, the invitations deleted)."""
    keys, store, cis = _keys(), MemoryDocumentStore(), FakeCis()
    a = await _inquiry(app_pool, keys)
    tenant, rid = a["tenant"], uuid.uuid4()
    await _invitation(app_pool, tenant)
    async with tenant_tx(app_pool, a["owner"], tenant) as ctx:
        req = await offboarding.request(ctx, export_waived=True)
        await offboarding.confirm_erase(ctx, uuid.UUID(req["request_id"]), confirm_tenant_id=tenant, dry_run=False)
    owner_erase = offboarding.build_handler(keys=keys, store=store, dry_run=False, dek_cache_ttl_s=300)
    [erase_job] = await _jobs(app_pool, "crm_offboard", tenant)
    await owner_erase(app_pool, erase_job)
    [agency] = await _owner_fetch(admin, tenant, "SELECT status, display_name FROM crm.tenant")
    assert agency["status"] == "offboarded" and agency["display_name"] != saga_rules.erased_name(tenant)

    cis.request(rid, tenant)
    await _handler(cis, keys, store, apply=True)(app_pool, _job())
    [dry] = cis.mine(rid)
    tables = {(t["table"], t["action"]): t["rows"] for t in dry["tables"]}
    assert dry["status"] == "ok" and tables[("crm.tenant", "anonymise")] == 1
    assert tables[("crm.studio_invitation", "delete")] == 1 and tables[("crm.tenant_key", "shred")] == 0
    assert tables[("crm.traveler", "delete")] == 0
    cis.request(rid, tenant, phase="apply", approved=dry["plan_hash"], expires="2026-11-16T03:00:00Z")
    await _handler(cis, keys, store, apply=True)(app_pool, _job())
    applied, verified = cis.mine(rid)[1:]
    assert (applied["status"], applied["plan_hash"]) == ("ok", dry["plan_hash"])
    assert applied["tables"] == dry["tables"]  # the apply did what the approved plan said, entry by entry
    assert verified["status"] == "ok" and verified["census"]["pii_rows_found"] == 0
    [agency] = await _owner_fetch(admin, tenant, "SELECT status, display_name FROM crm.tenant")
    assert (agency["status"], agency["display_name"]) == ("offboarded", saga_rules.erased_name(tenant))
    assert await _invitations(admin, tenant) == 0


class FlakyStore(MemoryDocumentStore):
    """The bucket refuses the first ``fail`` deletes."""

    def __init__(self, fail: int) -> None:
        super().__init__()
        self.fail = fail

    async def delete(self, key: str) -> None:
        if self.fail:
            self.fail -= 1
            raise Unavailable("documents_unavailable")
        await super().delete(key)


async def test_a_bucket_error_at_the_end_of_an_apply_rolls_it_back_and_the_next_run_redoes_it(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    """Review S281: the objects are deleted last inside the apply's transaction (as the owner's offboarding erase): a
    bucket error keeps the rows, the keys and no apply report, so the next run applies again and no file is left."""
    keys, store, cis = _keys(), FlakyStore(fail=0), FakeCis()
    a = await _agency_with_data(app_pool, keys, store)
    tenant, rid = a["tenant"], uuid.uuid4()
    cis.request(rid, tenant)
    await _handler(cis, keys, store, apply=True)(app_pool, _job())
    [dry] = cis.mine(rid)
    cis.request(rid, tenant, phase="apply", approved=dry["plan_hash"], expires="2026-11-16T03:00:00Z")
    store.fail = 1
    with pytest.raises(tenant_erase._ObjectsNotDeleted):
        await _handler(cis, keys, store, apply=True)(app_pool, _job())
    assert [r["mode"] for r in await _record(admin, rid)] == ["dry_run"] and len(cis.mine(rid)) == 1
    assert len(await _sql(app_pool, tenant, "SELECT 1 FROM crm.traveler")) == 2
    assert [k for k in store.objects if k.startswith(f"tenants/{tenant}/")], "no file deleted"
    keys_left = await _owner_fetch(admin, tenant, "SELECT destroyed_at FROM crm.tenant_key")
    assert keys_left and all(k["destroyed_at"] is None for k in keys_left), "the key destroy rolled back"

    await _handler(cis, keys, store, apply=True)(app_pool, _job())
    applied, verified = cis.mine(rid)[1:]
    assert (applied["status"], applied["plan_hash"], verified["status"]) == ("ok", dry["plan_hash"], "ok")
    assert not [k for k in store.objects if k.startswith(f"tenants/{tenant}/")], "documents erased"


async def test_two_runs_at_once_make_one_report(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    """Review S281: a rolling deploy or a reaped lease runs two workers at once. The request's work lock makes the
    second wait; under it the second reads the report the first made, and neither records nor posts a second one."""
    keys, store, cis = _keys(), MemoryDocumentStore(), FakeCis()
    tenant, rid = uuid.uuid4(), uuid.uuid4()
    cis.request(rid, tenant)
    req = saga_rules.parse_request(cis.items[str(rid)])
    assert isinstance(req, saga_rules.SagaRequest) and await saga_store.upsert_request(app_pool, req)
    body = saga_rules.report_body(
        req, report_id=uuid.uuid4(), mode="dry_run", outcome="refused", error_code="pilot_refused", reported_at=NOW
    )

    async with app_pool.acquire() as first, app_pool.acquire() as second:

        async def other_run() -> bool:
            async with second.transaction():
                return await saga_store.claim(second, rid, req.round, "dry_run")

        async with first.transaction():
            assert await saga_store.claim(first, rid, req.round, "dry_run")
            waiting = asyncio.create_task(other_run())
            await asyncio.sleep(0.3)
            assert not waiting.done(), "the second run waits for the lock"
            assert await saga_store.insert_report(first, body)
        assert await waiting is False  # under the lock it reads the report the first run made

    # A run that read the record before the other committed: its work finds the report and keeps nothing.
    handler = _handler(cis, keys, store, apply=True)
    assert await handler._report(app_pool, req, "dry_run", _job()) is None
    assert [(r["mode"], r["outcome"]) for r in await _record(admin, rid)] == [("dry_run", "refused")]
    assert cis.reports == []
