// @vitest-environment jsdom
// AA-957 S262 round 3 (design v3 C12 §2.7, second live acceptance): a cancelled (or cancelling) trip is closed on every
// card, and the cancel run closes every card in the same frame as the header (before, the Proposal and Invoices cards
// offered billing for the ~2 s of the server refresh); Signed documents, Stage 3 and the AA hold no longer read as
// active; messages stay open with a note; Traveller info words Booking details by stage; counted words take the
// singular for 1 ("1 commission", "1 day"); a cancelled commission "Was expected"; the claim shows on a claimed row
// only; /payments has marked tabs and phone cards; the filter strip keeps the active tab in view; the support banner
// prints the agency's zone.
import { readFileSync } from "node:fs";
import path from "node:path";
import { act, createElement, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToString } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Commission } from "@/lib/commissions";
import { tripsCount } from "@/lib/clients";
import { MESSAGES, plural, type MessageKey } from "@/lib/i18n";
import type { Receivable } from "@/lib/invoices";
import type { Line, TripLines } from "@/lib/ledger";
import { bookingDetailsKey, type CancelPreview, type Cancellation, type Member, type TripView } from "@/lib/pipeline";
import type { LifecycleView, Proposal, TripAgreements } from "@/lib/proposals";
import { pluralsEn } from "@/lib/i18nPlurals";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const refresh = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh, push: () => {} }) }));
const { TripClosedProvider } = await import("@/components/pipeline/TripClosed");
const { TripPanel } = await import("@/components/pipeline/TripPanel");
const { ProposalPanel } = await import("@/components/proposals/ProposalPanel");
const { InvoicesPanel } = await import("@/components/invoices/InvoicesPanel");
const { LinesPanel } = await import("@/components/ledger/LinesPanel");
const { TripAgreementsPanel } = await import("@/components/proposals/TripAgreementsPanel");
const { DocumentsPanel } = await import("@/components/clients/DocumentsPanel");
const { MessagesPanel } = await import("@/components/mail/MessagesPanel");
const { CommissionsTable, StatusCell } = await import("@/components/commissions/CommissionsTable");
const { ReceivableFilters, ReceivablesTable } = await import("@/components/invoices/ReceivablesTable");
const { scrollToShow } = await import("@/components/FilterTabs");
const { SupportBanner } = await import("@/components/support/SupportBanner");
const { leadSourceLabel } = await import("@/lib/clients");
const { ToastProvider } = await import("@/app/_kit");

/** The A-12 table inside the shell's toast provider (its follow-up forms report success as a toast). */
function commissionsHtml(rows: Commission[]): string {
  return renderToString(createElement(ToastProvider, null, createElement(CommissionsTable, { commissions: rows, base: "USD", lang: "en", locale: "en-AU" })));
}

const ID = "00000000-0000-4000-8000-0000000000a1";
const TRAVELLER = "00000000-0000-4000-8000-0000000000a2";
const SUPPLIER = "00000000-0000-4000-8000-0000000000a3";

const roots: Root[] = [];
afterEach(() => {
  for (const r of roots.splice(0)) act(() => r.unmount());
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
  refresh.mockReset();
});

async function mount(element: ReactNode): Promise<HTMLElement> {
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

// ── fixtures (invented names only) ──────────────────────────────────────────────────────────────

const VIEW: TripView = {
  trip: {
    trip_id: ID,
    title: "Lantern coast for two",
    stage: "booked",
    stage_entered_at: "2026-10-24T04:02:20Z",
    waiting_on: "agency",
    start_date: "2026-12-01",
    end_date: "2026-12-04",
    month: null,
    date_flex_days: 1,
    nights: 1,
    destinations: ["vietnam"],
    party_adults: 1,
    party_child_ages: [],
    party_type: null,
    budget_band: null,
    trip_style: null,
    owner_member_id: "m1",
    is_mine: true,
    lead_source: "synthetic_seed",
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

const BUILDER: Proposal = {
  proposal_id: "p-b1",
  version: 2,
  status: "accepted",
  body: "",
  currency: "USD",
  total_amount: "1800.00",
  deposit_amount: "540.00",
  balance_due_on: null,
  terms_doc_id: null,
  price_hash: null,
  sent_at: "2026-10-20T00:00:00Z",
  accepted_at: "2026-10-21T00:00:00Z",
  kind: "builder",
  view_count: 1,
  last_viewed_at: "2026-10-21T00:00:00Z",
  hold_expires_at: "2026-11-03T00:00:00Z",
};

const LIFECYCLE: LifecycleView = {
  trip_id: ID,
  stage: "booked",
  proposals: [BUILDER],
  schedule: [
    { schedule_id: "s-dep", proposal_id: "p-b1", kind: "deposit", amount: "540.00", currency: "USD", due_on: "2026-10-20", status: "paid", paid_at: "2026-10-20T00:00:00Z" },
    { schedule_id: "s-bal", proposal_id: "p-b1", kind: "balance", amount: "1260.00", currency: "USD", due_on: "2026-11-20", status: "due", paid_at: null },
  ],
  payments: [],
  deposit_paid: true,
  portal_unlocked: true,
  portal: {
    s3: 0,
    waiver_required: true,
    travellers: [{ traveler_id: TRAVELLER, role: "lead", details_done: false, missing: [], waiver_signed: false, portal: { access_id: "a1", status: "active", last_sign_in_at: null, stage3_submitted_at: null } }],
  },
};
const PARTY = [{ traveler_id: TRAVELLER, display_name: "Mai Lantern", role: "lead" }];

const OWN_LINE: Line = {
  booking_line_id: "l-1",
  trip_id: ID,
  supplier_kind: "own",
  agency_supplier_id: SUPPLIER,
  supplier_name: "Harbour Lodge",
  status: "planning",
  aa_sub_status: null,
  title: "Lodge 2 nights",
  confirmation_no: null,
  service_from: "2026-12-01",
  service_to: "2026-12-03",
  collect: "agency",
  confirm_class: null,
  visible_to_travelers: false,
  confirmed_at: null,
  read_only: false,
  aa: null,
} as unknown as Line;
const LINES: TripLines = { trip_id: ID, lines: [OWN_LINE], base_currency: "USD", booking_validation_level: "basic", lost_reason_hint: null };
const SUPPLIERS = [{ agency_supplier_id: SUPPLIER, kind: "own", name: "Harbour Lodge" }] as never[];

const PREVIEW: CancelPreview = {
  trip_id: ID,
  from_stage: "booked",
  cancel_reason_code: "client_request",
  dry_run: true,
  effects: [{ kind: "aa_bookings_cancelled", count: 1 }],
  aa_bookings: [{ booking_id: "b-h1", titles: ["River 3 days"], asked: false, failed_code: null, fee: { amount: "10.00", currency: "USD", fee_pct: "10.00", days_before_departure: 1 } }],
  supplier_lines: [],
  payments: [],
  commissions: [],
  terms: null,
  booking_unavailable: false,
  cancellation: null,
};
const PENDING: Cancellation = { status: "pending", reason_code: "client_request", from_stage: "booked", aa_bookings: 1, requested_at: "2026-10-31T21:04:03Z", finished_at: null };

function page(closed: boolean, view: TripView = VIEW): ReactNode {
  return createElement(
    TripClosedProvider,
    { closed },
    createElement(TripPanel, { view, members: MEMBERS, role: "owner", lang: "en", locale: "en-AU", stage2: null, timeZone: "Asia/Ho_Chi_Minh" }),
    createElement(ProposalPanel, { view: LIFECYCLE, terms: [], party: PARTY, role: "owner", lang: "en", locale: "en-AU", clientOrigin: null, timeZone: "Asia/Ho_Chi_Minh" }),
    createElement(InvoicesPanel, { tripId: ID, invoices: [], schedule: LIFECYCLE.schedule, role: "owner", lang: "en", locale: "en-AU", timeZone: "Asia/Ho_Chi_Minh" }),
    createElement(LinesPanel, { tripId: ID, view: LINES, suppliers: SUPPLIERS, role: "owner", lang: "en", locale: "en-AU" }),
  );
}

const text = (c: HTMLElement) => c.textContent ?? "";
const has = (c: HTMLElement, id: string) => c.querySelector(`[data-testid=${id}]`) !== null;

// ── 1 + 2: one closed state for every card ─────────────────────────────────────────────────────

describe("the cancel run closes every card at once", () => {
  it("a live trip offers billing and planning on every card", async () => {
    const c = await mount(page(false));
    expect(text(c)).toContain(MESSAGES.en["proposal.markPaid"]);
    expect(has(c, "invoice-from-schedule")).toBe(true);
    expect(text(c)).toContain(MESSAGES.en["aa.add.open"]);
    expect(text(c)).toContain(MESSAGES.en["ledger.add"]);
    expect(text(c)).toContain(MESSAGES.en["ledger.confirm"]);
    expect(has(c, "trip-dates")).toBe(true);
    expect(text(c)).toContain(MESSAGES.en["trip.delete"]);
    expect(has(c, "builder-open")).toBe(true);
  });

  it("right after the run, before the server refresh, no card offers billing, a new AA item, a line, dates or delete", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(json(200, PREVIEW)).mockResolvedValueOnce(json(200, { ...PREVIEW, dry_run: false, stage: "booked", cancellation: PENDING })));
    const c = await mount(page(false));
    await act(async () => c.querySelector<HTMLButtonElement>("[data-testid=trip-cancel] button")!.click());
    await setValue(c.querySelector<HTMLSelectElement>("[data-testid=cancel-reason]")!, "client_request");
    await setValue(c.querySelector<HTMLInputElement>("[data-testid=cancel-typed]")!, "CANCEL");
    await act(async () => c.querySelector<HTMLButtonElement>("[data-testid=cancel-confirm] button")!.click());
    // The server view has not changed (refresh is a no-op here): only the run's answer closed the page.
    expect(has(c, "trip-cancellation-pending")).toBe(true);
    expect(has(c, "proposal-closed")).toBe(true);
    expect(text(c)).not.toContain(MESSAGES.en["proposal.markPaid"]);
    expect(has(c, "invoice-from-schedule")).toBe(false);
    expect(text(c)).not.toContain(MESSAGES.en["invoice.newTyped"]);
    expect(has(c, "ledger-closed")).toBe(true);
    expect(text(c)).not.toContain(MESSAGES.en["aa.add.open"]);
    expect(text(c)).not.toContain(MESSAGES.en["ledger.confirm"]);
    expect(has(c, "trip-dates")).toBe(false);
    expect(has(c, "trip-dates-closed")).toBe(true);
    expect(text(c)).not.toContain(MESSAGES.en["trip.delete"]); // not while AA has not confirmed
    expect(has(c, "trip-closed-pending")).toBe(true);
    expect(has(c, "builder-open")).toBe(false);
  });

  it("a run that finished at once (no AA booking) reads cancelled, not 'being cancelled', before the server refresh", async () => {
    const done: Cancellation = { ...PENDING, status: "done", aa_bookings: 0, finished_at: "2026-10-31T21:04:04Z" };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(json(200, PREVIEW)).mockResolvedValueOnce(json(200, { ...PREVIEW, dry_run: false, stage: "cancelled", cancellation: done })));
    const c = await mount(page(false));
    await act(async () => c.querySelector<HTMLButtonElement>("[data-testid=trip-cancel] button")!.click());
    await setValue(c.querySelector<HTMLSelectElement>("[data-testid=cancel-reason]")!, "client_request");
    await setValue(c.querySelector<HTMLInputElement>("[data-testid=cancel-typed]")!, "CANCEL");
    await act(async () => c.querySelector<HTMLButtonElement>("[data-testid=cancel-confirm] button")!.click());
    expect(has(c, "trip-closed-pending")).toBe(false);
    expect(has(c, "trip-cancellation-pending")).toBe(false);
    expect(text(c)).not.toContain(MESSAGES.en["trip.closedPending"]);
    expect(has(c, "trip-dates-closed")).toBe(true); // "This trip is cancelled: …"
    expect(has(c, "ledger-closed")).toBe(true);
  });

  it("a trip the server already shows as cancelled keeps Delete (the API decides) but nothing else", async () => {
    const done: TripView = { ...VIEW, trip: { ...VIEW.trip, stage: "cancelled", cancel_reason_code: "client_request" }, allowed_moves: [], cancellation: { ...PENDING, status: "done", finished_at: "2026-10-31T21:05:00Z" } };
    const c = await mount(page(true, done));
    expect(text(c)).toContain(MESSAGES.en["trip.delete"]);
    expect(has(c, "trip-closed-pending")).toBe(false);
    expect(has(c, "trip-dates")).toBe(false);
    expect(has(c, "ledger-closed")).toBe(true);
    expect(has(c, "proposal-closed")).toBe(true);
  });
});

// ── 1: the cards of a closed trip, one by one ──────────────────────────────────────────────────

describe("a closed trip reads closed on every card", () => {
  it("Proposal: no Open builder, no client choices, no AA hold 'until', Stage 3 says nothing more is asked", () => {
    const open = renderToString(createElement(ProposalPanel, { view: LIFECYCLE, terms: [], party: PARTY, role: "owner", lang: "en", locale: "en-AU", clientOrigin: null }));
    expect(open).toContain('data-testid="builder-activity"');
    expect(open).toContain("AA places held until");
    expect(open).toContain(MESSAGES.en["proposal.portalOpen"]);
    const html = renderToString(createElement(ProposalPanel, { view: LIFECYCLE, terms: [], party: PARTY, role: "owner", lang: "en", locale: "en-AU", clientOrigin: null, closed: true }));
    for (const gone of ['data-testid="builder-open"', 'data-testid="builder-activity"', "AA places held until", MESSAGES.en["proposal.portalOpen"]]) expect(html).not.toContain(gone);
    expect(html).toContain(MESSAGES.en["proposal.portalClosed"]);
    expect(html).toContain(MESSAGES.en["builder.closed"]);
    expect(html).toContain("Viewed once"); // the singular (S262 round 3)
  });

  it("Proposal Stage 3 rows: what is missing reads 'not needed' on a closed trip, never as a to-do", () => {
    const open = renderToString(createElement(ProposalPanel, { view: LIFECYCLE, terms: [], party: PARTY, role: "owner", lang: "en", locale: "en-AU", clientOrigin: null }));
    expect(open).toContain(MESSAGES.en["proposal.detailsMissing"]);
    expect(open).toContain(MESSAGES.en["proposal.waiverPending"]);
    for (const lang of ["en", "vi"] as const) {
      const html = renderToString(createElement(ProposalPanel, { view: LIFECYCLE, terms: [], party: PARTY, role: "owner", lang, locale: "en-AU", clientOrigin: null, closed: true }));
      expect(html).not.toContain(MESSAGES[lang]["proposal.detailsMissing"]);
      expect(html).not.toContain(MESSAGES[lang]["proposal.waiverPending"]);
      expect(html).toContain(MESSAGES[lang]["proposal.detailsNotNeeded"]);
      expect(html).toContain(MESSAGES[lang]["proposal.waiverNotNeeded"]);
    }
  });

  it("Signed documents: nothing waits for a signature", () => {
    const view: TripAgreements = {
      trip_id: ID,
      waiver_required: true,
      all_signed: false,
      travellers: [{ traveler_id: TRAVELLER, role: "lead", waiver_signed: false, signature: null }],
      terms: { published: null, reaccept_required: true, acceptances: [] },
      events: [],
    };
    const open = renderToString(createElement(TripAgreementsPanel, { view, party: PARTY, lang: "en", locale: "en-AU" }));
    expect(open).toContain(MESSAGES.en["esign.pending"]);
    expect(open).toContain(MESSAGES.en["esign.notSigned"]);
    const html = renderToString(createElement(TripAgreementsPanel, { view, party: PARTY, lang: "en", locale: "en-AU", closed: true }));
    expect(html).not.toContain(MESSAGES.en["esign.pending"]);
    expect(html).not.toContain(MESSAGES.en["esign.termsReaccept"]);
    expect(html).toContain(MESSAGES.en["esign.closed"]);
    expect(html).toContain(MESSAGES.en["esign.notNeeded"]);
  });

  it("Documents: no 'Visible to the traveller' and no showing a hidden file; hiding a shown one stays", () => {
    const docs = [
      { document_id: "d1", filename: "Voucher.pdf", kind: "ticket", size_bytes: 10, created_at: "2026-10-30T00:00:00Z", visible_to_travelers: true, write_only: false },
      { document_id: "d2", filename: "Notes.pdf", kind: "other", size_bytes: 10, created_at: "2026-10-30T00:00:00Z", visible_to_travelers: false, write_only: false },
    ] as never[];
    const open = renderToString(createElement(DocumentsPanel, { documents: docs, tripId: ID, role: "owner", lang: "en", locale: "en-AU" }));
    expect(open).toContain(MESSAGES.en["docs.visible"]);
    const html = renderToString(createElement(DocumentsPanel, { documents: docs, tripId: ID, role: "owner", lang: "en", locale: "en-AU", closed: true }));
    expect(html).not.toContain(MESSAGES.en["docs.visible"]);
    expect(html).toContain('data-testid="documents-closed"');
    // One toggle left: "Hidden" on the shown file (hide it); the hidden file offers no "Shown".
    expect(html.split(`>${MESSAGES.en["docs.hidden"]}<`).length - 1).toBe(2); // the hidden file's badge + the toggle
    expect(html.split(`>${MESSAGES.en["docs.shown"]}<`).length - 1).toBe(1); // the shown file's badge only
  });

  it("Messages: the composer stays (refund and wrap-up) with a note that the trip is cancelled", () => {
    const html = renderToString(createElement(MessagesPanel, { tripId: ID, conversations: [], savedReplies: [], lang: "en", locale: "en-AU", closed: true }));
    expect(html).toContain('data-testid="messages-composer"');
    expect(html).toContain(MESSAGES.en["messages.closed"]);
    expect(renderToString(createElement(MessagesPanel, { tripId: ID, conversations: [], savedReplies: [], lang: "en", locale: "en-AU" }))).not.toContain("messages-closed");
  });
});

// ── 9: Booking details by stage ─────────────────────────────────────────────────────────────────

describe("Traveller info: Booking details", () => {
  it("opens later before booking, comes from the travellers once booked, is not needed on a cancelled trip", () => {
    expect(bookingDetailsKey("proposal", false)).toBe("info.bookingLocked");
    expect(bookingDetailsKey("booked", false)).toBe("info.bookingOpen");
    expect(bookingDetailsKey("travelling", false)).toBe("info.bookingOpen");
    expect(bookingDetailsKey("completed", false)).toBe("info.bookingDone"); // over: not an open request
    expect(bookingDetailsKey("booked", true)).toBe("info.bookingClosed");
    expect(bookingDetailsKey("cancelled", false)).toBe("info.bookingClosed");
  });
  it("a booked trip no longer says 'Opens once the trip is booked.'", async () => {
    const c = await mount(page(false));
    const note = c.querySelector("[data-testid=info-s3-note]")!.textContent;
    expect(note).toBe(MESSAGES.en["info.bookingOpen"]);
    expect(text(c)).not.toContain(MESSAGES.en["info.bookingLocked"]);
  });
});

// ── 4: plurals ─────────────────────────────────────────────────────────────────────────────────

describe("counted words", () => {
  it("take the singular for 1 in English and keep Vietnamese as it is", () => {
    expect(plural("en", "commissions.total.count", 1)).toBe("1 commission");
    expect(plural("en", "commissions.total.count", 2)).toBe("2 commissions");
    expect(plural("en", "commission.overdueDays", 1)).toBe("1 day overdue");
    expect(plural("en", "trip.adults", 1)).toBe("1 adult");
    expect(plural("en", "cancelTrip.aaFee", 1, { amount: "10.00 USD", pct: "10", days: 1 })).toBe("Fee 10.00 USD (10 %, 1 day before departure)");
    expect(plural("vi", "commissions.total.count", 1)).toBe("1 khoản hoa hồng");
    expect(plural("vi", "commission.overdueDays", 1)).toBe(plural("vi", "commission.overdueDays", 2).replace("2", "1"));
    expect(tripsCount("en", 1)).toBe("1 trip");
    expect(tripsCount("en", 3)).toBe("3 trips");
    expect(tripsCount("vi", 1)).toBe("1 chuyến");
    expect(plural("en", "clients.together", 1)).toBe("1 trip together");
    expect(plural("en", "clients.together", 2)).toBe("2 trips together");
    expect(plural("en", "messages.attachments", 1)).toBe("1 attachment kept in Documents");
    expect(plural("en", "messages.attachments", 4)).toBe("4 attachments kept in Documents");
    expect(plural("en", "reports.picks.total", 1, { n: "1" })).toBe("1 choice made by clients");
    expect(plural("en", "reports.money.missing", 1)).toBe("1 confirmed line has no cost or price yet; its margin is not counted.");
    expect(plural("en", "pipeline.bulkPreview", 1, { stage: "Booked" })).toBe("1 trip will move to Booked.");
    expect(plural("en", "pipeline.bulkConfirm", 1)).toBe("Move 1 trip");
    expect(plural("en", "pipeline.bulkBlocked", 2)).toBe("2 trips cannot move:");
  });
  it("every singular has its counted message and the same slots, in both languages", () => {
    const slots = (s: string) => [...s.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();
    for (const key of Object.keys(pluralsEn)) {
      const base = key.slice(0, -".one".length) as MessageKey;
      expect(MESSAGES.en[base], base).toBeTruthy();
      for (const lang of ["en", "vi"] as const) {
        const one = MESSAGES[lang][key as MessageKey];
        expect(slots(one).filter((s) => s !== "n"), `${lang}.${key}`).toEqual(slots(MESSAGES[lang][base]).filter((s) => s !== "n"));
      }
    }
  });
  it("the trip card reads '1 night' and '1 adult', not '1 nights' / '1 adults'", async () => {
    const c = await mount(page(false));
    expect(text(c)).toContain("1 night ·");
    expect(text(c)).toContain("1 adult");
    expect(text(c)).not.toMatch(/\b1 (nights|adults|days)\b/);
  });
});

// ── 8: lead sources ────────────────────────────────────────────────────────────────────────────

describe("lead sources", () => {
  const source = readFileSync(path.resolve(__dirname, "../../api/app/lead_sources.py"), "utf8");
  const body = source.slice(source.indexOf("LEAD_SOURCES"), source.indexOf("\n)", source.indexOf("LEAD_SOURCES")));
  const codes = [...body.matchAll(/"([a-z][a-z0-9_]*)"/g)].map((m) => m[1]);
  it("reads the API's list (the code of the live evidence among them)", () => {
    expect(codes).toContain("synthetic_seed");
    expect(codes.length).toBeGreaterThanOrEqual(5);
  });
  it.each(["en", "vi"] as const)("every code the API writes has a label (%s)", (lang) => {
    for (const code of codes) expect(MESSAGES[lang][`source.${code}` as MessageKey], `${lang} source.${code}`).toBeTruthy();
  });
  it("an unknown code reads as words, never raw; none is a dash", () => {
    expect(leadSourceLabel("en", "facebook_ads")).toBe("Facebook ads");
    expect(leadSourceLabel("vi", "synthetic_seed")).toBe(MESSAGES.vi["source.synthetic_seed"]);
    expect(leadSourceLabel("en", null)).toBe("—");
  });
  it("the trip card shows the label, not 'synthetic_seed'", async () => {
    const c = await mount(page(false));
    expect(c.querySelector("[data-testid=trip-source]")!.textContent).toBe(MESSAGES.en["source.synthetic_seed"]);
    expect(text(c)).not.toContain("synthetic_seed");
  });
});

// ── 3 + 6: commission rows ─────────────────────────────────────────────────────────────────────

function commission(over: Partial<Commission> = {}): Commission {
  return {
    commission_id: "c1",
    trip_id: ID,
    trip_title: "Lantern coast for two",
    trip_stage: "booked",
    booking_line_id: "l-1",
    line_title: "Lodge 2 nights",
    line_status: "confirmed",
    service_from: null,
    service_to: null,
    agency_supplier_id: SUPPLIER,
    supplier_name: "Harbour Lodge",
    status: "expected",
    due_on: "2026-12-13",
    overdue: false,
    days_overdue: 0,
    claimed_at: null,
    claim_ref: null,
    close_reason: null,
    note: null,
    keep_on_cancel: false,
    commission_expected: { amount: "25.00", currency: "USD" },
    commission_received: null,
    ...over,
  } as Commission;
}

describe("commission rows", () => {
  it("a cancelled commission 'Was expected', an open one 'Expected'", () => {
    const html = commissionsHtml([commission({ status: "cancelled", close_reason: "trip_cancelled" })]);
    expect(html).toContain(MESSAGES.en["commissions.col.wasExpected"]);
    expect(html).not.toContain(`>${MESSAGES.en["commissions.col.expected"]}<`);
    const live = commissionsHtml([commission()]);
    expect(live).toContain(`>${MESSAGES.en["commissions.col.expected"]}<`);
  });
  it("'Your claim' shows on a claimed row only (a withdrawn claim's reference is not shown on an expected row)", () => {
    const stale = renderToString(createElement(StatusCell, { c: commission({ claim_ref: "SV-INV-1" }), lang: "en" }));
    expect(stale).not.toContain("SV-INV-1");
    const claimed = renderToString(createElement(StatusCell, { c: commission({ status: "claimed", claim_ref: "SV-INV-1", claimed_at: "2026-10-30T00:00:00Z" }), lang: "en" }));
    expect(claimed).toContain("Your claim: SV-INV-1");
  });
  it("one day overdue is '1 day overdue'", () => {
    const html = commissionsHtml([commission({ overdue: true, days_overdue: 1 })]);
    expect(html).toContain("1 day overdue");
  });
});

// ── 5 + 7: filter tabs, /payments ──────────────────────────────────────────────────────────────

describe("filter tabs and /payments", () => {
  it("scrolls the strip just enough to show the active tab", () => {
    const strip = { left: 0, right: 358 };
    expect(scrollToShow(0, strip, { left: 380, right: 450 })).toBe(450 - 342); // off to the right: brought in
    expect(scrollToShow(200, strip, { left: -40, right: 20 })).toBe(200 - 56); // off to the left
    expect(scrollToShow(0, strip, { left: 100, right: 160 })).toBe(0); // already in view
    expect(scrollToShow(10, strip, { left: -100, right: -20 })).toBe(0); // never below 0
  });
  it("marks the active /payments filter as the kit tab (aria-selected), not a plain link", async () => {
    const c = await mount(createElement(ReceivableFilters, { status: "paid", lang: "en" }));
    const tabs = [...c.querySelectorAll('[role="tab"]')];
    expect(tabs.map((t) => t.textContent)).toEqual([MESSAGES.en["receivables.unpaid"], MESSAGES.en["invoice.status.draft"], MESSAGES.en["invoice.status.paid"], MESSAGES.en["invoice.status.void"]]);
    expect(tabs.filter((t) => t.getAttribute("aria-selected") === "true").map((t) => t.textContent)).toEqual([MESSAGES.en["invoice.status.paid"]]);
    expect(c.querySelector("[data-testid=receivables-filters]")!.getAttribute("data-active")).toBe("paid");
  });
  it("lists invoices on the kit DataTable (phone card mode) with the amount due, due date and status", () => {
    const inv: Receivable = { invoice_id: "i1", trip_id: ID, trip_title: "Lantern coast for two", kind: "balance", status: "open", number: "INV-000021", currency: "USD", total_amount: "1260.00", balance_amount: "1260.00", due_on: "2026-11-20", issue_date: "2026-10-30", paid_at: null, overdue: true };
    const html = renderToString(createElement(ReceivablesTable, { invoices: [inv], lang: "en", locale: "en-AU", timeZone: "Asia/Ho_Chi_Minh" }));
    expect(html).toContain('data-testid="receivables"');
    expect(html).toContain('data-testid="receivable-row"');
    expect(html).toContain("INV-000021");
    expect(html).toContain("1,260.00 USD");
    expect(html).toContain(MESSAGES.en["receivables.overdue"]);
    expect(html).not.toContain("minWidth:640"); // the old hand grid
  });
  it("keeps room under a list page for the toast on a phone", () => {
    const css = readFileSync(path.resolve(__dirname, "../app/globals.css"), "utf8");
    expect(css).toMatch(/@media \(max-width: 767px\) \{\s*\[data-crm-toast-room\] \{ padding-bottom: 96px; \}/);
    for (const file of ["app/(crm)/commissions/page.tsx", "app/(crm)/payments/page.tsx"]) {
      expect(readFileSync(path.resolve(__dirname, "..", file), "utf8"), file).toContain("data-crm-toast-room");
    }
  });
});

// ── 10: the support banner's zone ──────────────────────────────────────────────────────────────

describe("support banner", () => {
  it("prints the window's end in the agency's zone, not UTC", () => {
    const state = { kind: "support_grant" as const, until: "2026-11-02T03:00:00Z", showIdentity: false };
    const html = renderToString(createElement(SupportBanner, { state, canManage: false, lang: "en", locale: "en-AU", timeZone: "Asia/Ho_Chi_Minh" }));
    expect(html).toContain("10:00");
    expect(html).not.toContain("UTC");
  });
});
