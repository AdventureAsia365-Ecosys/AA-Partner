"use client";
// web/components/proposals/OptionPricesEditor.tsx — AA-1066 (crm-048, item crm-05): the price per traveller type of
// one builder option (adult, child, infant, single room supplement): quantity and sell each, plus net each for an
// agency item (owner / agent; an AA item's net is AA's quote). "Fill from the suggestion" asks the API for its grid
// (the AA suggestion after the agency's price rules, shared per head; else the current sell), "Price as one amount"
// removes the rows (the sell stays their sum). Every write goes through the builder's own write (seq, conflicts).
import { useState } from "react";
import { Button, Field, Input, K, Select, useHydrated } from "@/app/_kit";
import type { BuilderOption } from "@/lib/builder";
import { draftRows, PAX_TYPES, pricesBody, type DraftRow, type PaxType } from "@/lib/retailPricing";

export function OptionPricesEditor({
  option,
  busy,
  tr,
  onSave,
  onClose,
}: {
  option: BuilderOption;
  busy: boolean;
  tr: (key: string) => string;
  /** The PUT body (``prices`` rows, ``[]`` to remove them, or ``use_suggested``); resolves true when saved. */
  onSave: (body: Record<string, unknown>) => Promise<boolean>;
  onClose: () => void;
}) {
  const hydrated = useHydrated();
  const withNet = option.source === "agency" && option.net_amount !== undefined;
  const [rows, setRows] = useState<DraftRow[]>(() => draftRows(option.prices ?? [], option.suggested_prices ?? []));
  const [error, setError] = useState<string | null>(null);
  const unused = PAX_TYPES.filter((p) => !rows.some((r) => r.pax_type === p));

  function set(i: number, patch: Partial<DraftRow>) {
    setRows(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  }

  async function save() {
    const body = pricesBody(rows, withNet);
    if (!body) {
      setError(tr("pricing.invalid"));
      return;
    }
    setError(null);
    if (await onSave({ prices: body })) onClose();
  }

  return (
    <div data-testid="option-prices" style={{ display: "grid", gap: 8, padding: 10, borderRadius: 8, background: K.bg, width: "100%" }}>
      <strong style={{ fontSize: 13 }}>{tr("pricing.perTraveller")}</strong>
      <span style={{ fontSize: 12.5, color: K.muted }}>{tr("pricing.perTravellerHint")}</span>
      {rows.map((r, i) => (
        <div key={r.pax_type} data-testid="option-price-row" data-pax={r.pax_type} style={{ display: "grid", gap: 8, gridTemplateColumns: "repeat(auto-fit, minmax(110px, 1fr))", alignItems: "end" }}>
          <Field label={tr("pricing.type")}>
            <Select
              size="sm"
              value={r.pax_type}
              onChange={(e) => set(i, { pax_type: e.target.value as PaxType })}
              options={[r.pax_type, ...unused].map((p) => ({ value: p, label: tr(`pricing.pax.${p}`) }))}
            />
          </Field>
          <Field label={tr("pricing.qty")}>
            <Input size="sm" inputMode="numeric" value={r.qty} onChange={(e) => set(i, { qty: e.target.value })} />
          </Field>
          <Field label={tr("pricing.unitSell")}>
            <Input size="sm" inputMode="decimal" value={r.unit_sell} onChange={(e) => set(i, { unit_sell: e.target.value })} />
          </Field>
          {withNet && (
            <Field label={tr("pricing.unitNet")}>
              <Input size="sm" inputMode="decimal" value={r.unit_net} onChange={(e) => set(i, { unit_net: e.target.value })} />
            </Field>
          )}
          <span>
            <Button size="sm" variant="ghost" disabled={rows.length <= 1} onClick={() => setRows(rows.filter((_, j) => j !== i))}>
              ✕
            </Button>
          </span>
        </div>
      ))}
      {error && (
        <p role="status" style={{ margin: 0, fontSize: 12.5, color: K.danger }}>
          {error}
        </p>
      )}
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
        {unused.length > 0 && (
          <Button size="sm" variant="ghost" onClick={() => setRows([...rows, { pax_type: unused[0], qty: "1", unit_sell: "0", unit_net: "" }])}>
            + {tr(`pricing.pax.${unused[0]}`)}
          </Button>
        )}
        <span data-testid="option-prices-suggested">
          <Button size="sm" disabled={!hydrated || busy} onClick={async () => { if (await onSave({ use_suggested: true })) onClose(); }}>
            {tr("pricing.useSuggested")}
          </Button>
        </span>
        <span data-testid="option-prices-save">
          <Button size="sm" variant="primary" disabled={!hydrated || busy} onClick={() => void save()}>
            {tr("pricing.save")}
          </Button>
        </span>
        {(option.prices ?? []).length > 0 && (
          <Button size="sm" variant="ghost" disabled={!hydrated || busy} onClick={async () => { if (await onSave({ prices: [] })) onClose(); }}>
            {tr("pricing.remove")}
          </Button>
        )}
        <Button size="sm" variant="ghost" onClick={onClose}>
          {tr("pricing.close")}
        </Button>
      </div>
    </div>
  );
}
