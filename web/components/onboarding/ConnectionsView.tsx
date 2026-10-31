"use client";
// web/components/onboarding/ConnectionsView.tsx — Settings > Connections and privacy (AA-962, card CRM-31; errata
// S247 §2): every bring-your-own connection with its status in the four states, who pays for it and where it is set
// up (status codes only: no credential, account reference or address ever reaches the page), and the controller
// settings form (privacy-notice link, data-request contact, consent wording; owner).
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Badge, Button, Field, Input, K, Textarea } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { bffSend } from "@/lib/bffClient";
import { t, type Lang, type MessageKey } from "@/lib/i18n";
import { connectionTone, importErrorKey, type OnboardingView } from "@/lib/onboarding";

export function ConnectionsView({ view, owner, lang }: { view: OnboardingView; owner: boolean; lang: Lang }) {
  const router = useRouter();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [url, setUrl] = useState(view.controller.privacy_notice_url ?? "");
  const [contact, setContact] = useState(view.controller.data_request_contact ?? "");
  const [consent, setConsent] = useState(view.controller.consent_text ?? "");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setMessage(null);
    const res = await bffSend("PUT", "onboarding/controller", {
      privacy_notice_url: url.trim() || null,
      data_request_contact: contact.trim() || null,
      consent_text: consent.trim() || null,
    });
    setBusy(false);
    if (!res.ok) setMessage({ ok: false, text: tr(importErrorKey(res.status, res.detail)) });
    else {
      setMessage({ ok: true, text: t(lang, "onb.ctl.saved") });
      router.refresh();
    }
  }

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <Panel title={t(lang, "onb.conn.title")} testId="onb-connections">
        <p style={{ margin: "0 0 10px", fontSize: 13, color: K.muted }}>{t(lang, "onb.conn.subtitle")}</p>
        <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
          {view.connections.map((c) => (
            <li
              key={c.key}
              data-testid={`onb-conn-${c.key}`}
              style={{ padding: "12px 0", borderTop: `1px solid ${K.line}`, display: "grid", gap: 6 }}
            >
              <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
                <span style={{ fontSize: 14, fontWeight: 500, color: K.ink }}>{tr(`onb.conn.${c.key}`)}</span>
                <Badge tone={connectionTone(c.status)}>{tr(`onb.conn.state.${c.status}`)}</Badge>
                {c.required && <Badge tone="accent">{t(lang, "onb.conn.required")}</Badge>}
                {c.bank_transfer && <Badge tone="info">{t(lang, "onb.conn.bank")}</Badge>}
              </div>
              <span style={{ fontSize: 12.5, color: K.muted }}>{tr(`onb.conn.who.${c.key}`)}</span>
              <span style={{ display: "flex", gap: 14, flexWrap: "wrap", fontSize: 13 }}>
                {c.href ? (
                  <a href={c.href} style={{ color: K.accentDeep, fontWeight: 500 }}>
                    {t(lang, "onb.conn.setup")} →
                  </a>
                ) : (
                  <span style={{ color: K.muted }}>{t(lang, "onb.conn.soon")}</span>
                )}
                <a href={c.help} style={{ color: K.muted }}>
                  {t(lang, "onb.help")}
                </a>
              </span>
            </li>
          ))}
        </ul>
      </Panel>
      <section id="privacy">
        <Panel title={t(lang, "onb.ctl.title")} testId="onb-controller">
          <p style={{ margin: "0 0 12px", fontSize: 13, color: K.muted }}>{t(lang, "onb.ctl.subtitle")}</p>
          <form onSubmit={save} style={{ display: "grid", gap: 12, maxWidth: 620 }}>
            <Field label={t(lang, "onb.ctl.url")} hint={t(lang, "onb.ctl.urlHint")} locked={!owner}>
              <Input
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                placeholder="https://"
                maxLength={500}
                disabled={!owner}
                inputMode="url"
              />
            </Field>
            <Field label={t(lang, "onb.ctl.contact")} hint={t(lang, "onb.ctl.contactHint")} locked={!owner}>
              <Input value={contact} onChange={(e) => setContact(e.target.value)} maxLength={320} disabled={!owner} />
            </Field>
            <Field label={t(lang, "onb.ctl.consent")} hint={t(lang, "onb.ctl.consentHint")} locked={!owner}>
              <Textarea value={consent} onChange={(e) => setConsent(e.target.value)} maxLength={2000} rows={3} disabled={!owner} />
            </Field>
            {owner ? (
              <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
                <Button type="submit" variant="primary" disabled={busy}>
                  {t(lang, "onb.ctl.save")}
                </Button>
                {message && (
                  <span role={message.ok ? "status" : "alert"} style={{ fontSize: 13, color: message.ok ? K.muted : K.danger }}>
                    {message.text}
                  </span>
                )}
              </div>
            ) : (
              <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{t(lang, "onb.ctl.ownerOnly")}</p>
            )}
          </form>
        </Panel>
      </section>
    </div>
  );
}
