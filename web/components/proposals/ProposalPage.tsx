"use client";
// web/components/proposals/ProposalPage.tsx — the client side of C-03 (thin) + C-04 accept (AA-946, card CRM-18;
// design v3 §2.4, F6 steps 3 and 5, §2.9 rules 3 and 5). Loads the proposal of a signed link from the API's public
// route on this origin, shows the text, the total with the deposit and the balance apart, and the agency's booking
// terms; the client ticks the terms, types their full name and accepts with the price hash the page showed (409
// price_changed: the page reloads the new version). After acceptance the deposit / balance schedule shows.
// AA-950 (card CRM-21b): the page reads /crm/public/v1/proposal with the host-only link cookie the /c/p/<token> landing
// set (Fix B11; same-origin credentials, the token never reaches page script); a day-by-day version renders
// ClientBuilderProposal (C-03 Keep / Swap, C-04 hold + accept, C-12).
import { useEffect, useState } from "react";
import { K } from "@/app/_kit";
import { formatDate, type DateLocale } from "@/lib/dates";
import { fmt, type MessageKey } from "@/lib/i18n";
import { isBuilderPage, PROPOSAL_API, type AnyClientPage } from "@/lib/clientProposal";
import { clientErrorKey, formatAmount, type PublicProposal } from "@/lib/proposals";
import { ClientBuilderProposal } from "./ClientBuilderProposal";

type Load = { state: "loading" } | { state: "ready"; page: AnyClientPage } | { state: "failed"; status: number };

const box: React.CSSProperties = {
  font: "inherit",
  fontSize: 14,
  padding: "8px 10px",
  borderRadius: 8,
  border: `1px solid ${K.line}`,
  background: K.card,
  color: K.ink,
  width: "100%",
  boxSizing: "border-box",
};

export function ProposalPage({ dict, locale }: { dict: Partial<Record<MessageKey, string>>; locale: DateLocale }) {
  const t = (k: string) => dict[k as MessageKey] ?? k;
  const [load, setLoad] = useState<Load>({ state: "loading" });
  const [name, setName] = useState("");
  const [agree, setAgree] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [round, setRound] = useState(0);
  const url = PROPOSAL_API;

  useEffect(() => {
    let live = true;
    fetch(url, { headers: { Accept: "application/json" }, cache: "no-store", credentials: "same-origin" })
      .then(async (res) => {
        if (!live) return;
        setLoad(res.ok ? { state: "ready", page: (await res.json()) as AnyClientPage } : { state: "failed", status: res.status });
      })
      .catch(() => live && setLoad({ state: "failed", status: 0 }));
    return () => {
      live = false;
    };
  }, [url, round]);

  async function accept(page: PublicProposal) {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`${url}/accept`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ full_name: name, accept_terms: agree, price_hash: page.price_hash, terms_doc_id: page.terms?.doc_id ?? null }),
      });
      const body = (await res.json().catch(() => ({}))) as { detail?: string };
      if (res.ok) setRound((r) => r + 1);
      else {
        setError(t(clientErrorKey(body.detail ?? (res.status === 429 ? "rate_limited" : "error"))));
        if (body.detail === "price_changed" || body.detail === "terms_changed") setRound((r) => r + 1);
      }
    } catch {
      setError(t("client.error.other"));
    }
    setBusy(false);
  }

  if (load.state === "loading") return <p style={{ margin: 0, fontSize: 14, color: K.muted }}>{t("client.loading")}</p>;
  if (load.state === "failed") {
    return (
      <p data-testid="proposal-unavailable" data-status={load.status} style={{ margin: 0, fontSize: 14, color: K.body }}>
        {t(load.status === 404 ? "client.error.link_not_found" : load.status === 429 ? "client.error.rate_limited" : "client.error.other")}
      </p>
    );
  }
  if (isBuilderPage(load.page)) {
    return <ClientBuilderProposal key={`${load.page.version}:${load.page.status}`} initial={load.page} dict={dict} locale={locale} />;
  }
  const page: PublicProposal = load.page;
  const accepted = page.status === "accepted";
  return (
    <div data-testid="client-proposal" data-status={page.status} style={{ display: "grid", gap: 14 }}>
      <div style={{ display: "grid", gap: 4 }}>
        <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{fmt(t("client.preparedBy"), { agency: page.agency_name })}</p>
        <h1 style={{ margin: 0, fontSize: 20, color: K.ink, overflowWrap: "anywhere" }}>{page.trip.title}</h1>
        {page.trip.start_date && (
          <p style={{ margin: 0, fontSize: 13, color: K.body }}>
            {formatDate(page.trip.start_date, locale)}
            {page.trip.end_date ? ` – ${formatDate(page.trip.end_date, locale)}` : ""}
          </p>
        )}
      </div>
      <p style={{ margin: 0, fontSize: 14, color: K.body, whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{page.body}</p>
      <dl data-testid="client-amounts" style={{ margin: 0, display: "grid", gridTemplateColumns: "auto 1fr", gap: "6px 12px", fontSize: 14 }}>
        <dt style={{ color: K.muted }}>{t("client.total")}</dt>
        <dd style={{ margin: 0, color: K.ink, fontWeight: 600 }}>{formatAmount(page.total_amount, page.currency, locale)}</dd>
        <dt style={{ color: K.muted }}>{t("client.deposit")}</dt>
        <dd style={{ margin: 0, color: K.ink }}>{formatAmount(page.deposit_amount, page.currency, locale)}</dd>
        {page.balance_due_on && (
          <>
            <dt style={{ color: K.muted }}>{t("client.balance")}</dt>
            <dd style={{ margin: 0, color: K.ink }}>
              {fmt(t("client.balanceDue"), { amount: formatAmount(page.balance_amount, page.currency, locale), date: formatDate(page.balance_due_on, locale) })}
            </dd>
          </>
        )}
      </dl>
      {accepted ? (
        <div data-testid="client-accepted" style={{ display: "grid", gap: 8 }}>
          <p style={{ margin: 0, fontSize: 14, color: K.success }}>{fmt(t("client.acceptedThanks"), { agency: page.agency_name })}</p>
          {page.schedule.map((s) => (
            <p key={s.kind} style={{ margin: 0, fontSize: 13.5, color: K.body }}>
              {fmt(t(`client.scheduleRow.${s.status}`), {
                what: t(`client.kind.${s.kind}`),
                amount: formatAmount(s.amount, s.currency, locale),
                date: formatDate(s.due_on, locale),
              })}
            </p>
          ))}
        </div>
      ) : (
        <form
          data-testid="client-accept-form"
          onSubmit={(e) => {
            e.preventDefault();
            void accept(page);
          }}
          style={{ display: "grid", gap: 10 }}
        >
          {page.terms && (
            <details style={{ fontSize: 13, color: K.body }}>
              <summary>{fmt(t("client.readTerms"), { version: page.terms.version_label })}</summary>
              <p style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere", maxHeight: 240, overflowY: "auto" }}>{page.terms.body_text}</p>
            </details>
          )}
          <label style={{ display: "flex", gap: 8, alignItems: "flex-start", fontSize: 14 }}>
            <input type="checkbox" name="accept_terms" checked={agree} onChange={(e) => setAgree(e.target.checked)} />
            <span>{page.terms ? fmt(t("client.agreeTerms"), { agency: page.agency_name, version: page.terms.version_label }) : t("client.agreeProposal")}</span>
          </label>
          <label style={{ display: "grid", gap: 4, fontSize: 14 }}>
            {t("client.fullName")}
            <input name="full_name" autoComplete="name" value={name} onChange={(e) => setName(e.target.value)} style={box} />
          </label>
          {error && (
            <p role="alert" data-testid="client-error" style={{ margin: 0, fontSize: 13.5, color: K.danger }}>
              {error}
            </p>
          )}
          <button
            type="submit"
            disabled={busy || !agree || !name.trim()}
            data-testid="client-accept"
            style={{ padding: "12px 16px", borderRadius: 999, border: "none", background: K.accent, color: "var(--aa-on-accent)", fontSize: 15, fontWeight: 600, cursor: "pointer", opacity: busy || !agree || !name.trim() ? 0.5 : 1 }}
          >
            {t("client.accept")}
          </button>
        </form>
      )}
    </div>
  );
}
