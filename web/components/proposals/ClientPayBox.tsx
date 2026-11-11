"use client";
// web/components/proposals/ClientPayBox.tsx — the client's pay box on an accepted proposal (AA-952, card CRM-23 slice
// 4; design v3 F6 step 5, §3.5 "Deposit link (C31, Fix A8)", §2.9 rule 5 "the hold deadline sits next to the pay
// button, and the link dies at hold expiry"). Each issued deposit / balance invoice of the version: what to pay and by
// when; while its window is open, the agency's own bank details and "I have paid" (POST
// /crm/public/v1/proposal/invoices/<id>/claim: the agency checks the transfer; Booking holds the places a little
// longer, once). Past the hold's expiry the box says the link expired and shows no details and no button (the API
// answers 410 pay_link_expired anyway, and never starts a payment session). While the agency's newer booking terms wait
// for the client's re-accept (the box above it), the deposit reads "accept the updated terms first" with no details and
// no button (S261 F1; the claim answers 409 terms_reaccept_required). The agency is the merchant (BYO): the money goes
// to its own account.
// AA-1072: "Pay online" (POST .../invoices/<id>/card) when the agency connected its own gateway: the answer is the
// gateway's hosted page (the card never reaches this site); the gateway sends the client back to /c/p?payment=returned,
// where the box says the payment is being confirmed and reads the page again once.
import { useEffect, useRef, useState } from "react";
import { Button, K, useHydrated } from "@/app/_kit";
import { formatDate, type DateLocale } from "@/lib/dates";
import { fmt } from "@/lib/i18n";
import { PROPOSAL_API } from "@/lib/clientProposal";
import { clientErrorKey, formatAmount, type ClientInvoice } from "@/lib/proposals";
import { clientLineLabel } from "./ClientPaymentLines";

function when(value: string | null, locale: string): string {
  if (!value) return "";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString(locale, { dateStyle: "medium", timeStyle: "short" });
}

/** The i18n key of an invoice's state line and its values. */
export function payStateKey(inv: ClientInvoice): string {
  const { state, until } = inv.pay_window;
  if (state === "open") return until ? "client.pay.openUntil" : "client.pay.open";
  if (state === "terms") return until ? "client.pay.termsUntil" : "client.pay.terms";
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
  const hydrated = useHydrated();
  // Read after hydration only (the server has no address bar), so both renders agree.
  const returned = hydrated && new URLSearchParams(window.location.search).get("payment") === "returned";
  const reread = useRef(false);
  useEffect(() => {
    if (!returned || reread.current) return;
    // The gateway's signed notice usually lands within seconds: read the page again, once.
    const timer = window.setTimeout(() => {
      reread.current = true;
      onDone();
    }, 4000);
    return () => window.clearTimeout(timer);
  }, [returned, onDone]);
  if (invoices.length === 0) return null;

  async function payOnline(id: string) {
    setBusy(id);
    setError(null);
    try {
      const res = await fetch(`${PROPOSAL_API}/invoices/${encodeURIComponent(id)}/card`, {
        method: "POST",
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      const body = (await res.json().catch(() => ({}))) as { detail?: string; redirect_url?: string };
      if (res.ok && typeof body.redirect_url === "string" && /^https:\/\//.test(body.redirect_url)) {
        window.location.assign(body.redirect_url);
        return;
      }
      setError(t(clientErrorKey(body.detail ?? (res.status === 429 ? "rate_limited" : "error"))));
      onDone();
    } catch {
      setError(t("client.error.other"));
    }
    setBusy(null);
  }

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
      {returned && (
        <p role="status" data-testid="client-pay-returned" style={{ margin: 0, fontSize: 13.5, color: K.body }}>
          {t("client.pay.returned")}
        </p>
      )}
      {invoices.map((inv) => {
        const state = inv.pay_window.state;
        return (
          <div key={inv.invoice_id} data-testid="client-pay-invoice" data-state={state} style={{ display: "grid", gap: 6, padding: 12, borderRadius: 10, border: `1px solid ${K.line}`, background: K.card }}>
            <strong style={{ fontSize: 14, color: K.ink, overflowWrap: "anywhere" }}>
              {fmt(t("client.pay.line"), {
                what: clientLineLabel(inv.kind, inv.seq, t),
                number: inv.number,
                amount: formatAmount(inv.total_amount, inv.currency, locale),
              })}
            </strong>
            {inv.due_on && (state === "open" || state === "terms") && (
              <span style={{ fontSize: 13, color: K.muted }}>
                {fmt(t("client.pay.due"), { date: formatDate(inv.due_on, locale), amount: formatAmount(inv.balance_amount, inv.currency, locale) })}
              </span>
            )}
            <p data-testid="client-pay-state" style={{ margin: 0, fontSize: 13.5, color: state === "expired" ? K.danger : state === "paid" ? K.success : state === "terms" ? K.amber : K.body }}>
              {fmt(t(payStateKey(inv)), { time: when(inv.pay_window.until, locale), agency })}
            </p>
            {inv.pay_by && (
              <div data-testid="client-pay-details" style={{ display: "grid", gap: 2, fontSize: 13.5, color: K.body }}>
                <span style={{ color: K.muted }}>{inv.pay_by.name}</span>
                <span style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{inv.pay_by.instructions}</span>
              </div>
            )}
            {inv.pay_online && (
              <div style={{ display: "grid", gap: 4 }}>
                <span data-testid="client-pay-online">
                  <Button variant="primary" size="lg" disabled={busy !== null} onClick={() => void payOnline(inv.invoice_id)}>
                    {busy === inv.invoice_id ? t("client.pay.onlineBusy") : t("client.pay.online")}
                  </Button>
                </span>
                <span style={{ fontSize: 12.5, color: K.muted }}>{fmt(t("client.pay.onlineHint"), { agency })}</span>
              </div>
            )}
            {inv.can_claim && (
              <div>
                <Button variant={inv.pay_online ? "secondary" : "primary"} size="lg" disabled={busy !== null} onClick={() => void claim(inv.invoice_id)} testId="client-pay-claim">
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
