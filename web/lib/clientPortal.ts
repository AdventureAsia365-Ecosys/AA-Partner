// web/lib/clientPortal.ts
// AA-953 (card CRM-24a) — the client side of the traveller portal per person: the link cookie of §2.9 rule 1 for the
// portal (Fix B11, like the proposal page), the shapes the API's per-person home adds (api/app/portal/person.py), and
// the pure helpers the portal components share. Nothing here ever holds a passport or Stage 3 value: Stage 3 is
// write-only, a passport scan is upload-only.

/** The portal link's token goes into this host-only cookie (scoped to the API's traveller routes) and leaves the URL. */
export const PORTAL_LINK_COOKIE = "aa_portal_link";
export const PORTAL_API = "/crm/public/v1/traveller";
/** The longest life of a link (crm.client_link: 30 days); the API refuses an expired or revoked token anyway. */
export const PORTAL_COOKIE_MAX_AGE = 30 * 24 * 3600;
/** app.documents.service.MAX_BYTES: 7 MiB. */
export const MAX_SCAN_BYTES = 7 * 1024 * 1024;
export const SCAN_TYPES = ["image/jpeg", "image/png", "image/webp", "image/heic", "application/pdf"] as const;

/** The Set-Cookie value of the exchange: httpOnly (page script never sees the token), Secure, SameSite=Strict, no
 * Domain (host-only: the client host only), Path = the API's traveller routes only. */
export function portalCookie(token: string): string {
  return `${PORTAL_LINK_COOKIE}=${token}; Path=${PORTAL_API}; Max-Age=${PORTAL_COOKIE_MAX_AGE}; HttpOnly; Secure; SameSite=Strict`;
}

export type TravellerRole = "lead" | "companion";
export type FlightDirection = "arrival" | "departure";

/** Fix E4 "Who are you?": the travellers one address reaches. */
export interface PortalPick {
  traveler_id: string;
  name: string | null;
  role: TravellerRole;
  link: boolean;
}

export interface PortalPerson {
  traveler_id: string;
  name: string | null;
  role: TravellerRole;
}

export interface PortalFlight {
  direction: FlightDirection;
  flight_no: string;
  airport: string;
  local_time: string;
  local_date: string;
  tz: string;
  utc_offset: string;
  days_from_start: number | null;
}

export interface PortalDocument {
  document_id: string;
  kind: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  for: "me" | "trip";
  created_at: string;
}

export interface PortalService {
  title: string;
  from: string | null;
  to: string | null;
  status: "planning" | "confirmed";
  confirm_class: string | null;
  pay_on_arrival: boolean;
}

export interface PortalDay {
  day_no: number;
  before_you_go: boolean;
  date: string | null;
  title: string | null;
  place: string | null;
  tz: string;
  utc_offset: string;
}

export interface PortalPartyMember {
  traveler_id?: string;
  name?: string | null;
  role: string;
  details_done: boolean;
  waiver_signed: boolean | null;
  me: boolean;
  has_email?: boolean;
  via_you?: boolean;
  portal?: "invited" | "active" | null;
}

/** The fields AA-953 adds to the portal home (PortalHome in lib/proposals.ts keeps the AA-946 base). */
export interface PortalPersonHome {
  me?: PortalPerson;
  switch_to?: PortalPerson[];
  flights?: PortalFlight[];
  documents?: PortalDocument[];
  services?: PortalService[];
  itinerary?: PortalDay[];
  passport_scan_on_file?: boolean;
}

export interface FlightForm {
  flight_no: string;
  airport: string;
  local_time: string;
  tz: string;
}

export const EMPTY_FLIGHT: FlightForm = { flight_no: "", airport: "", local_time: "", tz: "" };

/** The airport zones a traveller picks from most often (Asia first, then the usual departure hubs); any IANA zone is
 * accepted by the API, the list only saves typing. */
export const COMMON_ZONES = [
  "Asia/Ho_Chi_Minh",
  "Asia/Bangkok",
  "Asia/Vientiane",
  "Asia/Phnom_Penh",
  "Asia/Yangon",
  "Asia/Kathmandu",
  "Asia/Thimphu",
  "Asia/Kolkata",
  "Asia/Singapore",
  "Asia/Kuala_Lumpur",
  "Asia/Jakarta",
  "Asia/Manila",
  "Asia/Hong_Kong",
  "Asia/Shanghai",
  "Asia/Tokyo",
  "Asia/Seoul",
  "Asia/Dubai",
  "Australia/Sydney",
  "Australia/Melbourne",
  "Australia/Perth",
  "Pacific/Auckland",
  "Europe/London",
  "Europe/Paris",
  "Europe/Berlin",
  "America/New_York",
  "America/Los_Angeles",
  "UTC",
] as const;

const FLIGHT_NO = /^[A-Z0-9]{2}[A-Z]?[0-9]{1,4}[A-Z]?$/;
const AIRPORT = /^[A-Z]{3}$/;
const LOCAL_TIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/;

/** The body of PUT /traveller/flights for one direction, or the fields to fix (the API checks again). */
export function flightBody(direction: FlightDirection, form: FlightForm): { body: Record<string, string> } | { fields: string[] } {
  const flight_no = form.flight_no.replace(/[\s-]/g, "").toUpperCase();
  const airport = form.airport.trim().toUpperCase();
  const fields: string[] = [];
  if (!FLIGHT_NO.test(flight_no)) fields.push("flight_no");
  if (!AIRPORT.test(airport)) fields.push("airport");
  if (!LOCAL_TIME.test(form.local_time)) fields.push("local_time");
  if (!form.tz) fields.push("tz");
  if (fields.length) return { fields };
  return { body: { direction, flight_no, airport, local_time: form.local_time, tz: form.tz } };
}

/** The form values of a saved flight (to edit it). */
export function flightForm(f: PortalFlight | undefined): FlightForm {
  return f ? { flight_no: f.flight_no, airport: f.airport, local_time: f.local_time, tz: f.tz } : { ...EMPTY_FLIGHT };
}

/** R33: which day a flight lands or leaves, relative to the trip (an overnight flight lands on its own local date):
 * the message key and its number. */
export function flightDayKey(f: PortalFlight): { key: string; n: number } | null {
  const d = f.days_from_start;
  if (d == null) return null;
  if (d === 0) return { key: "client.flightDay.first", n: 1 };
  if (d === -1) return { key: "client.flightDay.dayBefore", n: 1 };
  if (d < -1) return { key: "client.flightDay.before", n: -d };
  return { key: "client.flightDay.during", n: d + 1 };
}

/** "06:40" of a "2026-12-12T06:40" local time. */
export function localClock(localTime: string): string {
  return localTime.slice(11, 16);
}

/** A document's size for people: "820 KB", "1.4 MB". */
export function fileSize(bytes: number): string {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Whether a picked file may be sent as a passport scan (type and size; the API sniffs the bytes too). */
export function scanProblem(file: { type: string; size: number }): "type" | "size" | null {
  if (!(SCAN_TYPES as readonly string[]).includes(file.type)) return "type";
  if (file.size < 1 || file.size > MAX_SCAN_BYTES) return "size";
  return null;
}

/** The base64 of an ArrayBuffer (FileReader-free, so it is testable). */
export function toBase64(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let out = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) out += String.fromCharCode(...bytes.subarray(i, i + chunk));
  return btoa(out);
}
