// lib/currencies.ts — AA-1191: the currencies the platform enables (CIS Enabled currencies, USD only by default),
// served by GET /crm/v1/currencies and inside Settings > Payments. A picker lists these only and is hidden while USD is
// the only one; a stored value that is not enabled is kept as it is (never silently relabelled).

export const CURRENCIES_PATH = "currencies";
export const BASE_CURRENCY = "USD";

export interface CurrenciesView {
  enabled: string[];
  base: string;
}

/** USD first, then the other enabled codes, upper-cased, no duplicates; USD only when the API does not say. Pure. */
export function enabledCurrencies(list?: readonly string[] | null): string[] {
  const out = [BASE_CURRENCY];
  for (const c of list ?? []) {
    const code = typeof c === "string" ? c.trim().toUpperCase() : "";
    if (code && !out.includes(code)) out.push(code);
  }
  return out;
}

/** A picker is shown only when there is a choice. Pure. */
export function hasCurrencyChoice(list?: readonly string[] | null): boolean {
  return enabledCurrencies(list).length > 1;
}

/** The enabled list of a `GET /currencies` read, or null when it failed (the pickers then keep USD only). Pure. */
export function enabledFrom(read: { ok: true; data: CurrenciesView } | { ok: false }): string[] | null {
  return read.ok ? read.data.enabled : null;
}
