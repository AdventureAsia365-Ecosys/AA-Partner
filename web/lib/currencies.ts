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

/** The text proposal form's currency (AA-1191). Editing a saved draft (a PATCH) keeps its currency even when it is no
 *  longer enabled: the API accepts the stored value unchanged. A new version (a POST) of a proposal whose currency is
 *  not enabled starts in USD and `switchedFrom` names the old code, so the form leaves the amounts empty and says so
 *  (nothing is carried over or converted in the old currency). Pure. */
export function proposalFormCurrency(
  saved: string | null | undefined,
  list: readonly string[] | null | undefined,
  editingDraft: boolean,
): { currency: string; switchedFrom: string | null; choice: boolean } {
  const enabled = enabledCurrencies(list);
  const stored = (saved ?? "").trim().toUpperCase() || BASE_CURRENCY;
  if (enabled.includes(stored)) return { currency: stored, switchedFrom: null, choice: enabled.length > 1 };
  if (editingDraft) return { currency: stored, switchedFrom: null, choice: false };
  return { currency: BASE_CURRENCY, switchedFrom: stored, choice: enabled.length > 1 };
}

/** The text proposal form's starting values (see `proposalFormCurrency`): the amounts are left empty when a new
 *  version switches to USD, so no amount in the old currency is saved as USD. Pure. */
export function proposalFormSeed(
  seed: { currency: string; status: string; total_amount: string | null; deposit_amount: string | null } | null,
  list: readonly string[] | null | undefined,
): { currency: string; switchedFrom: string | null; choice: boolean; total: string; deposit: string } {
  const decided = proposalFormCurrency(seed?.currency, list, seed?.status === "draft");
  const keep = seed !== null && decided.switchedFrom === null;
  return { ...decided, total: keep ? (seed.total_amount ?? "") : "", deposit: keep ? (seed.deposit_amount ?? "") : "" };
}

/** The builder's "Start" row (AA-1191): open for the first version, or when the latest version cannot be copied
 *  (`blocked` names its currency, no longer enabled). Pure. */
export function builderStart(
  versions: readonly { version: number; currency: string }[],
  list?: readonly string[] | null,
): { open: boolean; blocked: string | null } {
  if (versions.length === 0) return { open: true, blocked: null };
  const latest = versions.reduce((a, p) => (a.version > p.version ? a : p));
  return canCopyBuilderVersion(latest.currency, list) ? { open: false, blocked: null } : { open: true, blocked: latest.currency };
}

/** A builder working copy is made from a version in an enabled currency only (the API refuses the copy otherwise, 422
 *  currency_not_enabled: its prices are in the old currency). False = offer a new version started in USD. Pure. */
export function canCopyBuilderVersion(currency: string | null | undefined, list?: readonly string[] | null): boolean {
  return enabledCurrencies(list).includes((currency ?? "").trim().toUpperCase() || BASE_CURRENCY);
}

/** The enabled list of a `GET /currencies` read, or null when it failed (the pickers then keep USD only). Pure. */
export function enabledFrom(read: { ok: true; data: CurrenciesView } | { ok: false }): string[] | null {
  return read.ok ? read.data.enabled : null;
}
