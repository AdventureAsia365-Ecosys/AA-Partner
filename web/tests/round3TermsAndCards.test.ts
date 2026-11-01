// @vitest-environment jsdom
// S262 round 3 + video findings (AA-957, AA-952): (1) a proposal version naming booking terms that are no longer
// published shows that version (marked) in its terms select instead of the current one, says why Publish / Send are
// refused, and offers "Use the current terms" (one PATCH); (2) the stage dialog shell's scrolling body has its own test
// id (stageUi.test.ts pins gate-dialog-body as the GateDialog paragraph); (3) on a phone the trip's Bookings card is a
// card per line, Status (with the cancelled badge and AA fee note), Cost, Sell and Collect in view; (4) between the
// cancel run's answer and the server refresh, the Invoices card is read-only, the balance row reads "Updating" instead
// of "Due" and a line's commission "Updating" instead of "Expected". Invented names only.
import { act, createElement, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { BuilderView } from "@/lib/builder";
import { MESSAGES } from "@/lib/i18n";
import type { Invoice } from "@/lib/invoices";
import type { Line, TripLines } from "@/lib/ledger";
import { termsChoices, type Agreement, type LifecycleView, type Proposal } from "@/lib/proposals";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const refresh = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh, push: () => {} }) }));
const { BuilderPage } = await import("@/components/proposals/BuilderPage");
const { ProposalPanel } = await import("@/components/proposals/ProposalPanel");
const { InvoicesPanel } = await import("@/components/invoices/InvoicesPanel");
const { LinesPanel } = await import("@/components/ledger/LinesPanel");
const { TripClosedProvider, useMarkTripClosed } = await import("@/components/pipeline/TripClosed");

const en = MESSAGES.en;
const TRIP = "00000000-0000-4000-8000-0000000000d1";
const P = "00000000-0000-4000-8000-0000000000d2";
const OLD = "00000000-0000-4000-8000-0000000000d3";
const NEW = "00000000-0000-4000-8000-0000000000d4";

function doc(doc_id: string, version_label: string, status: Agreement["status"]): Agreement {
  return { doc_id, kind: "booking_terms", version_label, status, body_text: "Invented terms", body_sha256: "00", published_at: status === "draft" ? null : "2026-10-01T00:00:00Z" };
}
const TERMS = [doc(OLD, "Marigold-1", "retired"), doc(NEW, "Marigold-2", "published"), doc("w1", "Waiver-1", "published")].map((d, i) =>
  i === 2 ? { ...d, kind: "waiver" as const } : d,
);

const roots: Root[] = [];
afterEach(() => {
  for (const r of roots.splice(0)) act(() => r.unmount());
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
  refresh.mockReset();
});

async function mount(element: ReactNode): Promise<{ c: HTMLElement; root: Root }> {
  const c = document.createElement("div");
  document.body.appendChild(c);
  const root = createRoot(c);
  roots.push(root);
  await act(async () => root.render(element));
  return { c, root };
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const has = (c: HTMLElement, id: string) => c.querySelector(`[data-testid=${id}]`) !== null;

// ── (1) terms ────────────────────────────────────────────────────────────────────────────────────

describe("termsChoices", () => {
  it("lists the published version and the stale one the version names, selected", () => {
    const r = termsChoices(TERMS, OLD);
    expect(r.options).toEqual([
      { doc_id: NEW, version_label: "Marigold-2", stale: false },
      { doc_id: OLD, version_label: "Marigold-1", stale: true, state: "retired" },
    ]);
    expect(r.value).toBe(OLD);
    expect(r.current?.doc_id).toBe(NEW);
    expect(r.stale?.doc_id).toBe(OLD);
  });
  it("a version on the published terms, or none, is not stale; none defaults to the current terms", () => {
    expect(termsChoices(TERMS, NEW)).toMatchObject({ value: NEW, stale: null });
    expect(termsChoices(TERMS, null)).toMatchObject({ value: NEW, stale: null });
    expect(termsChoices([], null)).toEqual({ options: [], value: "", current: null, stale: null });
  });
  it("a removed version is stale with no label; an unreadable list claims nothing", () => {
    expect(termsChoices(TERMS, "gone").stale).toEqual({ doc_id: "gone", version_label: "", stale: true, state: null });
    const unread = termsChoices([], OLD);
    expect(unread.stale).toBeNull();
    expect(unread.options).toEqual([{ doc_id: OLD, version_label: "", stale: false }]);
    expect(unread.value).toBe(OLD);
  });
});

function builderView(terms_doc_id: string | null, terms_status: BuilderView["terms_status"]): BuilderView {
  return {
    proposal_id: P,
    trip_id: TRIP,
    trip_stage: "proposal",
    version: 2,
    status: "draft",
    kind: "builder",
    edit_seq: 4,
    selection_seq: 0,
    currency: "USD",
    price_visibility: "total_with_deltas",
    room_config: [{ type: "double", adults: 2, children: 0 }],
    deposit_pct: "30.00",
    starts_on: "2026-12-01",
    balance_due_on: "2026-11-01",
    terms_doc_id,
    terms_status,
    intro: "",
    price_hash: null,
    published_at: null,
    based_on: null,
    days: [],
    rules: [],
    picks: [],
    totals: { currency: "USD", sell_total: "1500.00", net_total: "900.00", margin_total: "600.00", pay_on_arrival_total: "0.00", deposit: "450.00", balance: "1050.00" },
    checks: terms_status === "retired" ? { publishable: false, blocking: ["terms_retired"], warnings: [] } : { publishable: true, blocking: [], warnings: [] },
    stage2: { accommodation_tier: null, group_pref: null, pace: null, party_adults: 2, party_children: 0 },
  };
}

describe("builder: a draft on retired booking terms", () => {
  it("shows the retired version selected and marked, the blocking check, and one click picks the current terms", async () => {
    const fixed = builderView(NEW, "published");
    const fetchMock = vi.fn().mockResolvedValueOnce(json(200, { ...fixed, edit_seq: 5 }));
    vi.stubGlobal("fetch", fetchMock);
    const { c } = await mount(createElement(BuilderPage, { initial: builderView(OLD, "retired"), role: "agent", lang: "en", locale: "en-AU", terms: TERMS, suppliers: [] }));
    const select = c.querySelector<HTMLSelectElement>("[data-testid=builder-terms]")!;
    expect(select.value).toBe(OLD);
    expect([...select.options].map((o) => o.textContent)).toEqual(["Marigold-2", "Marigold-1 (no longer published)"]);
    expect(c.querySelector("[data-testid=builder-checks]")!.textContent).toContain(en["builder.check.terms_retired"]);
    expect(c.querySelector("[data-testid=builder-terms-stale]")!.textContent).toContain("booking terms Marigold-1 (no longer published)");
    const use = c.querySelector<HTMLButtonElement>("[data-testid=builder-terms-use-current] button")!;
    expect(use.textContent).toBe("Use the current terms (Marigold-2)");
    await act(async () => use.click());
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain(`proposals/${P}/builder`);
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(String(init.body))).toEqual({ seq: 4, terms_doc_id: NEW });
    expect(has(c, "builder-terms-stale")).toBe(false);
    expect(c.querySelector<HTMLSelectElement>("[data-testid=builder-terms]")!.value).toBe(NEW);
    expect(c.textContent).toContain("Booking terms updated to Marigold-2.");
  });

  it("a draft on the published terms shows no note; with no published version the note says so and offers No terms", async () => {
    const { c } = await mount(createElement(BuilderPage, { initial: builderView(NEW, "published"), role: "agent", lang: "en", locale: "en-AU", terms: TERMS, suppliers: [] }));
    expect(has(c, "builder-terms-stale")).toBe(false);
    const onlyOld = [doc(OLD, "Marigold-1", "retired")];
    const { c: c2 } = await mount(createElement(BuilderPage, { initial: builderView(OLD, "retired"), role: "agent", lang: "en", locale: "en-AU", terms: onlyOld, suppliers: [] }));
    expect(c2.querySelector("[data-testid=builder-terms-stale]")!.textContent).toContain(en["proposal.termsNoCurrent"]);
    expect(has(c2, "builder-terms-use-current")).toBe(false);
    expect([...c2.querySelector<HTMLSelectElement>("[data-testid=builder-terms]")!.options].map((o) => o.value)).toEqual(["", OLD]);
  });
});

const TEXT_DRAFT: Proposal = {
  proposal_id: P,
  version: 1,
  status: "draft",
  body: "Three days on the lantern coast.",
  currency: "USD",
  total_amount: "1800.00",
  deposit_amount: "540.00",
  balance_due_on: "2026-11-20",
  terms_doc_id: OLD,
  price_hash: null,
  sent_at: null,
  accepted_at: null,
  kind: "text",
};

function lifecycle(proposals: Proposal[], schedule: LifecycleView["schedule"] = []): LifecycleView {
  return {
    trip_id: TRIP,
    stage: "proposal",
    proposals,
    schedule,
    payments: [],
    deposit_paid: false,
    portal_unlocked: false,
    portal: { s3: 0, waiver_required: false, travellers: [] },
  };
}

describe("text proposal: a draft on retired booking terms", () => {
  it("says so, holds Send, and one click switches the draft to the current terms", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(json(200, { proposal_id: P, status: "draft" }));
    vi.stubGlobal("fetch", fetchMock);
    const { c } = await mount(
      createElement(ProposalPanel, { view: lifecycle([TEXT_DRAFT]), terms: TERMS, party: [], role: "owner", lang: "en", locale: "en-AU", clientOrigin: null }),
    );
    expect(c.querySelector("[data-testid=proposal-terms-stale]")!.textContent).toContain("Marigold-1 (no longer published)");
    const send = [...c.querySelectorAll<HTMLButtonElement>("[data-testid=proposal-current] button")].find((b) => b.textContent === en["proposal.send"])!;
    expect(send.disabled).toBe(true);
    await act(async () => c.querySelector<HTMLButtonElement>("[data-testid=proposal-terms-use-current] button")!.click());
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain(`proposals/${P}`);
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(String(init.body))).toEqual({ terms_doc_id: NEW });
    expect(refresh).toHaveBeenCalled();
  });

  it("the edit form lists the version the draft names; a new version from a sent one on retired terms starts on the current terms", async () => {
    const { c } = await mount(
      createElement(ProposalPanel, { view: lifecycle([TEXT_DRAFT]), terms: TERMS, party: [], role: "owner", lang: "en", locale: "en-AU", clientOrigin: null }),
    );
    const edit = [...c.querySelectorAll<HTMLButtonElement>("[data-testid=proposal-current] button")].find((b) => b.textContent === en["proposal.edit"])!;
    await act(async () => edit.click());
    expect(c.querySelector<HTMLSelectElement>("[data-testid=proposal-terms]")!.value).toBe(OLD);
    const sent: Proposal = { ...TEXT_DRAFT, status: "sent", sent_at: "2026-10-20T00:00:00Z" };
    const { c: c2 } = await mount(
      createElement(ProposalPanel, { view: lifecycle([sent]), terms: TERMS, party: [], role: "owner", lang: "en", locale: "en-AU", clientOrigin: null }),
    );
    expect(has(c2, "proposal-terms-stale")).toBe(false); // only a draft is fixed this way
    const again = [...c2.querySelectorAll<HTMLButtonElement>("[data-testid=proposal-current] button")].find((b) => b.textContent === en["proposal.newVersion"])!;
    await act(async () => again.click());
    expect(c2.querySelector<HTMLSelectElement>("[data-testid=proposal-terms]")!.value).toBe(NEW);
  });

  it("a draft on the published terms keeps Send", async () => {
    const { c } = await mount(
      createElement(ProposalPanel, { view: lifecycle([{ ...TEXT_DRAFT, terms_doc_id: NEW }]), terms: TERMS, party: [], role: "owner", lang: "en", locale: "en-AU", clientOrigin: null }),
    );
    expect(has(c, "proposal-terms-stale")).toBe(false);
    const send = [...c.querySelectorAll<HTMLButtonElement>("[data-testid=proposal-current] button")].find((b) => b.textContent === en["proposal.send"])!;
    expect(send.disabled).toBe(false);
  });
});

// ── (3) the Bookings card on a phone ─────────────────────────────────────────────────────────────

const CANCELLED: Line = {
  booking_line_id: "l-c",
  trip_id: TRIP,
  supplier_kind: "own",
  agency_supplier_id: "s-1",
  supplier_name: "Kestrel River Cruises",
  status: "cancelled",
  title: "Sunset river cruise",
  confirmation_no: "KR-1",
  service_from: "2026-12-01",
  service_to: "2026-12-01",
  collect: "agency",
  read_only: false,
  aa: null,
  money: {
    cost: { amount: "250.00", currency: "USD" },
    sell: { amount: "330.00", currency: "USD" },
    cancellation_cost: { amount: "79.27", currency: "USD" },
  },
  commission: { commission_id: "c-1", status: "expected", due_on: "2026-12-31", overdue: false },
} as unknown as Line;
const LINES: TripLines = { trip_id: TRIP, lines: [CANCELLED], base_currency: "USD", booking_validation_level: "basic", lost_reason_hint: null };

function phone(matches: boolean) {
  vi.stubGlobal("matchMedia", (query: string) => ({ matches, media: query, addEventListener: () => {}, removeEventListener: () => {}, onchange: null, addListener: () => {}, removeListener: () => {}, dispatchEvent: () => false }));
}

describe("Bookings card at phone width", () => {
  it("renders a card per line with Status, Cost, Sell and Collect labelled, the cancelled badge and the fee note", async () => {
    phone(true);
    const { c } = await mount(createElement(LinesPanel, { tripId: TRIP, view: LINES, suppliers: [], role: "owner", lang: "en", locale: "en-AU" }));
    expect(has(c, "ledger-lines")).toBe(false);
    const card = c.querySelector("[data-testid=line-l-c]")!;
    expect(card.getAttribute("data-layout")).toBe("card");
    const labels = [...card.querySelectorAll("dt")].map((d) => d.textContent);
    expect(labels).toEqual(["status", "day", "supplier", "conf", "cost", "sell", "fx", "collect"].map((k) => en[`ledger.col.${k}` as keyof typeof en]));
    const status = card.querySelector("[data-testid=line-card-status]")!;
    expect(status.textContent).toContain(en["ledger.status.cancelled"]);
    expect(status.querySelector("[data-testid=line-cancellation-fee]")!.textContent).toContain("79.27");
    expect(card.querySelector("[data-testid=line-card-cost]")!.textContent).toContain("250.00");
    expect(card.querySelector("[data-testid=line-card-sell]")!.textContent).toContain("330.00");
    expect(card.querySelector("[data-testid=line-card-collect]")!.textContent).toBe(en["ledger.collect.agency"]);
  });

  it("the assistant's card has no Cost row; a desktop keeps the table", async () => {
    phone(true);
    const { c } = await mount(createElement(LinesPanel, { tripId: TRIP, view: LINES, suppliers: [], role: "assistant", lang: "en", locale: "en-AU" }));
    expect(c.querySelector("[data-testid=line-card-cost]")).toBeNull();
    phone(false);
    const { c: desk } = await mount(createElement(LinesPanel, { tripId: TRIP, view: LINES, suppliers: [], role: "owner", lang: "en", locale: "en-AU" }));
    expect(has(desk, "ledger-lines")).toBe(true);
    expect(has(desk, "ledger-cards")).toBe(false);
    expect(desk.querySelector("[data-testid=line-l-c]")!.tagName).toBe("TR");
  });
});

// ── (4) the moment between the cancel run's answer and the server refresh ───────────────────────

const ISSUED: Invoice = {
  invoice_id: "i-16",
  trip_id: TRIP,
  kind: "balance",
  status: "open",
  number: "INV-000916",
  schedule_id: "s-bal",
  proposal_id: P,
  currency: "USD",
  subtotal_amount: "1260.00",
  tax_amount: "0.00",
  total_amount: "1260.00",
  tax_rate: "0",
  tax_label: "VAT",
  tax_inclusive: true,
  memo: null,
  due_on: "2026-11-11",
  issue_date: "2026-11-01",
  issued_at: "2026-11-01T04:00:00+00:00",
  paid_at: null,
  voided_at: null,
  void_reason: null,
  created_at: "2026-11-01T03:00:00+00:00",
  actions: ["void", "record_payment"],
  balance_amount: "1260.00",
  paid_amount: "0.00",
  refunded_amount: "0.00",
  payments: [],
  refund_requests: [
    { refund_id: "r-1", invoice_id: "i-16", payment_id: "pay-1", amount: "10.00", currency: "USD", reason: "Invented", status: "proposed", requested_by: "member:x", decided_by: null, decided_at: null, decision_note: null, executed_at: null },
  ],
};
const SCHEDULE: LifecycleView["schedule"] = [
  { schedule_id: "s-bal", proposal_id: P, kind: "balance", amount: "1260.00", currency: "USD", due_on: "2026-11-11", status: "due", paid_at: null },
];

function CloseButton() {
  const markClosed = useMarkTripClosed();
  return createElement("button", { "data-testid": "run-done", onClick: markClosed }, "run");
}

function tripPage(closed: boolean): ReactNode {
  return createElement(
    TripClosedProvider,
    { closed },
    createElement(CloseButton),
    createElement(ProposalPanel, { view: lifecycle([], SCHEDULE), terms: [], party: [], role: "owner", lang: "en", locale: "en-AU", clientOrigin: null }),
    createElement(InvoicesPanel, { tripId: TRIP, invoices: [ISSUED], schedule: SCHEDULE, role: "owner", lang: "en", locale: "en-AU", timeZone: "UTC" }),
    createElement(LinesPanel, { tripId: TRIP, view: { ...LINES, lines: [{ ...CANCELLED, status: "confirmed" } as Line] }, suppliers: [], role: "owner", lang: "en", locale: "en-AU" }),
  );
}

const buttons = (c: HTMLElement, sel: string) => [...c.querySelectorAll<HTMLButtonElement>(`${sel} button`)].map((b) => b.textContent);

describe("the cancel run's pending window", () => {
  it("before the run the invoice offers Void and Record payment, the balance reads Due and the commission Expected", async () => {
    phone(false);
    const { c } = await mount(tripPage(false));
    expect(buttons(c, "[data-testid=invoice-row]")).toEqual(expect.arrayContaining([en["invoice.void"], en["payment.record"], en["refund.approve"]]));
    expect(c.querySelector("[data-testid=schedule-row]")!.textContent).toContain(en["proposal.row.due"]);
    expect(c.querySelector("[data-testid=line-commission]")!.textContent).toContain(en["commission.status.expected"]);
    expect(has(c, "invoice-updating")).toBe(false);
  });

  it("after the run's answer the money reads 'Updating' with no action; the server view then restores what is kept", async () => {
    phone(false);
    const { c, root } = await mount(tripPage(false));
    await act(async () => c.querySelector<HTMLButtonElement>("[data-testid=run-done]")!.click());
    expect(c.querySelector("[data-testid=invoice-updating]")!.textContent).toBe(en["invoice.updating"]);
    const shown = buttons(c, "[data-testid=invoice-row]");
    for (const gone of [en["invoice.void"], en["payment.record"], en["refund.approve"], en["refund.reject"]]) expect(shown).not.toContain(gone);
    expect(shown).toContain(en["invoice.document"]); // the printable invoice stays
    const row = c.querySelector("[data-testid=schedule-row]")!;
    expect(row.textContent).toContain(en["trip.updating"]);
    expect(row.textContent).not.toContain(en["proposal.row.due"]);
    const commission = c.querySelector("[data-testid=line-commission]")!;
    expect(commission.getAttribute("data-status")).toBe("updating");
    expect(commission.textContent).toContain(en["trip.updating"]);
    // The refreshed server view says the trip is closed: a kept invoice is workable again (void, payment, refund).
    await act(async () => root.render(tripPage(true)));
    expect(has(c, "invoice-updating")).toBe(false);
    expect(buttons(c, "[data-testid=invoice-row]")).toEqual(expect.arrayContaining([en["invoice.void"], en["payment.record"]]));
    expect(c.querySelector("[data-testid=line-commission]")!.textContent).toContain(en["commission.status.expected"]);
  });
});
