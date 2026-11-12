// AA-1070 (crm-13): the "AA cancelled a departure" helpers (the departures offered, the builder link, the error keys,
// the agency's today), the pipeline filter in the URL, the BFF route of the re-quote, and every word of the flow in EN
// and VI. Invented names only.
import { describe, expect, it } from "vitest";
import { MESSAGES, type Lang, type MessageKey } from "@/lib/i18n";
import { aaCancelEn } from "@/lib/i18nAaCancel";
import { boardHref, parseFilter } from "@/lib/pipeline";
import { BFF_ROUTES } from "@/lib/bff";
import { agencyToday, requoteChoices, requoteErrorKey, requoteHref } from "@/lib/aaCancel";
import type { AaDeparture } from "@/lib/aa";

const A = "00000000-0000-4000-8000-00000000c013";
const B = "00000000-0000-4000-8000-00000000c015";
const LANGS: Lang[] = ["en", "vi"];

function dep(id: string, start: string, status = "scheduled"): AaDeparture {
  return { departure_id: id, start_date: start, available: 6, status, auto_confirm: true };
}

describe("aaCancel helpers", () => {
  it("offers the open departures from today, never the cancelled one, soonest first", () => {
    const all = [dep("d-late", "2031-06-20"), dep("d-gone", "2031-05-01", "cancelled"), dep("d-old", "2031-03-01"), dep("d-x", "2031-05-10"), dep("d-early", "2031-05-10")];
    expect(requoteChoices(all, "d-x", "2031-04-01").map((d) => d.departure_id)).toEqual(["d-early", "d-late"]);
    expect(requoteChoices([], "d-x", "2031-04-01")).toEqual([]);
  });
  it("links the version in the builder of its trip", () => {
    expect(requoteHref({ proposal_id: B, trip_id: A })).toBe(`/pipeline/${A}/proposal/${B}`);
  });
  it("words the API refusals", () => {
    expect(requoteErrorKey("same_departure")).toBe("aaCancel.err.same_departure");
    expect(requoteErrorKey({ code: "builder_draft_exists" })).toBe("aaCancel.err.builder_draft_exists");
    expect(requoteErrorKey("network")).toBe("aaCancel.err.generic");
    expect(requoteErrorKey(null)).toBe("aaCancel.err.generic");
  });
  it("reads today in the agency's zone", () => {
    const now = new Date("2031-04-01T20:00:00Z");
    expect(agencyToday("Asia/Ho_Chi_Minh", now)).toBe("2031-04-02");
    expect(agencyToday("UTC", now)).toBe("2031-04-01");
    expect(agencyToday("Not/A_Zone", now)).toBe("2031-04-01");
  });
  it("keeps the filter in the URL", () => {
    expect(boardHref({ issue: "aa_cancelled" })).toBe("/pipeline?issue=aa_cancelled");
    expect(parseFilter({ issue: "aa_cancelled" }).issue).toBe("aa_cancelled");
    expect(parseFilter({ issue: "anything" }).issue).toBeNull();
  });
  it("lets the re-quote through the BFF", () => {
    const allowed = (method: string, path: string) => BFF_ROUTES.some((r) => r.method === method && r.pattern.test(path));
    expect(allowed("POST", `trips/${A}/aa-cancel/${B}/requote`)).toBe(true);
    expect(allowed("GET", `trips/${A}/aa-cancel/${B}/requote`)).toBe(false);
    expect(allowed("POST", `trips/${A}/aa-cancel/x/requote`)).toBe(false);
  });
});

describe("aaCancel words", () => {
  it.each(LANGS)("%s", (lang) => {
    const missing = Object.keys(aaCancelEn).filter((k) => !MESSAGES[lang][k as MessageKey]?.trim());
    expect(missing).toEqual([]);
  });
  it("never shows an issue id", () => {
    for (const lang of LANGS) for (const k of Object.keys(aaCancelEn)) expect(MESSAGES[lang][k as MessageKey]).not.toMatch(/AA-\d+/);
  });
});
