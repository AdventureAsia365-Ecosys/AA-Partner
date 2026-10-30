"use client";
// web/components/invoices/InvoiceSettingsForm.tsx — Settings > Payments, the invoice part (card CRM-23 slice 2;
// design v3 WF-12 "service fee off", R27): the number prefix, the tax name, rate and whether prices include it, the
// default days until an invoice is due, and service-fee invoices (off by default). Owner only (the page shows other
// roles why). A change applies to invoices raised afterwards; an issued invoice keeps what it was issued with.
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Button, Checkbox, Field, Input, K, useHydrated } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { bffSend } from "@/lib/bffClient";
import { t, type Lang, type MessageKey } from "@/lib/i18n";
import { invoiceErrorKey, type InvoiceSettings } from "@/lib/invoices";

export function InvoiceSettingsForm({ settings, lang }: { settings: InvoiceSettings; lang: Lang }) {
  const router = useRouter();
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [form, setForm] = useState({
    prefix: settings.invoice_prefix,
    label: settings.invoice_tax_label,
    rate: settings.invoice_tax_rate,
    inclusive: settings.invoice_tax_inclusive,
    days: String(settings.invoice_due_days),
    serviceFee: settings.service_fee_enabled,
  });
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ tone: "danger" | "success"; text: string } | null>(null);

  async function save(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const days = Number(form.days);
    const rate = form.rate.trim();
    if (!/^\d{1,3}(\.\d{1,3})?$/.test(rate) || Number(rate) > 100) {
      setMessage({ tone: "danger", text: tr("invoice.error.invalid_tax_rate") });
      return;
    }
    if (!Number.isInteger(days) || days < 0 || days > 365) {
      setMessage({ tone: "danger", text: tr("invoice.error.due_days") });
      return;
    }
    setBusy(true);
    setMessage(null);
    const res = await bffSend("PUT", "settings/invoices", {
      invoice_prefix: form.prefix.trim().toUpperCase(),
      invoice_tax_label: form.label.trim(),
      invoice_tax_rate: rate,
      invoice_tax_inclusive: form.inclusive,
      invoice_due_days: days,
      service_fee_enabled: form.serviceFee,
    });
    setBusy(false);
    if (!res.ok) {
      setMessage({ tone: "danger", text: tr(invoiceErrorKey(res.detail)) });
      return;
    }
    setMessage({ tone: "success", text: tr("invoice.settings.saved") });
    router.refresh();
  }

  return (
    <Panel title={tr("invoice.settings.title")} testId="invoice-settings">
      <form onSubmit={save} data-hydrated={hydrated ? "true" : undefined} style={{ display: "grid", gap: 10 }}>
        <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("invoice.settings.help")}</p>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
          <Field label={tr("invoice.settings.prefix")} hint={tr("invoice.settings.prefixHint")}>
            <Input size="sm" value={form.prefix} maxLength={12} style={{ width: 130 }} onChange={(e) => setForm({ ...form, prefix: e.target.value.toUpperCase() })} />
          </Field>
          <Field label={tr("invoice.settings.taxLabel")}>
            <Input size="sm" value={form.label} maxLength={24} style={{ width: 130 }} onChange={(e) => setForm({ ...form, label: e.target.value })} />
          </Field>
          <Field label={tr("invoice.settings.taxRate")}>
            <Input size="sm" inputMode="decimal" value={form.rate} style={{ width: 100 }} onChange={(e) => setForm({ ...form, rate: e.target.value })} />
          </Field>
          <Field label={tr("invoice.settings.dueDays")}>
            <Input size="sm" inputMode="numeric" value={form.days} style={{ width: 90 }} onChange={(e) => setForm({ ...form, days: e.target.value })} />
          </Field>
        </div>
        <Checkbox label={tr("invoice.settings.inclusive")} checked={form.inclusive} onChange={(e) => setForm({ ...form, inclusive: e.target.checked })} />
        <Checkbox label={tr("invoice.settings.serviceFee")} checked={form.serviceFee} onChange={(e) => setForm({ ...form, serviceFee: e.target.checked })} />
        {message && (
          <p role="status" style={{ margin: 0, fontSize: 13, color: message.tone === "danger" ? K.danger : K.success }}>
            {message.text}
          </p>
        )}
        <div>
          <Button type="submit" size="sm" disabled={!hydrated || busy}>
            {tr("invoice.settings.save")}
          </Button>
        </div>
      </form>
    </Panel>
  );
}
