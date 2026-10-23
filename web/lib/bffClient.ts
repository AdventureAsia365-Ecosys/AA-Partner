// web/lib/bffClient.ts
// AA-936 — browser calls to the BFF (/bff/<path> on the page's own origin, lib/bff.ts allow-list). A 401 means the
// access token expired: the page reloads through /auth/refresh (which renews it or signs in again).

export type BffResult<T> = { ok: true; data: T } | { ok: false; status: number; detail: string };

export async function bffSend<T>(method: "GET" | "POST" | "PATCH", path: string, body?: unknown): Promise<BffResult<T>> {
  let res: Response;
  try {
    res = await fetch(`/bff/${path}`, {
      method,
      headers: body === undefined ? { Accept: "application/json" } : { Accept: "application/json", "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "same-origin",
    });
  } catch {
    return { ok: false, status: 0, detail: "network" };
  }
  if (res.status === 401 && typeof window !== "undefined") {
    window.location.assign(`/auth/refresh?next=${encodeURIComponent(window.location.pathname + window.location.search)}`);
    return { ok: false, status: 401, detail: "session_expired" };
  }
  const json = (await res.json().catch(() => null)) as unknown;
  if (!res.ok) {
    const detail = typeof (json as { detail?: unknown })?.detail === "string" ? (json as { detail: string }).detail : "error";
    return { ok: false, status: res.status, detail };
  }
  return { ok: true, data: json as T };
}
