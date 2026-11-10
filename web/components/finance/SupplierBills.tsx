"use client";
// web/components/finance/SupplierBills.tsx — AA-1068 (crm-25): the supplier bills of a trip on its Money card. Record a
// bill of one of the agency's own suppliers (number, bill date, currency, total, a note) with the amount it pays for each
// confirmed line of that supplier on this trip (POST /supplier-bills); mark a bill paid or void it (PATCH). A recorded
// bill never changes otherwise: a wrong one is voided and recorded again. The trip's P&L then counts the billed cost
// (at the bill date's rate) instead of the planned one. Owner and agent only.
import { useRouter } from "next/navigation";
import { type FormEvent, useState } from "react";
import { Badge, Button, Field, Input, K, Select, useHydrated, useToast } from "@/app/_kit";
import { DateInput } from "@/components/DateInput";
import { bffSend } from "@/lib/bffClient";
import { formatDate, type DateLocale } from "@/lib/dates";
import { financeErrorKey, type SupplierBill } from "@/lib/finance";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import { formatMoney, type Line } from "@/lib/ledger";

const STATUS_TONE = { recorded: "info", paid: "success", void: "neutral" } as const;
const CURRENCY = /^[A-Z]{3}$/;

export function SupplierBills({
  bills,
  lines,
  lang,
  locale,
  closed,
}: {
  bills: SupplierBill[];
  lines: Line[];
  lang: Lang;
  locale: DateLocale;
  closed: boolean;
}) {
  const router = useRouter();
  const hydrated = useHydrated();
  const toast = useToast();
  const tr = (key: string) => t(lang, key as MessageKey);
  const suppliers = Array.from(new Map(lines.map((l) => [l.agency_supplier_id, l.supplier_name])).entries());
  const [open, setOpen] = useState(false);
  const [supplier, setSupplier] = useState(suppliers[0]?.[0] ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ofSupplier = lines.filter((l) => l.agency_supplier_id === supplier);

  async function record(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const data = new FormData(e.currentTarget);
    const currency = String(data.get("currency") ?? "").trim().toUpperCase();
    const parts = ofSupplier
      .map((l) => ({ booking_line_id: l.booking_line_id, amount: String(data.get(`line-${l.booking_line_id}`) ?? "").trim() }))
      .filter((p) => p.amount !== "");
    if (!CURRENCY.test(currency) || parts.length === 0) {
      setError(tr(parts.length === 0 ? "finance.error.invalid_lines" : "finance.error.other"));
      return;
    }
    setBusy(true);
    setError(null);
    const res = await bffSend("POST", "supplier-bills", {
      agency_supplier_id: supplier,
      number: String(data.get("number") ?? "").trim(),
      bill_date: String(data.get("bill_date") ?? ""),
      currency,
      amount: String(data.get("amount") ?? "").trim(),
      note: String(data.get("note") ?? "").trim() || null,
      lines: parts,
    });
    setBusy(false);
    if (!res.ok) {
      setError(tr(financeErrorKey(res.detail)));
      return;
    }
    toast.success(tr("finance.bills.saved"));
    setOpen(false);
    router.refresh();
  }

  async function change(bill: SupplierBill, status: "paid" | "void") {
    setBusy(true);
    setError(null);
    const res = await bffSend("PATCH", `supplier-bills/${bill.supplier_bill_id}`, { status });
    setBusy(false);
    if (!res.ok) {
      setError(tr(financeErrorKey(res.detail)));
      return;
    }
    toast.success(tr("finance.bills.updated"));
    router.refresh();
  }

  return (
    <div style={{ display: "grid", gap: 8 }} data-testid="supplier-bills">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <h3 style={{ margin: 0, fontSize: 13 }}>{tr("finance.bills.title")}</h3>
        {!open && lines.length > 0 && !closed && (
          <Button size="sm" variant="ghost" disabled={!hydrated} onClick={() => setOpen(true)}>
            {tr("finance.bills.add")}
          </Button>
        )}
      </div>
      {bills.length === 0 && <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("finance.bills.none")}</p>}
      {bills.map((b) => (
        <div key={b.supplier_bill_id} data-testid="supplier-bill" style={{ border: `1px solid ${K.line}`, borderRadius: 8, padding: "8px 10px", fontSize: 13, display: "grid", gap: 4 }}>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", justifyContent: "space-between" }}>
            <span style={{ minWidth: 0, overflowWrap: "anywhere" }}>
              <strong>{b.supplier_name}</strong> · {b.number} · {formatDate(b.bill_date, locale)}
            </span>
            <Badge tone={STATUS_TONE[b.status]}>{tr(`finance.bills.status.${b.status}`)}</Badge>
          </div>
          <span style={{ fontVariantNumeric: "tabular-nums" }}>
            {formatMoney(b.bill_cost.amount, b.bill_cost.currency, locale)}
            {b.bill_cost.currency !== b.bill_cost.base_currency && ` = ${formatMoney(b.bill_cost.amount_base, b.bill_cost.base_currency, locale)}`}
          </span>
          {b.lines.map((x) => (
            <span key={x.booking_line_id} style={{ fontSize: 12.5, color: K.muted }}>
              {x.line_title}: {formatMoney(x.line_cost, b.bill_cost.currency, locale)}
            </span>
          ))}
          {Number(b.bill_cost.unallocated) > 0 && (
            <span style={{ fontSize: 12.5, color: K.muted }}>{fmt(tr("finance.bills.unallocated"), { amount: formatMoney(b.bill_cost.unallocated, b.bill_cost.currency, locale) })}</span>
          )}
          {b.status !== "void" && !closed && (
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
              {b.status === "recorded" && (
                <Button size="sm" variant="ghost" disabled={!hydrated || busy} onClick={() => change(b, "paid")}>
                  {tr("finance.bills.markPaid")}
                </Button>
              )}
              <Button size="sm" variant="ghost" disabled={!hydrated || busy} onClick={() => change(b, "void")}>
                {tr("finance.bills.void")}
              </Button>
            </div>
          )}
        </div>
      ))}
      {lines.length === 0 && <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("finance.bills.noLines")}</p>}
      {open && (
        <form onSubmit={record} data-testid="supplier-bill-form" style={{ display: "grid", gap: 8, minWidth: 0 }}>
          <Field label={tr("finance.bills.supplier")}>
            <Select value={supplier} onChange={(e) => setSupplier(e.target.value)} options={suppliers.map(([id, name]) => ({ value: id, label: name }))} />
          </Field>
          <div style={{ display: "grid", gap: 8, gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 160px), 1fr))" }}>
            <Field label={tr("finance.bills.number")}>
              <Input name="number" required maxLength={80} />
            </Field>
            <DateInput locale={locale} name="bill_date" label={tr("finance.bills.date")} readAs={tr("settings.readAs")} invalid={tr("settings.invalidDate")} />
            <Field label={tr("finance.bills.currency")}>
              <Input name="currency" required maxLength={3} defaultValue={ofSupplier[0]?.money?.cost?.currency ?? "USD"} />
            </Field>
            <Field label={tr("finance.bills.amount")}>
              <Input name="amount" required inputMode="decimal" />
            </Field>
          </div>
          <Field as="div" label={tr("finance.bills.lines")}>
            <div style={{ display: "grid", gap: 6 }}>
              {ofSupplier.map((l) => (
                <label key={l.booking_line_id} style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 13, minWidth: 0 }}>
                  <span style={{ flex: "1 1 auto", minWidth: 0, overflowWrap: "anywhere" }}>{l.title}</span>
                  <Input size="sm" name={`line-${l.booking_line_id}`} inputMode="decimal" style={{ width: 120 }} aria-label={l.title} />
                </label>
              ))}
            </div>
          </Field>
          <Field label={tr("finance.bills.note")}>
            <Input name="note" maxLength={500} />
          </Field>
          {error && <p role="alert" style={{ margin: 0, fontSize: 12.5, color: K.danger }}>{error}</p>}
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <Button type="submit" size="sm" disabled={!hydrated || busy || !supplier}>
              {tr("finance.bills.save")}
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
