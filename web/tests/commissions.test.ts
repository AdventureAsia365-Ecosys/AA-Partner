// @vitest-environment jsdom
// AA-957 (CRM-27): the commission helpers (actions per status, filters, refusal wording), the BFF lines (A-12 and the
// follow-up only; never a route the API does not serve), both languages, and the cancel dialog: the dry run shows
// Booking's fee, the agency's supplier bookings, the refund per payment (lowered by the member) and the commissions
// to keep; the run sends them; a trip waiting for AA shows "Cancellation pending" and "Ask AA again".
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { matchBff } from "@/lib/bff";
import { commissionActions, commissionErrorKey, FILTERS, isFilter } from "@/lib/commissions";
import { MESSAGES, t, type MessageKey } from "@/lib/i18n";
import { commissionEn, commissionVi } from "@/lib/i18nCommission";
import type { CancelPreview } from "@/lib/pipeline";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {}, push: () => {} }) }));
const { CancelDialog } = await import("@/components/pipeline/StageDialogs");

const UUID = "00000000-0000-4000-8000-000000000001";
const PAY = "00000000-0000-4000-8000-0000000000a2";
const KEEP = "00000000-0000-4000-8000-0000000000c3";

describe("commission helpers", () => {
  it("offers the next steps per status and nothing on a closed one", () => {
    expect(commissionActions({ status: "expected" })).toEqual(["claim", "receive", "write_off", "due"]);
    expect(commissionActions({ status: "claimed" })).toEqual(["receive", "unclaim", "write_off", "due"]);
    for (const s of ["received", "written_off", "cancelled"] as const) expect(commissionActions({ status: s })).toEqual([]);
  });
  it("knows its filters and words every refusal", () => {
    expect(FILTERS).toContain("overdue");
    expect(isFilter("overdue")).toBe(true);
    expect(isFilter("paid")).toBe(false);
    expect(commissionErrorKey("fx_rate_missing")).toBe("commission.error.fx_rate_missing");
    expect(commissionErrorKey("something_new")).toBe("commission.error.other");
    expect(t("en", commissionErrorKey("commission_closed") as MessageKey)).toBe("This commission is already closed.");
  });
});

describe("BFF lines", () => {
  it("serve A-12 and the follow-up, nothing else", () => {
    expect(matchBff("GET", "commissions")).not.toBeNull();
    expect(matchBff("GET", `commissions/${UUID}`)).not.toBeNull();
    expect(matchBff("PATCH", `commissions/${UUID}`)).not.toBeNull();
    for (const a of ["claim", "unclaim", "receive", "write-off"]) expect(matchBff("POST", `commissions/${UUID}/${a}`)).not.toBeNull();
    expect(matchBff("DELETE", `commissions/${UUID}`)).toBeNull();
    expect(matchBff("POST", `commissions/${UUID}/approve`)).toBeNull();
    expect(matchBff("POST", `trips/${UUID}/cancel`)).not.toBeNull();
  });
});

describe("both languages", () => {
  it("has every commission key in English and Vietnamese, spread into the dictionary", () => {
    expect(Object.keys(commissionVi).sort()).toEqual(Object.keys(commissionEn).sort());
    expect(MESSAGES.en["commissions.title"]).toBe("Commissions");
    expect(MESSAGES.vi["commissions.title"]).toBe("Hoa hồng");
    for (const [key, text] of Object.entries(commissionVi)) expect(text, key).not.toBe("");
  });
});

const roots: Root[] = [];
afterEach(() => {
  for (const r of roots.splice(0)) act(() => r.unmount());
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
});

async function mount(element: ReturnType<typeof createElement>): Promise<HTMLElement> {
  const container = document.createElement("div");
  document.body.appendChild(container);
  await act(async () => {
    const root = createRoot(container);
    roots.push(root);
    root.render(element);
  });
  return container;
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

async function setValue(el: HTMLInputElement | HTMLSelectElement, value: string) {
  await act(async () => {
    const proto = el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(el, value);
    el.dispatchEvent(new Event(el instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
  });
}

const PREVIEW: CancelPreview = {
  trip_id: UUID,
  from_stage: "booked",
  cancel_reason_code: "client_request",
  dry_run: true,
  effects: [
    { kind: "aa_bookings_cancelled", count: 1 },
    { kind: "supplier_lines_to_cancel", count: 1 },
    { kind: "refund_requests", count: 1 },
    { kind: "tasks_skipped", count: 0 },
  ],
  aa_bookings: [{ booking_id: "b-h1", titles: ["Mekong Delta 3 days"], asked: false, failed_code: null, fee: { amount: "250.00", currency: "USD", fee_pct: "25", days_before_departure: 40 } }],
  supplier_lines: [{ booking_line_id: UUID, title: "Lodge 2 nights", supplier_name: "Riverside Lodge", confirmation_no: "RL-7" }],
  payments: [{ payment_id: PAY, invoice_id: UUID, invoice_number: "INV-000004", amount: "300.00", currency: "USD", received_on: "2026-10-20", refundable: "300.00", refund_amount: "300.00", needs_invoice: false }],
  commissions: [{ commission_id: KEEP, status: "expected", title: "Lodge 2 nights", supplier_name: "Riverside Lodge", expected: { amount: "40.00", currency: "USD" } }],
  terms: null,
  booking_unavailable: false,
  cancellation: null,
};

describe("cancel dialog (C12)", () => {
  it("shows Booking's fee and the refunds, and sends the lowered refund and the kept commission", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(json(200, PREVIEW))
      .mockResolvedValueOnce(json(200, { ...PREVIEW, dry_run: false, stage: "booked", cancellation: { status: "pending" } }));
    vi.stubGlobal("fetch", fetchMock);
    const done = vi.fn();
    const c = await mount(createElement(CancelDialog, { tripId: UUID, lang: "en", locale: "en-AU", onDone: done, onClose: () => {} }));
    await setValue(c.querySelector<HTMLSelectElement>("[data-testid=cancel-reason]")!, "client_request");
    expect(c.querySelector("[data-testid=cancel-aa-fee]")!.textContent).toBe("Fee 250.00 USD (25 %, 40 days before departure)");
    expect(c.querySelector("[data-testid=cancel-supplier-lines]")!.textContent).toContain("Riverside Lodge: Lodge 2 nights (RL-7)");
    expect(c.querySelector("[data-kind=tasks_skipped]")).toBeNull(); // nothing to do: not listed
    const refund = c.querySelector<HTMLInputElement>("[data-testid=cancel-refund-amount]")!;
    expect(refund.value).toBe("300.00");
    await setValue(refund, "200");
    await act(async () => {
      c.querySelector<HTMLInputElement>("[data-testid=cancel-commissions] input[type=checkbox]")!.click();
    });
    const confirm = c.querySelector<HTMLButtonElement>("[data-testid=cancel-confirm] button")!;
    expect(confirm.disabled).toBe(true);
    await setValue(c.querySelector<HTMLInputElement>("[data-testid=cancel-typed]")!, "CANCEL");
    await act(async () => confirm.click());
    const [url, init] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(url).toBe(`/bff/trips/${UUID}/cancel?dry_run=false`);
    expect(JSON.parse(String(init.body))).toEqual({
      cancel_reason_code: "client_request",
      refunds: [{ payment_id: PAY, amount: "200" }],
      keep_commission: [KEEP],
    });
    expect(done).toHaveBeenCalledWith(expect.objectContaining({ cancellation: { status: "pending" } }));
  });

  it("on a pending trip lists what AA still has to answer and asks again", async () => {
    const pending = { ...PREVIEW, cancellation: { status: "pending" }, aa_bookings: [{ ...PREVIEW.aa_bookings![0], fee: null, asked: false, failed_code: "forbidden" }] };
    const fetchMock = vi.fn().mockResolvedValueOnce(json(200, pending)).mockResolvedValueOnce(json(200, { ...pending, requeued: 1 }));
    vi.stubGlobal("fetch", fetchMock);
    const c = await mount(createElement(CancelDialog, { tripId: UUID, lang: "en", locale: "en-AU", onDone: () => {}, onClose: () => {} }));
    await setValue(c.querySelector<HTMLSelectElement>("[data-testid=cancel-reason]")!, "client_request");
    expect(c.querySelector("[data-testid=cancel-effects]")!.getAttribute("data-pending")).toBe("true");
    expect(c.querySelector("[data-testid=cancel-aa-fee]")!.textContent).toBe("AA refused: forbidden");
    expect(c.querySelector("[data-testid=cancel-typed]")).toBeNull();
    await act(async () => c.querySelector<HTMLButtonElement>("[data-testid=cancel-retry] button")!.click());
    expect((fetchMock.mock.calls[1] as [string])[0]).toBe(`/bff/trips/${UUID}/cancel?dry_run=false`);
  });

  it("says AA cannot be reached when Booking gave no fee", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(json(200, { ...PREVIEW, booking_unavailable: true, aa_bookings: [{ ...PREVIEW.aa_bookings![0], fee: null }] })));
    const c = await mount(createElement(CancelDialog, { tripId: UUID, lang: "vi", locale: "vi-VN", onDone: () => {}, onClose: () => {} }));
    await setValue(c.querySelector<HTMLSelectElement>("[data-testid=cancel-reason]")!, "client_request");
    expect(c.querySelector("[data-testid=cancel-booking-down]")!.textContent).toBe(MESSAGES.vi["cancelTrip.bookingDown"]);
    expect(c.querySelector("[data-testid=cancel-aa-fee]")!.textContent).toBe(MESSAGES.vi["cancelTrip.aaFeeUnknown"]);
  });
});
