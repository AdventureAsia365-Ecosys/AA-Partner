"use client";
// web/components/clients/RevealField.tsx — one masked F1 field with Reveal (AA-937, card CRM-09; design v3 §4.4,
// ui-mockups "Travellers: identity fields hidden, each view is logged"). Pressing Reveal asks why (a fixed reason
// code); Show calls POST /bff/travelers/{id}/reveal {field, reason}, which writes one audit event. The value stays in
// this component's state only (never in the URL, storage or a log) and is gone on the next page load.
// The buttons stay disabled until React hydrates (a click on the server HTML is lost, AA-936 E2E run 40060691620);
// stable test ids: show-{field} wraps Reveal, reveal-reason-{field} the reason, reveal-show-{field} wraps Show.
import { useState } from "react";
import { Button, K, useHydrated } from "@/app/_kit";
import { bffSend } from "@/lib/bffClient";
import { MASK, REVEAL_REASONS, revealText, type RevealField as Field } from "@/lib/clients";
import { type Lang, type MessageKey, t } from "@/lib/i18n";

const select: React.CSSProperties = {
  font: "inherit",
  fontSize: 12.5,
  padding: "4px 6px",
  borderRadius: 8,
  border: `1px solid ${K.line2}`,
  background: K.card,
  color: K.ink,
  maxWidth: "100%",
};

export function RevealField({
  travelerId,
  field,
  filled,
  lang,
  label,
}: {
  travelerId: string;
  field: Field;
  filled: boolean;
  lang: Lang;
  label?: string;
}) {
  const [asking, setAsking] = useState(false);
  const [reason, setReason] = useState("");
  const [value, setValue] = useState<string | null>(null);
  const [error, setError] = useState(false);
  const [busy, setBusy] = useState(false);
  const hydrated = useHydrated();

  async function show() {
    setBusy(true);
    setError(false);
    const res = await bffSend<{ value: unknown }>("POST", `travelers/${travelerId}/reveal`, { field, reason });
    setBusy(false);
    if (!res.ok) {
      setError(true);
      return;
    }
    setValue(revealText(lang, field, res.data.value));
    setAsking(false);
  }

  return (
    <span data-testid={`reveal-${field}`} style={{ display: "inline-flex", flexWrap: "wrap", gap: 6, alignItems: "center", minWidth: 0 }}>
      {label && <span style={{ fontSize: 12, color: K.muted }}>{label}</span>}
      {value !== null ? (
        <code data-testid={`revealed-${field}`} style={{ fontSize: 12.5, overflowWrap: "anywhere" }}>
          {value}
        </code>
      ) : !filled ? (
        <span style={{ fontSize: 12.5, color: K.muted }}>—</span>
      ) : (
        <>
          <span aria-hidden style={{ fontSize: 12.5, color: K.muted, letterSpacing: 1 }}>
            {MASK}
          </span>
          {!asking ? (
            <span data-testid={`show-${field}`} style={{ display: "contents" }}>
              <Button size="sm" variant="ghost" disabled={!hydrated} onClick={() => setAsking(true)}>
                {t(lang, "clients.reveal")}
              </Button>
            </span>
          ) : (
            <>
              <select aria-label={t(lang, "clients.why")} value={reason} onChange={(e) => setReason(e.target.value)} style={select} data-testid={`reveal-reason-${field}`}>
                <option value="">{t(lang, "clients.why")}</option>
                {REVEAL_REASONS.map((r) => (
                  <option key={r} value={r}>
                    {t(lang, `why.${r}` as MessageKey)}
                  </option>
                ))}
              </select>
              <span data-testid={`reveal-show-${field}`} style={{ display: "contents" }}>
                <Button size="sm" variant="secondary" disabled={!reason || busy} onClick={() => void show()}>
                  {t(lang, "clients.show")}
                </Button>
              </span>
              <Button size="sm" variant="ghost" onClick={() => setAsking(false)}>
                {t(lang, "clients.cancel")}
              </Button>
            </>
          )}
        </>
      )}
      {error && (
        <span role="alert" style={{ fontSize: 12, color: K.danger }}>
          {t(lang, "trip.error")}
        </span>
      )}
    </span>
  );
}
