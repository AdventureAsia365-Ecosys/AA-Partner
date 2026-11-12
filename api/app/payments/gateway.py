"""The agency's own online gateway (AA-1072, item crm-03; BYO errata S247: the agency is the merchant, no Adventure Asia
account ever holds client money): connect the account, start a hosted payment for an invoice, take the gateway's
signed webhook. One gateway today, VNPay (provider ``vn_gateway``, pure protocol in :mod:`app.payments.vnpay`).

- **Connect** (:func:`connect`, owner ``PAYMENT_SETTINGS``): the merchant code goes on the agency's
  ``crm.payment_provider_account`` row (``external_account_id``, ``charges_enabled``, currency VND); the hash secret
  and the mode go into the agency-level ``vn_gateway`` ``crm.integration_credential``, sealed under the agency's
  integration DEK (only this module opens it: ``Vault.decrypt_secret`` is lint-allowed here). Mode ``sandbox``
  (VNPay's test host), ``live``, or ``stub`` (the simulator below; dev / test / local only, 422 ``mode_not_allowed``
  elsewhere). A disconnect (Settings > Payments, :func:`app.payments.service.disconnect`) stops new sessions and the
  gateway reads "not connected" (:func:`status` follows the active account); the credential stays, so a webhook of a
  session already started still verifies. A reconnect reuses the agency's last ``vn_gateway`` account row (reactivated
  with the new merchant code) instead of adding another one.
- **Start** (:func:`start_session`, the client link, after :func:`app.payments.claims.card_window`): the invoice's open
  amount, computed here from the invoice and its payments (never from the browser), in VND only (409
  ``currency_not_supported`` otherwise); one ``crm.payment_session`` (random merchant reference, 15 minutes); the
  answer is the signed gateway URL. The card is typed on the gateway's page only.
- **Webhook** (:func:`handle_ipn`, ``GET /crm/public/v1/hooks/vnpay/ipn``): the reference finds the session through
  :func:`app.tenancy.payment_session_tx`; the signature is checked with the agency's own secret **before** anything
  is written (``97``); a wrong merchant or amount is refused (``97`` / ``04``); a repeat is ``02``; a failure closes the
  session ``failed``; a success records the payment once (unique per gateway reference) through the same code as
  "Record payment" (:func:`app.payments.service.record_gateway_payment`: invoice paid, schedule row settled, the
  re-hold of a late deposit). **Never block on a business error**: when the invoice cannot take the money (paid or
  void meanwhile, above what is left, newer terms waiting) the money is still recorded
  (:func:`app.payments.service.record_unsettled_payment`), the session goes to ``review`` and the trip owner gets an
  urgent ``payment_review`` task; the gateway is answered ``00`` so it stops retrying.
- **Stub gateway** (:func:`stub_page`, :func:`stub_pay`; dev / test / local, and only for an account in mode ``stub``):
  a plain page of this API showing the amount with "Pay" and "Cancel"; a press builds a VNPay-shaped IPN signed with
  the agency's own secret and runs :func:`handle_ipn`, then sends the client back to the return page. The Dev live
  check of the whole flow without a merchant account; it never moves money.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import logging
import secrets
import uuid
from decimal import Decimal
from typing import Any

import asyncpg

from app.errors import Conflict, Invalid, NotFound, ServiceError
from app.payments import providers, vnpay
from app.payments import service as payments
from app.settings import Settings
from app.tenancy import AnyContext, TenantContext, payment_session_tx
from app.vault import KeyProvider, Vault

log = logging.getLogger(__name__)

PROVIDER = "vn_gateway"
SESSION_MINUTES = 15
STUB_ENVIRONMENTS = frozenset({"dev", "test", "local"})
IPN_PATH = "/crm/public/v1/hooks/vnpay/ipn"
STUB_PATH = "/crm/public/v1/pay/stub"
RETURN_PATH = "/c/p/paid"
MAX_DISPLAY_NAME = 80

CREDENTIAL_SQL = """
SELECT integration_credential_id, secret_enc, account_ref FROM crm.integration_credential
 WHERE provider = 'vn_gateway' AND member_id IS NULL AND deleted_at IS NULL
"""
INSERT_CREDENTIAL_SQL = """
INSERT INTO crm.integration_credential (integration_credential_id, provider, member_id, account_ref, secret_enc,
                                        status, last_checked_at, last_ok_at)
VALUES ($1, 'vn_gateway', NULL, $2, $3, 'connected', now(), now())
"""
UPDATE_CREDENTIAL_SQL = """
UPDATE crm.integration_credential
   SET account_ref = $2, secret_enc = $3, status = 'connected', failure_class = NULL, last_checked_at = now(),
       last_ok_at = now()
 WHERE integration_credential_id = $1
"""
# The agency's gateway account to (re)use: the active one, else the most recently connected one (a reconnect
# reactivates it, so connect / disconnect / connect never piles up account rows).
ACCOUNT_SQL = """
SELECT account_id, status FROM crm.payment_provider_account
 WHERE provider = 'vn_gateway' AND deleted_at IS NULL
 ORDER BY (status = 'active') DESC, connected_at DESC, account_id
 LIMIT 1
   FOR UPDATE
"""
ACTIVE_ACCOUNT_SQL = """
SELECT account_id FROM crm.payment_provider_account
 WHERE provider = 'vn_gateway' AND status = 'active' AND deleted_at IS NULL
 LIMIT 1
"""
INSERT_ACCOUNT_SQL = """
INSERT INTO crm.payment_provider_account (account_id, provider, display_name, external_account_id, default_currency,
                                          charges_enabled)
VALUES ($1, 'vn_gateway', $2, $3, 'VND', true)
"""
UPDATE_ACCOUNT_SQL = """
UPDATE crm.payment_provider_account SET display_name = $2, external_account_id = $3, charges_enabled = true
 WHERE account_id = $1
"""
REACTIVATE_ACCOUNT_SQL = """
UPDATE crm.payment_provider_account
   SET status = 'active', disconnected_at = NULL, connected_at = now(), display_name = $2, external_account_id = $3,
       charges_enabled = true
 WHERE account_id = $1 AND status = 'disconnected'
"""
OPEN_AMOUNT_SQL = """
SELECT i.total_amount - coalesce((SELECT sum(p.amount) FROM crm.payment p
                                   WHERE p.invoice_id = i.invoice_id AND p.entry_kind = 'payment'), 0)
  FROM crm.invoice i WHERE i.invoice_id = $1
"""
INSERT_SESSION_SQL = """
INSERT INTO crm.payment_session (session_id, invoice_id, trip_id, account_id, provider, txn_ref, amount, currency,
                                 expires_at)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
"""
SESSION_LOCK_SQL = """
SELECT s.session_id, s.invoice_id, s.trip_id, s.amount, s.currency, s.status, s.provider, s.txn_ref,
       s.expires_at, i.number, i.kind
  FROM crm.payment_session s JOIN crm.invoice i ON i.invoice_id = s.invoice_id
 WHERE s.session_id = $1
   FOR UPDATE OF s
"""
CLOSE_SQL = """
UPDATE crm.payment_session SET status = $2, closed_at = coalesce(closed_at, now()), response_code = $3,
       provider_ref = coalesce($4, provider_ref), payment_id = coalesce($5, payment_id)
 WHERE session_id = $1
"""


# ── connect ───────────────────────────────────────────────────────────────────────────────────────


def check_connect(*, tmn_code: Any, hash_secret: Any, mode: Any, display_name: Any, environment: str) -> dict[str, str]:
    """The checked values, or 422 with a field code (never the value)."""
    if mode not in vnpay.MODES:
        raise Invalid("invalid_mode", "sandbox, live or stub")
    if mode == "stub" and environment not in STUB_ENVIRONMENTS:
        raise Invalid("mode_not_allowed", "the stub gateway is for the Dev app only")
    code = tmn_code.strip() if isinstance(tmn_code, str) else ""
    if not vnpay.TMN_CODE.match(code):
        raise Invalid("invalid_tmn_code", "the merchant code (letters and digits) from the gateway")
    secret = hash_secret.strip() if isinstance(hash_secret, str) else ""
    if not 16 <= len(secret) <= vnpay.MAX_SECRET or any(c.isspace() for c in secret):
        raise Invalid("invalid_hash_secret", "the hash secret from the gateway (16 to 128 characters)")
    name = display_name.strip() if isinstance(display_name, str) and display_name.strip() else "Online payment"
    if len(name) > MAX_DISPLAY_NAME or "\n" in name:
        raise Invalid("invalid_display_name", "one line, at most 80 characters")
    return {"tmn_code": code, "hash_secret": secret, "mode": str(mode), "display_name": name}


def ipn_url(settings: Settings) -> str | None:
    """The IPN URL the agency enters in its gateway's merchant portal (the client host)."""
    origin = settings.messaging_hooks_origin()
    return f"{origin}{IPN_PATH}" if origin else None


async def connect(ctx: TenantContext, vault: Vault, settings: Settings, body: dict[str, Any]) -> dict[str, Any]:
    values = check_connect(tmn_code=body.get("tmn_code"), hash_secret=body.get("hash_secret"), mode=body.get("mode"),
                           display_name=body.get("display_name"), environment=settings.environment)  # fmt: skip
    secret = json.dumps({"hash_secret": values["hash_secret"], "mode": values["mode"]})
    current = await ctx.conn.fetchrow(CREDENTIAL_SQL)
    credential_id = current["integration_credential_id"] if current is not None else uuid.uuid4()
    sealed = await vault.encrypt_secret(credential_id, secret)
    if current is None:
        await ctx.conn.execute(INSERT_CREDENTIAL_SQL, credential_id, values["tmn_code"], sealed)
    else:
        await ctx.conn.execute(UPDATE_CREDENTIAL_SQL, credential_id, values["tmn_code"], sealed)
    found = await ctx.conn.fetchrow(ACCOUNT_SQL)
    fields: tuple[str, ...] = ("external_account_id", "charges_enabled")
    if found is None:
        account = uuid.uuid4()
        await ctx.conn.execute(INSERT_ACCOUNT_SQL, account, values["display_name"], values["tmn_code"])
    elif found["status"] == "active":
        account = found["account_id"]
        await ctx.conn.execute(UPDATE_ACCOUNT_SQL, account, values["display_name"], values["tmn_code"])
    else:  # S277 live check: a reconnect reuses the disconnected row instead of adding another one
        account = found["account_id"]
        await ctx.conn.execute(REACTIVATE_ACCOUNT_SQL, account, values["display_name"], values["tmn_code"])
        fields = ("status", *fields)
    ctx.audit("update", "allowed", "payment_provider_account", str(account), reason=f"vn_gateway_{values['mode']}",
              fields=fields)  # fmt: skip
    out = await payments.payment_methods(ctx)
    out["vn_gateway"] = {"mode": values["mode"], "ipn_url": ipn_url(settings)}
    return out


async def status(ctx: TenantContext, vault: Vault, settings: Settings) -> dict[str, Any]:
    """Settings > Payments: whether the gateway is connected, its mode and the IPN URL (never the secret). Connected =
    an **active** ``vn_gateway`` account (what the client's "Pay online" needs) with its credential: after a
    disconnect the credential stays for the webhooks of sessions already started, but the gateway is not connected."""
    out: dict[str, Any] = {"connected": False, "mode": None, "ipn_url": ipn_url(settings)}
    if await ctx.conn.fetchval(ACTIVE_ACCOUNT_SQL) is None:
        return out
    loaded = await load(ctx, vault)
    if loaded is not None:
        out.update(connected=True, mode=loaded[2])
    return out


async def load(ctx: AnyContext, vault: Vault) -> tuple[str, str, str] | None:
    """``(merchant code, hash secret, mode)`` of the agency's gateway credential, or None. Never returned to a
    caller outside this module's flows."""
    row = await ctx.conn.fetchrow(CREDENTIAL_SQL)
    if row is None:
        return None
    value = json.loads(await vault.decrypt_secret(row["integration_credential_id"], bytes(row["secret_enc"])))
    return str(row["account_ref"] or ""), str(value["hash_secret"]), str(value.get("mode") or "sandbox")


# ── start a hosted payment ────────────────────────────────────────────────────────────────────────


def _base_url(mode: str, settings: Settings) -> str:
    if mode == "stub":
        if settings.environment not in STUB_ENVIRONMENTS:
            raise Conflict("provider_not_available", "online payment is not available right now")
        return f"{settings.messaging_hooks_origin()}{STUB_PATH}"
    return vnpay.PAY_URLS[mode]


async def start_session(
    ctx: AnyContext,
    vault: Vault,
    settings: Settings,
    row: Any,
    account: Any,
    *,
    client_ip: str,
    locale: str,
    now: dt.datetime,
) -> dict[str, Any]:
    """One hosted payment of the invoice's open amount (``row`` is the locked client invoice the pay window allowed,
    ``account`` the agency's active gateway account)."""
    impl = providers.provider(account["provider"])  # 409 provider_not_available for a provider without code
    if impl.name != PROVIDER:
        raise Conflict("provider_not_available", "online payment is not available right now")
    currency = str(row["currency"]).strip()
    if currency != vnpay.CURRENCY:
        raise Conflict("currency_not_supported", "online payment takes VND only: use the bank details shown")
    loaded = await load(ctx, vault)
    if loaded is None:
        raise Conflict("provider_not_available", "online payment is not available right now")
    tmn_code, secret, mode = loaded
    origin = settings.messaging_hooks_origin()
    if not origin:
        raise Conflict("provider_not_available", "online payment is not available right now")
    amount = Decimal(await ctx.conn.fetchval(OPEN_AMOUNT_SQL, row["invoice_id"])).quantize(Decimal("1"))
    if amount <= 0:
        raise Conflict("invoice_not_payable", "this invoice is paid")
    session_id, ref = uuid.uuid4(), secrets.token_hex(12)
    expires = now + dt.timedelta(minutes=SESSION_MINUTES)
    await ctx.conn.execute(INSERT_SESSION_SQL, session_id, row["invoice_id"], row["trip_id"], account["account_id"],
                           PROVIDER, ref, amount, currency, expires)  # fmt: skip
    ctx.audit("invoice_card_session", "allowed", "invoice", str(row["invoice_id"]), reason=f"vn_gateway_{mode}")
    request = vnpay.PayRequest(
        tmn_code=tmn_code, secret=secret, txn_ref=ref, amount=amount,
        order_info=vnpay.order_info(row["number"], row["kind"]), return_url=f"{origin}{RETURN_PATH}",
        client_ip=client_ip, locale="vn" if locale == "vi" else "en", created=now, expires=expires,
    )  # fmt: skip
    return {"redirect_url": vnpay.pay_url(_base_url(mode, settings), request), "expires_at": expires.isoformat()}


# ── the webhook ───────────────────────────────────────────────────────────────────────────────────


async def handle_ipn(pool: asyncpg.Pool, keys: KeyProvider, fields: dict[str, str]) -> dict[str, str]:
    """VNPay's IPN: always a VNPay answer body, never an error page (the gateway retries on anything else)."""
    try:
        notice = vnpay.read_notice(fields)
    except vnpay.NoticeInvalid:
        return vnpay.ipn_answer(vnpay.NOT_FOUND if not fields.get("vnp_TxnRef") else vnpay.OTHER)
    if not vnpay.signature_shaped(fields):
        return vnpay.ipn_answer(vnpay.BAD_SIGNATURE)  # AA-1092: unsigned, so no lookup and no secret is opened
    try:
        async with payment_session_tx(pool, notice.txn_ref) as (ctx, ref):
            return await _apply(ctx, Vault(keys, ctx), ref.session_id, notice, fields)
    except NotFound:
        return vnpay.ipn_answer(vnpay.NOT_FOUND)


async def _apply(ctx: AnyContext, vault: Vault, session_id: uuid.UUID, notice: vnpay.Notice,
                 fields: dict[str, str]) -> dict[str, str]:  # fmt: skip
    loaded = await load(ctx, vault)
    if loaded is None or not vnpay.verify(fields, loaded[1]) or notice.tmn_code != loaded[0]:
        ctx.audit("payment_webhook", "denied", "payment_session", str(session_id), reason="invalid_signature")
        return vnpay.ipn_answer(vnpay.BAD_SIGNATURE)
    session = await ctx.conn.fetchrow(SESSION_LOCK_SQL, session_id)
    if session is None:
        return vnpay.ipn_answer(vnpay.NOT_FOUND)
    if Decimal(session["amount"]) != notice.amount:
        ctx.audit("payment_webhook", "denied", "payment_session", str(session_id), reason="amount_mismatch")
        return vnpay.ipn_answer(vnpay.BAD_AMOUNT)
    late_success = session["status"] == "failed" and notice.succeeded
    if session["status"] != "open" and not late_success:
        return vnpay.ipn_answer(vnpay.ALREADY)
    if not notice.succeeded:
        await ctx.conn.execute(CLOSE_SQL, session_id, "failed", notice.response_code[:32], None, None)
        ctx.audit("payment_webhook", "allowed", "payment_session", str(session_id), reason="payment_failed")
        return vnpay.ipn_answer(vnpay.CONFIRMED)
    reference = notice.transaction_no or session["txn_ref"]
    try:
        async with ctx.conn.transaction():  # savepoint: a business refusal undoes only the settle attempt
            if late_success:
                raise Conflict("session_closed", "the session was closed before the money arrived")
            out = await payments.record_gateway_payment(
                ctx, vault, session["invoice_id"], amount=notice.amount, provider_name=PROVIDER, reference=reference
            )
            await ctx.conn.execute(CLOSE_SQL, session_id, "paid", notice.response_code, reference,
                                   uuid.UUID(out["payment_id"]))  # fmt: skip
    except asyncpg.UniqueViolationError:
        return vnpay.ipn_answer(vnpay.ALREADY)  # this gateway transaction is recorded already
    except ServiceError as exc:
        payment_id = await payments.record_unsettled_payment(
            ctx, session["invoice_id"], amount=notice.amount, provider_name=PROVIDER, reference=reference,
            reason=exc.code,
        )  # fmt: skip
        await ctx.conn.execute(CLOSE_SQL, session_id, "review", notice.response_code, reference, payment_id)
        log.warning("payment_webhook_review session_id=%s code=%s", session_id, exc.code)
    ctx.audit("payment_webhook", "allowed", "payment_session", str(session_id), reason="payment_received")
    return vnpay.ipn_answer(vnpay.CONFIRMED)


# ── the stub gateway (dev / test / local) ─────────────────────────────────────────────────────────


async def _stub_session(ctx: AnyContext, vault: Vault, settings: Settings, session_id: uuid.UUID) -> tuple[Any, Any]:
    if settings.environment not in STUB_ENVIRONMENTS:
        raise NotFound("session_not_found")
    loaded = await load(ctx, vault)
    if loaded is None or loaded[2] != "stub":
        raise NotFound("session_not_found")  # never simulate a sandbox or live merchant
    session = await ctx.conn.fetchrow(SESSION_LOCK_SQL, session_id)
    if session is None:
        raise NotFound("session_not_found")
    return session, loaded


async def stub_page(pool: asyncpg.Pool, keys: KeyProvider, settings: Settings, fields: dict[str, str]) -> str:
    """The simulator's page for a signed pay URL (checked like VNPay would: the agency's own secret)."""
    ref = fields.get("vnp_TxnRef") or ""
    async with payment_session_tx(pool, ref) as (ctx, found):
        vault = Vault(keys, ctx)
        session, loaded = await _stub_session(ctx, vault, settings, found.session_id)
        if not vnpay.verify(fields, loaded[1]):
            raise NotFound("session_not_found")
        amount = f"{Decimal(session['amount']):,.0f} {session['currency']}"
        state = session["status"]
    action = html.escape(f"{STUB_PATH}/{ref}", quote=True)
    buttons = (
        f'<form method="post" action="{action}"><button name="outcome" value="paid">Pay (simulated)</button> '
        f'<button name="outcome" value="cancel">Cancel</button></form>'
        if state == "open"
        else "<p>This payment is closed.</p>"
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; "
        "form-action 'self'\"><title>Test payment gateway</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:420px;margin:48px auto;padding:0 16px;color:#1d1d1f}"
        "button{font-size:16px;padding:10px 18px;margin:8px 8px 0 0}</style></head><body>"
        "<h1>Test payment gateway</h1><p>No real money moves. This page stands in for the agency's gateway on the "
        f"Dev app.</p><p>Amount: <strong>{html.escape(amount)}</strong></p>{buttons}</body></html>"
    )


async def stub_pay(pool: asyncpg.Pool, keys: KeyProvider, settings: Settings, ref: str, outcome: str) -> str:
    """A press on the simulator: a VNPay-shaped IPN signed with the agency's own secret, through :func:`handle_ipn`.
    Returns where the client goes next (the return page)."""
    async with payment_session_tx(pool, ref) as (ctx, found):
        session, loaded = await _stub_session(ctx, Vault(keys, ctx), settings, found.session_id)
        fields = vnpay.notice_fields(
            tmn_code=loaded[0], txn_ref=ref, amount=Decimal(session["amount"]), succeeded=outcome == "paid",
            transaction_no=str(secrets.randbelow(10**8) + 10**7), when=dt.datetime.now(dt.UTC), secret=loaded[1],
        )  # fmt: skip
    await handle_ipn(pool, keys, fields)
    return f"{settings.messaging_hooks_origin()}{RETURN_PATH}"
