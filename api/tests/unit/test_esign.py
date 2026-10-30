"""AA-955 (CRM-25) e-sign v1 over scripted connections: merge fields, the envelope, the seal, the portal signing
(per adult, guardian for a minor, insurance declination, refused codes logged), the terms re-accept rule and the
agency's agreements view. The database side (crm-025) is tests/integration/test_crm025_schema.py; end to end on
PostgreSQL is tests/integration/test_esign_api.py."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError

from app.errors import Conflict, Forbidden, Invalid, NotFound, Unavailable
from app.esign import envelope as env
from app.esign import merge, service, signing, terms
from app.esign.seal import (
    KMS_ALG,
    STUB_ALG,
    DevStubSealer,
    KmsSealer,
    Seal,
    build_sealer,
    require_sealer,
    verify_any,
)
from app.payments import service as payments
from app.portal import public as portal
from app.proposals import agreements
from app.proposals import public as proposal_page
from app.proposals import service as proposals
from app.settings import Settings
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import FakeVault, tenant_ctx
from tests.unit.test_lifecycle_services import (
    ACCESS,
    DOC,
    ECHO,
    LEAD,
    NOW,
    OTHER,
    OTP,
    PLAIN,
    TODAY,
    TOKEN,
    TRIP,
    _page_row,
    _portal,
    _raises,
    _signed_in,
)

TERMS = uuid.UUID("00000000-0000-4000-8000-000000000711")
DECL = uuid.UUID("00000000-0000-4000-8000-000000000712")
CHILD = uuid.UUID("00000000-0000-4000-8000-0000000007b3")
WAIVER_ROW = {"doc_id": DOC, "kind": "waiver", "version_label": "w1", "body_text": "Risk on {{trip.title}}.",
              "body_sha256": b"\x01" * 32}  # fmt: skip
TERMS_ROW = {"doc_id": TERMS, "kind": "booking_terms", "version_label": "t2", "body_text": "Terms of {{agency.name}}.",
             "body_sha256": b"\x02" * 32}  # fmt: skip
DECL_ROW = {"doc_id": DECL, "kind": "insurance_declination", "version_label": "d1",
            "body_text": "I, {{signer.name}}, decline insurance.", "body_sha256": b"\x03" * 32}  # fmt: skip
GOOD_CODE = {"otp_id": OTP, "code_hash": portal.code_hash(OTP, "654321"), "attempts": 0, "expired": False,
             "delivery": "mailbox"}  # fmt: skip


def _docs(*rows: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"doc_id": str(r["doc_id"]), "doc_sha256": bytes(r["body_sha256"]).hex()} for r in rows]


SIGN = {"code": "654321", "full_name": "Grace Lee", "agree": True}


def _conn(*, published: list[dict[str, Any]] | None = None, party: list[dict[str, Any]] | None = None,
          signature: Any = None, code: Any = None) -> ScriptedConn:  # fmt: skip
    conn = _signed_in(ScriptedConn()).on("crm.portal_unlocked", True).on("display_name", "Sen Travel")
    conn.on("kind IN ('booking_terms', 'waiver', 'insurance_declination')",
            [WAIVER_ROW] if published is None else published)  # fmt: skip
    adult = {"traveler_id": LEAD, "role": "lead", "dob_enc": b"enc:t.dob:1980-01-01"}
    conn.on("SELECT tt.traveler_id, tt.role, t.dob_enc", party or [adult])
    conn.on("AND traveler_id = $2 AND doc_id = $3", signature)
    conn.on("ORDER BY created_at DESC LIMIT 1", code)
    conn.on("SELECT now()", NOW)
    conn.on("INSERT INTO crm.terms_acceptance", uuid.uuid4())
    return conn


async def _sign(raw: dict[str, Any], sealer: Any = None, **kw: Any) -> tuple[bool, dict[str, Any]]:
    return await portal.sign(object(), object(), ECHO, TOKEN, "s", raw, origin=None,  # type: ignore[arg-type]
                             sealer=sealer if sealer is not None else DevStubSealer(), **kw)  # fmt: skip


# ── merge fields, envelope, seal ─────────────────────────────────────────────────────────────────


def test_merge_fields() -> None:
    assert merge.unknown_fields("Hi {{ trip.title }} {{passport.no}} {{x}} {{x}}") == ["passport.no", "x"]
    text = "{{agency.name}} / {{trip.end_date}} / {{nope}} / {{signer.name}}"
    assert merge.render(text, {"agency.name": "Sen", "signer.name": "Grace"}) == "Sen / — / {{nope}} / Grace"


def test_the_envelope_is_canonical_and_holds_no_name_in_the_evidence() -> None:
    doc = env.Document("waiver", str(DOC), "w1", "Risk on {{trip.title}} for {{signer.name}}.", b"\x01" * 32)
    args = {
        "agency_name": "Sen",
        "trip": {"title": "Laos", "start_date": "2026-11-01", "end_date": None},
        "documents": [doc],
        "values": {"trip.title": "Laos", "signer.name": "Grace Lee"},
        "audit": {"request_id": "r", "signer_role": "self", "typed_name_sha256": env.sha256("Grace Lee").hex()},
    }
    first, record, digest = env.build(**args)  # type: ignore[arg-type]
    assert env.build(**args)[0] == first  # type: ignore[arg-type]
    assert b"Risk on Laos for Grace Lee." in first and digest == hashlib.sha256(record).digest()
    parsed = json.loads(record)
    assert parsed["documents"][0]["rendered_sha256"] == env.sha256("Risk on Laos for Grace Lee.").hex()
    assert "Grace" not in record.decode() and f"Evidence SHA-256: {digest.hex()}".encode() in first


@pytest.mark.asyncio
async def test_the_dev_stub_seal() -> None:
    digest = hashlib.sha256(b"x").digest()
    seal = await DevStubSealer().seal(digest)
    assert seal.alg == STUB_ALG and await DevStubSealer().verify(digest, seal)
    assert not await DevStubSealer().verify(hashlib.sha256(b"y").digest(), seal)
    assert await verify_any(None, digest, seal) is True
    assert await verify_any(None, digest, Seal(KMS_ALG, "k", b"s" * 32)) is None
    with pytest.raises(ValueError, match="32 bytes"):
        await DevStubSealer().seal(b"short")


class FakeKms:
    """KMS as it behaves: Verify of a signature that does not match raises KMSInvalidSignatureException (a botocore
    ClientError); it never answers SignatureValid=false."""

    def __init__(self, fail: bool = False, invalid: type[Exception] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail = fail
        self.invalid = invalid

    def sign(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("sign", kw))
        if self.fail:
            raise RuntimeError("AccessDeniedException secret detail")
        return {"KeyId": "test-key-arn", "Signature": b"s" * 384}

    def verify(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("verify", kw))
        if self.fail:
            raise RuntimeError("KMSInvalidStateException secret detail")
        if kw["Signature"] != b"s" * 384:
            error = {"Error": {"Code": "KMSInvalidSignatureException", "Message": "fake"}}
            raise (self.invalid or ClientError)(error, "Verify")
        return {"KeyId": kw["KeyId"], "SignatureValid": True, "SigningAlgorithm": kw["SigningAlgorithm"]}


@pytest.mark.asyncio
async def test_the_kms_seal() -> None:
    kms = FakeKms()
    sealer = KmsSealer("alias/crm-test/seal", client_factory=lambda: kms)
    digest = hashlib.sha256(b"x").digest()
    seal = await sealer.seal(digest)
    assert seal == Seal(KMS_ALG, "test-key-arn", b"s" * 384)
    assert kms.calls[0][1] == {"KeyId": "alias/crm-test/seal", "Message": digest, "MessageType": "DIGEST",
                               "SigningAlgorithm": "RSASSA_PSS_SHA_256"}  # fmt: skip
    assert await verify_any(sealer, digest, seal) is True and kms.calls[-1][1]["KeyId"] == "test-key-arn"
    broken = KmsSealer("alias/crm-test/seal", client_factory=lambda: FakeKms(fail=True))
    with _raises(Unavailable, "seal_unavailable") as err:
        await broken.seal(digest)
    assert "secret" not in err.value.message
    # KMS not answering at reveal time: the seal is "not checked" (None), not a failed reveal.
    assert await verify_any(broken, digest, seal) is None
    with _raises(Unavailable, "seal_unavailable"):
        await broken.verify(digest, seal)


@pytest.mark.asyncio
async def test_a_tampered_kms_seal_is_false_and_only_an_outage_is_not_checked() -> None:
    digest = hashlib.sha256(b"x").digest()
    tampered = Seal(KMS_ALG, "test-key-arn", b"t" * 384)
    sealer = KmsSealer("alias/crm-test/seal", client_factory=lambda: FakeKms())
    assert await sealer.verify(digest, tampered) is False
    assert await verify_any(sealer, digest, tampered) is False
    # The modeled exception the real client raises (a ClientError subclass) reads the same.
    modeled = boto3.client("kms", region_name="us-west-1").exceptions.KMSInvalidSignatureException
    sealer = KmsSealer("alias/crm-test/seal", client_factory=lambda: FakeKms(invalid=modeled))
    assert await verify_any(sealer, digest, tampered) is False
    # Any other KMS error (key state, throttling, access) is an outage: not checked, never "forged".
    for code in ("KMSInvalidStateException", "ThrottlingException", "AccessDeniedException", "NotFoundException"):
        kms = FakeKms()

        def verify(**kw: Any) -> dict[str, Any]:
            raise ClientError({"Error": {"Code": code, "Message": "fake"}}, "Verify")  # noqa: B023

        kms.verify = verify  # type: ignore[method-assign]
        down = KmsSealer("alias/crm-test/seal", client_factory=lambda: kms)  # noqa: B023
        assert await verify_any(down, digest, tampered) is None
        with _raises(Unavailable, "seal_unavailable"):
            await down.verify(digest, tampered)


def test_which_sealer() -> None:
    assert isinstance(build_sealer(Settings(environment="test", db_ssl="disable")), DevStubSealer)
    assert isinstance(build_sealer(Settings(environment="dev", db_ssl="require")), DevStubSealer)
    assert build_sealer(Settings(environment="stg", db_ssl="require")) is None
    kms = build_sealer(Settings(environment="prod", db_ssl="require", kms_seal_key_id="alias/crm-prod/seal"))
    assert isinstance(kms, KmsSealer)
    with _raises(Unavailable, "esign_not_configured"):
        require_sealer(None)


# ── agreement versions ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_agreement_kinds_and_merge_fields() -> None:
    conn = ScriptedConn()
    out = await agreements.create(tenant_ctx(conn), kind="insurance_declination", version_label="d1",
                                  body_text="I decline insurance for {{trip.title}}.")  # fmt: skip
    assert out["status"] == "draft"
    with _raises(Invalid, "unknown_merge_field") as err:
        await agreements.create(tenant_ctx(conn), kind="waiver", version_label="w", body_text="{{passport.no}}")
    assert err.value.extra["fields"] == ["passport.no"]
    listed = await agreements.list_docs(tenant_ctx(ScriptedConn()))
    assert {f["name"] for f in listed["merge_fields"]} == set(merge.MERGE_FIELDS)


# ── portal signing ───────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sign_refusals(monkeypatch: pytest.MonkeyPatch) -> None:
    with _raises(Invalid, "invalid_submission") as err:
        await _sign({**SIGN, "agree": False, "full_name": 7, "insurance": "maybe", "traveler_id": "x"})
    assert err.value.extra["fields"] == ["full_name", "agree", "traveler_id", "insurance"]
    _portal(monkeypatch, _conn())
    with _raises(Unavailable, "esign_not_configured"):
        await portal.sign(object(), object(), PLAIN, TOKEN, "s", {**SIGN, "documents": _docs(WAIVER_ROW)},  # type: ignore[arg-type]
                          origin=None, sealer=None)  # fmt: skip
    _portal(monkeypatch, _conn(published=[]))
    with _raises(Conflict, "nothing_to_sign"):
        await _sign(SIGN)
    # Booking terms are published: the envelope holds them, so the v0 body (waiver only) is out of date.
    _portal(monkeypatch, _conn(published=[WAIVER_ROW, TERMS_ROW]))
    with _raises(Conflict, "document_changed") as err:
        await _sign({**SIGN, "doc_id": str(DOC), "doc_sha256": "01" * 32})
    assert [d["kind"] for d in err.value.extra["documents"]] == ["booking_terms", "waiver"]
    assert err.value.extra["documents"][0]["body_text"] == "Terms of Sen Travel."
    # An insurance declination is published: the choice is required.
    _portal(monkeypatch, _conn(published=[WAIVER_ROW, DECL_ROW]))
    with _raises(Invalid, "invalid_submission") as err:
        await _sign({**SIGN, "documents": _docs(WAIVER_ROW)})
    assert err.value.extra["fields"] == ["insurance"]
    _portal(monkeypatch, _conn(signature={"request_id": uuid.uuid4(), "status": "signed", "signed_at": NOW,
                                          "signer_role": "self"}))  # fmt: skip
    assert (await _sign({**SIGN, "documents": _docs(WAIVER_ROW)}))[1]["status"] == "already_signed"


@pytest.mark.parametrize(
    ("code", "detail", "reason"),
    [
        (None, "code_invalid", "code_missing"),
        ({**GOOD_CODE, "expired": True}, "code_expired", "code_expired"),
        ({**GOOD_CODE, "code_hash": b"x" * 32}, "code_invalid", "code_invalid"),
    ],
)
@pytest.mark.asyncio
async def test_a_refused_code_is_logged(monkeypatch: pytest.MonkeyPatch, code: Any, detail: str, reason: str) -> None:
    conn = _conn(code=code).on("RETURNING attempts, status", {"attempts": 1, "status": "pending"})
    ctx = _portal(monkeypatch, conn)
    signed, refusal = await _sign({**SIGN, "documents": _docs(WAIVER_ROW)})
    assert not signed and refusal["detail"] == detail
    (logged,) = conn.sql("INSERT INTO crm.signature_event (event_id, kind, trip_id")
    assert logged[1:] == ("code_refused", TRIP, LEAD, None, ACCESS, reason)
    assert not conn.sql("SET status = 'signed'")
    if reason == "code_expired":
        assert ("portal_code_check", "denied") in [(e.action, e.outcome) for e in ctx.audit_events]


@pytest.mark.asyncio
async def test_the_last_wrong_attempt_is_logged_as_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _conn(code={**GOOD_CODE, "code_hash": b"x" * 32}).on("RETURNING attempts, status",
                                                                 {"attempts": 5, "status": "failed"})  # fmt: skip
    _portal(monkeypatch, conn)
    signed, refusal = await _sign({**SIGN, "documents": _docs(WAIVER_ROW)})
    assert not signed and refusal == {"detail": "code_invalid", "attempts_left": 0}
    assert conn.sql("INSERT INTO crm.signature_event (event_id, kind, trip_id")[0][-1] == "attempts_exhausted"


@pytest.mark.asyncio
async def test_sign_one_envelope_with_terms_waiver_and_declination(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _conn(published=[WAIVER_ROW, TERMS_ROW, DECL_ROW], code=GOOD_CODE)
    _portal(monkeypatch, conn)
    raw = {**SIGN, "insurance": "declined", "documents": _docs(TERMS_ROW, WAIVER_ROW, DECL_ROW)}
    signed, answer = await _sign(raw, client_ip="192.0.2.7", user_agent="Browser/1\x00")
    assert signed and answer["status"] == "signed" and answer["signer_role"] == "self"
    assert answer["seal_alg"] == STUB_ALG and answer["traveler_id"] == str(LEAD)
    request_id = conn.sql("INSERT INTO crm.signature_request")[0][0]
    assert conn.sql("INSERT INTO crm.signature_request")[0][1:] == (TRIP, LEAD, ACCESS, DOC, None, "self")
    sign = conn.sql("SET status = 'signed', signed_at")[0]
    assert sign[0] == request_id and sign[3] == b"\x01" * 32 and sign[5] == OTP and sign[6] == "mailbox"
    assert sign[8:13] == (TERMS, b"\x02" * 32, DECL, b"\x03" * 32, "declined")
    assert hashlib.sha256(sign[7]).digest() == sign[4] and sign[14] == STUB_ALG
    record = json.loads(sign[7])
    assert [d["kind"] for d in record["documents"]] == ["booking_terms", "waiver", "insurance_declination"]
    assert record["client_ip_sha256"] == hashlib.sha256(b"192.0.2.7").hexdigest()
    assert record["user_agent_sha256"] == hashlib.sha256(b"Browser/1").hexdigest()
    assert "Grace" not in sign[7].decode() and record["insurance_choice"] == "declined"
    # The sealed envelope: stored under the docs DEK, its hash on the request, the stub seal verifies.
    (event,) = conn.sql("'signed', $3, $4")
    envelope = event[8].split(b":", 2)[2]
    assert b"I, Grace Lee, decline insurance." in envelope and b"Terms of Sen Travel." in envelope
    assert hashlib.sha256(envelope).digest() == sign[13] == event[9]
    assert await DevStubSealer().verify(event[9], Seal(event[10], event[11], event[12]))
    assert event[13].startswith(b"enc:signature_event.client_ip_enc") and event[14].endswith(b"Browser/1")
    # The booking terms accepted inside the envelope (R34).
    (acc,) = conn.sql("INSERT INTO crm.terms_acceptance")
    assert acc[5] == request_id and acc[6] == "envelope" and acc[7] == TERMS
    assert conn.sql("'signature', 'signed'")[0][1] == f"signature:{request_id}"


@pytest.mark.asyncio
async def test_insured_travellers_sign_without_the_declination(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _conn(published=[WAIVER_ROW, DECL_ROW], code=GOOD_CODE)
    _portal(monkeypatch, conn)
    signed, _ = await _sign({**SIGN, "insurance": "insured", "documents": _docs(WAIVER_ROW)})
    assert signed
    sign = conn.sql("SET status = 'signed', signed_at")[0]
    assert sign[8:13] == (None, None, None, None, "insured") and not conn.sql("INSERT INTO crm.terms_acceptance")


PARTY = [
    {"traveler_id": LEAD, "role": "lead", "dob_enc": b"enc:t.dob:1980-01-01"},
    {"traveler_id": CHILD, "role": "companion", "dob_enc": b"enc:t.dob:2015-06-01"},
    {"traveler_id": OTHER, "role": "companion", "dob_enc": None},
]


@pytest.mark.asyncio
async def test_the_lead_signs_as_guardian_for_a_minor(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _conn(party=PARTY, code=GOOD_CODE)
    ctx = _portal(monkeypatch, conn)
    signed, answer = await _sign({**SIGN, "traveler_id": str(CHILD), "documents": _docs(WAIVER_ROW)})
    assert signed and answer["signer_role"] == "guardian" and answer["traveler_id"] == str(CHILD)
    assert conn.sql("INSERT INTO crm.signature_request")[0][1:] == (TRIP, CHILD, ACCESS, DOC, LEAD, "guardian")
    assert json.loads(conn.sql("SET status = 'signed', signed_at")[0][7])["signer_role"] == "guardian"
    event = conn.sql("'signed', $3, $4")[0]
    assert event[3:5] == (CHILD, LEAD) and b"guardian of a minor" not in event[7]
    assert ("guardian_check", "allowed") in [(e.action, e.outcome) for e in ctx.audit_events]
    # Not a minor (no DOB on file counts as an adult): the lead cannot sign for them.
    _portal(monkeypatch, _conn(party=PARTY, code=GOOD_CODE))
    with _raises(Forbidden, "not_guardian"):
        await _sign({**SIGN, "traveler_id": str(OTHER), "documents": _docs(WAIVER_ROW)})


@pytest.mark.asyncio
async def test_a_minor_never_signs(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _conn(party=PARTY, code=GOOD_CODE)
    conn.on("FROM crm.traveler_access a JOIN crm.trip t", {**_access(CHILD), "role": "companion"})
    ctx = _portal(monkeypatch, conn)
    with _raises(Forbidden, "guardian_signs"):
        await _sign({**SIGN, "documents": _docs(WAIVER_ROW)})
    assert ("portal_sign", "denied") in [(e.action, e.outcome) for e in ctx.audit_events]
    # A companion adult signs only their own envelope.
    other = _conn(party=PARTY, code=GOOD_CODE)
    other.on("FROM crm.traveler_access a JOIN crm.trip t", {**_access(OTHER), "role": "companion"})
    _portal(monkeypatch, other)
    with _raises(Forbidden, "not_guardian"):
        await _sign({**SIGN, "traveler_id": str(CHILD), "documents": _docs(WAIVER_ROW)})


def _access(traveler: uuid.UUID) -> dict[str, Any]:
    from tests.unit.test_lifecycle_services import _access_row

    return _access_row(traveler_id=traveler)


@pytest.mark.asyncio
async def test_home_lists_the_envelopes_the_traveller_signs(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _conn(party=PARTY, published=[WAIVER_ROW, TERMS_ROW, DECL_ROW])
    conn.on("SELECT doc_id, version_label, body_text", WAIVER_ROW)
    conn.on("FROM crm.signature_request WHERE trip_id = $1 AND doc_id = $2",
            [{"traveler_id": CHILD, "request_id": uuid.uuid4(), "status": "signed", "signed_at": NOW,
              "signer_role": "guardian", "insurance_choice": "insured"}])  # fmt: skip
    _portal(monkeypatch, conn)
    home = await portal.home(object(), TOKEN, "session", object())  # type: ignore[arg-type]
    box = home["agreements"]
    assert [d["kind"] for d in box["documents"]] == ["booking_terms", "waiver"]
    assert box["documents"][1]["body_text"] == "Risk on Laos." and not box["guardian_signs_for_me"]
    assert box["insurance_declination"]["body_text"] == "I, [your full name], decline insurance."
    assert [(e["for"], e["signed"], e["minor_number"]) for e in box["envelopes"]] == [
        ("self", False, None),
        ("minor", True, 1),
    ]


@pytest.mark.asyncio
async def test_a_signing_code_is_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _signed_in(ScriptedConn()).on("crm.portal_unlocked", True)
    conn.on("SELECT email_enc FROM crm.traveler", b"enc:traveler.email_enc:grace@example.test")
    _portal(monkeypatch, conn)
    out = await portal.request_code(object(), object(), ECHO, TOKEN, purpose="sign", session="s", origin=None)  # type: ignore[arg-type]
    assert "dev_code" in out
    (logged,) = conn.sql("INSERT INTO crm.signature_event (event_id, kind, trip_id")
    assert logged[1:] == ("code_sent", TRIP, LEAD, None, ACCESS, None)


# ── terms re-accept ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_reaccept_rule() -> None:
    def ctx(published: Any, row: Any) -> Any:
        return tenant_ctx(ScriptedConn().on("WHERE kind = 'booking_terms' AND status = 'published'", published)
                          .on("EXISTS (SELECT 1 FROM crm.terms_acceptance a", row))  # fmt: skip

    assert await terms.reaccept_needed(ctx(None, None), TRIP) is None
    assert await terms.reaccept_needed(ctx(TERMS_ROW, None), TRIP) is None  # no accepted proposal
    assert await terms.reaccept_needed(ctx(TERMS_ROW, {"accepted": True, "deposit_paid": False}), TRIP) is None
    assert await terms.reaccept_needed(ctx(TERMS_ROW, {"accepted": False, "deposit_paid": True}), TRIP) is None
    found = await terms.reaccept_needed(ctx(TERMS_ROW, {"accepted": False, "deposit_paid": False}), TRIP)
    assert found is TERMS_ROW


@pytest.mark.asyncio
async def test_the_deposit_waits_for_the_new_terms(monkeypatch: pytest.MonkeyPatch) -> None:
    row = {"schedule_id": uuid.uuid4(), "trip_id": TRIP, "proposal_id": uuid.uuid4(), "kind": "deposit",
           "amount": 300, "currency": "USD", "status": "due", "proposal_status": "accepted",
           "stage": "proposal"}  # fmt: skip
    conn = ScriptedConn().on("FOR UPDATE OF s", row)
    conn.on("WHERE kind = 'booking_terms' AND status = 'published'", TERMS_ROW)
    conn.on("EXISTS (SELECT 1 FROM crm.terms_acceptance a", {"accepted": False, "deposit_paid": False})
    with _raises(Conflict, "terms_reaccept_required") as err:
        await proposals.mark_paid(
            tenant_ctx(conn), FakeVault(), row["schedule_id"], received_on=None, method="bank_transfer"
        )  # type: ignore[arg-type]
    assert err.value.extra["version_label"] == "t2" and not conn.sql("INSERT INTO crm.payment")


@pytest.mark.asyncio
async def test_an_invoice_payment_on_the_deposit_waits_for_the_new_terms() -> None:
    schedule = {"schedule_id": uuid.uuid4(), "trip_id": TRIP, "proposal_id": uuid.uuid4(), "kind": "deposit",
                "amount": 300, "currency": "USD", "status": "due", "proposal_status": "accepted",
                "stage": "proposal"}  # fmt: skip
    invoice = {"invoice_id": uuid.uuid4(), "trip_id": TRIP, "kind": "deposit", "status": "open",
               "schedule_id": schedule["schedule_id"], "currency": "USD", "total_amount": 300,
               "number": "INV-1"}  # fmt: skip
    conn = ScriptedConn().on("FROM crm.invoice WHERE invoice_id = $1", invoice).on("FOR UPDATE OF s", schedule)
    conn.on("WHERE kind = 'booking_terms' AND status = 'published'", TERMS_ROW)
    conn.on("EXISTS (SELECT 1 FROM crm.terms_acceptance a", {"accepted": False, "deposit_paid": False})
    with _raises(Conflict, "terms_reaccept_required"):
        await payments.record_payment(tenant_ctx(conn), FakeVault(), invoice["invoice_id"], amount="300",  # type: ignore[arg-type]
                                      method="bank_transfer", received_on=None)  # fmt: skip
    assert not conn.sql("INSERT INTO crm.payment")


@pytest.mark.asyncio
async def test_reaccept_on_the_proposal_page(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {"full_name": "Grace Lee", "accept_terms": True, "doc_id": str(TERMS), "doc_sha256": "02" * 32}
    with _raises(Invalid, "invalid_submission") as err:
        await proposal_page.reaccept_terms(object(), object(), PLAIN, TOKEN, {"doc_id": "x"}, origin=None)  # type: ignore[arg-type]
    assert err.value.extra["fields"] == ["full_name", "accept_terms"]

    def page(row: dict[str, Any], published: Any, state: Any) -> ScriptedConn:
        conn = ScriptedConn().on("WHERE p.link_id = $1", row).on("SELECT now()", NOW)
        conn.on("WHERE kind = 'booking_terms' AND status = 'published'", published)
        conn.on("EXISTS (SELECT 1 FROM crm.terms_acceptance a", state)
        return conn.on("INSERT INTO crm.terms_acceptance", uuid.uuid4())

    open_state = {"accepted": False, "deposit_paid": False}
    _portal(monkeypatch, page(_page_row(), TERMS_ROW, open_state), proposal_page, "proposal")
    with _raises(Conflict, "proposal_not_accepted"):
        await proposal_page.reaccept_terms(object(), object(), PLAIN, TOKEN, body, origin=None)  # type: ignore[arg-type]
    accepted = _page_row(status="accepted", accepted_at=NOW)
    _portal(monkeypatch, page(accepted, TERMS_ROW, {**open_state, "accepted": True}), proposal_page, "proposal")
    with _raises(Conflict, "nothing_to_accept"):
        await proposal_page.reaccept_terms(object(), object(), PLAIN, TOKEN, body, origin=None)  # type: ignore[arg-type]
    _portal(monkeypatch, page(accepted, TERMS_ROW, open_state), proposal_page, "proposal")
    with _raises(Conflict, "terms_changed"):
        await proposal_page.reaccept_terms(object(), object(), PLAIN, TOKEN, {**body, "doc_sha256": "09" * 32},  # type: ignore[arg-type]
                                           origin=None)  # fmt: skip
    conn = page(accepted, TERMS_ROW, open_state)
    _portal(monkeypatch, conn, proposal_page, "proposal")
    out = await proposal_page.reaccept_terms(object(), object(), PLAIN, TOKEN, body, origin=None,  # type: ignore[arg-type]
                                             client_ip="192.0.2.9", user_agent="UA")  # fmt: skip
    assert out["status"] == "accepted"
    (acc,) = conn.sql("INSERT INTO crm.terms_acceptance")
    assert acc[6] == "reaccept" and acc[7] == TERMS and acc[9] == b"\x01" * 32
    assert conn.sql("INSERT INTO crm.metric_event")[0][2] == "proposal.terms_reaccepted"


@pytest.mark.asyncio
async def test_the_accepted_page_shows_the_new_terms(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = ScriptedConn().on("WHERE p.link_id = $1", _page_row(status="accepted", accepted_at=NOW))
    conn.on("WHERE kind = 'booking_terms' AND status = 'published'", TERMS_ROW)
    conn.on("EXISTS (SELECT 1 FROM crm.terms_acceptance a", {"accepted": False, "deposit_paid": False})
    conn.on("display_name", "Sen").on("proposal.body_enc", "x")
    _portal(monkeypatch, conn, proposal_page, "proposal")
    monkeypatch.setattr(proposal_page, "Vault", lambda keys, ctx: _BodyVault())
    page = await proposal_page.read(object(), object(), TOKEN)  # type: ignore[arg-type]
    assert page["terms_reaccept"] == {
        "doc_id": str(TERMS),
        "version_label": "t2",
        "body_text": "Terms of Sen.",
        "body_sha256": "02" * 32,
    }


class _BodyVault(FakeVault):
    async def decrypt(self, table: str, column: str, row_id: Any, blob: bytes, **_: Any) -> str:
        return "the proposal text"


# ── the agency's view ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_trip_agreements_view() -> None:
    with _raises(NotFound, "trip_not_found"):
        await service.trip_agreements(tenant_ctx(ScriptedConn()), TRIP)
    request = uuid.uuid4()
    conn = ScriptedConn().on("SELECT trip_id FROM crm.trip", TRIP)
    conn.on("SELECT doc_id, version_label, body_sha256 FROM crm.agreement_doc", {"doc_id": DOC})
    conn.on("FROM crm.trip_traveler tt", [
        {"traveler_id": CHILD, "role": "companion", "details_done": True, "details_have": [True] * 8,
         "access_id": None, "access_status": None, "last_sign_in_at": None, "stage3_submitted_at": None,
         "waiver_signed": True},
    ])  # fmt: skip
    conn.on("WHERE trip_id = $1 AND status = 'signed'", [
        {"request_id": request, "traveler_id": CHILD, "signer_traveler_id": LEAD, "signer_role": "guardian",
         "insurance_choice": "declined", "signed_at": NOW, "envelope_sha256": "ab" * 32, "seal_alg": STUB_ALG,
         "doc_id": DOC, "terms_doc_id": TERMS, "declination_doc_id": DECL},
    ])  # fmt: skip
    conn.on("FROM crm.signature_event WHERE trip_id", [{"traveler_id": CHILD, "kind": "code_refused",
                                                         "reason": "code_expired", "occurred_at": NOW}])  # fmt: skip
    out = await service.trip_agreements(tenant_ctx(conn), TRIP)
    assert out["all_signed"] and out["waiver_required"]
    sig = out["travellers"][0]["signature"]
    assert sig["signer_role"] == "guardian" and sig["with_declination"] and sig["request_id"] == str(request)
    assert out["events"][0]["reason"] == "code_expired" and out["terms"]["published"] is None


@pytest.mark.asyncio
async def test_the_envelope_download() -> None:
    request, event = uuid.uuid4(), uuid.uuid4()
    with _raises(Invalid, "reason_required"):
        await service.envelope(tenant_ctx(ScriptedConn()), FakeVault(), None, request, "curious")  # type: ignore[arg-type]
    with _raises(NotFound, "signature_not_found"):
        await service.envelope(tenant_ctx(ScriptedConn()), FakeVault(), None, request, "client_request")  # type: ignore[arg-type]
    content = b"the envelope"
    digest = hashlib.sha256(content).digest()
    seal = await DevStubSealer().seal(digest)

    def conn(stored_digest: bytes) -> ScriptedConn:
        c = ScriptedConn().on("SELECT request_id, trip_id FROM crm.signature_request", {"request_id": request})
        return c.on("kind = 'signed'", {"event_id": event, "request_id": request, "envelope_enc": b"x",
                                        "envelope_sha256": stored_digest, "seal_alg": seal.alg,
                                        "seal_key_ref": seal.key_ref, "seal_value": seal.value,
                                        "evidence_sha256": b"\x05" * 32, "occurred_at": NOW})  # fmt: skip

    class DocsVault(FakeVault):
        async def decrypt_bytes(self, *a: Any, **k: Any) -> bytes:
            return content

    ctx = tenant_ctx(conn(b"\x00" * 32))
    with _raises(Unavailable, "document_integrity"):
        await service.envelope(ctx, DocsVault(), None, request, "client_request")  # type: ignore[arg-type]
    ctx = tenant_ctx(conn(digest))
    out = await service.envelope(ctx, DocsVault(), None, request, "client_request")  # type: ignore[arg-type]
    assert out["envelope_text"] == "the envelope" and out["seal"]["verified"] is True
    assert [(e.action, e.outcome) for e in ctx.audit_events][-1] == ("reveal", "allowed")


def test_documents_and_hashes_helpers() -> None:
    found = signing.Published(waiver=signing._doc(WAIVER_ROW), terms=None, declination=signing._doc(DECL_ROW))
    assert [d.kind for d in signing.documents(found, "insured")] == ["waiver"]
    assert [d.kind for d in signing.documents(found, "declined")] == ["waiver", "insurance_declination"]
    assert signing.shown_hashes({"doc_id": "a", "doc_sha256": "b"}) == {"a": "b"}
    assert signing.shown_hashes({"documents": [{"doc_id": "a", "doc_sha256": "b"}, "junk"]}) == {"a": "b"}
    lead = signing.PartyMember(LEAD, "lead", False)
    kid = signing.PartyMember(CHILD, "companion", True)
    assert signing.envelopes([lead, kid], LEAD) == [lead, kid] and signing.envelopes([lead, kid], CHILD) == []
    assert TODAY  # the shared fixtures stay importable
