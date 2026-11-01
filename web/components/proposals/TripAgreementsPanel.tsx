"use client";
// web/components/proposals/TripAgreementsPanel.tsx — the trip's signed documents (AA-955, card CRM-25; design v3 M12
// "100% signed before departure", R34, R13 decline): per traveller whether their envelope is signed, by them or by the
// lead as guardian, the insurance choice and the seal; the booking-terms acceptances and the re-accept state (the
// deposit waits for it); the signing log (codes sent and refused). "View sealed document" is a reveal: a reason is
// asked first and the API writes one reveal event. Names come from the trip's party (GET /trips/{id}); the API sends
// ids, codes and hashes only. AA-957 S262 round 3 (C12 §2.7): on a cancelled trip nothing waits for a signature (the
// signed envelopes and the log stay readable).
import { useState } from "react";
import { Badge, Button, K, Modal, useHydrated } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { useTripClosed } from "@/components/pipeline/TripClosed";
import { bffSend } from "@/lib/bffClient";
import { REVEAL_REASONS } from "@/lib/clients";
import { formatDate, type DateLocale } from "@/lib/dates";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import { proposalErrorKey, type SealedEnvelope, type TripAgreements } from "@/lib/proposals";

const select: React.CSSProperties = {
  font: "inherit",
  fontSize: 13,
  padding: "5px 8px",
  borderRadius: 8,
  border: `1px solid ${K.line2}`,
  background: K.card,
  color: K.ink,
};

export function TripAgreementsPanel({
  view,
  party,
  lang,
  locale,
  closed: closedProp = false,
}: {
  view: TripAgreements;
  party: { traveler_id: string; display_name: string }[];
  lang: Lang;
  locale: DateLocale;
  /** The trip is cancelled or being cancelled. */
  closed?: boolean;
}) {
  const hydrated = useHydrated();
  const closed = useTripClosed(closedProp);
  const tr = (key: string) => t(lang, key as MessageKey);
  const [opening, setOpening] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [shown, setShown] = useState<SealedEnvelope | null>(null);
  const name = (id: string | null) => party.find((p) => p.traveler_id === id)?.display_name ?? tr("esign.notSigned");
  const day = (iso: string) => formatDate(iso.slice(0, 10), locale);

  async function open(requestId: string) {
    setBusy(true);
    setError(null);
    const res = await bffSend<SealedEnvelope>("POST", `signatures/${requestId}/envelope`, { reason });
    setBusy(false);
    if (!res.ok) {
      setError(tr(proposalErrorKey(res.detail)));
      return;
    }
    setShown(res.data);
    setOpening(null);
    setReason("");
  }

  return (
    <Panel title={tr("esign.title")} testId="trip-agreements">
      <div data-hydrated={hydrated ? "true" : undefined} data-all-signed={view.all_signed ? "true" : "false"} style={{ display: "grid", gap: 10, minWidth: 0 }}>
        <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("esign.intro")}</p>
        {error && (
          <p role="alert" style={{ margin: 0, fontSize: 13, color: K.danger }}>
            {error}
          </p>
        )}
        {!view.waiver_required ? (
          <p style={{ margin: 0, fontSize: 13, color: K.body }}>{tr("esign.notRequired")}</p>
        ) : (
          <>
            {closed && !view.all_signed ? (
              <p data-testid="esign-closed" style={{ margin: 0, fontSize: 13, color: K.muted }}>
                {tr("esign.closed")}
              </p>
            ) : (
              <Badge tone={view.all_signed ? "success" : "neutral"}>{tr(view.all_signed ? "esign.allSigned" : "esign.pending")}</Badge>
            )}
            {view.travellers.map((x) => {
              const sig = x.signature;
              return (
                <div key={x.traveler_id} data-testid="esign-row" data-signed={sig ? "true" : "false"} style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", fontSize: 13, padding: "6px 0", borderTop: `1px solid ${K.line}` }}>
                  <strong style={{ overflowWrap: "anywhere" }}>{name(x.traveler_id)}</strong>
                  <span style={{ color: K.muted }}>{tr(`proposal.role.${x.role}`)}</span>
                  {!sig ? (
                    <Badge tone="neutral">{tr(closed ? "esign.notNeeded" : "esign.notSigned")}</Badge>
                  ) : (
                    <>
                      <Badge tone="success">
                        {fmt(tr(sig.signer_role === "guardian" ? "esign.signedGuardian" : "esign.signedSelf"), { date: day(sig.signed_at) })}
                      </Badge>
                      {sig.insurance_choice && <span style={{ color: K.body }}>{tr(`esign.insurance.${sig.insurance_choice}`)}</span>}
                      {sig.seal_alg && <span style={{ color: K.muted }}>{tr(sig.seal_alg.startsWith("dev-") ? "esign.sealDev" : "esign.sealKms")}</span>}
                      {sig.envelope_sha256 &&
                        (opening === sig.request_id ? (
                          <>
                            <select aria-label={tr("esign.reason")} value={reason} onChange={(e) => setReason(e.target.value)} style={select} data-testid="esign-reason">
                              <option value="">{tr("esign.reason")}</option>
                              {REVEAL_REASONS.map((r) => (
                                <option key={r} value={r}>
                                  {tr(`why.${r}`)}
                                </option>
                              ))}
                            </select>
                            <Button size="sm" variant="secondary" disabled={!reason || busy} onClick={() => void open(sig.request_id)}>
                              {tr("esign.view")}
                            </Button>
                          </>
                        ) : (
                          <Button size="sm" variant="ghost" disabled={!hydrated} onClick={() => setOpening(sig.request_id)}>
                            {tr("esign.view")}
                          </Button>
                        ))}
                    </>
                  )}
                </div>
              );
            })}
          </>
        )}

        <section style={{ display: "grid", gap: 4 }}>
          <h3 style={{ margin: 0, fontSize: 13, color: K.ink }}>{tr("esign.terms")}</h3>
          {view.terms.published && <span style={{ fontSize: 13 }}>{fmt(tr("esign.termsPublished"), { version: view.terms.published.version_label })}</span>}
          {view.terms.reaccept_required && !closed && (
            <p data-testid="esign-reaccept" style={{ margin: 0, fontSize: 13, color: K.danger }}>
              {tr("esign.termsReaccept")}
            </p>
          )}
          {view.terms.acceptances.length > 0 && (
            <ul style={{ margin: 0, paddingLeft: 18, fontSize: 12.5, color: K.body }}>
              {view.terms.acceptances.map((a) => (
                <li key={a.acceptance_id}>
                  {a.version_label} · {tr(`esign.source.${a.source}`)} · {day(a.accepted_at)}
                </li>
              ))}
            </ul>
          )}
        </section>

        {view.events.length > 0 && (
          <section style={{ display: "grid", gap: 4 }}>
            <h3 style={{ margin: 0, fontSize: 13, color: K.ink }}>{tr("esign.log")}</h3>
            <ul style={{ margin: 0, paddingLeft: 18, fontSize: 12.5, color: K.body }}>
              {view.events.slice(0, 20).map((e, i) => (
                <li key={`${e.occurred_at}-${i}`}>
                  {day(e.occurred_at)} · {name(e.traveler_id)} · {fmt(tr(`esign.event.${e.kind}`), { reason: e.reason ?? "" })}
                </li>
              ))}
            </ul>
          </section>
        )}
      </div>

      <Modal open={shown !== null} onClose={() => setShown(null)} title={tr("esign.title")} width={720} testId="esign-envelope">
        {shown && (
          <div style={{ display: "grid", gap: 8 }}>
            <p style={{ margin: 0, fontSize: 13, color: shown.seal.verified === false ? K.danger : K.success }}>
              {tr(shown.seal.verified === true ? "esign.verified" : shown.seal.verified === false ? "esign.notVerified" : "esign.notChecked")}
            </p>
            <pre style={{ margin: 0, fontSize: 12, whiteSpace: "pre-wrap", overflowWrap: "anywhere", maxHeight: 420, overflowY: "auto", color: K.ink }}>{shown.envelope_text}</pre>
            <div>
              <Button size="sm" variant="ghost" onClick={() => setShown(null)}>
                {tr("esign.close")}
              </Button>
            </div>
          </div>
        )}
      </Modal>
    </Panel>
  );
}
