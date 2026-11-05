"use client";
// web/components/settings/AddOnsView.tsx — Settings > Add-ons (AA-1036 R3, root ADR 0019 d7): the agency's add-ons with
// their state (On / Requested / Not added), the pending request with the agency's note, the last decision (a decline
// with its reason, an approval still being set up), and the owner's request with an optional note. The page is the
// owner's only (the API answers the others 403 before CIS is asked). Data: GET /crm/v1/add-ons (the server page); the
// request: POST /bff/add-on-requests (lib/bff.ts), relayed by the CRM API to CIS tenant.addons.v1. Kit: Section, Badge,
// Button, Field, Textarea.
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Badge, Button, Field, K, Section, Textarea, useHydrated } from "@/app/_kit";
import { type AddOn, type AddOnList, decisionDate, decisionKey, orderAddOns, refusalKey } from "@/lib/addOns";
import { bffSend } from "@/lib/bffClient";
import { type DateLocale, formatDate } from "@/lib/dates";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";

const small = { margin: 0, fontSize: 12.5, color: K.muted, lineHeight: 1.55 } as const;

function stateBadge(lang: Lang, state: AddOn["state"]) {
  if (state === "active") return <Badge tone="success" dot>{t(lang, "addons.state.active")}</Badge>;
  if (state === "requested") return <Badge tone="warning" dot>{t(lang, "addons.state.requested")}</Badge>;
  return <Badge tone="neutral">{t(lang, "addons.state.available")}</Badge>;
}

function AddOnCard({ addOn, inService, lang, locale }: { addOn: AddOn; inService: boolean; lang: Lang; locale: DateLocale }) {
  const router = useRouter();
  const hydrated = useHydrated();
  const tr = (key: MessageKey, vars: Record<string, string> = {}) => fmt(t(lang, key), vars);
  const date = (iso: string) => formatDate(iso, locale);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ tone: "danger" | "success"; text: string } | null>(null);
  const name = tr(addOn.product === "crm" ? "addons.crm" : "addons.planner");
  const last = addOn.last_decision;

  async function send() {
    setBusy(true);
    setMessage(null);
    const res = await bffSend("POST", "add-on-requests", { product: addOn.product, note: note.trim() || null }, { renew: true });
    setBusy(false);
    if (!res.ok) {
      if (!res.redirecting) setMessage({ tone: "danger", text: tr(refusalKey(res.status, res.detail)) });
      return;
    }
    setNote("");
    setMessage({ tone: "success", text: tr("addons.sent", { name }) });
    router.refresh();
  }

  return (
    <Section
      title={name}
      description={tr(addOn.product === "crm" ? "addons.crmAbout" : "addons.plannerAbout")}
      actions={
        <span data-testid={`addon-state-${addOn.product}`} data-state={addOn.state}>
          {stateBadge(lang, addOn.state)}
        </span>
      }
    >
      <div style={{ display: "grid", gap: 12 }} data-testid={`addon-${addOn.product}`}>
        {addOn.state === "active" && (
          <p style={small}>{tr(addOn.product === "crm" ? "addons.activeNote" : "addons.plannerActiveNote")}</p>
        )}
        {addOn.state === "requested" && addOn.request && (
          <>
            <p style={small}>{tr("addons.requestedNote", { date: date(addOn.request.requested_at) })}</p>
            {addOn.request.note && (
              <Field label={tr("addons.yourNote")}>
                <p style={{ ...small, color: K.ink, whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{addOn.request.note}</p>
              </Field>
            )}
          </>
        )}
        {last && addOn.state !== "requested" && (
          <div role="status" style={{ padding: "10px 14px", borderRadius: 10, border: `1px solid ${K.line}`, background: K.accentTint }}>
            <p style={{ ...small, color: K.ink }} data-testid={`addon-decision-${addOn.product}`}>
              {tr(decisionKey(last), { date: date(decisionDate(last)) })}
            </p>
            {last.decision_note && (
              <p style={{ ...small, marginTop: 4, overflowWrap: "anywhere" }}>
                <strong style={{ color: K.ink }}>{tr("addons.reason")}:</strong> {last.decision_note}
              </p>
            )}
          </div>
        )}
        {addOn.state === "available" && inService && (
          <>
            <Field label={tr("addons.noteLabel")}>
              <Textarea
                rows={2}
                maxLength={500}
                value={note}
                disabled={busy}
                placeholder={tr("addons.notePlaceholder")}
                onChange={(e) => {
                  setMessage(null);
                  setNote(e.target.value);
                }}
              />
            </Field>
            <span data-testid={`addon-request-${addOn.product}`}>
              <Button variant="primary" size="sm" disabled={!hydrated || busy} onClick={() => void send()}>
                {busy ? tr("addons.requesting") : tr("addons.request", { name })}
              </Button>
            </span>
          </>
        )}
        {message && (
          <p role={message.tone === "danger" ? "alert" : "status"} style={{ margin: 0, fontSize: 12.5, color: message.tone === "danger" ? K.danger : K.success }}>
            {message.text}
          </p>
        )}
      </div>
    </Section>
  );
}

export function AddOnsView({ list, lang, locale }: { list: AddOnList; lang: Lang; locale: DateLocale }) {
  return (
    <div style={{ display: "grid", gap: 12 }}>
      {!list.in_service && (
        <div
          role="status"
          data-testid="addons-not-in-service"
          style={{ padding: "10px 14px", borderRadius: 10, border: `1px solid ${K.line}`, background: K.accentTint, color: K.ink, fontSize: 13 }}
        >
          {t(lang, "addons.notInService")}
        </div>
      )}
      {orderAddOns(list.add_ons).map((a) => (
        <AddOnCard key={a.product} addOn={a} inService={list.in_service} lang={lang} locale={locale} />
      ))}
    </div>
  );
}
