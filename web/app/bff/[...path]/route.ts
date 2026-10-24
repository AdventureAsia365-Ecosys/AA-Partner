// /bff/<path> — the browser's only way to the CRM API (AA-935, card CRM-07 "BFF allow-list"). Order:
//   1. a cross-site write -> 403 (lib/crossSiteGuard.ts, before any cookie or body is read);
//   2. a path segment that could escape the prefix -> 400 (lib/bffPath.ts);
//   3. not on the allow-list (lib/bff.ts) -> 404;
//   4. no session or an expired access token -> 401 session_expired (the page reloads through /auth/refresh);
//   5. forwarded to /crm/v1/<path> with the session's token and agency; only Content-Type comes back
//      (lib/proxyResponse.ts), never a Set-Cookie of the API.
import { randomUUID } from "node:crypto";
import type { NextRequest } from "next/server";
import { matchBff, upstreamHeaders } from "@/lib/bff";
import { proxyPath } from "@/lib/bffPath";
import { config } from "@/lib/config";
import { streamUpstream } from "@/lib/proxyResponse";
import { json } from "@/lib/respond";
import { accessValid, readSession } from "@/lib/session";
import { refuseForeignWrite } from "@/lib/writeGuard";

export const dynamic = "force-dynamic";

const UPSTREAM_TIMEOUT_MS = 15_000;
const MAX_BODY_BYTES = 256 * 1024;

async function handle(request: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  const refused = refuseForeignWrite(config(), request);
  if (refused) return refused;
  const { path } = await ctx.params;
  const sub = proxyPath(path ?? []);
  if (sub === null) return json({ detail: "invalid_path" }, 400);
  const route = matchBff(request.method, sub);
  if (!route) return json({ detail: "not_found" }, 404);

  const cfg = config();
  const session = cfg.authConfigured ? readSession(cfg, request.cookies) : null;
  if (!accessValid(session)) return json({ detail: "session_expired" }, 401);
  if (route.tenant && !session.tid) return json({ detail: "no_agency_selected" }, 409);

  let body: string | undefined;
  if (request.method !== "GET" && request.method !== "DELETE") {
    body = await request.text();
    if (Buffer.byteLength(body) > MAX_BODY_BYTES) return json({ detail: "body_too_large" }, 413);
  }
  const headers = upstreamHeaders(
    session.at,
    route.tenant ? session.tid : null,
    request.headers.get("content-type"),
    randomUUID(),
  );
  const query = request.nextUrl.search;
  try {
    const upstream = await fetch(`${cfg.crmApiOrigin}/crm/v1/${sub}${query}`, {
      method: request.method,
      headers,
      body,
      cache: "no-store",
      redirect: "manual",
      signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
    });
    const res = streamUpstream(upstream);
    res.headers.set("Cache-Control", "no-store");
    return res;
  } catch (err) {
    const timedOut = err instanceof DOMException && err.name === "TimeoutError";
    return json({ detail: timedOut ? "api_timeout" : "api_unreachable" }, timedOut ? 504 : 502);
  }
}

export const GET = handle;
export const POST = handle;
export const PUT = handle;
export const PATCH = handle;
export const DELETE = handle;
