// web/lib/bookNow.ts
// AA-1072 (item crm-02) — "Book now" on the public tour page (/trip/<tourId>?f=<form key>[&p=<AA tour key>]): the
// shapes of the API's public routes and the pure helpers the widget uses. The browser calls the API on its own origin
// (the client host sends /crm/public/* to the API), so the per-address limits key on the visitor.
//
//   GET  /crm/public/v1/book/<form key>?tour=&page=   the widget (agency, currency, departures, honeypot field)
//   POST /crm/public/v1/book/<form key>/price         the live price of a departure for a party (nothing stored)
//   POST /crm/public/v1/book/<form key>/bookings      one booking -> {status: created, link_path: /c/p/<token>}

export const BOOK_API = "/crm/public/v1/book";
export const FORM_KEY = /^[A-Za-z0-9_-]{20,64}$/;
export const TOUR_KEY = /^[^\u0000-\u001f]{1,128}$/;
export const MAX_ADULTS = 20;
export const MAX_CHILDREN = 10;

export interface BookingWidget {
  agency_name: string;
  tour: string;
  title: string;
  duration_days: number | null;
  currency: string;
  child_age_max: number;
  infant_age_max: number;
  honeypot_field: string;
  consent_text: string | null;
  privacy_notice_url: string | null;
  departures: { departure_id: string; start_date: string; seats_left: number }[];
}

export interface BookingPrice {
  currency: string;
  total: string;
  lines: { pax_type: "adult" | "child" | "infant"; qty: number; unit: string }[];
  availability: "available" | "on_request" | "sold_out";
  start_date: string;
}

export type BookingAnswer =
  | { status: "created"; link_path: string; currency: string; total: string }
  | { status: "already_created" }
  | { status: "received" };

/** The form key and the AA tour key of the page link, or null when the link names no agency (no widget). */
export function bookingLink(search: { f?: string | string[]; p?: string | string[] }): { formKey: string; tour: string | null } | null {
  const f = Array.isArray(search.f) ? search.f[0] : search.f;
  const p = Array.isArray(search.p) ? search.p[0] : search.p;
  if (!f || !FORM_KEY.test(f)) return null;
  return { formKey: f, tour: p && TOUR_KEY.test(p) ? p : null };
}

/** The widget's address: the AA tour key of the link (else the page's tour id, which is a catalogue key for a tour
 * without versions). */
export function widgetUrl(formKey: string, tour: string | null, pageId: string): string {
  const q = new URLSearchParams();
  if (tour) q.set("tour", tour);
  q.set("page", pageId);
  return `${BOOK_API}/${encodeURIComponent(formKey)}?${q.toString()}`;
}

/** Child ages from the form's rows ("" rows are ignored), or null when one is not a whole age from 0 to 17. */
export function childAges(rows: string[]): number[] | null {
  const out: number[] = [];
  for (const row of rows) {
    const text = row.trim();
    if (!text) continue;
    if (!/^\d{1,2}$/.test(text) || Number(text) > 17) return null;
    out.push(Number(text));
  }
  return out.length <= MAX_CHILDREN ? out : null;
}

/** The i18n key of an API refusal on the widget (an unknown code reads as the generic message). */
const CODES = new Set([
  "consent_required",
  "invalid_submission",
  "invalid_party",
  "aa_sold_out",
  "departure_not_available",
  "no_retail_price",
  "price_on_request",
  "aa_tour_not_served",
  "aa_quote_refused",
  "booking_rate_limited",
  "rate_limited",
  "booking_in_progress",
  "booking_unavailable",
  "booking_failed",
  "origin_not_allowed",
]);

export function bookErrorKey(code: string | undefined, status: number): string {
  const c = code ?? (status === 429 ? "rate_limited" : "");
  return CODES.has(c) ? `book.error.${c}` : "book.error.other";
}

/** A link path the API may send the client to: only its own proposal page. */
export function safeLinkPath(path: unknown): string | null {
  return typeof path === "string" && /^\/c\/p\/[A-Za-z0-9_-]{43}$/.test(path) ? path : null;
}
