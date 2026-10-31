// @vitest-environment jsdom
// AA-957 S262 live acceptance follow-up, A-12 Commissions: the list is the kit DataTable (a card per commission on a
// phone, with its status and its follow-up); a trip shows its dates; a receipt in another currency reads as its pair
// with the base amount and the bank reference (never "Your claim" on a received row); a written-off row shows its
// note; the filter is the kit underline tabs with the active one marked; a recorded commission says so in a page
// toast (the row may leave the filter); the forms wrap.
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToString } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ToastProvider } from "@/app/_kit";
import { type Commission, rateText, tripWhen } from "@/lib/commissions";
import { MESSAGES } from "@/lib/i18n";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const push = vi.fn();
const refresh = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh, push }) }));
const { CommissionsTable } = await import("@/components/commissions/CommissionsTable");
const { CommissionFilters, filterHref } = await import("@/components/commissions/CommissionFilters");
const { CommissionActions } = await import("@/components/commissions/CommissionActions");

const roots: Root[] = [];
afterEach(() => {
  for (const r of roots.splice(0)) act(() => r.unmount());
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
  push.mockReset();
  refresh.mockReset();
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

function commission(over: Partial<Commission> = {}): Commission {
  return {
    commission_id: "00000000-0000-4000-8000-00000000c001",
    trip_id: "00000000-0000-4000-8000-0000000007e1",
    trip_title: "Mekong, 3 nights",
    trip_stage: "booked",
    trip_start_date: "2026-11-12",
    trip_end_date: "2026-11-15",
    trip_month: null,
    booking_line_id: "00000000-0000-4000-8000-0000000001a1",
    line_title: "River cruise 2D1N",
    line_status: "confirmed",
    service_from: "2026-11-12",
    service_to: "2026-11-13",
    agency_supplier_id: "00000000-0000-4000-8000-000000000005",
    supplier_name: "Lotus River Cruises",
    status: "expected",
    due_on: "2026-11-25",
    overdue: false,
    days_overdue: 0,
    claimed_at: null,
    claim_ref: null,
    close_reason: null,
    note: null,
    keep_on_cancel: false,
    commission_expected: { amount: "50.00", currency: "USD", amount_base: "50.00", base_currency: "USD" },
    commission_received: null,
    ...over,
  };
}

const RECEIVED = commission({
  status: "received",
  claim_ref: "LT-INV-001",
  commission_received: {
    amount: "46.00",
    currency: "EUR",
    received_on: "2026-10-28",
    reference: "BANK-REF-77",
    amount_base: "50.00",
    base_currency: "USD",
    fx: { rate: "1.0869565217", rate_date: "2026-10-28", source: "platform_daily", basis: "receipt_date", stale_days: 0 },
    short_amount: null,
    fx_difference_base: null,
  },
});

describe("helpers", () => {
  it("reads a rate as its pair, rounded", () => {
    expect(rateText("1.0869565217", "EUR", "USD")).toBe("1 EUR = 1.0870 USD");
  });
  it("tells a trip by its dates, else its month", () => {
    expect(tripWhen(commission(), "en-AU")).toBe("12 Nov – 15 Nov 2026");
    expect(tripWhen(commission({ trip_start_date: null, trip_end_date: null, trip_month: "2026-12-01" }), "en-AU")).toBe("Dec 2026");
    expect(tripWhen(commission({ trip_start_date: null, trip_end_date: null }), "en-AU")).toBe("");
  });
  it("keeps each filter on its own URL", () => {
    expect(filterHref("open")).toBe("/commissions");
    expect(filterHref("received")).toBe("/commissions?status=received");
  });
});

describe("the list", () => {
  const html = renderToString(
    createElement(ToastProvider, null, createElement(CommissionsTable, {
      commissions: [RECEIVED, commission({ commission_id: "c2", status: "written_off", close_reason: "too_small", note: "Not worth chasing the boat school" })],
      base: "USD",
      lang: "en",
      locale: "en-AU",
    })),
  );
  it("shows the receipt's pair, base amount and bank reference, and no claim line on a received row", () => {
    expect(html).toContain("1 EUR = 1.0870 USD on 28 Oct 2026 (the day it arrived)");
    expect(html).toContain("Counts as 50.00 USD");
    expect(html).toContain("Bank reference: BANK-REF-77");
    expect(html).not.toContain("Your claim");
    expect(html).not.toContain("1.0869565217");
  });
  it("shows the write-off's note and the trip's dates", () => {
    expect(html).toContain("Note: Not worth chasing the boat school");
    expect(html).toContain("Too small to chase");
    expect(html).toContain("12 Nov – 15 Nov 2026");
  });
  it("is the kit table, with a follow-up column and a row per commission", () => {
    expect(html).toContain('data-testid="kit-datatable-body"');
    expect((html.match(/data-testid="commission-row"/g) ?? []).length).toBe(2);
    expect(html).toContain(MESSAGES.en["commission.col.actions"]);
  });
  it("keeps the claim on a claimed row", () => {
    const claimed = renderToString(
      createElement(ToastProvider, null, createElement(CommissionsTable, { commissions: [commission({ status: "claimed", claim_ref: "LT-INV-002" })], base: "USD", lang: "vi", locale: "vi-VN" })),
    );
    expect(claimed).toContain(`${MESSAGES.vi["commission.claimRefShown"].replace("{ref}", "LT-INV-002")}`);
  });
});

describe("filter tabs", () => {
  it("marks the active filter and moves to the next one's URL", async () => {
    const c = await mount(createElement(CommissionFilters, { active: "received", lang: "en" }));
    const selected = c.querySelector('[role=tab][aria-selected="true"]')!;
    expect(selected.textContent).toBe("Received");
    expect(c.querySelectorAll("[role=tab]").length).toBe(6);
    await act(async () => [...c.querySelectorAll<HTMLButtonElement>("[role=tab]")].find((b) => b.textContent === "Overdue")!.click());
    expect(push).toHaveBeenCalledWith("/commissions?status=overdue");
  });
});

describe("follow-up", () => {
  it("says 'Commission recorded' in a page toast that outlives the row; a refusal stays on the row", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ status: "received" }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "fx_rate_missing" }), { status: 409, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    const c = await mount(createElement(ToastProvider, null, createElement(CommissionActions, { commission: commission(), lang: "en", locale: "en-AU" })));
    const openReceive = () => [...c.querySelectorAll<HTMLButtonElement>("button")].find((b) => b.textContent === "Record received")!;
    await act(async () => openReceive().click());
    const form = c.querySelector<HTMLFormElement>("[data-testid=commission-receive-form]")!;
    // Wraps at phone width: the amount / currency row wraps and every field may shrink.
    expect(form.querySelector<HTMLElement>("div[style*='flex-wrap']")).not.toBeNull();
    await act(async () => form.requestSubmit());
    expect(refresh).toHaveBeenCalled();
    expect(document.body.textContent).toContain("Commission recorded");
    await act(async () => openReceive().click());
    await act(async () => c.querySelector<HTMLFormElement>("[data-testid=commission-receive-form]")!.requestSubmit());
    expect(c.querySelector("[data-testid=commission-error]")!.textContent).toBe(MESSAGES.en["commission.error.fx_rate_missing"]);
  });
});
