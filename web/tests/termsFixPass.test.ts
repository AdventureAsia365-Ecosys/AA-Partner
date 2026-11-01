// @vitest-environment jsdom
// S262 fix pass (AA-957, AA-952), after round3TermsAndCards.test.ts: (T3) a proposal version naming a booking terms
// version that was never published reads "(not published)", not "(no longer published)", EN + VI; (T4) a refund
// request form opened before the cancel run closes while the run is applied and stays closed once the trip is closed
// (the run proposed that refund); (T5) the terms <select> is controlled: it shows the draft's terms after the server
// view names another version, and keeps the agent's unsaved pick in between. Invented names only.
import { act, createElement, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { BuilderView } from "@/lib/builder";
import { MESSAGES } from "@/lib/i18n";
import type { Invoice } from "@/lib/invoices";
import { termsChoiceLabel, termsChoices, termsStaleNote, type Agreement, type LifecycleView, type Proposal } from "@/lib/proposals";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const refresh = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh, push: () => {} }) }));
const { BuilderPage } = await import("@/components/proposals/BuilderPage");
const { ProposalPanel } = await import("@/components/proposals/ProposalPanel");
const { InvoicesPanel } = await import("@/components/invoices/InvoicesPanel");
const { TripClosedProvider, useMarkTripClosed } = await import("@/components/pipeline/TripClosed");

const en = MESSAGES.en;
const vi_ = MESSAGES.vi;
const TRIP = "00000000-0000-4000-8000-0000000000e1";
const P = "00000000-0000-4000-8000-0000000000e2";
const OLD = "00000000-0000-4000-8000-0000000000e3";
const NEW = "00000000-0000-4000-8000-0000000000e4";
const DRAFT = "00000000-0000-4000-8000-0000000000e5";
const OTHER = "00000000-0000-4000-8000-0000000000e6";

function doc(doc_id: string, version_label: string, status: Agreement["status"]): Agreement {
  return { doc_id, kind: "booking_terms", version_label, status, body_text: "Invented terms", body_sha256: "00", published_at: status === "draft" ? null : "2026-10-01T00:00:00Z" };
}
const TERMS = [doc(OLD, "Juniper-1", "retired"), doc(NEW, "Juniper-2", "published"), doc(DRAFT, "Juniper-3", "draft")];

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
const optionTexts = (sel: HTMLSelectElement) => [...sel.options].map((o) => o.textContent);

/** Picks a value in a controlled <select> the way a browser does (the value setter React tracks, then "change"). */
function choose(sel: HTMLSelectElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")!.set!;
  setter.call(sel, value);
  sel.dispatchEvent(new Event("change", { bubbles: true }));
}

// ── T3: a never-published version has its own words ──────────────────────────────────────────────

describe("T3: terms labels by state", () => {
  it("a draft version reads '(not published)', a retired one '(no longer published)', EN + VI", () => {
    const draft = termsChoices(TERMS, DRAFT).stale!;
    expect(draft).toEqual({ doc_id: DRAFT, version_label: "Juniper-3", stale: true, state: "draft" });
    expect(termsChoiceLabel(draft, "en")).toBe("Juniper-3 (not published)");
    expect(termsChoiceLabel(draft, "vi")).toBe("Juniper-3 (chưa công bố)");
    const retired = termsChoices(TERMS, OLD).stale!;
    expect(termsChoiceLabel(retired, "en")).toBe("Juniper-1 (no longer published)");
    expect(termsChoiceLabel(retired, "vi")).toBe("Juniper-1 (không còn công bố)");
    const removed = termsChoices(TERMS, "gone").stale!;
    expect(termsChoiceLabel(removed, "en")).toBe(en["proposal.termsRetiredUnknown"]);
    expect(termsChoiceLabel(termsChoices(TERMS, NEW).options[0], "en")).toBe("Juniper-2");
    // The note says the draft version was never published (not "no longer").
    expect(termsStaleNote(draft, "en")).toContain("Juniper-3 (not published), a version that was never published");
    expect(termsStaleNote(draft, "vi")).toContain("chưa từng được công bố");
    expect(termsStaleNote(retired, "en")).toContain("no longer the published version");
    expect(en["proposal.termsNotPublished"]).toBeTruthy();
    expect(vi_["proposal.termsNotPublished"]).not.toBe(en["proposal.termsNotPublished"]);
    expect(vi_["proposal.termsStaleNoteDraft"]).not.toBe(en["proposal.termsStaleNoteDraft"]);
  });

  it("the builder and the text proposal list a draft version as not published", async () => {
    const { c } = await mount(createElement(BuilderPage, { initial: builderView(DRAFT, "draft"), role: "agent", lang: "en", locale: "en-AU", terms: TERMS, suppliers: [] }));
    expect(optionTexts(c.querySelector<HTMLSelectElement>("[data-testid=builder-terms]")!)).toEqual(["Juniper-2", "Juniper-3 (not published)"]);
    expect(c.querySelector("[data-testid=builder-terms-stale]")!.textContent).toContain("Juniper-3 (not published)");
    const { c: c2 } = await mount(
      createElement(ProposalPanel, { view: lifecycle([{ ...TEXT_DRAFT, terms_doc_id: DRAFT }]), terms: TERMS, party: [], role: "owner", lang: "vi", locale: "vi-VN", clientOrigin: null }),
    );
    expect(c2.querySelector("[data-testid=proposal-terms-stale]")!.textContent).toContain("Juniper-3 (chưa công bố)");
  });
});

// ── T5: the terms select is controlled ──────────────────────────────────────────────────────────

function builderView(terms_doc_id: string | null, terms_status: BuilderView["terms_status"], edit_seq = 4): BuilderView {
  return {
    proposal_id: P,
    trip_id: TRIP,
    trip_stage: "proposal",
    version: 2,
    status: "draft",
    kind: "builder",
    edit_seq,
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
    checks: terms_status === "published" ? { publishable: true, blocking: [], warnings: [] } : { publishable: false, blocking: ["terms_retired"], warnings: [] },
    stage2: { accommodation_tier: null, group_pref: null, pace: null, party_adults: 2, party_children: 0 },
  };
}

const TEXT_DRAFT: Proposal = {
  proposal_id: P,
  version: 1,
  status: "draft",
  body: "Four days in the cedar hills.",
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
  return { trip_id: TRIP, stage: "proposal", proposals, schedule, payments: [], deposit_paid: false, portal_unlocked: false, portal: { s3: 0, waiver_required: false, travellers: [] } };
}

describe("T5: the terms select follows the draft's terms", () => {
  it("builder: keeps the agent's pick until the page reads a new version state, then shows the version's terms", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(json(200, builderView(NEW, "published", 5)));
    vi.stubGlobal("fetch", fetchMock);
    const { c } = await mount(createElement(BuilderPage, { initial: builderView(OLD, "retired"), role: "agent", lang: "en", locale: "en-AU", terms: TERMS, suppliers: [] }));
    const select = () => c.querySelector<HTMLSelectElement>("[data-testid=builder-terms]")!;
    expect(select().value).toBe(OLD);
    await act(async () => choose(select(), NEW));
    expect(select().value).toBe(NEW); // the pick holds (a controlled select without onChange would snap back)
    await act(async () => choose(select(), OLD));
    expect(select().value).toBe(OLD);
    await act(async () => c.querySelector<HTMLButtonElement>("[data-testid=builder-terms-use-current] button")!.click());
    expect(select().value).toBe(NEW);
    expect(optionTexts(select())).toEqual(["Juniper-2"]);
  });

  it("text proposal: the open edit form shows the terms the server view names after the switch", async () => {
    // Two published versions in the list, so the version the form showed stays an option: an uncontrolled select (the
    // old defaultValue) kept showing it after the draft moved to the other one.
    const both = [doc(NEW, "Juniper-2", "published"), doc(OTHER, "Juniper-4", "published")];
    const draft = { ...TEXT_DRAFT, terms_doc_id: NEW };
    const props = (p: Proposal) => ({ view: lifecycle([p]), terms: both, party: [], role: "owner", lang: "en" as const, locale: "en-AU" as const, clientOrigin: null });
    const { c, root } = await mount(createElement(ProposalPanel, props(draft)));
    const edit = [...c.querySelectorAll<HTMLButtonElement>("[data-testid=proposal-current] button")].find((b) => b.textContent === en["proposal.edit"])!;
    await act(async () => edit.click());
    const select = () => c.querySelector<HTMLSelectElement>("[data-testid=proposal-terms]")!;
    expect(select().value).toBe(NEW);
    // The draft now names Juniper-4 (another tab, or "Use the current terms" then router.refresh()).
    await act(async () => root.render(createElement(ProposalPanel, props({ ...draft, terms_doc_id: OTHER }))));
    expect(has(c, "proposal-form")).toBe(true);
    expect(select().value).toBe(OTHER);
    // An unsaved pick holds across a refresh that names the same version.
    await act(async () => choose(select(), NEW));
    await act(async () => root.render(createElement(ProposalPanel, props({ ...draft, terms_doc_id: OTHER }))));
    expect(select().value).toBe(NEW);
  });
});

// ── T4: the refund request form and the cancel run ───────────────────────────────────────────────

const PAID: Invoice = {
  invoice_id: "i-21",
  trip_id: TRIP,
  kind: "deposit",
  status: "paid",
  number: "INV-000921",
  schedule_id: "s-dep",
  proposal_id: P,
  currency: "USD",
  subtotal_amount: "540.00",
  tax_amount: "0.00",
  total_amount: "540.00",
  tax_rate: "0",
  tax_label: "VAT",
  tax_inclusive: true,
  memo: null,
  due_on: "2026-11-05",
  issue_date: "2026-11-01",
  issued_at: "2026-11-01T04:00:00+00:00",
  paid_at: "2026-11-01T05:00:00+00:00",
  voided_at: null,
  void_reason: null,
  created_at: "2026-11-01T03:00:00+00:00",
  actions: [],
  balance_amount: "0.00",
  paid_amount: "540.00",
  refunded_amount: "0.00",
  payments: [
    { payment_id: "pay-21", entry_kind: "payment", amount: "540.00", currency: "USD", method: "bank_transfer", received_on: "2026-11-01", provider: "manual", reference: null, refund_of: null, refundable_amount: "540.00" },
  ],
  refund_requests: [],
};
/** The server view after the cancel run: it proposed the refund of that payment (the agent approves it next). */
const AFTER_RUN: Invoice = {
  ...PAID,
  refund_requests: [
    { refund_id: "r-21", invoice_id: "i-21", payment_id: "pay-21", amount: "540.00", currency: "USD", reason: "Trip cancelled", status: "proposed", requested_by: "member:x", decided_by: null, decided_at: null, decision_note: null, executed_at: null },
  ],
};

function CloseButton() {
  const markClosed = useMarkTripClosed();
  return createElement("button", { "data-testid": "run-done", onClick: markClosed }, "run");
}

function tripPage(closed: boolean, invoice: Invoice): ReactNode {
  return createElement(
    TripClosedProvider,
    { closed },
    createElement(CloseButton),
    createElement(InvoicesPanel, { tripId: TRIP, invoices: [invoice], schedule: [], role: "owner", lang: "en", locale: "en-AU", timeZone: "UTC" }),
  );
}

const requestButton = (c: HTMLElement) => [...c.querySelectorAll<HTMLButtonElement>("[data-testid=payment-row] button")].find((b) => b.textContent === en["refund.request"]);

describe("T4: the refund request form during the cancel run", () => {
  it("closes when the run's answer is in and stays closed once the trip is closed with the refund proposed", async () => {
    const { c, root } = await mount(tripPage(false, PAID));
    await act(async () => requestButton(c)!.click());
    expect(has(c, "refund-request-form")).toBe(true);
    await act(async () => c.querySelector<HTMLButtonElement>("[data-testid=run-done]")!.click());
    expect(has(c, "refund-request-form")).toBe(false); // nothing to submit while the run is applied
    await act(async () => root.render(tripPage(true, AFTER_RUN)));
    expect(has(c, "refund-request-form")).toBe(false); // the run proposed this refund: no second request
    expect(requestButton(c)).toBeUndefined();
  });

  it("on a closed trip with nothing proposed the form opens again on demand, never by itself", async () => {
    const { c, root } = await mount(tripPage(false, PAID));
    await act(async () => requestButton(c)!.click());
    await act(async () => c.querySelector<HTMLButtonElement>("[data-testid=run-done]")!.click());
    await act(async () => root.render(tripPage(true, PAID)));
    expect(has(c, "refund-request-form")).toBe(false);
    await act(async () => requestButton(c)!.click());
    expect(has(c, "refund-request-form")).toBe(true);
  });

  it("a trip the server already shows closed keeps a form the agent opens", async () => {
    const { c, root } = await mount(tripPage(true, PAID));
    await act(async () => requestButton(c)!.click());
    await act(async () => root.render(tripPage(true, PAID)));
    expect(has(c, "refund-request-form")).toBe(true);
  });
});
