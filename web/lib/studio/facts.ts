// web/lib/studio/facts.ts
// AA-814 — Brand › Facts: the pure rules of the page (no React, no imports; `npm test` runs lib/facts.test.ts). The
// shapes are CIS /v1/facts (api/routers/v1_facts.py): the agency's own Facts Entries (also expired ones, flagged) and
// the Adventure Asia facts that are still valid, each with `overridden_by` when an own fact replaces it. The writer
// keeps an own fact over an Adventure Asia fact on the same subject: the same kind, subject (title) and country,
// compared without case or extra spaces (services/acp_shared/facts_store.subject_key). AA-1036 (R3): the words take
// the UI language (lib.messages.ts), English by default.

import { translator, type Lang } from "@/app/_kit/core/i18n";
import { FACTS_LIB_MESSAGES } from "./lib.messages";

export const FACT_KINDS = ["price", "season", "visa_entry", "transfer_time", "booking_terms", "other"] as const;
export type FactKind = (typeof FACT_KINDS)[number];

// AA-1144 (S285 decision B6): an agency's own facts are its markup (price) and its sale terms only; tour facts
// (seasons, visas and entry, transfer times, anything else) are Adventure Asia's. The form offers these kinds, and
// "Use my own" only on an Adventure Asia fact of these kinds. An older own fact of another kind still shows (and can
// be deleted); the CIS facts API itself is a follow-up (another repo).
export const AGENCY_FACT_KINDS = ["price", "booking_terms"] as const satisfies readonly FactKind[];

/** Whether the agency may write a fact of this kind (a new one, or its own version of an Adventure Asia fact). */
export function agencyMayWrite(kind: unknown): boolean {
  return typeof kind === "string" && (AGENCY_FACT_KINDS as readonly string[]).includes(kind);
}

/** Why the tenant proxy refuses a facts write before it reaches CIS (status + plain words for the member). */
export interface FactWriteRefusal {
  status: 400 | 422;
  detail: string;
}

export const FACT_KIND_REFUSAL =
  "An agency's own facts are its price and its booking terms only. Seasons, visas and entry, transfer times and " +
  "other tour facts come from Adventure Asia.";

const NOT_JSON: FactWriteRefusal = { status: 400, detail: "A fact is sent as JSON." };

/** "create" (POST v1/facts), "edit" (PATCH v1/facts/{id}) or null (any other route or method). */
function factWriteOp(method: string, segments: readonly string[]): "create" | "edit" | null {
  if (segments[0] !== "v1" || segments[1] !== "facts") return null;
  const m = method.toUpperCase();
  if (m === "POST" && segments.length === 2) return "create";
  return m === "PATCH" && segments.length === 3 ? "edit" : null;
}

/** The JSON object a facts write sends, or null when the proxy cannot read one (CIS reads JSON only). */
function factJsonObject(contentType: string, body: ArrayBuffer | string | undefined): Record<string, unknown> | null {
  if (contentType.includes("multipart/form-data") || typeof body !== "string") return null;
  try {
    const parsed: unknown = JSON.parse(body);
    return parsed !== null && typeof parsed === "object" && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

/**
 * AA-1144 (B6) in the tenant proxy (app/api/tenant/[...path]/route.ts): the CIS facts API takes every kind, so the
 * proxy refuses a new own fact (POST v1/facts) whose kind is not the agency's (a missing kind would be "other" in
 * CIS), and an edit (PATCH v1/facts/{id}) that sets such a kind. An edit that leaves the kind alone (an older fact of
 * another kind) and a delete pass. A body the proxy cannot read is refused too: what passes is what the check saw.
 */
export function factWriteRefusal(
  method: string, segments: readonly string[], contentType: string, body: ArrayBuffer | string | undefined,
): FactWriteRefusal | null {
  const op = factWriteOp(method, segments);
  if (!op) return null;
  const fields = factJsonObject(contentType, body);
  if (!fields) return NOT_JSON;
  const setsKind = op === "create" || Object.hasOwn(fields, "kind");
  return setsKind && !agencyMayWrite(fields.kind) ? { status: 422, detail: FACT_KIND_REFUSAL } : null;
}

/** The kinds the form offers: the agency's two, plus the fact's own kind when an older fact of another kind is edited. */
export function formKinds(current: FactKind): readonly FactKind[] {
  return agencyMayWrite(current) ? AGENCY_FACT_KINDS : [...AGENCY_FACT_KINDS, current];
}

const KIND_KEY = {
  price: "kindPrice",
  season: "kindSeason",
  visa_entry: "kindVisa",
  transfer_time: "kindTransfer",
  booking_terms: "kindBooking",
  other: "kindOther",
} as const satisfies Record<FactKind, keyof typeof FACTS_LIB_MESSAGES.en>;

export const KIND_LABEL: Record<FactKind, string> = Object.fromEntries(
  Object.entries(KIND_KEY).map(([k, key]) => [k, FACTS_LIB_MESSAGES.en[key]]),
) as Record<FactKind, string>;

/** A fact kind in words, in the UI language (an unknown kind passes through). */
export function kindLabel(kind: string, lang: Lang = "en"): string {
  return Object.hasOwn(KIND_KEY, kind) ? translator(FACTS_LIB_MESSAGES, lang)(KIND_KEY[kind as FactKind]) : kind;
}

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
  kind: "price", title: "", body: "", provenance: "", country: "", stated_on: "", valid_until: "",
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
  required: FACTS_LIB_MESSAGES.en.required,
  dates: FACTS_LIB_MESSAGES.en.dates,
};

/** A form problem in words, in the UI language. */
export function problemText(p: "required" | "dates", lang: Lang = "en"): string {
  return translator(FACTS_LIB_MESSAGES, lang)(p);
}

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
export function errorText(status: number, detail: unknown, lang: Lang = "en"): string {
  const t = translator(FACTS_LIB_MESSAGES, lang);
  if (status === 403) return t("forbidden");
  if (status === 409) return t("conflict");
  if (status === 404) return t("gone");
  if (typeof detail === "string" && detail) return detail.charAt(0).toUpperCase() + detail.slice(1);
  if (Array.isArray(detail)) {
    const parts = detail.map((d) => {
      const loc = Array.isArray(d?.loc) ? d.loc.filter((x: unknown) => x !== "body").join(".") : "";
      return loc ? `${loc}: ${d?.msg ?? ""}` : String(d?.msg ?? "");
    }).filter(Boolean);
    if (parts.length) return parts.join("; ");
  }
  return status ? t("failed", status) : t("network");
}

export type CallResult<T> = { ok: true; body: T } | { ok: false; status: number; message: string };

export async function factCall<T>(method: string, path: string, body?: unknown, lang: Lang = "en"): Promise<CallResult<T>> {
  let res: Response;
  try {
    res = await fetch(`${FACTS_API}${path}`, {
      method,
      headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    return { ok: false, status: 0, message: errorText(0, null, lang) };
  }
  let json: unknown = null;
  try { json = await res.json(); } catch { /* empty body */ }
  if (!res.ok) return { ok: false, status: res.status, message: errorText(res.status, (json as { detail?: unknown } | null)?.detail, lang) };
  return { ok: true, body: json as T };
}
