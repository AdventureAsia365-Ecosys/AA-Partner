"use client";
// web/components/proposals/PricingSettingsForm.tsx — Settings > Pricing (AA-1066, crm-048; owner only, API
// Action.PAYMENT_SETTINGS): the agency's age bands for traveller prices, the discount staff may give without the
// owner's approval (default 10 %), and the retail price rules the builder applies to the suggested sell of a new AA
// product (season, group size, early booking, last minute; add up, or best one only by priority).
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Badge, Button, Field, Input, K, Select, useHydrated } from "@/app/_kit";
import { DateInput } from "@/components/DateInput";
import { Panel } from "@/components/PageTitle";
import { bffSend } from "@/lib/bffClient";
import { formatDate, type DateLocale } from "@/lib/dates";
import { t, type Lang, type MessageKey } from "@/lib/i18n";
import {
  EMPTY_RULE,
  RULE_KINDS,
  RULE_SCOPES,
  ruleBody,
  ruleSummary,
  signedPct,
  type PriceRule,
  type PricingSettings,
  type RuleForm,
} from "@/lib/retailPricing";

export function PricingSettingsForm({ view, lang, locale }: { view: PricingSettings; lang: Lang; locale: DateLocale }) {
  const router = useRouter();
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [data, setData] = useState<PricingSettings>(view);
  const [bands, setBands] = useState({
    infant: String(view.infant_age_max),
    child: String(view.child_age_max),
    limit: String(Number(view.discount_owner_limit_pct)),
  });
  const [rule, setRule] = useState<RuleForm>(EMPTY_RULE);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ tone: "danger" | "success"; text: string } | null>(null);

  async function call(method: "PUT" | "POST" | "PATCH" | "DELETE", path: string, body?: unknown, failKey = "pricingSettings.error") {
    setBusy(true);
    setMessage(null);
    const res = await bffSend<PricingSettings>(method, path, body);
    setBusy(false);
    if (!res.ok) {
      setMessage({ tone: "danger", text: tr(res.detail === "too_many_price_rules" ? "pricingSettings.tooMany" : failKey) });
      return false;
    }
    setData(res.data);
    setMessage({ tone: "success", text: tr("pricingSettings.saved") });
    router.refresh();
    return true;
  }

  async function saveBands() {
    const infant = Number(bands.infant);
    const child = Number(bands.child);
    const limit = bands.limit.trim().replace(",", ".");
    if (!Number.isInteger(infant) || !Number.isInteger(child) || infant < 0 || infant > 5 || child < 1 || child > 17 || infant >= child || !/^\d{1,3}(\.\d{1,2})?$/.test(limit) || Number(limit) > 100) {
      setMessage({ tone: "danger", text: tr("pricingSettings.error") });
      return;
    }
    await call("PUT", "settings/pricing", { infant_age_max: infant, child_age_max: child, discount_owner_limit_pct: limit });
  }

  async function addRule(form: HTMLFormElement) {
    const fd = new FormData(form);
    const body = ruleBody({ ...rule, date_from: String(fd.get("date_from") ?? ""), date_to: String(fd.get("date_to") ?? "") });
    if (!body) {
      setMessage({ tone: "danger", text: tr("pricingSettings.ruleError") });
      return;
    }
    if (await call("POST", "settings/price-rules", body, "pricingSettings.ruleError")) setRule(EMPTY_RULE);
  }

  const date = (iso: string) => (iso ? formatDate(iso, locale) : "");
  const scopeLabel = (s: string) => (s === "all" ? tr("pricingSettings.scope.all") : tr(`markup.kind.${s}`));

  return (
    <div style={{ display: "grid", gap: 14 }}>
      {message && (
        <p role="status" data-testid="pricing-message" style={{ margin: 0, fontSize: 13, color: message.tone === "danger" ? K.danger : K.success }}>
          {message.text}
        </p>
      )}
      <Panel title={tr("pricingSettings.bands")} testId="pricing-bands">
        <form
          data-hydrated={hydrated ? "true" : undefined}
          onSubmit={(e) => {
            e.preventDefault();
            void saveBands();
          }}
          style={{ display: "grid", gap: 10 }}
        >
          <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("pricingSettings.bandsHelp")}</p>
          <div style={{ display: "grid", gap: 10, gridTemplateColumns: "repeat(auto-fill, minmax(180px, 1fr))" }}>
            <Field label={tr("pricingSettings.infantMax")}>
              <Input size="sm" inputMode="numeric" value={bands.infant} onChange={(e) => setBands({ ...bands, infant: e.target.value })} />
            </Field>
            <Field label={tr("pricingSettings.childMax")}>
              <Input size="sm" inputMode="numeric" value={bands.child} onChange={(e) => setBands({ ...bands, child: e.target.value })} />
            </Field>
            <Field label={tr("pricingSettings.limit")}>
              <Input size="sm" inputMode="decimal" value={bands.limit} onChange={(e) => setBands({ ...bands, limit: e.target.value })} />
            </Field>
          </div>
          <div>
            <Button type="submit" size="sm" disabled={!hydrated || busy}>
              {tr("pricingSettings.save")}
            </Button>
          </div>
        </form>
      </Panel>

      <Panel title={tr("pricingSettings.rules")} testId="pricing-rules">
        <p style={{ margin: "0 0 8px", fontSize: 12.5, color: K.muted }}>{tr("pricingSettings.rulesHelp")}</p>
        {data.rules.length === 0 && <p style={{ margin: 0, fontSize: 13 }}>{tr("pricingSettings.none")}</p>}
        <ul style={{ margin: 0, padding: 0, listStyle: "none", display: "grid", gap: 6 }} data-testid="pricing-rule-list">
          {data.rules.map((r: PriceRule) => (
            <li key={r.retail_price_rule_id} data-testid="pricing-rule" data-kind={r.kind} style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", fontSize: 13, padding: "6px 0", borderTop: `1px solid ${K.line}` }}>
              <strong style={{ color: K.ink, overflowWrap: "anywhere" }}>{r.name}</strong>
              <span>{signedPct(r.adjust_pct)}</span>
              <span style={{ color: K.muted }}>
                {tr(`pricingSettings.kind.${r.kind}`)}: {ruleSummary(r, tr, date)} · {scopeLabel(r.product_scope)}
                {r.aa_tour_source_id ? ` · ${r.aa_tour_source_id}` : ""} · {tr(`pricingSettings.stacking.${r.stacking}`)} · {r.priority}
              </span>
              {!r.active && <Badge tone="neutral">{tr("pricingSettings.paused")}</Badge>}
              <span style={{ flex: 1 }} />
              <Button size="sm" variant="ghost" disabled={!hydrated || busy} onClick={() => void call("PATCH", `settings/price-rules/${r.retail_price_rule_id}`, { active: !r.active })}>
                {tr(r.active ? "pricingSettings.pause" : "pricingSettings.resume")}
              </Button>
              <Button size="sm" variant="ghost" disabled={!hydrated || busy} onClick={() => void call("DELETE", `settings/price-rules/${r.retail_price_rule_id}`)}>
                {tr("pricingSettings.deleteRule")}
              </Button>
            </li>
          ))}
        </ul>

        <form
          key={data.rules.length}
          data-testid="pricing-rule-form"
          onSubmit={(e) => {
            e.preventDefault();
            void addRule(e.currentTarget);
          }}
          style={{ display: "grid", gap: 10, marginTop: 12 }}
        >
          <strong style={{ fontSize: 13 }}>{tr("pricingSettings.addRule")}</strong>
          <div style={{ display: "grid", gap: 10, gridTemplateColumns: "repeat(auto-fill, minmax(180px, 1fr))" }}>
            <Field label={tr("pricingSettings.rule.name")}>
              <Input size="sm" maxLength={80} value={rule.name} onChange={(e) => setRule({ ...rule, name: e.target.value })} />
            </Field>
            <Field label={tr("pricingSettings.rule.kind")}>
              <Select size="sm" value={rule.kind} onChange={(e) => setRule({ ...rule, kind: e.target.value as RuleForm["kind"] })} options={RULE_KINDS.map((k) => ({ value: k, label: tr(`pricingSettings.kind.${k}`) }))} />
            </Field>
            <Field label={tr("pricingSettings.rule.adjust")} hint={tr("pricingSettings.rule.adjustHelp")}>
              <Input size="sm" inputMode="decimal" value={rule.adjust_pct} onChange={(e) => setRule({ ...rule, adjust_pct: e.target.value })} />
            </Field>
            {rule.kind === "group_size" && (
              <Field label={tr("pricingSettings.rule.minPax")}>
                <Input size="sm" inputMode="numeric" value={rule.min_pax} onChange={(e) => setRule({ ...rule, min_pax: e.target.value })} />
              </Field>
            )}
            {(rule.kind === "early_booking" || rule.kind === "last_minute") && (
              <Field label={tr("pricingSettings.rule.daysBefore")}>
                <Input size="sm" inputMode="numeric" value={rule.days_before} onChange={(e) => setRule({ ...rule, days_before: e.target.value })} />
              </Field>
            )}
            <Field label={tr("pricingSettings.rule.scope")}>
              <Select size="sm" value={rule.product_scope} onChange={(e) => setRule({ ...rule, product_scope: e.target.value as RuleForm["product_scope"] })} options={RULE_SCOPES.map((s) => ({ value: s, label: scopeLabel(s) }))} />
            </Field>
            <Field label={tr("pricingSettings.rule.stacking")}>
              <Select size="sm" value={rule.stacking} onChange={(e) => setRule({ ...rule, stacking: e.target.value as RuleForm["stacking"] })} options={(["add", "best_only"] as const).map((s) => ({ value: s, label: tr(`pricingSettings.stacking.${s}`) }))} />
            </Field>
            <Field label={tr("pricingSettings.rule.priority")}>
              <Input size="sm" inputMode="numeric" value={rule.priority} onChange={(e) => setRule({ ...rule, priority: e.target.value })} />
            </Field>
            <Field label={tr("pricingSettings.rule.tour")}>
              <Input size="sm" maxLength={128} value={rule.aa_tour_source_id} onChange={(e) => setRule({ ...rule, aa_tour_source_id: e.target.value })} />
            </Field>
          </div>
          {rule.kind === "season" && (
            <div style={{ display: "grid", gap: 10, gridTemplateColumns: "repeat(auto-fill, minmax(180px, 1fr))" }}>
              <DateInput locale={locale} name="date_from" label={tr("pricingSettings.rule.from")} readAs={tr("settings.readAs")} invalid={tr("settings.invalidDate")} />
              <DateInput locale={locale} name="date_to" label={tr("pricingSettings.rule.to")} readAs={tr("settings.readAs")} invalid={tr("settings.invalidDate")} />
            </div>
          )}
          <div>
            <span data-testid="pricing-rule-save">
              <Button type="submit" size="sm" variant="primary" disabled={!hydrated || busy || data.rules.length >= data.max_rules}>
                {tr("pricingSettings.saveRule")}
              </Button>
            </span>
          </div>
        </form>
      </Panel>
    </div>
  );
}
