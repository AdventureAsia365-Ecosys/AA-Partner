// /bff/<path> — the browser's only way to the CRM API (AA-935, card CRM-07 "BFF allow-list"). Order:
//   0. a Host other than the partner host -> 404 (AA-945, the proxy's check repeated, see below);
//   1. a cross-site write -> 403 (lib/crossSiteGuard.ts, before any cookie or body is read);
//   2. a path segment that could escape the prefix -> 400 (lib/bffPath.ts);
//   3. not on the allow-list (lib/bff.ts) -> 404;
//   4. no session or an expired access token -> 401 session_expired (the page reloads through /auth/refresh);
//   5. forwarded to /crm/v1/<path> with the session's token and agency; only Content-Type comes back
//      (lib/proxyResponse.ts), never a Set-Cookie of the API.
// AA-945: a raw route (the mail history upload) is read as bytes, capped while it streams in (413 before the API is
// called), and refused if fewer bytes arrived than Content-Length said or the upload broke off (400, never a cut-short
// file to the API). At most MAX_RAW_IN_FLIGHT raw uploads run at once per web task (503 `upload_busy` before the body
// is read). /bff/mail/imports* is outside the proxy matcher (proxy.ts, so Next does not clone the body), so the
// proxy's host check is repeated here, for every /bff path (an encoded or look-alike path is covered too).
import { randomUUID } from "node:crypto";
import type { NextRequest } from "next/server";
import { matchBff, MAX_RAW_IN_FLIGHT, upstreamHeaders, type BffRoute } from "@/lib/bff";
import { readCapped } from "@/lib/bffBody";
import { proxyPath } from "@/lib/bffPath";
import { requestConfig } from "@/lib/config";
import { classifyHost } from "@/lib/hosts";
import { streamUpstream } from "@/lib/proxyResponse";
import { json } from "@/lib/respond";
import { accessValid, readSession } from "@/lib/session";
import { refuseForeignWrite } from "@/lib/writeGuard";

export const dynamic = "force-dynamic";

const UPSTREAM_TIMEOUT_MS = 15_000; // a 7 MiB document upload (AA-937) fits comfortably
const MAX_BODY_BYTES = 256 * 1024;
let rawInFlight = 0; // raw uploads being read or forwarded by this process (lib/bff.ts MAX_RAW_IN_FLIGHT)

async function handle(request: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  if (classifyHost(request.headers.get("host"), requestConfig(request)) !== "partner") return json({ detail: "not_found" }, 404);
  const refused = refuseForeignWrite(requestConfig(request), request);
  if (refused) return refused;
  const { path } = await ctx.params;
  const sub = proxyPath(path ?? []);
  if (sub === null) return json({ detail: "invalid_path" }, 400);
  const route = matchBff(request.method, sub);
  if (!route) return json({ detail: "not_found" }, 404);

  const cfg = requestConfig(request);
  const session = cfg.authConfigured ? readSession(cfg, request.cookies) : null;
  if (!accessValid(session)) return json({ detail: "session_expired" }, 401);
  if (route.tenant && !session.tid) return json({ detail: "no_agency_selected" }, 409);

  const limit = route.maxBody ?? MAX_BODY_BYTES;
  if (!route.raw) {
    let body: string | undefined;
    if (request.method !== "GET" && request.method !== "DELETE") {
      body = await request.text();
      if (Buffer.byteLength(body) > limit) return json({ detail: "body_too_large" }, 413);
    }
    return forward(request, cfg.crmApiOrigin, sub, route, session, body);
  }

  const declared = Number(request.headers.get("content-length") ?? "");
  if (Number.isFinite(declared) && declared > limit) return json({ detail: "body_too_large" }, 413);
  if (rawInFlight >= MAX_RAW_IN_FLIGHT) {
    const busy = json({ detail: "upload_busy" }, 503);
    busy.headers.set("Retry-After", "10");
    return busy;
  }
  rawInFlight += 1;
  try {
    const blob = await readCapped(request.body, limit);
    if (blob === "too_large") return json({ detail: "body_too_large" }, 413);
    if (blob === "aborted") return json({ detail: "body_incomplete" }, 400);
    if (Number.isFinite(declared) && declared > 0 && blob.size !== declared) return json({ detail: "body_incomplete" }, 400);
    return await forward(request, cfg.crmApiOrigin, sub, route, session, blob);
  } finally {
    rawInFlight -= 1;
  }
}

async function forward(
  request: NextRequest,
  apiOrigin: string,
  sub: string,
  route: BffRoute,
  session: { at: string; tid?: string | null },
  body: string | Blob | undefined,
) {
  const headers = upstreamHeaders(
    session.at,
    route.tenant ? (session.tid ?? null) : null,
    request.headers.get("content-type"),
    randomUUID(),
    route.raw === true,
  );
  const query = request.nextUrl.search;
  try {
    const upstream = await fetch(`${apiOrigin}/crm/v1/${sub}${query}`, {
      method: request.method,
      headers,
      body,
      cache: "no-store",
      redirect: "manual",
      signal: AbortSignal.timeout(route.timeoutMs ?? UPSTREAM_TIMEOUT_MS),
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
