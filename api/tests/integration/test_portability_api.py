"""AA-967 (card CRM-35, Module 18) end to end on PostgreSQL 15 with the RDS role shape, as crm_app, with real field
encryption (crm-026).

- Agency export: the ``crm_data_export`` job builds the ZIP with progress, seals and stores it; the owner's
  single-use link downloads it; every table's row count in the ZIP equals the database's (the "re-imports with equal
  counts" check of the card, on the counts), and the document comes back byte for byte.
- Client export: only the rows about that client.
- Data-request log -> client export -> erasure: the erasure withdraws the client's export (object deleted), verifies the
  client is gone, writes the receipt (``verified_at``) and closes the logged request with it.
- Agency deletion with a cool-off: the confirmation schedules the erase and the agency keeps working; after the
  cool-off the job erases, withdraws the exports, crypto-shreds and proves a canary no longer opens.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import uuid
import zipfile
from typing import Any

import asyncpg
import pytest

from app.documents import service as documents
from app.documents.store import MemoryDocumentStore
from app.errors import Conflict, Forbidden
from app.lifecycle import offboarding, trash
from app.portability import exports, requests, subject
from app.portability import tables as T
from app.tenancy import tenant_tx
from app.vault import KeyProvider, Vault
from tests.integration.test_crm005_intake import _keys, _owner_fetch
from tests.integration.test_crm010_lifecycle import JPEG, _done, _inquiry, _jobs, _raises, _sql

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _upload(pool: asyncpg.Pool, keys: KeyProvider, store: MemoryDocumentStore, a: dict[str, Any]) -> None:
    async with tenant_tx(pool, a["owner"], a["tenant"]) as ctx:
        await documents.upload(
            ctx,
            Vault(keys, ctx),
            store,
            traveler_id=a["lead"],
            trip_id=a["trip"],
            kind="other",
            filename="ticket.jpg",
            content_type="image/jpeg",
            content=JPEG,
        )


async def _export(
    pool: asyncpg.Pool,
    keys: KeyProvider,
    store: MemoryDocumentStore,
    a: dict[str, Any],
    scope: str = "tenant",
    **kw: Any,
) -> dict[str, Any]:
    async with tenant_tx(pool, a["owner"], a["tenant"]) as ctx:
        queued = await exports.request(ctx, scope, kw.get("traveler"), data_request_id=kw.get("data_request"))
    assert queued["status"] == "queued"
    [job] = await _jobs(pool, "crm_data_export", a["tenant"])
    result = await exports.build_handler(keys=keys, store=store)(pool, job)
    await _done(pool, job)
    assert result == {"export_id": queued["export_id"], "status": "ready"}
    async with tenant_tx(pool, a["owner"], a["tenant"]) as ctx:
        [row] = [e for e in (await exports.list_exports(ctx))["exports"] if e["export_id"] == queued["export_id"]]
    return row


async def _download(pool: asyncpg.Pool, keys: KeyProvider, store: MemoryDocumentStore, a: dict[str, Any],
                    export_id: str) -> tuple[zipfile.ZipFile, str]:  # fmt: skip
    async with tenant_tx(pool, a["owner"], a["tenant"]) as ctx:
        link = await exports.create_link(ctx, uuid.UUID(export_id), store)
    async with tenant_tx(pool, a["owner"], a["tenant"]) as ctx:
        content, _name, digest = await exports.download(ctx, Vault(keys, ctx), store, uuid.UUID(export_id),
                                                        link["token"])  # fmt: skip
    assert hashlib.sha256(content).hexdigest() == digest
    return zipfile.ZipFile(io.BytesIO(content)), link["token"]


async def test_the_agency_export_holds_every_table_with_equal_counts(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    keys = _keys()
    store = MemoryDocumentStore()
    a = await _inquiry(app_pool, keys, "owner", "agent")
    await _upload(app_pool, keys, store, a)
    (_, _), (agent, _) = a["people"]
    with _raises(Forbidden, "owner_only"):
        async with tenant_tx(app_pool, agent, a["tenant"]) as ctx:
            await exports.request(ctx, "tenant", None)

    row = await _export(app_pool, keys, store, a)
    assert row["progress"]["done"] == row["progress"]["total"] > 0 and row["content_sha256"]
    assert row["documents_included"] == 1 and row["expires_at"]
    zf, token = await _download(app_pool, keys, store, a, row["export_id"])
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["schema_version"] == 1 and "crm-026" in manifest["crm_schema_versions"]
    # Every table in the ZIP has the database's row count; the never-exported tables are not there.
    for entry in manifest["tables"]:
        assert entry["table"] not in T.NEVER_EXPORT_TABLES
        [db] = await _owner_fetch(admin, a["tenant"], f"SELECT count(*) AS n FROM crm.{entry['table']}")  # noqa: S608
        assert entry["rows"] == db["n"] == len(json.loads(zf.read(entry["json"]))), entry["table"]
        assert len(list(csv.reader(io.StringIO(zf.read(entry["csv"]).decode("utf-8-sig"))))) == db["n"] + 1
    exported_tables = {e["table"] for e in manifest["tables"]}
    assert {"traveler", "trip", "consent", "form_submission", "document", "member"} <= exported_tables
    travellers = json.loads(zf.read("json/traveler.json"))
    assert "Nguyễn Thị Hoa" in {t["display_name"] for t in travellers}
    assert not any(k.endswith("_bidx") or k.endswith("_enc") for t in travellers for k in t)
    [doc] = list(csv.DictReader(io.StringIO(zf.read("documents/manifest.csv").decode("utf-8-sig"))))
    assert doc["included"] == "true" and zf.read(doc["path"]) == JPEG
    # The link is single use; AA staff have no route at all (unit test), an agent is refused above.
    with _raises(Forbidden, "link_invalid"):
        async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
            await exports.download(ctx, Vault(keys, ctx), store, uuid.UUID(row["export_id"]), token)
    events = await _owner_fetch(
        admin, a["tenant"], "SELECT action, outcome FROM crm.audit_event WHERE action LIKE 'export_%' ORDER BY at"
    )
    got = [(e["action"], e["outcome"]) for e in events]
    for step in (("export_request", "allowed"), ("export_create", "allowed"), ("export_link", "allowed"),
                 ("export_download", "allowed"), ("export_download", "denied")):  # fmt: skip
        assert step in got
    # One open export per subject.
    async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
        await exports.request(ctx, "tenant", None)
        with _raises(Conflict, "export_running"):
            await exports.request(ctx, "tenant", None)


async def test_a_logged_request_a_client_export_and_the_verified_erasure(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    keys = _keys()
    store = MemoryDocumentStore()
    a = await _inquiry(app_pool, keys)
    b = await _inquiry(app_pool, keys)
    await _upload(app_pool, keys, store, a)
    async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
        logged = await requests.create(ctx, kind="erasure", channel="aa_forwarded",
                                       received_on=dt.date.today(), traveler_id=a["lead"])  # fmt: skip
    assert logged["status"] == "open" and logged["due_on"] == (dt.date.today() + dt.timedelta(days=30)).isoformat()
    log_id = uuid.UUID(logged["data_request_id"])

    row = await _export(app_pool, keys, store, a, "traveler", traveler=a["lead"], data_request=log_id)
    zf, _ = await _download(app_pool, keys, store, a, row["export_id"])
    manifest = json.loads(zf.read("manifest.json"))
    names = {e["table"] for e in manifest["tables"]}
    assert "traveler" in names and "tenant_settings" not in names and "member" not in names
    assert [t["traveler_id"] for t in json.loads(zf.read("json/traveler.json"))] == [str(a["lead"])]
    assert all(c["traveler_id"] == str(a["lead"]) for c in json.loads(zf.read("json/consent.json")))
    assert [t["trip_id"] for t in json.loads(zf.read("json/trip.json"))] == [str(a["trip"])]
    object_key = exports.object_key(a["tenant"], uuid.UUID(row["export_id"]))
    assert object_key in store.objects

    # Erasure: the client is on a live trip -> refused; with the trip in the trash it runs.
    async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
        with _raises(Conflict, "traveler_on_trip"):
            await subject.erase_client(ctx, Vault(keys, ctx), store, a["lead"], dry_run=True, deletion_dry_run=False)
    async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
        await trash.move_to_trash(ctx, "trip", a["trip"])
    async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
        preview = await subject.erase_client(ctx, Vault(keys, ctx), store, a["lead"], dry_run=True,
                                             deletion_dry_run=False)  # fmt: skip
        assert preview["dry_run"] is True and preview["exports_withdrawn"] == 1 and preview["counts"]["traveler"] == 1
        with _raises(Conflict, "deletion_dry_run"):
            await subject.erase_client(ctx, Vault(keys, ctx), store, a["lead"], dry_run=False, deletion_dry_run=True)
    async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
        done = await subject.erase_client(ctx, Vault(keys, ctx), store, a["lead"], dry_run=False,
                                          deletion_dry_run=False, data_request_id=log_id)  # fmt: skip
    receipt = done["receipt"]
    assert receipt["verify"] == {"subject_gone": True} and receipt["exports_withdrawn"] == 1
    assert receipt["counts"]["traveler"] == 1 and "backup_purge_by" in receipt
    assert object_key not in store.objects, "the client's export object is deleted with them"
    [req_row] = await _owner_fetch(
        admin, a["tenant"], "SELECT status, verified_at, receipt FROM crm.erasure_request WHERE request_id = $1",
        uuid.UUID(done["request_id"]),
    )  # fmt: skip
    assert req_row["status"] == "erased" and req_row["verified_at"] is not None
    async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
        [entry] = (await requests.list_requests(ctx))["requests"]
        [kept_export] = (await exports.list_exports(ctx))["exports"]
    assert (entry["status"], entry["outcome"], entry["erasure_request_id"]) == ("done", "fulfilled", done["request_id"])
    assert entry["traveler_id"] == str(a["lead"]) and kept_export["status"] == "cancelled"
    # The other agency sees none of it.
    assert await _sql(app_pool, b["tenant"], "SELECT 1 FROM crm.data_request") == []
    assert await _sql(app_pool, b["tenant"], "SELECT 1 FROM crm.export_job") == []


async def test_the_agency_deletion_waits_for_its_cool_off_then_verifies(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    keys = _keys()
    store = MemoryDocumentStore()
    a = await _inquiry(app_pool, keys)
    await _upload(app_pool, keys, store, a)
    ready = await _export(app_pool, keys, store, a)
    object_key = exports.object_key(a["tenant"], uuid.UUID(ready["export_id"]))
    async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
        req = await offboarding.request(ctx, export_waived=True)
    request_id = uuid.UUID(req["request_id"])
    async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
        scheduled = await offboarding.confirm_erase(ctx, request_id, confirm_tenant_id=a["tenant"], dry_run=False,
                                                    cool_off_hours=72)  # fmt: skip
        with _raises(Conflict, "erase_scheduled"):
            await offboarding.confirm_erase(ctx, request_id, confirm_tenant_id=a["tenant"], dry_run=False,
                                            cool_off_hours=72)  # fmt: skip
    assert scheduled["status"] == "requested" and scheduled["confirmed"] and scheduled["erase_not_before"]
    # The agency keeps working during the cool-off (a member context opens), and the job waits.
    async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
        assert (await offboarding.latest(ctx))["erase_not_before"] == scheduled["erase_not_before"]
    async with app_pool.acquire() as conn:
        due = await conn.fetchval(
            "SELECT run_after FROM crm.job WHERE kind = 'crm_offboard' AND payload->>'request_id' = $1", str(request_id)
        )
    assert due > dt.datetime.now(dt.UTC) + dt.timedelta(hours=71)
    [erase_job] = await _jobs(app_pool, "crm_offboard", a["tenant"])
    handler = offboarding.build_handler(keys=keys, store=store, dry_run=False, dek_cache_ttl_s=300)
    with pytest.raises(Exception, match="cool_off_running"):  # run early: retried later, nothing changed
        await handler(app_pool, erase_job)
    assert (await _owner_fetch(admin, a["tenant"], "SELECT status FROM crm.tenant"))[0]["status"] == "active"

    # The cool-off ends (moved back here instead of waiting 72 hours).
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE crm_owner")
        await admin.execute("SELECT set_config('app.tenant_id', $1, true)", str(a["tenant"]))
        await admin.execute(
            "UPDATE crm.erasure_request SET erase_not_before = now() - interval '1 minute' WHERE request_id = $1",
            request_id,
        )
    result = await handler(app_pool, erase_job)
    assert result["status"] == "erased"
    [row] = await _owner_fetch(admin, a["tenant"], "SELECT status, receipt, verified_at FROM crm.erasure_request "
                               "WHERE request_id = $1", request_id)  # fmt: skip
    receipt = json.loads(row["receipt"])
    assert row["status"] == "erased" and row["verified_at"] is not None
    assert receipt["verify"]["canary_unreadable"] is True and receipt["verify"]["keys_destroyed"] >= 1
    assert receipt["exports_withdrawn"] == 1 and {"export_job", "data_request"} <= set(receipt["kept"])
    assert object_key not in store.objects
    assert (await _owner_fetch(admin, a["tenant"], "SELECT status FROM crm.tenant"))[0]["status"] == "offboarded"
    [kept] = await _owner_fetch(admin, a["tenant"], "SELECT status, object_key FROM crm.export_job")
    assert (kept["status"], kept["object_key"]) == ("cancelled", None)


async def test_a_cancel_during_the_cool_off_stops_the_erase(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    keys = _keys()
    store = MemoryDocumentStore()
    a = await _inquiry(app_pool, keys)
    async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
        req = await offboarding.request(ctx, export_waived=True)
        request_id = uuid.UUID(req["request_id"])
        await offboarding.confirm_erase(ctx, request_id, confirm_tenant_id=a["tenant"], dry_run=False, cool_off_hours=1)
    async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
        assert (await offboarding.cancel(ctx, request_id, store))["status"] == "cancelled"
    [erase_job] = await _jobs(app_pool, "crm_offboard", a["tenant"])
    handler = offboarding.build_handler(keys=keys, store=store, dry_run=False, dek_cache_ttl_s=300)
    assert (await handler(app_pool, erase_job))["status"] == "skipped"
    assert (await _owner_fetch(admin, a["tenant"], "SELECT status FROM crm.tenant"))[0]["status"] == "active"
    assert len(await _sql(app_pool, a["tenant"], "SELECT 1 FROM crm.traveler")) >= 1
