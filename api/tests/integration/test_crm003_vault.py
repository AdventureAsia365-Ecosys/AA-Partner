"""crm-003 vault on PostgreSQL 15 with the RDS role shape, through tenant_tx() as crm_app and the real DEK store.

Card CRM-06: Done when "a DB dump of the vault shows ciphertext only"; R "decrypting with another tenant's context
fails and is audited denied"; the rewrap contract on crm.tenant_key; the table rules (guard trigger, append-only
audit, reader roles refused, integration credential states).
"""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest

from app.auth.route_guard import Caller
from app.tenancy import tenant_tx
from app.vault import DecryptionDenied, KeyProvider, Vault
from app.vault.errors import KeyDestroyed
from app.vault.wrapping import LocalKeyWrapper
from tests.integration.conftest import READERS

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ISS = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_TESTPARTN"
VAULT_TABLES = ("tenant_key", "integration_credential", "traveler", "audit_event")


def caller(sub: str) -> Caller:
    return Caller("agency", "partner", "partner-web", ISS, sub, ())


async def _agency(pool: asyncpg.Pool) -> tuple[uuid.UUID, Caller]:
    tenant_id, sub = uuid.uuid4(), f"sub-{uuid.uuid4()}"
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        await conn.execute("SELECT set_config('app.actor', 'seed:test', true)")  # crm-008: crm_app names its actor
        await conn.execute("INSERT INTO crm.tenant (tenant_id) VALUES ($1)", tenant_id)
        await conn.execute(
            "INSERT INTO crm.member (cognito_iss, cognito_sub, role, status) VALUES ($1, $2, 'owner', 'invited')",
            ISS,
            sub,
        )
        await conn.execute("UPDATE crm.member SET status = 'active' WHERE cognito_sub = $1", sub)
    return tenant_id, caller(sub)


def _keys(wrapper: LocalKeyWrapper | None = None) -> KeyProvider:
    wrapper = wrapper or LocalKeyWrapper({"k1": os.urandom(32)}, "k1")
    return KeyProvider({"local": wrapper}, "local")


TRAVELER = {
    "display_name_enc": "Nguyễn Thị Hoa",
    "given_name_enc": "Hoa",
    "family_name_enc": "Nguyễn",
    "email_enc": "hoa.nguyen@example.test",
    "phone_enc": "+84912345678",
    "dob_enc": "1990-04-03",
    "passport_no_enc": "C1234567",
    "emergency_contact_enc": "Trần Văn Nam +84987654321",
}


async def _write_traveler(keys: KeyProvider, pool: asyncpg.Pool, tenant: uuid.UUID, who: Caller) -> uuid.UUID:
    traveler_id = uuid.uuid4()
    async with tenant_tx(pool, who, tenant) as ctx:
        vault = Vault(keys, ctx)
        enc = {c: await vault.encrypt("traveler", c, traveler_id, v) for c, v in TRAVELER.items()}
        await ctx.conn.execute(
            "INSERT INTO crm.traveler (traveler_id, display_name_enc, given_name_enc, family_name_enc, email_enc, "
            "phone_enc, dob_enc, passport_no_enc, emergency_contact_enc, email_bidx, phone_bidx, name_bidx, "
            "name_token_bidx) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)",
            traveler_id,
            *enc.values(),
            await vault.email_index("traveler", "email", TRAVELER["email_enc"]),
            await vault.phone_index("traveler", "phone", TRAVELER["phone_enc"]),
            await vault.name_index("traveler", "name", TRAVELER["display_name_enc"]),
            await vault.name_token_indexes("traveler", "name_token", TRAVELER["display_name_enc"]),
        )
    return traveler_id


async def _read_traveler(keys: KeyProvider, pool: asyncpg.Pool, tenant: uuid.UUID, who: Caller, tid: uuid.UUID) -> dict:
    async with tenant_tx(pool, who, tenant) as ctx:
        row = await ctx.conn.fetchrow("SELECT * FROM crm.traveler WHERE traveler_id = $1", tid)
        vault = Vault(keys, ctx)
        return {c: await vault.decrypt("traveler", c, tid, row[c], tenant_id=row["tenant_id"]) for c in TRAVELER}


async def test_a_db_dump_of_the_vault_shows_ciphertext_only(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    keys = _keys()
    tenant, who = await _agency(app_pool)
    tid = await _write_traveler(keys, app_pool, tenant, who)
    async with admin.transaction():
        await admin.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
        dump = "".join(
            [
                str(await admin.fetchval(f"SELECT json_agg(t)::text FROM crm.{table} t"))  # noqa: S608
                for table in ("traveler", "tenant_key")
            ]
        )
        raw = b"".join(
            bytes(v)
            for r in await admin.fetch("SELECT * FROM crm.traveler")
            for v in r.values()
            if isinstance(v, bytes | bytearray)
        )
    assert "\\x" in dump  # bytea values are there, hex-encoded
    for plain in [*TRAVELER.values(), "Nguyen", "hoa.nguyen"]:
        assert plain not in dump and plain.encode().hex() not in dump and plain.encode() not in raw
    # The same values read back through the vault, with a fresh cache.
    keys.clear_cache()
    assert await _read_traveler(keys, app_pool, tenant, who, tid) == TRAVELER


async def test_another_tenants_value_is_denied_and_the_denial_is_committed(app_pool: asyncpg.Pool) -> None:
    """Card R: the decrypt fails, the request's own writes roll back, the ``denied`` audit row stays."""
    keys = _keys()
    (a, who_a), (b, who_b) = await _agency(app_pool), await _agency(app_pool)
    tid_b = await _write_traveler(keys, app_pool, b, who_b)
    await _write_traveler(keys, app_pool, a, who_a)  # A has its own vault key
    async with tenant_tx(app_pool, who_b, b) as ctx:
        blob_b = await ctx.conn.fetchval("SELECT email_enc FROM crm.traveler WHERE traveler_id = $1", tid_b)
    stray = uuid.uuid4()
    with pytest.raises(DecryptionDenied) as info:
        async with tenant_tx(app_pool, who_a, a) as ctx:
            await ctx.conn.execute(
                "INSERT INTO crm.traveler (traveler_id, display_name_enc) VALUES ($1, $2)",
                stray,
                await Vault(keys, ctx).encrypt("traveler", "display_name_enc", stray, "Should roll back"),
            )
            await Vault(keys, ctx).decrypt("traveler", "email_enc", tid_b, blob_b)
    assert info.value.reason == "integrity_check_failed"
    async with tenant_tx(app_pool, who_a, a) as ctx:
        assert await ctx.conn.fetchval("SELECT count(*) FROM crm.traveler WHERE traveler_id = $1", stray) == 0
        events = await ctx.conn.fetch("SELECT * FROM crm.audit_event WHERE action = 'decrypt'")
        member_id = ctx.member_id
    assert len(events) == 1
    e = events[0]
    assert (e["tenant_id"], e["outcome"], e["object_kind"], e["object_id"], e["fields"], e["reason"]) == (
        a,
        "denied",
        "traveler",
        str(tid_b),
        ["email_enc"],
        "integrity_check_failed",
    )
    assert (e["actor_kind"], e["actor_ref"]) == ("user", f"member:{member_id}")
    # Tenant B sees no event of A (its own are the AA-940 key_create events of its first DEKs).
    async with tenant_tx(app_pool, who_b, b) as ctx:
        assert await ctx.conn.fetchval("SELECT count(*) FROM crm.audit_event WHERE action NOT LIKE 'key_%'") == 0
        assert await ctx.conn.fetchval("SELECT count(*) FROM crm.audit_event WHERE tenant_id <> $1", b) == 0


async def test_rewrap_on_the_table_store_keeps_every_value(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    old = LocalKeyWrapper({"kek-old": os.urandom(32)}, "kek-old")
    keys = _keys(old)
    tenant, who = await _agency(app_pool)
    tid = await _write_traveler(keys, app_pool, tenant, who)
    async with tenant_tx(app_pool, who, tenant) as ctx:
        await keys.rotate(ctx, "vault")
        await Vault(keys, ctx).encrypt_secret(uuid.uuid4(), "rk_test_fake")
    async with tenant_tx(app_pool, who, tenant) as ctx:
        before = await ctx.conn.fetch("SELECT traveler_id, email_enc, email_bidx FROM crm.traveler")
    target = old.with_kek("kek-new", os.urandom(32))
    rewrapper = KeyProvider({"local": target}, "local")
    async with tenant_tx(app_pool, who, tenant) as ctx:
        assert await rewrapper.rewrap(ctx, target) == 3  # vault v1, vault v2, integration
    async with admin.transaction():
        await admin.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
        rows = await admin.fetch("SELECT kek_id, rewrapped_at FROM crm.tenant_key ORDER BY purpose, key_version")
    assert [r["kek_id"] for r in rows] == ["kek-new"] * 3 and all(r["rewrapped_at"] for r in rows)
    fresh = KeyProvider({"local": target.without("kek-old")}, "local")
    assert await _read_traveler(fresh, app_pool, tenant, who, tid) == TRAVELER
    async with tenant_tx(app_pool, who, tenant) as ctx:
        after = await ctx.conn.fetch("SELECT traveler_id, email_enc, email_bidx FROM crm.traveler")
        assert (
            await Vault(fresh, ctx).email_index("traveler", "email", TRAVELER["email_enc"]) == before[0]["email_bidx"]
        )
    assert after == before


async def test_a_key_created_in_a_rolled_back_transaction_is_never_used(app_pool: asyncpg.Pool) -> None:
    keys = _keys()
    tenant, who = await _agency(app_pool)
    with pytest.raises(RuntimeError):
        async with tenant_tx(app_pool, who, tenant) as ctx:
            await Vault(keys, ctx).encrypt("traveler", "display_name_enc", uuid.uuid4(), "x")
            raise RuntimeError("request failed")
    assert keys._cache == {} and keys._current == {}
    async with tenant_tx(app_pool, who, tenant) as ctx:
        assert await ctx.conn.fetchval("SELECT count(*) FROM crm.tenant_key") == 0
    await _write_traveler(keys, app_pool, tenant, who)
    async with tenant_tx(app_pool, who, tenant) as ctx:
        assert await ctx.conn.fetchval("SELECT array_agg(key_version) FROM crm.tenant_key") == [1]


async def test_tenant_key_guard_and_constraints(app_pool: asyncpg.Pool) -> None:
    keys = _keys()
    tenant, who = await _agency(app_pool)
    await _write_traveler(keys, app_pool, tenant, who)

    async def refused(sql: str, error: type[Exception], *args: object) -> None:
        with pytest.raises(error):
            async with tenant_tx(app_pool, who, tenant) as ctx:
                await ctx.conn.execute(sql, *args)

    await refused("UPDATE crm.tenant_key SET key_version = 9", asyncpg.CheckViolationError)
    await refused("UPDATE crm.tenant_key SET purpose = 'docs'", asyncpg.CheckViolationError)
    await refused(
        "UPDATE crm.tenant_key SET wrapped_dek = decode(repeat('ab', 40), 'hex')", asyncpg.CheckViolationError
    )
    await refused("DELETE FROM crm.tenant_key", asyncpg.InsufficientPrivilegeError)
    await refused("TRUNCATE crm.tenant_key", asyncpg.InsufficientPrivilegeError)
    await refused(
        "INSERT INTO crm.tenant_key (purpose, key_version, kek_provider, kek_id, wrapped_dek, encryption_context) "
        "VALUES ('vault', 2, 'local', 'k', decode(repeat('ab', 40), 'hex'), '{}'::jsonb)",
        asyncpg.CheckViolationError,
    )
    await refused(  # a second current key for the purpose
        "INSERT INTO crm.tenant_key (purpose, key_version, kek_provider, kek_id, wrapped_dek, encryption_context) "
        "VALUES ('vault', 2, 'local', 'k', decode(repeat('ab', 40), 'hex'), "
        "jsonb_build_object('tenant_id', current_setting('app.tenant_id'), 'purpose', 'vault'))",
        asyncpg.UniqueViolationError,
    )
    await refused("UPDATE crm.tenant_key SET destroyed_at = now()", asyncpg.CheckViolationError)
    # The wrapping triple moves only together with rewrapped_at.
    await refused("UPDATE crm.tenant_key SET kek_id = 'another-kek'", asyncpg.CheckViolationError)
    await refused("UPDATE crm.tenant_key SET kek_provider = 'kms'", asyncpg.CheckViolationError)
    # Destroy (crypto-shred), then the rows may go; a retire cannot be undone.
    async with tenant_tx(app_pool, who, tenant) as ctx:
        assert await keys.destroy(ctx) == 1
    await refused("UPDATE crm.tenant_key SET retired_at = NULL", asyncpg.CheckViolationError)
    await refused(  # a destroyed key never gets a wrapped DEK back
        "UPDATE crm.tenant_key SET wrapped_dek = decode(repeat('ab', 40), 'hex'), rewrapped_at = clock_timestamp()",
        asyncpg.CheckViolationError,
    )
    with pytest.raises(KeyDestroyed):  # a new value after the shred: KeyDestroyed, not a key race
        async with tenant_tx(app_pool, who, tenant) as ctx:
            await Vault(_keys(), ctx).encrypt("traveler", "email_enc", uuid.uuid4(), "z@example.test")
    with pytest.raises(KeyDestroyed):
        async with tenant_tx(app_pool, who, tenant) as ctx:
            row = await ctx.conn.fetchrow("SELECT traveler_id, email_enc FROM crm.traveler")
            await Vault(_keys(), ctx).decrypt("traveler", "email_enc", row["traveler_id"], row["email_enc"])
    async with tenant_tx(app_pool, who, tenant) as ctx:
        assert await ctx.conn.execute("DELETE FROM crm.tenant_key") == "DELETE 1"


async def test_audit_event_is_append_only(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    tenant, who = await _agency(app_pool)
    async with tenant_tx(app_pool, who, tenant) as ctx:
        ctx.audit("reveal", "allowed", "traveler", "t-1", fields=("email_enc",), reason="client_call")
    for sql in ("UPDATE crm.audit_event SET reason = 'x'", "DELETE FROM crm.audit_event", "TRUNCATE crm.audit_event"):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with tenant_tx(app_pool, who, tenant) as ctx:
                await ctx.conn.execute(sql)
    # Even the owner: the trigger refuses.
    await admin.execute("SET ROLE crm_owner")
    try:
        async with admin.transaction():
            await admin.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
            with pytest.raises(asyncpg.InsufficientPrivilegeError, match="append-only"):
                await admin.execute("DELETE FROM crm.audit_event")
    finally:
        await admin.execute("RESET ROLE")
    with pytest.raises(asyncpg.CheckViolationError):
        async with tenant_tx(app_pool, who, tenant) as ctx:
            await ctx.conn.execute(
                "INSERT INTO crm.audit_event (actor_kind, actor_ref, action, outcome, object_kind, reason) "
                "VALUES ('user', 'member:x', 'reveal', 'allowed', 'traveler', 'The client called me')"
            )


async def test_reader_roles_reach_no_vault_table(app_pool: asyncpg.Pool) -> None:
    tenant, who = await _agency(app_pool)
    for reader in READERS:
        for table in VAULT_TABLES:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                async with tenant_tx(app_pool, who, tenant, reader_role=reader) as ctx:
                    await ctx.conn.fetchval(f"SELECT count(*) FROM crm.{table}")  # noqa: S608


async def test_a_denial_under_a_reader_role_is_still_recorded(app_pool: asyncpg.Pool) -> None:
    tenant, who = await _agency(app_pool)
    with pytest.raises(DecryptionDenied):
        async with tenant_tx(app_pool, who, tenant, reader_role="crm_support_reader") as ctx:
            await Vault(_keys(), ctx).decrypt(
                "traveler", "email_enc", "t-9", b"\x00\x01" + b"\x00" * 40, tenant_id=uuid.uuid4()
            )
    async with tenant_tx(app_pool, who, tenant) as ctx:
        assert await ctx.conn.fetchval("SELECT reason FROM crm.audit_event") == "tenant_mismatch"


async def test_integration_credential_states(app_pool: asyncpg.Pool) -> None:
    keys = _keys()
    tenant, who = await _agency(app_pool)
    cred = uuid.uuid4()
    async with tenant_tx(app_pool, who, tenant) as ctx:
        await ctx.conn.execute(
            "INSERT INTO crm.integration_credential (integration_credential_id, provider, account_ref, secret_enc) "
            "VALUES ($1, 'stripe_key', 'acct_test', $2)",
            cred,
            await Vault(keys, ctx).encrypt_secret(cred, "rk_test_fake"),
        )
        await ctx.conn.execute(
            "UPDATE crm.integration_credential SET status = 'needs_attention', failure_class = 'permission_missing'"
        )
        await ctx.conn.execute("UPDATE crm.integration_credential SET status = 'connected', failure_class = NULL")
        blob = await ctx.conn.fetchval("SELECT secret_enc FROM crm.integration_credential")
        assert await Vault(keys, ctx).decrypt_secret(cred, blob) == "rk_test_fake"
    for sql in (
        "UPDATE crm.integration_credential SET status = 'not_set_up'",
        "UPDATE crm.integration_credential SET failure_class = 'unreachable'",
        "UPDATE crm.integration_credential SET provider = 'paypal'",
    ):
        with pytest.raises(asyncpg.CheckViolationError):
            async with tenant_tx(app_pool, who, tenant) as ctx:
                await ctx.conn.execute(sql)
    with pytest.raises(asyncpg.UniqueViolationError):
        async with tenant_tx(app_pool, who, tenant) as ctx:
            await ctx.conn.execute(
                "INSERT INTO crm.integration_credential (provider, secret_enc) VALUES ('stripe_key', $1)", blob
            )


async def test_traveler_checks(app_pool: asyncpg.Pool) -> None:
    tenant, who = await _agency(app_pool)
    blob = b"\x00\x01" + os.urandom(40)
    for sql, args in (
        ("INSERT INTO crm.traveler (display_name_enc) VALUES ($1)", (b"plain",)),
        ("INSERT INTO crm.traveler (display_name_enc, email_enc) VALUES ($1, $1)", (blob,)),  # email without index
        ("INSERT INTO crm.traveler (display_name_enc, email_bidx) VALUES ($1, $2)", (blob, os.urandom(16))),
        ("INSERT INTO crm.traveler (display_name_enc, email_enc, email_bidx) VALUES ($1, $1, $2)", (blob, b"x" * 15)),
        ("INSERT INTO crm.traveler (display_name_enc, lead_source) VALUES ($1, 'Facebook Ads!')", (blob,)),
    ):
        with pytest.raises(asyncpg.CheckViolationError):
            async with tenant_tx(app_pool, who, tenant) as ctx:
                await ctx.conn.execute(sql, *args)
