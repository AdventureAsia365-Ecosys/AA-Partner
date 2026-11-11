// web/lib/travelOps.ts
// AA-1070 (crm-054 / crm-055) — the types the travel ops API returns (departure roster, passport check, rooming list,
// vouchers, server-side PDFs, visa tracker, Settings > Travel) and the pure helpers the pages share. Edge-safe,
// unit-tested (tests/travelOps.test.ts). No passport number, expiry or nationality ever reaches the browser.

export const PASSPORT_STATUSES = ["ok", "expires_too_soon", "missing", "no_trip_end"] as const;
export type PassportStatus = (typeof PASSPORT_STATUSES)[number];
export const ROOM_TYPES = ["single", "double", "twin", "triple", "quad", "family"] as const;
export type RoomType = (typeof ROOM_TYPES)[number];
export const CAPACITY: Record<RoomType, number> = { single: 1, double: 2, twin: 2, triple: 3, quad: 4, family: 6 };
export const BED_PREFS = ["double_bed", "twin_beds", "extra_bed", "cot"] as const;
export type BedPref = (typeof BED_PREFS)[number];
export const VISA_TYPES = ["evisa", "visa_on_arrival", "embassy", "visa_exempt", "transit", "other"] as const;
export type VisaType = (typeof VISA_TYPES)[number];
export const VISA_STATUSES = ["to_do", "collecting", "submitted", "approved", "refused", "not_needed", "cancelled"] as const;
export type VisaStatus = (typeof VISA_STATUSES)[number];
/** The moves crm-055 allows (the API and the database check them again). */
export const VISA_MOVES: Record<VisaStatus, VisaStatus[]> = {
  to_do: ["collecting", "submitted", "not_needed", "cancelled"],
  collecting: ["to_do", "submitted", "not_needed", "cancelled"],
  submitted: ["approved", "refused", "collecting", "cancelled"],
  refused: ["collecting", "cancelled"],
  not_needed: ["to_do"],
  approved: ["cancelled"],
  cancelled: [],
};
export const PDF_KINDS = ["confirmation", "invoice", "proposal", "voucher"] as const;
export type PdfKind = (typeof PDF_KINDS)[number];
export const DEPARTURE_STATES = ["scheduled", "guaranteed", "cancelled"] as const;
export type DepartureState = (typeof DEPARTURE_STATES)[number];
export const MANIFEST_FIELDS = ["name", "trip_ref", "age_band", "room", "passport_check", "dietary", "mobility", "arrival_flight", "departure_flight"] as const;
export type ManifestField = (typeof MANIFEST_FIELDS)[number];
export const MANIFEST_DEFAULT: readonly ManifestField[] = ["name", "trip_ref", "age_band", "room"];

export interface PassportCheck {
  trip_id: string;
  valid_months: number;
  valid_until_needed: string | null;
  issues: number;
  travellers: { traveler_id: string; role: string; status: PassportStatus }[];
}

export interface RoomTraveller {
  traveler_id: string;
  name: string;
  role: string;
  bed_pref?: BedPref | null;
}

export interface Room {
  room_no: number;
  room_type: RoomType;
  travellers: RoomTraveller[];
}

export interface RoomingView {
  trip_id: string;
  rooms: Room[];
  unassigned: { traveler_id: string; name: string; role: string }[];
  room_config: Record<string, number> | null;
  can_edit?: boolean;
}

export interface Voucher {
  booking_line_id: string;
  voucher_no: string;
  title: string;
  service_from: string | null;
  service_to: string | null;
  confirmation_no: string | null;
  supplier: { name: string; category: string; contact_name: string | null; aa_booking_ref: string | null; aa_departure_id: string | null };
  pickup: string | null;
  dropoff: string | null;
  service_time: string | null;
  pnr: string | null;
  note: string | null;
  released: boolean;
  not_released_reason: "trip_not_booked" | "line_hidden" | null;
}

export interface VouchersView {
  trip_id: string;
  agency_name: string | null;
  trip_title: string;
  start_date: string | null;
  end_date: string | null;
  lead_traveller: string | null;
  pax: number;
  vouchers: Voucher[];
}

export interface ChecklistItem {
  visa_checklist_item_id: string;
  label: string;
  done: boolean;
  done_by_kind: "member" | "traveler" | null;
}

export interface VisaApplication {
  visa_application_id: string;
  trip_id: string;
  traveler_id: string;
  traveler_name: string | null;
  visa_template_id: string | null;
  country: string;
  visa_type: VisaType;
  status: VisaStatus;
  deadline: string | null;
  submitted_on: string | null;
  decided_on: string | null;
  visa_no: string | null;
  valid_from: string | null;
  valid_until: string | null;
  checklist: ChecklistItem[];
  checklist_done: number;
}

export interface VisasView {
  trip_id: string;
  can_add: boolean;
  applications: VisaApplication[];
}

export interface VisaTemplate {
  visa_template_id: string;
  country: string;
  visa_type: VisaType;
  processing_days: number | null;
  fee: string | null;
  fee_currency: string | null;
  documents: string[];
  official_url: string | null;
  active: boolean;
}

export interface VisaSuggestion {
  traveler_id: string;
  traveler_name: string;
  country: string;
  nationality_known: boolean;
  template: VisaTemplate | null;
}

export interface Pax {
  adults: number;
  children: number;
  infants: number;
}

export interface DepartureStateView {
  status: DepartureState;
  guaranteed_at: string | null;
  cancelled_at: string | null;
  min_to_operate: number | null;
}

export interface DepartureRow {
  departure_id: string;
  title: string;
  service_from: string | null;
  service_to: string | null;
  state: DepartureStateView;
  trips: number;
  pax: Pax;
  stage3_complete: number;
}

export interface RosterView {
  departure_id: string;
  title: string;
  service_from: string | null;
  service_to: string | null;
  state: DepartureStateView;
  pax: Pax;
  stage3_complete: number;
  trips: {
    trip_id: string;
    title: string;
    stage: string;
    start_date: string | null;
    end_date: string | null;
    pax: Pax;
    s3: number;
    travellers: number;
    rooms_placed: number;
    passport_issues: number;
  }[];
  travellers: {
    trip_id: string;
    traveler_id: string;
    name: string;
    role: string;
    details_done: boolean;
    passport: PassportStatus;
    room: { room_no: number; room_type: RoomType; bed_pref: BedPref | null } | null;
  }[];
  manifest_fields: ManifestField[];
  manifest_default: ManifestField[];
}

export const DEPARTURE_REF = /^[A-Za-z0-9:_.-]{1,80}$/;

export function paxTotal(p: Pax): number {
  return p.adults + p.children + p.infants;
}

/** Badge tone of a passport status (the kit Badge tones). */
export function passportTone(status: PassportStatus | null | undefined): "success" | "danger" | "warning" | "neutral" {
  if (status === "ok") return "success";
  if (status === "expires_too_soon") return "danger";
  if (status === "missing") return "warning";
  return "neutral";
}

export function departureTone(state: DepartureState): "success" | "danger" | "neutral" {
  return state === "guaranteed" ? "success" : state === "cancelled" ? "danger" : "neutral";
}

export function visaTone(status: VisaStatus): "success" | "danger" | "info" | "warning" | "neutral" {
  if (status === "approved" || status === "not_needed") return "success";
  if (status === "refused") return "danger";
  if (status === "submitted") return "info";
  if (status === "cancelled") return "neutral";
  return "warning";
}

/** An open application past its deadline (to do / collecting): the task the hourly tick opens. */
export function visaOverdue(app: Pick<VisaApplication, "status" | "deadline">, today: string): boolean {
  return (app.status === "to_do" || app.status === "collecting") && !!app.deadline && app.deadline < today;
}

/** Manifest columns with health codes: picking one always needs a reason (the API checks again). */
export const MANIFEST_HEALTH_FIELDS: readonly ManifestField[] = ["dietary", "mobility"];
/** Bulk reveals of more travellers than this need a reason (the duty reveal's threshold, card R). */
export const MANIFEST_REASON_THRESHOLD = 5;

/** Whether a manifest of ``travellers`` with ``fields`` needs the member to pick a reason. */
export function manifestNeedsReason(travellers: number, fields: readonly ManifestField[]): boolean {
  return travellers > MANIFEST_REASON_THRESHOLD || fields.some((f) => MANIFEST_HEALTH_FIELDS.includes(f));
}

/** A draft room list -> the PUT body, or the first problem (the API checks again). */
export function roomingBody(
  rooms: { room_no: number; room_type: RoomType; travellers: { traveler_id: string; bed_pref?: BedPref | null }[] }[],
): { ok: true; body: { rooms: unknown[] } } | { ok: false; error: "room_over_capacity" | "traveler_twice" | "room_no_twice" } {
  const seen = new Set<string>();
  const numbers = new Set<number>();
  const out: unknown[] = [];
  for (const room of rooms) {
    const people = room.travellers.filter((p) => p.traveler_id);
    if (!people.length) continue;
    if (numbers.has(room.room_no)) return { ok: false, error: "room_no_twice" };
    numbers.add(room.room_no);
    if (people.length > CAPACITY[room.room_type]) return { ok: false, error: "room_over_capacity" };
    for (const p of people) {
      if (seen.has(p.traveler_id)) return { ok: false, error: "traveler_twice" };
      seen.add(p.traveler_id);
    }
    out.push({ room_no: room.room_no, room_type: room.room_type, travellers: people.map((p) => ({ traveler_id: p.traveler_id, bed_pref: p.bed_pref ?? null })) });
  }
  return { ok: true, body: { rooms: out } };
}

/** The API's error code -> a message key (unknown codes fall back to the generic one). */
export function travelErrorKey(detail: string | undefined): string {
  const known = new Set([
    "room_over_capacity",
    "traveler_twice",
    "room_no_twice",
    "rooms_not_in_proposal",
    "legal_hold",
    "trip_not_booked",
    "invalid_transition",
    "visa_application_exists",
    "visa_template_exists",
    "visa_details_need_approval",
    "decided_before_submitted",
    "line_not_confirmed",
    "stage3_locked",
    "lead_only",
    "trip_cancelled",
    "role_forbidden",
    "owner_only",
    "reason_required",
    "reveal_rate_limited",
  ]);
  return detail && known.has(detail) ? `travel.err.${detail}` : "travel.err.generic";
}

/** The departure page's path for a departure id. */
export function departurePath(ref: string): string {
  return `/departures/${encodeURIComponent(ref)}`;
}
