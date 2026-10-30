"""The printable invoice (card CRM-23 slice 2, AA-952): one self-contained HTML document the agency prints or saves as
PDF from the browser.

- Every value is HTML-escaped; the document carries a CSP ``<meta>`` that allows inline styles only (no script, no
  remote load, no form), so a typed description can never run in the page that opens it.
- The agency is the brand (design §2.9): its display name heads the document; no Adventure Asia mark.
- Never shown: per-line prices of the trip's booking lines, the B2B price, any margin (design §2.9 rule 3): included
  lines are titles only.
- A draft prints with a DRAFT banner and no number; a void invoice with VOID and its reason.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from html import escape
from typing import Any

CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; form-action 'none'; base-uri 'none'"
KIND_TITLE = {
    "deposit": "Deposit invoice",
    "balance": "Balance invoice",
    "service_fee": "Service fee invoice",
    "adjustment": "Invoice",
}
STYLE = """
body{font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:#1d232b;margin:0;padding:32px;
background:#fff}
main{max-width:760px;margin:0 auto}
header{display:flex;justify-content:space-between;gap:24px;flex-wrap:wrap;border-bottom:2px solid #1d232b;
padding-bottom:16px}
h1{font-size:22px;margin:0 0 4px}
.agency{font-size:18px;font-weight:600}
.meta{font-size:14px;line-height:1.6;text-align:right}
.banner{margin:16px 0;padding:8px 12px;border:2px solid #b42318;color:#b42318;font-weight:700;letter-spacing:.08em}
.billto{margin:20px 0;font-size:14px}
table{width:100%;border-collapse:collapse;margin-top:16px;font-size:14px}
th,td{padding:8px 6px;border-bottom:1px solid #d5d9de;text-align:left;vertical-align:top}
td.num,th.num{text-align:right;white-space:nowrap}
.totals{margin-left:auto;margin-top:16px;width:300px;font-size:14px}
.totals td{border:none;padding:4px 6px}
.totals tr.total td{font-weight:700;font-size:16px;border-top:2px solid #1d232b}
.included{margin-top:20px;font-size:13px;color:#4a5563}
.included ul{margin:6px 0 0 18px;padding:0}
.notes{margin-top:20px;font-size:13px;white-space:pre-wrap}
@media print{body{padding:0}}
"""


def _amount(value: str | None, currency: str) -> str:
    if value is None:
        return ""
    return f"{currency} {Decimal(value):,.2f}"


def _date(value: str | None) -> str:
    if not value:
        return ""
    return dt.date.fromisoformat(value[:10]).strftime("%d %b %Y")


def render(
    invoice: dict[str, Any],
    *,
    agency_name: str | None,
    bill_to: str | None,
    pay_instructions: list[dict[str, str]] | None = None,
    payments: list[dict[str, Any]] | None = None,
    balance: str | None = None,
) -> str:
    """The HTML document of one invoice view (:func:`app.invoices.service.invoice_view` with its lines)."""
    e = escape
    cur = invoice["currency"]
    status = invoice["status"]
    title = KIND_TITLE.get(invoice["kind"], "Invoice")
    charges = [line for line in invoice.get("lines", []) if line["kind"] == "charge"]
    included = [line for line in invoice.get("lines", []) if line["kind"] == "included"]
    banner = ""
    if status == "draft":
        banner = '<div class="banner">DRAFT: not issued</div>'
    elif status == "void":
        banner = f'<div class="banner">VOID: {e(invoice.get("void_reason") or "")}</div>'
    meta = [f"<div>Number: <strong>{e(invoice['number'])}</strong></div>"] if invoice.get("number") else []
    if invoice.get("issue_date"):
        meta.append(f"<div>Issued: {e(_date(invoice['issue_date']))}</div>")
    if invoice.get("due_on"):
        meta.append(f"<div>Due: {e(_date(invoice['due_on']))}</div>")
    rows = "".join(
        f'<tr><td>{e(line["description"])}</td><td class="num">{e(line["quantity"] or "")}</td>'
        f'<td class="num">{e(_amount(line["unit_amount"], cur))}</td>'
        f'<td class="num">{e(_amount(line["amount"], cur))}</td></tr>'
        for line in charges
    )
    rate = invoice["tax_rate"]
    tax_name = f"{invoice['tax_label']} {rate}%"
    if invoice["tax_inclusive"]:
        totals = (
            f'<tr><td>Total</td><td class="num">{e(_amount(invoice["total_amount"], cur))}</td></tr>'
            if Decimal(invoice["tax_amount"]) == 0
            else f'<tr><td>Subtotal</td><td class="num">{e(_amount(invoice["subtotal_amount"], cur))}</td></tr>'
            f'<tr><td>{e(tax_name)} (included)</td><td class="num">{e(_amount(invoice["tax_amount"], cur))}</td></tr>'
        )
    else:
        totals = (
            f'<tr><td>Subtotal</td><td class="num">{e(_amount(invoice["subtotal_amount"], cur))}</td></tr>'
            f'<tr><td>{e(tax_name)}</td><td class="num">{e(_amount(invoice["tax_amount"], cur))}</td></tr>'
        )
    due = e(_amount(invoice["total_amount"], cur))
    totals += f'<tr class="total"><td>Amount due</td><td class="num">{due}</td></tr>'
    paid_rows = ""
    for p in payments or []:
        sign = "-" if p.get("entry_kind", "payment") == "payment" else "+"
        label = "Paid" if sign == "-" else "Refunded"
        paid_rows += (
            f"<tr><td>{label} {e(_date(p.get('received_on')))}</td>"
            f'<td class="num">{sign} {e(_amount(p["amount"], cur))}</td></tr>'
        )
    if paid_rows:
        totals += paid_rows + (
            f'<tr class="total"><td>Balance</td><td class="num">{e(_amount(balance, cur))}</td></tr>'
            if balance is not None
            else ""
        )
    included_html = ""
    if included:
        items = "".join(f"<li>{e(line['description'])}</li>" for line in included)
        included_html = f'<div class="included">This invoice covers:<ul>{items}</ul></div>'
    notes = []
    if invoice.get("memo"):
        notes.append(f'<div class="notes">{e(invoice["memo"])}</div>')
    for method in pay_instructions or []:
        name, text = e(method["display_name"]), e(method["instructions"])
        notes.append(f'<div class="notes"><strong>{name}</strong>\n{text}</div>')
    bill = f'<div class="billto">Bill to: <strong>{e(bill_to)}</strong></div>' if bill_to else ""
    doc_title = e(invoice.get("number") or title)
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta http-equiv="Content-Security-Policy" content="{CSP}">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="referrer" content="no-referrer">'
        f"<title>{doc_title}</title><style>{STYLE}</style></head><body><main>"
        f'<header><div><div class="agency">{e(agency_name or "")}</div><h1>{e(title)}</h1></div>'
        f'<div class="meta">{"".join(meta)}</div></header>{banner}{bill}'
        '<table><thead><tr><th>Description</th><th class="num">Qty</th><th class="num">Unit</th>'
        f'<th class="num">Amount</th></tr></thead><tbody>{rows}</tbody></table>'
        f'<table class="totals"><tbody>{totals}</tbody></table>{included_html}{"".join(notes)}'
        "</main></body></html>"
    )
