// POST /clients/search (form q, archived, owner) — the clients directory search (AA-937). Same-origin only. The term is
// sealed into a short-lived cookie (lib/clientSearch.ts) and the browser is sent to /clients?s=1, so an e-mail or a
// phone number never appears in a URL. An empty term clears the search.
import type { NextRequest } from "next/server";
import { directoryHref, SEARCH_MAX } from "@/lib/clients";
import { searchCookie } from "@/lib/clientSearch";
import { config } from "@/lib/config";
import { redirectTo } from "@/lib/respond";
import { readSession } from "@/lib/session";
import { refuseForeignWrite } from "@/lib/writeGuard";

export const dynamic = "force-dynamic";

export async function POST(request: NextRequest) {
  const cfg = config();
  const refused = refuseForeignWrite(cfg, request);
  if (refused) return refused;
  const form = await request.formData();
  const q = String(form.get("q") ?? "").trim().slice(0, SEARCH_MAX);
  const archived = form.get("archived") === "true";
  const owner = form.get("owner") === "me" ? "me" : "anyone";
  const session = readSession(cfg, request.cookies);
  const cookie = searchCookie(cfg, session, q);
  const res = redirectTo(cfg, directoryHref({ search: Boolean(cookie.value), archived, owner }), 303, request.nextUrl.origin);
  res.cookies.set(cookie.name, cookie.value, {
    httpOnly: cookie.httpOnly,
    secure: cookie.secure,
    sameSite: cookie.sameSite,
    path: cookie.path,
    maxAge: cookie.maxAge,
  });
  return res;
}

/** A GET here (e.g. after a session refresh replayed the URL) has no term: back to the directory. */
export async function GET(request: NextRequest) {
  return redirectTo(config(), "/clients", 303, request.nextUrl.origin);
}
