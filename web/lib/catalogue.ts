// web/lib/catalogue.ts
// AA-1069 (crm-052 / crm-053) — the shapes of the agency service catalogue API (api/app/catalogue) and of the supplier
// requests (api/app/ledger/requests), with the small pure helpers the Services page, the builder's "From catalogue"
// tab and the trip's Bookings tab share. unit_net and the tiers arrive only for owner and agent (the API builds them
// for view_margin roles; the margin serializer strips `*_net` as a backstop): a missing key means "not shown".

export const CATEGORIES = ["flight", "insurance", "visa", "transfer", "fee"] as const;
export type Category = (typeof CATEGORIES)[number];
export const QTY_BASES = ["per_person", "per_adult", "per_booking", "per_room", "per_night", "per_person_night"] as const;
export type QtyBasis = (typeof QTY_BASES)[number];

export interface Tier {
  min_pax: number;
  max_pax: number | null;
  unit_net: string;
}

export interface Service {
  service_id: string;
  agency_supplier_id: string | null;
  supplier_name: string | null;
  category: Category;
  name: string;
  unit_sell: string;
  currency: string;
  qty_basis: QtyBasis;
  valid_from: string | null;
  valid_to: string | null;
  active: boolean;
  tier_count: number;
  updated_at: string;
  /** Owner / agent only. */
  unit_net?: string | null;
  tiers?: Tier[];
}

export interface CatalogueView {
  services: Service[];
  categories: Category[];
  qty_bases: QtyBasis[];
  truncated: boolean;
}

/** How "Add from catalogue" priced an option (returned with the builder page). */
export interface CatalogueSummary {
  service_id: string;
  name: string;
  qty_basis: QtyBasis;
  quantity: number;
  travellers: number;
  adults: number;
  rooms: number;
  nights: number;
  unit_sell: string;
  currency: string;
  sell_amount: string;
  unit_net?: string | null;
  net_amount?: string | null;
}

/** One supplier's request draft (POST trips/{id}/supplier-requests). */
export interface SupplierRequest {
  agency_supplier_id: string;
  supplier_name: string;
  category: string;
  line_ids: string[];
  include_names: boolean;
  has_email: boolean;
  reason?: "supplier_no_email";
  conversation_id?: string;
  message_id?: string;
  subject?: string;
  body?: string;
}

export interface SupplierRequests {
  trip_id: string;
  locale: "en" | "vi";
  requests: SupplierRequest[];
  skipped: { booking_line_id: string; reason: string }[];
}

/** The supplier thread of an agency line (GET trips/{id}/lines). */
export interface SupplierThread {
  conversation_id: string;
  last_reply_at: string | null;
  replied: boolean;
}

const MONEY = /^\d{1,8}(\.\d{1,2})?$/;

/** A money text ("1,250.5") as the API's decimal string, null when empty, undefined when not a valid amount. */
export function unitInput(raw: string): string | null | undefined {
  const text = raw.replace(/[\s,]/g, "");
  if (text === "") return null;
  return MONEY.test(text) ? text : undefined;
}

/** Tier rows typed in the form ("1-4: 95", min, max (empty = and more), net) -> the API list, or null when invalid
 *  (a bad number, max below min, or two overlapping ranges). */
export function tiersInput(rows: { min: string; max: string; net: string }[]): Tier[] | null {
  const out: Tier[] = [];
  for (const row of rows) {
    if (!row.min.trim() && !row.max.trim() && !row.net.trim()) continue;
    const min = Number(row.min);
    const max = row.max.trim() === "" ? null : Number(row.max);
    const net = unitInput(row.net);
    if (!Number.isInteger(min) || min < 1 || min > 999 || net === undefined || net === null) return null;
    if (max !== null && (!Number.isInteger(max) || max < min || max > 999)) return null;
    out.push({ min_pax: min, max_pax: max, unit_net: net });
  }
  out.sort((a, b) => a.min_pax - b.min_pax);
  for (let i = 1; i < out.length; i += 1) {
    const prev = out[i - 1];
    if (prev.max_pax === null || prev.max_pax >= out[i].min_pax) return null;
  }
  return out;
}

/** The unit count a basis takes for a party (the same rule as the API's app.catalogue.service.quantity), for the
 *  builder's preview line; the API recomputes it when the option is added. */
export function quantityFor(basis: QtyBasis, party: { travellers: number; adults: number; rooms: number }, nights: number): number {
  const n = Math.max(1, Math.floor(nights));
  switch (basis) {
    case "per_person":
      return party.travellers;
    case "per_adult":
      return party.adults;
    case "per_booking":
      return 1;
    case "per_room":
      return party.rooms;
    case "per_night":
      return n;
    case "per_person_night":
      return party.travellers * n;
  }
}

/** Whether a service is offered on a day (its validity window; an open end is no limit). ISO dates compare as text. */
export function validOn(service: Pick<Service, "valid_from" | "valid_to">, day: string | null): boolean {
  if (!day) return true;
  if (service.valid_from && day < service.valid_from) return false;
  if (service.valid_to && day > service.valid_to) return false;
  return true;
}

/** The catalogue's refusal codes -> message keys (lib/i18nCatalogue.ts). */
const ERRORS = new Set([
  "service_inactive",
  "service_not_valid",
  "service_not_found",
  "tier_overlap",
  "fx_rate_missing",
  "role_forbidden",
  "invalid_agency_supplier_id",
  "supplier_no_email",
  "nothing_to_request",
  "no_lead_traveller",
  "mailbox_not_connected",
  "line_not_editable",
  "line_incomplete",
  "duplicate_confirmation_no",
  "trip_cancelled",
  "version_conflict",
]);

export function catalogueErrorKey(detail: string): string {
  return ERRORS.has(detail) ? `catalogue.error.${detail}` : "catalogue.error.other";
}
