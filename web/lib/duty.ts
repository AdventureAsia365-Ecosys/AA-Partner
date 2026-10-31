// web/lib/duty.ts
// AA-961 (card CRM-30; design v3 R37, A-02 Travelling now): the shapes of GET /duty/travelling, /duty/reach,
// POST /duty/reveal and GET /trips/{id}/incidents, and the pure helpers the pages use. Travellers come without names;
// a name or phone opens only through POST /duty/reveal (one audit event per traveller; a reason above
// REASON_THRESHOLD travellers; an hourly limit, 429 reveal_rate_limited). Edge-safe.

import { formatDate, type DateLocale } from "./dates";
import { fmt, type MessageKey } from "./i18n";

export type IncidentCode = "weather" | "road" | "health" | "political" | "supplier" | "other";
export type BulkField = "display_name" | "phone" | "email" | "emergency_contact";

export interface DutyLine {
  booking_line_id: string;
  title: string;
  supplier_kind: "aa" | "agency";
  departure_id: string | null;
  service_from: string | null;
  service_to: string | null;
}

export interface DutyTrip {
  trip_id: string;
  title: string;
  stage: "booked" | "pre_departure" | "travelling" | "completed";
  start_date: string | null;
  end_date: string | null;
  owner_member_id: string | null;
  is_mine: boolean;
  on_date: string;
  day_no: number | null;
  place: string | null;
  lines: DutyLine[];
  traveler_ids: string[];
  incident_flag_id: string | null;
}

export interface DutyTraveller {
  traveler_id: string;
  trip_ids: string[];
  regions: string[];
  has_phone: boolean;
  has_email: boolean;
  has_emergency_contact: boolean;
  has_health_info: boolean;
}

export interface IncidentTrip {
  trip_id: string;
  title: string;
  stage: string;
  start_date: string | null;
  end_date: string | null;
}

/** S262 (AA-957): an AA departure as the agency knows it, its own booking line on it (title, first service day). */
export interface IncidentDeparture {
  title: string | null;
  date: string | null;
}

export interface OpenIncident {
  incident_flag_id: string;
  departure_id: string;
  departure?: IncidentDeparture | null;
  incident_code: IncidentCode;
  raised_at: string;
  trips: IncidentTrip[];
}

export interface TravellingView {
  today: string;
  timezone: string;
  from: string;
  to: string;
  regions: { region: string; trips: DutyTrip[]; traveller_count: number }[];
  travellers: DutyTraveller[];
  incidents: OpenIncident[];
  truncated: boolean;
}

export interface ReachView {
  region: string;
  on_date: string;
  trips: DutyTrip[];
  travellers: DutyTraveller[];
  incidents: OpenIncident[];
}

export interface TripIncident {
  incident_flag_id: string;
  departure_id: string;
  departure?: IncidentDeparture | null;
  incident_code: IncidentCode;
  status: "open" | "cleared";
  raised_at: string;
  cleared_at: string | null;
}

export type RevealedTraveller = { traveler_id: string } & Partial<Record<BulkField, string | null>>;

export const REASON_THRESHOLD = 5;
export const MAX_BULK = 50;
export const BULK_REASONS = ["duty_of_care", "incident_response", "contact_client", "ops_handoff", "client_request"] as const;
export const WINDOWS = [1, 7, 14] as const;

/** Region codes are ISO countries ("VN"), or "unknown" when the trip says nothing usable. */
export function regionKey(region: string): MessageKey {
  return (/^[A-Z]{2}$/.test(region) ? `duty.region.${region}` : "duty.region.unknown") as MessageKey;
}

export const KNOWN_REGIONS = ["VN", "LA", "KH", "TH", "MM", "BT", "NP", "ID"] as const;

/** The label key of a region, falling back to the raw code for a country the CRM has no name for. */
export function regionLabel(region: string, tr: (key: MessageKey) => string): string {
  if ((KNOWN_REGIONS as readonly string[]).includes(region) || region === "unknown") return tr(regionKey(region));
  return region;
}

/** Whether a bulk reveal of ``count`` travellers needs the member to pick a reason (card R). */
export function needsReason(count: number): boolean {
  return count > REASON_THRESHOLD;
}

/** The trips of one traveller in the view, in the view's order (card E: one row, every trip). */
export function tripsOf(view: Pick<TravellingView, "regions">, traveller: DutyTraveller): DutyTrip[] {
  const all = view.regions.flatMap((g) => g.trips);
  return traveller.trip_ids.map((id) => all.find((t) => t.trip_id === id)).filter((t): t is DutyTrip => t !== undefined);
}

/** The fields worth asking for: only those the traveller has. */
export function availableFields(traveller: DutyTraveller): BulkField[] {
  const out: BulkField[] = ["display_name"];
  if (traveller.has_phone) out.push("phone");
  if (traveller.has_email) out.push("email");
  if (traveller.has_emergency_contact) out.push("emergency_contact");
  return out;
}

/** The error key of a failed bulk reveal (BFF detail -> message). */
export function revealErrorKey(status: number, detail: string): MessageKey {
  if (status === 429 || detail === "reveal_rate_limited") return "duty.reveal.rateLimited";
  if (detail === "reason_required") return "duty.reveal.reasonRequired";
  if (detail === "too_many_travelers") return "duty.reveal.tooMany";
  return "duty.reveal.failed";
}

/** The departure of an incident in words ("Ha Long cruise, from 12 Nov 2026"), or null to leave it out: the Booking
 * departure id is an opaque code and is never shown (S262 AA-957 evidence). */
export function incidentDeparture(
  departure: IncidentDeparture | null | undefined,
  locale: DateLocale,
  tr: (key: MessageKey) => string,
): string | null {
  const title = departure?.title?.trim();
  if (!title) return null;
  return departure?.date ? fmt(tr("duty.incident.departureOn"), { title, date: formatDate(departure.date, locale) }) : title;
}
