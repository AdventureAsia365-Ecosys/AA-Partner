// web/lib/clients.ts
// AA-937 (CRM-09) — the client directory (A-06), the client profile (A-07) and the trip's travellers and documents:
// the types the CRM API returns (/crm/v1/travelers, /travelers/{id}, /trips/{id}/documents) and the pure helpers the
// pages use. Lists carry display names only (one list_view audit event per load, design v3 §4.4); every other F1
// field is masked ("••••") until a member presses Reveal and picks a reason (one reveal event per field). No partial
// digits are ever formatted here. Edge-safe, unit-tested.

import { fmt, type Lang, type MessageKey, t } from "./i18n";
import type { Stage, WaitingOn } from "./pipeline";

export const REVEAL_FIELDS = [
  "given_name",
  "family_name",
  "email",
  "phone",
  "dob",
  "nationality",
  "passport_no",
  "passport_expiry",
  "passport_country",
  "emergency_contact",
  "health",
] as const;
export type RevealField = (typeof REVEAL_FIELDS)[number];

export const REVEAL_REASONS = [
  "contact_client",
  "prepare_booking",
  "verify_identity",
  "client_request",
  "fill_forms",
  "ops_handoff",
  "other",
] as const;
export type RevealReason = (typeof REVEAL_REASONS)[number];

export const RELATION_KINDS = ["spouse", "partner", "parent", "child", "sibling", "friend", "referrer", "referred"] as const;
export type RelationKind = (typeof RELATION_KINDS)[number];

export const DOC_KINDS = ["passport_scan", "waiver_pdf", "terms_pdf", "ticket", "voucher", "invoice_pdf", "supplier_confirmation", "other"] as const;
export type DocKind = (typeof DOC_KINDS)[number];
export const DOC_TYPES = ["application/pdf", "image/jpeg", "image/png", "image/webp", "image/heic"] as const;
export const DOC_MAX_BYTES = 7 * 1024 * 1024;

export const PROFILE_CHOICES = {
  budget_band: ["under_1000", "1000_2000", "2000_3500", "3500_5000", "over_5000", "not_sure"],
  pace: ["relaxed", "balanced", "adventure"],
  group_pref: ["private", "small_group"],
  fitness_band: ["up_to_2h", "2_4h", "4_6h", "over_6h"],
  trek_experience: ["none", "some", "experienced"],
  accommodation_tier: ["budget", "comfort", "premium", "luxury"],
  occasion: ["none", "honeymoon", "anniversary", "birthday", "family_reunion", "graduation", "retirement", "other"],
  budget_flex: ["fixed", "some", "flexible"],
  decision_maker: ["self", "partner", "family", "group", "company"],
  booking_timeline: ["ready_now", "within_month", "within_3_months", "later", "just_looking"],
} as const;
export type ProfileChoice = keyof typeof PROFILE_CHOICES;
export const DIETARY = ["vegetarian", "vegan", "halal", "kosher", "gluten_free", "lactose_free", "nut_allergy", "shellfish_allergy", "other_allergy"] as const;
export const MOBILITY = ["limited_walking", "wheelchair", "no_stairs", "no_long_drives", "other"] as const;
export const MEDICAL = ["asthma", "diabetes", "heart_condition", "epilepsy", "pregnancy", "severe_allergy", "other_medical"] as const;
export const AGE_BANDS = ["under_2", "2_11", "12_17", "18_24", "25_34", "35_44", "45_54", "55_64", "65_74", "75_plus"] as const;

export interface ClientRow {
  traveler_id: string;
  display_name: string;
  owner_member_id: string | null;
  is_mine: boolean;
  lead_source: string | null;
  archived: boolean;
  has_email: boolean;
  has_phone: boolean;
  trips: number;
  created_at: string;
}

export interface Directory {
  items: ClientRow[];
  page: number;
  has_more: boolean;
  filter: { q: string | null; search: "none" | "email" | "phone" | "name"; archived: boolean; owner: "anyone" | "me" };
}

export interface ClientProfile {
  traveler: {
    traveler_id: string;
    display_name: string;
    preferred_channel: string | null;
    owner_member_id: string | null;
    is_mine: boolean;
    lead_source: string | null;
    referrer_traveler_id: string | null;
    archived: boolean;
    archive_reason: string | null;
    created_at: string;
    filled: Record<Exclude<RevealField, "health">, boolean>;
    has_health: boolean;
  };
  profile: (Partial<Record<ProfileChoice, string | null>> & {
    residence_country: string | null;
    altitude_ok: boolean | null;
    competing_quotes: boolean | null;
    interests: string[];
    must_do_codes: string[];
    must_avoid_codes: string[];
    dietary_flags: string[];
    mobility_flags: string[];
    repeat_count: number;
    stage2_completeness: number;
    marketing_ok: boolean;
  }) | null;
  consent: Record<string, { value: boolean; at: string; source: string }>;
  relations: { relation_id: string; kind: RelationKind; traveler_id: string; display_name: string; trips_together: number }[];
  trips: { trip_id: string; title: string; stage: Stage; waiting_on: WaitingOn; month: string | null; start_date: string | null; role: "lead" | "companion"; age_band_at_departure: string | null }[];
  documents: DocumentRow[];
}

export interface DocumentRow {
  document_id: string;
  trip_id: string | null;
  traveler_id: string | null;
  kind: DocKind;
  filename: string;
  size_bytes: number;
  content_type: string;
  visible_to_travelers: boolean;
  write_only: boolean;
  created_at: string;
}

/** The masked form of a field that holds a value: never a digit or a letter of it (design §4.4, Fix B15). */
export const MASK = "••••••";

export function maskedValue(filled: boolean): string {
  return filled ? MASK : "—";
}

/** A revealed value for display: health codes become their labels, everything else stays as typed. */
export function revealText(lang: Lang, field: RevealField, value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (field === "health" && typeof value === "object") {
    const v = value as { dietary?: string[]; mobility?: string[]; medical?: string[] };
    const codes = [...(v.dietary ?? []).map((c) => label(lang, "dietary", c)), ...(v.mobility ?? []).map((c) => label(lang, "mobility", c)), ...(v.medical ?? []).map((c) => label(lang, "medical", c))];
    return codes.length ? codes.join(", ") : t(lang, "clients.noHealth");
  }
  return String(value);
}

/** "<prefix>.<code>" label, or the code itself when no label exists (codes from the API are never free text). */
export function label(lang: Lang, prefix: string, code: string | null | undefined): string {
  if (!code) return "—";
  const text = t(lang, `${prefix}.${code}` as MessageKey);
  return text ?? code;
}

/** Directory URL for a filter (archived, mine, page) and whether the saved search applies: links, so the list stays a
 * server render. The search term itself never goes into a URL (Vercel / CDN request logs, browser history): the form
 * POSTs it to /clients/search, which keeps it in a short-lived sealed cookie and links here with `s=1`. */
export function directoryHref(f: { search?: boolean; archived?: boolean; owner?: "anyone" | "me"; page?: number }): string {
  const q = new URLSearchParams();
  if (f.search) q.set("s", "1");
  if (f.archived) q.set("archived", "true");
  if (f.owner === "me") q.set("owner", "me");
  if (f.page && f.page > 1) q.set("page", String(f.page));
  const s = q.toString();
  return s ? `/clients?${s}` : "/clients";
}

export function parseDirectory(params: Record<string, string | string[] | undefined>): { search: boolean; archived: boolean; owner: "anyone" | "me"; page: number } {
  const page = Number(typeof params.page === "string" ? params.page : "1");
  return {
    search: params.s === "1",
    archived: params.archived === "true",
    owner: params.owner === "me" ? "me" : "anyone",
    page: Number.isInteger(page) && page >= 1 && page <= 200 ? page : 1,
  };
}

/** The longest search term the API accepts (POST /crm/v1/travelers/search). */
export const SEARCH_MAX = 120;

/** What a search box value will be matched as (mirrors the API: exact only, R35). */
export function searchKind(q: string): "none" | "email" | "phone" | "name" {
  const v = q.trim();
  if (!v) return "none";
  if (v.includes("@")) return "email";
  if (/^\+?[\d\s-]+$/.test(v)) return "phone";
  return "name";
}

/** "Lily is Anna's child": the relation sentence of a row of A-07 (the row reads "<other> is <me>'s <kind>"). */
export function relationLabel(lang: Lang, kind: RelationKind): string {
  return t(lang, `relation.${kind}` as MessageKey);
}

/** "3 Apr 2027 · 35–44" for a trip row (age band at departure, R32). */
export function bandLabel(lang: Lang, band: string | null): string {
  if (!band) return "—";
  return t(lang, `age.${band}` as MessageKey) ?? band;
}

export function consentLabel(lang: Lang, consent: ClientProfile["consent"], purpose: "contact" | "marketing" | "health_data"): string {
  const c = consent[purpose];
  if (!c) return t(lang, "clients.consentUnknown");
  return c.value ? t(lang, "clients.consentYes") : t(lang, "clients.consentNo");
}

export function fileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Base64 of a file's bytes (the BFF forwards JSON only; the API decodes and checks the magic number). */
export function toBase64(bytes: Uint8Array): string {
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
  return btoa(binary);
}

export function fromBase64(b64: string): Uint8Array {
  const binary = atob(b64);
  const out = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) out[i] = binary.charCodeAt(i);
  return out;
}

/** Checks the browser can do before sending a file (the API checks again). */
export function fileProblem(file: { size: number; type: string }): "too_big" | "type" | null {
  if (file.size > DOC_MAX_BYTES) return "too_big";
  if (!(DOC_TYPES as readonly string[]).includes(file.type)) return "type";
  return null;
}

/** A trip of the profile: "Mar 2027" / "12 Mar 2027" are formatted by the page; this gives the role text. */
export function roleText(lang: Lang, role: "lead" | "companion"): string {
  return t(lang, role === "lead" ? "trip.lead" : "trip.companion");
}

export function tripsCount(lang: Lang, n: number): string {
  return fmt(t(lang, "clients.trips"), { n });
}
