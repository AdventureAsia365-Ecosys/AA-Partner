// AA-1093 part 1: an AA tour Adventure Asia no longer offers — the trip card's item lines, the pipeline filter in the
// URL and every word of the flow in EN and VI. Invented names only.
import { describe, expect, it } from "vitest";
import { MESSAGES, type Lang, type MessageKey } from "@/lib/i18n";
import { tourRetiredEn } from "@/lib/i18nTourRetired";
import { boardHref, parseFilter, tourRetiredLines, type TourRetiredView } from "@/lib/pipeline";

const LANGS: Lang[] = ["en", "vi"];

function tour(items: Partial<TourRetiredView["items"]>): TourRetiredView {
  return {
    source_id: "00000000-0000-4000-8000-000000001093",
    title: "Lotus lake 3 days",
    raised_at: "2031-05-01T03:00:00Z",
    items: { open_option: 0, held_line: 0, quoted_line: 0, confirmed_line: 0, ...items },
  };
}

describe("tour retired", () => {
  it("lists the kinds with items in a fixed order and leaves the empty ones out", () => {
    expect(tourRetiredLines(tour({ confirmed_line: 1, open_option: 2 }))).toEqual([
      { kind: "open_option", n: 2 },
      { kind: "confirmed_line", n: 1 },
    ]);
    expect(tourRetiredLines(tour({}))).toEqual([]);
  });

  it("keeps the pipeline filter in the URL and refuses unknown issues", () => {
    expect(boardHref({ issue: "aa_tour_retired" })).toBe("/pipeline?issue=aa_tour_retired");
    expect(boardHref({ owner: "me", issue: "aa_tour_retired" })).toBe("/pipeline?owner=me&issue=aa_tour_retired");
    expect(parseFilter({ issue: "aa_tour_retired" }).issue).toBe("aa_tour_retired");
    expect(parseFilter({ issue: "passport" }).issue).toBe("passport");
    expect(parseFilter({ issue: "aa_tour_gone" }).issue).toBeNull();
    expect(parseFilter({ issue: ["aa_tour_retired"] }).issue).toBeNull();
  });

  it("words every key in EN and VI, the task kind and each item kind included", () => {
    const keys = Object.keys(tourRetiredEn) as MessageKey[];
    expect(keys).toContain("task.aa_tour_discontinued");
    for (const kind of ["open_option", "held_line", "quoted_line", "confirmed_line"]) {
      expect(keys).toContain(`tourRetired.item.${kind}`);
    }
    for (const lang of LANGS) {
      for (const key of keys) {
        expect(MESSAGES[lang][key], `${lang} ${key}`).toBeTruthy();
      }
    }
    expect(MESSAGES.vi["tourRetired.badge"]).not.toBe(MESSAGES.en["tourRetired.badge"]);
    for (const lang of LANGS) for (const key of keys) expect(MESSAGES[lang][key]).not.toMatch(/AA-\d+/);
  });
});
