"use client";
// web/components/proposals/TermsReaccept.tsx — "terms updated after you saw them" on an accepted proposal (AA-955, card
// CRM-25; design v3 §2.11 E: re-accept before payment). The agency published newer booking terms after the client
// accepted; until the client accepts them here, the agency cannot record the deposit. The client reads the new
// version, ticks it, types their full name; the page sends the version id and its SHA-256 as shown (409
// terms_changed: the page reloads).
import { useState } from "react";
import { Button, K } from "@/app/_kit";
import { fmt } from "@/lib/i18n";
import { PROPOSAL_API } from "@/lib/clientProposal";
import { clientErrorKey } from "@/lib/proposals";

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

export function TermsReaccept({
  terms,
  agency,
  t,
  onDone,
}: {
  terms: { doc_id: string; version_label: string; body_text: string; body_sha256: string };
  agency: string;
  t: (k: string) => string;
  onDone: () => void;
}) {
  const [name, setName] = useState("");
  const [agree, setAgree] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ready = !busy && agree && name.trim().length > 0;

  async function send() {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`${PROPOSAL_API}/terms`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ full_name: name, accept_terms: agree, doc_id: terms.doc_id, doc_sha256: terms.body_sha256 }),
      });
      const body = (await res.json().catch(() => ({}))) as { detail?: string };
      if (res.ok || body.detail === "nothing_to_accept" || body.detail === "terms_changed") onDone();
      if (!res.ok) setError(t(clientErrorKey(body.detail ?? (res.status === 429 ? "rate_limited" : "error"))));
    } catch {
      setError(t("client.error.other"));
    }
    setBusy(false);
  }

  return (
    <section data-testid="client-terms-reaccept" style={{ display: "grid", gap: 8, paddingTop: 12, borderTop: `1px solid ${K.line}` }}>
      <h2 style={{ margin: 0, fontSize: 16, color: K.ink }}>{fmt(t("client.termsUpdatedTitle"), { version: terms.version_label })}</h2>
      <p style={{ margin: 0, fontSize: 13.5, color: K.body }}>{t("client.termsUpdatedIntro")}</p>
      <p style={{ margin: 0, fontSize: 13.5, color: K.body, whiteSpace: "pre-wrap", overflowWrap: "anywhere", maxHeight: 260, overflowY: "auto" }}>{terms.body_text}</p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void send();
        }}
        style={{ display: "grid", gap: 8 }}
      >
        <label style={{ display: "flex", gap: 8, alignItems: "flex-start", fontSize: 14 }}>
          <input type="checkbox" checked={agree} onChange={(e) => setAgree(e.target.checked)} data-testid="client-reaccept-agree" />
          <span>{fmt(t("client.agreeTerms"), { agency, version: terms.version_label })}</span>
        </label>
        <label style={{ display: "grid", gap: 4, fontSize: 14 }}>
          {t("client.fullName")}
          <input autoComplete="name" value={name} onChange={(e) => setName(e.target.value)} style={box} data-testid="client-reaccept-name" />
        </label>
        {error && (
          <p role="alert" style={{ margin: 0, fontSize: 13.5, color: K.danger }}>
            {error}
          </p>
        )}
        <Button type="submit" variant="primary" size="lg" disabled={!ready} testId="client-reaccept">
          {t("client.acceptNewTerms")}
        </Button>
      </form>
    </section>
  );
}
