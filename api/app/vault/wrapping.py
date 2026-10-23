"""KEK wrappers: how a tenant DEK is generated, wrapped and unwrapped (design v3 §4.1, lane §5.1).

A wrapper never sees a table: it turns ``(plaintext DEK, context)`` into a wrapped blob and back. The context is
always exactly ``{"tenant_id": "<uuid>", "purpose": "<purpose>"}`` — the KMS encryption context the AA-932 key policy
requires (``kms:EncryptionContextKeys`` = tenant_id + purpose, a known purpose), so a DEK wrapped for one tenant or
purpose cannot be unwrapped under another.

- :class:`KmsKeyWrapper`: ``GenerateDataKey`` (AES_256) / ``Decrypt`` / ``Encrypt`` on the CRM data key
  (``KMS_DATA_KEY_ID`` = ``alias/crm-<env>/data``). boto3 is blocking, so every call runs in a thread. The KEK id
  stored per row is the key ARN KMS returns, so an unwrap names its key (and a BYOK key later).
- :class:`LocalKeyWrapper`: AES-256-GCM under a keyring of local KEKs, context as AAD. Tests and local runs only;
  settings refuse it elsewhere.

Errors: a refusal about *this* blob or context (``InvalidCiphertextException``, ``IncorrectKeyException``,
``AccessDeniedException``, a bad local tag) raises :class:`~app.vault.errors.UnwrapDenied` (audited ``denied`` by the
caller); anything else (throttling, network, a disabled key) raises :class:`~app.vault.errors.KeyUnavailable` (503).
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.vault.cipher import DEK_BYTES, NONCE_BYTES
from app.vault.errors import KeyUnavailable, UnwrapDenied

Context = Mapping[str, str]

# KMS error codes that mean "this blob / context / key is not yours": audited as a denial.
KMS_DENIED_CODES = frozenset(
    {"InvalidCiphertextException", "IncorrectKeyException", "AccessDeniedException", "InvalidKeyUsageException"}
)
KMS_REGION_ENV = "AWS_REGION"


def canonical_context(context: Context) -> bytes:
    if set(context) != {"tenant_id", "purpose"}:
        raise ValueError("the encryption context is exactly {tenant_id, purpose}")
    return json.dumps(dict(context), sort_keys=True, separators=(",", ":")).encode("utf-8")


class KeyWrapper(Protocol):
    provider: str

    async def generate(self, context: Context) -> tuple[bytes, bytes, str]:
        """A new DEK: ``(plaintext, wrapped, kek_id)``."""
        ...

    async def wrap(self, plaintext: bytes, context: Context) -> tuple[bytes, str]:
        """Wrap an existing DEK (rewrap): ``(wrapped, kek_id)``."""
        ...

    async def unwrap(self, wrapped: bytes, context: Context, kek_id: str) -> bytes: ...

    async def self_test(self) -> None:
        """Generate and unwrap one throwaway DEK under the nil tenant (``python -m app.vault.selftest``)."""
        ...


class LocalKeyWrapper:
    provider = "local"

    def __init__(self, keyring: Mapping[str, bytes], active: str) -> None:
        if active not in keyring:
            raise ValueError("the active KEK must be in the keyring")
        for kek in keyring.values():
            if len(kek) != DEK_BYTES:
                raise ValueError("a local KEK is 32 bytes")
        self._keyring = dict(keyring)
        self.active = active

    @classmethod
    def ephemeral(cls) -> LocalKeyWrapper:
        return cls({"local-ephemeral": os.urandom(DEK_BYTES)}, "local-ephemeral")

    def with_kek(self, kek_id: str, kek: bytes) -> LocalKeyWrapper:
        """A wrapper whose active KEK is ``kek_id`` and which still knows the old ones (local rotation)."""
        return LocalKeyWrapper({**self._keyring, kek_id: kek}, kek_id)

    def without(self, kek_id: str) -> LocalKeyWrapper:
        keyring = {k: v for k, v in self._keyring.items() if k != kek_id}
        return LocalKeyWrapper(keyring, self.active)

    async def generate(self, context: Context) -> tuple[bytes, bytes, str]:
        plaintext = os.urandom(DEK_BYTES)
        wrapped, kek_id = await self.wrap(plaintext, context)
        return plaintext, wrapped, kek_id

    async def wrap(self, plaintext: bytes, context: Context) -> tuple[bytes, str]:
        nonce = os.urandom(NONCE_BYTES)
        sealed = AESGCM(self._keyring[self.active]).encrypt(nonce, plaintext, canonical_context(context))
        return nonce + sealed, self.active

    async def unwrap(self, wrapped: bytes, context: Context, kek_id: str) -> bytes:
        kek = self._keyring.get(kek_id)
        if kek is None:
            raise KeyUnavailable("kek_unknown")
        try:
            return AESGCM(kek).decrypt(wrapped[:NONCE_BYTES], wrapped[NONCE_BYTES:], canonical_context(context))
        except InvalidTag:
            raise UnwrapDenied("unwrap_refused") from None

    async def self_test(self) -> None:
        context = {"tenant_id": "00000000-0000-0000-0000-000000000000", "purpose": "vault"}
        plaintext, wrapped, kek_id = await self.generate(context)
        if await self.unwrap(wrapped, context, kek_id) != plaintext:
            raise KeyUnavailable("self_test_mismatch")


def default_kms_client() -> Any:
    import boto3  # lazy: only the KMS path needs it
    from botocore.config import Config

    config = Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 3, "mode": "standard"})
    return boto3.client("kms", region_name=os.environ.get(KMS_REGION_ENV) or None, config=config)


class KmsKeyWrapper:
    provider = "kms"

    def __init__(self, key_id: str, client_factory: Callable[[], Any] = default_kms_client) -> None:
        if not key_id:
            raise ValueError("a KMS key id or alias is required")
        self.key_id = key_id
        self._client_factory = client_factory
        self._client: Any = None

    def _kms(self) -> Any:
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    async def _call(self, method: str, **kwargs: Any) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            return getattr(self._kms(), method)(**kwargs)

        try:
            return await asyncio.to_thread(run)
        except Exception as exc:  # noqa: BLE001 — classified below; the message is never echoed
            code = _kms_error_code(exc)
            if code in KMS_DENIED_CODES:
                raise UnwrapDenied(code) from None
            raise KeyUnavailable(code or type(exc).__name__) from None

    async def generate(self, context: Context) -> tuple[bytes, bytes, str]:
        out = await self._call(
            "generate_data_key", KeyId=self.key_id, KeySpec="AES_256", EncryptionContext=dict(context)
        )
        return out["Plaintext"], out["CiphertextBlob"], out["KeyId"]

    async def wrap(self, plaintext: bytes, context: Context) -> tuple[bytes, str]:
        out = await self._call("encrypt", KeyId=self.key_id, Plaintext=plaintext, EncryptionContext=dict(context))
        return out["CiphertextBlob"], out["KeyId"]

    async def unwrap(self, wrapped: bytes, context: Context, kek_id: str) -> bytes:
        out = await self._call("decrypt", CiphertextBlob=wrapped, EncryptionContext=dict(context), KeyId=kek_id)
        plaintext = out["Plaintext"]
        if len(plaintext) != DEK_BYTES:
            raise UnwrapDenied("not_a_dek")
        return plaintext

    async def self_test(self) -> None:
        context = {"tenant_id": "00000000-0000-0000-0000-000000000000", "purpose": "vault"}
        plaintext, wrapped, kek_id = await self.generate(context)
        if await self.unwrap(wrapped, context, kek_id) != plaintext:
            raise KeyUnavailable("self_test_mismatch")


def _kms_error_code(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return str(response.get("Error", {}).get("Code") or "")
    return ""
