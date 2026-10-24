// EN/VI: both dictionaries complete, Vietnamese diacritics kept as NFC, locale resolution order (English by default).
import { describe, expect, it } from "vitest";
import { formatDate } from "@/lib/dates";
import { explicitLocale, langOf, MESSAGES, resolveLocale, t } from "@/lib/i18n";
import { SECTIONS } from "@/lib/nav";

describe("dictionaries", () => {
  it("have the same keys and no empty string in either language", () => {
    expect(Object.keys(MESSAGES.vi).sort()).toEqual(Object.keys(MESSAGES.en).sort());
    for (const lang of ["en", "vi"] as const) {
      for (const [key, value] of Object.entries(MESSAGES[lang])) expect(value.trim(), `${lang}.${key}`).not.toBe("");
    }
  });

  it("keep Vietnamese text in NFC (diacritics round-trip byte for byte)", () => {
    for (const value of Object.values(MESSAGES.vi)) expect(value.normalize("NFC")).toBe(value);
    const decoded = new TextDecoder().decode(new TextEncoder().encode(t("vi", "nav.clients")));
    expect(decoded).toBe("Khách hàng");
  });

  it("cover every nav label and empty state", () => {
    for (const s of SECTIONS) {
      expect(MESSAGES.en[s.label]).toBeTruthy();
      if (s.empty) expect(MESSAGES.vi[s.empty.title]).toBeTruthy();
    }
  });
});

describe("locale resolution (owner rule S249, AA-936: English unless the person picks another)", () => {
  it("uses the person's cookie first, then a language set on the record, else English (en-AU)", () => {
    expect(resolveLocale({ cookie: "vi-VN", explicit: "en" })).toBe("vi-VN");
    expect(resolveLocale({ cookie: "en-US" })).toBe("en-US");
    expect(resolveLocale({ cookie: "xx", explicit: "vi" })).toBe("vi-VN");
    expect(resolveLocale({ explicit: "en-GB" })).toBe("en-GB");
    expect(resolveLocale({})).toBe("en-AU");
    expect(resolveLocale({ cookie: null, explicit: null })).toBe("en-AU");
  });

  it("ignores anything that is not an explicit choice (agency default, browser language are not inputs)", () => {
    // The old inputs are gone from the signature: an agency in vi-VN or a Vietnamese browser no longer switch the UI.
    const legacy = { agencyDefault: "vi-VN", acceptLanguage: "vi,en;q=0.5" } as unknown as Parameters<typeof resolveLocale>[0];
    expect(resolveLocale(legacy)).toBe("en-AU");
    expect(explicitLocale("fr")).toBeNull();
    expect(explicitLocale(undefined)).toBeNull();
    expect(explicitLocale("vi")).toBe("vi-VN");
    expect(explicitLocale("en")).toBe("en-AU");
  });

  it("derives the UI language and the date reading from the chosen locale", () => {
    expect(langOf("vi-VN")).toBe("vi");
    expect(langOf("en-US")).toBe("en");
    expect(formatDate("2026-04-03", resolveLocale({}))).toBe("3 Apr 2026");
    expect(formatDate("2026-04-03", resolveLocale({ cookie: "vi-VN" }))).toBe("3 thg 4, 2026");
  });
});
