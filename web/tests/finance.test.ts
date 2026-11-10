// @vitest-environment jsdom
// AA-1068 (crm-051): the finance helpers (who sees money, dimensions, refusal wording, the departure link, the profit
// CSV path, sums), the BFF lines (only the routes the API serves), both languages, the "Bookings with AA" nav entry for
// owner / agent only, and the supplier bill form: it sends the bill with the amount of each line of the picked
// supplier, a refusal is worded, and a recorded bill can be marked paid or voided.
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { matchBff } from "@/lib/bff";
import {
  canSeeMoney,
  departureHref,
  DIMENSIONS,
  financeErrorKey,
  isDimension,
  profitExportPath,
  sumAmounts,
  type SupplierBill,
} from "@/lib/finance";
import { t, type MessageKey } from "@/lib/i18n";
import { financeEn, financeVi } from "@/lib/i18nFinance";
import type { Line } from "@/lib/ledger";
import { visibleSections } from "@/lib/nav";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {}, push: () => {} }) }));
const sent: { method: string; path: string; body: unknown }[] = [];
let answer: { ok: boolean; status?: number; detail?: string; data?: unknown } = { ok: true, data: {} };
vi.mock("@/lib/bffClient", () => ({
  bffSend: async (method: string, path: string, body?: unknown) => {
    sent.push({ method, path, body });
    return answer;
  },
  bffDownload: async () => ({ ok: true, data: null }),
}));
const { SupplierBills } = await import("@/components/finance/SupplierBills");
const { ToastProvider } = await import("@/app/_kit");

const UUID = "00000000-0000-4000-8000-000000000001";
const LINE_A = "00000000-0000-4000-8000-0000000000a1";
const LINE_B = "00000000-0000-4000-8000-0000000000b1";
const SUP = "00000000-0000-4000-8000-0000000000c1";

describe("finance helpers", () => {
  it("shows money to owner and agent only", () => {
    expect(canSeeMoney("owner")).toBe(true);
    expect(canSeeMoney("agent")).toBe(true);
    expect(canSeeMoney("assistant")).toBe(false);
  });
  it("knows its dimensions, words refusals and builds links", () => {
    expect(DIMENSIONS).toContain("product");
    expect(isDimension("travel_month")).toBe(true);
    expect(isDimension("colour")).toBe(false);
    expect(financeErrorKey("bill_lines_over")).toBe("finance.error.bill_lines_over");
    expect(financeErrorKey("something_new")).toBe("finance.error.other");
    expect(t("en", financeErrorKey("duplicate_bill_number") as MessageKey)).toBe("This supplier already has a bill with this number.");
    expect(departureHref("GRP:7")).toBe("/departures/GRP%3A7");
    expect(profitExportPath("12m", "product", null)).toBe("reports/export?section=profit&period=12m&dimension=product");
    expect(profitExportPath("ytd", "lead_source", "me")).toContain("owner=me");
    expect(sumAmounts(["10.10", "0.20", "-1.00"])).toBe("9.30");
  });
});

describe("BFF lines", () => {
  it("serve the finance routes the API has, nothing else", () => {
    for (const [m, p] of [
      ["GET", `trips/${UUID}/pnl`],
      ["PUT", `trips/${UUID}/group-code`],
      ["GET", "supplier-bills"],
      ["POST", "supplier-bills"],
      ["GET", `supplier-bills/${UUID}`],
      ["PATCH", `supplier-bills/${UUID}`],
      ["GET", "aa-bookings"],
      ["GET", "aa-bookings/invoices/AAT-2031-000001"],
      ["GET", "departures/GRP%3A7/pnl"],
      ["POST", "departure-costs"],
      ["PATCH", `departure-costs/${UUID}`],
      ["DELETE", `departure-costs/${UUID}`],
      ["GET", "reports/profit"],
      ["GET", "reports/agenda"],
    ] as const) {
      expect(matchBff(m, p), `${m} ${p}`).not.toBeNull();
    }
    expect(matchBff("DELETE", `supplier-bills/${UUID}`)).toBeNull();
    expect(matchBff("GET", "aa-bookings/invoices/../../x")).toBeNull();
    expect(matchBff("POST", `trips/${UUID}/pnl`)).toBeNull();
  });
});

describe("words and navigation", () => {
  it("has every finance word in English and Vietnamese", () => {
    expect(Object.keys(financeVi).sort()).toEqual(Object.keys(financeEn).sort());
    for (const v of Object.values(financeVi)) expect(v.trim()).not.toBe("");
    expect(t("vi", "nav.aaBookings")).toBe("Đặt chỗ với AA");
  });
  it("lists Bookings with AA for owner and agent, not the assistant", () => {
    expect(visibleSections("owner").map((s) => s.key)).toContain("aa-bookings");
    expect(visibleSections("agent").map((s) => s.key)).toContain("aa-bookings");
    expect(visibleSections("assistant").map((s) => s.key)).not.toContain("aa-bookings");
  });
});

function line(id: string, title: string): Line {
  return {
    booking_line_id: id, trip_id: UUID, supplier_kind: "agency", agency_supplier_id: SUP, supplier_name: "Pine Hill Lodge",
    status: "confirmed", aa_sub_status: null, title, confirmation_no: "PH-1", service_from: null, service_to: null,
    collect: "agency", confirm_class: null, visible_to_travelers: false, confirmed_at: null, read_only: false,
    money: { cost: { amount: "100.00", currency: "EUR" } },
  };
}

const BILL: SupplierBill = {
  supplier_bill_id: UUID, agency_supplier_id: SUP, supplier_name: "Pine Hill Lodge", number: "PH-9", bill_date: "2031-02-01",
  status: "recorded", paid_on: null, note: null,
  bill_cost: { amount: "120.00", currency: "EUR", amount_base: "132.00", base_currency: "USD", allocated: "100.00", unallocated: "20.00" },
  lines: [{ booking_line_id: LINE_A, line_title: "Lodge 2 nights", trip_id: UUID, trip_title: "Lakes", line_cost: "100.00" }],
};

let root: Root | null = null;
afterEach(() => {
  act(() => root?.unmount());
  root = null;
  document.body.innerHTML = "";
  sent.length = 0;
  answer = { ok: true, data: {} };
});

async function render(bills: SupplierBill[]) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(
      createElement(ToastProvider, null,
        createElement(SupplierBills, { bills, lines: [line(LINE_A, "Lodge 2 nights"), line(LINE_B, "Lodge dinner")], lang: "en", locale: "en-GB", closed: false })),
    );
  });
  return host;
}

function setValue(el: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
  setter.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
}

describe("SupplierBills", () => {
  it("records a bill with the amount of each line and words a refusal", async () => {
    const host = await render([]);
    expect(host.textContent).toContain("No bill recorded for this trip yet.");
    const add = [...host.querySelectorAll("button")].find((b) => b.textContent === "Record a bill")!;
    await act(async () => add.click());
    const form = host.querySelector("form[data-testid=supplier-bill-form]") as HTMLFormElement;
    const input = (name: string) => form.querySelector(`input[name="${name}"]`) as HTMLInputElement;
    setValue(input("number"), "PH-10");
    setValue(input("amount"), "150");
    setValue(input(`line-${LINE_A}`), "120");
    expect(input("currency").value).toBe("EUR");
    answer = { ok: false, status: 409, detail: "duplicate_bill_number" };
    await act(async () => form.requestSubmit());
    expect(sent[0].method).toBe("POST");
    const body = sent[0].body as { number: string; lines: { booking_line_id: string; amount: string }[]; currency: string };
    expect(body.number).toBe("PH-10");
    expect(body.currency).toBe("EUR");
    expect(body.lines).toEqual([{ booking_line_id: LINE_A, amount: "120" }]);
    expect(host.textContent).toContain("This supplier already has a bill with this number.");
  });

  it("shows a bill and marks it paid or void", async () => {
    const host = await render([BILL]);
    expect(host.textContent).toContain("PH-9");
    expect(host.textContent).toContain("Not on this trip's lines");
    const paid = [...host.querySelectorAll("button")].find((b) => b.textContent === "Mark paid")!;
    await act(async () => paid.click());
    const voided = [...host.querySelectorAll("button")].find((b) => b.textContent === "Void")!;
    await act(async () => voided.click());
    expect(sent.map((s) => [s.method, s.path, s.body])).toEqual([
      ["PATCH", `supplier-bills/${UUID}`, { status: "paid" }],
      ["PATCH", `supplier-bills/${UUID}`, { status: "void" }],
    ]);
  });
});
