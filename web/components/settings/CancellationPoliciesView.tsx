"use client";
// web/components/settings/CancellationPoliciesView.tsx — Settings > Cancellation policy (AA-1067, crm-049, item
// crm-11): the agency's charge table for travellers who cancel ("N days or more before departure: X %"). The owner
// starts from the Adventure Asia table, edits the draft, publishes it (the default for new proposals), and later makes
// a new version (publishing it retires the old one; proposals keep the version their client accepted). Every role
// reads it; only the owner changes it (the API refuses the others too).
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Badge, Button, Field, Input, K, useHydrated } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { bffSend } from "@/lib/bffClient";
import { fmt, langOf, t, type Lang, type MessageKey } from "@/lib/i18n";
import type { DateLocale } from "@/lib/dates";
import { draftTiers, paymentsErrorKey, tiersBody, tierText, type CancellationPolicy, type DraftTier, type PolicyListing } from "@/lib/retailPayments";

const TONE = { draft: "warning", published: "success", retired: "neutral" } as const;

export function CancellationPoliciesView({ view, lang, locale, isOwner }: { view: PolicyListing; lang: Lang; locale: DateLocale; isOwner: boolean }) {
  const router = useRouter();
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ tone: "danger" | "success"; text: string } | null>(null);

  async function call(method: "POST" | "PATCH" | "DELETE", path: string, body?: Record<string, unknown>, done = "policy.saved"): Promise<boolean> {
    setBusy(true);
    setMessage(null);
    const res = method === "DELETE" ? await bffSend("DELETE", path) : await bffSend(method, path, body ?? {});
    setBusy(false);
    if (!res.ok) {
      const key = paymentsErrorKey(res.detail);
      setMessage({ tone: "danger", text: key ? tr(key) : tr("error.api") });
      return false;
    }
    setMessage({ tone: "success", text: tr(done) });
    router.refresh();
    return true;
  }

  const hasDraftFor = (id: string) => view.policies.some((p) => p.status === "draft" && p.replaces_policy_id === id);
  return (
    <div style={{ display: "grid", gap: 14 }}>
      <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{fmt(tr("policy.mergeField"), { field: "{{cancellation.policy}}" })}</p>
      {view.policies.length === 0 && (
        <Panel testId="policy-empty">
          <p style={{ margin: "0 0 8px", fontSize: 13 }}>{tr("policy.none")}</p>
          <ul style={{ margin: "0 0 8px", paddingLeft: 18, fontSize: 13 }}>
            {view.aa_default.tiers.map((tier) => (
              <li key={tier.days_before_min}>{tierText(tier, tr)}</li>
            ))}
          </ul>
          {isOwner && (
            <span data-testid="policy-start-aa">
              <Button size="sm" disabled={!hydrated || busy} onClick={() => void call("POST", "cancellation-policies", { aa_default: true })}>
                {tr("policy.startAa")}
              </Button>
            </span>
          )}
        </Panel>
      )}
      {view.policies.map((p) => (
        <PolicyCard
          key={`${p.policy_id}-${p.updated_at}`}
          policy={p}
          tr={tr}
          lang={langOf(locale)}
          isOwner={isOwner}
          busy={busy || !hydrated}
          canNewVersion={p.status === "published" && !hasDraftFor(p.policy_id)}
          call={call}
          onInvalid={() => setMessage({ tone: "danger", text: tr("policy.invalid") })}
        />
      ))}
      {view.policies.length > 0 && <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("policy.frozenNote")}</p>}
      {message && (
        <p role={message.tone === "danger" ? "alert" : "status"} data-testid="policy-message" style={{ margin: 0, fontSize: 13, color: message.tone === "danger" ? K.danger : K.success }}>
          {message.text}
        </p>
      )}
    </div>
  );
}

function PolicyCard({
  policy,
  tr,
  lang,
  isOwner,
  busy,
  canNewVersion,
  call,
  onInvalid,
}: {
  policy: CancellationPolicy;
  tr: (key: string) => string;
  lang: "en" | "vi";
  isOwner: boolean;
  busy: boolean;
  canNewVersion: boolean;
  call: (method: "POST" | "PATCH" | "DELETE", path: string, body?: Record<string, unknown>, done?: string) => Promise<boolean>;
  onInvalid: () => void;
}) {
  const [name, setName] = useState(policy.name);
  const [currency, setCurrency] = useState(policy.currency ?? "");
  const [tiers, setTiers] = useState<DraftTier[]>(() => draftTiers(policy.tiers));
  const draft = policy.status === "draft" && isOwner;
  const path = `cancellation-policies/${policy.policy_id}`;

  function set(i: number, patch: Partial<DraftTier>) {
    setTiers(tiers.map((r, n) => (n === i ? { ...r, ...patch } : r)));
  }

  async function save() {
    const body = tiersBody(tiers);
    if (!body || !name.trim()) {
      onInvalid();
      return;
    }
    await call("PATCH", path, { name: name.trim(), currency: currency.trim().toUpperCase() || null, tiers: body });
  }

  return (
    <Panel testId="policy-card">
      <div data-status={policy.status} data-default={policy.is_default ? "true" : undefined} style={{ display: "grid", gap: 8 }}>
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <strong style={{ fontSize: 14 }}>{policy.name}</strong>
          <span style={{ fontSize: 12.5, color: K.muted }}>{fmt(tr("policy.version"), { n: policy.version })}</span>
          <Badge tone={TONE[policy.status]}>{tr(`policy.status.${policy.status}`)}</Badge>
          {policy.is_default && <Badge tone="info">{tr("policy.default")}</Badge>}
        </div>
        {draft ? (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void save();
            }}
            style={{ display: "grid", gap: 8 }}
          >
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <Field label={tr("policy.name")}>
                <Input size="sm" value={name} maxLength={80} onChange={(e) => setName(e.target.value)} />
              </Field>
              <Field label={tr("policy.currency")}>
                <Input size="sm" value={currency} maxLength={3} onChange={(e) => setCurrency(e.target.value)} style={{ width: 80 }} />
              </Field>
            </div>
            {tiers.map((r, i) => (
              <div key={i} data-testid="policy-tier" style={{ display: "flex", gap: 8, alignItems: "end", flexWrap: "wrap" }}>
                <Field label={tr("policy.days")}>
                  <Input size="sm" inputMode="numeric" value={r.days_before_min} onChange={(e) => set(i, { days_before_min: e.target.value })} style={{ width: 90 }} />
                </Field>
                <Field label={tr("policy.pct")}>
                  <Input size="sm" inputMode="decimal" value={r.charge_pct} onChange={(e) => set(i, { charge_pct: e.target.value })} style={{ width: 90 }} />
                </Field>
                <Field label={tr("policy.minCharge")}>
                  <Input size="sm" inputMode="decimal" value={r.min_charge_amount} onChange={(e) => set(i, { min_charge_amount: e.target.value })} style={{ width: 110 }} />
                </Field>
                <Button type="button" size="sm" variant="ghost" onClick={() => setTiers(tiers.filter((_, n) => n !== i))}>
                  {tr("policy.removeTier")}
                </Button>
              </div>
            ))}
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <Button type="button" size="sm" variant="secondary" disabled={tiers.length >= 12} onClick={() => setTiers([...tiers, { days_before_min: "", charge_pct: "", min_charge_amount: "" }])}>
                {tr("policy.addTier")}
              </Button>
              <Button type="submit" size="sm" disabled={busy}>
                {tr("policy.save")}
              </Button>
              <span data-testid="policy-publish">
                <Button type="button" size="sm" variant="primary" disabled={busy} onClick={() => void call("POST", `${path}/publish`, { make_default: true }, "policy.published")}>
                  {tr("policy.publishDefault")}
                </Button>
              </span>
              <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => void call("DELETE", path)}>
                {tr("policy.delete")}
              </Button>
            </div>
          </form>
        ) : (
          <>
            {policy.text ? (
              <p style={{ margin: 0, fontSize: 13, whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{policy.text[lang] ?? policy.text.en}</p>
            ) : (
              <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>
                {policy.tiers.map((tier) => (
                  <li key={tier.days_before_min}>{tierText(tier, tr)}</li>
                ))}
              </ul>
            )}
            {isOwner && policy.status === "published" && (
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                {canNewVersion && (
                  <span data-testid="policy-new-version">
                    <Button size="sm" variant="secondary" disabled={busy} onClick={() => void call("POST", "cancellation-policies", { from_policy_id: policy.policy_id })}>
                      {tr("policy.newVersion")}
                    </Button>
                  </span>
                )}
                {!policy.is_default && (
                  <Button size="sm" variant="secondary" disabled={busy} onClick={() => void call("POST", `${path}/default`)}>
                    {tr("policy.setDefault")}
                  </Button>
                )}
                <Button size="sm" variant="ghost" disabled={busy} onClick={() => void call("POST", `${path}/retire`)}>
                  {tr("policy.retire")}
                </Button>
              </div>
            )}
          </>
        )}
      </div>
    </Panel>
  );
}
