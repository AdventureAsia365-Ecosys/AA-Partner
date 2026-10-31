// AA-966 (CRM-34): the Reports helpers of the web: the filters read from the URL, the export path and file name,
// which sections a role may download, tile values and hints (last year in % or points, the 3-month trend of a new
// agency), code labels with a readable fallback, bar widths, the success-metric wording, the BFF line, the nav entry
// (a real page now, no empty state) and both languages.
import { describe, expect, it } from "vitest";
import { matchBff } from "@/lib/bff";
import { MESSAGES, t } from "@/lib/i18n";
import { sectionByKey } from "@/lib/nav";
import {
  EXPORT_SECTIONS,
  barWidths,
  codeText,
  exportFilename,
  exportPath,
  exportableSections,
  pageHref,
  parseFilters,
  pctText,
  successTarget,
  successValue,
  tileHint,
  tileValue,
  type SuccessMetric,
  type Tile,
} from "@/lib/reports";

const MEMBER = "00000000-0000-4000-8000-0000000000a9";

function tile(over: Partial<Tile> = {}): Tile {
  return {
    key: "inquiries",
    unit: "count",
    value: 142,
    href: "/pipeline",
    basis: null,
    previous: 120,
    delta: 18.3,
    trend: [],
    ...over,
  };
}

describe("filters and links", () => {
  it("reads the period and owner from the URL, falling back to safe defaults", () => {
    expect(parseFilters({})).toEqual({ period: "12m", owner: null });
    expect(parseFilters({ period: "30d", owner: "me" })).toEqual({ period: "30d", owner: "me" });
    expect(parseFilters({ period: "7d", owner: "someone" })).toEqual({ period: "12m", owner: null });
    expect(parseFilters({ period: ["30d"], owner: MEMBER })).toEqual({ period: "12m", owner: MEMBER });
  });

  it("builds the page link and the export path through the BFF", () => {
    expect(pageHref({ period: "ytd", owner: null })).toBe("/reports?period=ytd");
    const path = exportPath("lead_sources", { period: "90d", owner: MEMBER });
    expect(path).toBe(`reports/export?period=90d&section=lead_sources&owner=${MEMBER}`);
    expect(matchBff("GET", path.split("?")[0])).not.toBeNull();
    expect(matchBff("GET", "reports/summary")).not.toBeNull();
    expect(matchBff("GET", "reports/tiles")).not.toBeNull();
    expect(matchBff("POST", "reports/summary")).toBeNull();
    expect(matchBff("GET", "reports/other")).toBeNull();
    expect(exportFilename("money_by_month", "12m", new Date("2031-05-20T10:00:00Z"))).toBe(
      "report-money-by-month-12m-20310520.csv",
    );
  });

  it("offers the money sections only to roles that see margins", () => {
    expect(exportableSections(true)).toEqual([...EXPORT_SECTIONS]);
    expect(exportableSections(false)).not.toContain("money_by_month");
    expect(exportableSections(false)).toContain("top_products");
  });
});

describe("tiles", () => {
  it("formats values by unit", () => {
    expect(tileValue(tile(), "USD", "en-AU")).toBe("142");
    expect(tileValue(tile({ unit: "pct", value: 31.25 }), "USD", "en-AU")).toBe("31.3%");
    expect(tileValue(tile({ unit: "money", value: "61000.50" }), "AUD", "en-AU")).toBe("61,000.50 AUD");
    expect(tileValue(tile({ unit: "money", value: "2540000" }), "VND", "vi-VN")).toBe("2.540.000 VND");
    expect(tileValue(tile({ value: null }), "USD", "en-AU")).toBe("—");
  });

  it("compares with last year in percent, rates in points, and shows the trend for a new agency", () => {
    expect(tileHint(tile(), "previous_year", "en", "en-AU", "USD")).toEqual({
      text: "+18.3% vs last year",
      tone: "success",
    });
    expect(tileHint(tile({ unit: "pct", delta: -4 }), "previous_year", "en", "en-AU", "USD")).toEqual({
      text: "-4 pts vs last year",
      tone: "danger",
    });
    expect(tileHint(tile({ delta: null }), "previous_year", "en", "en-AU", "USD").text).toBe("No figure for last year");
    const trend = tile({
      trend: [
        { from: "2031-03-01", to: "2031-03-31", value: 3 },
        { from: "2031-04-01", to: "2031-04-30", value: 5 },
        { from: "2031-05-01", to: "2031-05-20", value: 2 },
      ],
    });
    expect(tileHint(trend, "trend_3m", "en", "en-AU", "USD").text).toBe("Last 3 months: 3 → 5 → 2");
    expect(tileHint(tile(), "none", "vi", "vi-VN", "USD").text).toBe(t("vi", "reports.noHistory"));
  });
});

describe("labels and bars", () => {
  it("names known codes and spells unknown ones in words", () => {
    expect(codeText("en", "lost", "price_aa_tour", "reports.notSet")).toBe("Price (AA tour)");
    expect(codeText("en", "reports.source", "website_form", "reports.notSet")).toBe("website form");
    expect(codeText("vi", "lost", null, "reports.notSet")).toBe("Chưa đặt");
  });

  it("scales bars to the largest value", () => {
    expect(barWidths([5, 10, 0])).toEqual([50, 100, 0]);
    expect(barWidths([0, 0])).toEqual([0, 0]);
    expect(barWidths([])).toEqual([]);
    expect(pctText(null, "en-AU")).toBe("—");
  });

  it("words a success metric with its target, or says it is not built", () => {
    const m: SuccessMetric = {
      key: "first_reply_median",
      module: "1",
      unit: "minutes",
      value: 42,
      target: 120,
      comparator: "lte",
      status: "met",
    };
    expect(successValue(m, "en", "en-AU")).toBe("42 min");
    expect(successTarget(m, "en", "en-AU")).toBe("goal: at most 120 min");
    expect(successValue({ ...m, unit: "pct", value: 80 }, "en", "en-AU")).toBe("80%");
    expect(successTarget({ ...m, unit: "pct", comparator: "gte", target: 80 }, "en", "en-AU")).toBe("goal: at least 80%");
    expect(successValue({ ...m, value: null, status: "no_data" }, "en", "en-AU")).toBe("No data yet");
    expect(successValue({ ...m, status: "not_built" }, "en", "en-AU")).toBe("Coming later");
    expect(successValue({ ...m, unit: "count", value: 3 }, "en", "en-AU")).toBe("3");
    expect(successTarget({ ...m, target: null, comparator: null }, "en", "en-AU")).toBe("");
  });
});

describe("nav and languages", () => {
  it("is a real page for every role, without the empty state", () => {
    const reports = sectionByKey("reports");
    expect(reports?.path).toBe("/reports");
    expect(reports?.empty).toBeUndefined();
    expect(reports?.roles).toEqual(["owner", "agent", "assistant"]);
  });

  it("has every report string in English and Vietnamese, with the same placeholders", () => {
    const keys = Object.keys(MESSAGES.en).filter((k) => k.startsWith("reports.") || k.startsWith("home.tiles"));
    expect(keys.length).toBeGreaterThan(100);
    for (const k of keys) {
      const en = MESSAGES.en[k as keyof typeof MESSAGES.en];
      const vi = MESSAGES.vi[k as keyof typeof MESSAGES.vi];
      expect(vi, k).toBeTruthy();
      expect((vi.match(/\{\w+\}/g) ?? []).sort(), k).toEqual((en.match(/\{\w+\}/g) ?? []).sort());
    }
    for (const s of EXPORT_SECTIONS) expect(MESSAGES.en[`reports.section.${s}` as keyof typeof MESSAGES.en]).toBeTruthy();
  });
});
