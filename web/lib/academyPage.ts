// web/lib/academyPage.ts
// AA-1105 — one read of the learning API for an academy server page, with the refresh rule of the agency pages
// (lib/crmContext.ts): a 401 sends the page once through /academy/auth/refresh with the one-shot marker; a second 401
// on the marked path means CIS refuses tokens this web accepts, and the page shows an error instead of looping.

import { redirect } from "next/navigation";
import type { AcademyErrorKind } from "./academy";
import { learnCall } from "./academyApi";
import type { AcademyContext } from "./academyContext";
import { ACADEMY_AUTH_PREFIX, ACADEMY_SIGNED_OUT, academyAccessValid } from "./academySession";
import { hasRefreshMarker, withRefreshMarker } from "./hosts";

export type PageRead<T> = { ok: true; data: T } | { ok: false; kind: AcademyErrorKind | "refused"; retryAt: string | null };

/** The session must be valid (the proxy already checked; this is the second line). Redirects otherwise. */
export function requireAcademySession(ctx: AcademyContext): string {
  if (!ctx.cfg.academyAuthConfigured) redirect(`${ACADEMY_SIGNED_OUT}?reason=unavailable`);
  if (!academyAccessValid(ctx.session)) redirect(`${ACADEMY_AUTH_PREFIX}/refresh?next=${encodeURIComponent(ctx.path)}`);
  return ctx.session.at;
}

export async function academyRead<T>(ctx: AcademyContext, subPath: string, query?: URLSearchParams): Promise<PageRead<T>> {
  const token = requireAcademySession(ctx);
  const res = await learnCall<T>(ctx.cfg.cisApiUrl, token, "GET", subPath, { query });
  if (res.ok) return { ok: true, data: res.data };
  if (res.kind === "unauthorized") {
    if (hasRefreshMarker(ctx.path)) return { ok: false, kind: "refused", retryAt: null };
    redirect(`${ACADEMY_AUTH_PREFIX}/refresh?next=${encodeURIComponent(withRefreshMarker(ctx.path))}`);
  }
  return { ok: false, kind: res.kind, retryAt: res.problem?.retry_at ?? null };
}

/** The message key of a refused read. */
export function refusalKey(kind: AcademyErrorKind | "refused"):
  | "academy.notLearner"
  | "academy.partnerNotOpen"
  | "academy.forbidden"
  | "academy.apiDown"
  | "academy.notFound"
  | "academy.refused"
  | "academy.error" {
  switch (kind) {
    case "not_a_learner":
      return "academy.notLearner";
    case "partner_not_open":
      return "academy.partnerNotOpen";
    case "forbidden":
      return "academy.forbidden";
    case "unavailable":
      return "academy.apiDown";
    case "not_found":
      return "academy.notFound";
    case "refused":
      return "academy.refused";
    default:
      return "academy.error";
  }
}
