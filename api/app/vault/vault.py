"""``Vault``: field encryption for request code, inside ``tenant_tx()`` (design v3 §3.2, §4.1–4.2).

Usage::

    async with tenant_tx(pool, caller, tenant_id) as ctx:
        vault = Vault(keys, ctx)
        blob = await vault.encrypt("traveler", "email_enc", traveler_id, "an@example.test")
        index = await vault.email_index("traveler", "email", "an@example.test")
        ...
        email = await vault.decrypt("traveler", "email_enc", traveler_id, row["email_enc"])

Every value is bound to ``(tenant, table, column, row id)`` and to its key version (:mod:`app.vault.cipher`). A value
that cannot be opened here — another tenant's (``tenant_id=`` of its row differs, or the AAD fails), another row's or
column's, tampered, under an unknown version — raises :class:`~app.vault.errors.DecryptionDenied` after queueing an
``audit_event`` with outcome ``denied`` (action ``decrypt``, the table as object kind, the row id, the column, a reason
code). ``tenant_tx()`` writes that row even when the exception ends the request.

Allowed decrypts are not audited here: the reveal of a field and the one ``list_view`` event per page are written by
the cards that show data (CRM-09, CRM-12; design §4.4), so list views do not flood the audit.

Integration secrets (purpose ``integration``, errata S247) have their own pair, :meth:`Vault.encrypt_secret` /
:meth:`Vault.decrypt_secret`; the generic methods refuse that purpose, and a lint allows ``decrypt_secret`` only in
the dispatcher, webhook and health-check modules (``tests/unit/test_vault_rules.py``).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, NoReturn

from app.vault import blind_index
from app.vault.cipher import FieldFormatError, InvalidTag, field_aad, key_version_of, open_sealed, seal
from app.vault.errors import DecryptionDenied, UnwrapDenied
from app.vault.provider import PURPOSES, KeyProvider

if TYPE_CHECKING:  # pragma: no cover
    from app.tenancy import TenantContext

CREDENTIAL_TABLE = "integration_credential"
CREDENTIAL_COLUMN = "secret_enc"
GENERIC_PURPOSES = PURPOSES - {"integration"}


class Vault:
    def __init__(self, keys: KeyProvider, ctx: TenantContext) -> None:
        self._keys = keys
        self._ctx = ctx

    # ── Field values ───────────────────────────────────────────────────────────────────────────────
    async def encrypt(
        self, table: str, column: str, row_id: uuid.UUID | str, value: str | bytes, *, purpose: str = "vault"
    ) -> bytes:
        _generic(purpose)
        return await self._seal(table, column, row_id, value, purpose)

    async def decrypt(
        self,
        table: str,
        column: str,
        row_id: uuid.UUID | str,
        blob: bytes,
        *,
        purpose: str = "vault",
        tenant_id: uuid.UUID | None = None,
    ) -> str:
        """Open one value. ``tenant_id``: the tenant of the row the value was read from, when the caller has it; a
        value of another tenant is refused before any key is touched."""
        _generic(purpose)
        return (await self._open(table, column, row_id, blob, purpose, tenant_id)).decode("utf-8")

    async def decrypt_bytes(
        self, table: str, column: str, row_id: uuid.UUID | str, blob: bytes, *, purpose: str = "vault"
    ) -> bytes:
        _generic(purpose)
        return await self._open(table, column, row_id, blob, purpose, None)

    # ── Integration secrets (errata S247) ──────────────────────────────────────────────────────────
    async def encrypt_secret(self, credential_id: uuid.UUID, secret: str) -> bytes:
        return await self._seal(CREDENTIAL_TABLE, CREDENTIAL_COLUMN, credential_id, secret, "integration")

    async def decrypt_secret(self, credential_id: uuid.UUID, blob: bytes) -> str:
        """Only the dispatcher, webhook and health-check paths may call this (lint); never return the result."""
        plaintext = await self._open(CREDENTIAL_TABLE, CREDENTIAL_COLUMN, credential_id, blob, "integration", None)
        return plaintext.decode("utf-8")

    # ── Blind indexes (exact match only) ───────────────────────────────────────────────────────────
    async def email_index(self, table: str, column: str, value: str) -> bytes:
        return blind_index.email_index(await self._index_key(table, column), value)

    async def phone_index(self, table: str, column: str, value: str, region: str | None = None) -> bytes:
        return blind_index.phone_index(await self._index_key(table, column), value, region)

    async def name_index(self, table: str, column: str, value: str) -> bytes:
        return blind_index.name_index(await self._index_key(table, column), value)

    async def name_token_indexes(self, table: str, column: str, value: str) -> list[bytes]:
        return blind_index.name_token_indexes(await self._index_key(table, column), value)

    # ── Internals ──────────────────────────────────────────────────────────────────────────────────
    async def _seal(self, table: str, column: str, row_id: uuid.UUID | str, value: str | bytes, purpose: str) -> bytes:
        aad = field_aad(self._ctx.tenant_id, table, column, row_id)
        plaintext = value.encode("utf-8") if isinstance(value, str) else bytes(value)
        dek = await self._keys.current_dek(self._ctx, purpose)
        return seal(dek.key, dek.version, plaintext, aad)

    async def _open(
        self,
        table: str,
        column: str,
        row_id: uuid.UUID | str,
        blob: bytes,
        purpose: str,
        tenant_id: uuid.UUID | None,
    ) -> bytes:
        aad = field_aad(self._ctx.tenant_id, table, column, row_id)
        if tenant_id is not None and tenant_id != self._ctx.tenant_id:
            self._deny(table, column, row_id, "tenant_mismatch")
        try:
            version = key_version_of(bytes(blob))
            dek = await self._keys.get_dek(self._ctx, purpose, version)
            return open_sealed(dek.key, bytes(blob), aad)
        except (InvalidTag, FieldFormatError):
            self._deny(table, column, row_id, "integrity_check_failed")
        except UnwrapDenied as exc:
            reason = "key_version_unknown" if str(exc) == "key_version_unknown" else "key_unwrap_refused"
            self._deny(table, column, row_id, reason)

    def _deny(self, table: str, column: str, row_id: uuid.UUID | str, reason: str) -> NoReturn:
        self._ctx.audit("decrypt", "denied", table, str(row_id), fields=(column,), reason=reason)
        raise DecryptionDenied(reason)

    async def _index_key(self, table: str, column: str) -> bytes:
        root = await self._keys.index_root(self._ctx)
        return blind_index.index_key(root.key, self._ctx.tenant_id, f"{table}.{column}")


def _generic(purpose: str) -> None:
    if purpose not in GENERIC_PURPOSES:
        raise ValueError("integration secrets go through encrypt_secret / decrypt_secret")
