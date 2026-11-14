"""The X.509 certificate of the seal key (AA-1012, card CRM-25b): what a PDF reader shows as the signer of a PAdES PDF.

The seal key is an RSA_3072 KMS key (``alias/crm-<env>/seal``, AA-932); its private key never leaves KMS, so a
certificate for it can only be issued by someone who signs with another key. On Dev that is a **self-issued company
CA** (:func:`issue_chain`): a fresh RSA key made in memory signs a CA certificate and a leaf certificate holding the
seal key's public key, then the CA key is dropped (it is never returned, written or logged). The chain (leaf, then CA;
PEM; public material only) is what the app reads (:func:`load_pem`, ``app/esign/certs/seal-chain-<environment>.crt``,
committed by the orchestrator after running ``api/scripts/esign_seal_certificate.py``). Re-issuing makes a new CA;
who issues the prod certificate is decided at go-live.

The leaf: not a CA, key usage digital signature + non-repudiation (content commitment), no extended key usage (PDF
readers accept a signing certificate without one), subject and authority key identifiers.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

ORGANIZATION = "Adventure Asia"
CA_KEY_BITS = 3072
DER = serialization.Encoding.DER
SPKI = serialization.PublicFormat.SubjectPublicKeyInfo


class CertificateError(ValueError):
    """A certificate chain the seal cannot use."""


_PEM_LABEL = re.compile(rb"-----BEGIN ([A-Z0-9 ]+)-----")
_ACCOUNT_LIKE = re.compile(r"arn:aws|(?<!\d)\d{12}(?!\d)")


@dataclass(frozen=True)
class Chain:
    """The signing certificate (``leaf``) and the certificates above it, DER."""

    leaf: bytes
    issuers: tuple[bytes, ...]

    def leaf_public_key(self) -> bytes:
        """The leaf's public key as DER SubjectPublicKeyInfo (the shape ``kms:GetPublicKey`` answers)."""
        return x509.load_der_x509_certificate(self.leaf).public_key().public_bytes(DER, SPKI)

    def key_bits(self) -> int:
        key = x509.load_der_x509_certificate(self.leaf).public_key()
        return key.key_size if isinstance(key, rsa.RSAPublicKey) else 0


def fingerprint(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


def load_pem(data: bytes) -> Chain:
    """The chain of a PEM file: the leaf first, an RSA key that is not a CA, then its issuers. Certificates only: a
    file that also holds a key (or any other PEM block) is refused, so a private key is never read as a chain."""
    labels = set(_PEM_LABEL.findall(data))
    if labels - {b"CERTIFICATE"}:
        raise CertificateError("a chain file holds certificates only")
    try:
        certs = x509.load_pem_x509_certificates(data)
    except ValueError:
        raise CertificateError("not a PEM certificate chain") from None
    leaf = certs[0]
    if not isinstance(leaf.public_key(), rsa.RSAPublicKey):
        raise CertificateError("the signing certificate must hold an RSA key")
    try:
        if leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
            raise CertificateError("the first certificate must be the signing certificate, not a CA")
    except x509.ExtensionNotFound:
        pass
    return Chain(leaf.public_bytes(DER), tuple(c.public_bytes(DER) for c in certs[1:]))


def public_problems(data: bytes) -> list[str]:
    """Why a chain file must not be committed to the public repo (empty = fine): only CERTIFICATE blocks, and no AWS
    account ID or ARN in any subject or issuer name (the names are built from the organization and environment)."""
    try:
        chain = load_pem(data)
    except CertificateError as exc:
        return [str(exc)]
    problems = []
    for der in (chain.leaf, *chain.issuers):
        cert = x509.load_der_x509_certificate(der)
        for name in (cert.subject.rfc4514_string(), cert.issuer.rfc4514_string()):
            if _ACCOUNT_LIKE.search(name):
                problems.append(f"an account ID or ARN in {name!r}")
    return problems


def _name(common: str, environment: str, organization: str) -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, organization),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, f"Partner CRM ({environment})"),
            x509.NameAttribute(NameOID.COMMON_NAME, common),
        ]
    )


def issue_chain(
    public_key_der: bytes,
    *,
    environment: str,
    now: dt.datetime,
    ca_days: int,
    leaf_days: int,
    organization: str = ORGANIZATION,
) -> bytes:
    """A PEM chain (leaf, CA) for ``public_key_der`` (the seal key's SubjectPublicKeyInfo), issued by a fresh
    in-memory CA whose key is discarded when this returns."""
    if leaf_days < 1 or ca_days < leaf_days:
        raise CertificateError("the CA must outlive the leaf, and both must last at least a day")
    try:
        subject_key = serialization.load_der_public_key(public_key_der)
    except ValueError:
        raise CertificateError("not a DER public key") from None
    if not isinstance(subject_key, rsa.RSAPublicKey):
        raise CertificateError("the seal key must be an RSA key")
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=CA_KEY_BITS)
    ca_name = _name(f"{organization} e-sign CA ({environment})", environment, organization)
    start = now - dt.timedelta(minutes=5)
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(now + dt.timedelta(days=ca_days))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )  # fmt: skip
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    leaf = (
        x509.CertificateBuilder()
        .subject_name(_name(f"{organization} platform seal ({environment})", environment, organization))
        .issuer_name(ca_name)
        .public_key(subject_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(now + dt.timedelta(days=leaf_days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=True,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )  # fmt: skip
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(subject_key), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    del ca_key
    return leaf.public_bytes(serialization.Encoding.PEM) + ca.public_bytes(serialization.Encoding.PEM)
