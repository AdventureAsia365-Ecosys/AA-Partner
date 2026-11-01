// web/lib/bffClient.ts
// AA-936 — browser calls to the BFF (/bff/<path> on the page's own origin, lib/bff.ts allow-list). A 401 means the
// access token expired: the page reloads through /auth/refresh (which renews it or signs in again).
// AA-1014 — a file download (bffDownload) or a fetch that must keep the page's state (bffFetch, bffSend with
// `renew`) first renews the token in the background (/auth/refresh answers a 307 with the new session cookie; redirect
// "manual" keeps the browser on the page but still stores the cookie) and tries once more; only a second 401 reloads
// through /auth/refresh. A result with `redirecting: true` means that reload has started: the caller shows no error.

export type BffResult<T> =
  | { ok: true; data: T }
  | { ok: false; status: number; detail: string; body?: unknown; redirecting?: true };

function refreshUrl(): string {
  return `/auth/refresh?next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
}

const SESSION_EXPIRED = { ok: false, status: 401, detail: "session_expired", redirecting: true } as const;

/**
 * A JSON call to /bff/<path>. On a 401 the page reloads through /auth/refresh. With `renew: true` it first renews the
 * token in the background and sends the same request once more (bffFetch); the body is always a JSON string, so it can
 * be sent twice. Use `renew` for a call whose page state must survive (a 401 from the BFF or the API means nothing was
 * applied, so the second send is not a duplicate).
 */
export async function bffSend<T>(
  method: "GET" | "POST" | "PATCH" | "PUT" | "DELETE",
  path: string,
  body?: unknown,
  opts: { renew?: boolean } = {},
): Promise<BffResult<T>> {
  const init: RequestInit = {
    method,
    headers: body === undefined ? { Accept: "application/json" } : { Accept: "application/json", "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  };
  let res: Response;
  try {
    res = opts.renew ? await bffFetch(path, init) : await fetch(`/bff/${path}`, { ...init, credentials: "same-origin" });
  } catch {
    return { ok: false, status: 0, detail: "network" };
  }
  if (res.status === 401 && typeof window !== "undefined") {
    // bffFetch already started the reload on its second 401.
    if (!opts.renew) window.location.assign(refreshUrl());
    return SESSION_EXPIRED;
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

/**
 * A call to /bff/<path> that survives an access token expiring while the page was open: on a 401 it renews in the
 * background and sends the same request once more; a second 401 reloads through /auth/refresh (sign in again) and
 * that 401 response is returned, so in the browser a 401 from bffFetch always means the reload has started.
 * A network failure throws, like fetch.
 *
 * The request is sent twice, so `init.body` must be re-sendable: a string (JSON), URLSearchParams, a Blob or nothing.
 * A one-shot body (a ReadableStream) is consumed by the first send and cannot be retried; callers pass strings/JSON
 * only (the raw file uploads in lib/onboarding.ts and lib/mailImport.ts use XMLHttpRequest and keep the reload).
 */
export async function bffFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const send = () => fetch(`/bff/${path}`, { ...init, credentials: "same-origin" });
  const first = await send();
  if (first.status !== 401 || typeof window === "undefined") return first;
  await renewSession();
  const second = await send();
  if (second.status === 401) window.location.assign(refreshUrl());
  return second;
}

/**
 * Download /bff/<path> and hand it to the browser as `filename` (a blob link, so the BFF's cookie auth applies).
 * Never throws: a failed fetch is `network`, a refusal carries the API's `detail`, a browser that cannot save the blob
 * is `save_failed`, and a second 401 is `redirecting` (the page is on its way to sign in again).
 */
export async function bffDownload(path: string, opts: { filename: string }): Promise<BffResult<null>> {
  let res: Response;
  let blob: Blob;
  try {
    res = await bffFetch(path);
    if (res.status === 401 && typeof window !== "undefined") return SESSION_EXPIRED;
    if (!res.ok) return failure(res.status, await res.json().catch(() => null));
    blob = await res.blob();
  } catch {
    return { ok: false, status: 0, detail: "network" };
  }
  try {
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = opts.filename;
    document.body.appendChild(a);
    try {
      a.click();
    } finally {
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
  } catch {
    return { ok: false, status: 0, detail: "save_failed" };
  }
  return { ok: true, data: null };
}
