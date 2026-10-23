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

const memo = new Map<string, { at: number; value: FormLookup }>();

export function formKey(pathname: string): string | null {
  return FORM_PATH.exec(pathname)?.[1] ?? null;
}

export async function loadForm(
  apiOrigin: string,
  key: string,
  opts: { fetchImpl?: typeof fetch; now?: number } = {},
): Promise<FormLookup> {
  const now = opts.now ?? Date.now();
  const hit = memo.get(key);
  if (hit && now - hit.at < MEMO_TTL_MS) return hit.value;
  let value: FormLookup;
  try {
    const res = await (opts.fetchImpl ?? fetch)(`${apiOrigin}/crm/public/v1/forms/${encodeURIComponent(key)}`, {
      headers: { Accept: "application/json" },
      cache: "no-store",
      signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
    });
    value = res.ok ? { ok: true, form: (await res.json()) as PublicForm } : { ok: false, status: res.status };
  } catch {
    return { ok: false, status: 0 }; // not memoised: the next view tries again
  }
  if (value.ok || value.status === 404) {
    if (memo.size >= MEMO_MAX) memo.delete(memo.keys().next().value as string);
    memo.set(key, { at: now, value });
  }
  return value;
}

/** Tests only. */
export function clearFormMemo(): void {
  memo.clear();
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
