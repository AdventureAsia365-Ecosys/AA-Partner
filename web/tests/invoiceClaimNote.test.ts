// AA-952 S261 live finding F2: the claim note's deadline ("check your account before <time>") is printed in the agency
// timezone with its label on the trip's Invoices card, so it reads the same as /payments, on the server render and in
// any browser zone.
import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type { Invoice } from "@/lib/invoices";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {} }) }));
const { InvoicesPanel } = await import("@/components/invoices/InvoicesPanel");

const ID = "00000000-0000-4000-8000-0000000000e7";

const CLAIMED: Invoice = {
  invoice_id: ID,
  trip_id: ID,
  kind: "deposit",
  status: "payment_claimed",
  number: "INV-000007",
  schedule_id: ID,
  proposal_id: ID,
  currency: "USD",
  subtotal_amount: "540.00",
  tax_amount: "0.00",
  total_amount: "540.00",
  tax_rate: "0",
  tax_label: "VAT",
  tax_inclusive: true,
  memo: null,
  due_on: "2026-10-31",
  issue_date: "2026-10-31",
  issued_at: "2026-10-31T04:20:00+00:00",
  paid_at: null,
  voided_at: null,
  void_reason: null,
  created_at: "2026-10-31T04:19:00+00:00",
  actions: ["record_payment", "reject_claim"],
  balance_amount: "540.00",
  paid_amount: "0.00",
  refunded_amount: "0.00",
  payments: [],
  refund_requests: [],
  pay_window: { state: "claimed", until: "2026-11-05T04:23:07+00:00" },
  claimed_at: "2026-10-31T04:23:30+00:00",
  hold_extended_at: "2026-10-31T04:23:35+00:00",
  hold_extend_code: "extended",
  rehold: null,
};

function render(timeZone: string): string {
  return renderToString(
    createElement(InvoicesPanel, { tripId: ID, invoices: [CLAIMED], schedule: [], role: "agent", lang: "en", locale: "en-AU", timeZone }),
  );
}

function note(html: string): string {
  const m = /data-testid="invoice-claim-note"[^>]*>([^<]*)</.exec(html);
  return m ? m[1] : "";
}

describe("claim note deadline", () => {
  it("is the agency's local time with the zone label", () => {
    const text = note(render("Asia/Ho_Chi_Minh"));
    expect(text).toMatch(/5 Nov 2026/);
    expect(text).toMatch(/11:23/);
    expect(text).toMatch(/GMT\+7/);
  });
  it("does not follow the zone the page renders in", () => {
    const before = process.env.TZ;
    process.env.TZ = "UTC";
    const onServer = note(render("Asia/Ho_Chi_Minh"));
    process.env.TZ = "America/Los_Angeles";
    const inBrowser = note(render("Asia/Ho_Chi_Minh"));
    process.env.TZ = before;
    expect(onServer).toBe(inBrowser);
    expect(onServer).not.toMatch(/4:23/);
  });
});
