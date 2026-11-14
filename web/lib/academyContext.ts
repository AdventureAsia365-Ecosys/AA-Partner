// web/lib/academyContext.ts
// AA-1105 — what an academy server component needs: the configuration of this host, the ACADEMY session (never the
// agency or the staff console one), the locale and language (the same aa_crm_locale choice as the rest of the web,
// English by default) and the path the proxy recorded.

import { cookies, headers } from "next/headers";
import { config, forHost, type WebConfig } from "./config";
import type { DateLocale } from "./dates";
import { ACADEMY_PREFIX } from "./hosts";
import { langOf, LOCALE_COOKIE, resolveLocale, type Lang } from "./i18n";
import { readAcademySession, type AcademySession } from "./academySession";

export interface AcademyContext {
  cfg: WebConfig;
  session: AcademySession | null;
  locale: DateLocale;
  lang: Lang;
  path: string;
  /** The visitor address CloudFront saw (CloudFront-Viewer-Address), for the verify page's own limit. */
  viewerAddress: string | null;
}

export async function academyContext(): Promise<AcademyContext> {
  const h = await headers();
  const cfg = forHost(config(), h.get("host"));
  const jar = await cookies();
  const locale = resolveLocale({ cookie: jar.get(LOCALE_COOKIE)?.value });
  return {
    cfg,
    session: cfg.academyAuthConfigured ? readAcademySession(cfg, jar) : null,
    locale,
    lang: langOf(locale),
    path: h.get("x-aa-path") ?? ACADEMY_PREFIX,
    viewerAddress: h.get("cloudfront-viewer-address"),
  };
}
