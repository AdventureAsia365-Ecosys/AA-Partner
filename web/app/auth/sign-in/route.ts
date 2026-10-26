// GET /auth/sign-in?next=/path — start the Cognito managed-login flow (code + PKCE). AA-935.
import type { NextRequest } from "next/server";
import { requestConfig } from "@/lib/config";
import { safeNext } from "@/lib/hosts";
import { langOf, LOCALE_COOKIE, resolveLocale } from "@/lib/i18n";
import { authorizeUrl, randomToken } from "@/lib/oauth";
import { redirectExternal, redirectTo, withCookies } from "@/lib/respond";
import { txCookie } from "@/lib/session";

export const dynamic = "force-dynamic";

export function GET(request: NextRequest) {
  const cfg = requestConfig(request);
  if (!cfg.authConfigured) return redirectTo(cfg, "/signed-out?reason=unavailable", 307, request.nextUrl.origin);
  const next = safeNext(request.nextUrl.searchParams.get("next"));
  // The managed-login page speaks the person's saved language, else English (AA-936; no Accept-Language).
  const locale = resolveLocale({ cookie: request.cookies.get(LOCALE_COOKIE)?.value });
  const tx = { state: randomToken(), verifier: randomToken(48), nonce: randomToken(), next };
  const res = redirectExternal(authorizeUrl(cfg, { ...tx, lang: langOf(locale) }));
  return withCookies(res, [txCookie(cfg, tx)]);
}
