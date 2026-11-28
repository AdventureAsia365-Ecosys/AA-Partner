// web/lib/aaTariff.ts
// AA-1030 — the agency's AA prices (its AA tariff): AA's B2B price for this agency per tour, season and pax band, with
// its tier or contract discount, read live from AA (the CRM API's GET /crm/v1/aa/tariff, booking.b2b.v1 1.4). Owner
// and agent only (the API's aa_tariff_view; /me `permissions` says so, the API refuses the assistant on its own). The
// prices are indicative: a quote is the binding price. Pure helpers, unit-tested.

import type { Me } from "./crmApi";
import type { Lang, MessageKey } from "./i18n";
import { fmt, t } from "./i18n";

export const AA_TARIFF_PATH = "/aa-tariff";
export const TARIFF_CURRENCIES_PATH = "aa/tariff/currencies";
/** The whole tariff is one answer: the API waits up to 25 s for AA (api/app/booking/client.py BULK_TIMEOUT_S). */
export const TARIFF_TIMEOUT_MS = 30_000;
/** The quote currencies AA prices in (booking.b2b.v1 1.4; api/app/booking/tariff.py CURRENCIES). */
export const TARIFF_CURRENCIES = ["USD", "EUR", "GBP", "AUD", "SGD", "VND"] as const;
export type TariffCurrency = (typeof TARIFF_CURRENCIES)[number];

export interface TariffMoney {
  amount: string;
  currency: string;
}

export interface TariffItem {
  source_id: string;
  title: string | null;
  country: string | null;
  duration_days: number | null;
  season: string | null;
  valid_from: string;
  valid_to: string;
  pax_min: number;
  pax_max: number | null;
  priced_for_pax: number;
  /** Removed by the API's margin serializer for a role without view_margin (never this page's roles). */
  b2b_price?: TariffMoney;
  b2b_price_per_person?: TariffMoney;
  agency_discount_pct: string;
  rrp_per_person: TariffMoney | null;
  /** AA-1188 (booking.b2b.v1 1.7 / 1.12): the sell prices by age band and room (null: AA did not price it; absent
   * while the API is older). The price per person above is the twin share; the single supplement is on top. */
  b2b_child_price_per_person?: TariffMoney | null;
  b2b_infant_price?: TariffMoney | null;
  b2b_single_supplement_per_person?: TariffMoney | null;
  /** "on_request": the band reaches the group size AA quotes on request (absent = available). */
  availability?: "available" | "on_request";
  /** A departure inside the row's dates has its own price adjustment. */
  departure_adjusted?: boolean;
}

export interface TariffView {
  currency: string;
  priced_on: string;
  tier_code: string;
  basis: string;
  rate_date: string;
  truncated: boolean;
  items: TariffItem[];
  /** AA-1188 (1.12): null / absent while AA answers an older contract (the page then leaves the line out). */
  group_on_request_min_pax?: number | null;
  early_booking_days?: number | null;
  tours_priced?: number | null;
  catalog_tours?: number | null;
}

/** AA-1188 (decision D5): the row's price is shown as "On request" (AA quotes that group size on request). */
export function isOnRequest(item: Pick<TariffItem, "availability">): boolean {
  return item.availability === "on_request";
}

/** "Priced tours: 2 of 844" — null when the API did not count (an older AA answer, or a one-tour read). */
export function coverageText(view: Pick<TariffView, "tours_priced" | "catalog_tours">, lang: Lang): string | null {
  const priced = view.tours_priced;
  const total = view.catalog_tours;
  if (typeof priced !== "number" || typeof total !== "number" || total < 1) return null;
  return fmt(t(lang, "tariff.coverage"), { n: priced, m: total });
}

/** The notes under the table: twin share + single supplement, the group size on request, early booking, and that a
 * departure may carry its own price. Only the parts the answer supports. */
export function tariffNotes(view: Pick<TariffView, "group_on_request_min_pax" | "early_booking_days">, lang: Lang): string[] {
  const out = [t(lang, "tariff.note.twin")];
  if (typeof view.group_on_request_min_pax === "number") {
    out.push(fmt(t(lang, "tariff.note.group"), { n: view.group_on_request_min_pax }));
  }
  if (typeof view.early_booking_days === "number" && view.early_booking_days > 0) {
    out.push(fmt(t(lang, "tariff.note.early"), { days: view.early_booking_days }));
  }
  out.push(t(lang, "tariff.note.departure"));
  return out;
}

const SEASON_CODES = new Set(["low", "high", "peak", "festive"]);

/** AA's season calendar codes as words ("high" -> "High season"); a matrix label is shown as AA wrote it; none "—". */
export function seasonText(season: string | null, lang: Lang): string {
  if (!season) return "—";
  return SEASON_CODES.has(season) ? t(lang, `tariff.season.${season}` as MessageKey) : season;
}

/** Whether this member sees the AA prices page and its links: the API's matrix answer from /me, else (an older API)
 * the roles that see margins. */
export function seesAaTariff(me: Pick<Me, "member_role" | "permissions">): boolean {
  const answer = me.permissions?.aa_tariff_view;
  if (typeof answer === "boolean") return answer;
  return me.member_role === "owner" || me.member_role === "agent";
}

export function tariffCurrency(raw: string | string[] | undefined): TariffCurrency {
  const v = typeof raw === "string" ? raw.toUpperCase() : "";
  return (TARIFF_CURRENCIES as readonly string[]).includes(v) ? (v as TariffCurrency) : "USD";
}

/** S270: GET /crm/v1/aa/tariff/currencies — USD plus the quote currencies AA has an approved FX rate for today
 * (`checked: false`: AA could not be asked, every quote currency is offered). */
export interface TariffCurrencies {
  currencies: string[];
  rate_date: string | null;
  checked: boolean;
}

/** The currencies the select offers, in the order of TARIFF_CURRENCIES, USD always first: the API's answer (only the
 * platform's enabled currencies, AA-1191), else (the read failed or answered nothing usable) USD only. A currency the
 * web does not know is dropped. */
export function offeredCurrencies(answer: { ok: true; data: TariffCurrencies } | { ok: false } | null): TariffCurrency[] {
  const listed = answer?.ok && Array.isArray(answer.data?.currencies) ? new Set(answer.data.currencies) : null;
  if (!listed) return ["USD"];
  return TARIFF_CURRENCIES.filter((c) => c === "USD" || listed.has(c));
}

/** The currency the page shows: the one asked for when it is offered, else USD. `fellBack` names the asked currency
 * when it was not offered (the page says so in a role=status notice); a missing `?currency=` is plain USD. */
export function pickCurrency(
  raw: string | string[] | undefined,
  offered: readonly string[],
): { currency: TariffCurrency; fellBack: string | null } {
  const v = typeof raw === "string" ? raw.toUpperCase() : "";
  if (v && offered.includes(v)) return { currency: tariffCurrency(v), fellBack: null };
  // Only a plain three-letter code is echoed in the notice; anything else is just USD.
  return { currency: "USD", fellBack: /^[A-Z]{3}$/.test(v) && v !== "USD" ? v : null };
}

/** "aa/tariff?currency=EUR&format=csv" for the BFF (lib/bff.ts allow-list). */
export function tariffPath(currency: string, format: "json" | "csv" = "json"): string {
  const q = new URLSearchParams({ currency });
  if (format === "csv") q.set("format", "csv");
  return `aa/tariff?${q.toString()}`;
}

/** The name the CSV is saved under when the API's answer names none. */
export function tariffFilename(currency: string, today: Date = new Date()): string {
  return `aa-tariff-${currency.toLowerCase()}-${today.toISOString().slice(0, 10)}.csv`;
}

/** "2–4 people", "11+ people", "1 person". */
export function paxBand(item: Pick<TariffItem, "pax_min" | "pax_max">, lang: Lang): string {
  if (item.pax_max === null) return fmt(t(lang, "tariff.pax.open"), { min: item.pax_min });
  if (item.pax_max === item.pax_min) {
    return fmt(t(lang, item.pax_min === 1 ? "tariff.pax.one" : "tariff.pax.exact"), { n: item.pax_min });
  }
  return fmt(t(lang, "tariff.pax.range"), { min: item.pax_min, max: item.pax_max });
}

/** "0.0300" -> "3%", "0.0325" -> "3.25%", "0" -> "—" (no discount). */
export function discountText(pct: string): string {
  const n = Number(pct);
  if (!Number.isFinite(n) || n <= 0) return "—";
  const value = Math.round(n * 10000) / 100;
  return `${value}%`;
}

/** The error message key of a failed tariff read (the API's detail). */
export function tariffErrorKey(
  detail: string,
): "tariff.err.notLinked" | "tariff.err.unavailable" | "tariff.err.busy" | "tariff.err.currency" | "tariff.err.generic" {
  if (detail === "aa_partner_not_active") return "tariff.err.notLinked";
  // S270: AA has no approved FX rate for the asked currency today (Booking 422 invalid): the agency's choice, not
  // an outage.
  if (detail === "aa_tariff_invalid") return "tariff.err.currency";
  // The API reads at most one tariff per agency at a time (429 aa_tariff_busy, Retry-After a few seconds).
  if (detail === "aa_tariff_busy") return "tariff.err.busy";
  if (detail === "booking_unavailable" || detail === "booking_not_configured" || detail === "api_timeout" || detail === "api_unreachable") {
    return "tariff.err.unavailable";
  }
  return "tariff.err.generic";
}
