// web/lib/staffConsole.ts
// AA-1019 — the types /crm/staff/v1 answers (AA-956 S-02, AA-971 S-04) and the pure helpers of the staff console's
// pages: board rows, the deletion countdown, the manifest columns and file, ticket and break-glass input checks, the
// session banner, refusal codes. Edge-safe (no Node API), unit-tested (tests/staffConsole.test.ts).

import type { StaffGroup } from "./staffSession";

// ── S-02 ops releases ────────────────────────────────────────────────────────────────────────────────────────────
export const MISSING_ITEMS = ["stage3", "waiver", "arrival_flight", "departure_flight"] as const;
export type MissingItem = (typeof MISSING_ITEMS)[number];

export interface Completeness {
  travellers: number;
  complete: number;
  missing: Partial<Record<MissingItem, number>>;
}

export interface BoardItem {
  tenant_id: string;
  agency_name: string | null;
  agency_status: "active" | "locked";
  trip_id: string;
  trip_ref: string;
  status: "live" | "withdrawn";
  withdrawn_at: string | null;
  withdraw_reason: "trip_cancelled" | "aa_lines_cancelled" | "agency_withdrawn" | null;
  start_date: string | null;
  end_date: string | null;
  traveller_count: number;
  complete: boolean;
  completeness: Completeness;
  departure_ids: string[];
  payload_version: number;
  refreshed_at: string | null;
  t21_alerted: boolean;
  purge_at: string | null;
  days_to_purge: number | null;
  view_count: number;
  last_viewed_at: string | null;
}

export interface Board {
  items: BoardItem[];
  horizon_days: number;
  incident_admin_url: string | null;
}

export interface Flight {
  flight_no: string | null;
  airport: string | null;
  sched_local: string | null;
  tz: string | null;
}

export interface ReleaseTraveller {
  traveler_id: string;
  role: string;
  name: string | null;
  given_name: string | null;
  family_name: string | null;
  age_band: string | null;
  dietary: string[];
  mobility: string[];
  arrival: Flight | null;
  departure: Flight | null;
  insurance: string | null;
}

export interface ReleaseDocument {
  v: number;
  trip: {
    trip_ref: string;
    start_date: string | null;
    end_date: string | null;
    adults: number;
    children: number;
    agency_name: string | null;
    agency_timezone: string;
  };
  lines: {
    booking_line_id: string;
    title: string | null;
    tour_id: string | null;
    departure_id: string | null;
    bk_booking_id: string | null;
    service_from: string | null;
    service_to: string | null;
  }[];
  travellers: ReleaseTraveller[];
}

export interface Release {
  tenant_id: string;
  trip_id: string;
  ops_release_id: string;
  payload_version: number;
  fields_released: string[];
  completeness: Completeness;
  complete: boolean;
  departure_ids: string[];
  bk_booking_ids: string[];
  released_at: string | null;
  refreshed_at: string | null;
  purge_at: string | null;
  days_to_purge: number | null;
  view_count: number;
  incident_admin_url: string | null;
  document: ReleaseDocument;
}

export interface Manifest {
  filename: string;
  watermark: string;
  row_count: number;
  fields: string[];
  csv: string;
}

/** The manifest columns a supplier may receive (API MANIFEST_FIELDS, same order) and the API's default pick. */
export const MANIFEST_FIELDS = ["name", "age_band", "dietary", "mobility", "arrival_flight", "departure_flight"] as const;
export type ManifestField = (typeof MANIFEST_FIELDS)[number];
export const MANIFEST_DEFAULT: readonly ManifestField[] = ["name", "age_band", "dietary", "mobility"];

/** The board windows offered (API: 1..120 days, default 60). */
export const BOARD_WINDOWS = [14, 30, 60, 120] as const;
export const BOARD_DEFAULT_DAYS = 60;

export function boardDays(raw: string | string[] | undefined): number {
  const value = Number(Array.isArray(raw) ? raw[0] : raw);
  return (BOARD_WINDOWS as readonly number[]).includes(value) ? value : BOARD_DEFAULT_DAYS;
}

/** The missing items with a count above zero, in a fixed order. */
export function missingItems(c: Completeness | null | undefined): { item: MissingItem; n: number }[] {
  if (!c) return [];
  return MISSING_ITEMS.map((item) => ({ item, n: c.missing?.[item] ?? 0 })).filter((m) => m.n > 0);
}

/** What the "Deleted in" cell says: in n days, today, or no return date yet. */
export function purgeLabel(days: number | null | undefined): { key: "staff.ops.purgeIn" | "staff.ops.purgeToday" | "staff.ops.purgeUnknown"; n: number } {
  if (days === null || days === undefined) return { key: "staff.ops.purgeUnknown", n: 0 };
  if (days <= 0) return { key: "staff.ops.purgeToday", n: 0 };
  return { key: "staff.ops.purgeIn", n: days };
}

/** The tone of the countdown: under a week is a warning (the ground team should finish with it). */
export function purgeTone(days: number | null | undefined): "warning" | "neutral" {
  return days !== null && days !== undefined && days <= 7 ? "warning" : "neutral";
}

/** The console path of one release (agency and trip ids are UUIDs from the API). */
export function releaseHref(tenantId: string, tripId: string): string {
  return `/staff/ops/${encodeURIComponent(tenantId)}/${encodeURIComponent(tripId)}`;
}

/** Only known columns, in the API's order (unknown ones dropped, never sent). */
export function pickFields(picked: readonly string[]): ManifestField[] {
  return MANIFEST_FIELDS.filter((f) => picked.includes(f));
}

export function manifestQuery(fields: readonly ManifestField[]): URLSearchParams {
  const q = new URLSearchParams();
  for (const f of fields) q.append("fields", f);
  return q;
}

/** A safe download file name (the API's, else a plain fallback): letters, digits, dot, dash, underscore only. */
export function manifestFilename(m: Pick<Manifest, "filename" | "watermark">): string {
  const clean = (m.filename || "").replace(/[^A-Za-z0-9._-]/g, "");
  return clean.endsWith(".csv") && clean.length > 4 ? clean : `manifest-${(m.watermark || "export").replace(/[^A-Za-z0-9-]/g, "")}.csv`;
}

/** "VN123 SGN 2026-04-03T10:20 (Asia/Ho_Chi_Minh)" or "". */
export function flightText(f: Flight | null | undefined): string {
  if (!f) return "";
  const head = [f.flight_no, f.airport, f.sched_local?.replace("T", " ")].filter(Boolean).join(" ");
  return f.tz ? `${head} (${f.tz})` : head;
}

export function travellerName(p: Pick<ReleaseTraveller, "name" | "given_name" | "family_name">): string {
  const full = [p.given_name, p.family_name].filter(Boolean).join(" ");
  return full || p.name || "";
}

// ── S-04 support and break-glass ─────────────────────────────────────────────────────────────────────────────────
/** The ticket pattern of the API and crm-033 ("SUP-1042", "INC-7"). */
export const TICKET_PATTERN = /^[A-Za-z][A-Za-z0-9]{0,15}-[0-9]{1,10}$/;

export function ticketValid(value: string): boolean {
  return TICKET_PATTERN.test(value.trim());
}

/** The break-glass reason codes (API BreakglassReason, crm-033 CHECK). */
export const BREAKGLASS_REASONS = [
  "security_incident",
  "data_corruption",
  "legal_request",
  "owner_unreachable",
  "service_outage",
] as const;
export type BreakglassReason = (typeof BREAKGLASS_REASONS)[number];
export const BREAKGLASS_MINUTES_MIN = 15;
export const BREAKGLASS_MINUTES_MAX = 60;

export function minutesValid(value: number): boolean {
  return Number.isInteger(value) && value >= BREAKGLASS_MINUTES_MIN && value <= BREAKGLASS_MINUTES_MAX;
}

export interface GrantedAgency {
  tenant_id: string;
  agency_name: string;
  expires_at: string;
  show_identity: boolean;
  in_use: boolean;
  mine: boolean;
}

export interface BreakglassAgency {
  tenant_id: string;
  agency_name: string | null;
  agency_status: "active" | "locked";
  my_session_expires_at: string | null;
}

export type SessionKind = "support" | "breakglass";

export interface SessionView {
  tenant_id: string;
  session_id: string;
  kind: SessionKind;
  status: "active" | "ended" | "revoked" | "expired";
  ticket_ref: string;
  reason_code: string | null;
  show_identity: boolean;
  started_at: string;
  expires_at: string;
  owners_notified?: number | null;
}

export interface MySessions {
  tenant_id: string;
  agency_name: string | null;
  agency_status: string | null;
  sessions: SessionView[];
}

export interface SessionRef {
  kind: SessionKind;
  session_id: string;
  show_identity: boolean;
  expires_at: string;
}

export interface Overview {
  tenant_id: string;
  agency_name: string;
  status: string;
  default_locale: string;
  settings: Record<string, unknown> | null;
  pipeline: Record<string, number>;
  session: SessionRef;
}

export interface StaffTrip {
  trip_id: string;
  stage: string;
  stage_entered_at: string | null;
  waiting_on: string;
  start_date: string | null;
  end_date: string | null;
  month: string | null;
  nights: number | null;
  destinations: string[];
  party_adults: number;
  party_children: number;
  party_type: string | null;
  last_activity_at: string | null;
}

/** The trips page size of the agency view (API max 200, default 50). */
export const TRIPS_PAGE = 25;

export interface TripsPage {
  items: StaffTrip[];
  next_cursor: string | null;
  session: SessionRef;
}

export interface TripDetail extends StaffTrip {
  stage_history: {
    history_id: string;
    from_stage: string | null;
    to_stage: string;
    reason_code: string;
    at: string | null;
    by: string;
  }[];
  party: { traveler_id: string; role: string; display_name: string }[] | null;
  identity_hidden: boolean;
  session: SessionRef;
}

/** The live session among the caller's own (newest first from the API), or null. */
export function liveSession(sessions: readonly SessionView[]): SessionView | null {
  return sessions.find((s) => s.status === "active") ?? null;
}

/** The refusal codes the console words itself; anything else is the generic error. */
export const REFUSALS = [
  "no_support_session",
  "support_session_ended",
  "support_access_expired",
  "tenant_locked",
  "no_support_grant",
  "support_session_in_use",
  "breakglass_in_use",
  "e2e_synthetic_only",
  "missing_group",
] as const;

export function refusalKey(detail: string | null | undefined): `staff.refused.${(typeof REFUSALS)[number]}` | "staff.error" {
  return (REFUSALS as readonly string[]).includes(detail ?? "")
    ? (`staff.refused.${detail}` as `staff.refused.${(typeof REFUSALS)[number]}`)
    : "staff.error";
}

/** The console's first page for these groups: ops for crm-ops, else support. */
export function homeFor(groups: readonly StaffGroup[]): "/staff/ops" | "/staff/support" {
  return groups.includes("crm-ops") ? "/staff/ops" : "/staff/support";
}

/** An agency id from a URL segment: a lower-case UUID, else null (the page answers not found). */
export function uuidParam(raw: string): string | null {
  const v = (raw || "").toLowerCase();
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(v) ? v : null;
}
