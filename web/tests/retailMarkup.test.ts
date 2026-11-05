// AA-1030: the retail markup form's input rule, the BFF allow-list lines of its routes, and the new copy in both
// languages.
import { describe, expect, it } from "vitest";
import { markupInput } from "@/components/proposals/RetailMarkupForm";
import { matchBff } from "@/lib/bff";
import { MESSAGES } from "@/lib/i18n";

describe("retail markup", () => {
  it("reads a percentage from 0 to 300 with at most 2 decimals, empty = no markup", () => {
    expect(markupInput("")).toBeNull();
    expect(markupInput("  ")).toBeNull();
    expect(markupInput("20")).toBe(20);
    expect(markupInput("12.5")).toBe(12.5);
    expect(markupInput("300")).toBe(300);
    expect(markupInput("301")).toBeUndefined();
    expect(markupInput("-1")).toBeUndefined();
    expect(markupInput("1.234")).toBeUndefined();
    expect(markupInput("ten")).toBeUndefined();
  });

  it("lets the settings routes through the BFF and nothing else", () => {
    expect(matchBff("GET", "settings/retail-markup")).not.toBeNull();
    expect(matchBff("PUT", "settings/retail-markup")).not.toBeNull();
    expect(matchBff("DELETE", "settings/retail-markup")).toBeNull();
  });

  it("has every new message in English and Vietnamese", () => {
    const keys = ["aa.suggested", "aa.suggested.agency_markup", "aa.suggested.aa_rrp", "aa.useSuggested", "markup.title",
      "markup.kind.trip_plan", "markup.kind.meal", "markup.save", "markup.saved", "markup.error"] as const;
    for (const key of keys) {
      expect(MESSAGES.en[key]).toBeTruthy();
      expect(MESSAGES.vi[key]).toBeTruthy();
      expect(MESSAGES.vi[key]).not.toBe(MESSAGES.en[key]);
    }
  });
});
