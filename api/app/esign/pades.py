"""PAdES-B-B signatures on the PDF of a signed envelope (AA-1012, card CRM-25b; design v3 §5.7 "e-sign v1 with PAdES via
``seal``").

- :class:`PadesSealer` signs a PDF with pyHanko: ``/ETSI.CAdES.detached`` (PAdES), SHA-256, no timestamp (the B-B
  level), an invisible signature field ``PlatformSeal``, and the given files embedded first (the sealed envelope).
- The CMS signature value comes from KMS: the pyHanko signer's ``async_sign_raw`` hashes the signed attributes and calls
  :meth:`app.esign.seal.KmsSealer.seal` (``kms:Sign``, ``RSASSA_PSS_SHA_256``, ``MessageType = DIGEST``), the same call
  and IAM conditions as the AA-955 seal. **The private key never leaves KMS**; the app holds only the certificate chain.
  The CMS mechanism is ``rsassa_pss`` with SHA-256, MGF1-SHA-256 and a 32-byte salt (KMS's PSS parameters).
- The certificate chain of the seal key (:mod:`app.esign.certificate`) is the public file
  ``app/esign/certs/seal-chain-<environment>.pem``. Before the first signature of a process the leaf's public key is
  compared with ``kms:GetPublicKey`` of the seal key (503 ``pdf_seal_mismatch`` when they differ: a rotated key with an
  old certificate never signs).
- Not configured (no KMS seal key, the HMAC stub environments; or no chain file for the environment):
  :func:`build_pades` answers None and a PDF request is 503 ``pdf_seal_not_configured``.

pyHanko is imported on the first signature only, so the API start does not load it.
"""

from __future__ import annotations

import hashlib
import io
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

from app.errors import Unavailable
from app.esign.certificate import CertificateError, Chain, load_pem
from app.esign.seal import KmsSealer, Sealer
from app.settings import Settings

log = logging.getLogger(__name__)

CERT_DIR = Path(__file__).with_name("certs")
FIELD_NAME = "PlatformSeal"
DIGEST = "sha256"
PSS_SALT_BYTES = 32  # KMS RSASSA_PSS_SHA_256 uses a salt as long as the digest


@dataclass(frozen=True)
class Attachment:
    """A file embedded in the PDF before it is signed (so the signature covers it)."""

    name: str
    data: bytes
    mime_type: str
    description: str


def chain_path(environment: str) -> Path:
    return CERT_DIR / f"seal-chain-{environment}.pem"


def load_chain(environment: str, *, directory: Path | None = None) -> Chain | None:
    """The environment's chain, None when there is no file or it cannot be used (logged; PDFs are then 503)."""
    path = (directory or CERT_DIR) / f"seal-chain-{environment}.pem"
    if not path.is_file():
        return None
    try:
        return load_pem(path.read_bytes())
    except CertificateError as exc:
        log.error("pades_chain_unusable", extra={"reason": str(exc)})
        return None


@dataclass
class PadesSealer:
    sealer: KmsSealer
    chain: Chain
    _checked: bool = field(default=False, init=False)

    async def check_key(self) -> None:
        """The leaf certificate holds the seal key's public key (checked until it matched once in this process; two
        first requests at once both ask KMS, which is harmless)."""
        if self._checked:
            return
        if await self.sealer.public_key() != self.chain.leaf_public_key():
            log.error("pades_chain_key_mismatch")
            raise Unavailable("pdf_seal_mismatch", "signed PDFs are not available right now")
        self._checked = True

    async def sign(self, pdf: bytes, *, attachments: Sequence[Attachment] = (), reason: str) -> bytes:
        await self.check_key()
        return await sign_pdf(pdf, _cms_signer(self.sealer, self.chain), attachments=attachments, reason=reason)


def build_pades(settings: Settings, sealer: Sealer | None) -> PadesSealer | None:
    if not isinstance(sealer, KmsSealer):
        return None
    chain = load_chain(settings.environment)
    return PadesSealer(sealer, chain) if chain is not None else None


def require_pades(pades: PadesSealer | None) -> PadesSealer:
    if pades is None:
        raise Unavailable("pdf_seal_not_configured", "signed PDFs are not available right now")
    return pades


@cache
def _signer_class() -> type:
    from asn1crypto import algos
    from pyhanko.sign import signers
    from pyhanko.sign.general import SigningError

    mechanism = algos.SignedDigestAlgorithm(
        {
            "algorithm": "rsassa_pss",
            "parameters": algos.RSASSAPSSParams(
                {
                    "hash_algorithm": {"algorithm": DIGEST},
                    "mask_gen_algorithm": {"algorithm": "mgf1", "parameters": {"algorithm": DIGEST}},
                    "salt_length": PSS_SALT_BYTES,
                    "trailer_field": "trailer_field_bc",
                }
            ),
        }
    )

    class KmsCmsSigner(signers.Signer):  # type: ignore[misc]
        """A pyHanko signer whose raw signature is ``kms:Sign`` over the SHA-256 of the signed attributes."""

        def __init__(self, sealer: Sealer, chain: Chain) -> None:
            from asn1crypto import x509 as asn1_x509
            from pyhanko_certvalidator.registry import SimpleCertificateStore

            super().__init__(
                signing_cert=asn1_x509.Certificate.load(chain.leaf),
                cert_registry=SimpleCertificateStore.from_certs([asn1_x509.Certificate.load(c) for c in chain.issuers]),
                signature_mechanism=mechanism,
            )
            self._sealer = sealer
            self._size = chain.key_bits() // 8

        async def async_sign_raw(self, data: bytes, digest_algorithm: str, dry_run: bool = False) -> bytes:
            if digest_algorithm != DIGEST:
                raise SigningError("the seal signs SHA-256 digests only")
            if dry_run:
                return bytes(self._size)
            return (await self._sealer.seal(hashlib.sha256(data).digest())).value

    return KmsCmsSigner


def _cms_signer(sealer: Sealer, chain: Chain) -> Any:
    return _signer_class()(sealer, chain)


async def sign_pdf(pdf: bytes, signer: Any, *, attachments: Sequence[Attachment] = (), reason: str) -> bytes:
    """``pdf`` with ``attachments`` embedded and a PAdES-B-B signature by ``signer`` (a pyHanko signer), as one
    incremental update."""
    from pyhanko.pdf_utils import embed
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.sign import signers
    from pyhanko.sign.fields import SigSeedSubFilter

    writer = IncrementalPdfFileWriter(io.BytesIO(pdf))
    for item in attachments:
        data = embed.EmbeddedFileObject.from_file_data(writer, data=item.data, mime_type=item.mime_type)
        embed.embed_file(
            writer, embed.FileSpec(file_spec_string=item.name, embedded_data=data, description=item.description)
        )
    meta = signers.PdfSignatureMetadata(
        field_name=FIELD_NAME, md_algorithm=DIGEST, subfilter=SigSeedSubFilter.PADES, reason=reason
    )
    out = await signers.async_sign_pdf(writer, meta, signer=signer)
    return bytes(out.getvalue())
