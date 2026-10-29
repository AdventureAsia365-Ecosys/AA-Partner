// portal/lib/facts.ts
// AA-814 — Brand › Facts: the pure rules of the page (no React, no imports; `npm test` runs lib/facts.test.ts). The
// shapes are CIS /v1/facts (api/routers/v1_facts.py): the agency's own Facts Entries (also expired ones, flagged) and
// the Adventure Asia facts that are still valid, each with `overridden_by` when an own fact replaces it. The writer
// keeps an own fact over an Adventure Asia fact on the same subject: the same kind, subject (title) and country,
// compared without case or extra spaces (services/acp_shared/facts_store.subject_key).

export const FACT_KINDS = ["price", "season", "visa_entry", "transfer_time", "booking_terms", "other"] as const;
export type FactKind = (typeof FACT_KINDS)[number];

export const KIND_LABEL: Record<FactKind, string> = {
  price: "Price",
  season: "Season",
  visa_entry: "Visa & entry",
  transfer_time: "Transfer time",
  booking_terms: "Booking terms",
  other: "Other",
};

// Field limits (CIS services/acp_shared/facts_store.py).
export const MAX_TITLE = 200;
export const MAX_BODY = 1200;
export const MAX_SOURCE = 500;
export const MAX_COUNTRY = 80;

export interface TenantFact {
  fact_id: string;
  scope: "tenant" | "platform";
  kind: FactKind;
  title: string;
  body: string;
  provenance: string;
  country: string | null;
  stated_on: string | null;
  valid_until: string | null;
  created_at: string | null;
  updated_at: string | null;
  expired: boolean;
  overridden_by?: string | null;
}

export interface TenantFactList {
  own: TenantFact[];
  platform: TenantFact[];
  kinds: FactKind[];
  max_own: number;
}

export const FACTS_API = "/api/tenant/v1/facts";

/** The editable fields as the form holds them (strings; "" = no value). */
export interface FactForm {
  kind: FactKind;
  title: string;
  body: string;
  provenance: string;
  country: string;
  stated_on: string;
  valid_until: string;
}

export const EMPTY_FORM: FactForm = {
  kind: "other", title: "", body: "", provenance: "", country: "", stated_on: "", valid_until: "",
};

export function formOf(f: TenantFact): FactForm {
  return {
    kind: f.kind, title: f.title, body: f.body, provenance: f.provenance, country: f.country ?? "",
    stated_on: f.stated_on ?? "", valid_until: f.valid_until ?? "",
  };
}

/** "Use my own" on an Adventure Asia fact: the same subject (kind, title, country), the agency writes the rest. */
export function overrideForm(platform: TenantFact): FactForm {
  return { ...EMPTY_FORM, kind: platform.kind, title: platform.title, country: platform.country ?? "" };
}

const clean = (s: string) => s.replace(/\s+/g, " ").trim();
const orNull = (s: string) => (clean(s) ? clean(s) : null);

export function createBody(form: FactForm): Record<string, string | null> {
  return {
    kind: form.kind, title: clean(form.title), body: clean(form.body), provenance: clean(form.provenance),
    country: orNull(form.country), stated_on: form.stated_on || null, valid_until: form.valid_until || null,
  };
}

/** The fields that differ from the loaded fact (a PATCH sends only these, plus expected_updated_at). */
export function changedFields(fact: TenantFact, form: FactForm): Record<string, string | null> {
  const next = createBody(form);
  const before = createBody(formOf(fact));
  const out: Record<string, string | null> = {};
  for (const k of Object.keys(next)) if (next[k] !== before[k]) out[k] = next[k];
  return out;
}

export type FormProblem = "required" | "dates" | null;

export function formProblem(form: FactForm): FormProblem {
  if (!clean(form.title) || !clean(form.body) || !clean(form.provenance)) return "required";
  if (form.stated_on && form.valid_until && form.valid_until < form.stated_on) return "dates";
  return null;
}

export const PROBLEM_TEXT: Record<"required" | "dates", string> = {
  required: "Subject, fact and source are required.",
  dates: "Valid until cannot be before the stated-on date.",
};

/** The subject key the writer compares (kind, title, country; case and spacing ignored). */
export function subjectKey(f: { kind: string; title: string; country?: string | null }): string {
  return [f.kind, clean(f.title).toLowerCase(), clean(f.country ?? "").toLowerCase()].join("|");
}

/** The own fact that already covers this subject, if any (to warn before a duplicate). */
export function ownOnSubject(own: TenantFact[], form: FactForm, exceptId?: string): TenantFact | undefined {
  const key = subjectKey(form);
  return own.find((f) => f.fact_id !== exceptId && subjectKey(f) === key);
}

/** A readable line for an API refusal: the role refusal, a stale edit, a validation list, or the API's own text. */
export function errorText(status: number, detail: unknown): string {
  if (status === 403) return "Your role can read facts but not change them. Ask the workspace owner.";
  if (status === 409) return "This fact changed since you opened it. Close it and open it again.";
  if (status === 404) return "This fact no longer exists. Refresh the list.";
  if (typeof detail === "string" && detail) return detail.charAt(0).toUpperCase() + detail.slice(1);
  if (Array.isArray(detail)) {
    const parts = detail.map((d) => {
      const loc = Array.isArray(d?.loc) ? d.loc.filter((x: unknown) => x !== "body").join(".") : "";
      return loc ? `${loc}: ${d?.msg ?? ""}` : String(d?.msg ?? "");
    }).filter(Boolean);
    if (parts.length) return parts.join("; ");
  }
  return status ? `Request failed (${status})` : "Network error";
}

export type CallResult<T> = { ok: true; body: T } | { ok: false; status: number; message: string };

export async function factCall<T>(method: string, path: string, body?: unknown): Promise<CallResult<T>> {
  let res: Response;
  try {
    res = await fetch(`${FACTS_API}${path}`, {
      method,
      headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    return { ok: false, status: 0, message: errorText(0, null) };
  }
  let json: unknown = null;
  try { json = await res.json(); } catch { /* empty body */ }
  if (!res.ok) return { ok: false, status: res.status, message: errorText(res.status, (json as { detail?: unknown } | null)?.detail) };
  return { ok: true, body: json as T };
}
