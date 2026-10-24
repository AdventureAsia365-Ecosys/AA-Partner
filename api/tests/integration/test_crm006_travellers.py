"""AA-937 (card CRM-09) end to end on PostgreSQL 15 with the RDS role shape, as crm_app, with real field encryption.

Card CRM-09:
- **E**: a companion without an e-mail is valid; an unrelated edit does not recompute age bands; a board page load
  writes exactly one ``list_view`` event.
- **R** (with tests/integration/test_crm006_schema.py and tests/unit/test_reveal_boundary.py): the readers never reach
  ``crm.trip_traveler``; F2 reads need no decrypt.
- ``reveal(field, reason)`` -> one audit event with the reason; the directory writes one ``list_view`` per page with
  the ids shown and no contact data; documents are sealed with the tenant ``docs`` DEK before the store.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import uuid
from typing import Any

import asyncpg
import pytest

from app.documents import service as documents
from app.documents.store import MemoryDocumentStore
from app.errors import Conflict, NotFound, Unavailable
from app.intake import public as intake
from app.pipeline import BoardFilter, board, update_trip
from app.tenancy import system_tx, tenant_tx
from app.travellers import service
from app.travellers.reveal import reveal
from app.travellers.self_reveal import TravellerSession, traveller_self_reveal
from app.vault import DecryptionDenied, KeyProvider, Vault
from tests.integration.test_crm005_intake import SETTINGS, _agency, _form, _keys, _owner_fetch, _process_all, answers

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

JPEG = b"\xff\xd8\xff\xe0" + b"synthetic passport scan of Anna" + b"\x00" * 64


async def _inquiry(pool: asyncpg.Pool, keys: KeyProvider) -> tuple[uuid.UUID, Any, uuid.UUID, uuid.UUID]:
    """A synthetic agency with one processed inquiry: (tenant, owner caller, trip id, lead traveller id)."""
    tenant_id, [(owner, owner_id)] = await _agency(pool)
    form = await _form(pool, tenant_id, owner, default_owner_member_id=owner_id)
    assert (await intake.submit(pool, keys, SETTINGS, form.public_key, answers(), origin=None)).stored
    [result] = [r for r in await _process_all(pool, keys, tenant_id) if r["status"] == "created"]
    return tenant_id, owner, uuid.UUID(result["trip_id"]), uuid.UUID(result["traveler_id"])


async def _events(admin: asyncpg.Connection, tenant_id: uuid.UUID, action: str) -> list[asyncpg.Record]:
    return await _owner_fetch(
        admin,
        tenant_id,
        "SELECT action, outcome, object_kind, object_id, fields, reason, object_ids FROM crm.audit_event "
        "WHERE action = $1 ORDER BY at, event_id",
        action,
    )


async def test_e_a_board_page_load_writes_exactly_one_list_view(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    keys = _keys()
    tenant_id, owner, trip_id, lead = await _inquiry(app_pool, keys)
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        await board(ctx, Vault(keys, ctx), BoardFilter())
    [event] = await _events(admin, tenant_id, "list_view")
    assert event["object_kind"] == "pipeline" and event["fields"] == ["display_name"]
    assert event["object_ids"] == [str(lead)]


async def test_directory_search_and_one_list_view_per_page(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    keys = _keys()
    tenant_id, owner, _, lead = await _inquiry(app_pool, keys)
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        listed = await service.directory(ctx, Vault(keys, ctx))
    names = sorted(i["display_name"] for i in listed["items"])
    assert names == ["Nguyễn Thị Hoa", "Trần Văn Nam"]
    assert all("email" not in i and "phone" not in i for i in listed["items"])
    for q, expected in (
        ("hoa.nguyen@EXAMPLE.test", ["Nguyễn Thị Hoa"]),
        ("+84912345678", ["Nguyễn Thị Hoa"]),
        ("nguyen hoa", ["Nguyễn Thị Hoa"]),
        ("Tran", ["Trần Văn Nam"]),
        ("nobody@example.test", []),
    ):
        async with tenant_tx(app_pool, owner, tenant_id) as ctx:
            got = await service.directory(ctx, Vault(keys, ctx), q=q)
        assert [i["display_name"] for i in got["items"]] == expected, q
    events = await _events(admin, tenant_id, "list_view")
    assert [e["reason"] for e in events] == [
        "client_list",
        "client_search",
        "client_search",
        "client_search",
        "client_search",
    ]
    assert sorted(events[0]["object_ids"]) == sorted(i["traveler_id"] for i in listed["items"])
    assert str(lead) in events[0]["object_ids"]


async def test_reveal_with_reason_profile_view_and_another_agency(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    keys = _keys()
    tenant_id, owner, trip_id, lead = await _inquiry(app_pool, keys)
    other_tenant, [(other, _)] = await _agency(app_pool)
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        got = await reveal(ctx, Vault(keys, ctx), lead, "phone", "contact_client")
        prof = await service.profile(ctx, Vault(keys, ctx), lead)
    assert got["value"] == "+84912345678"
    assert prof["traveler"]["filled"]["phone"] is True and "+8491" not in repr(prof)
    assert prof["consent"]["contact"]["value"] is True and prof["trips"][0]["role"] == "lead"
    [event] = await _events(admin, tenant_id, "reveal")
    assert (event["fields"], event["reason"], event["object_id"]) == (["phone"], "contact_client", str(lead))
    assert len(await _events(admin, tenant_id, "view")) == 1
    with pytest.raises(NotFound):  # RLS: the other agency cannot see the traveller at all
        async with tenant_tx(app_pool, other, other_tenant) as ctx:
            await reveal(ctx, Vault(keys, ctx), lead, "email", "other")


async def test_e_companion_without_email_and_age_bands_only_on_dob_or_start_change(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    keys = _keys()
    tenant_id, owner, trip_id, lead = await _inquiry(app_pool, keys)
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        added = await service.add_to_trip(ctx, Vault(keys, ctx), trip_id, new={"display_name": "Lily Martin"})
    lily = uuid.UUID(added["traveler_id"])
    assert added["age_band_at_departure"] is None  # no DOB, no start date yet
    async with tenant_tx(app_pool, owner, tenant_id, reader_role="crm_ai_reader") as ctx:
        party = await ctx.conn.fetch(
            "SELECT role, age_band_at_departure FROM crm.v_ai_trip_party WHERE trip_id = $1", trip_id
        )
    assert sorted(r["role"] for r in party) == ["companion", "companion", "lead"]

    async def bands() -> list[tuple[Any, ...]]:
        rows = await _owner_fetch(
            admin, tenant_id, "SELECT trip_party_id, age_band_at_departure, updated_at FROM crm.trip_party ORDER BY 1"
        )
        return [tuple(r) for r in rows]

    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        await update_trip(ctx, trip_id, {"start_date": dt.date(2027, 3, 12)}, Vault(keys, ctx))
        await service.update_traveler(ctx, Vault(keys, ctx), lily, {"dob": "2018-06-01"})
        await service.update_traveler(ctx, Vault(keys, ctx), lead, {"dob": "1990-03-12"})
    after_dob = await bands()
    assert sorted(b[1] for b in after_dob if b[1]) == ["2_11", "35_44"]
    recomputes = len(await _events(admin, tenant_id, "age_band_recompute"))
    assert recomputes == 3  # the start date, then each DOB: one event per run
    # Unrelated edits: names, nationality, the title, the same DOB again, the same start date. Nothing moves.
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        await service.update_traveler(ctx, Vault(keys, ctx), lily, {"nationality": "AU", "given_name": "Lily"})
        await service.update_traveler(ctx, Vault(keys, ctx), lead, {"dob": "1990-03-12"})
        await update_trip(
            ctx, trip_id, {"title": "Northern Vietnam", "start_date": dt.date(2027, 3, 12)}, Vault(keys, ctx)
        )
    assert await bands() == after_dob
    assert len(await _events(admin, tenant_id, "age_band_recompute")) == recomputes
    # Moving the trip a year later moves the band of whoever crosses a boundary.
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        await update_trip(ctx, trip_id, {"start_date": dt.date(2030, 3, 12)}, Vault(keys, ctx))
    assert sorted(b[1] for b in await bands() if b[1]) == ["2_11", "35_44"]
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        await update_trip(ctx, trip_id, {"start_date": dt.date(2030, 6, 2)}, Vault(keys, ctx))
    assert sorted(b[1] for b in await bands() if b[1]) == ["12_17", "35_44"]
    with pytest.raises(Conflict):
        async with tenant_tx(app_pool, owner, tenant_id) as ctx:
            await service.remove_from_trip(ctx, Vault(keys, ctx), trip_id, lead)
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        await service.remove_from_trip(ctx, Vault(keys, ctx), trip_id, lily)
        await service.add_to_trip(ctx, Vault(keys, ctx), trip_id, traveler_id=lily)


async def test_create_dedupes_relations_health_and_f2_without_decrypt(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    keys = _keys()
    tenant_id, owner, trip_id, lead = await _inquiry(app_pool, keys)
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        tom = uuid.UUID(
            (await service.create_traveler(ctx, Vault(keys, ctx), {"display_name": "Tom Martin"}))["traveler_id"]
        )
    with pytest.raises(Conflict) as err:
        async with tenant_tx(app_pool, owner, tenant_id) as ctx:
            await service.create_traveler(
                ctx, Vault(keys, ctx), {"display_name": "Hoa", "email": "HOA.nguyen@example.test"}
            )
    assert err.value.extra["traveler_id"] == str(lead)
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        await service.add_relation(ctx, lead, tom, "referrer")
        await service.set_health(
            ctx, Vault(keys, ctx), lead, dietary=["vegetarian"], mobility=[], medical=["asthma"], consent_recorded=True
        )
        await service.update_profile(ctx, Vault(keys, ctx), lead, {"fitness_band": "4_6h", "interests": ["food"]})
        prof = await service.profile(ctx, Vault(keys, ctx), tom)
        health = await reveal(ctx, Vault(keys, ctx), lead, "health", "fill_forms")
    assert [(r["kind"], r["traveler_id"]) for r in prof["relations"]] == [("referred", str(lead))]
    assert health["value"] == {"dietary": ["vegetarian"], "medical": ["asthma"], "mobility": []}
    referrer = await _owner_fetch(
        admin, tenant_id, "SELECT referrer_traveler_id FROM crm.traveler WHERE traveler_id = $1", lead
    )
    assert referrer[0]["referrer_traveler_id"] == tom
    # F2 under crm_ai_reader: bands and the mirrored dietary flag, no medical code, and no key access at all.
    async with tenant_tx(app_pool, owner, tenant_id, reader_role="crm_ai_reader") as ctx:
        rows = await ctx.conn.fetch("SELECT * FROM crm.v_ai_traveler_profile WHERE fitness_band IS NOT NULL")
    assert [(r["fitness_band"], list(r["dietary_flags"]), list(r["mobility_flags"])) for r in rows] == [
        ("4_6h", ["vegetarian"], [])
    ]
    assert "asthma" not in repr([dict(r) for r in rows])
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with tenant_tx(app_pool, owner, tenant_id, reader_role="crm_ai_reader") as ctx:
            await ctx.conn.fetch("SELECT * FROM crm.tenant_key")
    # Health is ciphertext in F1.
    dump = repr([dict(r) for r in await _owner_fetch(admin, tenant_id, "SELECT * FROM crm.traveler_health")])
    assert "asthma" not in dump and "vegetarian" not in dump
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        [rel] = (await service.profile(ctx, Vault(keys, ctx), lead))["relations"]
        await service.remove_relation(ctx, lead, uuid.UUID(rel["relation_id"]))
    referrer = await _owner_fetch(
        admin, tenant_id, "SELECT referrer_traveler_id FROM crm.traveler WHERE traveler_id = $1", lead
    )
    assert referrer[0]["referrer_traveler_id"] is None


async def test_health_reveal_follows_the_latest_consent_and_profile_names_what_it_shows(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    """Design §4.3 (health with consent) and §4.4 (the ids of every identity shown), plus the reveal meter."""
    from app.observability import reveal_meter

    keys = _keys()
    tenant_id, owner, trip_id, lead = await _inquiry(app_pool, keys)
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        tom = uuid.UUID(
            (await service.create_traveler(ctx, Vault(keys, ctx), {"display_name": "Tom Martin"}))["traveler_id"]
        )
        await service.add_relation(ctx, lead, tom, "spouse")
        await service.set_health(
            ctx, Vault(keys, ctx), lead, dietary=["vegan"], mobility=[], medical=[], consent_recorded=True
        )
        member_id = ctx.member_id
    reveal_meter.METER.reset()
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        assert (await reveal(ctx, Vault(keys, ctx), lead, "health", "fill_forms"))["value"]["dietary"] == ["vegan"]
        await service.profile(ctx, Vault(keys, ctx), lead)
    assert reveal_meter.METER.count(tenant_id, member_id) == 1
    [view] = await _events(admin, tenant_id, "view")
    assert view["object_id"] == str(lead) and view["object_ids"] == [str(tom)]
    # The traveller withdraws the consent (append-only: a later row with value false). The codes stay sealed.
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        await ctx.conn.execute(
            "INSERT INTO crm.consent (traveler_id, purpose, value, source) VALUES ($1, 'health_data', false, "
            "'agent_recorded')",
            lead,
        )
    with pytest.raises(Conflict) as err:
        async with tenant_tx(app_pool, owner, tenant_id) as ctx:
            await reveal(ctx, Vault(keys, ctx), lead, "health", "fill_forms")
    assert err.value.code == "health_consent_withdrawn"
    reveals = await _events(admin, tenant_id, "reveal")
    assert [(r["outcome"], r["reason"]) for r in reveals] == [
        ("allowed", "fill_forms"),
        ("denied", "consent_withdrawn"),
    ]
    assert reveal_meter.METER.count(tenant_id, member_id) == 1  # a refused reveal released nothing
    reveal_meter.METER.reset()


async def test_documents_are_sealed_with_the_docs_dek(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    keys = _keys()
    store = MemoryDocumentStore()
    tenant_id, owner, trip_id, lead = await _inquiry(app_pool, keys)
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        doc = await documents.upload(
            ctx,
            Vault(keys, ctx),
            store,
            traveler_id=lead,
            trip_id=trip_id,
            kind="passport_scan",
            filename="Hoa passport.jpg",
            content_type="image/jpeg",
            content=JPEG,
        )
    [(key, sealed)] = store.objects.items()
    assert key == f"tenants/{tenant_id}/documents/{doc['document_id']}"
    assert b"Anna" not in sealed and len(sealed) == len(JPEG) + 30 and doc["write_only"] is True
    purposes = await _owner_fetch(admin, tenant_id, "SELECT purpose FROM crm.tenant_key ORDER BY purpose")
    assert {r["purpose"] for r in purposes} == {"docs", "vault"}
    rows = await _owner_fetch(admin, tenant_id, "SELECT * FROM crm.document")
    assert "passport.jpg" not in repr([dict(r) for r in rows])
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        got = await documents.download(ctx, Vault(keys, ctx), store, uuid.UUID(doc["document_id"]), "verify_identity")
        listed = await documents.trip_documents(ctx, Vault(keys, ctx), trip_id)
    assert base64.b64decode(got["content_b64"]) == JPEG and listed["documents"][0]["filename"] == "Hoa passport.jpg"
    # The object moved under another document's key opens nowhere: the AAD binds the document id.
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        twin = await documents.upload(
            ctx,
            Vault(keys, ctx),
            store,
            traveler_id=lead,
            trip_id=None,
            kind="ticket",
            filename="t.jpg",
            content_type="image/jpeg",
            content=JPEG,
        )
    twin_id = uuid.UUID(twin["document_id"])
    twin_key = f"tenants/{tenant_id}/documents/{twin_id}"
    # 1. A changed object fails the digest check before anything is decrypted (denial on record).
    store.objects[twin_key] = sealed
    with pytest.raises(Unavailable):
        async with tenant_tx(app_pool, owner, tenant_id) as ctx:
            await documents.download(ctx, Vault(keys, ctx), store, twin_id, "other")
    # 2. Even with a matching digest, another document's object does not open: the AAD binds the document id.
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        await ctx.conn.execute(
            "UPDATE crm.document SET sha256 = $2 WHERE document_id = $1", twin_id, hashlib.sha256(sealed).digest()
        )
    with pytest.raises(DecryptionDenied):
        async with tenant_tx(app_pool, owner, tenant_id) as ctx:
            await documents.download(ctx, Vault(keys, ctx), store, twin_id, "other")
    denied = await _owner_fetch(
        admin, tenant_id, "SELECT reason FROM crm.audit_event WHERE outcome = 'denied' ORDER BY at, event_id"
    )
    assert [r["reason"] for r in denied] == ["digest_mismatch", "integrity_check_failed"]


async def test_traveller_self_reveal_sees_only_their_own_record(app_pool: asyncpg.Pool) -> None:
    keys = _keys()
    tenant_id, owner, trip_id, lead = await _inquiry(app_pool, keys)
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        await service.update_traveler(ctx, Vault(keys, ctx), lead, {"passport_no": "C1234567"})
    session = TravellerSession(tenant_id=tenant_id, trip_id=trip_id, traveler_id=lead, access="lead")
    async with system_tx(app_pool, tenant_id, actor="job:crm_portal_test") as ctx:
        own = await traveller_self_reveal(ctx, Vault(keys, ctx), session)
    assert own["display_name"] == "Nguyễn Thị Hoa" and own["passport_no_masked"] == "•••• 567"
    assert "passport_no" not in own
    stranger = TravellerSession(tenant_id=tenant_id, trip_id=uuid.uuid4(), traveler_id=lead, access="lead")
    with pytest.raises(NotFound):
        async with system_tx(app_pool, tenant_id, actor="job:crm_portal_test") as ctx:
            await traveller_self_reveal(ctx, Vault(keys, ctx), stranger)
