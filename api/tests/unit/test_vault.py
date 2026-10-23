"""Vault: the card's R test (another tenant's value fails and is audited denied), tampering, the integration secret
path, blind-index helpers."""

from __future__ import annotations

import os
import uuid

import pytest

from app.vault import KeyProvider, Vault
from app.vault.errors import DecryptionDenied
from app.vault.wrapping import LocalKeyWrapper
from tests.unit.vault_fakes import TENANT_A, TENANT_B, MemoryDekStore, make_ctx

ROW = uuid.UUID("00000000-0000-4000-8000-0000000000e1")


def _keys(store: MemoryDekStore | None = None) -> KeyProvider:
    return KeyProvider({"local": LocalKeyWrapper({"k": os.urandom(32)}, "k")}, "local", store=store or MemoryDekStore())


def _only_denial(ctx) -> dict:  # noqa: ANN001
    assert len(ctx.audit_events) == 1
    event = ctx.audit_events[0]
    assert (event.actor_kind, event.actor_ref) == ("user", f"member:{ctx.member_id}")
    return {k: getattr(event, k) for k in ("action", "outcome", "object_kind", "object_id", "fields", "reason")}


@pytest.mark.asyncio
async def test_round_trip_unicode() -> None:
    keys = _keys()
    vault = Vault(keys, make_ctx())
    blob = await vault.encrypt("traveler", "display_name_enc", ROW, "Lê Thị Ngọc Ánh")
    assert "Ngọc".encode() not in blob
    assert await vault.decrypt("traveler", "display_name_enc", ROW, blob, tenant_id=TENANT_A) == "Lê Thị Ngọc Ánh"


@pytest.mark.asyncio
async def test_another_tenants_value_is_denied_and_audited() -> None:
    """Card R: decrypting with another tenant's context fails and is audited ``denied``."""
    keys = _keys()
    blob_b = await Vault(keys, make_ctx(TENANT_B)).encrypt("traveler", "email_enc", ROW, "b@example.test")
    ctx_a = make_ctx(TENANT_A)
    with pytest.raises(DecryptionDenied) as info:
        await Vault(keys, ctx_a).decrypt("traveler", "email_enc", ROW, blob_b)
    assert info.value.status_code == 403 and info.value.code == "decrypt_denied"
    assert "b@example.test" not in str(info.value.body())
    assert _only_denial(ctx_a) == {
        "action": "decrypt",
        "outcome": "denied",
        "object_kind": "traveler",
        "object_id": str(ROW),
        "fields": ("email_enc",),
        "reason": "key_version_unknown",  # tenant A has no key at all yet
    }
    # Tenant A with its own key: B's value fails the tag (AAD = tenant A, A's DEK).
    await Vault(keys, make_ctx(TENANT_A)).encrypt("traveler", "email_enc", ROW, "a@example.test")
    ctx_a2 = make_ctx(TENANT_A)
    with pytest.raises(DecryptionDenied):
        await Vault(keys, ctx_a2).decrypt("traveler", "email_enc", ROW, blob_b)
    assert _only_denial(ctx_a2)["reason"] == "integrity_check_failed"
    # A caller that knows the row's tenant is refused before any key is used.
    ctx_a3 = make_ctx(TENANT_A)
    with pytest.raises(DecryptionDenied):
        await Vault(keys, ctx_a3).decrypt("traveler", "email_enc", ROW, blob_b, tenant_id=TENANT_B)
    assert _only_denial(ctx_a3)["reason"] == "tenant_mismatch"


@pytest.mark.asyncio
async def test_a_copied_key_row_does_not_unwrap_in_another_tenant() -> None:
    """A wrapped DEK row copied from B into A (master-level DB access) is refused: the context names tenant A."""
    store = MemoryDekStore()
    keys = _keys(store)
    blob_b = await Vault(keys, make_ctx(TENANT_B)).encrypt("traveler", "email_enc", ROW, "b@example.test")
    store.copy_row(TENANT_B, TENANT_A, "vault", 1)
    ctx_a = make_ctx(TENANT_A)
    with pytest.raises(DecryptionDenied):
        await Vault(keys, ctx_a).decrypt("traveler", "email_enc", ROW, blob_b)
    assert _only_denial(ctx_a)["reason"] == "key_unwrap_refused"


@pytest.mark.asyncio
async def test_tampered_moved_or_garbage_values_are_denied() -> None:
    keys = _keys()
    vault = Vault(keys, make_ctx())
    blob = bytearray(await vault.encrypt("traveler", "email_enc", ROW, "a@example.test"))
    blob[-1] ^= 1
    for args in (
        ("traveler", "email_enc", ROW, bytes(blob)),
        ("traveler", "phone_enc", ROW, await vault.encrypt("traveler", "email_enc", ROW, "x@example.test")),
        ("traveler", "email_enc", "other-row", await vault.encrypt("traveler", "email_enc", ROW, "y@example.test")),
        ("traveler", "email_enc", ROW, b"short"),
    ):
        ctx = make_ctx()
        with pytest.raises(DecryptionDenied):
            await Vault(keys, ctx).decrypt(*args)
        assert _only_denial(ctx)["reason"] == "integrity_check_failed"


@pytest.mark.asyncio
async def test_integration_secrets_have_their_own_path() -> None:
    keys = _keys()
    vault = Vault(keys, make_ctx())
    blob = await vault.encrypt_secret(ROW, "rk_test_fake_value")
    assert await vault.decrypt_secret(ROW, blob) == "rk_test_fake_value"
    with pytest.raises(ValueError, match="encrypt_secret"):
        await vault.encrypt("integration_credential", "secret_enc", ROW, "x", purpose="integration")
    with pytest.raises(ValueError):
        await vault.decrypt("integration_credential", "secret_enc", ROW, blob, purpose="integration")
    with pytest.raises(DecryptionDenied):  # the vault DEK cannot open it either
        await vault.decrypt("integration_credential", "secret_enc", ROW, blob)


@pytest.mark.asyncio
async def test_blind_index_helpers_are_stable_per_tenant() -> None:
    keys = _keys()
    a1, a2, b = Vault(keys, make_ctx()), Vault(keys, make_ctx()), Vault(keys, make_ctx(TENANT_B))
    assert await a1.email_index("traveler", "email", "AN@x.test") == await a2.email_index(
        "traveler", "email", "an@x.test"
    )
    assert await a1.email_index("traveler", "email", "an@x.test") != await b.email_index(
        "traveler", "email", "an@x.test"
    )
    assert len(await a1.phone_index("traveler", "phone", "0912345678", "VN")) == 16
    name_a1 = await a1.name_index("traveler", "name", "Nguyễn An")
    assert name_a1 == await a2.name_index("traveler", "name", "nguyen an")
    assert len(await a1.name_token_indexes("traveler", "name_token", "Nguyễn Văn An")) == 3
