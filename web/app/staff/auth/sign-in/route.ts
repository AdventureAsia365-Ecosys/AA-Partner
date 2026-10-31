// GET /staff/auth/sign-in?next=/staff/... — start the AA staff console's sign-in (AA-1019): the WORKFORCE managed login
// with the crm-staff client, authorization code + PKCE (lib/oauth.ts with lib/staffSession.ts staffClient). The attempt
// lives in the staff tx cookie (Path /staff/auth), never in the agency's.
import type { NextRequest } from "next/server";
import { requestConfig } from "@/lib/config";
import { langOf, LOCALE_COOKIE, resolveLocale } from "@/lib/i18n";
import { authorizeUrl, randomToken } from "@/lib/oauth";
import { redirectExternal, redirectTo, withCookies } from "@/lib/respond";
import { safeStaffNext, STAFF_SIGNED_OUT, staffClient, staffTxCookie } from "@/lib/staffSession";

export const dynamic = "force-dynamic";

export function GET(request: NextRequest) {
  const cfg = requestConfig(request);
  if (!cfg.staffAuthConfigured) {
    return redirectTo(cfg, `${STAFF_SIGNED_OUT}?reason=unavailable`, 307, request.nextUrl.origin);
  }
  const next = safeStaffNext(request.nextUrl.searchParams.get("next"));
  const locale = resolveLocale({ cookie: request.cookies.get(LOCALE_COOKIE)?.value });
  const tx = { state: randomToken(), verifier: randomToken(48), nonce: randomToken(), next };
  const res = redirectExternal(authorizeUrl(staffClient(cfg), { ...tx, lang: langOf(locale) }));
  return withCookies(res, [staffTxCookie(cfg, tx)]);
}
