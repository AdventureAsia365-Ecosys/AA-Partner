"""The platform seal of a signed envelope (AA-955, card CRM-25; design v3 §4.1 ``alias/crm-<env>/seal``, §5.7).

Provider-neutral: a :class:`Sealer` signs the SHA-256 of the rendered envelope and verifies it again.

- :class:`KmsSealer` (``KMS_SEAL_KEY_ID``, the asymmetric RSA key AA-CIS-Infra ``crm_data.tf`` creates):
  ``kms:Sign`` with ``RSASSA_PSS_SHA_256``, ``MessageType = DIGEST``; the private key never leaves KMS. Verify with
  ``kms:Verify`` (or the key's public key, offline). Algorithm code ``kms-rsassa-pss-sha256``.
- :class:`DevStubSealer`: HMAC-SHA256 with a fixed, public key (``dev-stub-hmac-sha256``). It proves nothing to a third
  party; it exists so dev / test / local sign end to end without the seal key. Refused on stg / prod
  (:func:`build_sealer` answers None there, and signing is 503 ``esign_not_configured``).

A seal is not a PAdES signature yet: the envelope is canonical UTF-8 text, not a PDF. PAdES (a PDF with the CMS
signature embedded) is the follow-up the implementation notes describe; the same key signs it.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from app.errors import Unavailable
from app.settings import DEV_ONLY_ENVIRONMENTS, Settings
from app.vault.wrapping import default_kms_client

KMS_ALG = "kms-rsassa-pss-sha256"
STUB_ALG = "dev-stub-hmac-sha256"
STUB_KEY_REF = "dev-stub"
# Not a secret: the stub is a placeholder for the seal on environments without the seal key.
_STUB_KEY = b"aa-crm-dev-stub-seal-not-a-legal-seal"


@dataclass(frozen=True)
class Seal:
    alg: str
    key_ref: str
    value: bytes


class Sealer(Protocol):
    alg: str

    async def seal(self, digest: bytes) -> Seal: ...

    async def verify(self, digest: bytes, seal: Seal) -> bool: ...


class DevStubSealer:
    alg = STUB_ALG

    async def seal(self, digest: bytes) -> Seal:
        _digest(digest)
        return Seal(STUB_ALG, STUB_KEY_REF, hmac.new(_STUB_KEY, digest, hashlib.sha256).digest())

    async def verify(self, digest: bytes, seal: Seal) -> bool:
        if seal.alg != STUB_ALG:
            return False
        return hmac.compare_digest(hmac.new(_STUB_KEY, _digest(digest), hashlib.sha256).digest(), seal.value)


class KmsSealer:
    alg = KMS_ALG
    signing_algorithm = "RSASSA_PSS_SHA_256"

    def __init__(self, key_id: str, client_factory: Callable[[], Any] = default_kms_client) -> None:
        if not key_id:
            raise ValueError("a KMS key id or alias is required")
        self.key_id = key_id
        self._client_factory = client_factory
        self._client: Any = None
        self._lock = threading.Lock()

    def _kms(self) -> Any:
        if self._client is None:
            with self._lock:
                if self._client is None:
                    self._client = self._client_factory()
        return self._client

    async def _call(self, method: str, **kwargs: Any) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            return getattr(self._kms(), method)(**kwargs)

        try:
            return await asyncio.to_thread(run)
        except Exception as exc:  # noqa: BLE001 — the message is never echoed; the signer retries later
            raise Unavailable("seal_unavailable", "the document could not be sealed; try again shortly") from exc

    async def seal(self, digest: bytes) -> Seal:
        out = await self._call(
            "sign",
            KeyId=self.key_id,
            Message=_digest(digest),
            MessageType="DIGEST",
            SigningAlgorithm=self.signing_algorithm,
        )
        # KeyId in the answer is the key ARN: the seal names the exact key that signed (an alias can move).
        return Seal(KMS_ALG, str(out.get("KeyId") or self.key_id), bytes(out["Signature"]))

    async def verify(self, digest: bytes, seal: Seal) -> bool:
        if seal.alg != KMS_ALG:
            return False
        out = await self._call(
            "verify",
            KeyId=seal.key_ref,
            Message=_digest(digest),
            MessageType="DIGEST",
            Signature=seal.value,
            SigningAlgorithm=self.signing_algorithm,
        )
        return bool(out.get("SignatureValid"))


def _digest(digest: bytes) -> bytes:
    if len(digest) != 32:
        raise ValueError("a seal signs a SHA-256 digest (32 bytes)")
    return digest


def build_sealer(settings: Settings) -> Sealer | None:
    """The KMS sealer when ``KMS_SEAL_KEY_ID`` is set; the dev stub on dev / test / local; else None."""
    if settings.kms_seal_key_id:
        return KmsSealer(settings.kms_seal_key_id)
    if settings.environment in DEV_ONLY_ENVIRONMENTS:
        return DevStubSealer()
    return None


def require_sealer(sealer: Sealer | None) -> Sealer:
    if sealer is None:
        raise Unavailable("esign_not_configured", "signing is not available right now")
    return sealer


async def verify_any(sealer: Sealer | None, digest: bytes, seal: Seal) -> bool | None:
    """True / False when the seal's algorithm can be checked here; None when it cannot (a KMS seal on an environment
    without the seal key, or KMS not answering: the reveal still opens, the digest was checked before decrypting)."""
    if seal.alg == STUB_ALG:
        return await DevStubSealer().verify(digest, seal)
    if isinstance(sealer, KmsSealer):
        try:
            return await sealer.verify(digest, seal)
        except Unavailable:
            return None
    return None
