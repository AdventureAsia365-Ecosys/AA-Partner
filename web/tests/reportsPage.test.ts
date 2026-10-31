// @vitest-environment jsdom
// AA-966 (CRM-34): the Reports page renders from the API's answer: every section with its numbers for the owner, no
// money section and no money export for the assistant, the empty state when the period holds nothing, and the
// 3-month trend note of a new agency. The data calls are replaced; the page and the kit components are real.
import { renderToString } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ReportView } from "@/lib/reports";

const state: { role: "owner" | "agent" | "assistant"; view: ReportView | null } = { role: "owner", view: null };

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {} }), redirect: () => {} }));
vi.mock("@/lib/crmContext", () => ({
  crmContext: async () => ({ apiDown: false, lang: "en", locale: "en-AU", role: state.role, cfg: {}, me: {} }),
}));
vi.mock("@/lib/serverContext", () => ({ serverContext: async () => ({ session: { at: "x", tid: "t" } }) }));
vi.mock("@/lib/crmApi", () => ({
  crmCall: async (_cfg: unknown, _s: unknown, _m: string, path: string) => {
    if (path === "members") {
      return {
        ok: true,
        status: 200,
        data: {
          members: [
            { member_id: "00000000-0000-4000-8000-0000000000a1", role: "owner", status: "active", is_me: true },
            { member_id: "00000000-0000-4000-8000-0000000000a9", role: "agent", status: "active", is_me: false },
          ],
        },
      };
    }
    return state.view ? { ok: true, status: 200, data: state.view } : { ok: false, status: 503, detail: "down" };
  },
}));

const { default: ReportsPage } = await import("@/app/(crm)/reports/page");

function totals(sell: string, cost: string) {
  return {
    sell_base: sell,
    cost_base: cost,
    margin_base: (Number(sell) - Number(cost)).toFixed(2),
    margin_pct: 20,
    commission_expected_base: "0.00",
    lines: 2,
    lines_missing_amounts: 0,
  };
}

function view(over: Partial<ReportView> = {}): ReportView {
  return {
    period: {
      from: "2030-06-01",
      to: "2031-05-20",
      today: "2031-05-20",
      timezone: "Asia/Ho_Chi_Minh",
      base_currency: "AUD",
      compare: "previous_year",
      previous: { from: "2029-06-01", to: "2030-05-20" },
      owner_member_id: null,
    },
    tiles: [
      { key: "inquiries", unit: "count", value: 142, href: "/pipeline", basis: null, previous: 120, delta: 18.3, trend: [] },
      { key: "win_rate", unit: "pct", value: 31, href: "/pipeline", basis: null, previous: 27, delta: 4, trend: [] },
      { key: "revenue", unit: "money", value: "61000.00", href: "/bookings", basis: null, previous: "56000.00", delta: 8.9, trend: [] },
    ],
    money_visible: true,
    funnel: {
      created: 142,
      won: 44,
      win_rate_pct: 31,
      stages: [
        { stage: "inquiry", count: 142 },
        { stage: "qualifying", count: 118 },
        { stage: "proposal", count: 77 },
        { stage: "booked", count: 44 },
        { stage: "pre_departure", count: 40 },
        { stage: "travelling", count: 39 },
        { stage: "completed", count: 39 },
      ],
      lost: 61,
      cancelled: 3,
    },
    outcomes: { lost: [{ reason: "price", count: 21 }], cancelled: [{ reason: "client_request", count: 3 }] },
    lead_sources: [{ source: "website_form", trips: 61, won: 20, win_rate_pct: 32.8 }],
    cycle: { trips: 44, median_days: 12.5 },
    open_trips: [{ stage: "proposal", count: 9, stalled: 2, stalled_not_waiting: 1, median_age_days: 6 }],
    picks: { picks: 30, kept_default: 18, upgraded: 7, kept_default_pct: 60, upgrade_rate_pct: 23.3, most_changed_type: "lodging" },
    money: {
      currency: "AUD",
      ...totals("61000.00", "48800.00"),
      aa_share_pct: 64,
      by_month: [{ month: "2031-05-01", ...totals("5000.00", "4000.00") }],
      by_supplier: [],
      by_party_type: [{ party_type: "family", ...totals("3000.00", "2500.00") }],
      top_products: [{ supplier_kind: "aa", title: "Mekong 3 days", trips: 4, ...totals("4000.00", "3200.00") }],
    },
    top_products: [{ supplier_kind: "aa", title: "Mekong 3 days", lines: 2, trips: 4, sell_base: "4000.00" }],
    payments: {
      open_invoices: [{ currency: "AUD", count: 2, total: "1000.00", paid: "250.00", outstanding: "750.00", overdue_count: 1, overdue: "500.00" }],
      schedule_due: [],
      collected: [{ currency: "AUD", count: 3, received: "900.00", refunded: "100.00", kept: "800.00" }],
    },
    team: {
      members: [
        {
          member_id: "00000000-0000-4000-8000-0000000000a9",
          role: "agent",
          status: "active",
          is_me: false,
          open_trips: 5,
          new_trips: 8,
          won: 2,
          overdue_tasks: 1,
          tasks_done: 3,
          messages_sent: 12,
          first_reply_median_min: 60,
        },
      ],
      unassigned_open_trips: 2,
      unanswered_24h: 1,
    },
    success: [
      { key: "upgrade_rate", module: "3", unit: "pct", value: 23.3, target: 20, comparator: "gte", status: "met" },
      { key: "commission_unclaimed", module: "13", unit: "count", value: null, target: 0, comparator: "lte", status: "not_built" },
    ],
    insights_included: true,
    ...over,
  };
}

async function render(): Promise<string> {
  const element = await ReportsPage({ searchParams: Promise.resolve({ period: "12m" }) });
  return element ? renderToString(element) : "";
}

describe("Reports page", () => {
  beforeEach(() => {
    state.role = "owner";
    state.view = view();
  });

  it("renders every section with the API's numbers for the owner", async () => {
    const html = await render();
    for (const id of ["reports-tiles", "reports-funnel", "reports-open", "reports-outcomes", "reports-sources",
      "reports-picks", "reports-money", "reports-products", "reports-payments", "reports-team", "reports-success",
      "reports-insights", "reports-export"]) {
      expect(html, id).toContain(`data-testid="${id}"`);
    }
    expect(html).toContain("61,000.00 AUD");
    expect(html).toContain("+18.3% vs last year");
    expect(html).toContain("Price");
    expect(html).toContain("Revenue and margin by month"); // the money export is offered
    expect(html).toContain("Coming later");
    expect(html).toContain("800.00 AUD");
    expect(html).not.toContain("AA-9");
  });

  it("shows the assistant no money section and no money export", async () => {
    state.role = "assistant";
    state.view = view({ money: null, money_visible: false, tiles: view().tiles.filter((x) => x.unit !== "money") });
    const html = await render();
    expect(html).not.toContain('data-testid="reports-money"');
    expect(html).not.toContain("Revenue and margin by month");
    expect(html).toContain("Mekong 3 days");
  });

  it("says when the period is empty and explains the trend of a new agency", async () => {
    state.view = view({
      period: { ...view().period, compare: "trend_3m", previous: null },
      funnel: { ...view().funnel, created: 0 },
      open_trips: [],
      money: null,
    });
    const html = await render();
    expect(html).toContain("No trips in this period yet");
    expect(html).toContain("less than a year of history");
    expect(html).not.toContain('data-testid="reports-funnel"');
  });

  it("shows the API error state when the read fails", async () => {
    state.view = null;
    expect(await render()).toContain("not answering");
  });
});
