"""AA-962 (card CRM-31) end to end on PostgreSQL 15 with the RDS role shape, real field encryption and an in-memory
ingest bucket: the phased client import (dry run, apply as crm_client_import jobs, re-run), duplicate review and
merge, and the onboarding checklist. Every name, address and number below is invented.

- **R**: a file without a required column is refused before any write (no job, no row, no object).
- **E**: re-running the same import creates no duplicates; merging two clients keeps both trip histories and one live
  F1 record.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import uuid
from collections.abc import Iterator
from typing import Any

import asyncpg
import pytest

from app.errors import Conflict, Forbidden, Invalid
from app.imports import service as imports
from app.intake import forms as intake_forms
from app.intake import public as intake
from app.jobs.queue import Job
from app.mail.store import MemoryIngestStore
from app.onboarding import ACK_VERSION, acknowledge, overview, set_controller, set_step
from app.permissions import Action
from app.tenancy import tenant_tx
from app.travellers import merge
from app.vault import Vault
from tests.integration.test_crm005_intake import _agency, _keys
from tests.integration.test_crm010_lifecycle import _inquiry

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

CLIENTS = (
    "Client ID,Full name,Email,Phone,Date of birth,Marketing\n"
    "C-1,Lê Minh Châu,chau.le@example.test,+84901112233,1988-04-12,yes\n"
    "C-2,Ana Pereira,ana.pereira@example.test,,,no\n"
    "C-3,Tobias Reed,,+61400111222,,\n"
    "C-4,Ana Pereira,,,,\n"
    "C-5,,missing.name@example.test,,,\n"
    "C-1,Lê Minh Châu,again@example.test,,,\n"
)
RELATIONSHIPS = "client_ref,related_client_ref,relation\nC-1,C-3,spouse\nC-3,C-1,spouse\nC-1,C-99,friend\n"
TRIPS = (
    "trip_ref,client_ref,title,start_date,end_date,status,companion_refs\n"
    "T-1,C-1,Hà Giang loop,2025-03-01,2025-03-08,,C-3\n"
    "T-2,C-2,Mekong slow boat,01/09/2024,05/09/2024,completed,\n"
    "T-3,C-77,Unknown lead,,,,\n"
)
NOTES = "client_ref,trip_ref,date,text\nC-1,,2025-02-01,Prefers aisle seats\n,T-1,,Asked for a vegetarian cook\n"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def _owner(pool: asyncpg.Pool) -> dict[str, Any]:
    tenant_id, people = await _agency(pool, "owner", "agent")
    return {
        "tenant": tenant_id,
        "owner": people[0][0],
        "owner_id": people[0][1],
        "agent": people[1][0],
        "keys": _keys(),
    }


async def _preview(pool: asyncpg.Pool, d: dict[str, Any], entity: str, data: bytes, **kw: Any) -> dict[str, Any]:
    async with tenant_tx(pool, d["owner"], d["tenant"], action=Action.CLIENT_IMPORT) as ctx:
        return await imports.preview(ctx, Vault(d["keys"], ctx), entity=entity, data=data, region=kw.get("region"))


async def _import(pool: asyncpg.Pool, d: dict[str, Any], entity: str, data: bytes) -> dict[str, Any]:
    """Start the import and run its jobs like the worker would; the aggregated status."""
    store = d.setdefault("store", MemoryIngestStore())
    async with tenant_tx(pool, d["owner"], d["tenant"], action=Action.CLIENT_IMPORT) as ctx:
        started = await imports.start(ctx, store, entity=entity, data=data, sha256=_sha(data), region=None)
    handler = imports.build_handler(store, d["keys"])
    while True:
        rows = await pool.fetch(
            "SELECT id::text AS id, payload FROM crm.job WHERE kind = 'crm_client_import' AND status = 'queued' "
            "AND payload->>'import_id' = $1 ORDER BY created_at",
            started["import_id"],
        )
        if not rows:
            break
        for r in rows:
            job = Job(r["id"], imports.KIND, json.loads(r["payload"]), 1, 3)
            result = await handler(pool, job)
            await pool.execute(
                "UPDATE crm.job SET status = 'succeeded', result = $2::jsonb, finished_at = now() WHERE id = $1::uuid",
                r["id"],
                json.dumps(result),
            )
    async with tenant_tx(pool, d["owner"], d["tenant"], action=Action.CLIENT_IMPORT) as ctx:
        return await imports.status(ctx, uuid.UUID(started["import_id"]))


async def _sql(pool: asyncpg.Pool, tenant_id: uuid.UUID, sql: str, *args: Any) -> Any:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        return await conn.fetch(sql, *args)


@contextlib.contextmanager
def _raises(cls: type[Exception], code: str) -> Iterator[Any]:
    with pytest.raises(cls) as err:
        yield err
    assert getattr(err.value, "code", None) == code


async def test_card_r_missing_column_is_refused_before_any_write(app_pool: asyncpg.Pool) -> None:
    d = await _owner(app_pool)
    data = b"client_ref,email\nC-1,someone@example.test\n"
    with _raises(Invalid, "missing_columns") as err:
        await _preview(app_pool, d, "clients", data)
    assert err.value.extra["columns"] == ["name"]
    store = MemoryIngestStore()
    with _raises(Invalid, "missing_columns"):
        async with tenant_tx(app_pool, d["owner"], d["tenant"], action=Action.CLIENT_IMPORT) as ctx:
            await imports.start(ctx, store, entity="clients", data=data, sha256=_sha(data), region=None)
    assert store.objects == {}
    assert (await _sql(app_pool, d["tenant"], "SELECT count(*) AS n FROM crm.traveler"))[0]["n"] == 0
    jobs = await app_pool.fetchval(
        "SELECT count(*) FROM crm.job WHERE kind = 'crm_client_import' AND payload->>'tenant_id' = $1", str(d["tenant"])
    )
    assert jobs == 0
    events = await _sql(app_pool, d["tenant"], "SELECT action FROM crm.audit_event WHERE action LIKE 'import%'")
    assert events == []


async def test_dry_run_counts_then_apply_then_rerun_adds_nothing(app_pool: asyncpg.Pool) -> None:
    d = await _owner(app_pool)
    data = CLIENTS.encode()
    preview = await _preview(app_pool, d, "clients", data)
    assert preview["counts"] == {
        "create": 4,
        "match": 0,
        "already_imported": 0,
        "duplicate_in_file": 1,
        "invalid": 1,
    }
    assert preview["possible_duplicate_count"] == 1  # the second "Ana Pereira" (no shared e-mail or phone)
    issues = {i["row"]: i["codes"] for i in preview["issues"]}
    assert (
        issues[6] == ["invalid_name"] and issues[7] == ["duplicate_client_ref"] and issues[5] == ["possible_duplicate"]
    )
    assert "Lê Minh Châu" not in str(preview) and "example.test" not in str(preview)  # codes only, no value echoed
    assert (await _sql(app_pool, d["tenant"], "SELECT count(*) AS n FROM crm.traveler"))[0]["n"] == 0

    done = await _import(app_pool, d, "clients", data)
    assert done["status"] == "done" and done["counts"]["create"] == 4 and done["row_count"] == 6
    rows = await _sql(
        app_pool,
        d["tenant"],
        "SELECT lead_source, owner_member_id, import_ref_bidx IS NOT NULL AS has_ref FROM crm.traveler",
    )
    assert len(rows) == 4 and all(r["lead_source"] == "import" and r["has_ref"] for r in rows)
    assert {r["owner_member_id"] for r in rows} == {d["owner_id"]}
    consents = await _sql(app_pool, d["tenant"], "SELECT purpose, value, source FROM crm.consent ORDER BY value")
    assert [(c["purpose"], c["value"], c["source"]) for c in consents] == [
        ("marketing", False, "import"),
        ("marketing", True, "import"),
    ]
    assert d["store"].objects == {}  # the file is deleted after the last slice

    # Card E: the same file again creates nothing.
    again = await _preview(app_pool, d, "clients", data)
    # (the repeated C-1 row now meets the imported C-1: already imported, no longer a duplicate in the file)
    assert again["counts"]["create"] == 0 and again["counts"]["already_imported"] == 5
    rerun = await _import(app_pool, d, "clients", data)
    assert rerun["counts"]["create"] == 0 and rerun["counts"]["already_imported"] == 5
    assert (await _sql(app_pool, d["tenant"], "SELECT count(*) AS n FROM crm.traveler"))[0]["n"] == 4

    async with tenant_tx(app_pool, d["owner"], d["tenant"], action=Action.CLIENT_IMPORT) as ctx:
        listed = await imports.recent(ctx)
    assert [i["status"] for i in listed["imports"]] == ["done", "done"]


async def test_phases_relationships_trips_notes_and_rerun(app_pool: asyncpg.Pool) -> None:
    d = await _owner(app_pool)
    await _import(app_pool, d, "clients", CLIENTS.encode())

    rel = await _import(app_pool, d, "relationships", RELATIONSHIPS.encode())
    assert rel["counts"] == {"create": 1, "match": 0, "already_imported": 0, "duplicate_in_file": 1, "invalid": 1}
    trips = await _import(app_pool, d, "trips", TRIPS.encode())
    assert trips["counts"]["create"] == 2 and trips["counts"]["invalid"] == 1
    notes = await _import(app_pool, d, "notes", NOTES.encode())
    assert notes["counts"]["create"] == 2

    stages = await _sql(app_pool, d["tenant"], "SELECT stage, waiting_on FROM crm.trip ORDER BY title")
    assert [(s["stage"], s["waiting_on"]) for s in stages] == [("completed", "none"), ("completed", "none")]
    party = await _sql(app_pool, d["tenant"], "SELECT role FROM crm.trip_traveler ORDER BY role")
    assert [p["role"] for p in party] == ["companion", "lead", "lead"]
    assert (await _sql(app_pool, d["tenant"], "SELECT count(*) AS n FROM crm.trip_party"))[0]["n"] == 3
    history = await _sql(app_pool, d["tenant"], "SELECT reason_code FROM crm.trip_stage_history")
    assert {h["reason_code"] for h in history} == {"import"}
    relations = await _sql(app_pool, d["tenant"], "SELECT kind FROM crm.traveler_relation WHERE deleted_at IS NULL")
    assert [r["kind"] for r in relations] == ["spouse", "spouse"]  # the database wrote the inverse
    dated = await _sql(
        app_pool, d["tenant"], "SELECT created_at::date AS day FROM crm.note WHERE traveler_id IS NOT NULL"
    )
    assert str(dated[0]["day"]) == "2025-02-01"

    # Re-running every phase adds nothing.
    for entity, text in (("relationships", RELATIONSHIPS), ("trips", TRIPS), ("notes", NOTES)):
        again = await _import(app_pool, d, entity, text.encode())
        assert again["counts"]["create"] == 0, entity
    for table, n in (("trip", 2), ("note", 2), ("trip_traveler", 3)):
        assert (await _sql(app_pool, d["tenant"], f"SELECT count(*) AS n FROM crm.{table}"))[0]["n"] == n  # noqa: S608


async def test_a_known_email_matches_the_existing_client(app_pool: asyncpg.Pool) -> None:
    keys = _keys()
    d = await _inquiry(app_pool, keys, "owner")
    d["keys"] = keys
    before = (await _sql(app_pool, d["tenant"], "SELECT count(*) AS n FROM crm.traveler"))[0]["n"]
    data = b"client_ref,name,email\nOLD-9,Hoa N.,hoa.nguyen@example.test\n"
    preview = await _preview(app_pool, d, "clients", data)
    assert preview["counts"]["match"] == 1
    result = await _import(app_pool, d, "clients", data)
    assert result["counts"]["match"] == 1
    assert (await _sql(app_pool, d["tenant"], "SELECT count(*) AS n FROM crm.traveler"))[0]["n"] == before
    linked = await _sql(
        app_pool,
        d["tenant"],
        "SELECT import_ref_bidx IS NOT NULL AS ok FROM crm.traveler WHERE traveler_id = $1",
        d["lead"],
    )
    assert linked[0]["ok"]
    assert (await _import(app_pool, d, "clients", data))["counts"]["already_imported"] == 1


async def test_file_changed_and_local_phones_with_a_region(app_pool: asyncpg.Pool) -> None:
    d = await _owner(app_pool)
    data = b"client_ref,name,phone\nP-1,Quang Vu,0901112244\n"
    assert (await _preview(app_pool, d, "clients", data))["counts"]["invalid"] == 1
    assert (await _preview(app_pool, d, "clients", data, region="VN"))["counts"]["create"] == 1
    with _raises(Conflict, "file_changed"):
        async with tenant_tx(app_pool, d["owner"], d["tenant"], action=Action.CLIENT_IMPORT) as ctx:
            await imports.start(ctx, MemoryIngestStore(), entity="clients", data=data, sha256="0" * 64, region="VN")


async def test_duplicates_and_merge_keep_both_trip_histories_and_one_f1_record(app_pool: asyncpg.Pool) -> None:
    d = await _owner(app_pool)
    await _import(
        app_pool,
        d,
        "clients",
        b"client_ref,name,email,phone\nA-1,Ines Varga,ines.v@example.test,\nA-2,Ines Varga,,+84903334455\n",
    )
    await _import(
        app_pool,
        d,
        "trips",
        b"trip_ref,client_ref,title,start_date,end_date\nTA,A-1,Coast ride,2024-01-02,2024-01-09\n"
        b"TB,A-2,Highland walk,2024-06-02,2024-06-07\n",
    )
    await _import(app_pool, d, "notes", b"client_ref,text\nA-2,Likes early starts\n")
    async with tenant_tx(app_pool, d["owner"], d["tenant"], action=Action.CLIENT_VIEW) as ctx:
        found = await merge.candidates(ctx, Vault(d["keys"], ctx))
    (pair,) = found["pairs"]
    assert pair["matched_on"] == ["name"] and found["total"] == 1
    ids = {c["traveler_id"] for c in pair["clients"]}
    by_ref = {
        r["n"]: r["traveler_id"]
        for r in await _sql(app_pool, d["tenant"], "SELECT traveler_id, phone_enc IS NOT NULL AS n FROM crm.traveler")
    }
    keep, other = by_ref[False], by_ref[True]  # keep the one with the e-mail; the other has the phone
    assert {str(keep), str(other)} == ids

    # The agent may not merge (owner only, like a client delete).
    with pytest.raises(Forbidden):
        async with tenant_tx(app_pool, d["agent"], d["tenant"], action=Action.CLIENT_MERGE):
            pass  # pragma: no cover

    async with tenant_tx(app_pool, d["owner"], d["tenant"], action=Action.CLIENT_MERGE) as ctx:
        out = await merge.merge(ctx, Vault(d["keys"], ctx), keep, other)
    assert out["trips_moved"] == 1 and out["fields_filled"] == ["phone"]
    live = await _sql(
        app_pool,
        d["tenant"],
        "SELECT traveler_id, phone_bidx IS NOT NULL AS p FROM crm.traveler WHERE deleted_at IS NULL",
    )
    assert [(r["traveler_id"], r["p"]) for r in live] == [(keep, True)]
    trips = await _sql(app_pool, d["tenant"], "SELECT traveler_id FROM crm.trip_traveler")
    assert {t["traveler_id"] for t in trips} == {keep} and len(trips) == 2
    notes = await _sql(app_pool, d["tenant"], "SELECT traveler_id FROM crm.note")
    assert [n["traveler_id"] for n in notes] == [keep]
    async with tenant_tx(app_pool, d["owner"], d["tenant"], action=Action.CLIENT_VIEW) as ctx:
        assert (await merge.candidates(ctx, Vault(d["keys"], ctx)))["pairs"] == []
        events = await ctx.conn.fetch("SELECT action, fields FROM crm.audit_event WHERE action = 'merge'")
    assert events[0]["fields"] == ["phone"]
    # Re-importing the merged client's ref finds the kept client (its ref was carried over or matched).
    again = await _import(
        app_pool,
        d,
        "clients",
        b"client_ref,name,email,phone\nA-1,Ines Varga,ines.v@example.test,\nA-2,Ines Varga,,+84903334455\n",
    )
    assert again["counts"]["create"] == 0


async def _count(pool: asyncpg.Pool, d: dict[str, Any]) -> int:
    return int(
        (await _sql(pool, d["tenant"], "SELECT count(*) AS n FROM crm.traveler WHERE deleted_at IS NULL"))[0]["n"]
    )


async def test_card_e_rerun_after_merging_two_clients_with_their_own_contacts_creates_nothing(
    app_pool: asyncpg.Pool,
) -> None:
    """Both clients have their own e-mail, so the kept one gets no contact field from the other: only the carried
    import ref (an alias) lets a re-run find the merged client's rows, and its trips still resolve."""
    d = await _owner(app_pool)
    clients = b"client_ref,name,email\nM-1,Odile Brant,odile.one@example.test\nM-2,Odile Brant,odile.two@example.test\n"
    await _import(app_pool, d, "clients", clients)
    rows = await _sql(app_pool, d["tenant"], "SELECT traveler_id FROM crm.traveler ORDER BY traveler_id")
    keep, other = rows[0]["traveler_id"], rows[1]["traveler_id"]
    async with tenant_tx(app_pool, d["owner"], d["tenant"], action=Action.CLIENT_MERGE) as ctx:
        out = await merge.merge(ctx, Vault(d["keys"], ctx), keep, other)
    assert out["fields_filled"] == []
    kept = await _sql(
        app_pool,
        d["tenant"],
        "SELECT cardinality(import_ref_alias_bidx) AS n FROM crm.traveler WHERE traveler_id = $1",
        keep,
    )
    assert kept[0]["n"] == 1
    assert await _count(app_pool, d) == 1

    again = await _preview(app_pool, d, "clients", clients)
    assert again["counts"]["create"] == 0 and again["counts"]["already_imported"] == 2
    rerun = await _import(app_pool, d, "clients", clients)
    assert rerun["counts"]["create"] == 0 and await _count(app_pool, d) == 1
    # The merged client's ref still names a client: its trip goes to the kept one.
    trips = await _import(
        app_pool, d, "trips", b"trip_ref,client_ref,title,start_date\nMT-1,M-2,Lagoon kayak,2024-03-03\n"
    )
    assert trips["counts"]["create"] == 1 and trips["counts"]["invalid"] == 0
    party = await _sql(app_pool, d["tenant"], "SELECT traveler_id FROM crm.trip_traveler")
    assert [p["traveler_id"] for p in party] == [keep]


async def test_every_accepted_client_ref_resolves_in_the_later_phases(app_pool: asyncpg.Pool) -> None:
    """A row matched by e-mail to a client that already has a ref, and a row sharing an earlier row's e-mail (a family
    on one address): both refs point to that client, so their trips and notes import, and a re-run adds nothing."""
    d = await _owner(app_pool)
    await _import(app_pool, d, "clients", b"client_ref,name,email\nF-1,Pia Moss,moss.family@example.test\n")
    second = (
        b"client_ref,name,email\n"
        b"OLD-1,Pia Moss,moss.family@example.test\n"  # matches F-1's client, which already has a ref
        b"G-1,Bo Lind,lind.home@example.test\n"
        b"G-2,Cleo Lind,lind.home@example.test\n"  # shares the e-mail of the row above
    )
    preview = await _preview(app_pool, d, "clients", second)
    assert preview["counts"] == {"create": 1, "match": 2, "already_imported": 0, "duplicate_in_file": 0, "invalid": 0}
    assert {i["row"]: i["codes"] for i in preview["issues"]} == {4: ["duplicate_contact"]}
    done = await _import(app_pool, d, "clients", second)
    assert done["counts"]["create"] == 1 and done["counts"]["match"] == 2
    assert await _count(app_pool, d) == 2

    trips = await _import(
        app_pool,
        d,
        "trips",
        b"trip_ref,client_ref,title,start_date,companion_refs\n"
        b"FT-1,OLD-1,Delta cycling,2024-02-02,\nFT-2,G-2,Rice terraces,2024-05-05,G-1\n",
    )
    assert trips["counts"]["create"] == 2 and trips["counts"]["invalid"] == 0
    notes = await _import(app_pool, d, "notes", b"client_ref,text\nOLD-1,Allergic to peanuts\nG-2,Window seat\n")
    assert notes["counts"]["create"] == 2 and notes["counts"]["invalid"] == 0

    again = await _import(app_pool, d, "clients", second)
    assert again["counts"]["create"] == 0 and again["counts"]["already_imported"] == 3
    assert await _count(app_pool, d) == 2


async def test_merge_is_blocked_by_links_and_offers_the_swap(app_pool: asyncpg.Pool) -> None:
    keys = _keys()
    d = await _inquiry(app_pool, keys, "owner")
    d["keys"] = keys
    await _import(app_pool, d, "clients", b"client_ref,name\nX-1,Spare Client\n")
    (spare,) = [
        r["traveler_id"]
        for r in await _sql(app_pool, d["tenant"], "SELECT traveler_id FROM crm.traveler WHERE lead_source = 'import'")
    ]
    await _sql(
        app_pool,
        d["tenant"],
        "INSERT INTO crm.client_link (trip_id, traveler_id, purpose, token_hash, expires_at) "
        "VALUES ($1, $2, 'stage2_form', $3, now() + interval '7 days')",
        d["trip"],
        d["lead"],
        hashlib.sha256(b"invented-token").digest(),
    )
    with _raises(Conflict, "merge_blocked") as err:
        async with tenant_tx(app_pool, d["owner"], d["tenant"], action=Action.CLIENT_MERGE) as ctx:
            await merge.merge(ctx, Vault(keys, ctx), spare, d["lead"])
    assert err.value.extra == {"blockers": ["client_links"], "swap_possible": True}
    async with tenant_tx(app_pool, d["owner"], d["tenant"], action=Action.CLIENT_MERGE) as ctx:
        out = await merge.merge(ctx, Vault(keys, ctx), d["lead"], spare)
    assert out["merged_traveler_id"] == str(spare)


async def test_onboarding_checklist_steps_controller_and_acknowledgement(app_pool: asyncpg.Pool) -> None:
    d = await _owner(app_pool)
    async with tenant_tx(app_pool, d["owner"], d["tenant"], action=Action.SESSION_VIEW) as ctx:
        first = await overview(ctx, duplicate_count=0)
    steps = {s["key"]: s for s in first["steps"]}
    assert first["required_total"] == 3 and first["required_done"] == 1  # two members: the team step is done
    assert steps["invite_team"]["done"] and not steps["connect_mailbox"]["done"]
    assert not first["complete"] and not first["acknowledgement"]["accepted"]
    assert {c["key"]: c["status"] for c in first["connections"]} == {
        "mailbox": "not_set_up",
        "card_payments": "not_set_up",
        "sms_whatsapp": "not_set_up",
        "calendar": "not_set_up",
    }
    async with tenant_tx(app_pool, d["owner"], d["tenant"], action=Action.ONBOARDING_MANAGE) as ctx:
        with _raises(Invalid, "step_required"):
            await set_step(ctx, "connect_mailbox", "later")
        with _raises(Invalid, "step_not_manual"):
            await set_step(ctx, "import_clients", "done")
        await set_step(ctx, "kickoff_call", "later")
        await set_step(ctx, "embed_form", "done")
        await set_controller(
            ctx, "https://agency.example.test/privacy", "privacy@agency.example.test", "We may contact you about it."
        )
        with _raises(Invalid, "invalid_privacy_notice_url"):
            await set_controller(ctx, "http://plain.example.test", None, None)
        with _raises(Conflict, "version_changed"):
            await acknowledge(ctx, "old-version")
        await acknowledge(ctx, ACK_VERSION)
    async with tenant_tx(app_pool, d["agent"], d["tenant"], action=Action.SESSION_VIEW) as ctx:
        after = await overview(ctx, duplicate_count=0)
    steps = {s["key"]: s for s in after["steps"]}
    assert steps["kickoff_call"]["later"] and steps["embed_form"]["done"] and steps["privacy_settings"]["done"]
    assert after["required_done"] == 2 and after["acknowledgement"]["accepted"]
    events = await _sql(
        app_pool, d["tenant"], "SELECT action, reason FROM crm.audit_event WHERE action = 'acknowledge'"
    )
    assert [(e["action"], e["reason"]) for e in events] == [("acknowledge", "shared_responsibility")]
    with pytest.raises(Exception) as err:
        async with tenant_tx(app_pool, d["agent"], d["tenant"], action=Action.ONBOARDING_MANAGE):
            pass  # pragma: no cover
    assert getattr(err.value, "code", None) == "role_forbidden"


async def test_controller_settings_reach_the_public_form(app_pool: asyncpg.Pool) -> None:
    """Errata S247 CRM-31: the agency's privacy link (when the form has none), consent wording and data-request
    contact show on its hosted inquiry form; clearing them brings back the defaults."""
    tenant_id, [(owner, _)] = await _agency(app_pool)
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        form = await intake_forms.create_form(ctx, label="Site")
    shown = await intake.read_form(app_pool, form.public_key)
    assert (shown["privacy_notice_url"], shown["consent_text"], shown["data_request_contact"]) == (None, None, None)
    async with tenant_tx(app_pool, owner, tenant_id, action=Action.DATA_SETTINGS) as ctx:
        await set_controller(ctx, "https://agency.example.test/privacy", "privacy@agency.example.test", "Own words.")
    shown = await intake.read_form(app_pool, form.public_key)
    assert shown["privacy_notice_url"] == "https://agency.example.test/privacy"
    assert shown["consent_text"] == "Own words." and shown["data_request_contact"] == "privacy@agency.example.test"
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        await intake_forms.update_form(
            ctx, uuid.UUID(form.intake_form_id), {"privacy_notice_url": "https://agency.example.test/form"}
        )
    assert (await intake.read_form(app_pool, form.public_key))[
        "privacy_notice_url"
    ] == "https://agency.example.test/form"
    async with tenant_tx(app_pool, owner, tenant_id, action=Action.DATA_SETTINGS) as ctx:
        await set_controller(ctx, None, None, None)
    shown = await intake.read_form(app_pool, form.public_key)
    assert shown["consent_text"] is None and shown["data_request_contact"] is None
