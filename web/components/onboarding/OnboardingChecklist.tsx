"use client";
// web/components/onboarding/OnboardingChecklist.tsx — the getting-started checklist (AA-962, card CRM-31, A-20; design
// v3 §2.7 Fix E2): the three required steps, the optional ones with "Later", the steps on later with "Show again", and
// the one-time shared-responsibility acknowledgement (errata S247). The owner marks steps; other roles read. Writes go
// through the BFF allow-list; the API decides who may (403 role_forbidden).
import { CheckCircle2, Circle, HelpCircle } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Badge, Button, K } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { bffSend } from "@/lib/bffClient";
import { formatDate, type DateLocale } from "@/lib/dates";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import { ACK_VERSION, importErrorKey, type OnboardingView, type Step } from "@/lib/onboarding";

export function OnboardingChecklist({
  view,
  owner,
  lang,
  locale,
}: {
  view: OnboardingView;
  owner: boolean;
  lang: Lang;
  locale: DateLocale;
}) {
  const router = useRouter();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const tr = (key: string) => t(lang, key as MessageKey);
  const required = view.steps.filter((s) => s.required);
  const optional = view.steps.filter((s) => !s.required && !s.later);
  const later = view.steps.filter((s) => s.later);

  async function mark(step: Step, state: "done" | "later" | "open") {
    setBusy(step.key);
    setError(null);
    const res = await bffSend("PUT", `onboarding/steps/${step.key}`, { state });
    setBusy(null);
    if (!res.ok) setError(tr(importErrorKey(res.status, res.detail)));
    else router.refresh();
  }

  async function accept() {
    setBusy("ack");
    setError(null);
    const res = await bffSend("POST", "onboarding/acknowledgement", { version: ACK_VERSION });
    setBusy(null);
    if (!res.ok) setError(tr(importErrorKey(res.status, res.detail)));
    else router.refresh();
  }

  function row(step: Step) {
    return (
      <li
        key={step.key}
        data-testid={`onb-step-${step.key}`}
        style={{ display: "flex", gap: 12, alignItems: "flex-start", padding: "10px 0", borderTop: `1px solid ${K.line}` }}
      >
        {step.done ? (
          <CheckCircle2 size={20} color={K.success} aria-label={t(lang, "onb.done")} style={{ flex: "none", marginTop: 1 }} />
        ) : (
          <Circle size={20} color={K.muted2} aria-hidden style={{ flex: "none", marginTop: 1 }} />
        )}
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
            <span style={{ fontSize: 14, color: step.done ? K.muted : K.ink, fontWeight: 500 }}>{tr(`onb.step.${step.key}`)}</span>
            {step.required && <Badge tone="accent">{t(lang, "onb.required")}</Badge>}
          </div>
          <p style={{ margin: "3px 0 0", fontSize: 13, color: K.muted }}>{tr(`onb.stepHelp.${step.key}`)}</p>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginTop: 8, alignItems: "center" }}>
            {!step.done && (
              <a href={step.href} style={{ fontSize: 13, color: K.accentDeep, fontWeight: 500 }}>
                {t(lang, "onb.go")} →
              </a>
            )}
            <a href={step.help} style={{ fontSize: 13, color: K.muted, display: "inline-flex", gap: 4, alignItems: "center" }}>
              <HelpCircle size={14} aria-hidden /> {t(lang, "onb.help")}
            </a>
            {owner && step.can_mark_done && !step.done && (
              <Button size="sm" variant="ghost" disabled={busy !== null} onClick={() => mark(step, "done")}>
                {t(lang, "onb.markDone")}
              </Button>
            )}
            {owner && step.marked_done && (
              <Button size="sm" variant="ghost" disabled={busy !== null} onClick={() => mark(step, "open")}>
                {t(lang, "onb.undoDone")}
              </Button>
            )}
            {owner && !step.required && !step.done && !step.later && (
              <Button size="sm" variant="ghost" disabled={busy !== null} onClick={() => mark(step, "later")}>
                {t(lang, "onb.later")}
              </Button>
            )}
            {owner && step.later && (
              <Button size="sm" variant="ghost" disabled={busy !== null} onClick={() => mark(step, "open")}>
                {t(lang, "onb.restore")}
              </Button>
            )}
          </div>
        </div>
      </li>
    );
  }

  const ack = view.acknowledgement;
  return (
    <div style={{ display: "grid", gap: 16 }} data-testid="onb-checklist">
      {error && (
        <p role="alert" style={{ margin: 0, color: K.danger, fontSize: 13 }}>
          {error}
        </p>
      )}
      <Panel title={t(lang, "onb.ack.title")} testId="onb-ack">
        <div style={{ display: "grid", gap: 8, fontSize: 13.5, color: K.body, maxWidth: 720 }}>
          <p style={{ margin: 0 }}>{t(lang, "onb.ack.body1")}</p>
          <p style={{ margin: 0 }}>{t(lang, "onb.ack.body2")}</p>
          <p style={{ margin: 0 }}>{t(lang, "onb.ack.body3")}</p>
          <p style={{ margin: 0, fontSize: 12, color: K.muted }}>{fmt(t(lang, "onb.ack.version"), { version: ACK_VERSION })}</p>
          <div>
            {ack.accepted && ack.accepted_at ? (
              <Badge tone="success">{fmt(t(lang, "onb.ack.accepted"), { date: formatDate(new Date(ack.accepted_at), locale) })}</Badge>
            ) : owner ? (
              <Button variant="primary" disabled={busy !== null} onClick={accept}>
                {t(lang, "onb.ack.accept")}
              </Button>
            ) : (
              <span style={{ fontSize: 13, color: K.muted }}>{t(lang, "onb.ack.ownerOnly")}</span>
            )}
          </div>
        </div>
      </Panel>
      <Panel title={t(lang, "onb.required")} testId="onb-required">
        <p style={{ margin: "0 0 4px", fontSize: 13.5, color: K.ink }} data-testid="onb-progress">
          {fmt(t(lang, "onb.progress"), { done: view.required_done, total: view.required_total })}
        </p>
        {view.complete && <p style={{ margin: "0 0 6px", fontSize: 13, color: K.muted }}>{t(lang, "onb.allDone")}</p>}
        <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>{required.map(row)}</ul>
      </Panel>
      <Panel title={t(lang, "onb.optional")} testId="onb-optional">
        <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>{optional.map(row)}</ul>
      </Panel>
      {later.length > 0 && (
        <Panel title={t(lang, "onb.laterTitle")} testId="onb-later">
          <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>{later.map(row)}</ul>
        </Panel>
      )}
      {!owner && <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{t(lang, "onb.ownerOnly")}</p>}
    </div>
  );
}
