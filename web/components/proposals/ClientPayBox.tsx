"use client";
// web/components/proposals/ClientPayBox.tsx — the client's pay box on an accepted proposal (AA-952, card CRM-23 slice
// 4; design v3 F6 step 5, §3.5 "Deposit link (C31, Fix A8)", §2.9 rule 5 "the hold deadline sits next to the pay
// button, and the link dies at hold expiry"). Each issued deposit / balance invoice of the version: what to pay and by
// when; while its window is open, the agency's own bank details and "I have paid" (POST
// /crm/public/v1/proposal/invoices/<id>/claim: the agency checks the transfer; Booking holds the places a little
// longer, once). Past the hold's expiry the box says the link expired and shows no details and no button (the API
// answers 410 pay_link_expired anyway, and never starts a payment session). The agency is the merchant (BYO): the
// money goes to its own account.
import { useState } from "react";
import { Button, K } from "@/app/_kit";
import { formatDate, type DateLocale } from "@/lib/dates";
import { fmt } from "@/lib/i18n";
import { PROPOSAL_API } from "@/lib/clientProposal";
import { clientErrorKey, formatAmount, type ClientInvoice } from "@/lib/proposals";

function when(value: string | null, locale: string): string {
  if (!value) return "";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString(locale, { dateStyle: "medium", timeStyle: "short" });
}

/** The i18n key of an invoice's state line and its values. */
export function payStateKey(inv: ClientInvoice): string {
  const { state, until } = inv.pay_window;
  if (state === "open") return until ? "client.pay.openUntil" : "client.pay.open";
  if (state === "claimed") return until ? "client.pay.claimedUntil" : "client.pay.claimed";
  if (state === "settling") return "client.pay.settling";
  if (state === "expired") return "client.pay.expired";
  return "client.pay.paid";
}

export function ClientPayBox({
  invoices,
  agency,
  locale,
  t,
  onDone,
}: {
  invoices: ClientInvoice[];
  agency: string;
  locale: DateLocale;
  t: (k: string) => string;
  onDone: () => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  if (invoices.length === 0) return null;

  async function claim(id: string) {
    setBusy(id);
    setError(null);
    try {
      const res = await fetch(`${PROPOSAL_API}/invoices/${encodeURIComponent(id)}/claim`, {
        method: "POST",
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      const body = (await res.json().catch(() => ({}))) as { detail?: string };
      if (!res.ok) setError(t(clientErrorKey(body.detail ?? (res.status === 429 ? "rate_limited" : "error"))));
      onDone();
    } catch {
      setError(t("client.error.other"));
    }
    setBusy(null);
  }

  return (
    <section data-testid="client-pay-box" style={{ display: "grid", gap: 10, paddingTop: 12, borderTop: `1px solid ${K.line}` }}>
      <h2 style={{ margin: 0, fontSize: 16, color: K.ink }}>{t("client.pay.title")}</h2>
      {invoices.map((inv) => {
        const state = inv.pay_window.state;
        return (
          <div key={inv.invoice_id} data-testid="client-pay-invoice" data-state={state} style={{ display: "grid", gap: 6, padding: 12, borderRadius: 10, border: `1px solid ${K.line}`, background: K.card }}>
            <strong style={{ fontSize: 14, color: K.ink, overflowWrap: "anywhere" }}>
              {fmt(t("client.pay.line"), {
                what: t(`client.kind.${inv.kind}`),
                number: inv.number,
                amount: formatAmount(inv.total_amount, inv.currency, locale),
              })}
            </strong>
            {inv.due_on && state === "open" && (
              <span style={{ fontSize: 13, color: K.muted }}>
                {fmt(t("client.pay.due"), { date: formatDate(inv.due_on, locale), amount: formatAmount(inv.balance_amount, inv.currency, locale) })}
              </span>
            )}
            <p style={{ margin: 0, fontSize: 13.5, color: state === "expired" ? K.danger : state === "paid" ? K.success : K.body }}>
              {fmt(t(payStateKey(inv)), { time: when(inv.pay_window.until, locale), agency })}
            </p>
            {inv.pay_by && (
              <div data-testid="client-pay-details" style={{ display: "grid", gap: 2, fontSize: 13.5, color: K.body }}>
                <span style={{ color: K.muted }}>{inv.pay_by.name}</span>
                <span style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{inv.pay_by.instructions}</span>
              </div>
            )}
            {inv.can_claim && (
              <div>
                <Button variant="primary" size="lg" disabled={busy !== null} onClick={() => void claim(inv.invoice_id)} testId="client-pay-claim">
                  {t("client.pay.claim")}
                </Button>
              </div>
            )}
          </div>
        );
      })}
      {error && (
        <p role="alert" data-testid="client-pay-error" style={{ margin: 0, fontSize: 13.5, color: K.danger }}>
          {error}
        </p>
      )}
    </section>
  );
}
