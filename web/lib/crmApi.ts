// web/lib/crmApi.ts
// AA-935 — server-side calls to the CRM API (/crm/v1) for server components and route handlers. Only (method, path)
// pairs on the BFF allow-list (lib/bff.ts) can be called, with the session's access token and agency; the API checks
// the token class and the membership (tenant_tx(), ADR 0006b decision 6).

import { randomUUID } from "node:crypto";
import { matchBff, upstreamHeaders, type Method } from "./bff";
import type { WebConfig } from "./config";
import type { DateLocale } from "./dates";
import type { Role } from "./nav";
import type { Session } from "./session";

const API_TIMEOUT_MS = 10_000;

export type ApiResult<T> = { ok: true; status: number; data: T } | { ok: false; status: number; detail: string };

export interface Membership {
  tenant_id: string;
  member_id: string;
  member_role: Role;
  display_name: string;
  default_locale: DateLocale;
  tenant_status: "active" | "locked";
}

export interface Me {
  tenant_id: string;
  member_id: string;
  member_role: Role;
  display_name: string | null;
  default_locale: DateLocale | null;
}

export async function crmCall<T>(
  cfg: WebConfig,
  session: Session,
  method: Method,
  subPath: string,
  opts: { body?: unknown; fetchImpl?: typeof fetch; requestId?: string } = {},
): Promise<ApiResult<T>> {
  const route = matchBff(method, subPath);
  if (!route) throw new Error(`not on the BFF allow-list: ${method} ${subPath}`);
  if (route.tenant && !session.tid) return { ok: false, status: 409, detail: "no_agency_selected" };
  const headers = upstreamHeaders(
    session.at,
    route.tenant ? session.tid : null,
    opts.body === undefined ? null : "application/json",
    opts.requestId ?? randomUUID(),
  );
  let res: Response;
  try {
    res = await (opts.fetchImpl ?? fetch)(`${cfg.crmApiOrigin}/crm/v1/${subPath}`, {
      method,
      headers,
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
    const detail = typeof (json as { detail?: unknown })?.detail === "string" ? (json as { detail: string }).detail : "error";
    return { ok: false, status: res.status, detail };
  }
  return { ok: true, status: res.status, data: json as T };
}

export async function fetchMemberships(cfg: WebConfig, session: Session, fetchImpl?: typeof fetch) {
  const r = await crmCall<{ memberships: Membership[] }>(cfg, session, "GET", "memberships", { fetchImpl });
  return r.ok ? ({ ok: true, status: r.status, data: r.data.memberships } as const) : r;
}

export function fetchMe(cfg: WebConfig, session: Session, fetchImpl?: typeof fetch) {
  return crmCall<Me>(cfg, session, "GET", "me", { fetchImpl });
}

/** The membership a session may switch to: one of the caller's own, in an active agency. */
export function selectable(memberships: Membership[], tenantId: string): Membership | null {
  return memberships.find((m) => m.tenant_id === tenantId && m.tenant_status === "active") ?? null;
}
