// web/lib/serverContext.ts
// AA-935 — what a server component needs about the current request: configuration, session, locale, language and
// the path the proxy recorded (x-aa-path, set by proxy.ts after it strips any client copy).

import { cookies, headers } from "next/headers";
import { config, forHost, type WebConfig } from "./config";
import type { DateLocale } from "./dates";
import { langOf, LOCALE_COOKIE, resolveLocale, type Lang } from "./i18n";
import { readSession, type Session } from "./session";

export interface ServerContext {
  cfg: WebConfig;
  session: Session | null;
  locale: DateLocale;
  lang: Lang;
  path: string;
}

/**
 * `explicit`: a language set on the page's own record (a public form's `locale`), never the agency's default_locale
 * nor Accept-Language — without the person's cookie or that, the page is English (lib/i18n.ts resolveLocale).
 */
export async function serverContext(explicit?: string | null): Promise<ServerContext> {
  const h = await headers();
  // AA-979 P2b: the configuration of the host the page is served on (its own public origin).
  const cfg = forHost(config(), h.get("host"));
  const jar = await cookies();
  const locale = resolveLocale({ cookie: jar.get(LOCALE_COOKIE)?.value, explicit });
  return {
    cfg,
    session: cfg.authConfigured ? readSession(cfg, jar) : null,
    locale,
    lang: langOf(locale),
    path: h.get("x-aa-path") ?? "/",
  };
}
