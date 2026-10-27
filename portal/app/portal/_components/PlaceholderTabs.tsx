"use client";
// portal/app/portal/_components/PlaceholderTabs.tsx
// Billing and Settings. (AA-761: the Activity Log moved to AccountActivity.tsx, on GET /v1/account/activity.)
//
// AA-636: these three pages were largely mock UI — every tenant saw "WanderLux Travel" /
// "sara@wanderlux.com" in Settings, a Growth-is-current plan table with hardcoded "300 RPM" /
// "20,000" API calls, and Settings controls (edit name/email, rotate key, notification toggles,
// default language/SEO mode) that saved nothing. Everything shown now comes from the tenant's own
// data (/api/tenant/me, /v1/billing); account changes go through the Adventure Asia team, since
// no tenant self-service endpoint exists for them.

import { useState } from "react";
import { signOutEverywhere } from "@/lib/signOut";
import { useQuery } from "@tanstack/react-query";
import { Check, Mail, ExternalLink, KeyRound, LogOut, AlertTriangle, FileText } from "lucide-react";
import { EmptyState as KitEmptyState, Modal, SkeletonBar, SkeletonStyle, apiGet } from "@/app/_kit";
import { T, sans, Card, CardSkeleton, PageHeader, Btn, ProgressBar, fmtBillingMonth } from "./ui";
import { usePortalShell, type ShellBilling } from "./PortalShellContext";
import { fmtDay, fmtUsd, usageMeter, type Quota } from "@/lib/account";
import { SUPPORT_EMAIL, SITE_URL } from "@/app/_kit/core/brand";


const titleCase = (s: string) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : s);

// AA-760 — "Couldn't load …" + Retry on the Billing page (a failed /v1/billing call used to read as
// "$0 · Custom pricing").
function LoadError({ what, onRetry }: { what: string; onRetry: () => void }) {
  return (
    <Card>
      <div role="alert" style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap", fontSize: 13, color: T.ink }}>
        <AlertTriangle size={16} color={T.red} style={{ flexShrink: 0 }} />
        <span style={{ flex: 1, minWidth: 180 }}>Couldn&rsquo;t load {what}. Check your connection and try again.</span>
        <Btn size="sm" onClick={onRetry} style={{ whiteSpace: "nowrap" }}>Try again</Btn>
      </div>
    </Card>
  );
}

// ─── Contact modal (plan / account changes) ───────────────────────────────────
// AA-761 — on the kit Modal (focus trap, Escape, scroll lock); was a hand-rolled dialog (AA-732 kit rule).
function ContactModal({ heading, body, subject, onClose }: {
  heading: string; body: string; subject: string; onClose: () => void;
}) {
  return (
    <Modal open onClose={onClose} title={heading} width={460}
      footer={<Btn size="sm" variant="ghost" onClick={onClose}>Close</Btn>}>
      <p style={{ fontSize: 13, color: T.muted, lineHeight: 1.6, margin: "0 0 18px" }}>{body}</p>
      <div style={{ padding: "12px 14px", background: T.bg, border: `1px solid ${T.line}`, borderRadius: 10, marginBottom: 16 }}>
        <div style={{ fontSize: 10.5, textTransform: "uppercase", letterSpacing: "0.12em", color: T.muted, fontWeight: 600, marginBottom: 4 }}>Email</div>
        <div style={{ fontSize: 14, fontWeight: 600, color: T.ink, userSelect: "all" }}>{SUPPORT_EMAIL}</div>
        <div style={{ fontSize: 11.5, color: T.muted2, marginTop: 4 }}>Subject: {subject}</div>
      </div>
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
        <a href={`mailto:${SUPPORT_EMAIL}?subject=${encodeURIComponent(subject)}`}
          style={{ flex: 1, display: "inline-flex", alignItems: "center", justifyContent: "center", gap: 8, padding: "11px 18px", background: T.gold, color: T.ink, borderRadius: 999, fontSize: 13, fontWeight: 700, textDecoration: "none", minWidth: 160 }}>
          <Mail size={15} /> Email us
        </a>
        <a href={SITE_URL} target="_blank" rel="noreferrer"
          style={{ flex: 1, display: "inline-flex", alignItems: "center", justifyContent: "center", gap: 8, padding: "10px 18px", background: T.card, border: `1px solid ${T.line}`, color: T.ink3, borderRadius: 999, fontSize: 13, fontWeight: 500, textDecoration: "none", minWidth: 160 }}>
          adventure.asia <ExternalLink size={13} />
        </a>
      </div>
    </Modal>
  );
}

// ─── Billing ──────────────────────────────────────────────────────────────────
type Plan = { plan_name: string; tours_quota_monthly: number; api_calls_quota_monthly: number; price_usd_monthly: number | null; rate_limit_rpm: number | null };

const fmtQuota = (n: number | null | undefined) => (n == null ? "—" : n >= 999999 ? "Unlimited" : n.toLocaleString());

// AA-760 — typed as the shell's /v1/billing value (was `any`); the fields are read defensively below.
// eslint-disable-next-line @typescript-eslint/no-explicit-any -- the plan fields are read loosely (number | string | null)
type BillingRecord = Record<string, any>;

export function BillingTab({ billing }: { billing: ShellBilling | null }) {
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
        <PageHeader title="Billing" sub="Your plan and usage for this billing period." />
        {billingStatus === "error"
          ? <LoadError what="your billing" onRetry={retryBilling} />
          : (
            <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
              <CardSkeleton lines={3} label="Loading your plan" />
              <CardSkeleton lines={2} label="Loading this period" />
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
  const month      = fmtBillingMonth(b?.billing_month);
  const plans: Plan[] = b?.plans ?? [];
  const pct = (u: number, t: number | null) => (t != null && t > 0 ? Math.min(100, (u / t) * 100) : 0);

  return (
    <div style={{ maxWidth: 760 }}>
      {contactPlan && (
        <ContactModal
          heading={`Switch to ${titleCase(contactPlan)}`}
          body="Plan changes are handled by the Adventure Asia team. Email us and we will switch your plan, usually within one business day."
          subject={`Plan change to ${titleCase(contactPlan)} — ${tenantName}`}
          onClose={() => setContactPlan(null)}
        />
      )}

      <PageHeader title="Billing" sub={`Your plan and usage for the ${month} billing period.`} />

      {/* Current plan */}
      <Card dark style={{ marginBottom: 16 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 16, flexWrap: "wrap" }}>
          <div>
            <div style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: "0.14em", color: "rgba(255,255,255,0.5)", marginBottom: 8 }}>Current plan</div>
            <div style={{ fontSize: 32, fontWeight: 600, color: T.onSolid, letterSpacing: "-0.02em" }}>{noPlan ? "No plan attached" : titleCase(plan || "—")}</div>
            <div style={{ fontSize: 14, color: "rgba(255,255,255,0.65)", marginTop: 4 }}>
              {noPlan ? "Contact us to set up your plan." : price > 0 ? `$${price.toLocaleString()} / month · billed monthly` : "Custom pricing"}
            </div>
          </div>
          <Btn variant="primary" onClick={() => setContactPlan(plans.find(p => p.plan_name !== plan && (p.price_usd_monthly ?? Infinity) > price)?.plan_name ?? "enterprise")}>
            Change plan
          </Btn>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(120px, 1fr))", gap: 16, marginTop: 20, paddingTop: 16, borderTop: "1px solid rgba(255,255,255,0.08)" }}>
          {[["Rewrites / month", fmtQuota(toursTotal)], ["API calls / month", fmtQuota(apiTotal)], ["Rate limit", rpm ? `${rpm.toLocaleString()} RPM` : "—"]].map(([l, v]) => (
            <div key={l}>
              <div style={{ fontSize: 10.5, textTransform: "uppercase", letterSpacing: "0.1em", color: "rgba(255,255,255,0.45)", marginBottom: 4 }}>{l}</div>
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
        <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em", color: T.muted, marginBottom: 16 }}>This period — {month}</div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 16 }}>
          {[
            { l: "API calls",       v: `${apiUsed.toLocaleString()} / ${fmtQuota(apiTotal)}`,     sub: `${pct(apiUsed, apiTotal).toFixed(0)}% used` },
            { l: "Amount due so far", v: noPlan ? "—" : `$${(price + overage).toLocaleString(undefined, { maximumFractionDigits: 2 })}`, sub: noPlan ? "No plan attached" : overage > 0 ? `Includes $${overage.toLocaleString()} overage` : "Plan fee, no overage" },
          ].map(({ l, v, sub }) => (
            <div key={l} style={{ padding: "14px 16px", background: T.bg, borderRadius: 8, border: `1px solid ${T.line}` }}>
              <div style={{ fontSize: 10.5, textTransform: "uppercase", letterSpacing: "0.1em", color: T.muted, fontWeight: 600, marginBottom: 6 }}>{l}</div>
              <div style={{ fontSize: 18, fontWeight: 700, color: T.ink, fontVariantNumeric: "tabular-nums" }}>{v}</div>
              <div style={{ fontSize: 11, color: T.muted2, marginTop: 4 }}>{sub}</div>
            </div>
          ))}
        </div>
        {/* AA-760 — what "API calls" counts (api/middleware/rate_limit.py): a portal session is not metered. */}
        <p data-testid="billing-api-calls-note" style={{ fontSize: 11.5, color: T.muted, lineHeight: 1.5, margin: "14px 0 0" }}>
          API calls are the requests your own systems send to the API with a token from your API key. Using this
          portal, including the API playground, is not counted; it does share your rate limit.
        </p>
      </Card>

      {/* AA-761 — AA-issued invoices. CIS stores none yet (the billing provider is still open): the empty state. */}
      <Card style={{ marginBottom: 16 }}>
        <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em", color: T.muted, marginBottom: 4 }}>Invoices</div>
        <div data-testid="billing-invoices">
          <KitEmptyState
            icon={<FileText size={28} />}
            title="No invoices yet"
            description="Invoices Adventure Asia issues to you will be listed here. For a copy of an earlier invoice, email us."
            action={<a href={`mailto:${SUPPORT_EMAIL}?subject=${encodeURIComponent(`Invoice copy — ${tenantName}`)}`}
              style={{ fontSize: 12.5, fontWeight: 600, color: T.ink3 }}>Email {SUPPORT_EMAIL}</a>}
          />
        </div>
      </Card>

      {/* Plan comparison — from shared.membership_plans via /v1/billing */}
      {plans.length > 0 && (
        <Card>
          <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em", color: T.muted, marginBottom: 16 }}>Compare plans</div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 12 }}>
            {plans.map(p => {
              const active = p.plan_name === plan;
              return (
                <div key={p.plan_name} style={{ padding: 14, borderRadius: 10, border: `${active ? 2 : 1}px solid ${active ? T.gold : T.line}`, background: active ? T.goldTint : T.bg, display: "flex", flexDirection: "column" }}>
                  <div style={{ fontSize: 13, fontWeight: 700, color: active ? T.amber : T.ink, marginBottom: 4 }}>{titleCase(p.plan_name)}</div>
                  <div style={{ fontSize: 20, fontWeight: 600, color: T.ink, marginBottom: 10, fontVariantNumeric: "tabular-nums" }}>
                    {p.price_usd_monthly != null ? <>${p.price_usd_monthly.toLocaleString()}<span style={{ fontSize: 11, color: T.muted, fontWeight: 400 }}>/mo</span></> : "Custom"}
                  </div>
                  {[`${fmtQuota(p.tours_quota_monthly)} rewrites / month`, `${fmtQuota(p.api_calls_quota_monthly)} API calls`, p.rate_limit_rpm ? `${p.rate_limit_rpm.toLocaleString()} RPM` : "Custom rate limit"].map(feat => (
                    <div key={feat} style={{ fontSize: 11.5, color: T.muted, marginBottom: 4, display: "flex", gap: 6, alignItems: "center" }}>
                      <Check size={12} color={T.green} style={{ flexShrink: 0 }} />{feat}
                    </div>
                  ))}
                  <div style={{ marginTop: "auto", paddingTop: 10 }}>
                    {active
                      ? <div style={{ fontSize: 11, fontWeight: 700, color: T.green, textAlign: "center", display: "flex", alignItems: "center", justifyContent: "center", gap: 4 }}><Check size={12} /> Current plan</div>
                      : <Btn size="sm" onClick={() => setContactPlan(p.plan_name)} style={{ width: "100%" }}>Switch</Btn>}
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
  if (failed) return <div style={{ marginBottom: 16 }}><LoadError what="your rewrite usage" onRetry={onRetry} /></div>;
  if (loading || !quota) {
    return <div style={{ marginBottom: 16 }}><CardSkeleton lines={2} label="Loading your rewrite usage" /></div>;
  }
  const m = usageMeter(quota, billing);
  const limitText = m.state === "unlimited" ? "Unlimited" : m.quota == null ? "—" : m.quota.toLocaleString();
  const tone = m.state === "over" ? T.red : m.state === "near" ? T.amber : T.green;
  return (
    <Card style={{ marginBottom: 16 }}>
      <div data-testid="billing-usage-meter" data-state={m.state}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 12, flexWrap: "wrap", marginBottom: 10 }}>
          <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em", color: T.muted }}>Rewrites — {month}</div>
          <div style={{ fontSize: 12, color: T.muted }}>Resets {fmtDay(m.resetsAt)}</div>
        </div>
        <div style={{ display: "flex", alignItems: "baseline", gap: 8, marginBottom: 10 }}>
          <span style={{ fontSize: 28, fontWeight: 700, color: T.ink, fontVariantNumeric: "tabular-nums" }}>{m.used.toLocaleString()}</span>
          <span style={{ fontSize: 14, color: T.muted, fontVariantNumeric: "tabular-nums" }}>of {limitText} rewrites</span>
        </div>
        {(m.state === "ok" || m.state === "near" || m.state === "over") && (
          <div role="meter" aria-label="Rewrites used this period" aria-valuemin={0} aria-valuemax={m.quota ?? 0}
            aria-valuenow={Math.min(m.used, m.quota ?? 0)}>
            <ProgressBar pct={m.pct} warn={m.state !== "ok"} />
          </div>
        )}
        <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 10, fontSize: 12.5, color: T.body, flexWrap: "wrap" }}>
          <span style={{ width: 8, height: 8, borderRadius: 999, background: tone, flexShrink: 0 }} aria-hidden />
          {m.state === "no_plan" && <span>No plan attached: rewrites are not billed yet.</span>}
          {m.state === "unlimited" && <span>Your plan has no rewrite limit.</span>}
          {m.state === "ok" && <span>{m.remaining?.toLocaleString()} rewrites left this period.</span>}
          {m.state === "near" && <span>{m.remaining?.toLocaleString()} rewrites left. Extra rewrites are billed as overage.</span>}
          {m.state === "over" && (
            <span>
              {m.overTours.toLocaleString()} over your quota · {fmtUsd(m.overageUsd)} overage so far
              {m.ratePerTour ? ` (${fmtUsd(m.ratePerTour)} per extra rewrite)` : ""}.
            </span>
          )}
        </div>
      </div>
    </Card>
  );
}

// ─── Settings ─────────────────────────────────────────────────────────────────
export function SettingsTab() {
  const { tenantName, planTier, tenantStatus } = usePortalShell();
  // AA-760 — skeleton text until /api/tenant/me answers (was the generic "Partner" + a blank plan).
  const loadingMe = tenantStatus === "pending";
  const skel = (w: number) => <SkeletonBar w={w} h={11} style={{ marginTop: 4 }} />;
  const [contact, setContact] = useState(false);

  const row: React.CSSProperties = {
    display: "flex", alignItems: "center", justifyContent: "space-between", gap: 16,
    padding: "13px 0", borderBottom: `1px solid ${T.line2}`,
  };
  const last: React.CSSProperties = { ...row, borderBottom: "none" };
  const label = (t: string, sub?: React.ReactNode) => (
    <div style={{ minWidth: 0 }}>
      <div style={{ fontSize: 13, fontWeight: 500, color: T.ink }}>{t}</div>
      {sub && <div style={{ fontSize: 11.5, color: T.muted2, marginTop: 2 }}>{sub}</div>}
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
          heading="Update account details"
          body="Company name, contacts and API keys are managed by the Adventure Asia team for now. Email us with the change and we will update it."
          subject={`Account change — ${tenantName}`}
          onClose={() => setContact(false)}
        />
      )}

      <SkeletonStyle />
      {/* AA-760 — "Account settings" (was "Team & Settings": there is no team section yet). */}
      <PageHeader title="Account settings" sub={loadingMe ? "Your account details." : `Account details for ${tenantName}.`} />

      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <Card>
          <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em", color: T.muted, marginBottom: 6 }}>Account</div>
          <div style={row}>{label("Company", loadingMe ? skel(120) : tenantName)}</div>
          <div style={row}>{label("Plan", loadingMe ? skel(70) : titleCase(planTier) || "No plan attached")}<a href="/portal/account/billing" style={{ fontSize: 12, color: T.ink3, fontWeight: 500, textDecoration: "none" }}>View billing →</a></div>
          <div style={last}>
            {label("Need to change something?", "Company name, contacts or a new API key")}
            <Btn size="sm" onClick={() => setContact(true)} style={{ whiteSpace: "nowrap", flexShrink: 0 }}><Mail size={13} /> Contact us</Btn>
          </div>
        </Card>

        <Card>
          <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em", color: T.muted, marginBottom: 6 }}>API access</div>
          <div style={last}>
            {label("API key", "Shown once when it was issued. Use it to call the Adventure Asia API.")}
            <a href="/portal/account/api" style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12, color: T.ink3, fontWeight: 500, textDecoration: "none", whiteSpace: "nowrap" }}>
              <KeyRound size={13} /> API docs →
            </a>
          </div>
        </Card>

        <Card>
          <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em", color: T.muted, marginBottom: 6 }}>Session</div>
          <div style={last}>
            {label("Sign out", "Ends your session on this browser.")}
            <Btn size="sm" variant="danger" onClick={signOut}><LogOut size={13} /> Sign out</Btn>
          </div>
        </Card>
      </div>
    </div>
  );
}
