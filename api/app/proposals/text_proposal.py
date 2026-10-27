"""Module 4 text proposal, the agency side (card CRM-22, AA-951; design v3 M4, F4, WF-5 / A-05). Routes in
:mod:`app.routers.proposal_text`; the pure renderers in :mod:`app.proposals.text_render`.

- **Preview** (``GET /proposals/{id}/text``, every role): one version rendered as e-mail or WhatsApp text, English or
  Vietnamese (default: the agency's locale), optionally inside a message template (R9), with the version's
  ``price_hash``. A version renders when its price is fixed or computable: a builder version once published (sent and
  accepted too), a text version as a draft (its hash computed now, exactly as Send fixes it) or once sent. A builder
  draft is 409 ``proposal_not_published``; a superseded or withdrawn version 409 ``proposal_not_sendable``.
- **Send** (``POST /proposals/{id}/text/send``, owner / agent: ``PROPOSAL_PUBLISH``; e-mail also ``MESSAGE_SEND``): the
  request names the ``price_hash`` of the text the agent saw. **A hash that is not the version's current one is 409
  ``price_hash_mismatch``** with the current hash (card R: the amounts, the text or the terms changed since the
  preview, or another version is the one to send) and nothing is written. Then, in the same transaction:

  1. a version not sent yet goes out as the link send does (AA-946 / AA-950 :func:`app.proposals.service.send`: the
     price hash fixed for a text version, the earlier version superseded, the client's ``proposal`` link made or
     reused); a version already sent keeps its link (the text then carries no link: the client has it);
  2. the text is rendered again from the same snapshot, with the link when one was made
     (``<client origin>/c/p/<token>``, the client origin = the first ``INTAKE_PAGE_ORIGINS`` entry);
  3. e-mail: one message from the agency's mailbox on the trip's conversation (AA-945 send-as: the 10 s Undo, a
     disconnected mailbox holds it, no mailbox = 409 ``mailbox_not_connected`` and the version is not sent either);
     WhatsApp: the CRM has no WhatsApp channel, so the answer carries the final text for the agent to paste (and the
     link path once), and the send is recorded (audit ``proposal_text_send``, reason ``whatsapp``).

Every function takes the open context and never opens one. Nothing here reads or returns a net amount or a margin.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from app.errors import Conflict, Invalid, NotFound
from app.mail import conversations, templates
from app.permissions import Action, require
from app.proposals import builder, prep_packs, rules, service
from app.proposals import text_render as tr
from app.tenancy import TenantContext
from app.vault import Vault

RENDERABLE = {
    "builder": ("published", "sent", "accepted"),
    "text": ("draft", "sent", "accepted"),
}
TO_SEND = {"builder": "published", "text": "draft"}

ROW_SQL = """
SELECT p.proposal_id, p.trip_id, p.version, p.status, p.kind, p.body_enc, p.currency, p.total_amount,
       p.deposit_amount, p.balance_due_on, p.terms_doc_id, p.price_hash, p.price_visibility, p.room_config,
       p.deposit_pct, p.starts_on, p.link_id, t.title AS trip_title, t.stage, t.start_date, t.destinations
  FROM crm.proposal p JOIN crm.trip t ON t.trip_id = p.trip_id AND t.deleted_at IS NULL
 WHERE p.proposal_id = $1 AND p.deleted_at IS NULL
"""
LOCK_SQL = ROW_SQL + "   FOR UPDATE OF p"
AGENCY_SQL = "SELECT display_name, default_locale FROM crm.tenant LIMIT 1"
TERMS_SHA_SQL = "SELECT body_sha256 FROM crm.agreement_doc WHERE doc_id = $1"
OWNER_SQL = "SELECT owner_member_id FROM crm.trip WHERE trip_id = $1"
LEAD_NAME_SQL = """
SELECT t.traveler_id, t.given_name_enc, t.display_name_enc
  FROM crm.trip_traveler tt JOIN crm.traveler t ON t.traveler_id = tt.traveler_id
 WHERE tt.trip_id = $1 AND tt.role = 'lead' AND tt.deleted_at IS NULL AND t.deleted_at IS NULL
 LIMIT 1
"""


def _place(code: str) -> str:
    return " ".join(w.capitalize() for w in code.split("_") if w)


async def current_hash(ctx: TenantContext, vault: Vault, row: Any) -> str:
    """The version's price hash now: stored once fixed; a text draft's computed as Send would fix it."""
    kind, status = row["kind"], row["status"]
    if status not in RENDERABLE.get(kind, ()):
        if kind == "builder" and status == "draft":
            raise Conflict("proposal_not_published", "publish this version in the builder first")
        raise Conflict("proposal_not_sendable", "a superseded or withdrawn version is not sent again")
    if row["price_hash"]:
        return bytes(row["price_hash"]).hex()
    body = await vault.decrypt("proposal", "body_enc", row["proposal_id"], row["body_enc"])
    terms = await ctx.conn.fetchval(TERMS_SHA_SQL, row["terms_doc_id"]) if row["terms_doc_id"] else None
    return service.price_hash(
        proposal_id=row["proposal_id"], version=row["version"], currency=row["currency"], total=row["total_amount"],
        deposit=row["deposit_amount"], balance_due_on=row["balance_due_on"], body=body,
        terms_doc_id=row["terms_doc_id"], terms_sha256=bytes(terms) if terms else None,
    ).hex()  # fmt: skip


def _items(s: dict[str, Any], day_id: Any, visibility: str) -> tuple[tr.Item, ...]:
    options = builder._option_map(s)
    items = []
    for b in s["blocks"]:
        if b["proposal_day_id"] != day_id or b["default_option_id"] is None:
            continue
        default = options.get(str(b["default_option_id"]))
        if default is None:
            continue
        alternatives = []
        if b["client_may_swap"]:
            for o in s["options"]:
                if o["option_block_id"] != b["option_block_id"] or o["option_id"] == default["option_id"]:
                    continue
                if o["availability"] == "sold_out":
                    continue
                diff = Decimal(o["sell_amount"]) - Decimal(default["sell_amount"])
                alternatives.append(tr.Alternative(
                    o["title"], rules.money(diff) if visibility == "total_with_deltas" else None,
                    o["availability"] == "on_request",
                ))  # fmt: skip
        items.append(tr.Item(b["title"], default["title"], bool(default["pay_on_arrival"]),
                             default["availability"] == "on_request", tuple(alternatives)))  # fmt: skip
    return tuple(items)


async def snapshot(ctx: TenantContext, vault: Vault, row: Any, price_hash: str, locale: str) -> tr.Snapshot:
    """The version as the client sees it (defaults, sell amounts, deltas per D1; never a net or a margin)."""
    agency = await ctx.conn.fetchrow(AGENCY_SQL)
    body = await vault.decrypt("proposal", "body_enc", row["proposal_id"], row["body_enc"])
    common = {
        "version": row["version"], "price_hash": price_hash,
        "agency_name": agency["display_name"] if agency else "", "trip_title": row["trip_title"],
        "currency": row["currency"].strip(), "total": Decimal(row["total_amount"]),
        "deposit": Decimal(row["deposit_amount"]), "balance_due_on": row["balance_due_on"], "body": body,
    }  # fmt: skip
    if row["kind"] != builder.KIND:
        return tr.Snapshot(kind="text", **common)
    s = await builder._structure(ctx, row["proposal_id"])
    sums = builder._charged(s, None, row["deposit_pct"])
    days: list[tr.Day] = []
    before: tr.BeforeYouGo | None = None
    for d in s["days"]:
        notes = None
        if d["notes_enc"] is not None:
            notes = await vault.decrypt("proposal_day", "notes_enc", d["proposal_day_id"], d["notes_enc"])
        if d["day_no"] == 0:
            before = await _before_you_go(ctx, row, notes, locale)
            continue
        date = builder._day_date(row["starts_on"], d["day_no"])
        days.append(tr.Day(
            d["day_no"], date, d["title"], d["place"], rules.utc_offset(d["tz"], date or row["starts_on"]), notes,
            _items(s, d["proposal_day_id"], row["price_visibility"]),
        ))  # fmt: skip
    rooms = builder._jsonb(row["room_config"]) or []
    return tr.Snapshot(kind="builder", pay_on_arrival_total=Decimal(sums["pay_on_arrival_total"]),
                       starts_on=row["starts_on"], rooms=tuple(rooms), days=tuple(days), before_you_go=before,
                       **common)  # fmt: skip


async def _before_you_go(ctx: TenantContext, row: Any, notes: str | None, locale: str) -> tr.BeforeYouGo:
    """Day 0: the agency's notes, the packs (only while the trip has AA items, card E), the fitness warning."""
    b = await prep_packs.basis(ctx, row["proposal_id"], row["trip_id"])
    packs: list[tr.Pack] = []
    if b["aa_items"]:
        for rows in prep_packs.by_group(await prep_packs.selected(ctx, row["proposal_id"])).values():
            pick = prep_packs.best(rows, locale)
            packs.append(tr.Pack(pick["title"], tuple(prep_packs.sections(pick["body_markdown"]))))
    return tr.BeforeYouGo(notes, tuple(packs), b["fitness_warning"])


async def _values(ctx: TenantContext, row: Any, locale: str) -> dict[str, str | None]:
    """The merge field values (R9 names). The lead's first name is opened only when the template asks for it."""
    agency = await ctx.conn.fetchrow(AGENCY_SQL)
    start = row["starts_on"] or row["start_date"]
    return {
        "trip_name": row["trip_title"],
        "agency_name": agency["display_name"] if agency else None,
        "agent_name": None,  # a member has no stored name (identity is the token's); the agent types it
        "destination": ", ".join(_place(c) for c in row["destinations"] or []) or None,
        "departure_date": tr.date(start, locale, weekday=False) if start else None,
    }


async def _first_name(ctx: TenantContext, vault: Vault, trip_id: uuid.UUID, locale: str) -> str:
    lead = await ctx.conn.fetchrow(LEAD_NAME_SQL, trip_id)
    if lead is not None and lead["given_name_enc"] is not None:
        name = await vault.decrypt("traveler", "given_name_enc", lead["traveler_id"], lead["given_name_enc"],
                                   tenant_id=ctx.tenant_id)  # fmt: skip
        if name.strip():
            return name.strip()
    if lead is not None and lead["display_name_enc"] is not None:
        name = await vault.decrypt("traveler", "display_name_enc", lead["traveler_id"], lead["display_name_enc"],
                                   tenant_id=ctx.tenant_id)  # fmt: skip
        if name.split():
            return name.split()[0]
    return tr.STRINGS[locale]["fallback_name"]


async def _template_text(
    ctx: TenantContext, vault: Vault, template_id: uuid.UUID | None, intro: str | None
) -> str | None:
    if intro is not None:
        text = intro.replace("\r\n", "\n").strip()
        if len(text) > templates.MAX_BODY:
            raise Invalid("intro_too_long", f"at most {templates.MAX_BODY} characters")
        return text or None
    if template_id is None:
        return None
    return (await templates.get(ctx, vault, template_id))["body"]


async def _render(
    ctx: TenantContext, vault: Vault, row: Any, price_hash: str, fmt: str, locale: str, template: str | None,
    link_url: str | None,
) -> tuple[tr.Rendered, list[str]]:  # fmt: skip
    snap = await snapshot(ctx, vault, row, price_hash, locale)
    values = await _values(ctx, row, locale)
    fields: list[str] = []
    if template and "traveller_first_name" in template:
        values["traveller_first_name"] = await _first_name(ctx, vault, row["trip_id"], locale)
        fields.append("given_name")
    return tr.render(snap, fmt, locale, template=template, values=values, link_url=link_url), fields


def _check(fmt: str, locale: str | None) -> None:
    if fmt not in tr.FORMATS:
        raise Invalid("invalid_format", "email or whatsapp")
    if locale is not None and locale not in tr.LOCALES:
        raise Invalid("invalid_locale", "en or vi")


async def preview(
    ctx: TenantContext, vault: Vault, proposal_id: uuid.UUID, *, fmt: str, locale: str | None,
    template_id: uuid.UUID | None = None,
) -> dict[str, Any]:  # fmt: skip
    _check(fmt, locale)
    row = await ctx.conn.fetchrow(ROW_SQL, proposal_id)
    if row is None:
        raise NotFound("proposal_not_found")
    digest = await current_hash(ctx, vault, row)
    lang = locale or await prep_packs.agency_locale(ctx)
    template = await _template_text(ctx, vault, template_id, None)
    rendered, fields = await _render(ctx, vault, row, digest, fmt, lang, template, None)
    ctx.audit("view", "allowed", "proposal", str(proposal_id), fields=("text_proposal", *fields))
    return {
        "proposal_id": str(proposal_id),
        "trip_id": str(row["trip_id"]),
        "version": row["version"],
        "kind": row["kind"],
        "status": row["status"],
        "format": rendered.format,
        "locale": rendered.locale,
        "subject": rendered.subject,
        "body": rendered.body,
        "price_hash": digest,
        "reference": tr.reference(digest),
        "unfilled": list(rendered.unfilled),
        # Send makes or reuses the client's link and adds it to the text; a sent version keeps the one it has.
        "link": "added_on_send" if row["status"] == TO_SEND[row["kind"]] else "client_has_it",
    }


def _origin(client_origin: str | None) -> str | None:
    return client_origin.rstrip("/") if client_origin else None


async def send(
    ctx: TenantContext,
    vault: Vault,
    proposal_id: uuid.UUID,
    *,
    fmt: str,
    locale: str | None,
    price_hash: str,
    template_id: uuid.UUID | None = None,
    intro: str | None = None,
    message_id: uuid.UUID | None = None,
    client_origin: str | None = None,
) -> dict[str, Any]:
    _check(fmt, locale)
    if fmt == "email" and message_id is None:
        raise Invalid("message_id_required", "the composer's message id (an e-mail is a message)")
    row = await ctx.conn.fetchrow(LOCK_SQL, proposal_id)
    if row is None:
        raise NotFound("proposal_not_found")
    digest = await current_hash(ctx, vault, row)
    if price_hash.lower() != digest:
        raise Conflict(
            "price_hash_mismatch",
            "this proposal changed since the text was rendered; review the new text before sending",
            current_price_hash=digest,
            current_reference=tr.reference(digest),
        )
    if fmt == "email":
        # Checked before anything is written (the same check as the message send, which runs last).
        trip_owner = await ctx.conn.fetchval(OWNER_SQL, row["trip_id"])
        require(ctx, Action.MESSAGE_SEND, object_kind="proposal", object_id=str(proposal_id),
                assigned=trip_owner is not None and trip_owner == ctx.member_id)  # fmt: skip
    lang = locale or await prep_packs.agency_locale(ctx)
    template = await _template_text(ctx, vault, template_id, intro)
    sent: dict[str, Any] | None = None
    link_path: str | None = None
    if row["status"] == TO_SEND[row["kind"]]:
        sent = await service.send(ctx, vault, proposal_id)
        link_path = sent.get("link_path")
        if sent.get("price_hash") != digest:  # pragma: no cover — the same record hashed twice in one transaction
            raise Conflict("price_hash_mismatch", "the version changed while it was sent")
        row = await ctx.conn.fetchrow(ROW_SQL, proposal_id)
    origin = _origin(client_origin)
    link_url = f"{origin}{link_path}" if link_path and origin else None
    rendered, fields = await _render(ctx, vault, row, digest, fmt, lang, template, link_url)
    out: dict[str, Any] = {
        "proposal_id": str(proposal_id),
        "status": row["status"],
        "format": fmt,
        "locale": lang,
        "price_hash": digest,
        "reference": tr.reference(digest),
        "link_path": link_path,
        "link_in_text": link_url is not None,
        "version_sent": sent is not None,
        "superseded": sent.get("superseded") if sent else None,
        "subject": rendered.subject,
        "body": rendered.body,
    }
    if fmt == "email":
        message = await conversations.send(ctx, vault, message_id, conversation_id=None, trip_id=row["trip_id"],
                                           subject=rendered.subject, body=rendered.body)  # fmt: skip
        out["message"] = message
    ctx.audit("proposal_text_send", "allowed", "proposal", str(proposal_id), reason=fmt,
              fields=tuple(fields))  # fmt: skip
    return out
