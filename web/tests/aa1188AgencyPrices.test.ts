// @vitest-environment jsdom
// AA-1188: agencies see the right price. The AA prices page: twin share + single supplement, child and infant columns,
// the "On request" badge of a band AA quotes on request (D5), the departure note, "Priced tours N of M", the notes
// under the table and "Booking conditions with AA" (GET /crm/v1/aa/terms). The sidebar lists AA prices under Money
// (D10). The studio's "In my catalog" reads the price.from.v1 "from" price, "Price on request" without one. Invented
// tours, places and clauses only.
import { createElement } from "react";
import { renderToStaticMarkup, renderToString } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { coverageText, isOnRequest, seasonText, tariffNotes, type TariffView } from "@/lib/aaTariff";
import { cancellationLines, paymentLines, pctText, quoteRuleLines, type AaTerms } from "@/lib/aaTerms";
import { MESSAGES } from "@/lib/i18n";
import { SECTIONS, visibleSections } from "@/lib/nav";
import { tariffItem, tariffView } from "./aaTariffFixture";

const TERMS: AaTerms = {
  as_of: "2031-01-05",
  payment: { deposit_pct: "30.00", balance_days_before_departure: 45, settlement_due_days: 14, scope: "default" },
  cancellation_tiers: [
    { min_days: 0, fee_pct: "100.00" },
    { min_days: 60, fee_pct: "10.00" },
    { min_days: 30, fee_pct: "50.00" },
  ],
  clauses: [
    { code: "price_error", title: "Invented price correction", body: "AA corrects an invented price.", free_cancel_days: 3, version: 1 },
  ],
  quote_rules: { quote_validity_days: 14, group_on_request_min_pax: 9, age_bands: { child_age_max: 11, infant_age_max: 1 } },
  holds_paused: false,
};

const state: {
  answer: { ok: true; status: number; data: TariffView } | { ok: false; status: number; detail: string };
  terms: { ok: true; status: number; data: AaTerms } | { ok: false; status: number; detail: string };
} = {
  answer: { ok: true, status: 200, data: tariffView() },
  terms: { ok: true, status: 200, data: TERMS },
};

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {}, push: () => {} }), usePathname: () => "/aa-tariff", redirect: () => {}, notFound: () => {} }));
vi.mock("@/lib/crmContext", () => ({
  crmContext: async () => ({
    apiDown: false,
    lang: "en",
    locale: "en-AU",
    role: "owner",
    cfg: {},
    me: { member_role: "owner", permissions: { aa_tariff_view: true }, display_name: "Invented Lotus Travel", tenant_id: "t" },
  }),
}));
vi.mock("@/lib/serverContext", () => ({ serverContext: async () => ({ session: { at: "x", tid: "t" } }) }));
vi.mock("@/lib/crmApi", () => ({
  crmCall: async (_cfg: unknown, _s: unknown, _m: string, path: string) => {
    if (path === "aa/terms") return state.terms;
    if (path === "aa/tariff/currencies") return { ok: true, status: 200, data: { currencies: ["USD"], rate_date: "2031-01-05", checked: true } };
    return state.answer;
  },
}));

const { default: AaTariffPage } = await import("@/app/(crm)/aa-tariff/page");
const { MyCatalog, pickedFromPrice } = await import("@/app/(studio)/portal/_components/marketplace/MyCatalog");

async function page(): Promise<string> {
  return renderToString(await AaTariffPage({ searchParams: Promise.resolve({}) }));
}

const text = (html: string) => html.split(/<[^>]*>/).join("").replace(/&#x27;/g, "'").replace(/&amp;/g, "&").replace(/\s+/g, " ");

function money(amount: string) {
  return { amount, currency: "USD" };
}

function view112(): TariffView {
  return tariffView({
    group_on_request_min_pax: 9,
    early_booking_days: 60,
    tours_priced: 2,
    catalog_tours: 844,
    items: [
      tariffItem({
        b2b_single_supplement_per_person: money("95.00"),
        b2b_child_price_per_person: money("271.60"),
        b2b_infant_price: money("0.00"),
        availability: "available",
        departure_adjusted: true,
        season: "high",
      }),
      tariffItem({
        pax_min: 11,
        pax_max: null,
        priced_for_pax: 11,
        b2b_price_per_person: money("301.00"),
        b2b_single_supplement_per_person: money("95.00"),
        availability: "on_request",
      }),
    ],
  });
}

beforeEach(() => {
  state.answer = { ok: true, status: 200, data: view112() };
  state.terms = { ok: true, status: 200, data: TERMS };
});

describe("AA prices: helpers", () => {
  it("counts priced tours only when AA counted them", () => {
    expect(coverageText({ tours_priced: 2, catalog_tours: 844 }, "en")).toBe("Priced tours: 2 of 844");
    expect(coverageText({ tours_priced: 2, catalog_tours: null }, "en")).toBeNull();
    expect(coverageText({}, "en")).toBeNull();
    expect(coverageText({ tours_priced: 0, catalog_tours: 0 }, "en")).toBeNull();
  });

  it("words the notes from what AA answered", () => {
    expect(tariffNotes({ group_on_request_min_pax: 9, early_booking_days: 60 }, "en")).toEqual([
      MESSAGES.en["tariff.note.twin"],
      "Groups of 9 or more are priced on request: ask AA for a quote.",
      "Booking at least 60 days before departure can lower the price.",
      MESSAGES.en["tariff.note.departure"],
    ]);
    expect(tariffNotes({}, "en")).toEqual([MESSAGES.en["tariff.note.twin"], MESSAGES.en["tariff.note.departure"]]);
  });

  it("reads On request and the season", () => {
    expect(isOnRequest({ availability: "on_request" })).toBe(true);
    expect(isOnRequest({ availability: "available" })).toBe(false);
    expect(isOnRequest({})).toBe(false); // an older AA answer has no availability
    expect(seasonText("high", "en")).toBe("High season");
    expect(seasonText("festive", "vi")).toBe("Mùa lễ hội");
    expect(seasonText("Invented dry months", "en")).toBe("Invented dry months");
    expect(seasonText(null, "en")).toBe("—");
  });
});

describe("AA prices page", () => {
  it("shows twin share, single, child and infant columns, On request for a group band and the coverage line", async () => {
    const html = await page();
    const body = text(html);
    for (const header of ["Per person, twin share", "Single supplement", "Child", "Infant", "Suggested retail (RRP) per person"]) {
      expect(body).toContain(header);
    }
    expect(body).toContain("388.00 USD");
    expect(body).toContain("95.00 USD");
    expect(body).toContain("271.60 USD");
    expect(html.match(/data-testid="tariff-on-request"/g)).toHaveLength(1);
    expect(body).toContain("Guide 301.00 USD"); // the computed amount stays as a guide
    expect(html.match(/data-testid="tariff-price"/g)).toHaveLength(1);
    expect(html).toContain('data-testid="tariff-departure-adjusted"');
    expect(body).toContain("High season");
    expect(html).toContain('data-testid="tariff-coverage"');
    expect(body).toContain("Priced tours: 2 of 844");
    expect(body).toContain("Groups of 9 or more are priced on request");
  });

  it("shows Booking conditions with AA under the prices", async () => {
    const html = await page();
    const body = text(html);
    expect(html).toContain('data-testid="aa-terms"');
    expect(body).toContain("Booking conditions with AA");
    expect(body).toContain("Deposit: 30% of the AA total when the booking is confirmed");
    expect(body).toContain("60+ days before departure: 10% of the booking total");
    expect(body).toContain("Less than 30 days before departure: 100% of the booking total");
    expect(body).toContain("A quote holds its price for 14 days");
    expect(body).toContain("Children: up to 11 years · Infants: up to 1 years");
    expect(body).toContain("Invented price correction");
    expect(body).toContain("Free cancellation within 3 days of AA's notice");
    expect(html).not.toContain("aa-terms-holds-paused");
  });

  it("keeps the prices when the terms cannot be read, and the terms when the prices cannot", async () => {
    state.terms = { ok: false, status: 503, detail: "booking_unavailable" };
    let html = await page();
    expect(html).toContain('data-testid="aa-terms-error"');
    expect(html).toContain('data-testid="tariff-row"');
    state.terms = { ok: true, status: 200, data: { ...TERMS, holds_paused: true } };
    state.answer = { ok: false, status: 503, detail: "booking_unavailable" };
    html = await page();
    expect(html).toContain('data-testid="aa-terms-holds-paused"');
    expect(html).toContain('data-testid="tariff-error"');
  });

  it("reads an older AA answer (no 1.12 fields) as before", async () => {
    state.answer = { ok: true, status: 200, data: tariffView() };
    const html = await page();
    expect(html).not.toContain("tariff-coverage");
    expect(html).not.toContain("tariff-on-request");
    expect(html.match(/data-testid="tariff-price"/g)).toHaveLength(3);
  });
});

describe("booking conditions helpers", () => {
  it("words payment, fee tiers and quote rules, and says when AA has none", () => {
    expect(pctText("12.50")).toBe("12.5");
    expect(paymentLines({ payment: null }, "en")).toEqual([MESSAGES.en["aaTerms.noPayment"]]);
    expect(cancellationLines({ cancellation_tiers: null }, "en")).toEqual([MESSAGES.en["aaTerms.noCancellation"]]);
    expect(cancellationLines({ cancellation_tiers: [{ min_days: 0, fee_pct: "100.00" }] }, "en")).toEqual([
      "0+ days before departure: 100% of the booking total",
    ]);
    expect(quoteRuleLines({ quote_rules: null }, "en")).toEqual([]);
    expect(quoteRuleLines(TERMS, "vi")[1]).toBe("Đoàn từ 9 khách trở lên được báo giá theo yêu cầu");
  });

  it("has every new string in English and Vietnamese, without issue ids", () => {
    const keys = Object.keys(MESSAGES.en).filter((k) => k.startsWith("aaTerms.") || k.startsWith("tariff.") || k === "nav.aaTariff");
    for (const k of keys) {
      const vi = MESSAGES.vi[k as keyof typeof MESSAGES.vi];
      expect(vi, k).toBeTruthy();
      expect(`${MESSAGES.en[k as keyof typeof MESSAGES.en]} ${vi}`).not.toMatch(/\bAA-\d+/);
    }
  });
});

describe("sidebar (D10)", () => {
  it("lists AA prices under Money for the owner and the agent, never the assistant", () => {
    const entry = SECTIONS.find((s) => s.key === "aa-tariff");
    expect(entry?.group).toBe("money");
    expect(entry?.path).toBe("/aa-tariff");
    expect(visibleSections("owner").some((s) => s.key === "aa-tariff")).toBe(true);
    expect(visibleSections("agent").some((s) => s.key === "aa-tariff")).toBe(true);
    expect(visibleSections("assistant").some((s) => s.key === "aa-tariff")).toBe(false);
    expect(MESSAGES.en["nav.aaTariff"]).toBe("AA prices");
  });
});

describe("studio: In my catalog", () => {
  it("takes the price.from.v1 row, else the USD amount of an older CIS answer, else nothing", () => {
    const row = { amount: "425.00", currency: "USD", basis: "per_person", pax: 2, season: null, valid_to: null };
    expect(pickedFromPrice({ from_price: row, price_usd: 425, price_available: true })).toBe(row);
    expect(pickedFromPrice({ from_price: null, price_usd: 425, price_available: true })).toBeNull();
    expect(pickedFromPrice({ price_usd: 1225, price_available: true })?.amount).toBe("1225");
    expect(pickedFromPrice({ price_usd: null, price_available: false })).toBeNull();
  });

  it("is a client component export (rendered by the Marketplace page)", () => {
    expect(typeof MyCatalog).toBe("function");
    expect(renderToStaticMarkup(createElement("span", null, "ok"))).toBe("<span>ok</span>");
  });
});
