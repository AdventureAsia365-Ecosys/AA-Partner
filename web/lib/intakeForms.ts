// web/lib/intakeForms.ts
// AA-936 — the public inquiry form (C-01) on the client host: its URL, the form definition the API serves
// (GET /crm/public/v1/forms/<key>, no token), a short per-process memo (the page and the proxy's CSP read the same
// answer), the CSP frame-ancestors from the form's allowed origins, and the embed snippet for Settings > Forms.
// The browser posts the submission to /crm/public/v1/... on its own origin (the client host's ALB rule sends that
// path to the API), so the API sees the viewer address CloudFront set and rate-limits per visitor.

export const FORM_PATH = /^\/c\/f\/([A-Za-z0-9_-]{20,64})\/?$/;
export const MEMO_TTL_MS = 60_000;
const MEMO_MAX = 500;
const FETCH_TIMEOUT_MS = 5_000;

export interface FormField {
  name: string;
  type: string;
  required: boolean;
  options?: string[];
  min?: number;
  max?: number;
  max_items?: number;
  max_length?: number;
  collapsed?: boolean;
  note?: string;
}

export interface PublicForm {
  schema_key: string;
  version: number;
  fields: FormField[];
  honeypot_field: string;
  agency_name: string;
  default_locale: string;
  allowed_origins: string[];
  privacy_notice_url: string | null;
}

export type FormLookup = { ok: true; form: PublicForm } | { ok: false; status: number };

// Found forms and missing keys are memoised apart, so a flood of random keys (each one a 404) cannot evict the real
// forms. A found form is kept past its TTL as a fallback: when a refresh fails (API down, or the API's public limit
// answers 429 for the web's NAT address) the page and its CSP keep the last good definition instead of 'error' and
// frame-ancestors 'self' (which would break every embed).
const memo = new Map<string, { at: number; value: FormLookup & { ok: true } }>();
const missing = new Map<string, number>();
const MISSING_MAX = 2_000;

// Per-viewer budget for memo misses (each miss is one API call from the web's single NAT address, which the API's
// public limit counts as one caller): random /c/f/<key> requests from one viewer stop here instead of using up the
// API's allowance for every agency's form page. Fixed one-minute window, like the API's limiter.
export const MISS_LIMIT_PER_MINUTE = 20;
const MISS_WINDOW_MS = 60_000;
const MISS_MAX_VIEWERS = 10_000;
const misses = new Map<string, number>();
let missWindowStart = 0;

function missAllowed(viewer: string, now: number): boolean {
  if (now - missWindowStart >= MISS_WINDOW_MS) {
    missWindowStart = now;
    misses.clear();
  }
  const count = (misses.get(viewer) ?? 0) + 1;
  misses.delete(viewer);
  misses.set(viewer, count); // re-inserted last: the most recently seen viewer
  while (misses.size > MISS_MAX_VIEWERS) misses.delete(misses.keys().next().value as string);
  return count <= MISS_LIMIT_PER_MINUTE;
}

/** The viewer key from CloudFront-Viewer-Address (`<ip>:<port>`, IPv6 without brackets; CloudFront sets it and a
 * viewer cannot): the IPv4 address or the /64 of an IPv6 address. "local" without the header (local runs, tests). */
export function viewerKey(viewerAddress: string | null | undefined): string {
  const value = (viewerAddress ?? "").trim();
  const cut = value.lastIndexOf(":");
  if (!value || cut < 0) return "local";
  const host = value.slice(0, cut);
  if (!host.includes(":")) return host;
  const [head, tail = ""] = host.toLowerCase().split("::");
  const left = head ? head.split(":") : [];
  const right = tail ? tail.split(":") : [];
  const groups = host.includes("::") ? [...left, ...Array(Math.max(0, 8 - left.length - right.length)).fill("0"), ...right] : left;
  return `${groups.slice(0, 4).map((g) => g.replace(/^0+(?=.)/, "")).join(":")}::/64`;
}

export function formKey(pathname: string): string | null {
  return FORM_PATH.exec(pathname)?.[1] ?? null;
}

export async function loadForm(
  apiOrigin: string,
  key: string,
  opts: { fetchImpl?: typeof fetch; now?: number; viewer?: string } = {},
): Promise<FormLookup> {
  const now = opts.now ?? Date.now();
  const hit = memo.get(key);
  if (hit && now - hit.at < MEMO_TTL_MS) return hit.value;
  const gone = missing.get(key);
  if (gone !== undefined && now - gone < MEMO_TTL_MS) return { ok: false, status: 404 };
  if (opts.viewer !== undefined && !missAllowed(opts.viewer, now)) return hit ? hit.value : { ok: false, status: 429 };
  let value: FormLookup;
  try {
    const res = await (opts.fetchImpl ?? fetch)(`${apiOrigin}/crm/public/v1/forms/${encodeURIComponent(key)}`, {
      headers: { Accept: "application/json" },
      cache: "no-store",
      signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
    });
    value = res.ok ? { ok: true, form: (await res.json()) as PublicForm } : { ok: false, status: res.status };
  } catch {
    return hit ? hit.value : { ok: false, status: 0 }; // not memoised: the next view tries again
  }
  if (value.ok) {
    memo.delete(key);
    if (memo.size >= MEMO_MAX) memo.delete(memo.keys().next().value as string);
    memo.set(key, { at: now, value });
    missing.delete(key);
  } else if (value.status === 404) {
    memo.delete(key); // the form was disabled or deleted
    if (missing.size >= MISSING_MAX) missing.delete(missing.keys().next().value as string);
    missing.set(key, now);
  } else if (hit) {
    return hit.value; // 429 / 5xx: keep serving the last good definition
  }
  return value;
}

/** Tests only. */
export function clearFormMemo(): void {
  memo.clear();
  missing.clear();
  misses.clear();
  missWindowStart = 0;
}

/** frame-ancestors for a form page: the client host itself and the agency's own websites (https only). */
export function frameAncestors(form: PublicForm | null): string {
  const origins = (form?.allowed_origins ?? []).filter((o) => /^https:\/\/[a-z0-9.-]+(:\d{1,5})?$/.test(o));
  return ["'self'", ...origins].join(" ");
}

/** The form's public URL on the first client host, or null when no client host is configured. */
export function hostedUrl(clientHosts: string[], key: string): string | null {
  const host = clientHosts[0];
  return host ? `https://${host}/c/f/${key}` : null;
}

export function embedSnippet(url: string, title: string): string {
  const safeTitle = title.replace(/["<>&]/g, "");
  return `<iframe src="${url}" title="${safeTitle}" style="width:100%;min-height:960px;border:0" loading="lazy"></iframe>`;
}

/** Children's ages typed as "7, 12" -> [7, 12]; null when a part is not a whole number 0–17. */
export function parseAges(text: string): number[] | null {
  const parts = text.split(/[,;\s]+/).filter(Boolean);
  const ages = parts.map((p) => (/^\d{1,2}$/.test(p) ? Number(p) : NaN));
  return ages.every((a) => Number.isInteger(a) && a >= 0 && a <= 17) ? ages : null;
}
