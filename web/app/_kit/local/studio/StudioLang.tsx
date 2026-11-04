"use client";
// web/app/_kit/local/studio/StudioLang.tsx
// AA-1036 (R3) — the Content studio's language provider: the kit I18nProvider (core/useI18n.ts) around every studio
// page, fed by the language the server layout resolved from the app's one setting (lib/studio/lang.ts). Kit core
// components inside (LoadingScreen, ErrorState, Field…) follow it too. useStudioLocale() gives the date locale the
// language switch shows (components/LocaleSwitch.tsx).

import { createContext, useContext, useMemo, type ReactNode } from "react";
import { I18nProvider, type UiLang } from "@/app/_kit";
import type { DateLocale } from "@/lib/dates";
import { studioLangStore } from "@/lib/studio/lang";

const LocaleContext = createContext<DateLocale>("en-AU");

export function StudioLang({ lang, locale, children }: { lang: UiLang; locale: DateLocale; children: ReactNode }) {
  const store = useMemo(() => studioLangStore(lang, locale), [lang, locale]);
  return (
    <LocaleContext.Provider value={locale}>
      <I18nProvider store={store} initialLang={lang}>{children}</I18nProvider>
    </LocaleContext.Provider>
  );
}

/** The person's date locale (en-AU, en-GB, en-US, vi-VN): the value the language switch shows. */
export function useStudioLocale(): DateLocale {
  return useContext(LocaleContext);
}
