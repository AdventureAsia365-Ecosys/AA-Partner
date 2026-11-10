"use client";
// web/components/finance/DepartureCosts.tsx — AA-1068 (crm-32): the costs of one group departure and the form to add
// one (POST /departure-costs: what, which of the agency's own suppliers, amount, currency, split per departure or per
// traveller, the day; the API freezes the exchange rate of that day) or delete one (DELETE /departure-costs/{id}).
// Owner and agent only.
import { useRouter } from "next/navigation";
import { type FormEvent, useState } from "react";
import { Button, Field, Input, K, Select, useHydrated, useToast } from "@/app/_kit";
import { DateInput } from "@/components/DateInput";
import { bffSend } from "@/lib/bffClient";
import { formatDate, type DateLocale } from "@/lib/dates";
import { type DepartureCost, financeErrorKey } from "@/lib/finance";
import { t, type Lang, type MessageKey } from "@/lib/i18n";
import { formatMoney, type Supplier } from "@/lib/ledger";

export function DepartureCosts({
  departureRef,
  costs,
  suppliers,
  lang,
  locale,
}: {
  departureRef: string;
  costs: DepartureCost[];
  suppliers: Supplier[];
  lang: Lang;
  locale: DateLocale;
}) {
  const router = useRouter();
  const hydrated = useHydrated();
  const toast = useToast();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function add(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const data = new FormData(e.currentTarget);
    setBusy(true);
    setError(null);
    const res = await bffSend("POST", "departure-costs", {
      departure_ref: departureRef,
      agency_supplier_id: String(data.get("supplier") ?? ""),
      title: String(data.get("title") ?? "").trim(),
      amount: String(data.get("amount") ?? "").trim(),
      currency: String(data.get("currency") ?? "").trim().toUpperCase(),
      basis: String(data.get("basis") ?? "per_departure"),
      cost_date: String(data.get("cost_date") ?? "") || null,
    });
    setBusy(false);
    if (!res.ok) {
      setError(tr(financeErrorKey(res.detail)));
      return;
    }
    toast.success(tr("departure.saved"));
    setOpen(false);
    router.refresh();
  }

  async function remove(cost: DepartureCost) {
    setBusy(true);
    setError(null);
    const res = await bffSend("DELETE", `departure-costs/${cost.departure_cost_id}`);
    setBusy(false);
    if (!res.ok) {
      setError(tr(financeErrorKey(res.detail)));
      return;
    }
    toast.success(tr("departure.deleted"));
    router.refresh();
  }

  return (
    <div style={{ display: "grid", gap: 8 }} data-testid="departure-cost-list">
      {costs.length === 0 && <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{tr("departure.noCosts")}</p>}
      {costs.map((c) => (
        <div key={c.departure_cost_id} data-testid="departure-cost" style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", justifyContent: "space-between", fontSize: 13, borderBottom: `1px solid ${K.line}`, paddingBottom: 6 }}>
          <span style={{ minWidth: 0, overflowWrap: "anywhere" }}>
            <strong>{c.title}</strong> · {c.supplier_name} · {tr(`departure.basis.${c.basis}`)} · {formatDate(c.cost_date, locale)}
          </span>
          <span style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <span style={{ fontVariantNumeric: "tabular-nums" }}>{formatMoney(c.group_cost.amount, c.group_cost.currency, locale)}</span>
            <Button size="sm" variant="ghost" disabled={!hydrated || busy} onClick={() => remove(c)}>
              {tr("departure.delete")}
            </Button>
          </span>
        </div>
      ))}
      {suppliers.length === 0 && <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("departure.noSuppliers")}</p>}
      {!open && suppliers.length > 0 && (
        <div>
          <Button size="sm" variant="ghost" disabled={!hydrated} onClick={() => setOpen(true)}>
            {tr("departure.add")}
          </Button>
        </div>
      )}
      {open && (
        <form onSubmit={add} data-testid="departure-cost-form" style={{ display: "grid", gap: 8, minWidth: 0 }}>
          <div style={{ display: "grid", gap: 8, gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 180px), 1fr))" }}>
            <Field label={tr("departure.what")}>
              <Input name="title" required maxLength={120} />
            </Field>
            <Field label={tr("departure.supplier")}>
              <Select name="supplier" options={suppliers.map((s) => ({ value: s.agency_supplier_id, label: s.name }))} />
            </Field>
            <Field label={tr("departure.amount")}>
              <Input name="amount" required inputMode="decimal" />
            </Field>
            <Field label={tr("departure.currency")}>
              <Input name="currency" required maxLength={3} defaultValue="USD" />
            </Field>
            <Field label={tr("departure.basis")}>
              <Select
                name="basis"
                options={[
                  { value: "per_departure", label: tr("departure.basis.per_departure") },
                  { value: "per_pax", label: tr("departure.basis.per_pax") },
                ]}
              />
            </Field>
            <DateInput locale={locale} name="cost_date" label={tr("departure.date")} readAs={tr("settings.readAs")} invalid={tr("settings.invalidDate")} />
          </div>
          {error && <p role="alert" style={{ margin: 0, fontSize: 12.5, color: K.danger }}>{error}</p>}
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <Button type="submit" size="sm" disabled={!hydrated || busy}>
              {tr("departure.save")}
            </Button>
            <Button type="button" size="sm" variant="ghost" onClick={() => setOpen(false)}>
              {tr("finance.bills.close")}
            </Button>
          </div>
        </form>
      )}
      {!open && error && <p role="alert" style={{ margin: 0, fontSize: 12.5, color: K.danger }}>{error}</p>}
    </div>
  );
}
