"use client";
// web/app/(studio)/portal/_components/DashboardTab.tsx
// API: GET /api/tenant/v1/billing (AA-496 — was /api/admin/billing, guaranteed 401 for every
//      real tenant since that proxy requires an admin JWT; see api/routers/v1_tours.py's
//      GET /v1/billing for the real tenant-scoped sibling)
//      GET /api/tenant/v1/tours/pool?page_size=1 (for total count)

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ArrowRight, FileText, Code2, Globe2, BookOpen, Sparkles, Clock, AlertTriangle, Check, X, RefreshCw } from "lucide-react";
import { T, Card, CardHead, CtaLink, ProgressBar, TextLink } from "./ui";
import { fmtDateTime, fmtBillingMonth } from "@/lib/studio/format";
import { ActivityStateBadge } from "./workStatus";
import { usePortalShell } from "./PortalShellContext";
import { LinkButton, PageHeader, apiGet, useIsPhone, K, mono, sans, serif, useLang, useT, type Translator } from "@/app/_kit";
import { STUDIO_MESSAGES } from "@/app/_kit/local/studio/studio.messages";
import { fmtCount } from "@/lib/studio/format";
import { DASHBOARD_MESSAGES } from "./dashboard.messages";

type DashT = Translator<typeof DASHBOARD_MESSAGES.en>;
import GettingStarted from "./GettingStarted";
import UsageSparkline, { type DailyPoint } from "./UsageSparkline";
import { DashboardSkeleton } from "./Skeleton";

interface BillingData {
  tenant_name: string | null; plan_tier: string | null; tours_quota_monthly: number | null;
  api_calls_quota_monthly: number | null; price_usd_monthly: number | null;
  plan_attached?: boolean;  // AA-798 — false: no plan; plan fields are null
  tours_rewritten: number; api_calls_used: number;
  quota_tours_pct: number; quota_calls_pct: number;
  billing_month: string;
  overage_usd: number; overage_rate_usd_per_tour: number; tours_overage?: number;
  rate_limit_rpm?: number | null;
  daily?: DailyPoint[];
  activity: { id: string; status: string; edit_source: string; tour_name: string; country: string | null; created_at: string }[];
}

// AA-430: was onTabChange(t: Tab) — portal is real routes now, so this just takes the
// destination href directly (router.push in the page.tsx that renders this component).
export default function DashboardTab({ onNavigate }: { onNavigate: (href: string) => void }) {
  const t = useT(DASHBOARD_MESSAGES);
  const tShell = useT(STUDIO_MESSAGES);
  const lang = useLang();
  const n = (v: number) => fmtCount(v, lang);
  const [dismissAlert, setDismissAlert] = useState(false);
  // AA-760 — billing, the pool total and the activity feed come from the shell's react-query
  // queries (they were fetched a second time here, in an effect); only /v1/quota is page-local.
  const { tenantName, catTotal, poolTotal: pool, activity } = usePortalShell();
  // Same key + request as the shell's billing query (one cached answer); read here for its state.
  const billingQ = useQuery({
    queryKey: ["tenant-billing"],
    queryFn: () => apiGet<BillingData>("/api/tenant/v1/billing"),
  });
  const quotaQ = useQuery({
    queryKey: ["tenant-quota"],
    queryFn: () => apiGet<{ resets_at?: string | null }>("/api/tenant/v1/quota"),
  });
  const resetsAt = quotaQ.data?.resets_at ?? null;
  const phone = useIsPhone(); // AA-836 — Recent Activity drops its short-date column on a phone

  if (billingQ.isPending) return <DashboardSkeleton />;
  if (billingQ.isError) {
    return (
      <div role="alert" style={{ padding: "28px 20px", textAlign: "center", color: K.muted, fontSize: 13 }}>
        {t("loadFailed")}{" "}
        <LinkButton size="md" onClick={() => billingQ.refetch()}>
          {t("tryAgain")}
        </LinkButton>
      </div>
    );
  }

  const b = billingQ.data;
  const toursUsed  = b?.tours_rewritten ?? 0;
  // AA-798 — no invented Growth plan (was 200 tours / 20,000 calls / "growth" when missing).
  const noPlan     = b?.plan_attached === false;
  const toursTotal = b?.tours_quota_monthly ?? null;
  const toursPct   = b?.quota_tours_pct ?? 0;
  const apiUsed    = b?.api_calls_used ?? 0;
  const apiTotal   = b?.api_calls_quota_monthly ?? null;
  const apiPct     = b?.quota_calls_pct ?? 0;
  const price      = b?.price_usd_monthly ?? 0;
  const planName   = b?.plan_tier ?? "";
  const planLabel  = noPlan || !planName ? t("noPlan") : planName.charAt(0).toUpperCase() + planName.slice(1);
  const fmtTotal   = (v: number | null) => (v == null ? "—" : n(v));
  const month      = fmtBillingMonth(b?.billing_month, lang);
  const overage    = b?.overage_usd ?? 0;
  const rpm        = b?.rate_limit_rpm ?? null;
  // AA-636: was the literal "22 days". Quota resets on the 1st of next month (UTC) — the same
  // rule the backend enforces (v1_tours._current_year_month_and_reset); /v1/quota serves the date.
  const resetDays  = daysUntil(resetsAt ?? firstOfNextMonthUTC());

  return (
    <div style={{ fontFamily: sans }}>
      <PageHeader
        title={tenantName && tenantName !== tShell("partnerFallback") ? t("welcome", tenantName) : t("title")}
        description={t("sub", month)}
      />

      {/* Usage alert — AA-636: was "> 30%" with a hardcoded "Growth plan = 300 RPM" for every plan */}
      {!dismissAlert && (apiPct >= 80 || toursPct >= 80) && (
        <div role="status" style={{ background: K.amberSoft, border: `1px solid ${K.accentSoft}`, borderLeft: `4px solid ${K.amber}`, borderRadius: "0 8px 8px 0", padding: "11px 16px", marginBottom: 20, display: "flex", alignItems: "center", gap: 12 }}>
          <AlertTriangle size={16} color={K.amber} style={{ flexShrink: 0 }} />
          <div style={{ flex: 1, fontSize: 13, color: "var(--aa-amber-deep)", lineHeight: 1.5 }}>
            <strong>{apiPct >= toursPct ? t("usedApi", Math.max(apiPct, toursPct).toFixed(0)) : t("usedRewrites", Math.max(apiPct, toursPct).toFixed(0))}</strong>{" "}
            {t("resetsIn", resetDays)} <TextLink href="/portal/account/billing">{t("comparePlans")}</TextLink>
          </div>
          <button onClick={() => setDismissAlert(true)} aria-label={t("dismiss")} style={{ background: "none", border: "none", color: K.amber, cursor: "pointer", padding: 0, display: "flex" }}><X size={16} /></button>
        </div>
      )}

      {/* Row 1: Membership · Quota · Re-rewrite */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: 18 }}>

        {/* Membership — dark */}
        <Card dark>
          <CardHead title={t("membership")} light action={
            <button onClick={() => onNavigate("/portal/account/billing")} style={{ background: K.accent, color: "var(--aa-side-bg)", border: 0, fontWeight: 700, fontSize: 12, padding: "7px 14px", borderRadius: 6, cursor: "pointer", fontFamily: sans }}>
              {t("upgrade")}
            </button>
          } />
          {/* Active pill — in normal flow under the title (was position:absolute on top of it) */}
          <div style={{ marginTop: 2 }}>
            <span style={{ display: "inline-flex", alignItems: "center", gap: 6, padding: "3px 9px", background: "rgba(219,150,40,0.15)", color: K.accentDeep, borderRadius: 999, fontSize: 11, fontWeight: 600, letterSpacing: "0.04em", textTransform: "uppercase" }}>
              <span style={{ width: 5, height: 5, background: K.accent, borderRadius: "50%", display: "block" }} />{t("active")}
            </span>
          </div>
          <div style={{ marginTop: 16 }}>
            <div style={{ fontFamily: serif, fontSize: 36, fontWeight: 500, letterSpacing: "-0.02em", color: T.onSolid, lineHeight: 1 }}>
              {planLabel}
            </div>
            <div style={{ display: "flex", alignItems: "baseline", gap: 4, marginTop: 8, color: "rgba(255,255,255,0.65)" }}>
              <span style={{ fontSize: 22, fontWeight: 600, color: T.onSolid, letterSpacing: "-0.01em" }}>${n(price)}</span>
              <span style={{ fontSize: 12 }}>{t("perMonth")}</span>
            </div>
          </div>
          <div style={{ display: "flex", gap: 22, marginTop: 22, paddingTop: 18, borderTop: "1px solid rgba(255,255,255,0.08)" }}>
            {[[t("rewritesMo"), fmtTotal(toursTotal)], [t("apiCalls"), fmtTotal(apiTotal)], [t("rateLimit"), rpm ? t("rpm", n(rpm)) : "—"]].map(([l, v]) => (
              <div key={l}>
                <div style={{ fontSize: 10.5, textTransform: "uppercase", letterSpacing: "0.12em", color: "rgba(255,255,255,0.45)", marginBottom: 4 }}>{l}</div>
                <div style={{ fontSize: 12.5, fontWeight: 600, color: T.onSolid, fontVariantNumeric: "tabular-nums" }}>{v}</div>
              </div>
            ))}
          </div>
        </Card>

        {/* Quota */}
        <Card>
          <CardHead title={t("quotaUsage")} action={<TextLink href="/portal/account/billing">{t("details")}</TextLink>} />
          <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
            {/* AA-760 — the quota counts rewrites (versions), not tours: "Tours rewritten 12" next to
                "My Tours 3" read as two answers to one question. */}
            <QuotaRow icon={<FileText size={13} color={K.accent} />} label={t("rewritesUsed")} used={toursUsed} total={toursTotal} pct={toursPct} warn={toursPct >= 80}
              note={catTotal > 0 ? t("toursInMyTours", n(catTotal), catTotal === 1) : undefined} t={t} n={n} />
            <QuotaRow icon={<Code2 size={13} color={K.accent} />} label={t("apiCallsRow")} used={apiUsed} total={apiTotal} pct={apiPct} warn={apiPct >= 80} t={t} n={n} />
          </div>
          {(b?.daily?.length ?? 0) >= 2 && (
            <div style={{ marginTop: 18 }}>
              <UsageSparkline data={b!.daily!} metric="api_calls" label={t("apiCallsRow")} />
            </div>
          )}
          <div style={{ marginTop: 18, paddingTop: 14, borderTop: `1px dashed ${K.line}`, display: "flex", alignItems: "center", gap: 8, fontSize: 12, color: K.muted }}>
            <Clock size={13} color={K.muted2} /> {t("resetsInShort")} <strong style={{ color: K.ink }}>{t("days", resetDays)}</strong> · {month}
          </div>
        </Card>

        {/* AA-638 — getting started (hides itself once every step is done) */}
        <GettingStarted catalogCount={catTotal} />
      </div>

      {/* Row 2: Spend + Activity */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(300px, 1fr))", gap: 18, marginTop: 18 }}>

        {/* This month — AA-636: was "LLM Cost $0.0000" + "~$0.018/tour · Bedrock" (internal
            cost / model names). Tenants see what they pay for: plan fee, usage, any overage. */}
        <Card>
          <CardHead title={t("thisMonth", month)} action={<TextLink href="/portal/account/billing">{t("viewBilling")}</TextLink>} />
          <div style={{ display: "flex", alignItems: "flex-end", justifyContent: "space-between", gap: 16, flexWrap: "wrap" }}>
            <div>
              <div style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: "0.12em", color: K.muted, marginBottom: 6, fontWeight: 600 }}>{t("amountDue")}</div>
              <div style={{ fontFamily: sans, fontVariantNumeric: "tabular-nums", fontSize: 36, fontWeight: 600, color: K.ink, letterSpacing: "-0.02em", lineHeight: 1 }}>
                ${(price + overage).toLocaleString(lang === "vi" ? "vi-VN" : "en-US", { maximumFractionDigits: 2 })}
              </div>
            </div>
            <span style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: 12, fontWeight: 600, color: overage > 0 ? K.amber : K.success, background: overage > 0 ? K.amberSoft : K.successSoft, padding: "3px 8px", borderRadius: 5 }}>
              {overage > 0 ? <AlertTriangle size={12} /> : <Check size={12} />}
              {overage > 0 ? t("includesOverage") : t("withinPlan")}
            </span>
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: 12, marginTop: 18 }}>
            <SpendTile label={t("planFee")} value={`$${n(price)}`} sub={t("planFeeSub", planLabel)} />
            <SpendTile label={t("rewritesThisMonth")} value={`${n(toursUsed)} / ${fmtTotal(toursTotal)}`}
              sub={overage > 0 ? t("overage", n(overage)) : t("noOverage")} warn={overage > 0} />
          </div>
        </Card>

        {/* Activity */}
        <div style={{ background: K.card, border: `1px solid ${K.line}`, borderRadius: 12, padding: "22px 22px 8px" }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 6 }}>
            <span style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em", color: K.muted }}>{t("recentActivity")}</span>
            <TextLink href="/portal/account/activity">{t("viewAll")}</TextLink>
          </div>
          {activity.length === 0 ? (
            // AA-760 — an empty feed offers its next step (was text only).
            <div data-testid="dashboard-activity-empty" style={{ padding: "28px 0 24px", textAlign: "center", color: K.muted2, fontSize: 13, display: "flex", flexDirection: "column", alignItems: "center", gap: 12 }}>
              {t("noActivity")}
              <CtaLink href="/portal/tours">{t("browseTours")}</CtaLink>
            </div>
          ) : activity.slice(0, 5).map((a, i) => {
            // AA-760 — state from the version's job (shell activity view), not the row status.
            const bad       = a.state === "rejected" || a.state === "failed";
            const iconBg    = a.state === "approved" ? K.successSoft : bad ? K.dangerSoft : K.accentTint;
            const iconColor = a.state === "approved" ? K.success    : bad ? K.danger     : K.amber;
            const Icon      = a.state === "approved" ? Check : bad ? X : RefreshCw;
            return (
              // AA-836 — title column can shrink (minmax(0, 1fr)) and the tour name is one line with
              // an ellipsis; on a phone the trailing short date goes (the full date/time is already
              // under the title), which gives the name the room instead of 1–2 words per line.
              // S241 UI verify — on a phone the status badge (~140px) still left the name ~10
              // characters; it now moves to the meta line under the name, so the name gets the
              // whole row (grid is just icon + text) and stays one line with an ellipsis.
              <div key={a.id} style={{ display: "grid", gridTemplateColumns: phone ? "36px minmax(0, 1fr)" : "36px minmax(0, 1fr) auto auto", alignItems: "center", gap: phone ? 10 : 12, padding: "13px 0", borderTop: i === 0 ? "none" : `1px solid ${K.line2}` }}>
                <div style={{ width: 36, height: 36, borderRadius: 8, background: iconBg, color: iconColor, display: "grid", placeItems: "center" }}>
                  <Icon size={15} strokeWidth={2.2} />
                </div>
                <div style={{ minWidth: 0 }}>
                  {/* AA-760 — on desktop the name may wrap to 2 lines (was cut at ~28 characters), and
                      the version number ("v3") tells repeat rows of one tour apart. */}
                  <div style={{ display: "flex", alignItems: "baseline", minWidth: 0, fontSize: 13, fontWeight: 500, color: K.ink, lineHeight: 1.3 }}>
                    <strong title={a.tour_name || t("tour")} style={phone ? ONE_LINE : TWO_LINES}>
                      {a.tour_name || t("tour")}
                    </strong>
                    <span style={{ color: K.muted2, fontSize: 11, marginLeft: 6, fontWeight: 400, flexShrink: 0, whiteSpace: "nowrap" }}>
                      · {a.version_number != null ? `v${a.version_number} · ` : ""}{a.edit_source === "ai_generated" ? t("ai") : t("edited")}
                    </span>
                  </div>
                  {phone ? (
                    <div style={{ display: "flex", alignItems: "center", flexWrap: "wrap", gap: "4px 8px", marginTop: 4, minWidth: 0 }}>
                      <span style={{ fontSize: 11, color: K.muted, whiteSpace: "nowrap" }}>{fmtDateTime(a.created_at, lang)}</span>
                      <ActivityStateBadge state={a.state} />
                    </div>
                  ) : (
                    <div style={{ fontSize: 11, color: K.muted, marginTop: 2 }}>{fmtDateTime(a.created_at, lang)}</div>
                  )}
                </div>
                {!phone && <ActivityStateBadge state={a.state} />}
                {!phone && (
                  <div style={{ fontSize: 11, color: K.muted2, fontFamily: mono, whiteSpace: "nowrap" }}>
                    {a.created_at ? new Date(a.created_at).toLocaleDateString(lang === "vi" ? "vi-VN" : "en-GB", { day: "numeric", month: "short" }) : "—"}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>

      {/* Row 3: Quick Actions */}
      <div style={{ marginTop: 18 }}>
        <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em", color: K.muted, marginBottom: 12 }}>
          {t("quickActions")}
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))", gap: 14 }}>
          {[
            { icon: <Globe2 size={18} />, title: t("qaMarketplace"), sub: pool != null ? t("qaPoolCount", n(pool)) : t("qaPool"), href: "/portal/tours" }, // AA-576 Phần 3 (was "Browse Pool"); AA-671: "Browse Tours" → "Marketplace"
            { icon: <BookOpen size={18} />, title: t("qaMyTours"), sub: t("qaMyToursSub", n(toursUsed)), href: "/portal/tours/mine" }, // AA-576 Phần 3 (was "My Catalog")
            { icon: <Sparkles size={18} />, title: t("qaBrand"), sub: t("qaBrandSub"), href: "/portal/brand" },
          ].map(a => (
            <ActionCard key={a.href} icon={a.icon} title={a.title} sub={a.sub} onClick={() => onNavigate(a.href)} />
          ))}
        </div>
      </div>
    </div>
  );
}

// ── Helpers ────────────────────────────────────────────────────────────────────

const ONE_LINE: React.CSSProperties = { minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" };
const TWO_LINES: React.CSSProperties = {
  minWidth: 0, overflow: "hidden", display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical",
};

function firstOfNextMonthUTC(): string {
  const now = new Date();
  return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 1)).toISOString().slice(0, 10);
}

// Whole days from now until 00:00 UTC of `isoDate` — at least 1 while still inside the month.
function daysUntil(isoDate: string): number {
  const target = Date.parse(isoDate.length === 10 ? `${isoDate}T00:00:00Z` : isoDate);
  if (Number.isNaN(target)) return 0;
  return Math.max(1, Math.ceil((target - Date.now()) / 86_400_000));
}

// ── Sub-components ─────────────────────────────────────────────────────────────

function QuotaRow({ icon, label, used, total, pct, warn = false, note, t, n }: {
  icon: React.ReactNode; label: string; used: number; total: number | null; pct: number; warn?: boolean;
  t: DashT; n: (v: number) => string;
  /** A muted line under the bar (e.g. "3 tours in My Tours"). */
  note?: string;
}) {
  return (
    <div>
      <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", marginBottom: 8 }}>
        <div style={{ fontSize: 13, color: K.ink, fontWeight: 500, display: "flex", alignItems: "center", gap: 7 }}>
          {icon} {label}
        </div>
        <div style={{ fontFamily: mono, fontSize: 12, color: K.ink, fontVariantNumeric: "tabular-nums" }}>
          {n(used)} <span style={{ color: K.muted2 }}>/ {total == null ? "—" : n(total)}</span>
        </div>
      </div>
      <ProgressBar pct={pct} warn={warn} />
      <div style={{ fontSize: 11, color: warn ? K.amber : K.muted, marginTop: 6, display: "flex", justifyContent: "space-between" }}>
        <span>{t("pctUsed", pct.toFixed(0))}</span>
        <span style={{ fontWeight: warn ? 600 : 400 }}>
          {warn ? t("approaching") : total == null ? t("noPlan") : t("remaining", n(Math.max(total - used, 0)))}
        </span>
      </div>
      {note && <div style={{ fontSize: 11, color: K.muted, marginTop: 4 }}>{note}</div>}
    </div>
  );
}

function SpendTile({ label, value, sub, warn = false }: {
  label: string; value: string; sub?: string; warn?: boolean;
}) {
  return (
    <div style={{ background: warn ? K.amberSoft : K.bg, border: `1px solid ${warn ? K.accentSoft : K.line2}`, borderRadius: 8, padding: "12px 14px" }}>
      <div style={{ fontSize: 10.5, textTransform: "uppercase", letterSpacing: "0.1em", color: K.muted, fontWeight: 600, marginBottom: 5 }}>{label}</div>
      <div style={{ fontSize: 16, fontWeight: 600, color: K.ink, fontVariantNumeric: "tabular-nums" }}>{value}</div>
      {sub && <div style={{ fontSize: 11, color: K.muted2, marginTop: 4 }}>{sub}</div>}
    </div>
  );
}

function ActionCard({ icon, title, sub, onClick }: {
  icon: React.ReactNode; title: string; sub: string; onClick: () => void;
}) {
  const [hov, setHov] = useState(false);
  return (
    <button onClick={onClick}
      onMouseEnter={() => setHov(true)} onMouseLeave={() => setHov(false)}
      style={{
        background: K.card, border: `1px solid ${hov ? K.accent : K.line}`, borderRadius: 12,
        padding: "18px 20px", display: "flex", alignItems: "center", gap: 14,
        cursor: "pointer", textAlign: "left", fontFamily: sans,
        transform: hov ? "translateY(-1px)" : "none",
        boxShadow: hov ? "0 4px 12px -6px rgba(219,150,40,0.35)" : "none",
        transition: "all .15s",
      }}>
      <div style={{ width: 40, height: 40, borderRadius: 9, background: T.darkPanel, color: K.accent, display: "grid", placeItems: "center", flexShrink: 0 }}>
        {icon}
      </div>
      <div style={{ flex: 1 }}>
        <div style={{ fontSize: 14, fontWeight: 600, color: K.ink, lineHeight: 1.2 }}>{title}</div>
        <div style={{ fontSize: 12, color: K.muted, marginTop: 3 }}>{sub}</div>
      </div>
      <ArrowRight size={15} color={K.muted2} />
    </button>
  );
}
