// web/lib/academyClient.ts
// AA-1105 — browser calls of the academy pages (client components): /academy/bff/<path> on the academy origin
// (lib/academyBff.ts allow-list). A 401 means the academy access token expired: the page goes through
// /academy/auth/refresh (which renews it or signs in again). Every call carries ACADEMY_CALL_HEADER: the academy BFF
// refuses a call without it.

import type { Problem } from "./academy";
import { ACADEMY_CALL_HEADER } from "./academyBff";

export type AcademyResult<T> = { ok: true; status: number; data: T } | { ok: false; status: number; problem: Problem };

function headersFor(withBody: boolean): Record<string, string> {
  const h: Record<string, string> = { Accept: "application/json", [ACADEMY_CALL_HEADER]: "1" };
  if (withBody) h["Content-Type"] = "application/json";
  return h;
}

function text(json: Record<string, unknown> | null, key: string): string | null {
  const v = json ? json[key] : null;
  return typeof v === "string" ? v : null;
}

/** A refused call: the learning API's Problem body ({error, message, retry_at}) or the BFF's own {detail}. */
export function problemOf(json: Record<string, unknown> | null): Problem {
  return { error: text(json, "error") ?? text(json, "detail") ?? "error", message: text(json, "message") ?? "", retry_at: text(json, "retry_at") };
}

function goRefresh(): void {
  window.location.assign(`/academy/auth/refresh?next=${encodeURIComponent(window.location.pathname + window.location.search)}`);
}

export async function academySend<T>(method: "GET" | "POST", path: string, opts: { body?: unknown; query?: URLSearchParams } = {}): Promise<AcademyResult<T>> {
  const qs = opts.query?.toString() ? `?${opts.query.toString()}` : "";
  let res: Response;
  try {
    res = await fetch(`/academy/bff/${path}${qs}`, {
      method,
      headers: headersFor(opts.body !== undefined),
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
      credentials: "same-origin",
    });
  } catch {
    return { ok: false, status: 0, problem: { error: "unavailable", message: "" } };
  }
  if (res.status === 401 && typeof window !== "undefined") {
    goRefresh();
    return { ok: false, status: 401, problem: { error: "unauthorized", message: "" } };
  }
  const json = (await res.json().catch(() => null)) as Record<string, unknown> | null;
  return res.ok ? { ok: true, status: res.status, data: json as T } : { ok: false, status: res.status, problem: problemOf(json) };
}
