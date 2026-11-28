"use client";
// components/proposals/CurrencyFields.tsx — AA-1191: the proposal currency pickers. They list the platform's enabled
// currencies only (GET /crm/v1/currencies, USD only by default) and are hidden while USD is the only one; a saved
// currency that is not enabled stays as it is (a hidden value), never relabelled.

import type React from "react";
import { BASE_CURRENCY, enabledCurrencies } from "@/lib/currencies";

/** The builder's currency for a new working copy: a select when there is a choice, else nothing (USD). */
export function BuilderCurrencyPicker({ label, value, currencies, onChange, style }: {
  label: string; value: string; currencies?: string[] | null; onChange: (code: string) => void;
  style: React.CSSProperties;
}) {
  const enabled = enabledCurrencies(currencies);
  if (enabled.length < 2) return null;
  return (
    <label style={{ display: "grid", gap: 2, fontSize: 12.5 }}>
      {label}
      <select value={value} onChange={(e) => onChange(e.target.value)} style={{ ...style, width: 90 }} data-testid="builder-currency">
        {enabled.map((c) => <option key={c} value={c}>{c}</option>)}
      </select>
    </label>
  );
}

/** The text proposal's `currency` form field: a select of the enabled currencies, or a hidden value. */
export function ProposalCurrencyField({ label, saved, currencies, style }: {
  label: string; saved?: string | null; currencies?: string[] | null; style: React.CSSProperties;
}) {
  const enabled = enabledCurrencies(currencies);
  const current = saved ?? BASE_CURRENCY;
  if (enabled.length < 2 || !enabled.includes(current)) return <input type="hidden" name="currency" value={current} />;
  return (
    <label style={{ display: "grid", gap: 2, fontSize: 12.5 }}>
      {label}
      <select name="currency" defaultValue={current} style={{ ...style, width: 90 }}>
        {enabled.map((c) => <option key={c} value={c}>{c}</option>)}
      </select>
    </label>
  );
}
