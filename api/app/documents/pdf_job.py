"""``crm_pdf_render`` (AA-1070 item crm-21): the server-side PDFs of a trip, stored as agency documents.

A member asks (:func:`request`, ``POST /trips/{id}/pdfs``): one job row, ids only (the trip, the kind, the invoice /
proposal / booking line it is about, the member). The job (:func:`build_handler`) gathers the view in one
:func:`app.tenancy.system_tx` (actor ``job:crm_pdf_render``), builds the PDF (:mod:`app.documents.renders`), and
stores it through :func:`app.documents.service.upload` (sealed with the tenant docs DEK, uploader ``system``, not
shown to travellers until the agent shares it). The member sees it in the trip's Documents within seconds.

========================  ==================  =========================================================================
Kind (request)            ``crm.document``    Needs
========================  ==================  =========================================================================
``confirmation``          ``confirmation_pdf``  a booked (or later) trip
``invoice``               ``invoice_pdf``     ``invoice_id`` of the trip
``proposal``              ``proposal_pdf``    ``proposal_id`` of the trip
``voucher``               ``voucher``         ``booking_line_id``: a confirmed line of the trip
========================  ==================  =========================================================================

A request for the same document in the same minute queues once (idempotency key). Without the key provider or the
documents bucket the job finishes ``not_configured`` (never a queued-ahead retry that would keep DB Migrate busy).
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

import asyncpg

from app.documents import renders
from app.documents import service as documents
from app.documents.store import DocumentStore
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.invoices import service as invoices
from app.jobs import queue
from app.jobs.kinds import PDF_RENDER
from app.jobs.queue import Job
from app.jobs.worker import Handler, PermanentError
from app.proposals import prep_packs, text_proposal, text_render
from app.tenancy import AnyContext, TenantContext, system_tx
from app.travel_ops import vouchers
from app.vault import KeyProvider, Vault
from app.vault.errors import DecryptionDenied

log = logging.getLogger(__name__)

ACTOR = "job:crm_pdf_render"
KINDS = {"confirmation": "confirmation_pdf", "invoice": "invoice_pdf", "proposal": "proposal_pdf",
         "voucher": "voucher"}  # fmt: skip
SUBJECT = {"invoice": "invoice_id", "proposal": "proposal_id", "voucher": "booking_line_id"}
MAX_ATTEMPTS = 3

TRIP_SQL = """
SELECT trip_id, title, stage, start_date, end_date, party_adults, party_child_ages
  FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL
"""
INVOICE_TRIP_SQL = "SELECT trip_id FROM crm.invoice WHERE invoice_id = $1 AND deleted_at IS NULL"
PROPOSAL_TRIP_SQL = "SELECT trip_id FROM crm.proposal WHERE proposal_id = $1 AND deleted_at IS NULL"
LINE_SQL = """
SELECT trip_id, status FROM crm.booking_line WHERE booking_line_id = $1 AND deleted_at IS NULL
"""
PARTY_SQL = """
SELECT pt.traveler_id, pt.role, t.display_name_enc
  FROM crm.trip_traveler pt JOIN crm.traveler t ON t.traveler_id = pt.traveler_id AND t.deleted_at IS NULL
 WHERE pt.trip_id = $1 AND pt.deleted_at IS NULL
 ORDER BY pt.role = 'lead' DESC, pt.created_at, pt.traveler_id
"""
AGENCY_SQL = "SELECT display_name FROM crm.tenant WHERE tenant_id = $1"


async def _subject_trip(ctx: AnyContext, kind: str, subject_id: uuid.UUID) -> Any:
    if kind == "invoice":
        return await ctx.conn.fetchval(INVOICE_TRIP_SQL, subject_id)
    if kind == "proposal":
        return await ctx.conn.fetchval(PROPOSAL_TRIP_SQL, subject_id)
    line = await ctx.conn.fetchrow(LINE_SQL, subject_id)
    if line is None:
        return None
    if line["status"] != "confirmed":
        raise Conflict("line_not_confirmed", "a voucher is for a confirmed service")
    return line["trip_id"]


async def request(ctx: TenantContext, trip_id: uuid.UUID, kind: str, subject_id: uuid.UUID | None) -> dict[str, Any]:
    """Queue one PDF of the trip (202 shape: ``{queued, kind, document_kind}``)."""
    if kind not in KINDS:
        raise Invalid("invalid_pdf_kind", ", ".join(KINDS))
    trip = await ctx.conn.fetchrow(TRIP_SQL, trip_id)
    if trip is None:
        raise NotFound("trip_not_found")
    payload: dict[str, Any] = {"tenant_id": str(ctx.tenant_id), "trip_id": str(trip_id), "kind": kind,
                               "member_id": str(ctx.member_id)}  # fmt: skip
    if kind == "confirmation":
        if trip["stage"] not in vouchers.RELEASED_STAGES:
            raise Conflict("trip_not_booked", "a booking confirmation is for a booked trip")
    else:
        if subject_id is None:
            raise Invalid("subject_required", f"{SUBJECT[kind]} of the trip")
        if await _subject_trip(ctx, kind, subject_id) != trip_id:
            raise NotFound(f"{kind}_not_found")
        payload["subject_id"] = str(subject_id)
    minute = int(time.time() // 60)
    job = await queue.enqueue(
        ctx.conn,
        PDF_RENDER,
        payload,
        idempotency_key=f"{PDF_RENDER}:{trip_id}:{kind}:{subject_id or '-'}:{minute}",
        max_attempts=MAX_ATTEMPTS,
        created_by=f"member:{ctx.member_id}",
    )
    ctx.audit("create", "allowed", "document", None, reason=f"pdf_{kind}", object_ids=(str(trip_id),))
    return {"queued": job is not None, "kind": kind, "document_kind": KINDS[kind]}


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


async def build(ctx: AnyContext, vault: Vault, trip_id: uuid.UUID, kind: str,
                subject_id: uuid.UUID | None) -> tuple[bytes, str]:  # fmt: skip
    """The PDF bytes and the file name of one document."""
    trip = await ctx.conn.fetchrow(TRIP_SQL, trip_id)
    if trip is None:
        raise NotFound("trip_not_found")
    locale = await prep_packs.agency_locale(ctx)  # type: ignore[arg-type]
    agency = await ctx.conn.fetchval(AGENCY_SQL, ctx.tenant_id)
    ref = str(trip_id)[:8]
    if kind == "confirmation":
        party = [
            {
                "role": r["role"],
                "name": await vault.decrypt("traveler", "display_name_enc", r["traveler_id"], r["display_name_enc"]),
            }
            for r in await ctx.conn.fetch(PARTY_SQL, trip_id)
        ]
        lines = [
            {
                "title": ln["title"],
                "service_from": _iso(ln["service_from"]),
                "service_to": _iso(ln["service_to"]),
                "supplier": vouchers.AA_SUPPLIER_NAME if ln["supplier_kind"] == "aa" else ln["supplier_name"],
                "confirmation_no": ln["confirmation_no"],
            }
            for ln in await ctx.conn.fetch(vouchers.LINES_SQL, trip_id)
        ]
        view = {
            "agency_name": agency,
            "trip_title": trip["title"],
            "start_date": _iso(trip["start_date"]),
            "end_date": _iso(trip["end_date"]),
            "adults": trip["party_adults"],
            "children": len(trip["party_child_ages"] or []),
            "party": party,
            "lines": lines,
        }
        return renders.confirmation(view, locale), f"confirmation-{ref}.pdf"
    if subject_id is None:
        raise NotFound(f"{kind}_not_found")
    if kind == "invoice":
        view = await invoices.full(ctx, subject_id)
        if view["trip_id"] != str(trip_id):
            raise NotFound("invoice_not_found")
        pay_by = (
            [dict(r) for r in await ctx.conn.fetch(invoices.PAY_BY_SQL)]
            if view["status"] in invoices.RECEIVABLE or view["status"] == "draft"
            else []
        )
        bill = await invoices.bill_to(ctx, vault, trip_id)  # type: ignore[arg-type]
        content = renders.invoice(view, locale, agency=agency, bill_to=bill, pay_instructions=pay_by)
        return content, f"invoice-{view.get('number') or 'draft'}.pdf"
    if kind == "proposal":
        row = await ctx.conn.fetchrow(text_proposal.ROW_SQL, subject_id)
        if row is None or row["trip_id"] != trip_id:
            raise NotFound("proposal_not_found")
        digest = await text_proposal.current_hash(ctx, vault, row)  # type: ignore[arg-type]
        snap = await text_proposal.snapshot(ctx, vault, row, digest, locale)  # type: ignore[arg-type]
        rendered = text_render.render(snap, "email", locale)
        content = renders.proposal(rendered.subject or trip["title"], rendered.body, agency=agency, locale=locale)
        return content, f"proposal-{ref}-v{row['version']}.pdf"
    found = await vouchers.trip_vouchers(ctx, vault, trip_id)
    one = next((v for v in found["vouchers"] if v["booking_line_id"] == str(subject_id)), None)
    if one is None:
        raise NotFound("voucher_not_found")
    return renders.voucher(found, one, locale), f"voucher-{one['voucher_no']}.pdf"


def _payload(payload: dict[str, Any]) -> tuple[uuid.UUID, uuid.UUID, str, uuid.UUID | None]:
    try:
        kind = str(payload["kind"])
        subject = payload.get("subject_id")
        return (uuid.UUID(str(payload["tenant_id"])), uuid.UUID(str(payload["trip_id"])), kind,
                uuid.UUID(str(subject)) if subject else None)  # fmt: skip
    except (KeyError, ValueError):
        raise PermanentError("bad_payload") from None


def build_handler(keys: KeyProvider | None, store: DocumentStore | None) -> Handler:
    async def crm_pdf_render(pool: asyncpg.Pool, job: Job) -> dict[str, Any]:
        tenant_id, trip_id, kind, subject_id = _payload(job.payload)
        if kind not in KINDS:
            raise PermanentError("bad_kind")
        if keys is None or store is None:
            log.error("crm_pdf_render_not_configured reason=%s", "key_provider" if keys is None else "documents")
            return {"reason_code": "not_configured"}
        try:
            async with system_tx(pool, tenant_id, actor=ACTOR) as ctx:
                vault = Vault(keys, ctx)  # type: ignore[arg-type]
                content, filename = await build(ctx, vault, trip_id, kind, subject_id)
                saved = await documents.upload(
                    ctx,  # type: ignore[arg-type]
                    vault,
                    store,
                    traveler_id=None,
                    trip_id=trip_id,
                    kind=KINDS[kind],
                    filename=filename,
                    content_type="application/pdf",
                    content=content,
                    uploaded_by_kind="system",
                )
        except DecryptionDenied as exc:
            raise PermanentError(type(exc).__name__) from None
        except (NotFound, Forbidden) as exc:
            # The trip, invoice, proposal or line is gone, or the agency is locked: nothing to make.
            return {"reason_code": exc.code if isinstance(exc.code, str) and len(exc.code) <= 64 else "not_found"}
        log.info("crm_pdf_rendered tenant_id=%s kind=%s size=%s", tenant_id, kind, len(content))
        return {"document_id": saved["document_id"], "kind": kind, "byte_count": len(content)}

    return crm_pdf_render
