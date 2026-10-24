"""``KeyProvider``: per-tenant, per-purpose data keys (design v3 §4.1; lane §5.1 ``get_dek`` / ``rewrap`` /
``destroy``).

- :meth:`KeyProvider.get_dek` returns the DEK of ``(tenant, purpose, version)``: from the per-transaction memo, then
  the process cache (TTL ``DEK_CACHE_TTL_SECONDS``, default 300 s), then the store + an unwrap by the wrapper named on
  the row. When the cache entry expired in the middle of a request, the next call simply unwraps again (card E).
- :meth:`KeyProvider.current_dek` is the key new values are encrypted under; the first use of a purpose generates
  version 1 through the active wrapper (KMS ``GenerateDataKey`` with the context ``{tenant_id, purpose}``).
- :meth:`KeyProvider.rotate` retires the current version and adds the next: new values use it, older values still
  decrypt (the version is in each value's header).
- :meth:`KeyProvider.rewrap` re-wraps every live DEK of the tenant under another KEK without touching any
  ciphertext (the contract test; the BYOK path, design Q 3.6).
- :meth:`KeyProvider.destroy` crypto-shreds the tenant (offboarding, CRM-35): wrapped DEKs nulled, ``destroyed_at`` set.

A DEK created inside a transaction (first use, rotation) stays in that transaction's memo and never enters the process
cache: if the transaction rolls back, no other request can encrypt under a key that was never stored.

:class:`~app.vault.errors.UnwrapDenied` leaves this class only through :meth:`KeyProvider.get_dek`, the read path
whose caller (``Vault._open``) turns it into an audited :class:`~app.vault.errors.DecryptionDenied`. On every other
path (encrypt via :meth:`current_dek`, blind indexes via :meth:`index_root`, :meth:`rotate`, :meth:`rewrap`) a refused
unwrap of the tenant's own current key row is not about a value the caller holds, so it becomes
:class:`~app.vault.errors.KeyUnavailable` (``key_unwrap_refused``, 503) instead of an unhandled 500.

Crypto-shred is immediate in the process that runs :meth:`destroy` only: other API / worker tasks may still hold an
unwrapped DEK in their cache for up to ``DEK_CACHE_TTL_SECONDS``. Offboarding (CRM-35) waits out that TTL before it
reports the tenant shredded.

Keys never reach a log line, an exception message, a job payload or ``repr``.
"""

from __future__ import annotations

import base64
import binascii
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.vault.errors import KeyDestroyed, KeyProviderNotConfigured, KeyUnavailable, UnwrapDenied
from app.vault.store import DekStore, KeyRow, TableDekStore
from app.vault.wrapping import KeyWrapper, KmsKeyWrapper, LocalKeyWrapper

if TYPE_CHECKING:  # pragma: no cover
    from app.settings import Settings
    from app.tenancy import TenantContext

PURPOSES = frozenset({"vault", "docs", "integration", "scrub", "ops_release", "export", "mail"})
MAX_VERSION = 65535
ROOT_MEMO = ":index_root"  # key in ctx.dek_current for the index-root version provisioned in this transaction


@dataclass(frozen=True)
class Dek:
    tenant_id: uuid.UUID
    purpose: str
    version: int
    key: bytes = field(repr=False)

    def __repr__(self) -> str:
        return f"Dek({self.tenant_id}, {self.purpose!r}, v{self.version})"


class KeyProvider:
    def __init__(
        self,
        wrappers: Mapping[str, KeyWrapper],
        active: str,
        *,
        store: DekStore | None = None,
        cache_ttl_s: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if active not in wrappers:
            raise ValueError("the active wrapper must be one of the wrappers")
        self._wrappers = dict(wrappers)
        self._active = active
        self._store = store or TableDekStore()
        self._ttl = cache_ttl_s
        self._clock = clock
        # (tenant, purpose, version) -> (key, expires_at); (tenant, purpose) -> (current version, expires_at)
        self._cache: dict[tuple[uuid.UUID, str, int], tuple[bytes, float]] = {}
        self._current: dict[tuple[uuid.UUID, str], tuple[int, float]] = {}
        self._root: dict[uuid.UUID, tuple[int, float]] = {}  # tenant -> index-root vault version
        self.unwraps = 0  # counter for tests and the self-test report

    @property
    def active_provider(self) -> str:
        return self._active

    def wrapper(self, provider: str) -> KeyWrapper:
        try:
            return self._wrappers[provider]
        except KeyError:
            raise KeyUnavailable("kek_provider_unknown") from None

    # ── Reads ──────────────────────────────────────────────────────────────────────────────────────
    async def get_dek(self, ctx: TenantContext, purpose: str, version: int) -> Dek:
        _check_purpose(purpose)
        memo = ctx.dek_memo.get((purpose, version))
        if memo is not None:
            return Dek(ctx.tenant_id, purpose, version, memo)
        cached = self._cache.get((ctx.tenant_id, purpose, version))
        if cached is not None and cached[1] > self._clock():
            return Dek(ctx.tenant_id, purpose, version, cached[0])
        row = await self._store.version(ctx, purpose, version)
        return Dek(ctx.tenant_id, purpose, version, await self._unwrap_row(ctx, row, cache=True))

    async def current_dek(self, ctx: TenantContext, purpose: str) -> Dek:
        """The DEK new values are encrypted under; generated (version 1) on the first use of a purpose."""
        return await _unwrap_refusal_is_unavailable(self._current_dek(ctx, purpose))

    async def _current_dek(self, ctx: TenantContext, purpose: str) -> Dek:
        _check_purpose(purpose)
        memo_version = ctx.dek_current.get(purpose)
        if memo_version is not None:
            return await self.get_dek(ctx, purpose, memo_version)
        current = self._current.get((ctx.tenant_id, purpose))
        if current is not None and current[1] > self._clock():
            return await self.get_dek(ctx, purpose, current[0])
        row = await self._store.current(ctx, purpose)
        if row is None:
            return await self._provision(ctx, purpose, 1)
        key = await self._unwrap_row(ctx, row, cache=True)
        self._current[(ctx.tenant_id, purpose)] = (row.key_version, self._clock() + self._ttl)
        return Dek(ctx.tenant_id, purpose, row.key_version, key)

    async def index_root(self, ctx: TenantContext) -> Dek:
        """The vault DEK blind-index keys derive from: the tenant's oldest live vault key, so a rotation does not
        change any index (no re-index job). Generated on first use like :meth:`current_dek`."""
        return await _unwrap_refusal_is_unavailable(self._index_root(ctx))

    async def _index_root(self, ctx: TenantContext) -> Dek:
        memo_version = ctx.dek_current.get(ROOT_MEMO)
        if memo_version is not None:
            return await self.get_dek(ctx, "vault", memo_version)
        cached = self._root.get(ctx.tenant_id)
        if cached is not None and cached[1] > self._clock():
            return await self.get_dek(ctx, "vault", cached[0])
        row = await self._store.oldest_live(ctx, "vault")
        if row is None:
            dek = await self._current_dek(ctx, "vault")  # provisioned in this transaction: memo only
            ctx.dek_current[ROOT_MEMO] = dek.version
            return dek
        self._root[ctx.tenant_id] = (row.key_version, self._clock() + self._ttl)
        return await self.get_dek(ctx, "vault", row.key_version)

    # ── Changes ────────────────────────────────────────────────────────────────────────────────────
    async def rotate(self, ctx: TenantContext, purpose: str) -> Dek:
        """Retire the current DEK of ``purpose`` and create the next version (in the caller's transaction)."""
        return await _unwrap_refusal_is_unavailable(self._rotate(ctx, purpose))

    async def _rotate(self, ctx: TenantContext, purpose: str) -> Dek:
        _check_purpose(purpose)
        retired = await self._store.retire_current(ctx, purpose)
        next_version = 1 if retired is None else retired.key_version + 1
        if next_version > MAX_VERSION:
            raise KeyUnavailable("key_versions_exhausted")
        self._current.pop((ctx.tenant_id, purpose), None)
        dek = await self._provision(ctx, purpose, next_version, after_retire=True)
        _audit_key(ctx, "key_rotate", purpose, next_version)
        return dek

    async def rewrap(self, ctx: TenantContext, target: KeyWrapper) -> int:
        """Re-wrap every live DEK of the tenant under ``target`` (another KEK). No ciphertext changes; the plaintext
        DEK stays the same, so every value and blind index stays valid. Returns the number of keys re-wrapped."""
        if target.provider not in self._wrappers:
            raise ValueError("register the target wrapper's provider before rewrapping to it")
        return await _unwrap_refusal_is_unavailable(self._rewrap(ctx, target))

    async def _rewrap(self, ctx: TenantContext, target: KeyWrapper) -> int:
        count = 0
        for row in await self._store.live(ctx):
            key = await self._unwrap_row(ctx, row, cache=False)
            wrapped, kek_id = await target.wrap(key, _context(ctx, row.purpose))
            await self._store.replace_wrapping(ctx, row.purpose, row.key_version, target.provider, kek_id, wrapped)
            count += 1
        audit = getattr(ctx, "audit", None)
        if audit is not None:  # design §4.9 "key operations" (AA-940): one event per run
            audit("key_rewrap", "allowed", "tenant_key", reason=f"to_{target.provider}")
        return count

    async def destroy(self, ctx: TenantContext) -> int:
        """Crypto-shred every key of the tenant (offboarding). Values encrypted under them are gone for good."""
        count = await self._store.destroy_all(ctx)
        audit = getattr(ctx, "audit", None)
        if audit is not None:
            audit("key_destroy", "allowed", "tenant_key", reason="crypto_shred")
        self.evict(ctx.tenant_id)
        ctx.dek_memo.clear()
        ctx.dek_current.clear()
        return count

    def evict(self, tenant_id: uuid.UUID) -> None:
        for k in [k for k in self._cache if k[0] == tenant_id]:
            del self._cache[k]
        for k in [k for k in self._current if k[0] == tenant_id]:
            del self._current[k]
        self._root.pop(tenant_id, None)

    def clear_cache(self) -> None:
        self._cache.clear()
        self._current.clear()
        self._root.clear()

    # ── Internals ──────────────────────────────────────────────────────────────────────────────────
    async def _provision(self, ctx: TenantContext, purpose: str, version: int, *, after_retire: bool = False) -> Dek:
        wrapper = self.wrapper(self._active)
        plaintext, wrapped, kek_id = await wrapper.generate(_context(ctx, purpose))
        if await self._store.insert(ctx, purpose, version, wrapper.provider, kek_id, wrapped):
            if not after_retire:
                _audit_key(ctx, "key_create", purpose, version)
            ctx.dek_memo[(purpose, version)] = plaintext
            ctx.dek_current[purpose] = version
            return Dek(ctx.tenant_id, purpose, version, plaintext)
        if after_retire:  # a concurrent rotation won; it holds the row lock order, so this cannot loop
            raise KeyUnavailable("concurrent_rotation")
        # Another request created the first key at the same time: use the stored one.
        row = await self._store.current(ctx, purpose)
        if row is None:
            existing = await self._store.version(ctx, purpose, version)
            if existing is not None and existing.destroyed:  # the tenant was crypto-shredded, not a race
                raise KeyDestroyed()
            raise KeyUnavailable("key_race")
        key = await self._unwrap_row(ctx, row, cache=True)
        return Dek(ctx.tenant_id, purpose, row.key_version, key)

    async def _unwrap_row(self, ctx: TenantContext, row: KeyRow | None, *, cache: bool) -> bytes:
        if row is None:
            raise UnwrapDenied("key_version_unknown")
        if row.destroyed or row.wrapped_dek is None:
            raise KeyDestroyed()
        key = await self.wrapper(row.kek_provider).unwrap(row.wrapped_dek, _context(ctx, row.purpose), row.kek_id)
        self.unwraps += 1
        if cache and self._ttl > 0:
            self._cache[(ctx.tenant_id, row.purpose, row.key_version)] = (key, self._clock() + self._ttl)
        return key


async def _unwrap_refusal_is_unavailable[T](work: Awaitable[T]) -> T:
    try:
        return await work
    except UnwrapDenied:
        raise KeyUnavailable("key_unwrap_refused") from None


def _audit_key(ctx: TenantContext, action: str, purpose: str, version: int) -> None:
    """Design §4.9 "key operations" (AA-940): a DEK created or rotated is an audit event of the agency (codes only).
    Written with the caller's transaction; a context without an audit queue (none today) records nothing."""
    audit = getattr(ctx, "audit", None)
    if audit is not None:
        audit(action, "allowed", "tenant_key", f"{purpose}:v{version}", reason=f"purpose_{purpose}")


def _check_purpose(purpose: str) -> None:
    if purpose not in PURPOSES:
        raise ValueError(f"unknown DEK purpose {purpose!r}")


def _context(ctx: TenantContext, purpose: str) -> dict[str, str]:
    return {"tenant_id": str(ctx.tenant_id), "purpose": purpose}


def build_key_provider(settings: Settings) -> KeyProvider | None:
    """The process's provider from settings, or ``None`` when ``KEY_PROVIDER=kms`` has no ``KMS_DATA_KEY_ID``."""
    if settings.key_provider == "local":
        if settings.local_kek_b64:
            try:
                kek = base64.b64decode(settings.local_kek_b64, validate=True)
            except (binascii.Error, ValueError):
                raise ValueError("LOCAL_KEK_B64 is not base64") from None
            local = LocalKeyWrapper({"local-1": kek}, "local-1")
        else:
            local = LocalKeyWrapper.ephemeral()
        return KeyProvider({"local": local}, "local", cache_ttl_s=settings.dek_cache_ttl_seconds)
    if not settings.kms_data_key_id:
        return None
    return KeyProvider(
        {"kms": KmsKeyWrapper(settings.kms_data_key_id)}, "kms", cache_ttl_s=settings.dek_cache_ttl_seconds
    )


def require_keys(keys: KeyProvider | None) -> KeyProvider:
    if keys is None:
        raise KeyProviderNotConfigured()
    return keys
