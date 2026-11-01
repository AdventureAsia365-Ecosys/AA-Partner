// web/lib/bffClient.ts
// AA-936 — browser calls to the BFF (/bff/<path> on the page's own origin, lib/bff.ts allow-list). A 401 means the
// access token expired: the page reloads through /auth/refresh (which renews it or signs in again).
// AA-1014 — a file download (bffDownload) or a fetch that must keep the page's state (bffFetch) first renews the token
// in the background (/auth/refresh answers a 307 with the new session cookie; redirect "manual" keeps the browser on
// the page but still stores the cookie) and tries once more; only a second 401 reloads through /auth/refresh.

export type BffResult<T> = { ok: true; data: T } | { ok: false; status: number; detail: string; body?: unknown };

function refreshUrl(): string {
  return `/auth/refresh?next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
}

export async function bffSend<T>(method: "GET" | "POST" | "PATCH" | "PUT" | "DELETE", path: string, body?: unknown): Promise<BffResult<T>> {
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
    window.location.assign(refreshUrl());
    return { ok: false, status: 401, detail: "session_expired" };
  }
  const json = (await res.json().catch(() => null)) as unknown;
  if (!res.ok) return failure(res.status, json);
  return { ok: true, data: json as T };
}

function failure(status: number, json: unknown): { ok: false; status: number; detail: string; body?: unknown } {
  const detail = typeof (json as { detail?: unknown })?.detail === "string" ? (json as { detail: string }).detail : "error";
  return { ok: false, status, detail, body: json };
}

// One renewal at a time: two downloads clicked together share it (the refresh token may rotate on each use).
let renewing: Promise<void> | null = null;

export function renewSession(): Promise<void> {
  if (!renewing) {
    renewing = fetch(refreshUrl(), { credentials: "same-origin", redirect: "manual", cache: "no-store" })
      .then(() => undefined)
      .catch(() => undefined) // the retry that follows decides
      .finally(() => {
        renewing = null;
      });
  }
  return renewing;
}

// A GET (or another call) to /bff/<path> that survives an access token expiring while the page was open: on a 401 it
// renews in the background and tries once more; a second 401 reloads through /auth/refresh (sign in again) and the
// 401 response is returned. A network failure throws, like fetch.
export async function bffFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const send = () => fetch(`/bff/${path}`, { ...init, credentials: "same-origin" });
  const first = await send();
  if (first.status !== 401 || typeof window === "undefined") return first;
  await renewSession();
  const second = await send();
  if (second.status === 401) window.location.assign(refreshUrl());
  return second;
}

// Download /bff/<path> and hand it to the browser as `filename` (a blob link, so the BFF's cookie auth applies).
export async function bffDownload(path: string, opts: { filename: string }): Promise<BffResult<null>> {
  let res: Response;
  let blob: Blob;
  try {
    res = await bffFetch(path);
    if (!res.ok) return failure(res.status, await res.json().catch(() => null));
    blob = await res.blob();
  } catch {
    return { ok: false, status: 0, detail: "network" };
  }
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = opts.filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  return { ok: true, data: null };
}
