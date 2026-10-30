"""AA-955 (card CRM-25) end to end on PostgreSQL 15 with the RDS role shape: e-sign v1 as crm_app through the
signed-link contexts and ``tenant_tx()``, with real field encryption and the dev stub seal.

- R34: the proposal accept writes a terms acceptance pinned to the version, its hash and the price hash.
- **E** (card): the agency publishes newer booking terms after the client saw them: the accepted page shows them, the
  deposit "Mark paid" is 409 ``terms_reaccept_required`` until the client re-accepts on the same page.
- **R** (card): an expired signing code is refused (422 answer) and logged (``signature_event`` code_refused).
- One envelope per traveller: booking terms + waiver + insurance declination (R13) with merge fields, sealed; the
  lead signs as guardian for a minor (O-6); the minor's own access is 403 ``guardian_signs``; the gate (s3 = 100)
  opens once every traveller's waiver is signed.
- The agency's Agreements view and the sealed envelope download (the stub seal verifies).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

import asyncpg
import pytest

from app import stages
from app.errors import Conflict, Forbidden
from app.esign import service as esign
from app.esign.seal import DevStubSealer
from app.portal import progress
from app.portal import public as portal
from app.proposals import agreements, service
from app.proposals import public as proposal_page
from app.tenancy import tenant_tx
from app.vault import Vault
from tests.integration.test_lifecycle_api import (
    ECHO,
    PLAIN,
    STAGE3,
    _proposal,
    _schedule,
    _setup,
    _sign_in,
    _token,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

SEALER = DevStubSealer()
EVIDENCE = {"client_ip": "192.0.2.10", "user_agent": "Mozilla/5.0 (test)"}


async def _code(pool: asyncpg.Pool, s: dict[str, Any], token: str, session: str) -> str:
    sent = await portal.request_code(pool, s["keys"], ECHO, token, purpose="sign", session=session, origin=None)
    return sent["dev_code"]


async def _sign(pool: asyncpg.Pool, s: dict[str, Any], token: str, session: str, name: str, **extra: Any) -> Any:
    home = await portal.home(pool, token, session, s["keys"])
    box = home["agreements"]
    docs = [{"doc_id": d["doc_id"], "doc_sha256": d["body_sha256"]} for d in box["documents"]]
    if extra.get("insurance") == "declined":
        d = box["insurance_declination"]
        docs.append({"doc_id": d["doc_id"], "doc_sha256": d["body_sha256"]})
    code = extra.pop("code", None) or await _code(pool, s, token, session)
    raw = {"code": code, "full_name": name, "agree": True, "documents": docs, **extra}
    return await portal.sign(pool, s["keys"], ECHO, token, session, raw, origin=None, sealer=SEALER, **EVIDENCE)


async def test_esign_v1_end_to_end(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"]) as ctx:
        text = "I, {{signer.name}}, decline travel insurance for {{trip.title}}."
        decl = await agreements.create(ctx, kind="insurance_declination", version_label="d1", body_text=text)
        await agreements.publish(ctx, uuid.UUID(decl["doc_id"]))
    sent = await _proposal(app_pool, s)
    token = _token(sent["link_path"])
    page = await proposal_page.read(app_pool, s["keys"], token)
    accepted = await proposal_page.accept(
        app_pool, s["keys"], PLAIN, token,
        {"full_name": "Grace Lee", "accept_terms": True, "price_hash": page["price_hash"],
         "terms_doc_id": page["terms"]["doc_id"]},
        origin=None, **EVIDENCE,
    )  # fmt: skip
    assert accepted["status"] == "accepted" and accepted["terms_reaccept_required"] is False
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        row = await ctx.conn.fetchrow(
            "SELECT source, doc_id, price_hash, client_ip_enc, evidence_record, evidence_sha256, keep_until "
            "FROM crm.terms_acceptance WHERE trip_id = $1",
            s["trip"],
        )
    assert row["source"] == "proposal" and row["doc_id"] == s["terms"]
    assert bytes(row["price_hash"]).hex() == sent["price_hash"] and b"192.0.2.10" not in bytes(row["client_ip_enc"])
    assert hashlib.sha256(bytes(row["evidence_record"])).digest() == bytes(row["evidence_sha256"])

    # Card E: the agency publishes newer terms; the deposit waits for the client's re-accept.
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"]) as ctx:
        v2 = await agreements.create(ctx, kind="booking_terms", version_label="v2026-11", body_text="New terms.")
        await agreements.publish(ctx, uuid.UUID(v2["doc_id"]))
    page = await proposal_page.read(app_pool, s["keys"], token)
    assert page["terms_reaccept"]["version_label"] == "v2026-11"
    deposit = next(r for r in (await _schedule(app_pool, s))["schedule"] if r["kind"] == "deposit")
    with pytest.raises(Conflict) as err:
        async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
            await service.mark_paid(ctx, Vault(s["keys"], ctx), uuid.UUID(deposit["schedule_id"]), received_on=None,
                                    method="bank_transfer")  # fmt: skip
    assert err.value.code == "terms_reaccept_required"
    again = page["terms_reaccept"]
    with pytest.raises(Conflict) as err:
        stale = {"full_name": "Grace Lee", "accept_terms": True, "doc_id": again["doc_id"], "doc_sha256": "0" * 64}
        await proposal_page.reaccept_terms(app_pool, s["keys"], PLAIN, token, stale, origin=None)
    assert err.value.code == "terms_changed"
    done = await proposal_page.reaccept_terms(
        app_pool, s["keys"], PLAIN, token,
        {"full_name": "Grace Lee", "accept_terms": True, "doc_id": again["doc_id"],
         "doc_sha256": again["body_sha256"]},
        origin=None, **EVIDENCE,
    )  # fmt: skip
    assert done["status"] == "accepted"
    assert (await proposal_page.read(app_pool, s["keys"], token))["terms_reaccept"] is None
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        paid = await service.mark_paid(ctx, Vault(s["keys"], ctx), uuid.UUID(deposit["schedule_id"]),
                                       received_on=None, method="bank_transfer")  # fmt: skip
    assert paid["trip_moved"]["stage"] == "booked"
    links = {i["traveler_id"]: _token(i["link_path"]) for i in paid["portal"]["invited"]}
    lead_token, child_token = links[str(s["lead"])], links[str(s["companion"])]

    # Stage 3: the lead is an adult; the companion is 10 on the start date (a minor).
    lead_session = await _sign_in(app_pool, s, lead_token)
    child_session = await _sign_in(app_pool, s, child_token)
    await portal.submit_stage3(app_pool, s["keys"], ECHO, lead_token, lead_session, STAGE3, origin=None)
    await portal.submit_stage3(app_pool, s["keys"], ECHO, child_token, child_session,
                               {**STAGE3, "given_name": "Tom", "dob": "2016-06-01", "passport_no": "PA7654321"},
                               origin=None)  # fmt: skip

    # The minor never signs; the lead sees two envelopes (theirs and the minor's).
    with pytest.raises(Forbidden) as err:
        await _sign(app_pool, s, child_token, child_session, "Tom Lee")
    assert err.value.code == "guardian_signs"
    home = await portal.home(app_pool, lead_token, lead_session, s["keys"])
    box = home["agreements"]
    assert [d["version_label"] for d in box["documents"]] == ["v2026-11", "v1"]
    assert [(e["for"], e["signed"]) for e in box["envelopes"]] == [("self", False), ("minor", False)]
    assert box["insurance_declination"]["body_text"] == (
        "I, [your full name], decline travel insurance for Laos 10 nights."
    )

    # Card R: an expired signing code is refused and logged.
    code = await _code(app_pool, s, lead_token, lead_session)
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        await ctx.conn.execute(
            "UPDATE crm.otp_challenge SET created_at = now() - interval '11 minutes', "
            "expires_at = now() - interval '1 minute' "
            "WHERE traveler_id = $1 AND purpose = 'sign' AND status = 'pending'",
            s["lead"],
        )
    signed, refusal = await _sign(app_pool, s, lead_token, lead_session, "Grace Lee", code=code,
                                insurance="insured")  # fmt: skip
    assert not signed and refusal["detail"] == "code_expired"
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        logged = await ctx.conn.fetch(
            "SELECT kind, reason FROM crm.signature_event WHERE trip_id = $1 ORDER BY occurred_at", s["trip"]
        )
    assert ("code_refused", "code_expired") in [(r["kind"], r["reason"]) for r in logged]
    assert ("code_sent", None) in [(r["kind"], r["reason"]) for r in logged]

    # The lead signs their own envelope (declining insurance), then the minor's as guardian.
    signed, mine = await _sign(app_pool, s, lead_token, lead_session, "Grace Lee", insurance="declined")
    assert signed and mine["signer_role"] == "self" and mine["seal_alg"] == "dev-stub-hmac-sha256"
    signed, child = await _sign(app_pool, s, lead_token, lead_session, "Grace Lee", insurance="insured",
                                traveler_id=str(s["companion"]))  # fmt: skip
    assert signed and child["signer_role"] == "guardian" and child["traveler_id"] == str(s["companion"])
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        assert (await progress.compute(ctx, s["trip"])).s3 == 100
        moved = await stages.move(ctx, s["trip"], "pre_departure", vault=Vault(s["keys"], ctx))
        assert moved["stage"] == "pre_departure"
        view = await esign.trip_agreements(ctx, s["trip"])
        sources = sorted(r["source"] for r in view["terms"]["acceptances"])
    assert view["all_signed"] and not view["terms"]["reaccept_required"]
    by = {t["traveler_id"]: t["signature"] for t in view["travellers"]}
    assert by[str(s["lead"])]["insurance_choice"] == "declined" and by[str(s["lead"])]["with_declination"]
    assert by[str(s["companion"])]["signer_role"] == "guardian"
    assert sources == ["envelope", "envelope", "proposal", "reaccept"]

    # The sealed envelope: decrypted on a reveal with a reason, its hash and the seal check out.
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        out = await esign.envelope(ctx, Vault(s["keys"], ctx), SEALER, uuid.UUID(by[str(s["lead"])]["request_id"]),
                                   "client_request")  # fmt: skip
        stored = await ctx.conn.fetchrow(
            "SELECT evidence_record, envelope_enc FROM crm.signature_event WHERE request_id = $1 AND kind = 'signed'",
            uuid.UUID(by[str(s["lead"])]["request_id"]),
        )
    text = out["envelope_text"]
    assert "I, Grace Lee, decline travel insurance for Laos 10 nights." in text and "New terms." in text
    assert out["seal"]["verified"] is True and out["envelope_sha256"] == by[str(s["lead"])]["envelope_sha256"]
    assert b"Grace" not in bytes(stored["envelope_enc"]) and b"Grace" not in bytes(stored["evidence_record"])
    record = json.loads(bytes(stored["evidence_record"]))
    assert record["user_agent_sha256"] == hashlib.sha256(b"Mozilla/5.0 (test)").hexdigest()
