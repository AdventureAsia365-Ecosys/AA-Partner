// POST /prefs/locale (form locale, next) — the person's own language and date locale (AA-935, R32). Same-origin
// only. The cookie holds nothing but the locale tag; it wins over the agency default (lib/i18n.ts resolveLocale).
import type { NextRequest } from "next/server";
import { config } from "@/lib/config";
import { refuseCrossSiteWrite } from "@/lib/crossSiteGuard";
import { isDateLocale } from "@/lib/dates";
import { safeNext } from "@/lib/hosts";
import { LOCALE_COOKIE } from "@/lib/i18n";
import { json, redirectTo } from "@/lib/respond";

export const dynamic = "force-dynamic";

const ONE_YEAR = 365 * 24 * 3600;

export async function POST(request: NextRequest) {
  const refused = refuseCrossSiteWrite(request);
  if (refused) return refused;
  const cfg = config();
  const form = await request.formData();
  const locale = String(form.get("locale") ?? "");
  if (!isDateLocale(locale)) return json({ detail: "invalid_locale" }, 422);
  const res = redirectTo(cfg, safeNext(String(form.get("next") ?? "/")), 303, request.nextUrl.origin);
  res.cookies.set(LOCALE_COOKIE, locale, {
    httpOnly: true,
    secure: cfg.secureCookies,
    sameSite: "lax",
    path: "/",
    maxAge: ONE_YEAR,
  });
  return res;
}
