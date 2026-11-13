// AA-1096 L4: the Data and privacy page's windows use the range AA sets on the platform (GET /lifecycle
// setting_ranges), and the column bounds when an API before it omits them.
import { describe, expect, it } from "vitest";
import { fmt, MESSAGES } from "@/lib/i18n";
import { inputRange, settingRanges } from "@/lib/lifecycle";

describe("owner setting ranges", () => {
  it("reads the platform range or falls back to the column bounds", () => {
    const ranges = { trash_days: { min: 2, max: 14 }, auto_archive_months: { min: 24, max: 60 } };
    expect(settingRanges({ setting_ranges: ranges })).toEqual(ranges);
    expect(settingRanges({})).toEqual({ trash_days: { min: 1, max: 30 }, auto_archive_months: { min: 12, max: 120 } });
  });

  it("words the range in both languages", () => {
    for (const lang of ["en", "vi"] as const) {
      const text = fmt(MESSAGES[lang]["data.trashDays"], { min: "2", max: "14" });
      expect(text).toContain("14");
      expect(text).not.toContain("{min}");
    }
  });

  it("widens a window's input to the agency's current value when AA narrowed the range", () => {
    expect(inputRange({ min: 2, max: 14 }, 7)).toEqual({ min: 2, max: 14 });
    expect(inputRange({ min: 2, max: 14 }, 21)).toEqual({ min: 2, max: 21 });
    expect(inputRange({ min: 24, max: 60 }, 12)).toEqual({ min: 12, max: 60 });
  });
});
