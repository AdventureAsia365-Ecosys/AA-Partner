// web/lib/crmContext.ts
// AA-935 — the signed-in agency context of one request, shared by the (crm) layout and its pages through React
// cache() (one /me and one /memberships call per request, whatever reads it). Redirects when the request cannot be
// served: no configuration -> /signed-out, no valid access token -> /auth/refresh, no agency picked or no longer a
// member -> the chooser. An unreachable API is returned as `apiDown` so the layout can show an error state; so is an
// API that still answers 401 right after a refresh (`reason: "refused"`, one-shot marker in lib/hosts.ts), which
// would otherwise loop page -> /auth/refresh -> page.

import { redirect } from "next/navigation";
import { cache } from "react";
import { fetchMe, fetchMemberships, type Me, type Membership } from "./crmApi";
import type { DateLocale } from "./dates";
import { hasRefreshMarker, withRefreshMarker } from "./hosts";
import type { Lang } from "./i18n";
import { isRole, type Role } from "./nav";
import { serverContext } from "./serverContext";
import { accessValid } from "./session";
import type { WebConfig } from "./config";

export type CrmContext =
  | { apiDown: true; reason: "down" | "refused"; lang: Lang; locale: DateLocale; cfg: WebConfig }
  | {
      apiDown: false;
      cfg: WebConfig;
      lang: Lang;
      locale: DateLocale;
      me: Me;
      role: Role;
      memberships: Membership[];
      path: string;
    };

export const crmContext = cache(async (): Promise<CrmContext> => {
  const base = await serverContext();
  const { cfg, session, path } = base;
  const back = encodeURIComponent(path);
  if (!cfg.authConfigured) redirect("/signed-out?reason=unavailable");
  if (!accessValid(session)) redirect(`/auth/refresh?next=${back}`);
  if (!session.tid) redirect(`/auth/agency?next=${back}`);
  const me = await fetchMe(cfg, session);
  if (!me.ok) {
    if (me.status === 401) {
      if (hasRefreshMarker(path)) return { apiDown: true, reason: "refused", cfg, lang: base.lang, locale: base.locale };
      redirect(`/auth/refresh?next=${encodeURIComponent(withRefreshMarker(path))}`);
    }
    if (me.status === 403 || me.status === 409 || me.status === 400) redirect(`/select-agency?next=${back}&manual=1`);
    return { apiDown: true, reason: "down", cfg, lang: base.lang, locale: base.locale };
  }
  if (!isRole(me.data.member_role)) redirect(`/select-agency?next=${back}&manual=1`);
  const memberships = await fetchMemberships(cfg, session);
  const withAgency = await serverContext(me.data.default_locale);
  return {
    apiDown: false,
    cfg,
    lang: withAgency.lang,
    locale: withAgency.locale,
    me: me.data,
    role: me.data.member_role,
    memberships: memberships.ok ? memberships.data : [],
    path,
  };
});
