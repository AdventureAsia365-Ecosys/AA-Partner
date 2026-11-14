"use client";
// web/components/settings/AiSettingsView.tsx — Settings > AI (AA-964, card CRM-33a): the AI switch and the monthly
// budget (owner), this month's meter with the 75 / 90 / 100 % alert (design R5), the test-mode note while the
// environment runs the test model (saying whether the privacy check is still billed), the plan state when the plan
// lacks the AI assistant (AI drafts are its only use; the trip card's wording), and the rules of AI drafts (AA-1033: "AI can make mistakes" first, the kit notice
// as it shows under a draft, and a link to the terms' AI section). Writes go through the BFF allow-list (lib/bff.ts).
import { useRouter } from "next/navigation";
import { useState } from "react";
import Link from "next/link";
import { AiNotice, Badge, Button, Checkbox, Field, Input, K, useHydrated } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { aiErrorKey, budgetError, meterTone, meterWidth, notOnPlan, testModeNote, usd, type AiSettings } from "@/lib/ai";
import { bffSend } from "@/lib/bffClient";
import type { DateLocale } from "@/lib/dates";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import { CRM_TERMS_AI_HREF } from "@/lib/legal";

const BAR: Record<ReturnType<typeof meterTone>, string> = { success: K.success, warning: K.amber, danger: K.danger };

export function AiSettingsView({ settings, lang, locale }: { settings: AiSettings; lang: Lang; locale: DateLocale }) {
  const router = useRouter();
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [enabled, setEnabled] = useState(settings.ai_enabled);
  const [budget, setBudget] = useState(settings.ai_budget_usd);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ tone: "danger" | "success"; text: string } | null>(null);
  const tone = meterTone(settings.budget_used_pct);
  const note = testModeNote(settings);
  const noPlan = notOnPlan(settings);
  // AA-1096 S280: the ceiling is AA's limit for this agency (platform setting or the agency's own), not a plan limit
  const ceiling = usd(settings.budget_max_usd, locale);

  async function save(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const error = budgetError(budget, settings.budget_max_usd);
    if (error) {
      setMessage({ tone: "danger", text: fmt(tr(error), { max: ceiling }) });
      return;
    }
    setBusy(true);
    setMessage(null);
    const res = await bffSend("PUT", "settings/ai", { ai_enabled: enabled, ai_budget_usd: budget.trim() });
    setBusy(false);
    if (!res.ok) {
      setMessage({ tone: "danger", text: fmt(tr(aiErrorKey(res.detail)), { max: ceiling }) });
      return;
    }
    setMessage({ tone: "success", text: tr("ai.saved") });
    router.refresh();
  }

  return (
    <div style={{ display: "grid", gap: 14 }}>
      {note && (
        <Panel testId="ai-test-mode">
          <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
            <Badge tone="info">{tr("ai.testMode")}</Badge>
            <span style={{ fontSize: 13, color: K.muted }}>
              {tr(note === "free" ? "ai.testModeHelp" : "ai.testModeHelpScrub")}
            </span>
          </div>
        </Panel>
      )}
      {noPlan && (
        <p role="status" data-testid="ai-no-plan" style={{ margin: 0, fontSize: 13, color: K.amber }}>
          {tr("aiDraft.noPlan")}
          {settings.can_edit && <> {tr("ai.noPlanSwitch")}</>}
        </p>
      )}
      {settings.alert_level && (
        <p
          role="status"
          data-testid="ai-alert"
          style={{ margin: 0, fontSize: 13, color: settings.alert_level === 100 ? K.danger : K.amber }}
        >
          {tr(`ai.alert.${settings.alert_level}`)}
        </p>
      )}

      <Panel title={tr("ai.meter")} testId="ai-meter">
        <div style={{ display: "grid", gap: 8 }}>
          <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
            <span style={{ fontSize: 13, color: K.muted }}>{tr("ai.status")}</span>
            <Badge tone={settings.ai_enabled && !noPlan ? "success" : "neutral"}>
              {tr(settings.ai_enabled ? "ai.on" : "ai.off")}
            </Badge>
            {noPlan && (
              <span data-testid="ai-status-no-plan">
                <Badge tone="warning">{tr("ai.notOnPlan")}</Badge>
              </span>
            )}
          </div>
          <div
            role="meter"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(meterWidth(settings.budget_used_pct))}
            aria-label={tr("ai.meter")}
            style={{ height: 8, borderRadius: 999, background: K.line, overflow: "hidden" }}
          >
            <div style={{ width: `${meterWidth(settings.budget_used_pct)}%`, height: "100%", background: BAR[tone] }} />
          </div>
          <span style={{ fontSize: 13, color: K.ink }} data-testid="ai-meter-value">
            {fmt(tr("ai.meterValue"), {
              spent: usd(settings.spent_usd_month, locale),
              budget: usd(settings.ai_budget_usd, locale),
              pct: settings.budget_used_pct,
            })}
          </span>
          <span style={{ fontSize: 12.5, color: K.muted }} data-testid="ai-rate-limit">
            {/* 0 turns the per-minute limit off (crm.rate.ai_per_minute), it never blocks AI */}
            {settings.rate_limit_per_minute > 0 ? fmt(tr("ai.limit"), { n: settings.rate_limit_per_minute }) : tr("ai.limitOff")}
          </span>
        </div>
      </Panel>

      <Panel title={tr("ai.title")} testId="ai-settings-form">
        {settings.can_edit ? (
          <form onSubmit={save} data-hydrated={hydrated ? "true" : undefined} style={{ display: "grid", gap: 12 }}>
            <div style={{ display: "grid", gap: 4 }}>
              <Checkbox label={tr("ai.enable")} checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
              <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("ai.enableHelp")}</p>
            </div>
            <Field label={tr("ai.budget")} hint={fmt(tr("ai.budgetHelp"), { max: ceiling })}>
              <Input
                size="sm"
                inputMode="decimal"
                value={budget}
                maxLength={8}
                style={{ width: 120 }}
                onChange={(e) => setBudget(e.target.value)}
              />
            </Field>
            {message && (
              <p role="status" style={{ margin: 0, fontSize: 13, color: message.tone === "danger" ? K.danger : K.success }}>
                {message.text}
              </p>
            )}
            <div>
              <Button type="submit" size="sm" disabled={!hydrated || busy}>
                {tr("ai.save")}
              </Button>
            </div>
          </form>
        ) : (
          <p style={{ margin: 0, fontSize: 13, color: K.muted }} data-testid="ai-owner-only">
            {tr("ai.ownerOnly")}
          </p>
        )}
      </Panel>

      <Panel title={tr("ai.rules")} testId="ai-rules">
        <ul style={{ margin: 0, paddingLeft: 18, display: "grid", gap: 6, fontSize: 13, color: K.ink }}>
          <li>{tr("ai.rule.mistakes")}</li>
          <li>{tr("ai.rule.review")}</li>
          <li>{tr("ai.rule.data")}</li>
          <li>{tr("ai.rule.manual")}</li>
        </ul>
        {/* AA-1033: the kit staff notice every AI draft carries, shown here as it appears, and the terms section. */}
        <p style={{ margin: "14px 0 6px", fontSize: 12.5, color: K.muted }}>{tr("ai.noticeExample")}</p>
        <AiNotice lang={lang} testId="ai-notice-example" />
        <p style={{ margin: "12px 0 0", fontSize: 13 }}>
          <Link href={CRM_TERMS_AI_HREF} style={{ color: K.accentDeep }} data-testid="ai-terms-link">
            {tr("ai.termsLink")}
          </Link>
        </p>
      </Panel>
    </div>
  );
}
