"""Stand-ins for the vault tests: an in-memory DEK store that behaves like ``crm.tenant_key`` under RLS (rows of the
context's tenant only), a fake KMS client with encryption-context checks, a manual clock and a context factory.

Real SQL behaviour (constraints, guard trigger, RLS) is covered by tests/integration/test_crm003_vault.py.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, replace
from typing import Any

from botocore.exceptions import ClientError
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.tenancy import TenantContext
from app.vault.store import KeyRow
from app.vault.wrapping import canonical_context

TENANT_A = uuid.UUID("00000000-0000-4000-8000-0000000000a1")
TENANT_B = uuid.UUID("00000000-0000-4000-8000-0000000000b2")


def make_ctx(tenant_id: uuid.UUID = TENANT_A, *, reader_role: str | None = None) -> TenantContext:
    return TenantContext(
        conn=None,  # type: ignore[arg-type] — the memory store never touches it
        tenant_id=tenant_id,
        member_id=uuid.UUID("00000000-0000-4000-8000-00000000c0de"),
        member_role="owner",
        reader_role=reader_role,
    )


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@dataclass
class _Stored:
    row: KeyRow
    rewraps: int = 0


class MemoryDekStore:
    """``crm.tenant_key`` semantics: unique (tenant, purpose, version), one current key per (tenant, purpose)."""

    def __init__(self) -> None:
        self.rows: dict[tuple[uuid.UUID, str, int], _Stored] = {}

    def _mine(self, ctx: TenantContext) -> list[_Stored]:
        return [s for (t, _, _), s in sorted(self.rows.items(), key=lambda kv: kv[0][1:]) if t == ctx.tenant_id]

    async def current(self, ctx, purpose):  # noqa: ANN001, ANN201
        found = [s.row for s in self._mine(ctx) if s.row.purpose == purpose and not s.row.retired]
        return found[0] if found else None

    async def version(self, ctx, purpose, key_version):  # noqa: ANN001, ANN201
        stored = self.rows.get((ctx.tenant_id, purpose, key_version))
        return stored.row if stored else None

    async def oldest_live(self, ctx, purpose):  # noqa: ANN001, ANN201
        live = [s.row for s in self._mine(ctx) if s.row.purpose == purpose and not s.row.destroyed]
        return min(live, key=lambda r: r.key_version) if live else None

    async def insert(self, ctx, purpose, key_version, kek_provider, kek_id, wrapped):  # noqa: ANN001, ANN201
        if (ctx.tenant_id, purpose, key_version) in self.rows or await self.current(ctx, purpose) is not None:
            return False
        row = KeyRow(purpose, key_version, kek_provider, kek_id, wrapped, False, False)
        self.rows[(ctx.tenant_id, purpose, key_version)] = _Stored(row)
        return True

    async def retire_current(self, ctx, purpose):  # noqa: ANN001, ANN201
        row = await self.current(ctx, purpose)
        if row is None:
            return None
        retired = replace(row, retired=True)
        self.rows[(ctx.tenant_id, purpose, row.key_version)].row = retired
        return retired

    async def live(self, ctx):  # noqa: ANN001, ANN201
        return [s.row for s in self._mine(ctx) if not s.row.destroyed]

    async def replace_wrapping(self, ctx, purpose, key_version, kek_provider, kek_id, wrapped):  # noqa: ANN001, ANN201
        stored = self.rows[(ctx.tenant_id, purpose, key_version)]
        stored.row = replace(stored.row, kek_provider=kek_provider, kek_id=kek_id, wrapped_dek=wrapped)
        stored.rewraps += 1

    async def destroy_all(self, ctx):  # noqa: ANN001, ANN201
        count = 0
        for stored in self._mine(ctx):
            if not stored.row.destroyed:
                stored.row = replace(stored.row, wrapped_dek=None, retired=True, destroyed=True)
                count += 1
        return count

    def copy_row(self, src: uuid.UUID, dst: uuid.UUID, purpose: str, version: int) -> None:
        """What a master user with DB access could do: copy one tenant's wrapped key row into another tenant."""
        self.rows[(dst, purpose, version)] = _Stored(self.rows[(src, purpose, version)].row)


def client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "fake"}}, "Fake")


class FakeKms:
    """The three KMS calls the KMS wrapper makes, with KMS's encryption-context rule (exact match on decrypt)."""

    ARN = "arn:aws:kms:us-west-1:123456789012:key/{}"

    def __init__(self, *key_ids: str) -> None:
        self.keys = {k: os.urandom(32) for k in (key_ids or ("data",))}
        self.calls: list[str] = []
        self.fail_with: str | None = None

    def _arn(self, key_id: str) -> str:
        name = key_id.removeprefix("alias/crm-dev/")
        if name not in self.keys:
            raise client_error("NotFoundException")
        return self.ARN.format(name)

    def _seal(self, arn: str, plaintext: bytes, context: dict[str, str]) -> bytes:
        nonce = os.urandom(12)
        name = arn.rsplit("/", 1)[1]
        sealed = AESGCM(self.keys[name]).encrypt(nonce, plaintext, canonical_context(context))
        return name.encode() + b"|" + nonce + sealed

    def _maybe_fail(self, call: str) -> None:
        self.calls.append(call)
        if self.fail_with:
            raise client_error(self.fail_with)

    def generate_data_key(self, KeyId: str, KeySpec: str, EncryptionContext: dict[str, str]) -> dict[str, Any]:  # noqa: N803
        self._maybe_fail("generate_data_key")
        assert KeySpec == "AES_256"
        arn = self._arn(KeyId)
        plaintext = os.urandom(32)
        return {"Plaintext": plaintext, "CiphertextBlob": self._seal(arn, plaintext, EncryptionContext), "KeyId": arn}

    def encrypt(self, KeyId: str, Plaintext: bytes, EncryptionContext: dict[str, str]) -> dict[str, Any]:  # noqa: N803
        self._maybe_fail("encrypt")
        arn = self._arn(KeyId)
        return {"CiphertextBlob": self._seal(arn, Plaintext, EncryptionContext), "KeyId": arn}

    def decrypt(self, CiphertextBlob: bytes, EncryptionContext: dict[str, str], KeyId: str) -> dict[str, Any]:  # noqa: N803
        self._maybe_fail("decrypt")
        name, _, rest = CiphertextBlob.partition(b"|")
        arn = self.ARN.format(name.decode())
        if KeyId != arn:
            raise client_error("IncorrectKeyException")
        try:
            aad = canonical_context(EncryptionContext)
            plaintext = AESGCM(self.keys[name.decode()]).decrypt(rest[:12], rest[12:], aad)
        except InvalidTag:
            raise client_error("InvalidCiphertextException") from None
        return {"Plaintext": plaintext, "KeyId": arn}
