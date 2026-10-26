// POST /prefs/locale (form locale, next) — the person's own language and date locale (AA-935, R32). Same-origin
// only. The cookie holds nothing but the locale tag; it is the only thing that turns the UI to Vietnamese (or another
// date locale): without it every page is English (lib/i18n.ts resolveLocale, owner rule S249).
import type { NextRequest } from "next/server";
import { requestConfig } from "@/lib/config";
import { isDateLocale } from "@/lib/dates";
import { safeNext } from "@/lib/hosts";
import { LOCALE_COOKIE } from "@/lib/i18n";
import { json, redirectTo } from "@/lib/respond";
import { refuseForeignWrite } from "@/lib/writeGuard";

export const dynamic = "force-dynamic";

const ONE_YEAR = 365 * 24 * 3600;

export async function POST(request: NextRequest) {
  const cfg = requestConfig(request);
  const refused = refuseForeignWrite(cfg, request);
  if (refused) return refused;
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
