// /academy/bff/<path> — the academy pages' browser's only way to the CIS learning API (AA-1105, learn.http.v1). Order:
//   0. a Host other than the academy host -> 404;
//   1. a cross-site write -> 403 (lib/writeGuard.ts, before any cookie or body is read); then ANY method without the
//      academy page's own call header, or with a Sec-Fetch-Site other than same-origin -> 403 (lib/academyBff.ts);
//   2. a path segment that could escape the prefix -> 400 (lib/bffPath.ts);
//   3. not on the academy allow-list -> 404;
//   4. no ACADEMY session or an expired academy access token -> 401 session_expired (the page goes through
//      /academy/auth/refresh). No other session is ever read;
//   5. forwarded to CIS /learn/v1/<path> (only listed query parameters) with the academy token; the learning API's
//      status and Problem body come back (only Content-Type, never a Set-Cookie).
import { randomUUID } from "node:crypto";
import type { NextRequest } from "next/server";
import { proxyPath } from "@/lib/bffPath";
import { requestConfig } from "@/lib/config";
import { academyHostAllowed } from "@/lib/hosts";
import { streamUpstream } from "@/lib/proxyResponse";
import { json } from "@/lib/respond";
import { academyCallRefusal, academyUpstreamHeaders, allowedAcademyQuery, LEARN_API_PREFIX, matchAcademyBff } from "@/lib/academyBff";
import { academyAccessValid, readAcademySession } from "@/lib/academySession";
import { refuseForeignWrite } from "@/lib/writeGuard";

export const dynamic = "force-dynamic";

const UPSTREAM_TIMEOUT_MS = 15_000; // technical constant: one learning API call
const MAX_BODY_BYTES = 64 * 1024; // technical constant: a quiz submission (learn.http.v1 caps it at 200 x 20 choices)

async function handle(request: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  const cfg = requestConfig(request);
  if (!academyHostAllowed(request.headers.get("host"), cfg)) return json({ detail: "not_found" }, 404);
  const refused = refuseForeignWrite(cfg, request);
  if (refused) return refused;
  const notPage = academyCallRefusal(request.headers);
  if (notPage) return json({ detail: notPage }, 403);
  const { path } = await ctx.params;
  const sub = proxyPath(path ?? []);
  if (sub === null) return json({ detail: "invalid_path" }, 400);
  if (!matchAcademyBff(request.method, sub)) return json({ detail: "not_found" }, 404);

  const session = readAcademySession(cfg, request.cookies);
  if (!academyAccessValid(session)) return json({ detail: "session_expired" }, 401);

  let body: string | undefined;
  if (request.method === "POST") {
    body = await request.text();
    if (Buffer.byteLength(body) > MAX_BODY_BYTES) return json({ detail: "body_too_large" }, 413);
    if (body === "") body = undefined;
  }
  const query = allowedAcademyQuery(sub, request.nextUrl.searchParams).toString();
  try {
    const upstream = await fetch(`${cfg.cisApiUrl}${LEARN_API_PREFIX}/${sub}${query ? `?${query}` : ""}`, {
      method: request.method,
      headers: academyUpstreamHeaders(session.at, randomUUID(), body !== undefined),
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
