"""AA-1012 (CRM-25b) without a database or AWS: the seal key's certificate chain, the envelope parser, the signed
agreement PDF (Vietnamese glyphs embedded, no missing-glyph box), its PAdES-B-B signature made through a KMS client
stand-in (a local RSA key answers ``Sign`` the way KMS does: RSASSA-PSS SHA-256 over a digest) and validated with
pyHanko against the issued CA, the service and portal downloads, the routes and the certificate script. Invented names
and places only."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import io
import json
import re
import uuid
import zlib
from pathlib import Path
from typing import Any

import pytest
from asn1crypto import x509 as asn1_x509
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa, utils
from fastapi.testclient import TestClient
from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.sign.general import SigningError
from pyhanko.sign.validation import async_validate_pdf_signature
from pyhanko_certvalidator import ValidationContext

from app.documents import pdf, renders
from app.errors import Forbidden, Invalid, NotFound, Unavailable
from app.esign import certificate, pades, service
from app.esign import envelope as env
from app.esign.seal import KMS_ALG, DevStubSealer, KmsSealer
from app.main import create_app
from app.portal import public as portal
from app.routers import client_pages
from app.routers import esign as esign_router
from app.settings import Settings
from scripts import esign_seal_certificate as script
from tests.tokens import LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import TENANT, FakeVault, fake_tx, tenant_ctx
from tests.unit.test_lifecycle_services import ACCESS, LEAD, OTHER, TOKEN, TRIP, _portal, _raises, _signed_in

REQUEST = uuid.UUID("00000000-0000-4000-8000-000000001012")
EVENT = uuid.UUID("00000000-0000-4000-8000-000000001013")
CHILD = uuid.UUID("00000000-0000-4000-8000-000000001014")
SIGNED_AT = dt.datetime(2031, 6, 2, 8, 30, tzinfo=dt.UTC)
VI_SAMPLE = "Tôi, Nguyễn Thị Hằng, đồng ý: chuyến đi Vịnh Mây Ngọc, đặt cọc, miễn trừ trách nhiệm ở Đảo Sương."
VI_CHARS = "ăâđêôơưằắẳẵặầấẩẫậèéẻẽẹềếểễệìíỉĩịòóỏõọồốổỗộờớởỡợùúủũụừứửữựỳýỷỹỵĂÂĐÊÔƠƯỆỘỮ"


# ── a KMS stand-in: a local RSA key that never leaves this object ────────────────────────────────────────────
class SoftKms:
    def __init__(self, key: rsa.RSAPrivateKey, public: bytes | None = None, invalid: bool = False) -> None:
        self._key = key
        self.public = public or key.public_key().public_bytes(certificate.DER, certificate.SPKI)
        self.invalid = invalid
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_public_key(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("get_public_key", kw))
        return {"KeyId": "test-key-arn", "PublicKey": self.public}

    def sign(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("sign", kw))
        assert kw["MessageType"] == "DIGEST" and kw["SigningAlgorithm"] == "RSASSA_PSS_SHA_256"
        assert len(kw["Message"]) == 32
        pss = padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32)
        return {
            "KeyId": "test-key-arn",
            "Signature": self._key.sign(kw["Message"], pss, utils.Prehashed(hashes.SHA256())),
        }

    def verify(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("verify", kw))
        if self.invalid:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "KMSInvalidSignatureException", "Message": "fake"}}, "Verify")
        pss = padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32)
        try:
            self._key.public_key().verify(kw["Signature"], kw["Message"], pss, utils.Prehashed(hashes.SHA256()))
        except InvalidSignature:
            return {"SignatureValid": False}
        return {"SignatureValid": True}


@pytest.fixture(scope="module")
def seal_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=3072)


@pytest.fixture(scope="module")
def chain_pem(seal_key: rsa.RSAPrivateKey) -> bytes:
    spki = seal_key.public_key().public_bytes(certificate.DER, certificate.SPKI)
    return certificate.issue_chain(spki, environment="test", now=dt.datetime.now(dt.UTC), ca_days=30, leaf_days=10)


def _pades(seal_key: rsa.RSAPrivateKey, chain_pem: bytes, **kms: Any) -> tuple[pades.PadesSealer, SoftKms]:
    fake = SoftKms(seal_key, **kms)
    return pades.PadesSealer(
        KmsSealer("alias/crm-test/seal", client_factory=lambda: fake), certificate.load_pem(chain_pem)
    ), fake


def _envelope() -> bytes:
    docs = [
        env.Document("booking_terms", "t1", "t-2031", "Booking terms of {{agency.name}}.", b"\x02" * 32),
        env.Document("waiver", "w1", "w-2031", VI_SAMPLE + "\n\nĐiều 2: tự chịu rủi ro.", b"\x01" * 32),
    ]
    text, _record, _digest = env.build(
        agency_name="Công ty Lữ Hành Sen Mây",
        trip={"title": "Vịnh Mây Ngọc 5 ngày", "start_date": "2031-06-10", "end_date": "2031-06-14"},
        documents=docs,
        values={"agency.name": "Công ty Lữ Hành Sen Mây", "signer.name": "Nguyễn Thị Hằng"},
        audit={"traveler_id": str(LEAD), "traveller_role": "lead", "signer_role": "self", "signed_at": "2031-06-02"},
    )
    return text


def _view(text: bytes, alg: str = KMS_ALG) -> dict[str, Any]:
    return {"agency_name": "Công ty Lữ Hành Sen Mây", "envelope_text": text.decode(),
            "signed_at": SIGNED_AT.isoformat(), "envelope_sha256": hashlib.sha256(text).hexdigest(), "seal_alg": alg,
            "seal_verified": True}  # fmt: skip


def _streams(data: bytes) -> list[bytes]:
    out = []
    for m in re.finditer(rb"/FlateDecode[^>]*>>\nstream\n", data):
        start = m.end()
        end = data.index(b"\nendstream", start)
        try:
            out.append(zlib.decompress(data[start:end]))
        except zlib.error:
            continue
    return out


def _ca(chain_pem: bytes) -> asn1_x509.Certificate:
    return asn1_x509.Certificate.load(certificate.load_pem(chain_pem).issuers[0])


# ── certificate ──────────────────────────────────────────────────────────────────────────────────────────────
def test_the_issued_chain(seal_key: rsa.RSAPrivateKey, chain_pem: bytes) -> None:
    chain = certificate.load_pem(chain_pem)
    leaf, ca = (x509.load_der_x509_certificate(d) for d in (chain.leaf, chain.issuers[0]))
    assert chain.leaf_public_key() == seal_key.public_key().public_bytes(certificate.DER, certificate.SPKI)
    assert chain.key_bits() == 3072 and leaf.issuer == ca.subject and b"PRIVATE KEY" not in chain_pem
    assert not leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    usage = leaf.extensions.get_extension_for_class(x509.KeyUsage).value
    assert usage.digital_signature and usage.content_commitment and not usage.key_cert_sign
    assert ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert "platform seal (test)" in leaf.subject.rfc4514_string()
    leaf_until = leaf.not_valid_after_utc
    assert leaf_until < ca.not_valid_after_utc
    assert len(certificate.fingerprint(chain.leaf)) == 64


def test_unusable_chains_and_keys(seal_key: rsa.RSAPrivateKey, chain_pem: bytes) -> None:
    with pytest.raises(certificate.CertificateError):
        certificate.load_pem(b"not a certificate")
    ca_first = chain_pem[chain_pem.index(b"-----BEGIN CERTIFICATE-----", 10) :]
    with pytest.raises(certificate.CertificateError, match="not a CA"):
        certificate.load_pem(ca_first)
    spki = seal_key.public_key().public_bytes(certificate.DER, certificate.SPKI)
    now = dt.datetime.now(dt.UTC)
    with pytest.raises(certificate.CertificateError):
        certificate.issue_chain(spki, environment="test", now=now, ca_days=5, leaf_days=10)
    with pytest.raises(certificate.CertificateError):
        certificate.issue_chain(b"junk", environment="test", now=now, ca_days=5, leaf_days=1)
    ec_spki = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(certificate.DER, certificate.SPKI)
    with pytest.raises(certificate.CertificateError, match="RSA"):
        certificate.issue_chain(ec_spki, environment="test", now=now, ca_days=5, leaf_days=1)
    # A self-signed EC certificate first: the seal key is RSA, so the chain is refused.
    ec_key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "Example")])
    ec_cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(ec_key.public_key())
               .serial_number(1).not_valid_before(now).not_valid_after(now + dt.timedelta(days=1))
               .sign(ec_key, hashes.SHA256()))  # fmt: skip
    with pytest.raises(certificate.CertificateError, match="RSA"):
        certificate.load_pem(ec_cert.public_bytes(serialization.Encoding.PEM))
    # No BasicConstraints at all: still a usable signing certificate.
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    plain = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(rsa_key.public_key())
             .serial_number(2).not_valid_before(now).not_valid_after(now + dt.timedelta(days=1))
             .sign(rsa_key, hashes.SHA256()))  # fmt: skip
    assert certificate.load_pem(plain.public_bytes(serialization.Encoding.PEM)).issuers == ()


# ── envelope parser and the printed agreement ────────────────────────────────────────────────────────────────
def test_the_envelope_parses_back() -> None:
    parsed = env.parse(_envelope().decode())
    assert parsed is not None
    assert parsed.header[0].startswith("Agency: Công ty") and len(parsed.documents) == 2
    assert parsed.documents[1].title == "Traveller waiver" and parsed.documents[1].version.startswith("w-2031")
    assert (
        parsed.documents[1].text.startswith(VI_SAMPLE)
        and parsed.audit[0] == "Signed for: traveller " + str(LEAD) + " (lead)"
    )
    # Not the envelope format, a rule line inside an agreement text, a broken document header: not parsed.
    assert env.parse("plain text") is None
    ruled = _envelope().decode().replace("Điều 2", env.RULE + "\nĐiều 2", 1)
    assert env.parse(ruled) is None
    assert env.parse(_envelope().decode().replace("Version: t-2031", "Edition: t-2031")) is None
    assert env.parse(_envelope().decode().replace("Documents: 2", "Documents: two")) is None


def test_vietnamese_glyphs_are_in_the_embedded_font() -> None:
    font = pdf.bundled_font()
    assert all(font.glyph(c) != 0 for c in VI_CHARS + VI_SAMPLE), [c for c in VI_CHARS if font.glyph(c) == 0]


@pytest.mark.parametrize("locale", ["en", "vi"])
def test_the_signed_agreement_pdf_prints_every_glyph(locale: str) -> None:
    text = _envelope()
    data = renders.signed_agreement(_view(text), locale)
    assert data.startswith(b"%PDF-1.7") and b"/FontFile2" in data
    streams = _streams(data)
    cmap = next(s for s in streams if b"beginbfchar" in s).decode()
    for char in set(VI_SAMPLE.replace(" ", "")):
        assert f"<{char.encode('utf-16-be').hex().upper()}>" in cmap, char
    shown = [m for s in streams if b" Tj ET" in s for m in re.findall(rb"<([0-9A-F]+)> Tj", s)]
    gids = [g for h in shown for g in re.findall(rb"....", h)]
    assert gids and b"0000" not in gids  # no character fell back to the missing-glyph box
    title = "Thoả thuận đã ký" if locale == "vi" else "Signed agreement"
    assert f"/Title {pdf._pdf_text(title)}".encode() in data


def test_an_unparsable_envelope_is_printed_whole() -> None:
    data = renders.signed_agreement(
        {**_view(b"free text"), "seal_alg": "dev-stub-hmac-sha256", "seal_verified": None}, "en"
    )
    assert data.startswith(b"%PDF") and len(data) > 1000


# ── the PAdES signature ───────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_the_pdf_signature_validates_as_pades_b_b(seal_key: rsa.RSAPrivateKey, chain_pem: bytes) -> None:
    sealer, kms = _pades(seal_key, chain_pem)
    text = _envelope()
    unsigned = renders.signed_agreement(_view(text), "vi")
    signed = await sealer.sign(unsigned, attachments=[pades.Attachment("envelope.txt", text, "text/plain", "env")],
                               reason="test")  # fmt: skip
    assert signed.startswith(unsigned)  # one incremental update over the rendered file
    reader = PdfFileReader(io.BytesIO(signed))
    sig = reader.embedded_signatures[0]
    status = await async_validate_pdf_signature(
        sig, ValidationContext(trust_roots=[_ca(chain_pem)], allow_fetching=False, revocation_mode="soft-fail")
    )
    assert status.bottom_line and status.intact and status.valid and status.trusted
    assert status.coverage.name == "ENTIRE_FILE" and status.md_algorithm == "sha256"
    assert status.pkcs7_signature_mechanism == "rsassa_pss"
    # PAdES-B-B: the ETSI sub-filter, the signing certificate attribute, no CMS signing time, no timestamp.
    assert sig.sig_object["/SubFilter"] == "/ETSI.CAdES.detached" and sig.field_name == pades.FIELD_NAME
    attrs = [a["type"].native for a in sig.signer_info["signed_attrs"]]
    assert "signing_certificate_v2" in attrs and "signing_time" not in attrs
    assert not sig.signer_info["unsigned_attrs"]
    # The sealed envelope rides inside the signed file.
    files = reader.root["/Names"]["/EmbeddedFiles"]["/Names"]
    assert files[0] == "envelope.txt" and files[1].get_object()["/EF"]["/F"].get_object().data == text
    # KMS saw a digest only, once for the key check and once to sign; nothing else reached it.
    assert [c[0] for c in kms.calls] == ["get_public_key", "sign"]
    again = await sealer.sign(unsigned, reason="test")
    assert again != signed and [c[0] for c in kms.calls].count("get_public_key") == 1


@pytest.mark.asyncio
async def test_a_tampered_pdf_fails_validation(seal_key: rsa.RSAPrivateKey, chain_pem: bytes) -> None:
    sealer, _ = _pades(seal_key, chain_pem)
    signed = bytearray(await sealer.sign(renders.signed_agreement(_view(_envelope()), "en"), reason="test"))
    at = signed.index(b"/Producer (AA Partner CRM)") + len("/Producer (")
    signed[at] = ord("X")
    sig = PdfFileReader(io.BytesIO(bytes(signed))).embedded_signatures[0]
    status = await async_validate_pdf_signature(
        sig, ValidationContext(trust_roots=[_ca(chain_pem)], allow_fetching=False)
    )
    assert not status.intact and not status.bottom_line


@pytest.mark.asyncio
async def test_a_certificate_for_another_key_never_signs(seal_key: rsa.RSAPrivateKey, chain_pem: bytes) -> None:
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    sealer, kms = _pades(seal_key, chain_pem, public=other.public_key().public_bytes(certificate.DER, certificate.SPKI))
    with _raises(Unavailable, "pdf_seal_mismatch"):
        await sealer.sign(b"%PDF-1.7", reason="test")
    assert [c[0] for c in kms.calls] == ["get_public_key"]


@pytest.mark.asyncio
async def test_the_cms_signer_signs_sha256_only(seal_key: rsa.RSAPrivateKey, chain_pem: bytes) -> None:
    sealer, kms = _pades(seal_key, chain_pem)
    signer = pades._cms_signer(sealer.sealer, sealer.chain)
    assert await signer.async_sign_raw(b"data", "sha256", dry_run=True) == bytes(384)
    with pytest.raises(SigningError):
        await signer.async_sign_raw(b"data", "sha512")
    assert kms.calls == []


def test_which_pades(chain_pem: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dev = Settings(environment="dev", db_ssl="require")
    kms = Settings(environment="dev", db_ssl="require", kms_seal_key_id="alias/crm-dev/seal")
    assert pades.build_pades(dev, DevStubSealer()) is None
    monkeypatch.setattr(pades, "CERT_DIR", tmp_path)
    assert pades.build_pades(kms, KmsSealer("alias/crm-dev/seal")) is None  # no chain file for dev
    (tmp_path / "seal-chain-dev.pem").write_bytes(b"broken")
    assert pades.build_pades(kms, KmsSealer("alias/crm-dev/seal")) is None
    (tmp_path / "seal-chain-dev.pem").write_bytes(chain_pem)
    built = pades.build_pades(kms, KmsSealer("alias/crm-dev/seal"))
    assert isinstance(built, pades.PadesSealer) and pades.require_pades(built) is built
    assert pades.chain_path("dev").name == "seal-chain-dev.pem"
    with _raises(Unavailable, "pdf_seal_not_configured"):
        pades.require_pades(None)


# ── the downloads ─────────────────────────────────────────────────────────────────────────────────────────────
def _event_row(text: bytes, seal: Any, digest: bytes | None = None) -> dict[str, Any]:
    return {"event_id": EVENT, "request_id": REQUEST, "envelope_enc": b"x" * 30,
            "envelope_sha256": digest or hashlib.sha256(text).digest(), "seal_alg": seal.alg,
            "seal_key_ref": seal.key_ref, "seal_value": seal.value, "evidence_sha256": b"\x05" * 32,
            "occurred_at": SIGNED_AT}  # fmt: skip


class EnvelopeVault(FakeVault):
    def __init__(self, content: bytes) -> None:
        super().__init__()
        self.content = content

    async def decrypt_bytes(self, *a: Any, **k: Any) -> bytes:
        return self.content


def _conn(row: dict[str, Any]) -> ScriptedConn:
    conn = ScriptedConn().on("SELECT request_id, trip_id FROM crm.signature_request", {"request_id": REQUEST})
    return (
        conn.on("kind = 'signed'", row)
        .on("SELECT display_name FROM crm.tenant", "Công ty Lữ Hành Sen Mây")
        .on("SELECT default_locale FROM crm.tenant", "vi")
    )


@pytest.mark.asyncio
async def test_the_agency_pdf_download(seal_key: rsa.RSAPrivateKey, chain_pem: bytes) -> None:
    signer, kms = _pades(seal_key, chain_pem)
    sealer = signer.sealer
    text = _envelope()
    seal = await sealer.seal(hashlib.sha256(text).digest())
    with _raises(Invalid, "reason_required"):
        await service.envelope_pdf(tenant_ctx(), FakeVault(), sealer, signer, REQUEST, "curious")  # type: ignore[arg-type]
    with _raises(NotFound, "signature_not_found"):
        await service.envelope_pdf(tenant_ctx(), FakeVault(), sealer, signer, REQUEST, "client_request")  # type: ignore[arg-type]
    ctx = tenant_ctx(_conn(_event_row(text, seal)))
    with _raises(Unavailable, "pdf_seal_not_configured"):
        await service.envelope_pdf(ctx, EnvelopeVault(text), sealer, None, REQUEST, "client_request")  # type: ignore[arg-type]
    out = await service.envelope_pdf(ctx, EnvelopeVault(text), sealer, signer, REQUEST, "client_request")  # type: ignore[arg-type]
    assert out["content_type"] == "application/pdf" and out["filename"] == "signed-agreement-00000000.pdf"
    assert out["seal_verified"] is True and out["envelope_sha256"] == hashlib.sha256(text).hexdigest()
    data = base64.b64decode(out["content_b64"])
    reader = PdfFileReader(io.BytesIO(data))
    files = reader.root["/Names"]["/EmbeddedFiles"]["/Names"]
    seal_json = json.loads(files[3].get_object()["/EF"]["/F"].get_object().data)
    assert seal_json["seal_key_id"] == "test-key-arn" and "arn:" not in json.dumps(seal_json)
    assert base64.b64decode(seal_json["seal_value_b64"]) == seal.value
    status = await async_validate_pdf_signature(reader.embedded_signatures[0],
                                    ValidationContext(trust_roots=[_ca(chain_pem)], allow_fetching=False))  # fmt: skip
    assert status.bottom_line
    assert ("reveal", "allowed") in [(e.action, e.outcome) for e in ctx.audit_events]
    assert ctx.audit_events[-1].fields == ("envelope_pdf",)
    # The seal key id never carries the account part of a key ARN.
    from app.esign.seal import Seal

    assert service._key_id(Seal(KMS_ALG, "arn:aws:kms:region:000000000000:key/abc-123", b"s")) == "abc-123"
    assert service._key_id(Seal("dev-stub-hmac-sha256", "dev-stub", b"s")) == "dev-stub"


@pytest.mark.asyncio
async def test_a_broken_envelope_or_seal_gives_no_pdf(seal_key: rsa.RSAPrivateKey, chain_pem: bytes) -> None:
    signer, _ = _pades(seal_key, chain_pem)
    text = _envelope()
    seal = await signer.sealer.seal(hashlib.sha256(text).digest())
    ctx = tenant_ctx(_conn(_event_row(text, seal, digest=b"\x00" * 32)))
    with _raises(Unavailable, "document_integrity"):
        await service.envelope_pdf(ctx, EnvelopeVault(text), signer.sealer, signer, REQUEST, "client_request")  # type: ignore[arg-type]
    rejecting, _ = _pades(seal_key, chain_pem, invalid=True)
    ctx = tenant_ctx(_conn(_event_row(text, seal)))
    with _raises(Unavailable, "document_integrity"):
        await service.envelope_pdf(ctx, EnvelopeVault(text), rejecting.sealer, rejecting, REQUEST, "client_request")  # type: ignore[arg-type]
    assert [(e.action, e.outcome, e.reason) for e in ctx.audit_events] == [("reveal", "denied", "seal_rejected")]
    ctx = tenant_ctx(_conn(_event_row(text, seal)).on("kind = 'signed'", None))
    with _raises(NotFound, "envelope_not_found"):
        await service.envelope_pdf(ctx, EnvelopeVault(text), signer.sealer, signer, REQUEST, "client_request")  # type: ignore[arg-type]


async def _portal_pdf(monkeypatch: pytest.MonkeyPatch, row: Any, traveler: Any = LEAD) -> tuple[Any, Any]:
    conn = _signed_in(ScriptedConn()).on("ORDER BY signed_at DESC LIMIT 1", row)
    ctx = _portal(monkeypatch, conn)
    calls: list[Any] = []

    async def signed_pdf(*args: Any, **kw: Any) -> dict[str, Any]:
        calls.append((args[4], kw))
        return {"filename": "x.pdf"}

    monkeypatch.setattr(service, "signed_pdf", signed_pdf)
    out = await portal.agreement_pdf(object(), object(), TOKEN, "s", traveler, sealer=None, pades=None)  # type: ignore[arg-type]
    return (out, calls, ctx)  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_the_traveller_downloads_only_their_own_or_their_minors(monkeypatch: pytest.MonkeyPatch) -> None:
    with _raises(NotFound, "signature_not_found"):
        await portal.agreement_pdf(object(), object(), TOKEN, "s", "not-an-id", sealer=None, pades=None)  # type: ignore[arg-type]
    with _raises(NotFound, "signature_not_found"):
        await _portal_pdf(monkeypatch, None)
    own = {"request_id": REQUEST, "traveler_id": LEAD, "signer_traveler_id": None}
    out, calls, ctx = await _portal_pdf(monkeypatch, own)  # type: ignore[misc]
    assert out == {"filename": "x.pdf"} and calls == [(REQUEST, {"action": "portal_document"})]
    assert (ctx.audit_events[-1].action, ctx.audit_events[-1].outcome) == ("portal_document", "allowed")
    minor = {"request_id": REQUEST, "traveler_id": CHILD, "signer_traveler_id": LEAD}
    out, calls, _ = await _portal_pdf(monkeypatch, minor, CHILD)  # type: ignore[misc]
    assert calls[0][0] == REQUEST
    someone_else = {"request_id": REQUEST, "traveler_id": OTHER, "signer_traveler_id": None}
    with _raises(Forbidden, "envelope_not_yours"):
        await _portal_pdf(monkeypatch, someone_else, OTHER)


# ── routes ───────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def pools() -> LocalPools:
    return LocalPools()


async def _pool(_request: Any) -> object:
    return object()


@pytest.mark.parametrize("role", ["owner", "agent", "assistant"])
def test_the_agency_pdf_route(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, role: str) -> None:
    settings = pools.settings()
    app = create_app(settings, verifier=pools.verifier(settings), keys=object())
    monkeypatch.setattr(esign_router, "request_pool", _pool)
    monkeypatch.setattr(esign_router, "tenant_tx", fake_tx(tenant_ctx(ScriptedConn(), role=role)))
    monkeypatch.setattr(esign_router, "Vault", lambda keys, ctx: FakeVault())
    monkeypatch.setattr(esign_router, "require_keys", lambda keys: keys)
    calls: list[Any] = []

    async def envelope_pdf(_ctx: Any, _vault: Any, sealer: Any, signer: Any, request_id: Any, reason: str) -> dict:
        calls.append((str(request_id), reason, type(sealer).__name__, signer))
        return {"filename": "x.pdf"}

    monkeypatch.setattr(service, "envelope_pdf", envelope_pdf)
    h = {"Authorization": f"Bearer {pools.partner_web()}", "X-AA-Tenant-Id": str(TENANT)}
    path = f"/crm/v1/signatures/{REQUEST}/pdf"
    with TestClient(app) as client:
        assert client.post(path, json={"reason": "client_request"}).status_code == 401
        assert client.post(path, headers=h, json={"reason": "client_request"}).json() == {"filename": "x.pdf"}
        assert client.post(path, headers=h, json={}).status_code == 422
        assert client.get(path, headers=h).status_code == 405
    # The test environment seals with the stub, so no PAdES signer is configured.
    assert calls == [(str(REQUEST), "client_request", "DevStubSealer", None)]


def test_the_traveller_pdf_route(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = pools.settings()
    app = create_app(settings, verifier=pools.verifier(settings), keys=object())
    monkeypatch.setattr(client_pages, "request_pool", _pool)
    monkeypatch.setattr(client_pages, "require_keys", lambda keys: keys)
    seen: list[Any] = []

    async def agreement_pdf(_pool: Any, _keys: Any, token: str, session: Any, traveler: str, **kw: Any) -> dict:
        seen.append((token, session, traveler, sorted(kw)))
        return {"filename": "x.pdf", "content_b64": "JVBERg=="}

    monkeypatch.setattr(portal, "agreement_pdf", agreement_pdf)
    path = f"/crm/public/v1/traveller/agreements/{LEAD}/pdf"
    with TestClient(app, base_url="https://go.test") as client:
        assert client.get(path).status_code == 404  # no link cookie
        client.cookies.set(client_pages.PORTAL_LINK_COOKIE, TOKEN, domain="go.test", path="/crm/public/v1/traveller")
        res = client.get(path)
        assert res.status_code == 200 and res.headers["cache-control"] == "no-store"
    assert seen == [(TOKEN, None, str(LEAD), ["pades", "sealer"])]


# ── the certificate script ───────────────────────────────────────────────────────────────────────────────────
def test_the_certificate_script(seal_key: rsa.RSAPrivateKey, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    kms = SoftKms(seal_key)
    client = lambda profile, region: kms  # noqa: E731
    assert script.main(["check", "--environment", "dev"], client=client, out_dir=tmp_path) == 1
    assert script.main(["issue", "--environment", "dev"], client=client, out_dir=tmp_path) == 0
    written = (tmp_path / "seal-chain-dev.pem").read_bytes()
    assert b"PRIVATE" not in written and certificate.load_pem(written).leaf_public_key() == kms.public
    assert script.main(["check", "--environment", "dev"], client=client, out_dir=tmp_path) == 0
    assert kms.calls[0] == ("get_public_key", {"KeyId": "alias/crm-dev/seal"})
    other = SoftKms(rsa.generate_private_key(public_exponent=65537, key_size=2048))
    assert script.main(["check", "--environment", "dev"], client=lambda p, r: other, out_dir=tmp_path) == 1
    assert "DOES NOT match" in capsys.readouterr().out
    assert [c[0] for c in kms.calls] == ["get_public_key"] * 3  # the script never asks KMS to sign
    assert ACCESS and TRIP  # the shared fixtures stay importable
