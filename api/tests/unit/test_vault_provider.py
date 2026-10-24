"""KeyProvider: provisioning, the 5-minute cache and its expiry (card E), the transaction memo, rotation, the rewrap
contract test (card checklist, BYOK path) and destroy. The store is the in-memory stand-in; SQL: integration tests."""

from __future__ import annotations

import os
from collections.abc import Callable

import pytest

from app.settings import Settings
from app.vault import KeyProvider, Vault, build_key_provider
from app.vault.errors import KeyDestroyed, KeyProviderNotConfigured, KeyUnavailable
from app.vault.provider import require_keys
from app.vault.wrapping import KmsKeyWrapper, LocalKeyWrapper
from tests.unit.vault_fakes import TENANT_A, TENANT_B, Clock, FakeKms, MemoryDekStore, make_ctx


def _local(store: MemoryDekStore, clock: Clock | None = None, ttl: float = 300) -> KeyProvider:
    wrapper = LocalKeyWrapper({"k1": os.urandom(32)}, "k1")
    return KeyProvider({"local": wrapper}, "local", store=store, cache_ttl_s=ttl, clock=clock or Clock())


@pytest.mark.asyncio
async def test_first_use_provisions_version_1_in_the_transaction_memo_only() -> None:
    store, clock = MemoryDekStore(), Clock()
    keys = _local(store, clock)
    ctx = make_ctx()
    dek = await keys.current_dek(ctx, "vault")
    assert (dek.version, len(dek.key)) == (1, 32) and "key" not in repr(dek)
    assert ctx.dek_memo == {("vault", 1): dek.key} and ctx.dek_current == {"vault": 1}
    assert keys._cache == {} and keys._current == {}  # nothing in the process cache before commit
    row = store.rows[(TENANT_A, "vault", 1)].row
    assert row.kek_provider == "local" and row.wrapped_dek and dek.key not in row.wrapped_dek
    assert "wrapped" not in repr(row)
    # The same transaction reuses it; a new transaction unwraps the stored key once, then hits the cache.
    assert (await keys.current_dek(ctx, "vault")).key == dek.key and keys.unwraps == 0
    ctx2 = make_ctx()
    assert (await keys.current_dek(ctx2, "vault")).key == dek.key and keys.unwraps == 1
    assert (await keys.current_dek(make_ctx(), "vault")).key == dek.key and keys.unwraps == 1


@pytest.mark.asyncio
async def test_cache_expiry_mid_request_re_unwraps_transparently() -> None:
    """Card E: the DEK cache expires in the middle of a request; the next field call unwraps again and works."""
    store, clock = MemoryDekStore(), Clock()
    keys = _local(store, clock, ttl=300)
    setup = make_ctx()
    blob = await Vault(keys, setup).encrypt("traveler", "email_enc", "t1", "an@example.test")
    request = make_ctx()
    vault = Vault(keys, request)
    assert await vault.decrypt("traveler", "email_enc", "t1", blob) == "an@example.test"
    unwraps = keys.unwraps
    clock.now += 301  # the cache entry expires between two fields of the same request
    assert await vault.decrypt("traveler", "email_enc", "t1", blob) == "an@example.test"
    assert keys.unwraps == unwraps + 1
    assert await vault.decrypt("traveler", "email_enc", "t1", blob) == "an@example.test"
    assert keys.unwraps == unwraps + 1  # cached again
    assert request.audit_events == []


@pytest.mark.asyncio
async def test_ttl_zero_disables_the_cache() -> None:
    store = MemoryDekStore()
    keys = _local(store, ttl=0)
    await keys.current_dek(make_ctx(), "docs")
    for _ in range(2):
        await keys.current_dek(make_ctx(), "docs")
    assert keys.unwraps == 2 and keys._cache == {}


@pytest.mark.asyncio
async def test_concurrent_first_use_takes_the_stored_key() -> None:
    store = MemoryDekStore()
    keys = _local(store)
    winner = await keys.current_dek(make_ctx(), "scrub")
    keys.clear_cache()
    # A second request that read "no key" before the first committed: its insert loses, it uses the stored one.
    original = store.current
    calls = {"n": 0}

    async def first_none(ctx, purpose):  # noqa: ANN001, ANN202
        calls["n"] += 1
        return None if calls["n"] == 1 else await original(ctx, purpose)

    store.current = first_none  # type: ignore[method-assign]
    loser = await keys.current_dek(make_ctx(), "scrub")
    assert loser.key == winner.key and loser.version == 1


@pytest.mark.asyncio
async def test_rotation_adds_a_version_and_old_values_still_decrypt() -> None:
    store = MemoryDekStore()
    keys = _local(store)
    ctx = make_ctx()
    vault = Vault(keys, ctx)
    old = await vault.encrypt("traveler", "phone_enc", "t1", "+84912345678")
    index_before = await vault.phone_index("traveler", "phone", "+84912345678")
    rotated = await keys.rotate(ctx, "vault")
    assert rotated.version == 2
    new = await vault.encrypt("traveler", "phone_enc", "t1", "+84912345678")
    assert old[:2] == b"\x00\x01" and new[:2] == b"\x00\x02"
    later = Vault(keys, make_ctx())
    assert await later.decrypt("traveler", "phone_enc", "t1", old) == "+84912345678"
    assert await later.decrypt("traveler", "phone_enc", "t1", new) == "+84912345678"
    # Blind indexes derive from the index root (oldest live vault key): rotation changes none of them.
    assert await later.phone_index("traveler", "phone", "+84912345678") == index_before
    assert store.rows[(TENANT_A, "vault", 1)].row.retired and not store.rows[(TENANT_A, "vault", 2)].row.retired
    assert (await keys.rotate(make_ctx(TENANT_B), "mail")).version == 1  # rotating a purpose never used


@pytest.mark.asyncio
async def test_lost_rotation_race_is_unavailable_and_version_cap() -> None:
    store = MemoryDekStore()
    keys = _local(store)
    await keys.current_dek(make_ctx(), "export")

    async def lose(*_: object) -> bool:
        return False

    store.insert = lose  # type: ignore[method-assign]
    with pytest.raises(KeyUnavailable, match="key_unavailable"):
        await keys.rotate(make_ctx(), "export")


def _wrappers() -> dict[str, Callable[[], dict]]:
    return {
        "local to local (new KEK)": lambda: {"source": "local", "target": "local"},
        "local to KMS": lambda: {"source": "local", "target": "kms"},
        "KMS to KMS (BYOK key)": lambda: {"source": "kms", "target": "kms"},
    }


@pytest.mark.parametrize("case", list(_wrappers()))
@pytest.mark.asyncio
async def test_rewrap_contract_every_dek_under_a_new_kek_data_stays_readable(case: str) -> None:
    """Card checklist "rewrap contract test" (lane §5.1, Q 3.6): re-wrap every DEK under a new KEK; every value and
    blind index stays valid without re-encrypting any row, and the old KEK is no longer needed."""
    spec = _wrappers()[case]()
    kms = FakeKms("data", "agency-byok")
    local_old = LocalKeyWrapper({"old": os.urandom(32)}, "old")
    sources = {"local": local_old, "kms": KmsKeyWrapper("alias/crm-dev/data", client_factory=lambda: kms)}
    store = MemoryDekStore()
    keys = KeyProvider({spec["source"]: sources[spec["source"]]}, spec["source"], store=store)
    ctx = make_ctx()
    vault = Vault(keys, ctx)
    values = {
        ("traveler", "display_name_enc", "t1"): "Trần Thị Bình",
        ("traveler", "email_enc", "t1"): "binh@example.test",
    }
    blobs = {k: await vault.encrypt(*k, v) for k, v in values.items()}
    secret = await vault.encrypt_secret(make_ctx().member_id, "rk_test_fake")
    docs = await vault.encrypt("document", "body_enc", "d1", b"%PDF", purpose="docs")
    await keys.rotate(ctx, "vault")
    blobs[("traveler", "phone_enc", "t1")] = await vault.encrypt("traveler", "phone_enc", "t1", "+61412345678")
    values[("traveler", "phone_enc", "t1")] = "+61412345678"
    index = await vault.email_index("traveler", "email", "binh@example.test")
    snapshot = dict(blobs)

    if spec["target"] == "local":
        # The rewriting process knows the old and the new local KEK (same provider name, one keyring).
        target = local_old.with_kek("new", os.urandom(32))
        during = {"local": target}
        after = {"local": target.without("old")}
    else:
        key_id = "alias/crm-dev/agency-byok" if spec["source"] == "kms" else "alias/crm-dev/data"
        target = KmsKeyWrapper(key_id, client_factory=lambda: kms)
        during = {spec["source"]: sources[spec["source"]], "kms": target}
        after = {"kms": target}
    keys_during = KeyProvider(during, spec["source"], store=store)
    assert await keys_during.rewrap(make_ctx(), target) == 4  # vault v1 + v2, integration, docs
    for stored in store.rows.values():
        assert stored.row.kek_provider == target.provider and stored.rewraps == 1
        if spec["target"] == "local":
            assert stored.row.kek_id == "new"

    # A fresh process that has ONLY the new KEK reads everything.
    fresh = KeyProvider(after, target.provider, store=store)
    reader = Vault(fresh, make_ctx())
    for (table, column, row), plain in values.items():
        assert await reader.decrypt(table, column, row, snapshot[(table, column, row)]) == plain
    assert await reader.decrypt_secret(make_ctx().member_id, secret) == "rk_test_fake"
    assert await reader.decrypt_bytes("document", "body_enc", "d1", docs, purpose="docs") == b"%PDF"
    assert await reader.email_index("traveler", "email", "binh@example.test") == index
    assert blobs == snapshot  # no value was re-encrypted


@pytest.mark.asyncio
async def test_rewrap_needs_a_registered_target_provider() -> None:
    keys = _local(MemoryDekStore())
    with pytest.raises(ValueError):
        await keys.rewrap(make_ctx(), KmsKeyWrapper("alias/x", client_factory=FakeKms))


@pytest.mark.asyncio
async def test_destroy_crypto_shreds_the_tenant_only() -> None:
    store = MemoryDekStore()
    keys = _local(store)
    a, b = make_ctx(TENANT_A), make_ctx(TENANT_B)
    blob_a = await Vault(keys, a).encrypt("traveler", "email_enc", "t1", "a@example.test")
    blob_b = await Vault(keys, b).encrypt("traveler", "email_enc", "t1", "b@example.test")
    await Vault(keys, make_ctx(TENANT_A)).decrypt("traveler", "email_enc", "t1", blob_a)  # warm the cache
    assert await keys.destroy(make_ctx(TENANT_A)) == 1
    with pytest.raises(KeyDestroyed):
        await Vault(keys, make_ctx(TENANT_A)).decrypt("traveler", "email_enc", "t1", blob_a)
    assert await Vault(keys, make_ctx(TENANT_B)).decrypt("traveler", "email_enc", "t1", blob_b) == "b@example.test"


@pytest.mark.asyncio
async def test_unknown_purpose_and_unknown_provider() -> None:
    keys = _local(MemoryDekStore())
    with pytest.raises(ValueError):
        await keys.current_dek(make_ctx(), "payments")
    with pytest.raises(KeyUnavailable):
        keys.wrapper("kms")
    with pytest.raises(ValueError):
        KeyProvider({"local": LocalKeyWrapper.ephemeral()}, "kms")


def test_build_from_settings() -> None:
    assert build_key_provider(Settings(environment="dev")) is None
    with pytest.raises(KeyProviderNotConfigured):
        require_keys(None)
    kms = build_key_provider(Settings(environment="dev", kms_data_key_id="alias/crm-dev/data"))
    assert kms is not None and kms.active_provider == "kms"
    local = build_key_provider(Settings(environment="test", key_provider="local", local_kek_b64="A" * 43 + "="))
    assert local is not None and local.active_provider == "local"
    assert build_key_provider(Settings(environment="test", key_provider="local")).active_provider == "local"
    with pytest.raises(ValueError, match="base64"):
        build_key_provider(Settings(environment="test", key_provider="local", local_kek_b64="***"))


@pytest.mark.asyncio
async def test_a_refused_unwrap_outside_decrypt_is_unavailable_not_a_500() -> None:
    """UnwrapDenied leaves the provider only through get_dek (audited by Vault._open). On the encrypt, blind-index and
    rewrap paths a tampered current key row (or a KMS refusal) is a 503 KeyUnavailable, never an unhandled error."""
    from dataclasses import replace

    from app.vault.errors import UnwrapDenied

    store = MemoryDekStore()
    keys = _local(store)
    await Vault(keys, make_ctx()).encrypt("traveler", "email_enc", "t1", "a@example.test")
    stored = store.rows[(TENANT_A, "vault", 1)]
    stored.row = replace(stored.row, wrapped_dek=os.urandom(60))  # tampered with master-level DB access
    keys.clear_cache()
    for attempt in (
        lambda v: v.encrypt("traveler", "email_enc", "t2", "b@example.test"),
        lambda v: v.email_index("traveler", "email", "b@example.test"),
    ):
        with pytest.raises(KeyUnavailable) as info:
            await attempt(Vault(keys, make_ctx()))
        assert info.value.reason == "key_unwrap_refused" and info.value.status_code == 503
    with pytest.raises(KeyUnavailable, match="key_unavailable"):
        await keys.rewrap(make_ctx(), keys.wrapper("local"))
    # The read path still sees the refusal, so the vault can audit it.
    with pytest.raises(UnwrapDenied):
        await keys.get_dek(make_ctx(), "vault", 1)


@pytest.mark.asyncio
async def test_after_destroy_new_values_report_key_destroyed_not_a_race() -> None:
    store = MemoryDekStore()
    keys = _local(store)
    await Vault(keys, make_ctx()).encrypt("traveler", "email_enc", "t1", "a@example.test")
    await keys.destroy(make_ctx())
    with pytest.raises(KeyDestroyed):
        await Vault(keys, make_ctx()).encrypt("traveler", "email_enc", "t2", "b@example.test")
    with pytest.raises(KeyDestroyed):
        await keys.current_dek(make_ctx(), "vault")


@pytest.mark.asyncio
async def test_key_operations_are_audit_events() -> None:
    """AA-940 (design §4.9 "key operations"): create, rotate, rewrap and destroy each leave one event (codes only)."""
    keys = _local(MemoryDekStore())
    ctx = make_ctx()
    await Vault(keys, ctx).encrypt("traveler", "email_enc", "t1", "a@example.test")
    await keys.rotate(ctx, "vault")
    target = LocalKeyWrapper({"kek-2": os.urandom(32)}, "kek-2")
    keys_two = KeyProvider({"local": target}, "local", store=MemoryDekStore())
    await keys_two.rewrap(ctx, target)
    await keys.destroy(ctx)
    assert [(e.action, e.object_kind, e.object_id, e.reason) for e in ctx.audit_events] == [
        ("key_create", "tenant_key", "vault:v1", "purpose_vault"),
        ("key_rotate", "tenant_key", "vault:v2", "purpose_vault"),
        ("key_rewrap", "tenant_key", None, "to_local"),
        ("key_destroy", "tenant_key", None, "crypto_shred"),
    ]
