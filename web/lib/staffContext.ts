// web/lib/staffContext.ts
// AA-1019 — what a staff console server component needs: the configuration of this host, the STAFF session (never the
// agency one), the locale and language (the same aa_crm_locale choice as the agency app, English by default).

import { cookies, headers } from "next/headers";
import { config, forHost, type WebConfig } from "./config";
import type { DateLocale } from "./dates";
import { langOf, LOCALE_COOKIE, resolveLocale, type Lang } from "./i18n";
import { readStaffSession, type StaffSession } from "./staffSession";

export interface StaffContext {
  cfg: WebConfig;
  session: StaffSession | null;
  locale: DateLocale;
  lang: Lang;
  path: string;
}

export async function staffContext(): Promise<StaffContext> {
  const h = await headers();
  const cfg = forHost(config(), h.get("host"));
  const jar = await cookies();
  const locale = resolveLocale({ cookie: jar.get(LOCALE_COOKIE)?.value });
  return {
    cfg,
    session: cfg.staffAuthConfigured ? readStaffSession(cfg, jar) : null,
    locale,
    lang: langOf(locale),
    path: h.get("x-aa-path") ?? "/staff",
  };
}
