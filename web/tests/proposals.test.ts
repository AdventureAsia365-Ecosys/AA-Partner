// AA-946 (CRM-18): the thin lifecycle helpers (draft body, amounts, Stage 3 body, refusal wording, the client
// link), the BFF allow-list lines of the new routes, and the client pages' message subset.
import { describe, expect, it } from "vitest";
import { BFF_ROUTES, matchBff } from "@/lib/bff";
import { clientMessages } from "@/lib/clientMessages";
import { MESSAGES } from "@/lib/i18n";
import {
  amountOf,
  clientErrorKey,
  clientUrl,
  CLIENT_TOKEN,
  currentProposal,
  draftBody,
  formatAmount,
  PAY_METHODS,
  proposalErrorKey,
  stage3Body,
  STAGE3_FIELDS,
  type Proposal,
} from "@/lib/proposals";

const ID = "00000000-0000-4000-8000-0000000000e1";
const FORM = { body: "Day 1", currency: "usd", total: "1,000", deposit: "300", balanceDueOn: "2026-12-01", termsDocId: "" };

describe("draft body", () => {
  it("builds the API body and normalises currency and amounts", () => {
    expect(draftBody(FORM)).toEqual({
      ok: true,
      body: { body: "Day 1", currency: "USD", total_amount: "1000", deposit_amount: "300", balance_due_on: "2026-12-01", terms_doc_id: null },
    });
    const full = draftBody({ ...FORM, deposit: "1000", termsDocId: ID });
    expect(full.ok && full.body.balance_due_on).toBeNull();
    expect(full.ok && full.body.terms_doc_id).toBe(ID);
  });

  it("refuses what the API would refuse, with the API's wording", () => {
    expect(draftBody({ ...FORM, body: " " })).toEqual({ ok: false, key: "proposal.error.body_required" });
    expect(draftBody({ ...FORM, currency: "dollar" })).toEqual({ ok: false, key: "proposal.error.invalid_currency" });
    expect(draftBody({ ...FORM, total: "0" })).toEqual({ ok: false, key: "proposal.error.invalid_amount" });
    expect(draftBody({ ...FORM, deposit: "2000" })).toEqual({ ok: false, key: "proposal.error.deposit_above_total" });
    expect(draftBody({ ...FORM, balanceDueOn: "" })).toEqual({ ok: false, key: "proposal.error.balance_due_on_required" });
  });

  it("reads amounts with two decimals at most", () => {
    expect(amountOf("1 200.50")).toBe("1200.50");
    expect(amountOf("1.234")).toBeNull();
    expect(amountOf("-1")).toBeNull();
    expect(amountOf("abc")).toBeNull();
  });
});

describe("display helpers", () => {
  it("formats amounts in the reader's locale, VND without decimals", () => {
    expect(formatAmount("1000.00", "USD", "en-AU")).toBe("1,000.00 USD");
    expect(formatAmount("2540000.00", "VND", "en-AU")).toBe("2,540,000 VND");
    expect(formatAmount(null, "USD", "en-AU")).toBe("—");
    expect(formatAmount("x", "USD", "en-AU")).toBe("x USD");
  });

  it("picks the version the tab shows", () => {
    const p = (status: Proposal["status"], version: number) => ({ status, version }) as Proposal;
    expect(currentProposal([p("draft", 3), p("accepted", 2)])?.version).toBe(2);
    expect(currentProposal([p("draft", 3), p("superseded", 2)])?.version).toBe(3);
    expect(currentProposal([p("withdrawn", 1)])?.version).toBe(1);
    expect(currentProposal([])).toBeNull();
  });

  it("builds client links on the client host", () => {
    expect(clientUrl("https://go-dev.example.test/", "/c/p/abc")).toBe("https://go-dev.example.test/c/p/abc");
    expect(clientUrl(null, "/c/t/abc")).toBe("/c/t/abc");
    expect(CLIENT_TOKEN.test("a".repeat(43))).toBe(true);
    expect(CLIENT_TOKEN.test("a".repeat(42))).toBe(false);
  });

  it("sends only the Stage 3 fields the traveller filled in (write-only)", () => {
    expect(stage3Body({ given_name: " Grace ", nationality: "au", dob: "", passport_country: "vn" })).toEqual({
      given_name: "Grace",
      nationality: "AU",
      passport_country: "VN",
    });
    expect(STAGE3_FIELDS).toHaveLength(8);
  });
});

describe("wording", () => {
  it("words every refusal code in both languages", () => {
    for (const code of ["trip_not_in_proposal", "deposit_above_total", "schedule_not_due", "terms_not_published", "role_forbidden"]) {
      const key = proposalErrorKey(code) as keyof typeof MESSAGES.en;
      expect(MESSAGES.en[key]).toBeTruthy();
      expect(MESSAGES.vi[key]).toBeTruthy();
    }
    expect(proposalErrorKey("teapot")).toBe("proposal.error.other");
    for (const code of ["price_changed", "code_invalid", "stage3_locked", "code_delivery_unavailable", "link_not_found"]) {
      const key = clientErrorKey(code) as keyof typeof MESSAGES.en;
      expect(MESSAGES.en[key]).toBeTruthy();
      expect(MESSAGES.vi[key]).toBeTruthy();
    }
    expect(clientErrorKey("teapot")).toBe("client.error.other");
    for (const m of PAY_METHODS) expect(MESSAGES.vi[`proposal.method.${m}` as keyof typeof MESSAGES.en]).toBeTruthy();
    for (const f of STAGE3_FIELDS) expect(MESSAGES.vi[`client.field.${f}` as keyof typeof MESSAGES.en]).toBeTruthy();
  });

  it("gives the client pages only the client strings", () => {
    const dict = clientMessages("en");
    expect(dict["client.accept"]).toBe("Accept the proposal");
    expect(Object.keys(dict).every((k) => k.startsWith("client.") || k.startsWith("common."))).toBe(true);
    expect(dict["proposal.markPaid" as keyof typeof dict]).toBeUndefined();
  });
});

describe("BFF lines of AA-946", () => {
  it("forwards exactly the CRM-18 routes", () => {
    for (const [method, path] of [
      ["GET", `trips/${ID}/lifecycle`],
      ["POST", `trips/${ID}/proposals`],
      ["PATCH", `proposals/${ID}`],
      ["POST", `proposals/${ID}/send`],
      ["POST", `proposals/${ID}/withdraw`],
      ["POST", `schedule/${ID}/mark-paid`],
      ["POST", `schedule/${ID}/void`],
      ["POST", `trips/${ID}/portal-access`],
      ["POST", `portal-access/${ID}/revoke`],
      ["GET", "agreements"],
      ["POST", "agreements"],
      ["PATCH", `agreements/${ID}`],
      ["POST", `agreements/${ID}/publish`],
    ] as const) {
      expect(matchBff(method, path)?.card, `${method} ${path}`).toBe("AA-946");
    }
    for (const [method, path] of [
      ["DELETE", `proposals/${ID}`],
      ["PATCH", `schedule/${ID}`],
      ["POST", `schedule/${ID}/refund`],
      ["DELETE", `agreements/${ID}`],
      ["GET", `portal-access/${ID}`],
    ] as const) {
      expect(matchBff(method, path), `${method} ${path}`).toBeNull();
    }
    expect(BFF_ROUTES.filter((r) => r.card === "AA-946")).toHaveLength(11);
  });
});
