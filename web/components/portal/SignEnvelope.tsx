"use client";
// web/components/portal/SignEnvelope.tsx — e-sign v1 in the traveller portal (AA-955, card CRM-25; design v3 C-08
// "terms + waiver + insurance decline in one envelope", O-6 per-adult signing, guardian for minors). The documents come
// from the API with the merge fields filled in (the signing facts as placeholders); the signer sends back each
// document's id and SHA-256 exactly as shown, the insurance choice when the agency asks it, and for a child the
// child's traveller id (the lead signs as guardian). A minor's own portal shows who signs for them. The section keeps
// the v0 test ids (portal-waiver, portal-sign-*) for the traveller's own envelope. AA-1012 (CRM-25b): a signed
// envelope (one's own, or a minor's one signed as guardian) downloads as a PDF the platform seal key signed
// (`<base>/agreements/<traveller>/pdf`, the documents answer shape).
import { useState } from "react";
import { Button, K, LinkButton } from "@/app/_kit";
import { formatDate, type DateLocale } from "@/lib/dates";
import { fmt } from "@/lib/i18n";
import { saveFile, signBody, type InsuranceChoice, type PortalAgreements, type PortalEnvelope, type SignedPdf } from "@/lib/proposals";

type Step = (method: string, path: string, body: unknown, done: (data: Record<string, unknown>) => void) => Promise<void>;

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
const section: React.CSSProperties = { display: "grid", gap: 8, paddingTop: 12, borderTop: `1px solid ${K.line}` };
const docText: React.CSSProperties = {
  margin: 0,
  fontSize: 13.5,
  color: K.body,
  whiteSpace: "pre-wrap",
  overflowWrap: "anywhere",
  maxHeight: 220,
  overflowY: "auto",
  padding: "8px 10px",
  border: `1px solid ${K.line}`,
  borderRadius: 8,
};

/** The signed PDF of one envelope; the HTTP status and the refusal code when it fails. */
async function downloadSigned(base: string, travelerId: string): Promise<{ status: number; detail: string | null }> {
  try {
    const res = await fetch(`${base}/agreements/${encodeURIComponent(travelerId)}/pdf`, { credentials: "same-origin", cache: "no-store" });
    if (!res.ok) {
      const body = (await res.json().catch(() => ({}))) as { detail?: unknown };
      return { status: res.status, detail: typeof body.detail === "string" ? body.detail : null };
    }
    saveFile((await res.json()) as SignedPdf);
    return { status: 200, detail: null };
  } catch {
    return { status: 0, detail: null };
  }
}

export function SignEnvelope({
  base,
  box: agreements,
  locked,
  busy,
  step,
  hint,
  onCode,
  onSigned,
  onError,
  t,
  locale,
}: {
  /** The portal API base on this origin (/crm/public/v1/traveller); without it no PDF link is shown. */
  base?: string;
  box: PortalAgreements;
  locked: boolean;
  busy: boolean;
  step: Step;
  hint: string | null;
  onCode: (data: Record<string, unknown>) => void;
  onSigned: () => void;
  /** A refused PDF download: the HTTP status and the API's refusal code. */
  onError?: (status: number, detail: string | null) => void;
  t: (k: string) => string;
  locale: DateLocale;
}) {
  const own = agreements.envelopes.find((e) => e.for === "self");
  const open = agreements.envelopes.filter((e) => !e.signed);
  const [active, setActive] = useState<string | null>(open[0]?.traveler_id ?? null);
  const [insurance, setInsurance] = useState<InsuranceChoice | null>(null);
  const [name, setName] = useState("");
  const [agree, setAgree] = useState(false);
  const [code, setCode] = useState("");
  const [asked, setAsked] = useState(false);
  const [fetching, setFetching] = useState<string | null>(null);
  const current = agreements.envelopes.find((e) => e.traveler_id === active && !e.signed) ?? open[0] ?? null;
  const needsInsurance = Boolean(agreements.insurance_declination);
  const ready = !busy && agree && name.trim() && code.trim().length === 6 && (!needsInsurance || insurance);

  function label(e: PortalEnvelope): string {
    return e.for === "self" ? t("client.envelope.self") : fmt(t("client.envelope.minor"), { n: e.minor_number ?? 1 });
  }

  function signedLine(e: PortalEnvelope): string {
    const date = e.signed_at ? formatDate(e.signed_at.slice(0, 10), locale) : "";
    return fmt(t(e.signer_role === "guardian" && e.for === "self" ? "client.signedByGuardian" : "client.signedOn"), { date });
  }

  return (
    <section data-testid="portal-waiver" data-signed={own?.signed ? "true" : "false"} style={section}>
      <h2 style={{ margin: 0, fontSize: 16, color: K.ink }}>{t("client.agreementsTitle")}</h2>
      {agreements.guardian_signs_for_me ? (
        <p style={{ margin: 0, fontSize: 14, color: K.body }} data-testid="portal-guardian-signs">
          {t("client.guardianSignsForMe")}
        </p>
      ) : (
        <p style={{ margin: 0, fontSize: 13.5, color: K.muted }}>{t("client.agreementsIntro")}</p>
      )}
      {agreements.documents.map((d) => (
        <div key={d.doc_id} style={{ display: "grid", gap: 4 }} data-testid={`portal-doc-${d.kind}`}>
          <h3 style={{ margin: 0, fontSize: 14.5, color: K.ink }}>{fmt(t(`client.doc.${d.kind}`), { version: d.version_label })}</h3>
          <p style={docText}>{d.body_text}</p>
        </div>
      ))}

      {agreements.envelopes.map((e) => (
        <p key={e.traveler_id} style={{ margin: 0, fontSize: 14, color: e.signed ? K.success : K.body }} data-testid={e.for === "self" ? (e.signed ? "portal-signed" : "portal-envelope-self") : `portal-envelope-minor-${e.minor_number}`}>
          {label(e)}: {e.signed ? signedLine(e) : t("client.waiverMissing")}
          {e.signed && e.insurance_choice ? ` · ${t(`client.insurance.${e.insurance_choice}`)}` : ""}
          <SignedPdfLink base={base} envelope={e} busy={fetching !== null} onBusy={setFetching} onError={onError} t={t} />
          {!e.signed && !locked && open.length > 1 && e.traveler_id !== current?.traveler_id && (
            <>
              {" "}
              <LinkButton
                size="inherit"
                testId={`portal-sign-for-${e.minor_number ?? "self"}`}
                onClick={() => {
                  setActive(e.traveler_id);
                  setInsurance(null);
                  setAgree(false);
                  setCode("");
                }}
              >
                {t("client.signFor")}
              </LinkButton>
            </>
          )}
        </p>
      ))}

      {current && locked && <p style={{ margin: 0, fontSize: 14, color: K.body }}>{t("client.stage3Locked")}</p>}
      {current && !locked && (
        <div style={{ display: "grid", gap: 8 }} data-testid="portal-sign-form" data-for={current.for}>
          <h3 style={{ margin: 0, fontSize: 14.5, color: K.ink }}>{label(current)}</h3>
          {needsInsurance && agreements.insurance_declination && (
            <fieldset style={{ border: "none", margin: 0, padding: 0, display: "grid", gap: 6 }}>
              <legend style={{ fontSize: 14, color: K.ink, marginBottom: 4 }}>{t("client.insuranceQuestion")}</legend>
              {(["insured", "declined"] as const).map((choice) => (
                <label key={choice} style={{ display: "flex", gap: 8, alignItems: "flex-start", fontSize: 14 }}>
                  <input type="radio" name="insurance" checked={insurance === choice} onChange={() => setInsurance(choice)} data-testid={`portal-insurance-${choice}`} />
                  <span>{t(`client.insurance.${choice}`)}</span>
                </label>
              ))}
              {insurance === "declined" && (
                <div style={{ display: "grid", gap: 4 }} data-testid="portal-doc-insurance_declination">
                  <h4 style={{ margin: 0, fontSize: 14, color: K.ink }}>
                    {fmt(t("client.doc.insurance_declination"), { version: agreements.insurance_declination.version_label })}
                  </h4>
                  <p style={docText}>{agreements.insurance_declination.body_text}</p>
                </div>
              )}
            </fieldset>
          )}
          <Button
            variant="secondary"
            size="lg"
            disabled={busy}
            testId="portal-sign-code"
            onClick={() =>
              step("POST", "/code", { purpose: "sign" }, (d) => {
                setAsked(true);
                onCode(d);
              })
            }
          >
            {t("client.sendSignCode")}
          </Button>
          {asked && hint && (
            <p data-testid="portal-sign-hint" style={{ margin: 0, fontSize: 13.5, color: K.body }}>
              {hint}
            </p>
          )}
          <form
            onSubmit={(ev) => {
              ev.preventDefault();
              void step("POST", "/sign", signBody(agreements, current, { code, fullName: name, agree, insurance }), () => {
                setCode("");
                setAgree(false);
                setAsked(false);
                onSigned();
              });
            }}
            style={{ display: "grid", gap: 8 }}
          >
            <label style={{ display: "flex", gap: 8, alignItems: "flex-start", fontSize: 14 }}>
              <input type="checkbox" checked={agree} onChange={(ev) => setAgree(ev.target.checked)} data-testid="portal-sign-agree" />
              <span>{t(current.for === "minor" ? "client.envelopeAgreeGuardian" : "client.envelopeAgree")}</span>
            </label>
            <label style={{ display: "grid", gap: 4, fontSize: 14 }}>
              {t("client.fullName")}
              <input autoComplete="name" value={name} onChange={(ev) => setName(ev.target.value)} style={box} data-testid="portal-sign-name" />
            </label>
            <label style={{ display: "grid", gap: 4, fontSize: 14 }}>
              {t("client.code")}
              <input inputMode="numeric" maxLength={6} value={code} onChange={(ev) => setCode(ev.target.value)} style={box} data-testid="portal-sign-code-input" />
            </label>
            <Button type="submit" variant="primary" size="lg" disabled={!ready} testId="portal-sign">
              {t("client.sign")}
            </Button>
          </form>
        </div>
      )}
    </section>
  );
}

/** " · Download PDF" after a signed envelope (AA-1012); nothing for an unsigned one or without the API base. */
function SignedPdfLink({
  base,
  envelope,
  busy,
  onBusy,
  onError,
  t,
}: {
  base?: string;
  envelope: PortalEnvelope;
  busy: boolean;
  onBusy: (travelerId: string | null) => void;
  onError?: (status: number, detail: string | null) => void;
  t: (k: string) => string;
}) {
  if (!envelope.signed || !base) return null;
  const api = base;
  async function fetchPdf() {
    onBusy(envelope.traveler_id);
    const res = await downloadSigned(api, envelope.traveler_id);
    onBusy(null);
    if (res.status !== 200) onError?.(res.status, res.detail);
  }
  return (
    <>
      {" · "}
      <LinkButton size="inherit" disabled={busy} testId={envelope.for === "self" ? "portal-signed-pdf" : `portal-signed-pdf-minor-${envelope.minor_number}`} onClick={() => void fetchPdf()}>
        {t("client.downloadSignedPdf")}
      </LinkButton>
    </>
  );
}
