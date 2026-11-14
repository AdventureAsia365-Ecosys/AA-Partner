// GET /academy/auth/sign-in?next=/academy/... — start the AA Academy sign-in (AA-1105, root ADR 0022 decision 6): the
// WORKFORCE managed login with the academy-web client, authorization code + PKCE (lib/oauth.ts with
// lib/academySession.ts academyClient). The attempt lives in the academy tx cookie (Path /academy/auth) on the academy
// host only.
import type { NextRequest } from "next/server";
import { requestConfig } from "@/lib/config";
import { langOf, LOCALE_COOKIE, resolveLocale } from "@/lib/i18n";
import { authorizeUrl, randomToken } from "@/lib/oauth";
import { redirectExternal, redirectTo, withCookies } from "@/lib/respond";
import { ACADEMY_SIGNED_OUT, academyClient, academyTxCookie, safeAcademyNext } from "@/lib/academySession";

export const dynamic = "force-dynamic";

export function GET(request: NextRequest) {
  const cfg = requestConfig(request);
  if (!cfg.academyAuthConfigured) {
    return redirectTo(cfg, `${ACADEMY_SIGNED_OUT}?reason=unavailable`, 307, request.nextUrl.origin);
  }
  const next = safeAcademyNext(request.nextUrl.searchParams.get("next"));
  const locale = resolveLocale({ cookie: request.cookies.get(LOCALE_COOKIE)?.value });
  const tx = { state: randomToken(), verifier: randomToken(48), nonce: randomToken(), next };
  const res = redirectExternal(authorizeUrl(academyClient(cfg), { ...tx, lang: langOf(locale) }));
  return withCookies(res, [academyTxCookie(cfg, tx)]);
}
