// AA-952 (CRM-23 slices 2-3): the invoice and refund helpers (refusal wording, who bills, the schedule rows still to invoice, the
// typed-lines body) and the BFF allow-list lines of the invoice routes.
import { describe, expect, it } from "vitest";
import { matchBff } from "@/lib/bff";
import { MESSAGES } from "@/lib/i18n";
import {
  canBill,
  canDecideRefund,
  invoiceErrorKey,
  linesBody,
  refundStep,
  STATUSES,
  uninvoicedRows,
  type Invoice,
  type RefundRequest,
} from "@/lib/invoices";

const ID = "00000000-0000-4000-8000-0000000000e1";
const ID2 = "00000000-0000-4000-8000-0000000000e2";

function invoice(over: Partial<Invoice>): Invoice {
  return {
    invoice_id: ID,
    trip_id: ID,
    kind: "deposit",
    status: "draft",
    number: null,
    schedule_id: ID,
    proposal_id: ID,
    currency: "USD",
    subtotal_amount: "300.00",
    tax_amount: "0.00",
    total_amount: "300.00",
    tax_rate: "0",
    tax_label: "Tax",
    tax_inclusive: true,
    memo: null,
    due_on: null,
    issue_date: null,
    issued_at: null,
    paid_at: null,
    voided_at: null,
    void_reason: null,
    created_at: "2026-10-30T00:00:00Z",
    actions: ["edit", "issue", "void"],
    ...over,
  };
}

describe("invoice helpers", () => {
  it("words every refusal code and status in both languages", () => {
    for (const code of ["invoice_exists", "invoice_not_draft", "invoice_has_payments", "service_fee_disabled", "teapot"]) {
      const key = invoiceErrorKey(code) as keyof typeof MESSAGES.en;
      expect(MESSAGES.en[key]).toBeTruthy();
      expect(MESSAGES.vi[key]).toBeTruthy();
    }
    expect(invoiceErrorKey("teapot")).toBe("invoice.error.other");
    for (const s of STATUSES) expect(MESSAGES.vi[`invoice.status.${s}` as keyof typeof MESSAGES.en]).toBeTruthy();
  });

  it("lets owner and agent bill", () => {
    expect(["owner", "agent", "assistant", ""].map(canBill)).toEqual([true, true, false, false]);
  });

  it("offers a due schedule row only until a live invoice names it", () => {
    const rows = [
      { schedule_id: ID, status: "due" },
      { schedule_id: ID2, status: "due" },
      { schedule_id: "x", status: "paid" },
    ];
    expect(uninvoicedRows(rows, [])).toHaveLength(2);
    expect(uninvoicedRows(rows, [invoice({})]).map((r) => r.schedule_id)).toEqual([ID2]);
    expect(uninvoicedRows(rows, [invoice({ status: "void" })])).toHaveLength(2);
  });

  it("builds the typed lines, skipping blank rows", () => {
    expect(
      linesBody([
        { description: " Planning fee ", quantity: "", unitAmount: "1,200.50" },
        { description: "", quantity: "1", unitAmount: "" },
      ]),
    ).toEqual({ ok: true, lines: [{ description: "Planning fee", quantity: "1", unit_amount: "1200.50" }] });
    expect(linesBody([{ description: "", quantity: "1", unitAmount: "" }])).toEqual({ ok: false, key: "invoice.error.invalid_lines" });
    expect(linesBody([{ description: "", quantity: "1", unitAmount: "5" }])).toEqual({ ok: false, key: "invoice.error.description" });
    expect(linesBody([{ description: "Fee", quantity: "0", unitAmount: "5" }])).toEqual({ ok: false, key: "invoice.error.invalid_quantity" });
    expect(linesBody([{ description: "Fee", quantity: "1", unitAmount: "abc" }])).toEqual({ ok: false, key: "invoice.error.invalid_unit_amount" });
  });
});

describe("BFF allow-list: invoices", () => {
  it("forwards the invoice routes and nothing else", () => {
    expect(matchBff("GET", `trips/${ID}/invoices`)).not.toBeNull();
    expect(matchBff("POST", `trips/${ID}/invoices`)).not.toBeNull();
    expect(matchBff("GET", "invoices")).not.toBeNull();
    expect(matchBff("GET", `invoices/${ID}`)).not.toBeNull();
    expect(matchBff("GET", `invoices/${ID}/document`)).not.toBeNull();
    expect(matchBff("PATCH", `invoices/${ID}`)).not.toBeNull();
    expect(matchBff("POST", `invoices/${ID}/issue`)).not.toBeNull();
    expect(matchBff("POST", `invoices/${ID}/void`)).not.toBeNull();
    expect(matchBff("PUT", "settings/invoices")).not.toBeNull();
    expect(matchBff("DELETE", `invoices/${ID}`)).toBeNull();
    expect(matchBff("POST", `invoices/${ID}/delete`)).toBeNull();
  });
});

describe("payments and refunds (slice 3)", () => {
  const refund = (status: RefundRequest["status"]): RefundRequest => ({
    refund_id: ID,
    invoice_id: ID,
    payment_id: ID2,
    amount: "20.00",
    currency: "USD",
    reason: "Shortened",
    status,
    requested_by: "member:a",
    decided_by: null,
    decided_at: null,
    decision_note: null,
    executed_at: null,
  });

  it("offers the next refund step to owner and agent only", () => {
    expect(refundStep(refund("proposed"), "agent")).toBe("decide");
    expect(refundStep(refund("approved"), "owner")).toBe("execute");
    expect(refundStep(refund("executed"), "owner")).toBeNull();
    expect(refundStep(refund("proposed"), "assistant")).toBeNull();
    expect(["owner", "agent", "assistant"].map(canDecideRefund)).toEqual([true, true, false]);
  });

  it("words the money refusals in both languages", () => {
    for (const code of ["amount_above_balance", "amount_above_refundable", "above_threshold", "refund_not_approved", "provider_not_available"]) {
      const key = invoiceErrorKey(code) as keyof typeof MESSAGES.en;
      expect(key).not.toBe("invoice.error.other");
      expect(MESSAGES.vi[key]).toBeTruthy();
    }
    for (const s of ["proposed", "approved", "rejected", "executed"]) {
      expect(MESSAGES.vi[`refund.status.${s}` as keyof typeof MESSAGES.en]).toBeTruthy();
    }
  });

  it("forwards the payment, refund and payment-method routes", () => {
    expect(matchBff("POST", `invoices/${ID}/payments`)).not.toBeNull();
    expect(matchBff("POST", `payments/${ID}/refund-requests`)).not.toBeNull();
    expect(matchBff("GET", "refund-requests")).not.toBeNull();
    for (const step of ["approve", "reject", "execute"]) expect(matchBff("POST", `refund-requests/${ID}/${step}`)).not.toBeNull();
    expect(matchBff("GET", "settings/payment-methods")).not.toBeNull();
    expect(matchBff("PUT", "settings/payment-methods/manual")).not.toBeNull();
    expect(matchBff("POST", `settings/payment-methods/${ID}/disconnect`)).not.toBeNull();
    expect(matchBff("POST", "settings/payment-methods")).toBeNull();
    expect(matchBff("DELETE", `refund-requests/${ID}`)).toBeNull();
  });
});
