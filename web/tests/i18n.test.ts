// EN/VI: both dictionaries complete, Vietnamese diacritics kept as NFC, locale resolution order.
import { describe, expect, it } from "vitest";
import { fromAcceptLanguage, langOf, MESSAGES, resolveLocale, t } from "@/lib/i18n";
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

describe("locale resolution", () => {
  it("prefers the person's cookie, then the agency default, then the browser", () => {
    expect(resolveLocale({ cookie: "en-US", agencyDefault: "vi-VN", acceptLanguage: "en-GB" })).toBe("en-US");
    expect(resolveLocale({ cookie: "xx", agencyDefault: "vi-VN", acceptLanguage: "en-GB" })).toBe("vi-VN");
    expect(resolveLocale({ acceptLanguage: "vi,en;q=0.5" })).toBe("vi-VN");
    expect(resolveLocale({})).toBe("en-AU");
  });

  it("maps Accept-Language tags with quality values", () => {
    expect(fromAcceptLanguage("fr-FR,en-US;q=0.8,vi;q=0.9")).toBe("vi-VN");
    expect(fromAcceptLanguage("en-US")).toBe("en-US");
    expect(fromAcceptLanguage("en-IE")).toBe("en-GB");
    expect(fromAcceptLanguage("en-NZ")).toBe("en-AU");
    expect(fromAcceptLanguage("de;q=1,fr;q=0")).toBeNull();
    expect(fromAcceptLanguage(null)).toBeNull();
    expect(langOf("vi-VN")).toBe("vi");
    expect(langOf("en-US")).toBe("en");
  });
});
