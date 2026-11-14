// web/lib/academyClient.ts
// AA-1105 — browser calls of the academy pages (client components): /academy/bff/<path> on the academy origin
// (lib/academyBff.ts allow-list). A 401 means the academy access token expired: the page goes through
// /academy/auth/refresh (which renews it or signs in again). Every call carries ACADEMY_CALL_HEADER: the academy BFF
// refuses a call without it.

import type { Problem } from "./academy";
import { ACADEMY_CALL_HEADER } from "./academyBff";

export type AcademyResult<T> = { ok: true; status: number; data: T } | { ok: false; status: number; problem: Problem };

export async function academySend<T>(method: "GET" | "POST", path: string, opts: { body?: unknown; query?: URLSearchParams } = {}): Promise<AcademyResult<T>> {
  const qs = opts.query && opts.query.toString() ? `?${opts.query.toString()}` : "";
  let res: Response;
  try {
    res = await fetch(`/academy/bff/${path}${qs}`, {
      method,
      headers:
        opts.body === undefined
          ? { Accept: "application/json", [ACADEMY_CALL_HEADER]: "1" }
          : { Accept: "application/json", "Content-Type": "application/json", [ACADEMY_CALL_HEADER]: "1" },
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
      credentials: "same-origin",
    });
  } catch {
    return { ok: false, status: 0, problem: { error: "unavailable", message: "" } };
  }
  if (res.status === 401 && typeof window !== "undefined") {
    window.location.assign(`/academy/auth/refresh?next=${encodeURIComponent(window.location.pathname + window.location.search)}`);
    return { ok: false, status: 401, problem: { error: "unauthorized", message: "" } };
  }
  const json = (await res.json().catch(() => null)) as Record<string, unknown> | null;
  if (!res.ok) {
    // The learning API's Problem body ({error, message, retry_at}) or the BFF's own {detail}.
    const error = typeof json?.error === "string" ? json.error : typeof json?.detail === "string" ? json.detail : "error";
    const retryAt = typeof json?.retry_at === "string" ? json.retry_at : null;
    return { ok: false, status: res.status, problem: { error, message: typeof json?.message === "string" ? json.message : "", retry_at: retryAt } };
  }
  return { ok: true, status: res.status, data: json as T };
}
