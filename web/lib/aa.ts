// web/lib/aa.ts
// AA-947 (card CRM-19) — the shapes of the Adventure Asia supply API (api/app/routers/aa.py, the `aa` block of a
// booking line from api/app/booking/views.py) and the small pure helpers the Bookings tab uses. An AA line is
// read-only: the agent only asks for things (quote, hold, confirm, release, re-quote); while Booking has not answered
// the line reads "AA pending" (with the next try when Booking is down).

export type AaAction = "hold" | "confirm" | "release" | "requote";
export const AA_ACTIONS: readonly AaAction[] = ["hold", "confirm", "release", "requote"];
export const AA_SUB_STATUSES = [
  "pending",
  "quoted",
  "sold_out",
  "held",
  "on_request",
  "declined",
  "expired",
  "released",
  "not_served",
  "failed",
] as const;
export type AaSubStatus = (typeof AA_SUB_STATUSES)[number];
export const COMPONENT_KINDS = ["trip_plan", "hotel_room", "transfer", "guide", "activity", "entrance", "meal"] as const;
export type ComponentKind = (typeof COMPONENT_KINDS)[number];

export interface AaPending {
  aa_request_id: string;
  kind: "quote" | "requote" | "hold" | "confirm" | "release" | "refresh";
  attempts: number;
  next_attempt_at: string;
  last_error_class: string | null;
}

export interface AaBlock {
  sub_status: AaSubStatus | null;
  tour_source_id: string | null;
  component_kind: ComponentKind | null;
  currency: string | null;
  pax: { adult?: number; child?: number; infant?: number; nights?: number } | null;
  quote_valid_until: string | null;
  hold_expires_at: string | null;
  hold_id: string | null;
  booking_id: string | null;
  from_price: { amount: string; currency: string } | null;
  below_from_price: boolean | null;
  decline_code: string | null;
  result_code: string | null;
  no_longer_offered: boolean;
  pending: AaPending | null;
  actions: AaAction[];
}

export interface CatalogTour {
  source_id: string;
  title: string;
  country_code: string | null;
  duration_days: number | null;
}

export interface AaComponent {
  component_id: string;
  kind: ComponentKind;
  day_no: number | null;
}

export interface AaDeparture {
  departure_id: string;
  start_date: string;
  available: number;
  status: string;
  auto_confirm: boolean;
}

export interface AaProduct {
  tour_source_id: string;
  has_fixed_departures: boolean;
  components: AaComponent[];
  departures: AaDeparture[];
  from_price: { amount: string; currency: string } | null;
}

/** The asks this role may make: a confirm creates a real AA booking, so only the owner and the agent see it (the
 * API's aa_confirm refuses the assistant with 403). The other asks are open to every role. */
export function allowedAaActions(actions: readonly AaAction[], role: string): AaAction[] {
  const confirms = role === "owner" || role === "agent";
  return actions.filter((action) => action !== "confirm" || confirms);
}

/** The sub-status message key; a pending request shows "AA pending" whatever the line's last state was. */
export function statusKey(aa: AaBlock): string {
  if (aa.pending) return "aa.status.pending";
  return `aa.status.${aa.sub_status ?? "confirmed"}`;
}

/** S262 (AA-957 live): the AA sub-status badge of a line, or null when it would only repeat (or contradict) the line's
 * own status: a cancelled line whose AA booking was cancelled has no sub-status left, and the badge then fell back to
 * "Confirmed by AA" next to "Cancelled". A released / declined / expired line keeps its badge (it says why). */
export function aaBadgeKey(aa: AaBlock, lineStatus: string): string | null {
  if (!aa.pending && lineStatus === "cancelled" && (aa.sub_status == null || (aa.sub_status as string) === "confirmed")) {
    return null;
  }
  return statusKey(aa);
}

/** Every result code the Booking sync can write on a line (api/app/booking/sync.py, proposal_lines.py, the Booking
 * contract's refusal codes): each has an `aa.result.<code>` sentence in EN and VI (tests/cancelledTrip.test.ts). */
export const AA_RESULT_CODES = [
  "sold_out",
  "price_changed",
  "hold_expired",
  "hold_released",
  "hold_not_found",
  "not_in_hold",
  "hold_confirmed",
  "rehold_limit",
  "extend_limit",
  "tour_not_served",
  "unknown_partner",
  "forbidden",
  "invalid",
  "not_found",
  "gave_up",
  "declined",
  "booking_cancelled",
  "cancelled_by_agency",
  "cancelled",
] as const;

/** The i18n key of a line's AA result code; a code a later Booking version adds reads as a plain refusal, never raw. */
export function aaResultKey(code: string): string {
  return (AA_RESULT_CODES as readonly string[]).includes(code) ? `aa.result.${code}` : "aa.result.other";
}

/** A failed request code that is not AA's answer: no answer for a day (`gave_up`), or the CRM could not reach Booking
 * (the retry classes the sync writes on `crm.aa_request.result_code`: `booking_*` such as `booking_timeout` /
 * `booking_unavailable`, and `token_*` from the sign-in to Booking). Never worded as a refusal. */
export function aaNoAnswer(code: string): boolean {
  return code === "gave_up" || /^(booking|token)_/.test(code);
}

/** The sentence key of a failed AA cancel request (the cancel dialog's pending mode): AA's refusal reads its result
 * sentence; a request AA never answered says so, never "refused". */
export function aaCancelFailedKey(code: string): string {
  if (code === "gave_up") return "aa.result.gave_up";
  if (aaNoAnswer(code)) return "cancelTrip.aaNoAnswer";
  return aaResultKey(code);
}

/** Booking failed the last try and the request waits for the next one (card E: shown, never hidden). */
export function isRetrying(aa: AaBlock): boolean {
  return !!aa.pending && aa.pending.attempts > 0 && !!aa.pending.last_error_class;
}

/** The sell per person (adults + children) below AA's public "from" price: a warning, never a block. Null when it
 * cannot be judged (no sell, no from price, another currency, no paying traveller). */
export function belowFromPrice(
  sell: string,
  currency: string,
  adults: number,
  children: number,
  fromPrice: { amount: string; currency: string } | null,
): boolean | null {
  const people = adults + children;
  const amount = Number(sell.replace(/[\s,]/g, ""));
  if (!fromPrice || fromPrice.currency !== currency || people <= 0 || !sell.trim() || !Number.isFinite(amount)) {
    return null;
  }
  return amount / people < Number(fromPrice.amount);
}

/** A request body for POST trips/{id}/aa-lines from the add form, or a message key of what is wrong. */
export function aaLineBody(form: {
  tourSourceId: string;
  componentId: string;
  componentKind: string;
  departureId: string;
  serviceDate: string;
  nights: string;
  adults: string;
  children: string;
  infants: string;
  currency: string;
  sell: string;
}): { ok: true; body: Record<string, unknown> } | { ok: false; error: string } {
  const adults = Number(form.adults);
  const children = Number(form.children || "0");
  const infants = Number(form.infants || "0");
  if (!form.tourSourceId || !form.componentId) return { ok: false, error: "aa.error.pickProduct" };
  if (!/^\d{4}-\d{2}-\d{2}$/.test(form.serviceDate)) return { ok: false, error: "aa.error.date" };
  if (![adults, children, infants].every(Number.isInteger) || adults < 1 || adults > 60 || children < 0 || infants < 0) {
    return { ok: false, error: "aa.error.pax" };
  }
  if (!/^[A-Z]{3}$/.test(form.currency)) return { ok: false, error: "aa.error.currency" };
  const nights = form.nights ? Number(form.nights) : null;
  if (form.componentKind === "hotel_room" && (!nights || !Number.isInteger(nights) || nights < 1 || nights > 60)) {
    return { ok: false, error: "aa.error.nights" };
  }
  const sell = form.sell.replace(/[\s,]/g, "");
  if (sell && !/^\d+(\.\d{1,2})?$/.test(sell)) return { ok: false, error: "aa.error.sell" };
  return {
    ok: true,
    body: {
      tour_source_id: form.tourSourceId,
      component_id: form.componentId,
      component_kind: form.componentKind,
      departure_id: form.departureId || null,
      service_date: form.serviceDate,
      nights,
      pax: { adult: adults, child: children, infant: infants },
      currency: form.currency,
      sell_amount: sell || null,
    },
  };
}

/** The API's refusal codes of the AA routes -> message keys (lib/i18n.ts aa.error.*). */
const ERRORS = new Set([
  "aa_request_pending",
  "aa_action_not_allowed",
  "deposit_required",
  "use_cancel_flow",
  "trip_closed",
  "aa_tour_not_served",
  "aa_request_invalid",
  "booking_unavailable",
  "booking_not_configured",
  "aa_partner_not_active",
  "aa_product_not_found",
  "role_forbidden",
]);

export function aaErrorKey(detail: string): string {
  return ERRORS.has(detail) ? `aa.error.${detail}` : "ledger.error.other";
}
