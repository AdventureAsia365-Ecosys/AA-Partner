// web/components/aaTariff/AaTermsPanel.tsx — AA-1188: "Booking conditions with AA" on the AA prices page (lib/aaTerms.ts,
// GET /crm/v1/aa/terms, live from AA): payment, cancellation fees, quote rules and AA's approved clauses. Plain text
// in a kit Section; no price, no margin. A failed read shows one error line and never hides the prices above it.
import { K, Section } from "@/app/_kit";
import { cancellationLines, paymentLines, quoteRuleLines, type AaTerms } from "@/lib/aaTerms";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";

function Lines({ title, lines, testId }: { title: string; lines: string[]; testId: string }) {
  if (lines.length === 0) return null;
  return (
    <div data-testid={testId} style={{ minWidth: 0 }}>
      <div style={{ fontSize: 13, fontWeight: 600, color: K.ink, marginBottom: 4 }}>{title}</div>
      <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13, color: K.body, lineHeight: 1.6 }}>
        {lines.map((line) => (
          <li key={line} style={{ overflowWrap: "anywhere" }}>
            {line}
          </li>
        ))}
      </ul>
    </div>
  );
}

export function AaTermsPanel({ terms, lang }: { terms: AaTerms | null; lang: Lang }) {
  const tr = (key: string) => t(lang, key as MessageKey);
  return (
    <Section title={tr("aaTerms.title")} description={tr("aaTerms.subtitle")} testId="aa-terms" bodyStyle={{ display: "grid", gap: 14 }}>
      {terms === null ? (
        <p role="status" data-testid="aa-terms-error" style={{ margin: 0, fontSize: 13, color: K.muted }}>
          {tr("aaTerms.error")}
        </p>
      ) : (
        <>
          {terms.holds_paused && (
            <p role="status" data-testid="aa-terms-holds-paused" style={{ margin: 0, fontSize: 13, color: K.amberDeep }}>
              {tr("aaTerms.holdsPaused")}
            </p>
          )}
          <div style={{ display: "grid", gap: 14, gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 260px), 1fr))" }}>
            <Lines title={tr("aaTerms.payment")} lines={paymentLines(terms, lang)} testId="aa-terms-payment" />
            <Lines title={tr("aaTerms.cancellation")} lines={cancellationLines(terms, lang)} testId="aa-terms-cancellation" />
            <Lines title={tr("aaTerms.quotes")} lines={quoteRuleLines(terms, lang)} testId="aa-terms-quotes" />
          </div>
          {terms.clauses.length > 0 && (
            <div data-testid="aa-terms-clauses" style={{ display: "grid", gap: 8 }}>
              <div style={{ fontSize: 13, fontWeight: 600, color: K.ink }}>{tr("aaTerms.clauses")}</div>
              {terms.clauses.map((c) => (
                <div key={c.code} style={{ minWidth: 0 }}>
                  <div style={{ fontSize: 13, fontWeight: 600, color: K.ink3 }}>{c.title}</div>
                  <p style={{ margin: "2px 0 0", fontSize: 13, color: K.body, lineHeight: 1.6, whiteSpace: "pre-line", overflowWrap: "anywhere" }}>
                    {c.body}
                  </p>
                  {c.free_cancel_days !== null && (
                    <p style={{ margin: "2px 0 0", fontSize: 12, color: K.muted }}>{fmt(tr("aaTerms.freeCancel"), { days: c.free_cancel_days })}</p>
                  )}
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </Section>
  );
}
