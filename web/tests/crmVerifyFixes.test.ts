// @vitest-environment jsdom
// S278 live check fixes. AA-1069: the Bookings page's "Requested" filter asks the API for `status=requested` and lists
// the lines (it rendered "The CRM service is not answering" because the API refused the status); an unknown status is
// not sent. AA-1070: the departure roster's text columns (the trip title, the checks) wrap on a phone card instead of
// staying on one line, which pushed the page to 458 px at 390; number columns keep one line. The data calls are
// replaced; the page and the kit components are real.
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToString } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { LedgerView, Line } from "@/lib/ledger";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const asked: { path: string; query: string | null }[] = [];
const state: { view: LedgerView | null } = { view: null };

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {} }), redirect: () => {} }));
vi.mock("@/lib/crmContext", () => ({
  crmContext: async () => ({ apiDown: false, lang: "en", locale: "en-AU", role: "owner", cfg: {}, me: {} }),
}));
vi.mock("@/lib/serverContext", () => ({ serverContext: async () => ({ session: { at: "x", tid: "t" } }) }));
vi.mock("@/lib/crmApi", () => ({
  crmCall: async (_cfg: unknown, _s: unknown, _m: string, path: string, opts?: { query?: URLSearchParams }) => {
    asked.push({ path, query: opts?.query ? opts.query.toString() : null });
    return state.view ? { ok: true, status: 200, data: state.view } : { ok: false, status: 422, detail: "literal_error" };
  },
}));

const { default: BookingsPage } = await import("@/app/(crm)/bookings/page");
const { ReportTable } = await import("@/components/reports/ReportTable");

const TRIP = "00000000-0000-4000-8000-0000000007a1";

function line(over: Partial<Line> = {}): Line {
  return {
    booking_line_id: "00000000-0000-4000-8000-0000000007b1",
    trip_id: TRIP,
    supplier_kind: "agency",
    agency_supplier_id: "00000000-0000-4000-8000-0000000007c1",
    supplier_name: "Lantern Air",
    status: "requested",
    aa_sub_status: null,
    title: "Flight to the hills",
    confirmation_no: null,
    service_from: "2031-03-04",
    service_to: null,
    collect: "agency",
    confirm_class: null,
    visible_to_travelers: true,
    confirmed_at: null,
    requested_at: "2031-02-01T09:00:00Z",
    read_only: false,
    trip_title: "Misty Ridge 6 days",
    trip_stage: "booked",
    ...over,
  };
}

async function bookings(status?: string): Promise<string> {
  const element = await BookingsPage({ searchParams: Promise.resolve(status === undefined ? {} : { status }) });
  return element ? renderToString(element) : "";
}

describe("Bookings page status filter (AA-1069)", () => {
  beforeEach(() => {
    asked.length = 0;
    state.view = { lines: [line()], base_currency: "USD", truncated: false };
  });

  it("asks the API for the requested lines and lists them", async () => {
    const html = await bookings("requested");
    expect(asked).toEqual([{ path: "bookings", query: "status=requested" }]);
    expect(html).toContain("Misty Ridge 6 days");
    expect(html).toContain("Requested");
    expect(html).not.toContain("not answering");
    expect(html).toMatch(/href="\/bookings\?status=requested" aria-current="page"/);
  });

  it("offers every line status as a filter and never sends an unknown one", async () => {
    const html = await bookings("booked");
    expect(asked).toEqual([{ path: "bookings", query: null }]);
    for (const s of ["planning", "requested", "confirmed", "cancelled"]) expect(html).toContain(`/bookings?status=${s}`);
  });
});

// jsdom has no matchMedia: a phone width (card mode of the kit DataTable) for the roster test.
window.matchMedia = ((query: string) => ({
  matches: query.includes("max-width"),
  media: query,
  onchange: null,
  addEventListener: () => {},
  removeEventListener: () => {},
  addListener: () => {},
  removeListener: () => {},
  dispatchEvent: () => false,
})) as unknown as typeof window.matchMedia;

const roots: Root[] = [];
afterEach(() => {
  for (const r of roots.splice(0)) act(() => r.unmount());
  document.body.innerHTML = "";
});

async function mountTable(textColumns?: number[]): Promise<HTMLElement> {
  const container = document.createElement("div");
  document.body.appendChild(container);
  await act(async () => {
    const root = createRoot(container);
    roots.push(root);
    root.render(
      createElement(ReportTable, {
        tableId: "roster-travellers",
        head: ["Traveller", "Trip", "Pax"],
        rows: [{ key: "a", href: `/pipeline/${TRIP}`, cells: ["Ana Vale (lead)", "Misty Ridge: Lantern Bay, Cedar Falls and Stone Harbour — 6 Days", "3"] }],
        empty: "None",
        ...(textColumns ? { textColumns } : {}),
      }),
    );
  });
  return container;
}

function cellSpan(container: HTMLElement, text: string): HTMLElement {
  // The cell's own span (the innermost one; the card wraps it in its value span).
  const span = [...container.querySelectorAll("span")].filter((s) => s.textContent === text).at(-1);
  if (!span) throw new Error(`no cell "${text}"`);
  return span;
}

describe("ReportTable text columns on a phone card (AA-1070)", () => {
  it("wraps a text column and keeps a number column on one line", async () => {
    const container = await mountTable([1]);
    const trip = cellSpan(container, "Misty Ridge: Lantern Bay, Cedar Falls and Stone Harbour — 6 Days");
    expect(trip.style.whiteSpace).not.toBe("nowrap");
    expect(trip.style.overflowWrap).toBe("anywhere");
    expect(cellSpan(container, "3").style.whiteSpace).toBe("nowrap");
  });

  it("keeps the old number layout when no text column is named", async () => {
    const container = await mountTable();
    expect(cellSpan(container, "Misty Ridge: Lantern Bay, Cedar Falls and Stone Harbour — 6 Days").style.whiteSpace).toBe("nowrap");
  });
});
