// @vitest-environment jsdom
// AA-1067 review fixes, in the browser: removing an instalment keeps every other line's own due date (stable row keys
// for the uncontrolled date fields); the cancel dialog never confirms a charge its figures were not worked out with;
// the client pay box and the portal payments list name an instalment ("Instalment 2"), never a raw message key.
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MESSAGES } from "@/lib/i18n";
import type { CancelPreview } from "@/lib/pipeline";
import type { ClientInvoice } from "@/lib/proposals";
import type { ScheduleLine } from "@/lib/retailPayments";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {}, push: () => {} }) }));
const { ScheduleEditor } = await import("@/components/proposals/ScheduleEditor");
const { CancelDialog } = await import("@/components/pipeline/StageDialogs");
const { ClientPayBox } = await import("@/components/proposals/ClientPayBox");

const P = "22222222-2222-4222-8222-222222222222";
const TRIP = "22222222-2222-4222-8222-2222222222a1";
const PAY = "22222222-2222-4222-8222-2222222222a2";
const tr = (key: string) => MESSAGES.en[key as keyof typeof MESSAGES.en] ?? key;

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

async function setValue(el: HTMLInputElement | HTMLSelectElement, value: string) {
  await act(async () => {
    const proto = el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(el, value);
    el.dispatchEvent(new Event(el instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
  });
}

async function click(el: Element) {
  await act(async () => {
    (el as HTMLElement).click();
  });
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

describe("builder payment lines", () => {
  const LINES: ScheduleLine[] = [
    { kind: "deposit", seq: 1, percent: "25", due_on: null, days_before: null },
    { kind: "instalment", seq: 1, percent: "25", due_on: "2027-01-10", days_before: null },
    { kind: "instalment", seq: 2, percent: "25", due_on: "2027-02-05", days_before: null },
    { kind: "balance", seq: 1, percent: null, due_on: "2027-02-20", days_before: null },
  ];

  it("keeps the next instalment's own date when an earlier one is removed", async () => {
    const write = vi.fn(async () => true);
    const c = await mount(
      createElement(ScheduleEditor, {
        proposalId: P, lines: LINES, amounts: undefined, policy: null, draft: true, canMoney: true, busy: false,
        locale: "en-AU", money: (v) => String(v), tr, write, onMessage: () => undefined,
      }),
    );
    const lines = () => [...c.querySelectorAll("[data-testid=builder-schedule-line]")];
    const removeFirst = [...lines()[1].querySelectorAll("button")].find((b) => b.textContent === tr("schedule.remove"))!;
    await click(removeFirst);
    expect(lines().map((l) => l.getAttribute("data-kind"))).toEqual(["deposit", "instalment", "balance"]);
    expect(lines()[1].querySelector<HTMLInputElement>("[data-testid=date-input]")!.value).toBe("2027-02-05");
    await act(async () => {
      c.querySelector("form")!.requestSubmit();
    });
    expect(write).toHaveBeenCalledTimes(1);
    const body = (write.mock.calls[0] as unknown[])[2] as { schedule: Record<string, unknown>[] };
    expect(body.schedule).toEqual([
      { kind: "deposit", percent: "25" },
      { kind: "instalment", percent: "25", due_on: "2027-02-05" },
      { kind: "balance", due_on: "2027-02-20" },
    ]);
  });

  it("refuses a deposit under 1 % before calling the API", async () => {
    const write = vi.fn(async () => true);
    const onMessage = vi.fn();
    const c = await mount(
      createElement(ScheduleEditor, {
        proposalId: P, lines: LINES, amounts: undefined, policy: null, draft: true, canMoney: true, busy: false,
        locale: "en-AU", money: (v) => String(v), tr, write, onMessage,
      }),
    );
    const deposit = c.querySelector("[data-testid=builder-schedule-line][data-kind=deposit] input")!;
    await setValue(deposit as HTMLInputElement, "0.5");
    await act(async () => {
      c.querySelector("form")!.requestSubmit();
    });
    expect(write).not.toHaveBeenCalled();
    expect(onMessage).toHaveBeenCalledWith("danger", tr("schedule.invalid"));
  });
});

describe("cancel dialog charge", () => {
  const charge = (over: Partial<NonNullable<CancelPreview["charge"]>> = {}): NonNullable<CancelPreview["charge"]> => ({
    currency: "USD", trip_total: "1000.00", days_before: 20, policy: { policy_id: P, name: "Standard", version: 1, frozen: true },
    tier: { days_before_min: 15, charge_pct: "50.00" }, policy_charge: "500.00", decision: "policy", charge_amount: "500.00",
    reason: null, paid_amount: "600.00", kept_amount: "500.00", refund_due: "100.00", invoice_amount: "0.00", warnings: [],
    needs_owner: false, ...over,
  });
  const preview = (c: NonNullable<CancelPreview["charge"]>, refund: string): CancelPreview => ({
    trip_id: TRIP, from_stage: "booked", cancel_reason_code: "client_request", dry_run: true, effects: [],
    payments: [{ payment_id: PAY, invoice_id: null, invoice_number: "INV-000101", amount: "600.00", currency: "USD", received_on: "2026-10-01", refundable: "600.00", refund_amount: refund, needs_invoice: false }],
    commissions: [], terms: null, cancellation: null, charge: c,
  });

  it("holds Confirm until the figures are worked out again with the new charge, then sends the matching refunds", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(json(200, preview(charge(), "100.00")))
      .mockResolvedValueOnce(json(200, preview(charge({ decision: "waived", charge_amount: "0.00", kept_amount: "0.00", refund_due: "600.00", reason: "Family illness" }), "600.00")))
      .mockResolvedValueOnce(json(200, { ...preview(charge(), "600.00"), dry_run: false }));
    vi.stubGlobal("fetch", fetchMock);
    const onDone = vi.fn();
    const c = await mount(createElement(CancelDialog, { tripId: TRIP, lang: "en", locale: "en-AU", onDone, onClose: () => {} }));
    await setValue(c.querySelector<HTMLSelectElement>("[data-testid=cancel-reason]")!, "client_request");
    await setValue(c.querySelector<HTMLInputElement>("[data-testid=cancel-typed]")!, "CANCEL");
    const confirm = () => c.querySelector<HTMLButtonElement>("[data-testid=cancel-confirm] button")!;
    expect(confirm().disabled).toBe(false);
    expect(c.querySelector("[data-testid=cancel-charge-stale]")).toBeNull();

    // Waive the charge: the figures on screen are the policy's, so Confirm waits.
    await setValue(c.querySelector<HTMLSelectElement>("[data-testid=cancel-charge-mode]")!, "waive");
    await setValue(c.querySelector<HTMLInputElement>("[data-testid=cancel-charge-reason]")!, "Family illness");
    expect(confirm().disabled).toBe(true);
    expect(c.querySelector("[data-testid=cancel-charge-stale]")!.textContent).toBe(tr("cancelCharge.stale"));

    await click(c.querySelector("[data-testid=cancel-charge-update] button")!);
    const dry = JSON.parse(String((fetchMock.mock.calls[1] as [string, RequestInit])[1].body));
    expect(dry.charge).toEqual({ mode: "waive", reason: "Family illness" });
    expect(c.querySelector("[data-testid=cancel-charge-stale]")).toBeNull();
    expect(c.querySelector<HTMLInputElement>("[data-testid=cancel-refund-amount]")!.value).toBe("600.00");

    await setValue(c.querySelector<HTMLInputElement>("[data-testid=cancel-typed]")!, "CANCEL");
    expect(confirm().disabled).toBe(false);
    await click(confirm());
    const run = JSON.parse(String((fetchMock.mock.calls[2] as [string, RequestInit])[1].body));
    expect(run.charge).toEqual({ mode: "waive", reason: "Family illness" });
    expect(run.refunds).toEqual([{ payment_id: PAY, amount: "600.00" }]);
    expect(onDone).toHaveBeenCalledTimes(1);
  });

  it("works another reason's figures out with the charge as set, and an incomplete one stays stale", async () => {
    const fetchMock = vi.fn().mockImplementation(async () => json(200, preview(charge(), "100.00")));
    vi.stubGlobal("fetch", fetchMock);
    const c = await mount(createElement(CancelDialog, { tripId: TRIP, lang: "en", locale: "en-AU", onDone: () => {}, onClose: () => {} }));
    const reason = () => c.querySelector<HTMLSelectElement>("[data-testid=cancel-reason]")!;
    await setValue(reason(), "client_request");
    await setValue(c.querySelector<HTMLSelectElement>("[data-testid=cancel-charge-mode]")!, "override");
    await setValue(c.querySelector<HTMLInputElement>("[data-testid=cancel-charge-input]")!, "250");
    await setValue(c.querySelector<HTMLInputElement>("[data-testid=cancel-charge-reason]")!, "Goodwill");
    const other = [...reason().options].map((o) => o.value).find((v) => v && v !== "client_request")!;
    await setValue(reason(), other);
    const sent = JSON.parse(String((fetchMock.mock.calls.at(-1) as [string, RequestInit])[1].body));
    expect(sent.charge).toEqual({ mode: "override", amount: "250", reason: "Goodwill" });
    expect(c.querySelector("[data-testid=cancel-charge-stale]")).toBeNull();
    // The amount cleared: nothing complete to send, the figures stay the last ones and Confirm waits.
    await setValue(c.querySelector<HTMLInputElement>("[data-testid=cancel-charge-input]")!, "");
    expect(c.querySelector("[data-testid=cancel-charge-stale]")).not.toBeNull();
  });
});

describe("client instalment labels", () => {
  it("names an instalment invoice in the pay box", () => {
    const inv: ClientInvoice = {
      invoice_id: P, number: "INV-000102", kind: "instalment", seq: 2, currency: "USD", total_amount: "250.00", balance_amount: "250.00",
      due_on: "2027-01-10", pay_window: { state: "open", until: null }, pay_by: null, can_claim: false,
    };
    const html = renderToStaticMarkup(createElement(ClientPayBox, { invoices: [inv], agency: "Lotus Lane Travel", locale: "en-AU", t: tr, onDone: () => {} }));
    expect(html).toContain("Instalment 2");
    expect(html).not.toContain("client.kind");
  });
});
