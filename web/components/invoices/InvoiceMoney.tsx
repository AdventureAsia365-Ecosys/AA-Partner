"use client";
// web/components/invoices/InvoiceMoney.tsx — the money of one invoice on the trip's Invoices card (card CRM-23 slice
// 3; design v3 M7, A-03 Payments): paid / refunded / still owed, every payment and refund entry, "Record payment"
// (partial payments; the money reached the agency's own account outside the CRM: bank transfer, cash, its own card
// terminal), "Request refund" on a payment (never more than is left to ask back), and the refund requests with their
// next step (RefundActions). Owner and agent move money; the assistant reads.
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Button, Field, Input, K, Select, useHydrated } from "@/app/_kit";
import { DateInput } from "@/components/DateInput";
import { RefundActions } from "@/components/invoices/RefundActions";
import { bffSend } from "@/lib/bffClient";
import { formatDate, type DateLocale } from "@/lib/dates";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import { canBill, invoiceErrorKey, type Invoice } from "@/lib/invoices";
import { amountOf, formatAmount, PAY_METHODS } from "@/lib/proposals";

export function InvoiceMoney({
  invoice,
  role,
  lang,
  locale,
  closed = false,
  readOnly = false,
  onMessage,
}: {
  invoice: Invoice;
  role: string;
  lang: Lang;
  locale: DateLocale;
  /** S262: the trip is cancelled: a payment whose refund the cancel already proposed offers no second request. */
  closed?: boolean;
  /** S262 round 3: the trip's cancel run is being applied (TripClosed "updating"): amounts only, no money action. */
  readOnly?: boolean;
  onMessage: (tone: "danger" | "success", text: string) => void;
}) {
  const router = useRouter();
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const bills = canBill(role) && !readOnly;
  const [busy, setBusy] = useState(false);
  const [paying, setPaying] = useState(false);
  const [method, setMethod] = useState("bank_transfer");
  const [refunding, setRefunding] = useState<string | null>(null);
  const payments = invoice.payments ?? [];
  const refunds = invoice.refund_requests ?? [];
  const money = (v: string | undefined) => formatAmount(v ?? null, invoice.currency, locale);
  const refundOpen = (paymentId: string) => refunds.some((r) => r.payment_id === paymentId && (r.status === "proposed" || r.status === "approved"));

  async function post(path: string, body: unknown, done: string): Promise<boolean> {
    setBusy(true);
    const res = await bffSend("POST", path, body);
    setBusy(false);
    if (!res.ok) {
      onMessage("danger", tr(invoiceErrorKey(res.detail)));
      return false;
    }
    onMessage("success", tr(done));
    router.refresh();
    return true;
  }

  async function recordPayment(form: HTMLFormElement) {
    const data = new FormData(form);
    const amount = amountOf(String(data.get("amount") ?? ""));
    if (!amount) {
      onMessage("danger", tr("invoice.error.invalid_amount_value"));
      return;
    }
    const day = String(data.get("received_on") ?? "");
    const reference = String(data.get("reference") ?? "").trim();
    const body = { amount, method, received_on: day || null, reference: reference || null };
    if (await post(`invoices/${invoice.invoice_id}/payments`, body, "payment.recorded")) setPaying(false);
  }

  async function requestRefund(paymentId: string, form: HTMLFormElement) {
    const data = new FormData(form);
    const amount = amountOf(String(data.get("amount") ?? ""));
    const reason = String(data.get("reason") ?? "").trim();
    if (!amount || !reason) {
      onMessage("danger", tr("refund.error.form"));
      return;
    }
    if (await post(`payments/${paymentId}/refund-requests`, { amount, reason }, "refund.requested")) setRefunding(null);
  }

  if (invoice.status === "draft" || invoice.status === "void") return null;
  return (
    <div data-testid="invoice-money" style={{ display: "grid", gap: 6, paddingLeft: 10, borderLeft: `2px solid ${K.line}` }}>
      <p style={{ margin: 0, fontSize: 12.5, color: K.muted }} data-testid="invoice-balance">
        {fmt(tr("payment.summary"), { paid: money(invoice.paid_amount), refunded: money(invoice.refunded_amount), balance: money(invoice.balance_amount) })}
      </p>
      {payments.map((p) => (
        <div key={p.payment_id} data-testid="payment-row" data-kind={p.entry_kind} style={{ display: "grid", gap: 4, fontSize: 13 }}>
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <span>{tr(p.entry_kind === "refund" ? "payment.refundEntry" : "payment.entry")}</span>
            <strong>{money(p.amount)}</strong>
            <span style={{ color: K.muted }}>
              {formatDate(p.received_on, locale)} · {tr(`proposal.method.${p.method}`)}
              {p.reference ? ` · ${p.reference}` : ""}
            </span>
            {bills && p.entry_kind === "payment" && Number(p.refundable_amount ?? 0) > 0 && refunding !== p.payment_id && !(closed && refundOpen(p.payment_id)) && (
              <Button size="sm" variant="ghost" disabled={!hydrated || busy} onClick={() => setRefunding(p.payment_id)}>
                {tr("refund.request")}
              </Button>
            )}
          </div>
          {refunding === p.payment_id && (
            <form
              data-testid="refund-request-form"
              style={{ display: "flex", gap: 8, alignItems: "end", flexWrap: "wrap" }}
              onSubmit={(e) => {
                e.preventDefault();
                void requestRefund(p.payment_id, e.currentTarget);
              }}
            >
              <Field label={tr("refund.amountLabel")} hint={fmt(tr("refund.upTo"), { amount: money(p.refundable_amount) })}>
                <Input size="sm" name="amount" inputMode="decimal" defaultValue={p.refundable_amount} style={{ width: 130 }} />
              </Field>
              <Field label={tr("refund.reasonLabel")} style={{ flex: "1 1 220px", minWidth: 0 }}>
                <Input size="sm" name="reason" maxLength={500} />
              </Field>
              <Button type="submit" size="sm" disabled={!hydrated || busy}>
                {tr("refund.submit")}
              </Button>
              <Button type="button" size="sm" variant="ghost" onClick={() => setRefunding(null)}>
                {tr("trip.cancel")}
              </Button>
            </form>
          )}
        </div>
      ))}
      {refunds.map((r) => (
        <RefundActions key={r.refund_id} refund={r} role={role} lang={lang} locale={locale} onMessage={onMessage} readOnly={readOnly} />
      ))}
      {bills && invoice.actions.includes("record_payment") && !paying && (
        <div>
          <Button size="sm" disabled={!hydrated || busy} onClick={() => setPaying(true)}>
            {tr("payment.record")}
          </Button>
        </div>
      )}
      {bills && paying && (
        <form
          data-testid="payment-form"
          style={{ display: "grid", gap: 8 }}
          onSubmit={(e) => {
            e.preventDefault();
            void recordPayment(e.currentTarget);
          }}
        >
          <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("payment.help")}</p>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <Field label={tr("payment.amount")}>
              <Input size="sm" name="amount" inputMode="decimal" defaultValue={invoice.balance_amount} style={{ width: 140 }} />
            </Field>
            <Field label={tr("proposal.method")}>
              <Select size="sm" value={method} onChange={(e) => setMethod(e.target.value)} options={PAY_METHODS.map((m) => ({ value: m, label: tr(`proposal.method.${m}`) }))} />
            </Field>
          </div>
          <DateInput locale={locale} name="received_on" label={tr("proposal.receivedOn")} help={tr("proposal.receivedOnHelp")} readAs={tr("settings.readAs")} invalid={tr("settings.invalidDate")} />
          <Field label={tr("payment.reference")} hint={tr("payment.referenceHint")}>
            <Input size="sm" name="reference" maxLength={120} />
          </Field>
          <div style={{ display: "flex", gap: 8 }}>
            <Button type="submit" size="sm" disabled={!hydrated || busy}>
              {tr("payment.confirm")}
            </Button>
            <Button type="button" size="sm" variant="ghost" onClick={() => setPaying(false)}>
              {tr("trip.cancel")}
            </Button>
          </div>
        </form>
      )}
    </div>
  );
}
