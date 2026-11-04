// web/lib/studio/lang.ts
// AA-1036 (R3) — the Content studio's UI language. ONE setting for the whole agency app: the person's own choice in
// the httpOnly cookie aa_crm_locale (lib/i18n.ts LOCALE_COOKIE), written only by POST /prefs/locale (the CRM's
// language switch, components/LocaleSwitch.tsx, also in the studio's top bar). Absent = English (owner rule S249).
//
// The studio renders with the kit i18n (app/_kit/core/i18n.ts + useI18n.ts, message files `*.messages.ts`). The kit's
// own store (createLangStore) reads a script-readable cookie; this app's cookie is httpOnly, so the studio uses a fixed
// store instead: the server layout resolves the language from the cookie per request and every snapshot returns it.
// Choosing another language posts the same form the CRM switch posts, and the page reloads in that language.
// No React here (the provider is app/_kit/local/studio/StudioLang.tsx).

import type { LangStore, UiLang } from "@/app/_kit";
import type { DateLocale } from "@/lib/dates";
import { LOCALE_COOKIE } from "@/lib/i18n";

/** The date locale a language choice saves ("vi" -> vi-VN; English keeps the person's English variant). */
export function localeForLang(lang: UiLang, current: DateLocale): DateLocale {
  if (lang === "vi") return "vi-VN";
  return current === "vi-VN" ? "en-AU" : current;
}

/** Post the language choice like the CRM switch does (same-origin form, 303 back to `next`). */
export function submitLocale(locale: DateLocale, next: string, doc: Document = document): void {
  const form = doc.createElement("form");
  form.method = "post";
  form.action = "/prefs/locale";
  form.style.display = "none";
  for (const [name, value] of [["locale", locale], ["next", next]] as const) {
    const input = doc.createElement("input");
    input.type = "hidden";
    input.name = name;
    input.value = value;
    form.appendChild(input);
  }
  doc.body.appendChild(form);
  form.submit();
}

/** A kit LangStore that always answers the language the server resolved for this request. */
export function studioLangStore(lang: UiLang, locale: DateLocale, submit: typeof submitLocale = submitLocale): LangStore {
  return {
    cookieName: LOCALE_COOKIE,
    readLang: () => lang,
    subscribe: () => () => {},
    getSnapshot: () => lang,
    getServerSnapshot: () => lang,
    setLang: (next: UiLang) => {
      if (next === lang || typeof window === "undefined") return;
      submit(localeForLang(next, locale), window.location.pathname + window.location.search);
    },
    // <html lang> is set by the root layout from the same cookie (app/layout.tsx); nothing to restore here.
    restoreScript: "",
  };
}
