"use client";
// web/app/(studio)/portal/_components/PlaceholderTabs.tsx
// Billing and Settings. (AA-761: the Activity Log moved to AccountActivity.tsx, on GET /v1/account/activity.)
//
// AA-636: these three pages were largely mock UI — every tenant saw "WanderLux Travel" /
// "sara@wanderlux.com" in Settings, a Growth-is-current plan table with hardcoded "300 RPM" /
// "20,000" API calls, and Settings controls (edit name/email, rotate key, notification toggles,
// default language/SEO mode) that saved nothing. Everything shown now comes from the tenant's own
// data (/api/tenant/me, /v1/billing); account changes go through the Adventure Asia team, since
// no tenant self-service endpoint exists for them.

import Link from "next/link";
import { useState } from "react";
import { signOutEverywhere } from "@/lib/studio/signOut";
import { useQuery } from "@tanstack/react-query";
import { Check, Mail, ExternalLink, LogOut, AlertTriangle, FileText } from "lucide-react";
import {
  Button, EmptyState as KitEmptyState, Modal, PageHeader, SkeletonBar, SkeletonStyle, SITE_URL, SUPPORT_EMAIL, alpha, apiGet, K, sans,
  useLang, useT, type UiLang,
} from "@/app/_kit";
import { T, Card, ProgressBar } from "./ui";
import { SectionSkeleton } from "./SectionSkeleton";
import { BILLING_MESSAGES, SETTINGS_MESSAGES } from "./account.messages";
import { fmtBillingMonth } from "@/lib/studio/format";
import { usePortalShell, type ShellBilling } from "./PortalShellContext";
import { fmtDay, fmtUsd, usageMeter, type Quota } from "@/lib/studio/account";
import { signedInText } from "@/lib/studio/crmIdentity";
import { useCrmIdentity } from "@/lib/studio/useCrmIdentity";


const titleCase = (s: string) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : s);

// AA-760 — "Couldn't load …" + Retry on the Billing page (a failed /v1/billing call used to read as
// "$0 · Custom pricing").
function LoadError({ what, onRetry }: { what: string; onRetry: () => void }) {
  const t = useT(BILLING_MESSAGES);
  return (
    <Card>
      <div role="alert" style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap", fontSize: 13, color: K.ink }}>
        <AlertTriangle size={16} color={K.danger} style={{ flexShrink: 0 }} />
        <span style={{ flex: 1, minWidth: 180 }}>{t("loadError", what)}</span>
        <Button size="sm" onClick={onRetry} style={{ whiteSpace: "nowrap" }}>{t("tryAgain")}</Button>
      </div>
    </Card>
  );
}

// ─── Contact modal (plan / account changes) ───────────────────────────────────
// AA-761 — on the kit Modal (focus trap, Escape, scroll lock); was a hand-rolled dialog (AA-732 kit rule).
// AA-1025 (S264): the address is labelled "Contact Adventure Asia" (was "Email"), so it never reads as the account's.
function ContactModal({ heading, body, subject, onClose }: {
  heading: string; body: string; subject: string; onClose: () => void;
}) {
  const t = useT(BILLING_MESSAGES);
  return (
    <Modal open onClose={onClose} title={heading} width={460}
      footer={<Button size="sm" variant="ghost" onClick={onClose}>{t("close")}</Button>}>
      <p style={{ fontSize: 13, color: K.muted, lineHeight: 1.6, margin: "0 0 18px" }}>{body}</p>
      <div style={{ padding: "12px 14px", background: K.bg, border: `1px solid ${K.line}`, borderRadius: 10, marginBottom: 16 }}>
        <div style={{ fontSize: 10.5, textTransform: "uppercase", letterSpacing: "0.12em", color: K.muted, fontWeight: 600, marginBottom: 4 }}>{t("contactAa")}</div>
        <div style={{ fontSize: 14, fontWeight: 600, color: K.ink, userSelect: "all" }}>{SUPPORT_EMAIL}</div>
        <div style={{ fontSize: 11.5, color: K.muted2, marginTop: 4 }}>{t("subject", subject)}</div>
      </div>
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
        <a href={`mailto:${SUPPORT_EMAIL}?subject=${encodeURIComponent(subject)}`}
          style={{ flex: 1, display: "inline-flex", alignItems: "center", justifyContent: "center", gap: 8, padding: "11px 18px", background: K.accent, color: K.ink, borderRadius: 999, fontSize: 13, fontWeight: 700, textDecoration: "none", minWidth: 160 }}>
          <Mail size={15} /> {t("emailUs")}
        </a>
        <a href={SITE_URL} target="_blank" rel="noreferrer"
          style={{ flex: 1, display: "inline-flex", alignItems: "center", justifyContent: "center", gap: 8, padding: "10px 18px", background: K.card, border: `1px solid ${K.line}`, color: K.ink3, borderRadius: 999, fontSize: 13, fontWeight: 500, textDecoration: "none", minWidth: 160 }}>
          adventure.asia <ExternalLink size={13} />
        </a>
      </div>
    </Modal>
  );
}

// ─── Billing ──────────────────────────────────────────────────────────────────
type Plan = { plan_name: string; tours_quota_monthly: number; api_calls_quota_monthly: number; price_usd_monthly: number | null; rate_limit_rpm: number | null };

function fmtQuota(n: number | null | undefined, lang: UiLang, unlimited: string): string {
  return n == null ? "—" : n >= 999999 ? unlimited : n.toLocaleString(lang === "vi" ? "vi-VN" : "en-US");
}

// AA-760 — typed as the shell's /v1/billing value (was `any`); the fields are read defensively below.
// eslint-disable-next-line @typescript-eslint/no-explicit-any -- the plan fields are read loosely (number | string | null)
type BillingRecord = Record<string, any>;

export function BillingTab({ billing }: { billing: ShellBilling | null }) {
  const t = useT(BILLING_MESSAGES);
  const lang = useLang();
  const num = (n: number) => n.toLocaleString(lang === "vi" ? "vi-VN" : "en-US");
  const q = (n: number | null | undefined) => fmtQuota(n, lang, t("unlimited"));
  const b: BillingRecord | null = billing;
  const { tenantName, billingStatus, retryBilling } = usePortalShell();
  const [contactPlan, setContactPlan] = useState<string | null>(null);
  // AA-761 — the rewrite meter's used / quota / reset date (GET /v1/quota, the counter the rewrite route charges).
  const quotaQ = useQuery({ queryKey: ["quota"], queryFn: () => apiGet<Quota>("/api/tenant/v1/quota") });

  // AA-760 — real figures only once /v1/billing answered: before that the page showed invented facts
  // ("Custom pricing", "$0 · Plan fee, no overage"), and forever if the call failed.
  if (billingStatus !== "success" || !b) {
    return (
      <div style={{ maxWidth: 760 }}>
        <PageHeader title={t("title")} description={t("subLoading")} />
        {billingStatus === "error"
          ? <LoadError what={t("yourBilling")} onRetry={retryBilling} />
          : (
            <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
              <SectionSkeleton lines={3} label={t("loadingPlan")} />
              <SectionSkeleton lines={2} label={t("loadingPeriod")} />
            </div>
          )}
      </div>
    );
  }

  // AA-798 — no "starter" fallback: a tenant without a plan sees "No plan attached", not a made-up one.
  const noPlan     = b?.plan_attached === false;
  const plan       = noPlan ? "" : String(b?.plan_tier ?? "");
  const price      = Number(b?.price_usd_monthly ?? 0);
  const toursTotal = b?.tours_quota_monthly == null ? null : Number(b.tours_quota_monthly);
  const apiUsed    = Number(b?.api_calls_used ?? 0);
  const apiTotal   = b?.api_calls_quota_monthly == null ? null : Number(b.api_calls_quota_monthly);
  const overage    = Number(b?.overage_usd ?? 0);
  const rpm        = b?.rate_limit_rpm as number | null | undefined;
  const month      = fmtBillingMonth(b?.billing_month, lang);
  const plans: Plan[] = b?.plans ?? [];
  const pct = (u: number, t: number | null) => (t != null && t > 0 ? Math.min(100, (u / t) * 100) : 0);

  return (
    <div style={{ maxWidth: 760 }}>
      {contactPlan && (
        <ContactModal
          heading={t("switchTo", titleCase(contactPlan))}
          body={t("switchBody")}
          subject={t("switchSubject", titleCase(contactPlan), tenantName)}
          onClose={() => setContactPlan(null)}
        />
      )}

      <PageHeader title={t("title")} description={t("sub", month)} />

      {/* Current plan */}
      <Card dark style={{ marginBottom: 16 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 16, flexWrap: "wrap" }}>
          <div>
            <div style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: "0.14em", color: alpha(K.sideTitle, 50), marginBottom: 8 }}>{t("currentPlan")}</div>
            <div style={{ fontSize: 32, fontWeight: 600, color: T.onSolid, letterSpacing: "-0.02em" }}>{noPlan ? t("noPlan") : titleCase(plan || "—")}</div>
            <div style={{ fontSize: 14, color: alpha(K.sideTitle, 65), marginTop: 4 }}>
              {noPlan ? t("setUpPlan") : price > 0 ? t("perMonth", num(price)) : t("customPricing")}
            </div>
          </div>
          <Button variant="primary" onClick={() => setContactPlan(plans.find(p => p.plan_name !== plan && (p.price_usd_monthly ?? Infinity) > price)?.plan_name ?? "enterprise")}>
            {t("changePlan")}
          </Button>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(120px, 1fr))", gap: 16, marginTop: 20, paddingTop: 16, borderTop: `1px solid ${alpha(K.sideTitle, 8)}` }}>
          {[[t("rewritesMonth"), q(toursTotal)], [t("apiCallsMonth"), q(apiTotal)], [t("rateLimit"), rpm ? t("rpm", num(rpm)) : "—"]].map(([l, v]) => (
            <div key={l}>
              <div style={{ fontSize: 10.5, textTransform: "uppercase", letterSpacing: "0.1em", color: alpha(K.sideTitle, 45), marginBottom: 4 }}>{l}</div>
              <div style={{ fontSize: 14, fontWeight: 600, color: T.onSolid, fontVariantNumeric: "tabular-nums" }}>{v}</div>
            </div>
          ))}
        </div>
      </Card>

      {/* AA-761 — the rewrite meter: used vs quota and the reset date (/v1/quota), overage so far (/v1/billing). */}
      <UsageMeterCard quota={quotaQ.data ?? null} billing={b} loading={quotaQ.isPending}
        failed={quotaQ.isError} onRetry={() => quotaQ.refetch()} month={month} />

      {/* This period */}
      <Card style={{ marginBottom: 16 }}>
        <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em", color: K.muted, marginBottom: 16 }}>{t("thisPeriod", month)}</div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 16 }}>
          {[
            { l: t("apiCalls"), v: `${num(apiUsed)} / ${q(apiTotal)}`, sub: t("pctUsed", pct(apiUsed, apiTotal).toFixed(0)) },
            { l: t("dueSoFar"), v: noPlan ? "—" : `$${(price + overage).toLocaleString(lang === "vi" ? "vi-VN" : "en-US", { maximumFractionDigits: 2 })}`, sub: noPlan ? t("noPlan") : overage > 0 ? t("includesOverage", num(overage)) : t("noOverage") },
          ].map(({ l, v, sub }) => (
            <div key={l} style={{ padding: "14px 16px", background: K.bg, borderRadius: 8, border: `1px solid ${K.line}` }}>
              <div style={{ fontSize: 10.5, textTransform: "uppercase", letterSpacing: "0.1em", color: K.muted, fontWeight: 600, marginBottom: 6 }}>{l}</div>
              <div style={{ fontSize: 18, fontWeight: 700, color: K.ink, fontVariantNumeric: "tabular-nums" }}>{v}</div>
              <div style={{ fontSize: 11, color: K.muted2, marginTop: 4 }}>{sub}</div>
            </div>
          ))}
        </div>
        {/* AA-760 — what "API calls" counts (api/middleware/rate_limit.py): a portal session is not metered. */}
        <p data-testid="billing-api-calls-note" style={{ fontSize: 11.5, color: K.muted, lineHeight: 1.5, margin: "14px 0 0" }}>
          {t("apiNote")}
        </p>
      </Card>

      {/* AA-761 — AA-issued invoices. CIS stores none yet (the billing provider is still open): the empty state. */}
      <Card style={{ marginBottom: 16 }}>
        <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em", color: K.muted, marginBottom: 4 }}>{t("invoices")}</div>
        <div data-testid="billing-invoices">
          <KitEmptyState
            icon={<FileText size={28} />}
            title={t("noInvoices")}
            description={t("noInvoicesSub")}
            action={<a href={`mailto:${SUPPORT_EMAIL}?subject=${encodeURIComponent(t("invoiceSubject", tenantName))}`}
              style={{ fontSize: 12.5, fontWeight: 600, color: K.ink3 }}>{t("emailAt", SUPPORT_EMAIL)}</a>}
          />
        </div>
      </Card>

      {/* Plan comparison — from shared.membership_plans via /v1/billing */}
      {plans.length > 0 && (
        <Card>
          <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em", color: K.muted, marginBottom: 16 }}>{t("comparePlans")}</div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 12 }}>
            {plans.map(p => {
              const active = p.plan_name === plan;
              return (
                <div key={p.plan_name} style={{ padding: 14, borderRadius: 10, border: `${active ? 2 : 1}px solid ${active ? K.accent : K.line}`, background: active ? K.accentTint : K.bg, display: "flex", flexDirection: "column" }}>
                  <div style={{ fontSize: 13, fontWeight: 700, color: active ? K.amber : K.ink, marginBottom: 4 }}>{titleCase(p.plan_name)}</div>
                  <div style={{ fontSize: 20, fontWeight: 600, color: K.ink, marginBottom: 10, fontVariantNumeric: "tabular-nums" }}>
                    {p.price_usd_monthly != null ? <>${num(p.price_usd_monthly)}<span style={{ fontSize: 11, color: K.muted, fontWeight: 400 }}>{t("perMo")}</span></> : t("custom")}
                  </div>
                  {[t("planRewrites", q(p.tours_quota_monthly)), t("planApi", q(p.api_calls_quota_monthly)), p.rate_limit_rpm ? t("rpm", num(p.rate_limit_rpm)) : t("customRate")].map(feat => (
                    <div key={feat} style={{ fontSize: 11.5, color: K.muted, marginBottom: 4, display: "flex", gap: 6, alignItems: "center" }}>
                      <Check size={12} color={K.success} style={{ flexShrink: 0 }} />{feat}
                    </div>
                  ))}
                  <div style={{ marginTop: "auto", paddingTop: 10 }}>
                    {active
                      ? <div style={{ fontSize: 11, fontWeight: 700, color: K.success, textAlign: "center", display: "flex", alignItems: "center", justifyContent: "center", gap: 4 }}><Check size={12} /> {t("current")}</div>
                      : <Button size="sm" onClick={() => setContactPlan(p.plan_name)} style={{ width: "100%" }}>{t("switch")}</Button>}
                  </div>
                </div>
              );
            })}
          </div>
        </Card>
      )}
    </div>
  );
}

// ─── AA-761: rewrite meter ───────────────────────────────────────────────────
function UsageMeterCard({ quota, billing, loading, failed, onRetry, month }: {
  quota: Quota | null; billing: BillingRecord; loading: boolean; failed: boolean; onRetry: () => void; month: string;
}) {
  const t = useT(BILLING_MESSAGES);
  const lang = useLang();
  const num = (n: number) => n.toLocaleString(lang === "vi" ? "vi-VN" : "en-US");
  if (failed) return <div style={{ marginBottom: 16 }}><LoadError what={t("yourUsage")} onRetry={onRetry} /></div>;
  if (loading || !quota) {
    return <div style={{ marginBottom: 16 }}><SectionSkeleton lines={2} label={t("loadingUsage")} /></div>;
  }
  const m = usageMeter(quota, billing);
  const limitText = m.state === "unlimited" ? t("unlimited") : m.quota == null ? "—" : num(m.quota);
  const tone = m.state === "over" ? K.danger : m.state === "near" ? K.amber : K.success;
  return (
    <Card style={{ marginBottom: 16 }}>
      <div data-testid="billing-usage-meter" data-state={m.state}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 12, flexWrap: "wrap", marginBottom: 10 }}>
          <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em", color: K.muted }}>{t("rewrites", month)}</div>
          <div style={{ fontSize: 12, color: K.muted }}>{t("resets", fmtDay(m.resetsAt, lang))}</div>
        </div>
        <div style={{ display: "flex", alignItems: "baseline", gap: 8, marginBottom: 10 }}>
          <span style={{ fontSize: 28, fontWeight: 700, color: K.ink, fontVariantNumeric: "tabular-nums" }}>{num(m.used)}</span>
          <span style={{ fontSize: 14, color: K.muted, fontVariantNumeric: "tabular-nums" }}>{t("ofRewrites", limitText)}</span>
        </div>
        {(m.state === "ok" || m.state === "near" || m.state === "over") && (
          <div role="meter" aria-label={t("meterAria")} aria-valuemin={0} aria-valuemax={m.quota ?? 0}
            aria-valuenow={Math.min(m.used, m.quota ?? 0)}>
            <ProgressBar pct={m.pct} warn={m.state !== "ok"} />
          </div>
        )}
        <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 10, fontSize: 12.5, color: K.body, flexWrap: "wrap" }}>
          <span style={{ width: 8, height: 8, borderRadius: 999, background: tone, flexShrink: 0 }} aria-hidden />
          {m.state === "no_plan" && <span>{t("meterNoPlan")}</span>}
          {m.state === "unlimited" && <span>{t("meterUnlimited")}</span>}
          {m.state === "ok" && <span>{t("meterOk", num(m.remaining ?? 0))}</span>}
          {m.state === "near" && <span>{t("meterNear", num(m.remaining ?? 0))}</span>}
          {m.state === "over" && (
            <span>
              {t("meterOver", num(m.overTours), fmtUsd(m.overageUsd))}
              {m.ratePerTour ? t("meterRate", fmtUsd(m.ratePerTour)) : ""}.
            </span>
          )}
        </div>
      </div>
    </Card>
  );
}

// ─── Settings ─────────────────────────────────────────────────────────────────
export function SettingsTab() {
  const t = useT(SETTINGS_MESSAGES);
  const lang = useLang();
  const { tenantName, planTier, tenantStatus } = usePortalShell();
  // AA-760 — skeleton text until /api/tenant/me answers (was the generic "Partner" + a blank plan).
  const loadingMe = tenantStatus === "pending";
  const skel = (w: number) => <SkeletonBar w={w} h={11} style={{ marginTop: 4 }} />;
  const [contact, setContact] = useState(false);
  // AA-1025 (S264): who is signed in, live from the CRM web (absent on an API-key session or off the agency host).
  const signedIn = signedInText(useCrmIdentity(), lang);

  const row: React.CSSProperties = {
    display: "flex", alignItems: "center", justifyContent: "space-between", gap: 16,
    padding: "13px 0", borderBottom: `1px solid ${K.line2}`,
  };
  const last: React.CSSProperties = { ...row, borderBottom: "none" };
  const label = (text: string, sub?: React.ReactNode) => (
    <div style={{ minWidth: 0 }}>
      <div style={{ fontSize: 13, fontWeight: 500, color: K.ink }}>{text}</div>
      {sub && <div style={{ fontSize: 11.5, color: K.muted2, marginTop: 2 }}>{sub}</div>}
    </div>
  );

  async function signOut() {
    // AA-427: tenant cookies are httpOnly — cleared server-side. AA-979: one sign-out with the CRM (lib/signOut.ts).
    await signOutEverywhere();
  }

  return (
    <div style={{ maxWidth: 600, fontFamily: sans }}>
      {contact && (
        <ContactModal
          heading={t("updateTitle")}
          body={t("updateBody")}
          subject={t("updateSubject", tenantName)}
          onClose={() => setContact(false)}
        />
      )}

      <SkeletonStyle />
      {/* AA-760 — "Account settings" (was "Team & Settings": there is no team section yet). */}
      <PageHeader title={t("title")} description={loadingMe ? t("subLoading") : t("sub", tenantName)} />

      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <Card>
          <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em", color: K.muted, marginBottom: 6 }}>{t("account")}</div>
          {signedIn && <div style={row} data-testid="settings-signed-in-as">{label(t("signedIn"), signedIn)}</div>}
          <div style={row}>{label(t("company"), loadingMe ? skel(120) : tenantName)}</div>
          <div style={row}>{label(t("plan"), loadingMe ? skel(70) : titleCase(planTier) || t("noPlan"))}<Link href="/portal/account/billing" style={{ fontSize: 12, color: K.ink3, fontWeight: 500, textDecoration: "none" }}>{t("viewBilling")}</Link></div>
          <div style={last}>
            {label(t("needChange"), t("needChangeSub"))}
            <Button size="sm" onClick={() => setContact(true)} style={{ whiteSpace: "nowrap", flexShrink: 0 }}><Mail size={13} /> {t("contactUs")}</Button>
          </div>
        </Card>

        <Card>
          <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em", color: K.muted, marginBottom: 6 }}>{t("session")}</div>
          <div style={last}>
            {label(t("signOut"), t("signOutSub"))}
            <Button size="sm" variant="danger" onClick={signOut}><LogOut size={13} /> {t("signOut")}</Button>
          </div>
        </Card>
      </div>
    </div>
  );
}
