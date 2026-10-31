// web/lib/staffClient.ts
// AA-1019 — browser calls of the staff console (client components): /staff/bff/<path> on the page's own origin
// (lib/staffBff.ts allow-list). A 401 means the staff access token expired: the page goes through /staff/auth/refresh
// (which renews it or signs in again). Never /bff (the agency BFF) and never the agency session.

export type StaffResult<T> = { ok: true; data: T } | { ok: false; status: number; detail: string };

export async function staffSend<T>(method: "GET" | "POST", path: string, opts: { body?: unknown; query?: URLSearchParams } = {}): Promise<StaffResult<T>> {
  const qs = opts.query && opts.query.toString() ? `?${opts.query.toString()}` : "";
  let res: Response;
  try {
    res = await fetch(`/staff/bff/${path}${qs}`, {
      method,
      headers: opts.body === undefined ? { Accept: "application/json" } : { Accept: "application/json", "Content-Type": "application/json" },
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
      credentials: "same-origin",
    });
  } catch {
    return { ok: false, status: 0, detail: "network" };
  }
  if (res.status === 401 && typeof window !== "undefined") {
    window.location.assign(`/staff/auth/refresh?next=${encodeURIComponent(window.location.pathname + window.location.search)}`);
    return { ok: false, status: 401, detail: "session_expired" };
  }
  const json = (await res.json().catch(() => null)) as unknown;
  if (!res.ok) {
    const detail = (json as { detail?: unknown })?.detail;
    return { ok: false, status: res.status, detail: typeof detail === "string" ? detail : "error" };
  }
  return { ok: true, data: json as T };
}
