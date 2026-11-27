// web/lib/aaTerms.ts
// AA-1188 — the agency's booking conditions with AA (the CRM API's GET /crm/v1/aa/terms, booking.b2b.v1 GET /terms
// 1.3 + quote_rules 1.12): deposit and balance, the default cancellation fee tiers, AA's approved clauses and the quote
// rules (quote validity, group size quoted on request, child and infant ages). Shown on the AA prices page. Pure
// helpers, unit-tested.

import type { Lang } from "./i18n";
import { fmt, t } from "./i18n";

export const AA_TERMS_PATH = "aa/terms";

export interface AaTermsTier {
  min_days: number;
  fee_pct: string;
}

export interface AaTermsClause {
  code: string;
  title: string;
  body: string;
  free_cancel_days: number | null;
  version: number;
}

export interface AaTerms {
  as_of: string;
  payment: {
    deposit_pct: string;
    balance_days_before_departure: number;
    settlement_due_days: number;
    scope: string | null;
  } | null;
  /** Largest min_days first; null when AA has no approved default policy in force. */
  cancellation_tiers: AaTermsTier[] | null;
  clauses: AaTermsClause[];
  /** Null while AA answers an older contract or a policy value has no approved version. */
  quote_rules: {
    quote_validity_days: number;
    group_on_request_min_pax: number;
    age_bands: { child_age_max: number; infant_age_max: number };
  } | null;
  holds_paused: boolean;
}

/** "30.00" -> "30", "12.50" -> "12.5". */
export function pctText(pct: string): string {
  const n = Number(pct);
  return Number.isFinite(n) ? String(Math.round(n * 100) / 100) : pct;
}

/** The payment lines ("Deposit: 30% …", "Balance: billed 45 days …", "Invoices are due 14 days …"), or the one line
 * that says AA has none yet. */
export function paymentLines(terms: Pick<AaTerms, "payment">, lang: Lang): string[] {
  const p = terms.payment;
  if (!p) return [t(lang, "aaTerms.noPayment")];
  return [
    fmt(t(lang, "aaTerms.deposit"), { pct: pctText(p.deposit_pct) }),
    fmt(t(lang, "aaTerms.balance"), { days: p.balance_days_before_departure }),
    fmt(t(lang, "aaTerms.settlement"), { days: p.settlement_due_days }),
  ];
}

/** One line per fee tier, largest min_days first: "45+ days before departure: 10% …"; a 0-day tier (the last one)
 * reads "Less than <the previous tier's days> days …". */
export function cancellationLines(terms: Pick<AaTerms, "cancellation_tiers">, lang: Lang): string[] {
  const tiers = [...(terms.cancellation_tiers ?? [])].sort((a, b) => b.min_days - a.min_days);
  if (tiers.length === 0) return [t(lang, "aaTerms.noCancellation")];
  return tiers.map((tier, i) => {
    const pct = pctText(tier.fee_pct);
    if (tier.min_days === 0 && i > 0) return fmt(t(lang, "aaTerms.cancelTierLast"), { days: tiers[i - 1].min_days, pct });
    return fmt(t(lang, "aaTerms.cancelTier"), { days: tier.min_days, pct });
  });
}

/** The quote rules as lines, none when AA did not answer them. */
export function quoteRuleLines(terms: Pick<AaTerms, "quote_rules">, lang: Lang): string[] {
  const r = terms.quote_rules;
  if (!r) return [];
  return [
    fmt(t(lang, "aaTerms.validity"), { days: r.quote_validity_days }),
    fmt(t(lang, "aaTerms.group"), { n: r.group_on_request_min_pax }),
    fmt(t(lang, "aaTerms.ages"), { child: r.age_bands.child_age_max, infant: r.age_bands.infant_age_max }),
  ];
}
