"use client";
// components/proposals/CurrencyFields.tsx — AA-1191: the proposal currency pickers. They list the platform's enabled
// currencies only (GET /crm/v1/currencies, USD only by default) and are hidden while USD is the only one. A saved draft
// in a currency that is not enabled keeps it (a hidden value, never relabelled); a new version of such a proposal
// starts in USD with a note (lib/currencies.ts proposalFormCurrency).

import type React from "react";
import { Button, K } from "@/app/_kit";
import { builderStart, enabledCurrencies } from "@/lib/currencies";

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

/** The text proposal's `currency` form field (see `proposalFormCurrency`): a select of the enabled currencies when
 *  there is a choice, else a hidden value; `note` is shown when a new version starts in USD instead of the old code. */
export function ProposalCurrencyField({ label, currency, choice, currencies, note, style }: {
  label: string; currency: string; choice: boolean; currencies?: string[] | null; note?: string | null;
  style: React.CSSProperties;
}) {
  const enabled = enabledCurrencies(currencies);
  const notice = note ? <span data-testid="proposal-currency-note" style={{ color: K.muted, fontSize: 12, flexBasis: "100%" }}>{note}</span> : null;
  if (!choice) return <><input type="hidden" name="currency" value={currency} />{notice}</>;
  return (
    <>
      <label style={{ display: "grid", gap: 2, fontSize: 12.5 }}>
        {label}
        <select name="currency" defaultValue={currency} style={{ ...style, width: 90 }}>
          {enabled.map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
      </label>
      {notice}
    </>
  );
}

/** The builder's "Start" row of the trip page (`builderStart`): the currency picker and the button for the first
 *  version, or for a new version when the latest one is in a currency that is no longer enabled (with a note). */
export function BuilderStartRow({ versions, currencies, value, onChange, onStart, disabled, label, startLabel, blockedNote, style }: {
  versions: readonly { version: number; currency: string }[]; currencies?: string[] | null; value: string;
  onChange: (code: string) => void; onStart: () => void; disabled: boolean; label: string; startLabel: string;
  blockedNote: (code: string) => string; style: React.CSSProperties;
}) {
  const start = builderStart(versions, currencies);
  if (!start.open) return null;
  return (
    <>
      {start.blocked && (
        <span style={{ color: K.muted, fontSize: 12, flexBasis: "100%" }} data-testid="builder-currency-note">{blockedNote(start.blocked)}</span>
      )}
      <BuilderCurrencyPicker label={label} value={value} currencies={currencies} onChange={onChange} style={style} />
      <Button size="sm" disabled={disabled || !/^[A-Z]{3}$/.test(value)} onClick={onStart}>{startLabel}</Button>
    </>
  );
}
