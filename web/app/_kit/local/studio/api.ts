// web/app/_kit/local/studio/api.ts (was portal/app/_kit/api.ts, a copy of AA-CIS-App frontend/app/_kit/api.ts)
// AA-994 S2a: the CIS file stays CIS-local under root ADR 0016 decision 1, so this copy is portal-owned from now on.
// AA-662 — small fetch helpers used with react-query across UI-v2 pages.
//
// All browser calls go through the BFF proxy (/api/admin/* or /api/tenant/*), never to the backend
// directly. These helpers add JSON handling + the admin reviewer-id header the existing pages send,
// and throw a readable Error on non-2xx so react-query's `error` is meaningful.

export class ApiError extends Error {
  status: number;
  /** The API's own sentence (`detail` / `message` of the body); "" when it sent none. AA-1036: the studio shows it
   *  as it is, and otherwise its own "Request failed (status)" in the UI language (studioError.ts plainError). */
  detail: string;
  /** The API's stable error code, when it sent one (CIS: `detail.detail` of a structured refusal such as
   *  {"detail": {"detail": "tenant_not_in_service", "message": …}}, or an X-Error-Code header when a proxy forwards
   *  it; the tenant BFF streams only the content type); "" otherwise. */
  code: string;
  constructor(message: string, status: number, detail = "", code = "") {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.code = code;
  }
}

/** The stable error code of a refusal: the X-Error-Code header, else a structured `detail` object's own `detail` /
 *  `code` (a short snake_case word only, never a sentence). */
export function errorCodeOf(header: string | null, body: unknown): string {
  const ok = (v: unknown): v is string => typeof v === "string" && /^[a-z][a-z0-9_]{0,63}$/.test(v);
  if (ok(header)) return header;
  const d = body && typeof body === "object" ? (body as { detail?: unknown }).detail : undefined;
  if (d && typeof d === "object") {
    const inner = d as { detail?: unknown; code?: unknown };
    if (ok(inner.detail)) return inner.detail;
    if (ok(inner.code)) return inner.code;
  }
  return "";
}

/** Admin identity header (temporary until AA-232 per-user auth): x-reviewer-id from localStorage. */
export function adminHeaders(json = false): Record<string, string> {
  const h: Record<string, string> = {};
  const rid =
    (typeof window !== "undefined" && window.localStorage.getItem("cis_reviewer_id")) || "";
  if (rid) h["x-reviewer-id"] = rid;
  if (json) h["Content-Type"] = "application/json";
  return h;
}

async function parseOrThrow<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = "";
    let body: unknown = null;
    try {
      body = await res.json();
      const d = (body as { detail?: unknown; message?: unknown } | null)?.detail
        || (body as { message?: unknown } | null)?.message;
      detail = typeof d === "string" ? d : "";
    } catch {
      /* non-JSON error body */
    }
    const code = errorCodeOf(res.headers?.get?.("x-error-code") ?? null, body);
    throw new ApiError(detail || `HTTP ${res.status}`, res.status, detail, code);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

/** GET JSON from a BFF path. `admin` toggles the reviewer-id header. */
export async function apiGet<T>(path: string, opts: { admin?: boolean } = {}): Promise<T> {
  const res = await fetch(path, {
    headers: opts.admin ? adminHeaders() : undefined,
  });
  return parseOrThrow<T>(res);
}

/** Send a JSON body (POST/PUT/PATCH/DELETE) to a BFF path. */
export async function apiSend<T>(
  path: string,
  opts: { method?: string; body?: unknown; admin?: boolean } = {},
): Promise<T> {
  const { method = "POST", body, admin = false } = opts;
  const res = await fetch(path, {
    method,
    headers: admin ? adminHeaders(true) : { "Content-Type": "application/json" },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  return parseOrThrow<T>(res);
}
