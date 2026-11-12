"""VNPay payment gateway 2.1.0, pure functions (AA-1072, item crm-03): the signed pay URL of a hosted payment and the
check of the signed IPN (the gateway's server-to-server notice). No I/O here: :mod:`app.payments.gateway` reads the
agency's credential and writes the rows.

The agency brings its own VNPay merchant account (BYO, errata S247): the merchant code (``vnp_TmnCode``) and the hash
secret are the agency's, sealed in its ``vn_gateway`` credential. The card is typed on VNPay's own page; the CRM
never sees card data.

Signature (VNPay 2.1.0): every ``vnp_*`` field except ``vnp_SecureHash`` / ``vnp_SecureHashType``, sorted by name,
each ``quote_plus(name)=quote_plus(value)``, joined with ``&``; HMAC-SHA512 of that string under the hash secret, hex.
The IPN is checked the same way, compared in constant time.

IPN answers (VNPay's codes, the body the gateway expects): ``00`` confirmed, ``01`` order not found, ``02`` already
confirmed, ``04`` invalid amount, ``97`` invalid signature, ``99`` other error. A payment succeeded when both
``vnp_ResponseCode`` and ``vnp_TransactionStatus`` are ``00``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import re
import unicodedata
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

VERSION = "2.1.0"
MODES = ("sandbox", "live", "stub")
PAY_URLS = {
    "sandbox": "https://sandbox.vnpayment.vn/paymentv2/vpcpay.html",
    "live": "https://pay.vnpay.vn/vpcpay.html",
}
CURRENCY = "VND"
VN_TZ = dt.timezone(dt.timedelta(hours=7))  # VNPay reads its dates in GMT+7
TMN_CODE = re.compile(r"^[A-Za-z0-9]{3,32}$")
TXN_REF = re.compile(r"^[A-Za-z0-9]{16,40}$")
# AA-1092: an HMAC-SHA512 hex digest. VNPay signs every IPN; a request without one of this shape is never VNPay's.
SECURE_HASH = re.compile(r"[0-9A-Fa-f]{128}")
MAX_SECRET = 128
SKIP = ("vnp_SecureHash", "vnp_SecureHashType")

CONFIRMED = "00"
NOT_FOUND = "01"
ALREADY = "02"
BAD_AMOUNT = "04"
BAD_SIGNATURE = "97"
OTHER = "99"
MESSAGES = {
    CONFIRMED: "Confirm Success",
    NOT_FOUND: "Order not found",
    ALREADY: "Order already confirmed",
    BAD_AMOUNT: "Invalid amount",
    BAD_SIGNATURE: "Invalid signature",
    OTHER: "Unknow error",
}


def ipn_answer(code: str) -> dict[str, str]:
    """The JSON body VNPay expects back from an IPN."""
    return {"RspCode": code, "Message": MESSAGES.get(code, MESSAGES[OTHER])}


def _canonical(fields: Mapping[str, str]) -> str:
    items = sorted((k, v) for k, v in fields.items() if k.startswith("vnp_") and k not in SKIP and v != "")
    return "&".join(f"{urllib.parse.quote_plus(k)}={urllib.parse.quote_plus(str(v))}" for k, v in items)


def sign(fields: Mapping[str, str], secret: str) -> str:
    return hmac.new(secret.encode(), _canonical(fields).encode(), hashlib.sha512).hexdigest()


def signature_shaped(fields: Mapping[str, str]) -> bool:
    """AA-1092: the notice carries a ``vnp_SecureHash`` of the HMAC-SHA512 hex shape (checked before any database work:
    an unsigned or garbled request is refused without a lookup and counted against its address)."""
    return bool(SECURE_HASH.fullmatch(fields.get("vnp_SecureHash") or ""))  # fullmatch: no trailing newline


def verify(fields: Mapping[str, str], secret: str) -> bool:
    given = fields.get("vnp_SecureHash") or ""
    if not given or not secret:
        return False
    return hmac.compare_digest(given.lower(), sign(fields, secret))


def amount_field(amount: Decimal) -> str:
    """VNPay's ``vnp_Amount``: the VND amount x 100, an integer (VND has no minor unit)."""
    return str(int((amount * 100).to_integral_value()))


def stamp(when: dt.datetime) -> str:
    return when.astimezone(VN_TZ).strftime("%Y%m%d%H%M%S")


@dataclass(frozen=True)
class PayRequest:
    tmn_code: str
    secret: str
    txn_ref: str
    amount: Decimal
    order_info: str
    return_url: str
    client_ip: str
    locale: str  # "vn" | "en"
    created: dt.datetime
    expires: dt.datetime


def order_info(invoice_number: str | None, kind: str) -> str:
    """The order description shown on VNPay's page: ASCII only, no client data (VNPay refuses accents: Vietnamese
    letters lose their marks, anything else is dropped)."""
    text = f"Invoice {invoice_number or ''} {kind}".strip().replace("đ", "d").replace("Đ", "D")
    plain = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return re.sub(r"[^A-Za-z0-9 _.-]", "", plain)[:200] or "Payment"


def pay_fields(req: PayRequest) -> dict[str, str]:
    return {
        "vnp_Version": VERSION,
        "vnp_Command": "pay",
        "vnp_TmnCode": req.tmn_code,
        "vnp_Amount": amount_field(req.amount),
        "vnp_CurrCode": CURRENCY,
        "vnp_TxnRef": req.txn_ref,
        "vnp_OrderInfo": req.order_info,
        "vnp_OrderType": "other",
        "vnp_Locale": "vn" if req.locale == "vn" else "en",
        "vnp_ReturnUrl": req.return_url,
        "vnp_IpAddr": req.client_ip or "127.0.0.1",
        "vnp_CreateDate": stamp(req.created),
        "vnp_ExpireDate": stamp(req.expires),
    }


def pay_url(base: str, req: PayRequest) -> str:
    """The signed URL the client is sent to (``base`` = the sandbox / live page, or the stub page)."""
    fields = pay_fields(req)
    query = _canonical(fields)
    return f"{base}?{query}&vnp_SecureHash={sign(fields, req.secret)}"


@dataclass(frozen=True)
class Notice:
    """A signed IPN, read: the reference, the amount, whether the payment succeeded and the gateway's codes."""

    txn_ref: str
    amount: Decimal
    succeeded: bool
    response_code: str
    transaction_no: str | None
    tmn_code: str


class NoticeInvalid(ValueError):
    """The IPN is not a well-formed VNPay notice (missing or malformed fields)."""


def read_notice(fields: Mapping[str, str]) -> Notice:
    """The fields of an IPN, checked for shape only (the signature is checked by :func:`verify` with the agency's
    secret, once the reference named the agency)."""
    ref = fields.get("vnp_TxnRef") or ""
    if not TXN_REF.match(ref):
        raise NoticeInvalid("vnp_TxnRef")
    raw = fields.get("vnp_Amount") or ""
    if not raw.isdigit() or len(raw) > 18:
        raise NoticeInvalid("vnp_Amount")
    code = fields.get("vnp_ResponseCode") or ""
    status = fields.get("vnp_TransactionStatus") or ""
    if not re.fullmatch(r"[A-Za-z0-9]{1,8}", code):
        raise NoticeInvalid("vnp_ResponseCode")
    number = fields.get("vnp_TransactionNo") or None
    if number is not None and not re.fullmatch(r"[A-Za-z0-9]{1,40}", number):
        raise NoticeInvalid("vnp_TransactionNo")
    return Notice(
        txn_ref=ref,
        amount=Decimal(raw) / 100,
        succeeded=code == "00" and status == "00",
        response_code=code,
        transaction_no=number if number and number != "0" else None,
        tmn_code=fields.get("vnp_TmnCode") or "",
    )


def notice_fields(*, tmn_code: str, txn_ref: str, amount: Decimal, succeeded: bool, transaction_no: str,
                  when: dt.datetime, secret: str) -> dict[str, str]:  # fmt: skip
    """A signed IPN of the shape VNPay sends: the stub gateway (dev / test / local only) and the tests build one."""
    code = "00" if succeeded else "24"  # 24 = the client cancelled on the gateway page
    fields = {
        "vnp_Amount": amount_field(amount),
        "vnp_BankCode": "STUB",
        "vnp_OrderInfo": "Stub payment",
        "vnp_PayDate": stamp(when),
        "vnp_ResponseCode": code,
        "vnp_TmnCode": tmn_code,
        "vnp_TransactionNo": transaction_no if succeeded else "0",
        "vnp_TransactionStatus": "00" if succeeded else "02",
        "vnp_TxnRef": txn_ref,
    }
    fields["vnp_SecureHash"] = sign(fields, secret)
    return fields
