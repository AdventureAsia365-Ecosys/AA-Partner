// web/lib/staffApi.ts
// AA-1019 — server-side calls of the staff console's pages to /crm/staff/v1. Only (method, path) pairs on the staff
// allow-list (lib/staffBff.ts) can be called, with the STAFF session's token, and only when the session holds one of the
// route's groups; the API checks the token class and the group again.

import { randomUUID } from "node:crypto";
import type { WebConfig } from "./config";
import type { ApiResult } from "./crmApi";
import { allowedQuery, groupAllowed, matchStaffBff, STAFF_API_PREFIX, staffUpstreamHeaders, type StaffMethod } from "./staffBff";
import type { StaffSession } from "./staffSession";

const API_TIMEOUT_MS = 10_000;

export async function staffCall<T>(
  cfg: WebConfig,
  session: StaffSession,
  method: StaffMethod,
  subPath: string,
  opts: { body?: unknown; query?: URLSearchParams; fetchImpl?: typeof fetch } = {},
): Promise<ApiResult<T>> {
  const route = matchStaffBff(method, subPath);
  if (!route) throw new Error(`not on the staff allow-list: ${method} ${subPath}`);
  if (!groupAllowed(route, session.groups)) return { ok: false, status: 403, detail: "missing_group" };
  const qs = opts.query ? allowedQuery(subPath, opts.query).toString() : "";
  let res: Response;
  try {
    res = await (opts.fetchImpl ?? fetch)(`${cfg.crmApiOrigin}${STAFF_API_PREFIX}/${subPath}${qs ? `?${qs}` : ""}`, {
      method,
      headers: staffUpstreamHeaders(session.at, randomUUID(), opts.body !== undefined),
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
      cache: "no-store",
      signal: AbortSignal.timeout(API_TIMEOUT_MS),
    });
  } catch {
    return { ok: false, status: 0, detail: "api_unreachable" };
  }
  let json: unknown = null;
  try {
    json = await res.json();
  } catch {
    json = null;
  }
  if (!res.ok) {
    const detail = (json as { detail?: unknown })?.detail;
    return { ok: false, status: res.status, detail: typeof detail === "string" ? detail : "error" };
  }
  return { ok: true, status: res.status, data: json as T };
}
