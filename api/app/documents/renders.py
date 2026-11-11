"""The server-side PDF documents of a trip (AA-1070 item crm-21): pure builders from plain views to PDF bytes
(:mod:`app.documents.pdf`). The data is gathered by :mod:`app.documents.pdf_job`.

- **Booking confirmation** (``confirmation_pdf``): the agency (its name heads the page: the agency is the brand, design
  §2.9), the trip dates and party, the lead traveller, and every confirmed service (title, dates, the supplier's
  confirmation number). No price, no B2B price, no margin.
- **Invoice** (``invoice_pdf``): the same content as the printable HTML invoice (:mod:`app.invoices.render`):
  number, dates, bill-to, charge lines, tax, totals, payments, the agency's payment instructions; DRAFT / VOID banner.
- **Proposal** (``proposal_pdf``): the version's text proposal (the e-mail text of :mod:`app.proposals.text_render`,
  the price visibility the agent chose).
- **Voucher** (``voucher``): one confirmed service for the travellers (:mod:`app.travel_ops.vouchers`).

Labels are English or Vietnamese (the agency's locale); the content is the agency's own wording.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from typing import Any

from app.documents.pdf import MUTED, Document

STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "confirmation": "Booking confirmation",
        "invoice": "Invoice",
        "proposal": "Proposal",
        "voucher": "Service voucher",
        "trip": "Trip",
        "dates": "Dates",
        "travellers": "Travellers",
        "lead": "Lead traveller",
        "party": "Party",
        "services": "Confirmed services",
        "service": "Service",
        "supplier": "Supplier",
        "confirmation_no": "Confirmation no.",
        "none": "No confirmed service yet.",
        "number": "Number",
        "issued": "Issued",
        "due": "Due",
        "bill_to": "Bill to",
        "description": "Description",
        "qty": "Qty",
        "unit": "Unit price",
        "amount": "Amount",
        "subtotal": "Subtotal",
        "total": "Amount due",
        "paid": "Paid",
        "refunded": "Refunded",
        "balance": "Balance",
        "covers": "This invoice covers",
        "draft": "DRAFT: not issued",
        "void": "VOID",
        "voucher_no": "Voucher no.",
        "pax": "Travellers",
        "time": "Start time",
        "pickup": "Pick-up",
        "dropoff": "Drop-off",
        "pnr": "Booking reference",
        "contact": "Contact",
        "aa_ref": "Adventure Asia booking",
        "note": "Note",
        "present": "Show this voucher (printed or on your phone) to the supplier.",
        "lead_role": "lead",
        "adults": "{n} adults",
        "children": "{n} children",
    },
    "vi": {
        "confirmation": "Xác nhận đặt chỗ",
        "invoice": "Hoá đơn",
        "proposal": "Đề xuất chuyến đi",
        "voucher": "Phiếu dịch vụ",
        "trip": "Chuyến đi",
        "dates": "Ngày",
        "travellers": "Khách",
        "lead": "Khách đại diện",
        "party": "Đoàn",
        "services": "Dịch vụ đã xác nhận",
        "service": "Dịch vụ",
        "supplier": "Nhà cung cấp",
        "confirmation_no": "Số xác nhận",
        "none": "Chưa có dịch vụ nào được xác nhận.",
        "number": "Số",
        "issued": "Ngày phát hành",
        "due": "Hạn thanh toán",
        "bill_to": "Người nhận",
        "description": "Mô tả",
        "qty": "SL",
        "unit": "Đơn giá",
        "amount": "Thành tiền",
        "subtotal": "Tạm tính",
        "total": "Số tiền phải trả",
        "paid": "Đã trả",
        "refunded": "Đã hoàn",
        "balance": "Còn lại",
        "covers": "Hoá đơn này gồm",
        "draft": "BẢN NHÁP: chưa phát hành",
        "void": "ĐÃ HUỶ",
        "voucher_no": "Số phiếu",
        "pax": "Số khách",
        "time": "Giờ bắt đầu",
        "pickup": "Điểm đón",
        "dropoff": "Điểm trả",
        "pnr": "Mã đặt chỗ",
        "contact": "Liên hệ",
        "aa_ref": "Mã đặt chỗ Adventure Asia",
        "note": "Ghi chú",
        "present": "Xuất trình phiếu này (bản in hoặc trên điện thoại) cho nhà cung cấp.",
        "lead_role": "đại diện",
        "adults": "{n} người lớn",
        "children": "{n} trẻ em",
    },
}
MONTHS = {
    "en": ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
    "vi": tuple(f"thg {n}" for n in range(1, 13)),
}


def _s(locale: str) -> dict[str, str]:
    return STRINGS["vi" if locale == "vi" else "en"]


def day(value: str | dt.date | None, locale: str) -> str:
    if value in (None, ""):
        return ""
    d = value if isinstance(value, dt.date) else dt.date.fromisoformat(str(value)[:10])
    return f"{d.day} {MONTHS['vi' if locale == 'vi' else 'en'][d.month - 1]} {d.year}"


def span(start: Any, end: Any, locale: str) -> str:
    a, b = day(start, locale), day(end, locale)
    if a and b and a != b:
        return f"{a} – {b}"
    return a or b


def amount(value: Any, currency: str) -> str:
    if value in (None, ""):
        return ""
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return str(value)
    return f"{number:,.2f} {currency}"


def _doc(title: str, agency: str | None) -> Document:
    doc = Document(title=title, author=agency, footer=f"{agency} · {title}" if agency else title)
    if agency:
        doc.text(agency, size=12, bold=True, gap=2)
    doc.heading(title)
    return doc


def confirmation(view: dict[str, Any], locale: str) -> bytes:
    """``view``: agency_name, trip_title, start_date, end_date, adults, children, party [{name, role}], lines [{title,
    service_from, service_to, supplier, confirmation_no}]."""
    s = _s(locale)
    doc = _doc(s["confirmation"], view.get("agency_name"))
    party = ", ".join(f"{p['name']} ({s['lead_role']})" if p.get("role") == "lead" else p["name"]
                      for p in view.get("party") or [])  # fmt: skip
    size = s["adults"].format(n=view.get("adults") or 0)
    if view.get("children"):
        size += ", " + s["children"].format(n=view["children"])
    doc.pairs([
        (s["trip"], view.get("trip_title")),
        (s["dates"], span(view.get("start_date"), view.get("end_date"), locale)),
        (s["party"], size),
        (s["travellers"], party),
    ])  # fmt: skip
    doc.rule()
    doc.text(s["services"], size=12, bold=True)
    lines = view.get("lines") or []
    if not lines:
        doc.text(s["none"], color=MUTED)
    else:
        doc.table(
            [s["service"], s["dates"], s["supplier"], s["confirmation_no"]],
            [
                [
                    ln["title"],
                    span(ln.get("service_from"), ln.get("service_to"), locale),
                    ln.get("supplier") or "",
                    ln.get("confirmation_no") or "",
                ]
                for ln in lines
            ],  # fmt: skip
            widths=[0.4, 0.22, 0.2, 0.18],
        )
    return doc.render()


def invoice(view: dict[str, Any], locale: str, *, agency: str | None, bill_to: str | None,
            pay_instructions: list[dict[str, str]]) -> bytes:  # fmt: skip
    """``view``: :func:`app.invoices.service.invoice_view` with its lines and payments."""
    s = _s(locale)
    cur = view["currency"]
    doc = _doc(s["invoice"], agency)
    if view["status"] == "draft":
        doc.text(s["draft"], bold=True, color=(0.71, 0.14, 0.09))
    elif view["status"] == "void":
        doc.text(f"{s['void']}: {view.get('void_reason') or ''}", bold=True, color=(0.71, 0.14, 0.09))
    doc.pairs([
        (s["number"], view.get("number")),
        (s["issued"], day(view.get("issue_date"), locale)),
        (s["due"], day(view.get("due_on"), locale)),
        (s["bill_to"], bill_to),
    ])  # fmt: skip
    charges = [ln for ln in view.get("lines") or [] if ln["kind"] == "charge"]
    included = [ln for ln in view.get("lines") or [] if ln["kind"] == "included"]
    doc.table(
        [s["description"], s["qty"], s["unit"], s["amount"]],
        [
            [
                ln["description"],
                str(ln.get("quantity") or ""),
                amount(ln.get("unit_amount"), cur),
                amount(ln.get("amount"), cur),
            ]
            for ln in charges
        ],  # fmt: skip
        widths=[0.5, 0.1, 0.2, 0.2],
        right=(1, 2, 3),
    )
    tax = f"{view.get('tax_label') or ''} {view.get('tax_rate') or ''}%".strip()
    totals: list[list[str]] = []
    if not (view.get("tax_inclusive") and Decimal(str(view.get("tax_amount") or 0)) == 0):
        totals.append([s["subtotal"], amount(view.get("subtotal_amount"), cur)])
        totals.append([tax, amount(view.get("tax_amount"), cur)])
    totals.append([s["total"], amount(view.get("total_amount"), cur)])
    for p in view.get("payments") or []:
        refund = p.get("entry_kind", "payment") != "payment"
        label = f"{s['refunded'] if refund else s['paid']} {day(p.get('received_on'), locale)}"
        totals.append([label, ("+ " if refund else "- ") + amount(p.get("amount"), cur)])
    if view.get("payments") and view.get("balance_amount") is not None:
        totals.append([s["balance"], amount(view.get("balance_amount"), cur)])
    doc.table(["", ""], totals, widths=[0.7, 0.3], right=(1,), show_header=False)
    if included:
        doc.text(s["covers"], bold=True)
        for ln in included:
            doc.text(f"• {ln['description']}", indent=8, gap=1)
    if view.get("memo"):
        doc.space()
        doc.text(view["memo"])
    for method in pay_instructions:
        doc.text(method["display_name"], bold=True, gap=1)
        doc.text(method["instructions"])
    return doc.render()


def proposal(title: str, body: str, *, agency: str | None, locale: str) -> bytes:
    s = _s(locale)
    doc = _doc(s["proposal"], agency)
    doc.text(title, size=12, bold=True)
    for paragraph in body.strip().split("\n\n"):
        doc.text(paragraph.replace("**", ""), gap=6)
    return doc.render()


def voucher(trip: dict[str, Any], v: dict[str, Any], locale: str) -> bytes:
    """``trip``: :func:`app.travel_ops.vouchers.trip_vouchers` (agency_name, lead_traveller, pax, trip_title); ``v``:
    one of its vouchers."""
    s = _s(locale)
    doc = _doc(s["voucher"], trip.get("agency_name"))
    supplier = v.get("supplier") or {}
    doc.pairs([
        (s["voucher_no"], v.get("voucher_no")),
        (s["service"], v.get("title")),
        (s["dates"], span(v.get("service_from"), v.get("service_to"), locale)),
        (s["time"], v.get("service_time")),
        (s["lead"], trip.get("lead_traveller")),
        (s["pax"], str(trip.get("pax") or "")),
        (s["pickup"], v.get("pickup")),
        (s["dropoff"], v.get("dropoff")),
        (s["supplier"], supplier.get("name")),
        (s["contact"], supplier.get("contact_name")),
        (s["confirmation_no"], v.get("confirmation_no")),
        (s["pnr"], v.get("pnr")),
        (s["aa_ref"], supplier.get("aa_booking_ref")),
        (s["note"], v.get("note")),
    ])  # fmt: skip
    doc.rule()
    doc.small(s["present"])
    return doc.render()
