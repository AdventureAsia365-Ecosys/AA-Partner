// @vitest-environment jsdom
// AA-957 S262 live acceptance follow-up (design v3 C12 §2.7): a cancelled trip (or one whose cancellation waits for
// AA) offers no billing (no Mark paid / Void / Create invoice / New invoice / New portal link, a read-only proposal, no
// second refund request while one is open); the cancel dialog speaks in the future before the action, trims Booking's
// fee percentage, words a payment without an invoice, and keeps the confirm step out of the scrolling body; after the
// run the trip shows "Cancellation pending" and offers no move at once (before the page reloads); a cancelled AA line
// shows no "Confirmed by AA" and words every result code the Booking sync can write, in both languages.
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToString } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { aaBadgeKey, aaResultKey, AA_RESULT_CODES, type AaBlock } from "@/lib/aa";
import { MESSAGES, type MessageKey } from "@/lib/i18n";
import { invoiceErrorKey, type Invoice } from "@/lib/invoices";
import type { Line, TripLines } from "@/lib/ledger";
import { percentText, tripClosedForBilling, type CancelPreview, type Member, type TripView } from "@/lib/pipeline";
import { proposalErrorKey, type LifecycleView } from "@/lib/proposals";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const refresh = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh, push: () => {} }) }));
const { ProposalPanel } = await import("@/components/proposals/ProposalPanel");
const { InvoicesPanel } = await import("@/components/invoices/InvoicesPanel");
const { LinesPanel } = await import("@/components/ledger/LinesPanel");
const { CancelDialog } = await import("@/components/pipeline/StageDialogs");
const { TripPanel } = await import("@/components/pipeline/TripPanel");

const ID = "00000000-0000-4000-8000-0000000000f1";
const TRAVELLER = "00000000-0000-4000-8000-0000000000f2";
const PAY = "00000000-0000-4000-8000-0000000000f3";

const roots: Root[] = [];
afterEach(() => {
  for (const r of roots.splice(0)) act(() => r.unmount());
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
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

describe("helpers", () => {
  it("closes billing for a cancelled trip and for one whose cancellation waits for AA", () => {
    expect(tripClosedForBilling("booked", null)).toBe(false);
    expect(tripClosedForBilling("booked", { status: "pending" })).toBe(true);
    expect(tripClosedForBilling("cancelled", undefined)).toBe(true);
  });
  it("prints Booking's fee percentage without trailing zeros", () => {
    expect(percentText("75.00")).toBe("75");
    expect(percentText("12.50")).toBe("12.5");
    expect(percentText(25)).toBe("25");
    expect(percentText(null)).toBe("");
  });
  it("words the refusal of a cancelled trip on both cards", () => {
    expect(MESSAGES.en[proposalErrorKey("trip_cancelled") as MessageKey]).toMatch(/cancelled/);
    expect(MESSAGES.vi[invoiceErrorKey("trip_cancelled") as MessageKey]).toMatch(/huỷ/);
  });
});

// ── the Proposal and Invoices cards of a cancelled trip ───────────────────────────────────────────

const LIFECYCLE: LifecycleView = {
  trip_id: ID,
  stage: "cancelled",
  proposals: [],
  schedule: [
    { schedule_id: "s-dep", proposal_id: ID, kind: "deposit", amount: "540.00", currency: "USD", due_on: "2026-10-20", status: "paid", paid_at: "2026-10-20T00:00:00Z" },
    { schedule_id: "s-bal", proposal_id: ID, kind: "balance", amount: "1260.00", currency: "USD", due_on: "2026-11-20", status: "due", paid_at: null },
  ],
  payments: [],
  deposit_paid: true,
  portal_unlocked: true,
  portal: {
    s3: 50,
    waiver_required: false,
    travellers: [{ traveler_id: TRAVELLER, role: "lead", details_done: true, missing: [], waiver_signed: null, portal: { access_id: "a1", status: "revoked", last_sign_in_at: null, stage3_submitted_at: null } }],
  },
};

function proposalHtml(closed: boolean): string {
  return renderToString(
    createElement(ProposalPanel, {
      view: LIFECYCLE,
      terms: [],
      party: [{ traveler_id: TRAVELLER, display_name: "Lan Pham", role: "lead" }],
      role: "owner",
      lang: "en",
      locale: "en-AU",
      clientOrigin: "https://go.example.test",
      closed,
    }),
  );
}

describe("Proposal tab", () => {
  it("offers Mark paid, Void, a new portal link and the draft form on a live trip", () => {
    const html = proposalHtml(false);
    expect(html).toContain("Mark paid");
    expect(html).toContain("New portal link");
    expect(html).toContain('data-testid="proposal-form"');
  });
  it("is read-only on a cancelled trip: no billing, no new link, no editable proposal", () => {
    const html = proposalHtml(true);
    expect(html).toContain('data-testid="proposal-closed"');
    expect(html).toContain(MESSAGES.en["trip.closedBilling"]);
    for (const gone of ["Mark paid", ">Void<", "New portal link", 'data-testid="proposal-form"', "Save draft"]) expect(html).not.toContain(gone);
    expect(html).toContain('data-kind="balance"'); // the schedule itself still shows
  });
});

const INVOICE: Invoice = {
  invoice_id: ID,
  trip_id: ID,
  kind: "deposit",
  status: "paid",
  number: "INV-000004",
  schedule_id: "s-dep",
  proposal_id: ID,
  currency: "USD",
  subtotal_amount: "540.00",
  tax_amount: "0.00",
  total_amount: "540.00",
  tax_rate: "0",
  tax_label: "VAT",
  tax_inclusive: true,
  memo: null,
  due_on: "2026-10-20",
  issue_date: "2026-10-18",
  issued_at: "2026-10-18T04:00:00+00:00",
  paid_at: "2026-10-20T04:00:00+00:00",
  voided_at: null,
  void_reason: null,
  created_at: "2026-10-18T03:00:00+00:00",
  actions: [],
  balance_amount: "0.00",
  paid_amount: "540.00",
  refunded_amount: "0.00",
  payments: [{ payment_id: PAY, entry_kind: "payment", amount: "540.00", currency: "USD", method: "bank_transfer", received_on: "2026-10-20", provider: "manual", reference: null, refund_of: null, refundable_amount: "40.00" }],
  refund_requests: [{ refund_id: "r1", invoice_id: ID, payment_id: PAY, amount: "500.00", currency: "USD", reason: "Trip cancelled", status: "proposed", requested_by: "member:x", decided_by: null, decided_at: null, decision_note: null, executed_at: null }],
};

function invoicesHtml(closed: boolean): string {
  return renderToString(
    createElement(InvoicesPanel, { tripId: ID, invoices: [INVOICE], schedule: LIFECYCLE.schedule, role: "owner", lang: "en", locale: "en-AU", timeZone: "Asia/Ho_Chi_Minh", closed }),
  );
}

describe("Invoices card", () => {
  it("offers Create invoice for the due balance, New invoice and Request refund on a live trip", () => {
    const html = invoicesHtml(false);
    expect(html).toContain('data-testid="invoice-from-schedule"');
    expect(html).toContain("Request refund");
  });
  it("on a cancelled trip: no invoice to create, no new invoice, no second refund while one is proposed", () => {
    const html = invoicesHtml(true);
    expect(html).not.toContain('data-testid="invoice-from-schedule"');
    expect(html).not.toContain(MESSAGES.en["invoice.newTyped"]);
    expect(html).not.toContain(MESSAGES.en["refund.request"]);
    expect(html).toContain("INV-000004"); // what the cancel kept stays visible
  });
});

// ── the Bookings tab: a cancelled AA line ────────────────────────────────────────────────────────

function aa(over: Partial<AaBlock> = {}): AaBlock {
  return {
    sub_status: null,
    tour_source_id: "T-100",
    component_kind: "trip_plan",
    currency: "USD",
    pax: { adult: 2, child: 0 },
    quote_valid_until: null,
    hold_expires_at: null,
    hold_id: null,
    booking_id: "b-h1",
    from_price: null,
    below_from_price: null,
    decline_code: null,
    result_code: "cancelled_by_agency",
    no_longer_offered: false,
    pending: null,
    actions: [],
    ...over,
  };
}

function line(over: Partial<Line> = {}): Line {
  return {
    booking_line_id: ID,
    trip_id: ID,
    supplier_kind: "aa",
    agency_supplier_id: ID,
    supplier_name: "Adventure Asia",
    status: "cancelled",
    aa_sub_status: null,
    title: "Delta river 3 days",
    confirmation_no: "AA-1",
    service_from: "2026-12-01",
    service_to: "2026-12-03",
    collect: "agency",
    confirm_class: "confirmed",
    visible_to_travelers: false,
    confirmed_at: null,
    read_only: true,
    aa: aa(),
    ...over,
  };
}

describe("cancelled AA line", () => {
  it("shows Cancelled with the worded result, never 'Confirmed by AA' or the raw code", () => {
    const view: TripLines = { trip_id: ID, lines: [line()], base_currency: "USD", booking_validation_level: "basic", lost_reason_hint: null };
    const html = renderToString(createElement(LinesPanel, { tripId: ID, view, suppliers: [], role: "owner", lang: "en", locale: "en-AU" }));
    expect(html).not.toContain(MESSAGES.en["aa.status.confirmed"]);
    expect(html).toContain(MESSAGES.en["aa.result.cancelled_by_agency"]);
    expect(html).not.toContain(">cancelled_by_agency<");
  });
  it("keeps the badge that says why a line closed, and the confirmed one on a live line", () => {
    expect(aaBadgeKey(aa(), "cancelled")).toBeNull();
    expect(aaBadgeKey(aa({ sub_status: "released" }), "cancelled")).toBe("aa.status.released");
    expect(aaBadgeKey(aa(), "confirmed")).toBe("aa.status.confirmed");
    const pending = { aa_request_id: ID, kind: "release" as const, attempts: 0, next_attempt_at: "2026-11-01T00:00:00Z", last_error_class: null };
    expect(aaBadgeKey(aa({ pending }), "cancelled")).toBe("aa.status.pending");
  });
  it("words every result code the sync can write, in English and Vietnamese, and an unknown one plainly", () => {
    for (const code of [...AA_RESULT_CODES, "other"]) {
      expect(MESSAGES.en[`aa.result.${code}` as MessageKey], code).toBeTruthy();
      expect(MESSAGES.vi[`aa.result.${code}` as MessageKey], code).toBeTruthy();
    }
    expect(aaResultKey("http_409")).toBe("aa.result.other");
  });
});

// ── the cancel dialog ────────────────────────────────────────────────────────────────────────────

const PREVIEW: CancelPreview = {
  trip_id: ID,
  from_stage: "booked",
  cancel_reason_code: "client_request",
  dry_run: true,
  effects: [{ kind: "aa_bookings_cancelled", count: 1 }, { kind: "schedule_rows_voided", count: 1 }],
  aa_bookings: [{ booking_id: "b-h1", titles: ["Delta river 3 days"], asked: false, failed_code: null, fee: { amount: "118.91", currency: "USD", fee_pct: "75.00", days_before_departure: 13 } }],
  supplier_lines: [],
  payments: [{ payment_id: PAY, invoice_id: null, invoice_number: null, amount: "540.00", currency: "USD", received_on: "2026-10-31", refundable: "540.00", refund_amount: "540.00", needs_invoice: true }],
  commissions: [],
  terms: null,
  booking_unavailable: false,
  cancellation: null,
};

describe("cancel dialog", () => {
  it("speaks in the future before the action, trims the fee, words a payment without an invoice", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(json(200, PREVIEW)));
    const c = await mount(createElement(CancelDialog, { tripId: ID, lang: "en", locale: "en-AU", onDone: () => {}, onClose: () => {} }));
    await setValue(c.querySelector<HTMLSelectElement>("[data-testid=cancel-reason]")!, "client_request");
    const body = c.querySelector("[data-testid=cancel-dialog-body]")!;
    expect(body.textContent).toContain(MESSAGES.en["cancelTrip.willWaitHint"]);
    expect(body.textContent).not.toContain(MESSAGES.en["trip.cancellationPendingHint"]); // "You cancelled ..." is the badge's
    expect(c.querySelector("[data-testid=cancel-aa-fee]")!.textContent).toBe("Fee 118.91 USD (75 %, 13 days before departure)");
    expect(body.textContent).toContain("Payment without an invoice, paid 540.00 USD on 31 Oct 2026");
    expect(body.textContent).not.toContain("—, paid");
    expect(c.querySelector("[data-kind=schedule_rows_voided]")!.textContent).toBe("Unpaid balances voided: 1");
  });
  it("keeps the confirm step and the buttons outside the scrolling body", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(json(200, PREVIEW)));
    const c = await mount(createElement(CancelDialog, { tripId: ID, lang: "vi", locale: "vi-VN", onDone: () => {}, onClose: () => {} }));
    await setValue(c.querySelector<HTMLSelectElement>("[data-testid=cancel-reason]")!, "client_request");
    const body = c.querySelector("[data-testid=cancel-dialog-body]")!;
    const typed = c.querySelector<HTMLInputElement>("[data-testid=cancel-typed]")!;
    expect(typed).not.toBeNull();
    expect(body.contains(typed)).toBe(false);
    expect(body.contains(c.querySelector("[data-testid=cancel-confirm]"))).toBe(false);
    expect((body as HTMLElement).style.overflowY).toBe("auto");
    expect(c.querySelector("[data-testid=cancel-effects]")!.getAttribute("style") ?? "").not.toContain("overflow");
  });
});

// ── the trip right after the run ─────────────────────────────────────────────────────────────────

const VIEW: TripView = {
  trip: {
    trip_id: ID,
    title: "Delta for two",
    stage: "booked",
    stage_entered_at: "2026-10-24T04:02:20Z",
    waiting_on: "agency",
    start_date: null,
    end_date: null,
    month: null,
    date_flex_days: 0,
    nights: 3,
    destinations: ["vietnam"],
    party_adults: 2,
    party_child_ages: [],
    party_type: null,
    budget_band: null,
    trip_style: null,
    owner_member_id: "m1",
    is_mine: true,
    lead_source: "form",
    lost_reason_code: null,
    last_inquiry_at: "2026-10-24T04:02:20Z",
    created_at: "2026-10-24T04:02:20Z",
  },
  party: [],
  tasks: [],
  notes: [],
  history: [],
  submissions: [],
  allowed_moves: ["pre_departure", "lost"],
  can_cancel: true,
  cancellation: null,
};
const MEMBERS: Member[] = [{ member_id: "m1", role: "owner", status: "active", is_me: true }];

describe("trip page after the cancel", () => {
  it("shows Cancellation pending and offers no move before the page reloads", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(json(200, PREVIEW))
      .mockResolvedValueOnce(json(200, { ...PREVIEW, dry_run: false, stage: "booked", cancellation: { status: "pending", reason_code: "client_request", from_stage: "booked", aa_bookings: 1, requested_at: "2026-10-31T21:04:03Z", finished_at: null } }));
    vi.stubGlobal("fetch", fetchMock);
    const c = await mount(createElement(TripPanel, { view: VIEW, members: MEMBERS, role: "owner", lang: "en", locale: "en-AU" }));
    expect(c.querySelector("[data-testid=trip-move-pre_departure]")).not.toBeNull();
    await act(async () => c.querySelector<HTMLButtonElement>("[data-testid=trip-cancel] button")!.click());
    await setValue(c.querySelector<HTMLSelectElement>("[data-testid=cancel-reason]")!, "client_request");
    await setValue(c.querySelector<HTMLInputElement>("[data-testid=cancel-typed]")!, "CANCEL");
    await act(async () => c.querySelector<HTMLButtonElement>("[data-testid=cancel-confirm] button")!.click());
    expect(c.querySelector("[data-testid=cancel-dialog]")).toBeNull();
    expect(c.querySelector("[data-testid=trip-cancellation-pending]")).not.toBeNull();
    expect(c.querySelector("[data-testid=trip-move-pre_departure]")).toBeNull();
    expect(c.querySelector("[data-testid=trip-stage]")!.getAttribute("data-stage")).toBe("booked");
    const cancel = c.querySelector("[data-testid=trip-cancel]")!;
    expect(cancel.getAttribute("data-pending")).toBe("true");
    expect(cancel.textContent).toBe(MESSAGES.en["cancelTrip.pendingOpen"]);
    expect(refresh).toHaveBeenCalled();
  });

  it("shows Cancelled at once when no AA booking waits", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(json(200, { ...PREVIEW, aa_bookings: [] }))
        .mockResolvedValueOnce(json(200, { ...PREVIEW, aa_bookings: [], dry_run: false, stage: "cancelled", cancellation: { status: "done", reason_code: "client_request", from_stage: "booked", aa_bookings: 0, requested_at: "2026-10-31T21:04:03Z", finished_at: "2026-10-31T21:04:03Z" } })),
    );
    const c = await mount(createElement(TripPanel, { view: VIEW, members: MEMBERS, role: "owner", lang: "en", locale: "en-AU" }));
    await act(async () => c.querySelector<HTMLButtonElement>("[data-testid=trip-cancel] button")!.click());
    await setValue(c.querySelector<HTMLSelectElement>("[data-testid=cancel-reason]")!, "client_request");
    await setValue(c.querySelector<HTMLInputElement>("[data-testid=cancel-typed]")!, "CANCEL");
    await act(async () => c.querySelector<HTMLButtonElement>("[data-testid=cancel-confirm] button")!.click());
    expect(c.querySelector("[data-testid=trip-stage]")!.getAttribute("data-stage")).toBe("cancelled");
    expect(c.querySelector("[data-testid=trip-cancel]")).toBeNull();
    expect(c.querySelector("[data-testid=trip-cancellation-pending]")).toBeNull();
  });

  it("server data that caught up wins over the run's answer", async () => {
    const pendingView: TripView = { ...VIEW, cancellation: { status: "pending", reason_code: "client_request", from_stage: "booked", aa_bookings: 1, requested_at: "2026-10-31T21:04:03Z", finished_at: null } };
    const c = await mount(createElement(TripPanel, { view: pendingView, members: MEMBERS, role: "owner", lang: "en", locale: "en-AU" }));
    expect(c.querySelector("[data-testid=trip-cancellation-pending]")).not.toBeNull();
    expect(c.querySelector("[data-testid=trip-move-pre_departure]")).toBeNull();
    expect(c.querySelector("[data-testid=trip-cancel]")!.textContent).toBe(MESSAGES.en["cancelTrip.pendingOpen"]);
  });
});
