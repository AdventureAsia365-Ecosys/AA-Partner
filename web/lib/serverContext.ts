// web/lib/serverContext.ts
// AA-935 — what a server component needs about the current request: configuration, session, locale, language and
// the path the proxy recorded (x-aa-path, set by proxy.ts after it strips any client copy).

import { cookies, headers } from "next/headers";
import { config, type WebConfig } from "./config";
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

export async function serverContext(agencyDefault?: string | null): Promise<ServerContext> {
  const cfg = config();
  const jar = await cookies();
  const h = await headers();
  const locale = resolveLocale({
    cookie: jar.get(LOCALE_COOKIE)?.value,
    agencyDefault,
    acceptLanguage: h.get("accept-language"),
  });
  return {
    cfg,
    session: cfg.authConfigured ? readSession(cfg, jar) : null,
    locale,
    lang: langOf(locale),
    path: h.get("x-aa-path") ?? "/",
  };
}
