// R32 / card CRM-07 E test: "03/04 renders 3 Apr for en-AU and Apr 3 for en-US"; typed dates follow the locale.
import { describe, expect, it } from "vitest";
import {
  dayFirst,
  formatDate,
  formatDayMonth,
  formatOutward,
  inputPattern,
  isDateLocale,
  parseDateInput,
  toDate,
} from "@/lib/dates";

describe("display spells the month", () => {
  it("renders 3 April as 3 Apr for en-AU and Apr 3 for en-US", () => {
    expect(formatDayMonth("2026-04-03", "en-AU")).toBe("3 Apr");
    expect(formatDayMonth("2026-04-03", "en-US")).toBe("Apr 3");
    expect(formatDayMonth("2026-04-03", "en-GB")).toBe("3 Apr");
  });

  it("includes the year by default and spells the month in full for outward text", () => {
    expect(formatDate("2026-04-03", "en-AU")).toBe("3 Apr 2026");
    expect(formatDate("2026-04-03", "en-US")).toBe("Apr 3, 2026");
    expect(formatDate("2026-04-03", "vi-VN")).toMatch(/^3 thg 4,? 2026$/);
    expect(formatOutward("2026-04-03", "en-AU")).toBe("3 April 2026");
    expect(formatOutward("2026-04-03", "en-US")).toBe("April 3, 2026");
    expect(formatOutward("2026-04-03", "vi-VN")).toMatch(/tháng 4/);
  });

  it("does not drift across midnight for calendar dates and shows a dash for nothing", () => {
    expect(formatDate("2026-12-31", "en-AU")).toBe("31 Dec 2026");
    expect(formatDate(null, "en-AU")).toBe("—");
    expect(formatDate("not a date", "en-US")).toBe("—");
    expect(toDate("2026-02-30")).toBeNull();
    expect(formatOutward(undefined, "en-AU")).toBe("—");
  });
});

describe("typed dates follow the locale order", () => {
  it("reads 03/04 as 3 April day-first and as March 4 for en-US", () => {
    expect(parseDateInput("03/04", "en-AU", 2026)).toBe("2026-04-03");
    expect(parseDateInput("03/04", "vi-VN", 2026)).toBe("2026-04-03");
    expect(parseDateInput("03/04", "en-US", 2026)).toBe("2026-03-04");
    // ... and the spelled reading removes the ambiguity.
    expect(formatDate(parseDateInput("03/04", "en-AU", 2026), "en-AU")).toBe("3 Apr 2026");
    expect(formatDate(parseDateInput("03/04", "en-US", 2026), "en-US")).toBe("Mar 4, 2026");
  });

  it("accepts years, separators and ISO; refuses impossible dates", () => {
    expect(parseDateInput("3.4.26", "en-GB")).toBe("2026-04-03");
    expect(parseDateInput("04-03-2027", "en-US")).toBe("2027-04-03");
    expect(parseDateInput("2026-04-03", "en-US")).toBe("2026-04-03");
    expect(parseDateInput("31/02/2026", "en-AU")).toBeNull();
    expect(parseDateInput("13/13", "en-AU")).toBeNull();
    expect(parseDateInput("2026-13-01", "en-AU")).toBeNull();
    expect(parseDateInput("", "en-AU")).toBeNull();
    expect(parseDateInput("next friday", "en-AU")).toBeNull();
    expect(typeof parseDateInput("1/2", "en-AU")).toBe("string");
  });

  it("knows each locale's field order", () => {
    expect(dayFirst("en-US")).toBe(false);
    expect(dayFirst("vi-VN")).toBe(true);
    expect(inputPattern("en-US")).toBe("MM/DD/YYYY");
    expect(inputPattern("en-AU")).toBe("DD/MM/YYYY");
    expect(isDateLocale("vi-VN")).toBe(true);
    expect(isDateLocale("fr-FR")).toBe(false);
  });
});
