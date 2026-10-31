// /staff/bff/<path> — the staff console browser's only way to the CRM API (AA-1019). Order:
//   0. a Host other than the partner host -> 404;
//   1. a cross-site write -> 403 (lib/writeGuard.ts, before any cookie or body is read); then ANY method (GET too: some
//      staff reads count a view or write the agency's access log) without the console's own call header, or with a
//      Sec-Fetch-Site other than same-origin -> 403 (lib/staffBff.ts consoleCallRefusal), still before any cookie;
//   2. a path segment that could escape the prefix -> 400 (lib/bffPath.ts);
//   3. not on the staff allow-list (lib/staffBff.ts) -> 404;
//   4. no STAFF session or an expired staff access token -> 401 session_expired (the page goes through
//      /staff/auth/refresh). The agency session is never read: an agency cookie alone is 401 here;
//   5. the session's groups do not include one the route names -> 403 missing_group (no upstream call);
//   6. forwarded to /crm/staff/v1/<path> (only listed query parameters) with the staff token; only Content-Type comes
//      back (lib/proxyResponse.ts), never a Set-Cookie of the API.
import { randomUUID } from "node:crypto";
import type { NextRequest } from "next/server";
import { proxyPath } from "@/lib/bffPath";
import { requestConfig } from "@/lib/config";
import { classifyHost } from "@/lib/hosts";
import { streamUpstream } from "@/lib/proxyResponse";
import { json } from "@/lib/respond";
import { allowedQuery, consoleCallRefusal, groupAllowed, matchStaffBff, STAFF_API_PREFIX, staffUpstreamHeaders } from "@/lib/staffBff";
import { readStaffSession, staffAccessValid } from "@/lib/staffSession";
import { refuseForeignWrite } from "@/lib/writeGuard";

export const dynamic = "force-dynamic";

const UPSTREAM_TIMEOUT_MS = 15_000;
const MAX_BODY_BYTES = 8 * 1024; // session starts and ends: a few ids and codes

async function handle(request: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  const cfg = requestConfig(request);
  if (classifyHost(request.headers.get("host"), cfg) !== "partner") return json({ detail: "not_found" }, 404);
  const refused = refuseForeignWrite(cfg, request);
  if (refused) return refused;
  const notConsole = consoleCallRefusal(request.headers);
  if (notConsole) return json({ detail: notConsole }, 403);
  const { path } = await ctx.params;
  const sub = proxyPath(path ?? []);
  if (sub === null) return json({ detail: "invalid_path" }, 400);
  const route = matchStaffBff(request.method, sub);
  if (!route) return json({ detail: "not_found" }, 404);

  const session = readStaffSession(cfg, request.cookies);
  if (!staffAccessValid(session)) return json({ detail: "session_expired" }, 401);
  if (!groupAllowed(route, session.groups)) return json({ detail: "missing_group" }, 403);

  let body: string | undefined;
  if (request.method === "POST") {
    body = await request.text();
    if (Buffer.byteLength(body) > MAX_BODY_BYTES) return json({ detail: "body_too_large" }, 413);
  }
  const query = allowedQuery(sub, request.nextUrl.searchParams).toString();
  try {
    const upstream = await fetch(`${cfg.crmApiOrigin}${STAFF_API_PREFIX}/${sub}${query ? `?${query}` : ""}`, {
      method: request.method,
      headers: staffUpstreamHeaders(session.at, randomUUID(), body !== undefined),
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
