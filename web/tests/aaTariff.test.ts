// @vitest-environment jsdom
// AA-1030: the agency's AA prices in the CRM web. The helpers (who sees the page, currency, pax band, discount), the
// page from the API's answer (layout fixture tests/aaTariffFixture.ts: table, indicative note, toolbar, wrapping
// cells, the not-linked and AA-down states, the assistant's forbidden state without an API call), the role-hidden
// links (Settings and the builder drawer), and the CSV download through the BFF with AA's own file name.
import { NextRequest } from "next/server";
import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { attachmentName, matchBff } from "@/lib/bff";
import {
  AA_TARIFF_PATH,
  discountText,
  paxBand,
  seesAaTariff,
  tariffCurrency,
  tariffErrorKey,
  tariffFilename,
  tariffPath,
  type TariffView,
} from "@/lib/aaTariff";
import { MESSAGES } from "@/lib/i18n";
import { refreshCookie, sessionCookie } from "@/lib/session";
import { LONG_TITLE, tariffView } from "./aaTariffFixture";
import { ENV, fakeAccessToken, testConfig } from "./helpers";

type Role = "owner" | "agent" | "assistant";
const state: {
  role: Role;
  permissions: { aa_tariff_view?: boolean } | undefined;
  answer: { ok: true; status: number; data: TariffView } | { ok: false; status: number; detail: string };
  calls: Array<{ path: string; query?: string }>;
} = { role: "owner", permissions: { aa_tariff_view: true }, answer: { ok: true, status: 200, data: tariffView() }, calls: [] };

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {}, push: () => {} }), redirect: () => {}, notFound: () => {} }));
vi.mock("@/lib/crmContext", () => ({
  crmContext: async () => ({
    apiDown: false,
    lang: "en",
    locale: "en-AU",
    role: state.role,
    cfg: {},
    me: { member_role: state.role, permissions: state.permissions, display_name: "Invented Lotus Travel", tenant_id: "t" },
  }),
}));
vi.mock("@/lib/serverContext", () => ({ serverContext: async () => ({ session: { at: "x", tid: "t" } }) }));
vi.mock("@/lib/crmApi", () => ({
  crmCall: async (_cfg: unknown, _s: unknown, _m: string, path: string, opts?: { query?: URLSearchParams }) => {
    state.calls.push({ path, query: opts?.query?.toString() });
    return state.answer;
  },
}));

const { default: AaTariffPage } = await import("@/app/(crm)/aa-tariff/page");
const { default: SettingsPage } = await import("@/app/(crm)/settings/page");
const { BuilderAaDrawer } = await import("@/components/proposals/BuilderAaDrawer");
const { TARIFF_TABLE_MIN_WIDTH } = await import("@/components/aaTariff/AaTariffView");

async function page(currency?: string): Promise<string> {
  return renderToString(await AaTariffPage({ searchParams: Promise.resolve(currency ? { currency } : {}) }));
}

beforeEach(() => {
  state.role = "owner";
  state.permissions = { aa_tariff_view: true };
  state.answer = { ok: true, status: 200, data: tariffView() };
  state.calls = [];
});
afterEach(() => vi.unstubAllGlobals());

describe("helpers", () => {
  it("follows /me permissions, else the roles that see margins", () => {
    expect(seesAaTariff({ member_role: "owner", permissions: { aa_tariff_view: true } })).toBe(true);
    expect(seesAaTariff({ member_role: "owner", permissions: { aa_tariff_view: false } })).toBe(false); // owner-only flip
    expect(seesAaTariff({ member_role: "assistant", permissions: { aa_tariff_view: false } })).toBe(false);
    expect(seesAaTariff({ member_role: "agent" })).toBe(true);
    expect(seesAaTariff({ member_role: "assistant" })).toBe(false);
    expect(seesAaTariff({ member_role: "assistant", permissions: {} })).toBe(false);
  });

  it("reads the currency, builds the BFF path and the file name", () => {
    expect(tariffCurrency("eur")).toBe("EUR");
    expect(tariffCurrency(["EUR"])).toBe("USD");
    expect(tariffCurrency("XYZ")).toBe("USD");
    expect(tariffCurrency(undefined)).toBe("USD");
    expect(tariffPath("EUR")).toBe("aa/tariff?currency=EUR");
    expect(tariffPath("VND", "csv")).toBe("aa/tariff?currency=VND&format=csv");
    expect(matchBff("GET", "aa/tariff")?.download).toBe(true);
    expect(matchBff("POST", "aa/tariff")).toBeNull();
    expect(tariffFilename("GBP", new Date("2031-02-03T10:00:00Z"))).toBe("aa-tariff-gbp-2031-02-03.csv");
  });

  it("words the pax band and the discount", () => {
    expect(paxBand({ pax_min: 2, pax_max: 4 }, "en")).toBe("2–4 people");
    expect(paxBand({ pax_min: 11, pax_max: null }, "en")).toBe("11+ people");
    expect(paxBand({ pax_min: 1, pax_max: 1 }, "en")).toBe("1 person");
    expect(paxBand({ pax_min: 3, pax_max: 3 }, "vi")).toBe("3 khách");
    expect(discountText("0.0300")).toBe("3%");
    expect(discountText("0.0625")).toBe("6.25%");
    expect(discountText("0")).toBe("—");
    expect(discountText("x")).toBe("—");
    expect(tariffErrorKey("aa_partner_not_active")).toBe("tariff.err.notLinked");
    expect(tariffErrorKey("booking_unavailable")).toBe("tariff.err.unavailable");
    expect(tariffErrorKey("aa_tariff_invalid")).toBe("tariff.err.generic");
  });

  it("has every page string in English and Vietnamese, without issue ids", () => {
    const keys = Object.keys(MESSAGES.en).filter((k) => k.startsWith("tariff."));
    expect(keys.length).toBeGreaterThan(25);
    for (const k of keys) {
      const vi = MESSAGES.vi[k as keyof typeof MESSAGES.vi];
      expect(vi, k).toBeTruthy();
      expect(`${MESSAGES.en[k as keyof typeof MESSAGES.en]} ${vi}`).not.toMatch(/\bAA-\d+/);
    }
    expect(MESSAGES.en["tariff.indicative"]).toBe("Indicative: a quote is the binding price.");
  });
});

describe("the AA prices page (layout fixture)", () => {
  it("shows every row with its price, band, discount and RRP, the indicative note and the toolbar", async () => {
    const html = await page("EUR");
    expect(state.calls).toEqual([{ path: "aa/tariff", query: "currency=EUR" }]);
    expect(html).toContain('data-testid="crm-aa-tariff"');
    expect(html).toContain("Indicative: a quote is the binding price.");
    expect(html.match(/data-testid="tariff-row"/g)).toHaveLength(3);
    expect(html).toContain("388.00 USD"); // the fixture's default view is priced in USD
    expect(html).toContain("2–4 people");
    expect(html).toContain("5+ people");
    expect(html).toContain("1 person");
    expect(html).toContain("6.25%");
    expect(html).toContain("500.00 USD");
    expect(html).toContain("Price level: Silver");
    for (const id of ["tariff-currency", "tariff-download", "tariff-print"]) expect(html).toContain(`data-testid="${id}"`);
    expect(html).toContain('data-print="hide"');
    expect(html).toContain('data-print="sheet"');
  });

  it("lets long cells wrap inside a grid that never stretches, and prints every row on one page", async () => {
    const html = await page();
    expect(html).toContain("grid-template-columns:minmax(0, 1fr)");
    expect(html).toContain(LONG_TITLE);
    expect(html).toContain("overflow-wrap:anywhere");
    // A narrow desktop scrolls the table inside its own frame (data-kit-scroll), never the page.
    expect(html).toContain(`min-width:${TARIFF_TABLE_MIN_WIDTH}px`);
    expect(html).toMatch(/data-kit-scroll=""[^>]*overflow:auto/);
    expect(html).not.toMatch(/aria-label="[^"]*(Next|next) page/); // paginate={false}: Print prints every row
  });

  it("prices VND without decimals and says when the list is cut short", async () => {
    state.answer = { ok: true, status: 200, data: tariffView({ truncated: true }, "VND") };
    state.answer.data.items[0].b2b_price_per_person = { amount: "9400000.00", currency: "VND" };
    const html = await page("VND");
    expect(html).toContain('data-testid="tariff-truncated"');
    expect(html).toMatch(/9,400,000 VND/);
  });

  it("says the agency is not linked (409) or AA cannot be reached (503)", async () => {
    state.answer = { ok: false, status: 409, detail: "aa_partner_not_active" };
    expect(await page()).toContain("not linked to AA");
    state.answer = { ok: false, status: 503, detail: "booking_unavailable" };
    expect(await page()).toContain("AA cannot be reached right now");
  });

  it("shows the assistant the forbidden state and never asks the API", async () => {
    state.role = "assistant";
    state.permissions = { aa_tariff_view: false };
    const html = await page();
    expect(html).toContain('data-testid="crm-section-forbidden"');
    expect(html).not.toContain("tariff-row");
    expect(state.calls).toEqual([]);
  });
});

describe("role-hidden links", () => {
  it("Settings links AA prices for the owner and the agent, never for the assistant", async () => {
    for (const [role, visible] of [["owner", true], ["agent", true], ["assistant", false]] as const) {
      state.role = role;
      state.permissions = { aa_tariff_view: visible };
      const html = renderToString(await SettingsPage());
      expect(html.includes('data-testid="settings-aa-tariff-link"'), role).toBe(visible);
      if (visible) expect(html).toContain(`href="${AA_TARIFF_PATH}"`);
    }
  });

  it("the builder drawer links AA prices only when the member may see them", () => {
    const view = { currency: "USD", days: [], status: "draft" } as unknown as Parameters<typeof BuilderAaDrawer>[0]["view"];
    const props = { view, blockId: "b1", canMoney: true, suppliers: [], lang: "en" as const, locale: "en-AU", write: async () => true, onClose: () => {} };
    const drawer = (over: Record<string, unknown>) => renderToString(createElement(BuilderAaDrawer, { ...props, ...over }));
    expect(drawer({ canTariff: true })).toContain('data-testid="builder-aa-tariff-link"');
    expect(drawer({})).not.toContain("builder-aa-tariff-link");
    expect(drawer({ canMoney: false, canTariff: false })).not.toContain(AA_TARIFF_PATH);
  });
});

describe("CSV download through the BFF", () => {
  const cfg = () => testConfig();
  function signedIn(): string {
    const c = cfg();
    const now = Math.floor(Date.now() / 1000);
    const s = sessionCookie(c, { at: fakeAccessToken(), atExp: now + 1200, iss: "i", sub: "sub-1", cid: "c", tid: "tenant-a" });
    const r = refreshCookie(c, "rt-1", "sub-1");
    return `${s.name}=${s.value}; ${r.name}=${r.value}`;
  }
  function request(path: string): NextRequest {
    return new NextRequest(`https://partner.example.test${path}`, { headers: { host: "partner.example.test", cookie: signedIn() } });
  }

  it("passes AA's plain file name on for the tariff route only, rebuilt, never a header as it came", async () => {
    const { GET } = await import("@/app/bff/[...path]/route");
    const csv = (disposition: string) =>
      new Response("tour_key,title\r\n", { headers: { "Content-Type": "text/csv; charset=utf-8", "Content-Disposition": disposition } });
    const upstream = vi.fn(async () => csv('attachment; filename="aa-tariff-lotus-usd-2031-01-05.csv"'));
    vi.stubGlobal("fetch", upstream);
    const res = await GET(request("/bff/aa/tariff?currency=USD&format=csv"), { params: Promise.resolve({ path: ["aa", "tariff"] }) });
    expect(res.status).toBe(200);
    expect(res.headers.get("content-disposition")).toBe('attachment; filename="aa-tariff-lotus-usd-2031-01-05.csv"');
    expect(res.headers.get("cache-control")).toBe("no-store");
    const [url] = upstream.mock.calls[0] as unknown as [string];
    expect(url).toBe(`${ENV.CRM_API_ORIGIN}/crm/v1/aa/tariff?currency=USD&format=csv`);

    vi.stubGlobal("fetch", vi.fn(async () => csv('attachment; filename="../x.csv"\r\nX-Evil: 1')));
    const odd = await GET(request("/bff/aa/tariff?format=csv"), { params: Promise.resolve({ path: ["aa", "tariff"] }) });
    expect(odd.headers.get("content-disposition")).toBeNull();

    vi.stubGlobal("fetch", vi.fn(async () => csv('attachment; filename="report.csv"')));
    const other = await GET(request("/bff/reports/export?section=funnel"), { params: Promise.resolve({ path: ["reports", "export"] }) });
    expect(other.headers.get("content-disposition")).toBeNull(); // not a download route: unchanged behaviour
  });

  it("attachmentName keeps only a plain CSV file name", () => {
    expect(attachmentName('attachment; filename="aa-tariff-x-usd-2031-01-05.csv"')).toBe("aa-tariff-x-usd-2031-01-05.csv");
    expect(attachmentName('attachment; filename="a b.csv"')).toBeNull();
    expect(attachmentName('inline; filename="x.csv"')).toBeNull();
    expect(attachmentName('attachment; filename="x.exe"')).toBeNull();
    expect(attachmentName(null)).toBeNull();
  });

  it("bffDownload saves under the BFF's file name, else the page's", async () => {
    const { bffDownload } = await import("@/lib/bffClient");
    const saved: string[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      saved.push(this.download);
    });
    vi.spyOn(URL, "createObjectURL").mockImplementation(() => "blob:x");
    const answers = [
      new Response("a\r\n", { headers: { "Content-Type": "text/csv", "Content-Disposition": 'attachment; filename="aa-tariff-lotus-eur-2031-01-05.csv"' } }),
      new Response("a\r\n", { headers: { "Content-Type": "text/csv" } }),
    ];
    vi.stubGlobal("fetch", vi.fn(async () => answers.shift()));
    expect((await bffDownload(tariffPath("EUR", "csv"), { filename: "fallback.csv" })).ok).toBe(true);
    expect((await bffDownload(tariffPath("EUR", "csv"), { filename: "fallback.csv" })).ok).toBe(true);
    expect(saved).toEqual(["aa-tariff-lotus-eur-2031-01-05.csv", "fallback.csv"]);
  });
});
