"use client";
import { useState, useEffect, useCallback } from "react";
import {
  LayoutDashboard, Search, BookOpen, Tag, Key,
  CheckCircle, XCircle, RotateCcw, Copy,
  ChevronRight, Loader2, Globe, Star, Filter,
  Eye, EyeOff, AlertCircle, Package,
} from "lucide-react";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "https://api-cis.lumiguides.it.com";
type Tab = "dashboard" | "pool" | "catalog" | "brand" | "apikey";

function getCookie(n: string) {
  if (typeof document === "undefined") return "";
  const m = document.cookie.match(new RegExp(`(^| )${n}=([^;]+)`));
  return m ? decodeURIComponent(m[2]) : "";
}

// ── Shared helpers ────────────────────────────────────────────────────────────

function ScoreBadge({ score }: { score: number }) {
  const color = score >= 9 ? "#22c55e" : score >= 7 ? "#f59e0b" : "#ef4444";
  return (
    <span style={{
      padding: "2px 8px", borderRadius: 20, fontSize: 11, fontWeight: 700,
      background: `${color}22`, color,
    }}>{score.toFixed(1)}</span>
  );
}

function TabBtn({ id, active, icon, label, onClick }: {
  id: Tab; active: boolean; icon: React.ReactNode; label: string; onClick: () => void;
}) {
  return (
    <button onClick={onClick} style={{
      display: "flex", alignItems: "center", gap: 7,
      padding: "9px 16px", borderRadius: 8, border: "none",
      background: active ? "var(--brand-gold)" : "var(--bg-card)",
      color: active ? "white" : "var(--text-secondary)",
      fontSize: 13, fontWeight: active ? 700 : 500, cursor: "pointer",
      transition: "all 0.15s",
      boxShadow: active ? "0 2px 8px rgba(219,150,40,0.3)" : "none",
    }}>
      {icon}<span>{label}</span>
    </button>
  );
}

// ── Dashboard Tab ─────────────────────────────────────────────────────────────

function QuotaBar({ label, used, total, pct, color = "var(--brand-gold)" }: {
  label: string; used: number; total: number; pct: number; color?: string;
}) {
  const warn = pct >= 80;
  return (
    <div style={{ marginBottom: 16 }}>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12,
        marginBottom: 6 }}>
        <span style={{ color: "var(--text-secondary)", fontWeight: 600 }}>{label}</span>
        <span style={{ color: warn ? "#f59e0b" : "var(--text-muted)" }}>
          {used.toLocaleString()} / {total.toLocaleString()} ({pct}%)
        </span>
      </div>
      <div style={{ height: 8, background: "var(--border)", borderRadius: 4, overflow: "hidden" }}>
        <div style={{
          height: "100%", borderRadius: 4, transition: "width 0.5s",
          width: `${Math.min(100, pct)}%`,
          background: warn
            ? "linear-gradient(90deg,#f59e0b,#ef4444)"
            : `linear-gradient(90deg,${color},#f59e0b)`,
        }}/>
      </div>
      {warn && (
        <div style={{ fontSize: 11, color: "#f59e0b", marginTop: 4 }}>
          ⚠ {pct >= 100 ? "Quota exceeded — overage charges apply" : "Approaching quota limit"}
        </div>
      )}
    </div>
  );
}

function DashboardTab({ planTier, onTabChange }: { planTier: string; onTabChange: (tab: Tab) => void }) {
  const [billing, setBilling] = useState<any>(null);
  const [pool, setPool]       = useState<number>(0);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([
      fetch("/api/tenant/v1/pipeline/billing"),
      fetch("/api/tenant/v1/tours/pool?page_size=1"),
    ]).then(async ([bRes, pRes]) => {
      if (bRes.ok) setBilling(await bRes.json());
      if (pRes.ok) { const d = await pRes.json(); setPool(d.pagination?.total ?? 0); }
    }).catch(() => {}).finally(() => setLoading(false));
  }, []);

  if (loading) return (
    <div style={{ textAlign: "center", padding: 40, color: "var(--text-muted)" }}>
      <Loader2 size={20} style={{ margin: "0 auto 8px", display: "block" }}/>Loading...
    </div>
  );

  const PLAN_COLOR: Record<string,string> = {
    starter: "#60a5fa", growth: "#a78bfa",
    business: "#34d399", enterprise: "#f59e0b", internal: "#f87171",
  };
  const planColor = PLAN_COLOR[billing?.plan_tier ?? planTier] ?? "var(--brand-gold)";

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>

      {/* Membership + Quota row */}
      <div style={{ display: "grid", gridTemplateColumns: "280px 1fr", gap: 16 }}>
        {/* Membership card */}
        <div style={{ background: "var(--bg-card)", border: `1px solid ${planColor}44`,
          borderRadius: 12, padding: 20 }}>
          <div style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)",
            textTransform: "uppercase", letterSpacing: 1, marginBottom: 12 }}>
            Membership
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 16 }}>
            <div style={{ width: 42, height: 42, borderRadius: 10,
              background: `${planColor}22`, display: "flex", alignItems: "center",
              justifyContent: "center" }}>
              <Star size={20} style={{ color: planColor }}/>
            </div>
            <div>
              <div style={{ fontSize: 18, fontWeight: 800, color: planColor,
                textTransform: "uppercase" }}>
                {billing?.plan_tier ?? planTier}
              </div>
              <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
                ${billing?.price_usd_monthly ?? 0}/month
              </div>
            </div>
          </div>
          <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 4 }}>
            Billing month: <span style={{ color: "var(--text-secondary)" }}>
              {billing?.billing_month ?? "—"}
            </span>
          </div>
          {billing?.overage_usd > 0 && (
            <div style={{ marginTop: 10, padding: "8px 12px",
              background: "rgba(239,68,68,0.1)", borderRadius: 8,
              fontSize: 12, color: "#f87171" }}>
              Overage: ${billing.overage_usd.toFixed(2)}
              ({billing.tours_overage} tours × ${billing.overage_rate_usd_per_tour})
            </div>
          )}
        </div>

        {/* Quota card */}
        <div style={{ background: "var(--bg-card)", border: "1px solid var(--border)",
          borderRadius: 12, padding: 20 }}>
          <div style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)",
            textTransform: "uppercase", letterSpacing: 1, marginBottom: 16 }}>
            Quota Usage — {billing?.billing_month ?? "This Month"}
          </div>
          <QuotaBar
            label="Tour Rewrites"
            used={billing?.tours_rewritten ?? 0}
            total={billing?.tours_quota_monthly ?? 50}
            pct={billing?.quota_tours_pct ?? 0}
          />
          <QuotaBar
            label="API Calls"
            used={billing?.api_calls_used ?? 0}
            total={billing?.api_calls_quota_monthly ?? 5000}
            pct={billing?.quota_calls_pct ?? 0}
            color="#a78bfa"
          />
          <div style={{ display: "flex", gap: 20, marginTop: 8 }}>
            <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
              Pool available: <span style={{ color: "var(--brand-gold)",
                fontWeight: 700 }}>{pool.toLocaleString()} tours</span>
            </div>
            <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
              LLM cost: <span style={{ color: "var(--text-primary)",
                fontWeight: 700 }}>${(billing?.llm_cost_usd ?? 0).toFixed(4)}</span>
            </div>
          </div>
        </div>
      </div>

      {/* Activity feed */}
      <div style={{ background: "var(--bg-card)", border: "1px solid var(--border)",
        borderRadius: 12, padding: 20 }}>
        <div style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)",
          textTransform: "uppercase", letterSpacing: 1, marginBottom: 14 }}>
          Recent Activity
        </div>
        {billing?.activity?.length > 0 ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {billing.activity.map((a: any) => {
              const statusColor = a.status === "approved" ? "#22c55e"
                : a.status === "rejected" ? "#ef4444" : "#f59e0b";
              return (
                <div key={a.id} style={{ display: "flex", alignItems: "center",
                  gap: 12, fontSize: 13, padding: "8px 12px",
                  background: "var(--bg-primary)", borderRadius: 8 }}>
                  <div style={{ width: 8, height: 8, borderRadius: "50%", flexShrink: 0,
                    background: statusColor }}/>
                  <div style={{ flex: 1, color: "var(--text-primary)" }}>
                    {a.tour_name || "Tour"}
                    {a.country && <span style={{ color: "var(--text-muted)",
                      fontSize: 11 }}> · {a.country}</span>}
                  </div>
                  <span style={{ fontSize: 11, padding: "2px 8px", borderRadius: 20,
                    background: `${statusColor}18`, color: statusColor,
                    fontWeight: 600 }}>{a.status}</span>
                  <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
                    {new Date(a.created_at).toLocaleDateString("en-GB", {
                      day: "2-digit", month: "short" })}
                  </span>
                </div>
              );
            })}
          </div>
        ) : (
          <div style={{ textAlign: "center", padding: "20px 0",
            color: "var(--text-muted)", fontSize: 13 }}>
            No activity yet — browse the pool to start rewriting tours
          </div>
        )}
      </div>

      {/* Quick actions */}
      <div style={{ background: "var(--bg-card)", border: "1px solid var(--border)",
        borderRadius: 12, padding: 20 }}>
        <div style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)",
          textTransform: "uppercase", letterSpacing: 1, marginBottom: 12 }}>
          Quick Actions
        </div>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap" as const }}>
          {[
            { label: "Browse Pool", tab: "pool", icon: <BookOpen size={14}/> },
            { label: "My Catalog",  tab: "catalog", icon: <Package size={14}/> },
            { label: "Brand Rules", tab: "brand", icon: <Star size={14}/> },
            { label: "API Access",  tab: "apikey", icon: <Key size={14}/> },
          ].map(a => (
            <button key={a.label}
              onClick={() => onTabChange(a.tab as Tab)}
              style={{ display: "flex", alignItems: "center", gap: 8,
                padding: "9px 16px", borderRadius: 8, border: "1px solid var(--border)",
                background: "var(--bg-primary)", color: "var(--text-secondary)",
                fontSize: 13, fontWeight: 500, cursor: "pointer",
                transition: "all 0.15s" }}>
              {a.icon}{a.label}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

// ── Browse Pool Tab ───────────────────────────────────────────────────────────

function PoolTab({ onRewrite }: { onRewrite: (tour: any) => void }) {
  const [tours, setTours]           = useState<any[]>([]);
  const [countries, setCountries]   = useState<string[]>([]);
  const [loading, setLoading]       = useState(true);
  const [search, setSearch]         = useState("");
  const [country, setCountry]       = useState("");
  const [page, setPage]             = useState(1);
  const [total, setTotal]           = useState(0);
  const [selected, setSelected]     = useState<any | null>(null);
  const [checkedIds, setCheckedIds] = useState<Set<string>>(new Set());
  const [rewriting, setRewriting]   = useState(false);
  const [language, setLanguage]     = useState("en-US");
  const [seoMode, setSeoMode]       = useState("standard");
  const [useBrandRules, setUseBrand] = useState(true);
  const [brandRules, setBrandRules] = useState<any>(null);
  const [panelTab, setPanelTab]     = useState<"details" | "rewrite">("details");

  const PAGE_SIZE = 20;

  // Fetch brand rules to show in panel
  useEffect(() => {
    fetch("/api/tenant/v1/pipeline/brand-identity")
      .then(r => r.ok ? r.json() : null)
      .then(d => setBrandRules(d))
      .catch(() => {});
  }, []);

  const fetchPool = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams({
        page: String(page), page_size: String(PAGE_SIZE),
        ...(country && { country }),
        ...(search  && { search }),
      });
      const res = await fetch(`/api/tenant/v1/tours/pool?${params}`);
      if (res.ok) {
        const d = await res.json();
        setTours(d.data ?? []);
        setTotal(d.pagination?.total ?? 0);
        if (d.countries?.length) setCountries(d.countries);
      }
    } catch {} finally { setLoading(false); }
  }, [page, country, search]);

  useEffect(() => { fetchPool(); }, [fetchPool]);

  const toggleCheck = (id: string, e: React.MouseEvent) => {
    e.stopPropagation();
    setCheckedIds(prev => {
      const n = new Set(prev);
      n.has(id) ? n.delete(id) : n.add(id);
      return n;
    });
  };

  const triggerRewrite = async (tourIds: string[]) => {
    setRewriting(true);
    let lastResult: any = null;
    try {
      for (const tid of tourIds) {
        const res = await fetch(`/api/tenant/v1/tours/pool/${tid}/rewrite`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ rewrite_language: language, seo_mode: seoMode }),
        });
        if (res.ok) lastResult = await res.json();
      }
      if (lastResult) onRewrite({ ...lastResult, count: tourIds.length });
      setSelected(null);
      setCheckedIds(new Set());
    } catch {} finally { setRewriting(false); }
  };

  const rewriteTargets = checkedIds.size > 0
    ? Array.from(checkedIds)
    : selected ? [selected.id] : [];

  // Parse SEO keywords
  const parseSeoKeywords = (raw: any): string[] => {
    if (!raw) return [];
    try {
      // Handle double-encoded: '"[]"' or '{"top_keywords":[...]}'
      let parsed = typeof raw === "string" ? JSON.parse(raw) : raw;
      // If still a string after first parse (double-encoded), parse again
      if (typeof parsed === "string") parsed = JSON.parse(parsed);
      if (Array.isArray(parsed)) return parsed.slice(0, 6);
      if (parsed?.top_keywords) return parsed.top_keywords.slice(0, 6);
    } catch {}
    return [];
  };

  return (
    <div style={{ display: "grid", gridTemplateColumns: selected ? "1fr 420px" : "1fr",
      gap: 20, alignItems: "start" }}>
      {/* LEFT — filters + tour list */}
      <div>
        {/* Filters */}
        <div style={{ display: "flex", gap: 8, marginBottom: 14, flexWrap: "wrap" as const }}>
          <div style={{ position: "relative", flex: 1, minWidth: 180 }}>
            <Search size={13} style={{ position: "absolute", left: 10, top: "50%",
              transform: "translateY(-50%)", color: "var(--text-muted)" }}/>
            <input value={search} onChange={e => { setSearch(e.target.value); setPage(1); }}
              placeholder="Search tours..."
              style={{ width: "100%", padding: "8px 10px 8px 30px",
                background: "var(--bg-card)", border: "1px solid var(--border)",
                borderRadius: 8, color: "var(--text-primary)", fontSize: 13, outline: "none" }}/>
          </div>
          <select value={country} onChange={e => { setCountry(e.target.value); setPage(1); }}
            style={{ padding: "8px 12px", background: "var(--bg-card)",
              border: "1px solid var(--border)", borderRadius: 8,
              color: "var(--text-primary)", fontSize: 13 }}>
            <option value="">All Countries</option>
            {countries.map(c => <option key={c} value={c}>{c}</option>)}
          </select>
        </div>

        {/* Batch action bar */}
        {checkedIds.size > 0 && (
          <div style={{ marginBottom: 12, padding: "10px 14px",
            background: "rgba(219,150,40,0.08)", border: "1px solid rgba(219,150,40,0.3)",
            borderRadius: 8, display: "flex", alignItems: "center", gap: 12 }}>
            <span style={{ fontSize: 13, color: "var(--brand-gold)", fontWeight: 600 }}>
              {checkedIds.size} tour{checkedIds.size > 1 ? "s" : ""} selected
            </span>
            <button onClick={() => triggerRewrite(Array.from(checkedIds))}
              disabled={rewriting}
              style={{ padding: "6px 14px", borderRadius: 6, border: "none",
                background: "var(--brand-gold)", color: "white",
                fontSize: 12, fontWeight: 700, cursor: "pointer" }}>
              {rewriting ? "Rewriting..." : `Rewrite ${checkedIds.size} tours`}
            </button>
            <button onClick={() => setCheckedIds(new Set())}
              style={{ padding: "6px 10px", borderRadius: 6,
                border: "1px solid var(--border)", background: "none",
                color: "var(--text-muted)", fontSize: 12, cursor: "pointer" }}>
              Clear
            </button>
          </div>
        )}

        {loading ? (
          <div style={{ textAlign: "center", padding: 40, color: "var(--text-muted)" }}>
            <Loader2 size={20} style={{ margin: "0 auto 8px", display: "block" }}/>Loading pool...
          </div>
        ) : (
          <>
            <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 10 }}>
              {total} tours available
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {tours.map(t => {
                const isChecked = checkedIds.has(t.id);
                const isSelected = selected?.id === t.id;
                const keywords = parseSeoKeywords(t.seo_keywords_used);
                return (
                  <div key={t.id}
                    style={{
                      background: isSelected ? "rgba(219,150,40,0.06)" : "var(--bg-card)",
                      border: `1px solid ${isChecked ? "var(--brand-gold)"
                        : isSelected ? "rgba(219,150,40,0.3)" : "var(--border)"}`,
                      borderRadius: 10, padding: "12px 14px", cursor: "pointer",
                      transition: "all 0.15s",
                    }}
                    onClick={() => { setSelected(isSelected ? null : t); setPanelTab("details"); }}>
                    <div style={{ display: "flex", alignItems: "flex-start", gap: 10 }}>
                      {/* Checkbox */}
                      <input type="checkbox" checked={isChecked}
                        onChange={() => {}}
                        onClick={e => toggleCheck(t.id, e as any)}
                        style={{ marginTop: 3, flexShrink: 0, cursor: "pointer",
                          accentColor: "var(--brand-gold)" }}/>
                      {/* Content */}
                      <div style={{ flex: 1 }}>
                        <div style={{ fontSize: 13, fontWeight: 600,
                          color: "var(--text-primary)", marginBottom: 3 }}>{t.aa_name}</div>
                        <div style={{ fontSize: 12, color: "var(--text-secondary)",
                          lineHeight: 1.5, marginBottom: 6 }}>
                          {t.aa_summary?.slice(0, 120)}{t.aa_summary?.length > 120 ? "..." : ""}
                        </div>
                        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" as const,
                          alignItems: "center" }}>
                          {t.country && (
                            <span style={{ fontSize: 11, color: "var(--text-muted)",
                              display: "flex", alignItems: "center", gap: 3 }}>
                              <Globe size={10}/>{t.country}
                            </span>
                          )}
                          {t.duration && (
                            <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
                              {t.duration}
                            </span>
                          )}
                          {keywords.slice(0,3).map((k: string) => (
                            <span key={k} style={{ fontSize: 10, padding: "1px 6px",
                              background: "rgba(219,150,40,0.08)",
                              color: "var(--brand-gold)", borderRadius: 20 }}>{k}</span>
                          ))}
                          {t.already_rewritten && (
                            <span style={{ fontSize: 10, padding: "1px 6px",
                              background: "rgba(34,197,94,0.1)", color: "#22c55e",
                              borderRadius: 20, fontWeight: 600 }}>✓ Rewritten</span>
                          )}
                        </div>
                      </div>
                      <ChevronRight size={14} style={{ color: "var(--text-muted)",
                        flexShrink: 0, marginTop: 2 }}/>
                    </div>
                  </div>
                );
              })}
            </div>
            <div style={{ display: "flex", gap: 8, marginTop: 14,
              justifyContent: "center" }}>
              <button onClick={() => setPage(p => Math.max(1, p-1))} disabled={page === 1}
                style={{ padding: "6px 14px", borderRadius: 6,
                  border: "1px solid var(--border)", background: "var(--bg-card)",
                  color: "var(--text-secondary)",
                  cursor: page === 1 ? "not-allowed" : "pointer", fontSize: 12 }}>Prev</button>
              <span style={{ padding: "6px 12px", fontSize: 12,
                color: "var(--text-muted)" }}>Page {page}</span>
              <button onClick={() => setPage(p => p+1)}
                disabled={page * PAGE_SIZE >= total}
                style={{ padding: "6px 14px", borderRadius: 6,
                  border: "1px solid var(--border)", background: "var(--bg-card)",
                  color: "var(--text-secondary)",
                  cursor: page * PAGE_SIZE >= total ? "not-allowed" : "pointer",
                  fontSize: 12 }}>Next</button>
            </div>
          </>
        )}
      </div>

      {/* RIGHT — tour detail + rewrite config */}
      {selected && (
        <div style={{ background: "var(--bg-card)", border: "1px solid var(--border)",
          borderRadius: 12, overflow: "hidden",
          position: "sticky" as const, top: 20 }}>
          {/* Tour header + tabs */}
          <div style={{ borderBottom: "1px solid var(--border)" }}>
            <div style={{ padding: "14px 18px 10px" }}>
              <div style={{ fontSize: 14, fontWeight: 700,
                color: "var(--text-primary)", marginBottom: 3 }}>{selected.aa_name}</div>
              <div style={{ fontSize: 12, color: "var(--text-muted)",
                display: "flex", gap: 10 }}>
                {selected.country && <span>📍 {selected.country}</span>}
                {selected.duration && <span>⏱ {selected.duration}</span>}
                {selected.price_raw && <span>💰 {selected.price_raw}</span>}
              </div>
            </div>
            <div style={{ display: "flex", padding: "0 18px", gap: 0 }}>
              {(["details","rewrite"] as const).map(t => (
                <button key={t} onClick={() => setPanelTab(t)}
                  style={{ padding: "8px 16px", fontSize: 12, fontWeight: 600,
                    border: "none", background: "none", cursor: "pointer",
                    color: panelTab===t ? "var(--brand-gold)" : "var(--text-muted)",
                    borderBottom: `2px solid ${panelTab===t ? "var(--brand-gold)" : "transparent"}`,
                    transition: "all 0.15s" }}>
                  {t === "details" ? "📄 Tour Details" : "✏️ Rewrite Config"}
                </button>
              ))}
            </div>
          </div>

          <div style={{ padding: 18, maxHeight: 580,
            overflowY: "auto" as const }}>

          {panelTab === "details" && (
            <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
              {/* Summary */}
              <div>
                <div style={{ fontSize: 10, fontWeight: 700, color: "var(--text-muted)",
                  textTransform: "uppercase", letterSpacing: 1, marginBottom: 6 }}>Summary</div>
                <div style={{ fontSize: 12, color: "var(--text-secondary)",
                  lineHeight: 1.7, padding: "10px 12px",
                  background: "var(--bg-primary)", borderRadius: 8 }}>
                  {selected.aa_summary || "—"}
                </div>
              </div>
              {/* SEO */}
              {(() => {
                const kws = parseSeoKeywords(selected.seo_keywords_used);
                return (
                  <div>
                    <div style={{ fontSize: 10, fontWeight: 700,
                      color: "var(--text-muted)", textTransform: "uppercase",
                      letterSpacing: 1, marginBottom: 8 }}>SEO</div>
                    <div style={{ padding: "10px 12px",
                      background: "var(--bg-primary)", borderRadius: 8 }}>
                      <div style={{ fontSize: 12, fontWeight: 600,
                        color: "var(--text-primary)", marginBottom: 4 }}>
                        {selected.seo_title || "—"}
                      </div>
                      <div style={{ fontSize: 11, color: "var(--text-secondary)",
                        lineHeight: 1.5, marginBottom: 8 }}>
                        {selected.seo_meta || "—"}
                      </div>
                      {kws.length > 0 && (
                        <div style={{ display: "flex", flexWrap: "wrap" as const, gap: 4 }}>
                          {kws.map((k: string) => (
                            <span key={k} style={{ fontSize: 10, padding: "2px 8px",
                              background: "rgba(219,150,40,0.1)",
                              color: "var(--brand-gold)", borderRadius: 20 }}>{k}</span>
                          ))}
                        </div>
                      )}
                    </div>
                  </div>
                );
              })()}
              {/* Published info */}
              <div style={{ fontSize: 11, color: "var(--text-muted)",
                padding: "8px 0", borderTop: "1px solid var(--border)" }}>
                Published: {selected.published_at
                  ? new Date(selected.published_at).toLocaleString("en-GB", {
                    day: "2-digit", month: "short", year: "numeric",
                    hour: "2-digit", minute: "2-digit"
                  }) : "—"}
                {selected.aa_quality && (
                  <span style={{ marginLeft: 12, color: "#22c55e", fontWeight: 600 }}>
                    ★ AA Quality: {Number(selected.aa_quality_score || selected.quality_score || 0).toFixed(1)}
                  </span>
                )}
                {selected.already_rewritten && (
                  <div style={{ marginTop: 6, fontSize: 11,
                    color: "#22c55e", fontWeight: 600 }}>
                    ✓ Already in your catalog — rewrite will create a new version
                  </div>
                )}
              </div>
            </div>
          )}

          {panelTab === "rewrite" && (
            <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            {/* Brand Identity */}
            <div style={{ padding: "12px 14px",
              background: useBrandRules ? "rgba(219,150,40,0.06)" : "var(--bg-primary)",
              border: `1px solid ${useBrandRules ? "rgba(219,150,40,0.3)" : "var(--border)"}`,
              borderRadius: 8 }}>
              <div style={{ display: "flex", justifyContent: "space-between",
                alignItems: "center", marginBottom: useBrandRules && brandRules?.configured ? 8 : 0 }}>
                <div style={{ fontSize: 12, fontWeight: 600,
                  color: useBrandRules ? "var(--brand-gold)" : "var(--text-muted)" }}>
                  Apply My Brand Rules
                </div>
                <label style={{ display: "flex", alignItems: "center",
                  gap: 6, cursor: "pointer" }}>
                  <input type="checkbox" checked={useBrandRules}
                    onChange={e => setUseBrand(e.target.checked)}
                    style={{ accentColor: "var(--brand-gold)" }}/>
                  <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
                    {useBrandRules ? "On" : "Off"}
                  </span>
                </label>
              </div>
              {useBrandRules && brandRules?.configured && (
                <div style={{ fontSize: 11, color: "var(--text-secondary)",
                  lineHeight: 1.5 }}>
                  <div>{brandRules.system_prompt?.slice(0, 80)}
                    {brandRules.system_prompt?.length > 80 ? "..." : ""}</div>
                  {(Array.isArray(brandRules.forbidden_words) ? brandRules.forbidden_words : []).length > 0 && (
                    <div style={{ marginTop: 4, color: "var(--text-muted)" }}>
                      Forbidden: {(Array.isArray(brandRules.forbidden_words) ? brandRules.forbidden_words : []).slice(0,5).join(", ")}
                    </div>
                  )}
                </div>
              )}
              {useBrandRules && !brandRules?.configured && (
                <div style={{ fontSize: 11, color: "#f59e0b", marginTop: 4 }}>
                  No brand rules saved yet —{" "}
                  <span style={{ textDecoration: "underline", cursor: "pointer" }}>
                    set up in Brand Identity tab
                  </span>
                </div>
              )}
            </div>

            {/* Language */}
            <div>
              <div style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)",
                textTransform: "uppercase", letterSpacing: 1, marginBottom: 8 }}>Language</div>
              <div style={{ display: "flex", gap: 8 }}>
                {["en-US","en-GB"].map(l => (
                  <button key={l} onClick={() => setLanguage(l)}
                    style={{ flex: 1, padding: "8px 10px", borderRadius: 6,
                      cursor: "pointer",
                      border: `1px solid ${language===l ? "var(--brand-gold)" : "var(--border)"}`,
                      background: language===l ? "rgba(219,150,40,0.08)" : "var(--bg-primary)",
                      color: language===l ? "var(--brand-gold)" : "var(--text-muted)",
                      fontSize: 12, fontWeight: language===l ? 700 : 400 }}>{l}</button>
                ))}
              </div>
            </div>

            {/* SEO Mode */}
            <div>
              <div style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)",
                textTransform: "uppercase", letterSpacing: 1, marginBottom: 8 }}>SEO Mode</div>
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                {[
                  {v:"standard",  l:"Standard",   d:"Balanced keyword density"},
                  {v:"aggressive",l:"Aggressive",  d:"Maximum keyword integration + PAA"},
                  {v:"minimal",   l:"Minimal",     d:"Brand voice first, light SEO"},
                ].map(s => (
                  <button key={s.v} onClick={() => setSeoMode(s.v)}
                    style={{ padding: "9px 12px", borderRadius: 8, cursor: "pointer",
                      border: `1px solid ${seoMode===s.v ? "var(--brand-gold)" : "var(--border)"}`,
                      background: seoMode===s.v ? "rgba(219,150,40,0.08)" : "var(--bg-primary)",
                      color: seoMode===s.v ? "var(--brand-gold)" : "var(--text-muted)",
                      fontSize: 12, textAlign: "left" as const,
                      fontWeight: seoMode===s.v ? 700 : 400 }}>
                    <div style={{ fontWeight: 600 }}>{s.l}</div>
                    <div style={{ fontSize: 11, opacity: 0.75, marginTop: 1 }}>{s.d}</div>
                  </button>
                ))}
              </div>
            </div>

            {/* Rewrite button */}
            <button onClick={() => triggerRewrite(rewriteTargets)}
              disabled={rewriting || rewriteTargets.length === 0}
              style={{ width: "100%", padding: "12px 16px", borderRadius: 8,
                border: "none",
                background: rewriting ? "var(--border)" : "var(--brand-gold)",
                color: rewriting ? "var(--text-muted)" : "white",
                fontSize: 14, fontWeight: 700,
                cursor: rewriting ? "not-allowed" : "pointer",
                display: "flex", alignItems: "center",
                justifyContent: "center", gap: 8 }}>
              {rewriting
                ? <><Loader2 size={15}/>Starting rewrite...</>
                : checkedIds.size > 0
                ? <><RotateCcw size={15}/>Rewrite {checkedIds.size} selected tours</>
                : <><RotateCcw size={15}/>Rewrite this tour</>}
            </button>
            <div style={{ fontSize: 11, color: "var(--text-muted)",
              textAlign: "center" as const }}>
              Rewrite uses your brand rules + selected SEO mode.
              Results appear in <strong>My Catalog</strong> (~30 seconds).
            </div>
            </div>
          )}
          </div>
        </div>
      )}
    </div>
  );
}

// ── My Catalog Tab ────────────────────────────────────────────────────────────

function CatalogTab() {
  const [versions, setVersions]       = useState<any[]>([]);
  const [loading, setLoading]         = useState(true);
  const [filter, setFilter]           = useState("");
  const [selected, setSelected]       = useState<any | null>(null);
  const [detail, setDetail]           = useState<any | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [acting, setActing]           = useState(false);
  const [saving, setSaving]           = useState(false);
  const [saveOk, setSaveOk]           = useState(false);

  // Editable fields
  const [editName, setEditName]       = useState("");
  const [editSubtitle, setEditSubtitle] = useState("");
  const [editSummary, setEditSummary] = useState("");
  const [editHighlights, setEditHighlights] = useState<string[]>([]);
  const [editSeoTitle, setEditSeoTitle] = useState("");
  const [editSeoMeta, setEditSeoMeta] = useState("");
  const [isDirty, setIsDirty]         = useState(false);

  const STATUS_COLOR: Record<string,string> = {
    approved: "#22c55e", rejected: "#ef4444",
    pending: "#f59e0b", needs_review: "#a78bfa",
  };

  const fetchVersions = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams({ page_size: "50",
        ...(filter && { status: filter }) });
      const res = await fetch(`/api/tenant/v1/tours/my-versions?${params}`);
      if (res.ok) { const d = await res.json(); setVersions(d.data ?? []); }
    } catch {} finally { setLoading(false); }
  }, [filter]);

  useEffect(() => { fetchVersions(); }, [fetchVersions]);

  const loadDetail = async (v: any) => {
    setSelected(v); setDetailLoading(true); setDetail(null);
    setIsDirty(false); setSaveOk(false);
    try {
      const res = await fetch(`/api/tenant/v1/tours/versions/${v.id}`);
      if (res.ok) {
        const d = await res.json();
        setDetail(d);
        // Populate edit fields
        const rc = parseContent(d.rewritten_content);
        setEditName(rc?.name || d.aa_name || "");
        setEditSubtitle(rc?.subtitle || "");
        setEditSummary(rc?.summary || "");
        setEditHighlights(Array.isArray(rc?.highlights) ? rc.highlights : []);
        setEditSeoTitle(rc?.seo_title || d.aa_seo_title || "");
        setEditSeoMeta(rc?.seo_meta || d.aa_seo_meta || "");
      }
    } catch {} finally { setDetailLoading(false); }
  };

  const parseContent = (raw: any) => {
    if (!raw) return null;
    try { return typeof raw === "string" ? JSON.parse(raw) : raw; } catch { return null; }
  };

  const doAction = async (action: string) => {
    if (!selected) return;
    setActing(true);
    try {
      const res = await fetch(`/api/tenant/v1/tours/versions/${selected.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action }),
      });
      if (res.ok) { await fetchVersions(); await loadDetail(selected); }
    } catch {} finally { setActing(false); }
  };

  const saveEdit = async () => {
    if (!selected || !isDirty) return;
    setSaving(true); setSaveOk(false);
    try {
      const edited = {
        name: editName, subtitle: editSubtitle, summary: editSummary,
        highlights: editHighlights, seo_title: editSeoTitle, seo_meta: editSeoMeta,
      };
      const res = await fetch(`/api/tenant/v1/tours/versions/${selected.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "edit", edited_content: edited, edited_by: "tenant" }),
      });
      if (res.ok) {
        const d = await res.json();
        setSaveOk(true); setIsDirty(false);
        await fetchVersions();
        // Load new version
        if (d.new_version_id) {
          const nv = versions.find(v => v.id === d.new_version_id) ||
            { ...selected, id: d.new_version_id, version_number: d.version_number, status: "pending" };
          await loadDetail(nv);
        }
      }
    } catch {} finally { setSaving(false); }
  };

  // SEO analysis helpers
  const seoTitleLen = editSeoTitle.length;
  const seoMetaLen  = editSeoMeta.length;
  const seoTitleOk  = seoTitleLen > 0 && seoTitleLen <= 60;
  const seoMetaOk   = seoMetaLen >= 80 && seoMetaLen <= 160;

  const field = (label: string, value: string, onChange: (v:string)=>void,
    rows: number, hint?: string, okColor?: string) => (
    <div style={{ marginBottom: 14 }}>
      <div style={{ display: "flex", justifyContent: "space-between",
        alignItems: "center", marginBottom: 5 }}>
        <label style={{ fontSize: 10, fontWeight: 700, color: "var(--text-muted)",
          textTransform: "uppercase", letterSpacing: 1 }}>{label}</label>
        {hint && <span style={{ fontSize: 10, color: okColor || "var(--text-muted)" }}>{hint}</span>}
      </div>
      <textarea value={value}
        onChange={e => { onChange(e.target.value); setIsDirty(true); }}
        rows={rows}
        style={{ width: "100%", padding: "8px 10px",
          background: "var(--bg-primary)", border: "1px solid var(--border)",
          borderRadius: 6, color: "var(--text-primary)", fontSize: 12,
          resize: "vertical" as const, outline: "none", lineHeight: 1.6,
          fontFamily: "inherit" }}/>
    </div>
  );

  return (
    <div style={{ display: "grid",
      gridTemplateColumns: selected ? "320px 1fr" : "1fr",
      gap: 20, alignItems: "start" }}>

      {/* LEFT — version list */}
      <div>
        <div style={{ display: "flex", gap: 6, marginBottom: 14, flexWrap: "wrap" as const }}>
          {["","pending","approved","rejected"].map(s => (
            <button key={s} onClick={() => setFilter(s)}
              style={{ padding: "5px 12px", borderRadius: 20, fontSize: 11,
                border: `1px solid ${filter===s ? "var(--brand-gold)" : "var(--border)"}`,
                background: filter===s ? "rgba(219,150,40,0.1)" : "var(--bg-card)",
                color: filter===s ? "var(--brand-gold)" : "var(--text-muted)",
                cursor: "pointer", fontWeight: filter===s ? 700 : 400 }}>
              {s || "All"}
            </button>
          ))}
        </div>
        {loading ? (
          <div style={{ textAlign: "center", padding: 40, color: "var(--text-muted)" }}>
            <Loader2 size={18} style={{ margin: "0 auto 8px", display: "block" }}/>
          </div>
        ) : versions.length === 0 ? (
          <div style={{ textAlign: "center", padding: 32, color: "var(--text-muted)",
            background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: 12 }}>
            <Package size={28} style={{ margin: "0 auto 10px", opacity: 0.3 }}/>
            <div style={{ fontWeight: 600, fontSize: 13 }}>No rewrites yet</div>
            <div style={{ fontSize: 12, marginTop: 4 }}>Browse the pool to get started</div>
          </div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {versions.map(v => {
              const rc = parseContent(v.rewritten_content);
              const isActive = selected?.id === v.id;
              return (
                <div key={v.id} onClick={() => loadDetail(v)}
                  style={{
                    background: isActive ? "rgba(219,150,40,0.06)" : "var(--bg-card)",
                    border: `1px solid ${isActive ? "rgba(219,150,40,0.4)" : "var(--border)"}`,
                    borderRadius: 10, padding: "11px 14px", cursor: "pointer",
                    transition: "all 0.15s",
                  }}>
                  <div style={{ display: "flex", justifyContent: "space-between",
                    alignItems: "flex-start", gap: 8 }}>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ fontSize: 13, fontWeight: 600,
                        color: "var(--text-primary)", marginBottom: 2,
                        overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                        {v.aa_name || rc?.name || "Tour"}
                      </div>
                      <div style={{ fontSize: 11, color: "var(--text-muted)",
                        display: "flex", gap: 8 }}>
                        <span>v{v.version_number}</span>
                        <span>{v.rewrite_language}</span>
                        {v.country && <span>{v.country}</span>}
                      </div>
                      {rc?.summary && (
                        <div style={{ fontSize: 11, color: "var(--text-muted)",
                          marginTop: 4, lineHeight: 1.4,
                          overflow: "hidden", display: "-webkit-box",
                          WebkitLineClamp: 2, WebkitBoxOrient: "vertical" as const }}>
                          {rc.summary}
                        </div>
                      )}
                    </div>
                    <span style={{ fontSize: 10, padding: "2px 7px", borderRadius: 20,
                      fontWeight: 700, flexShrink: 0,
                      background: `${STATUS_COLOR[v.status] ?? "#888"}22`,
                      color: STATUS_COLOR[v.status] ?? "#888" }}>
                      {v.status}
                    </span>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* RIGHT — Editorial Workspace */}
      {selected && (
        <div style={{ background: "var(--bg-card)", border: "1px solid var(--border)",
          borderRadius: 12, overflow: "hidden" }}>
          {/* Header */}
          <div style={{ padding: "14px 20px", borderBottom: "1px solid var(--border)",
            display: "flex", justifyContent: "space-between", alignItems: "center",
            background: "var(--bg-primary)" }}>
            <div>
              <div style={{ fontSize: 14, fontWeight: 700,
                color: "var(--text-primary)" }}>{selected.aa_name || "Tour"}</div>
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2,
                display: "flex", gap: 10 }}>
                <span>v{selected.version_number}</span>
                <span>{selected.rewrite_language}</span>
                <span>{selected.seo_mode}</span>
                {selected.quality_score && (
                  <span style={{ color: "#22c55e", fontWeight: 600 }}>
                    ★ {Number(selected.quality_score).toFixed(1)}
                  </span>
                )}
              </div>
            </div>
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              {isDirty && (
                <button onClick={saveEdit} disabled={saving}
                  style={{ padding: "6px 14px", borderRadius: 6, border: "none",
                    background: "var(--brand-gold)", color: "white",
                    fontSize: 12, fontWeight: 700,
                    cursor: saving ? "not-allowed" : "pointer" }}>
                  {saving ? "Saving..." : "Save Edit"}
                </button>
              )}
              {saveOk && !isDirty && (
                <span style={{ fontSize: 12, color: "#22c55e", fontWeight: 600 }}>
                  ✓ Saved as new version
                </span>
              )}
              <span style={{ fontSize: 11, padding: "3px 10px", borderRadius: 20,
                fontWeight: 700,
                background: `${STATUS_COLOR[selected.status] ?? "#888"}22`,
                color: STATUS_COLOR[selected.status] ?? "#888" }}>
                {selected.status}
              </span>
              <button onClick={() => { setSelected(null); setDetail(null); }}
                style={{ background: "none", border: "none", cursor: "pointer",
                  color: "var(--text-muted)", fontSize: 20, lineHeight: 1 }}>×</button>
            </div>
          </div>

          {detailLoading ? (
            <div style={{ padding: 40, textAlign: "center", color: "var(--text-muted)" }}>
              <Loader2 size={20} style={{ margin: "0 auto 8px", display: "block" }}/>
              Loading editorial workspace...
            </div>
          ) : detail ? (
            <div style={{ maxHeight: "75vh", overflowY: "auto" as const }}>

              {/* Full Before / After comparison */}
              <div style={{ padding: "16px 20px", borderBottom: "1px solid var(--border)" }}>
                <div style={{ fontSize: 10, fontWeight: 700, color: "var(--text-muted)",
                  textTransform: "uppercase", letterSpacing: 1, marginBottom: 12 }}>
                  Content Comparison — Before / After
                </div>

                {/* Timestamps */}
                <div style={{ display: "flex", gap: 16, marginBottom: 12,
                  fontSize: 11, color: "var(--text-muted)" }}>
                  <span>Created: <strong style={{ color: "var(--text-secondary)" }}>
                    {new Date(detail.created_at).toLocaleString("en-GB", {
                      day: "2-digit", month: "short", year: "numeric",
                      hour: "2-digit", minute: "2-digit"
                    })}
                  </strong></span>
                  {detail.edited_at && (
                    <span>Last edited: <strong style={{ color: "var(--text-secondary)" }}>
                      {new Date(detail.edited_at).toLocaleString("en-GB", {
                        day: "2-digit", month: "short", year: "numeric",
                        hour: "2-digit", minute: "2-digit"
                      })}
                    </strong></span>
                  )}
                  <span>Quality: <strong style={{ color: "#22c55e" }}>
                    {detail.quality_score ? Number(detail.quality_score).toFixed(1) : "—"}
                  </strong></span>
                  <span>Edit: <strong style={{ color: "var(--text-secondary)" }}>
                    {detail.edit_source === "ai_generated" ? "AI Generated"
                      : detail.edit_source === "tenant_edit" ? "Your Edit" : detail.edit_source}
                  </strong></span>
                </div>

                {[
                  { label: "Summary",
                    original: detail.aa_summary,
                    yours: editSummary },
                  { label: "SEO Title",
                    original: detail.aa_seo_title,
                    yours: editSeoTitle },
                  { label: "SEO Meta",
                    original: detail.aa_seo_meta,
                    yours: editSeoMeta },
                ].map(row => (
                  <div key={row.label} style={{ marginBottom: 12 }}>
                    <div style={{ fontSize: 10, fontWeight: 600,
                      color: "var(--text-muted)", textTransform: "uppercase",
                      letterSpacing: 1, marginBottom: 6 }}>{row.label}</div>
                    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
                      <div style={{ background: "rgba(239,68,68,0.05)",
                        border: "1px solid rgba(239,68,68,0.12)",
                        borderRadius: 6, padding: "8px 10px" }}>
                        <div style={{ fontSize: 9, fontWeight: 700, color: "#f87171",
                          marginBottom: 4 }}>AA ORIGINAL</div>
                        <div style={{ fontSize: 11, color: "var(--text-secondary)",
                          lineHeight: 1.6 }}>{row.original || "—"}</div>
                      </div>
                      <div style={{ background: "rgba(34,197,94,0.05)",
                        border: "1px solid rgba(34,197,94,0.12)",
                        borderRadius: 6, padding: "8px 10px" }}>
                        <div style={{ fontSize: 9, fontWeight: 700, color: "#22c55e",
                          marginBottom: 4 }}>YOUR VERSION</div>
                        <div style={{ fontSize: 11, color: "var(--text-secondary)",
                          lineHeight: 1.6 }}>{row.yours || "Generating..."}</div>
                      </div>
                    </div>
                  </div>
                ))}

                {/* Highlights comparison */}
                <div style={{ marginBottom: 12 }}>
                  <div style={{ fontSize: 10, fontWeight: 600,
                    color: "var(--text-muted)", textTransform: "uppercase",
                    letterSpacing: 1, marginBottom: 6 }}>Highlights</div>
                  <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
                    <div style={{ background: "rgba(239,68,68,0.05)",
                      border: "1px solid rgba(239,68,68,0.12)",
                      borderRadius: 6, padding: "8px 10px" }}>
                      <div style={{ fontSize: 9, fontWeight: 700, color: "#f87171",
                        marginBottom: 4 }}>AA ORIGINAL</div>
                      {(() => {
                        try {
                          const h = typeof detail.aa_highlights === "string"
                            ? JSON.parse(detail.aa_highlights) : detail.aa_highlights;
                          return Array.isArray(h) ? h.map((item: string, i: number) => (
                            <div key={i} style={{ fontSize: 11,
                              color: "var(--text-secondary)", marginBottom: 3 }}>
                              • {item}
                            </div>
                          )) : <div style={{ fontSize: 11,
                            color: "var(--text-muted)" }}>{String(h || "—")}</div>;
                        } catch { return <div style={{ fontSize: 11,
                          color: "var(--text-muted)" }}>—</div>; }
                      })()}
                    </div>
                    <div style={{ background: "rgba(34,197,94,0.05)",
                      border: "1px solid rgba(34,197,94,0.12)",
                      borderRadius: 6, padding: "8px 10px" }}>
                      <div style={{ fontSize: 9, fontWeight: 700, color: "#22c55e",
                        marginBottom: 4 }}>YOUR VERSION</div>
                      {editHighlights.length > 0
                        ? editHighlights.map((h, i) => (
                          <div key={i} style={{ fontSize: 11,
                            color: "var(--text-secondary)", marginBottom: 3 }}>• {h}</div>
                        ))
                        : <div style={{ fontSize: 11,
                          color: "var(--text-muted)" }}>Generating...</div>}
                    </div>
                  </div>
                </div>

                {/* Itineraries comparison */}
                {(detail.aa_itineraries || (() => {
                  const rc = parseContent(detail.rewritten_content);
                  return rc?.itineraries;
                })()) && (
                  <div>
                    <div style={{ fontSize: 10, fontWeight: 600,
                      color: "var(--text-muted)", textTransform: "uppercase",
                      letterSpacing: 1, marginBottom: 6 }}>Itineraries</div>
                    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
                      <div style={{ background: "rgba(239,68,68,0.05)",
                        border: "1px solid rgba(239,68,68,0.12)",
                        borderRadius: 6, padding: "8px 10px", maxHeight: 200,
                        overflowY: "auto" as const }}>
                        <div style={{ fontSize: 9, fontWeight: 700, color: "#f87171",
                          marginBottom: 4 }}>AA ORIGINAL</div>
                        <div style={{ fontSize: 11, color: "var(--text-secondary)",
                          lineHeight: 1.6, whiteSpace: "pre-wrap" as const }}>
                          {detail.aa_itineraries || "—"}
                        </div>
                      </div>
                      <div style={{ background: "rgba(34,197,94,0.05)",
                        border: "1px solid rgba(34,197,94,0.12)",
                        borderRadius: 6, padding: "8px 10px", maxHeight: 200,
                        overflowY: "auto" as const }}>
                        <div style={{ fontSize: 9, fontWeight: 700, color: "#22c55e",
                          marginBottom: 4 }}>YOUR VERSION</div>
                        <div style={{ fontSize: 11, color: "var(--text-secondary)",
                          lineHeight: 1.6, whiteSpace: "pre-wrap" as const }}>
                          {(() => {
                            const rc = parseContent(detail.rewritten_content);
                            return rc?.itineraries || "Will be generated on next rewrite";
                          })()}
                        </div>
                      </div>
                    </div>
                  </div>
                )}
              </div>

              {/* Editable content */}
              <div style={{ padding: "16px 20px",
                borderBottom: "1px solid var(--border)" }}>
                <div style={{ fontSize: 10, fontWeight: 700, color: "var(--text-muted)",
                  textTransform: "uppercase", letterSpacing: 1, marginBottom: 14,
                  display: "flex", justifyContent: "space-between" }}>
                  <span>Edit Content</span>
                  {isDirty && <span style={{ color: "var(--brand-gold)" }}>
                    ● Unsaved changes
                  </span>}
                </div>

                {field("Tour Name", editName, setEditName, 1)}
                {field("Subtitle", editSubtitle, setEditSubtitle, 2)}
                {field("Summary", editSummary, setEditSummary, 4)}

                {/* Highlights */}
                <div style={{ marginBottom: 14 }}>
                  <div style={{ display: "flex", justifyContent: "space-between",
                    alignItems: "center", marginBottom: 5 }}>
                    <label style={{ fontSize: 10, fontWeight: 700,
                      color: "var(--text-muted)", textTransform: "uppercase",
                      letterSpacing: 1 }}>Highlights</label>
                    <button onClick={() => {
                      setEditHighlights(prev => [...prev, ""]);
                      setIsDirty(true);
                    }} style={{ fontSize: 11, color: "var(--brand-gold)",
                      background: "none", border: "none", cursor: "pointer" }}>
                      + Add
                    </button>
                  </div>
                  {editHighlights.map((h, i) => (
                    <div key={i} style={{ display: "flex", gap: 6, marginBottom: 6 }}>
                      <span style={{ color: "var(--brand-gold)", fontWeight: 700,
                        paddingTop: 7, fontSize: 12 }}>•</span>
                      <input value={h}
                        onChange={e => {
                          const n = [...editHighlights];
                          n[i] = e.target.value;
                          setEditHighlights(n);
                          setIsDirty(true);
                        }}
                        style={{ flex: 1, padding: "6px 10px",
                          background: "var(--bg-primary)",
                          border: "1px solid var(--border)", borderRadius: 6,
                          color: "var(--text-primary)", fontSize: 12, outline: "none" }}/>
                      <button onClick={() => {
                        setEditHighlights(prev => prev.filter((_,j) => j !== i));
                        setIsDirty(true);
                      }} style={{ background: "none", border: "none",
                        cursor: "pointer", color: "var(--text-muted)",
                        padding: "0 4px" }}>×</button>
                    </div>
                  ))}
                </div>
              </div>

              {/* SEO Panel */}
              <div style={{ padding: "16px 20px",
                borderBottom: "1px solid var(--border)" }}>
                <div style={{ fontSize: 10, fontWeight: 700,
                  color: "var(--text-muted)", textTransform: "uppercase",
                  letterSpacing: 1, marginBottom: 14 }}>SEO</div>

                {field("SEO Title",
                  editSeoTitle, setEditSeoTitle, 1,
                  `${seoTitleLen}/60 ${seoTitleOk ? "✓" : "⚠"}`,
                  seoTitleOk ? "#22c55e" : "#f59e0b")}

                {field("SEO Meta Description",
                  editSeoMeta, setEditSeoMeta, 3,
                  `${seoMetaLen}/160 ${seoMetaOk ? "✓" : "⚠"}`,
                  seoMetaOk ? "#22c55e" : "#f59e0b")}

                {/* SEO Health */}
                <div style={{ display: "flex", gap: 8, flexWrap: "wrap" as const }}>
                  {[
                    { label: "Title ≤60", ok: seoTitleOk },
                    { label: "Meta 80-160", ok: seoMetaOk },
                    { label: "Highlights ≥3", ok: editHighlights.length >= 3 },
                    { label: "Summary filled", ok: editSummary.length > 50 },
                  ].map(c => (
                    <span key={c.label} style={{ fontSize: 10, padding: "2px 8px",
                      borderRadius: 20,
                      background: c.ok ? "rgba(34,197,94,0.1)" : "rgba(239,68,68,0.1)",
                      color: c.ok ? "#22c55e" : "#ef4444",
                      fontWeight: 600 }}>
                      {c.ok ? "✓" : "✗"} {c.label}
                    </span>
                  ))}
                </div>
              </div>

              {/* Version History */}
              {detail.version_history?.length > 0 && (
                <div style={{ padding: "14px 20px",
                  borderBottom: "1px solid var(--border)" }}>
                  <div style={{ fontSize: 10, fontWeight: 700,
                    color: "var(--text-muted)", textTransform: "uppercase",
                    letterSpacing: 1, marginBottom: 10 }}>Version History</div>
                  {detail.version_history.map((h: any) => (
                    <div key={h.id} style={{ display: "flex", alignItems: "center",
                      gap: 10, padding: "6px 0",
                      borderBottom: "1px solid var(--border)", fontSize: 12 }}>
                      <span style={{ color: "var(--brand-gold)", fontWeight: 700,
                        minWidth: 24 }}>v{h.version_number}</span>
                      <span style={{ color: "var(--text-muted)", flex: 1 }}>
                        {h.edit_source === "ai_generated" ? "AI Generated"
                          : h.edit_source === "tenant_edit" ? "Your Edit"
                          : h.edit_source}
                      </span>
                      <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
                        {new Date(h.created_at).toLocaleDateString("en-GB",
                          { day: "2-digit", month: "short" })}
                      </span>
                      <span style={{ fontSize: 10, padding: "2px 7px",
                        borderRadius: 20, fontWeight: 700,
                        background: `${STATUS_COLOR[h.status] ?? "#888"}22`,
                        color: STATUS_COLOR[h.status] ?? "#888" }}>{h.status}</span>
                    </div>
                  ))}
                </div>
              )}

              {/* Action buttons */}
              <div style={{ padding: "14px 20px",
                display: "flex", gap: 10 }}>
                {selected.status !== "approved" && (
                  <button onClick={() => doAction("approve")} disabled={acting}
                    style={{ flex: 1, padding: "10px 0", borderRadius: 8,
                      border: "none", background: "rgba(34,197,94,0.15)",
                      color: "#22c55e", fontSize: 13, fontWeight: 700,
                      cursor: acting ? "not-allowed" : "pointer",
                      display: "flex", alignItems: "center",
                      justifyContent: "center", gap: 6 }}>
                    <CheckCircle size={14}/> Approve
                  </button>
                )}
                {selected.status !== "rejected" && (
                  <button onClick={() => doAction("reject")} disabled={acting}
                    style={{ flex: 1, padding: "10px 0", borderRadius: 8,
                      border: "none", background: "rgba(239,68,68,0.08)",
                      color: "#ef4444", fontSize: 13, fontWeight: 700,
                      cursor: acting ? "not-allowed" : "pointer",
                      display: "flex", alignItems: "center",
                      justifyContent: "center", gap: 6 }}>
                    <XCircle size={14}/> Reject
                  </button>
                )}
                {isDirty && (
                  <button onClick={saveEdit} disabled={saving}
                    style={{ flex: 1, padding: "10px 0", borderRadius: 8,
                      border: "none", background: "var(--brand-gold)",
                      color: "white", fontSize: 13, fontWeight: 700,
                      cursor: saving ? "not-allowed" : "pointer" }}>
                    {saving ? "Saving..." : "Save as New Version"}
                  </button>
                )}
              </div>
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}

// ── Brand Identity Tab (preserved from existing) ───────────────────────────────

function BrandTab() {
  const [data, setData]           = useState<any>(null);
  const [loading, setLoading]     = useState(true);
  const [saving, setSaving]       = useState(false);
  const [saved, setSaved]         = useState(false);
  const [isDirty, setIsDirty]     = useState(false);
  const [systemPrompt, setSP]     = useState("");
  const [styleGuide, setSG]       = useState("");
  const [forbidden, setForbidden] = useState("");
  const [history, setHistory]     = useState<any[]>([]);
  const [showHistory, setShowHistory] = useState(false);
  const [viewingVersion, setViewingVersion] = useState<any | null>(null);

  const loadCurrent = () => {
    setLoading(true);
    fetch("/api/tenant/v1/pipeline/brand-identity")
      .then(r => r.ok ? r.json() : null)
      .then(d => {
        if (d?.configured) {
          setSP(d.system_prompt || "");
          setSG(d.style_guide || "");
          const fw = Array.isArray(d.forbidden_words)
            ? d.forbidden_words
            : (typeof d.forbidden_words === "string"
              ? JSON.parse(d.forbidden_words || "[]") : []);
          setForbidden(fw.join(", "));
        }
        setData(d);
        setIsDirty(false);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  };

  const loadHistory = async () => {
    // Fetch all versions via metrics endpoint — fallback: simulate from version number
    if (data?.version) {
      const versions = [];
      for (let v = data.version; v >= 1; v--) {
        versions.push({
          version: v,
          is_active: v === data.version,
          updated_at: data.updated_at,
        });
      }
      setHistory(versions);
    }
    setShowHistory(true);
  };

  useEffect(() => { loadCurrent(); }, []);

  const save = async () => {
    setSaving(true); setSaved(false);
    try {
      const res = await fetch("/api/tenant/v1/pipeline/brand-identity", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          system_prompt: systemPrompt,
          style_guide: styleGuide,
          forbidden_words: forbidden.split(",").map(w => w.trim()).filter(Boolean),
        }),
      });
      if (res.ok) {
        setSaved(true);
        setIsDirty(false);
        loadCurrent();
      }
    } catch {} finally { setSaving(false); }
  };

  const fld = (label: string, value: string,
    onChange: (v:string)=>void, rows: number, placeholder: string) => (
    <div style={{ marginBottom: 18 }}>
      <label style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)",
        textTransform: "uppercase", letterSpacing: 1, display: "block",
        marginBottom: 6 }}>{label}</label>
      <textarea value={value}
        onChange={e => { onChange(e.target.value); setIsDirty(true); setSaved(false); }}
        rows={rows} placeholder={placeholder}
        style={{ width: "100%", padding: "10px 12px",
          background: "var(--bg-primary)", border: `1px solid ${isDirty ? "rgba(219,150,40,0.4)" : "var(--border)"}`,
          borderRadius: 8, color: "var(--text-primary)", fontSize: 13,
          resize: "vertical" as const, outline: "none", lineHeight: 1.6 }}/>
    </div>
  );

  if (loading) return (
    <div style={{ textAlign: "center", padding: 40, color: "var(--text-muted)" }}>
      <Loader2 size={20} style={{ margin: "0 auto 8px", display: "block" }}/>
    </div>
  );

  return (
    <div style={{ display: "grid", gridTemplateColumns: showHistory ? "1fr 280px" : "1fr",
      gap: 24, alignItems: "start" }}>

      {/* Main form */}
      <div>
        {/* Header */}
        <div style={{ display: "flex", justifyContent: "space-between",
          alignItems: "flex-start", marginBottom: 20 }}>
          <div>
            <h2 style={{ fontSize: 18, fontWeight: 700,
              color: "var(--text-primary)", marginBottom: 4 }}>Brand Identity</h2>
            <p style={{ fontSize: 13, color: "var(--text-muted)", lineHeight: 1.6, margin: 0 }}>
              These rules are appended to AA core standards on every rewrite.
              They do not override quality thresholds.
            </p>
          </div>
          <div style={{ display: "flex", gap: 8, flexShrink: 0 }}>
            {data?.version && (
              <button onClick={loadHistory}
                style={{ padding: "7px 14px", borderRadius: 8, fontSize: 12,
                  border: "1px solid var(--border)", background: "var(--bg-card)",
                  color: "var(--text-secondary)", cursor: "pointer", fontWeight: 500 }}>
                History (v{data.version})
              </button>
            )}
          </div>
        </div>

        {/* Active version badge */}
        {data?.configured && data?.version && (
          <div style={{ display: "flex", alignItems: "center", gap: 10,
            padding: "8px 14px", marginBottom: 20,
            background: "rgba(34,197,94,0.08)",
            border: "1px solid rgba(34,197,94,0.2)", borderRadius: 8 }}>
            <span style={{ width: 8, height: 8, borderRadius: "50%",
              background: "#22c55e", flexShrink: 0 }}/>
            <span style={{ fontSize: 12, color: "#22c55e", fontWeight: 600 }}>
              Active — Version {data.version}
            </span>
            {data.updated_at && (
              <span style={{ fontSize: 11, color: "var(--text-muted)", marginLeft: "auto" }}>
                Last updated {new Date(data.updated_at).toLocaleDateString("en-GB",
                  { day: "2-digit", month: "short", year: "numeric" })}
              </span>
            )}
          </div>
        )}

        {/* Viewing old version banner */}
        {viewingVersion && (
          <div style={{ padding: "10px 14px", marginBottom: 16,
            background: "rgba(245,158,11,0.08)",
            border: "1px solid rgba(245,158,11,0.3)", borderRadius: 8,
            display: "flex", alignItems: "center", gap: 10 }}>
            <span style={{ fontSize: 12, color: "#f59e0b" }}>
              Viewing v{viewingVersion.version} — read only
            </span>
            <button onClick={() => { setViewingVersion(null); loadCurrent(); }}
              style={{ marginLeft: "auto", fontSize: 11, color: "var(--brand-gold)",
                background: "none", border: "none", cursor: "pointer", fontWeight: 600 }}>
              ← Back to active
            </button>
          </div>
        )}

        {fld("Brand Context / System Prompt", systemPrompt, setSP, 5,
          "e.g. We are a luxury private-travel operator for US/UK professionals aged 40-60. Emphasise depth of experience, exclusivity, and cultural immersion.")}

        {fld("Style Guide", styleGuide, setSG, 4,
          "e.g. Use active voice. Prefer concrete specifics over adjectives. Keep sentences under 25 words. Never open with 'Journey' or 'Discover'.")}

        {fld("Forbidden Words (comma-separated)", forbidden, setForbidden, 2,
          "e.g. cheap, budget, bargain, amazing, incredible, stunning")}

        {/* Preview */}
        {(systemPrompt || styleGuide || forbidden) && (
          <div style={{ marginBottom: 20, padding: "12px 14px",
            background: "var(--bg-primary)", border: "1px solid var(--border)",
            borderRadius: 8 }}>
            <div style={{ fontSize: 10, fontWeight: 700, color: "var(--text-muted)",
              textTransform: "uppercase", letterSpacing: 1, marginBottom: 8 }}>
              How it applies to rewrites
            </div>
            <div style={{ fontSize: 11, color: "var(--text-secondary)", lineHeight: 1.7 }}>
              {systemPrompt && (
                <div style={{ marginBottom: 6 }}>
                  <strong style={{ color: "var(--text-primary)" }}>Brand context:</strong>{" "}
                  {systemPrompt.slice(0, 120)}{systemPrompt.length > 120 ? "..." : ""}
                </div>
              )}
              {styleGuide && (
                <div style={{ marginBottom: 6 }}>
                  <strong style={{ color: "var(--text-primary)" }}>Style:</strong>{" "}
                  {styleGuide.slice(0, 80)}{styleGuide.length > 80 ? "..." : ""}
                </div>
              )}
              {forbidden && (
                <div>
                  <strong style={{ color: "var(--text-primary)" }}>Will avoid:</strong>{" "}
                  {forbidden.split(",").filter(Boolean).map(w => w.trim()).slice(0,6).map(w => (
                    <span key={w} style={{ display: "inline-block", margin: "1px 3px",
                      padding: "1px 6px", borderRadius: 20, fontSize: 10,
                      background: "rgba(239,68,68,0.1)", color: "#f87171" }}>{w}</span>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}

        <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
          <button onClick={save} disabled={saving || !isDirty || !!viewingVersion}
            style={{ padding: "10px 28px",
              background: (!isDirty || viewingVersion) ? "var(--border)" : saving ? "var(--border)" : "var(--brand-gold)",
              border: "none", borderRadius: 8,
              color: (!isDirty || viewingVersion) ? "var(--text-muted)" : "white",
              fontSize: 13, fontWeight: 700,
              cursor: (!isDirty || saving || !!viewingVersion) ? "not-allowed" : "pointer" }}>
            {saving ? "Saving..." : "Save as New Version"}
          </button>
          {saved && (
            <span style={{ fontSize: 12, color: "#22c55e", fontWeight: 600 }}>
              ✓ Saved — active on next pipeline run
            </span>
          )}
          {isDirty && !saved && (
            <span style={{ fontSize: 12, color: "#f59e0b" }}>
              ● Unsaved changes
            </span>
          )}
        </div>
      </div>

      {/* Version history sidebar */}
      {showHistory && (
        <div style={{ background: "var(--bg-card)", border: "1px solid var(--border)",
          borderRadius: 12, overflow: "hidden",
          position: "sticky" as const, top: 20 }}>
          <div style={{ padding: "12px 16px", borderBottom: "1px solid var(--border)",
            display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <div style={{ fontSize: 12, fontWeight: 600,
              color: "var(--text-secondary)" }}>Version History</div>
            <button onClick={() => setShowHistory(false)}
              style={{ background: "none", border: "none",
                cursor: "pointer", color: "var(--text-muted)", fontSize: 16 }}>×</button>
          </div>
          <div style={{ padding: 12 }}>
            {history.map(h => (
              <div key={h.version} style={{ padding: "10px 12px", borderRadius: 8,
                marginBottom: 6, cursor: "pointer",
                background: h.is_active ? "rgba(34,197,94,0.08)" : "var(--bg-primary)",
                border: `1px solid ${h.is_active ? "rgba(34,197,94,0.2)" : "var(--border)"}`,
              }}>
                <div style={{ display: "flex", justifyContent: "space-between",
                  alignItems: "center" }}>
                  <span style={{ fontSize: 13, fontWeight: 700,
                    color: h.is_active ? "#22c55e" : "var(--text-primary)" }}>
                    Version {h.version}
                    {h.is_active && (
                      <span style={{ marginLeft: 6, fontSize: 10,
                        color: "#22c55e" }}>ACTIVE</span>
                    )}
                  </span>
                </div>
                {h.updated_at && (
                  <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 3 }}>
                    {new Date(h.updated_at).toLocaleDateString("en-GB",
                      { day: "2-digit", month: "short" })}
                  </div>
                )}
                {!h.is_active && (
                  <div style={{ fontSize: 11, color: "var(--text-muted)",
                    marginTop: 4 }}>
                    Previous version — content not retrievable
                  </div>
                )}
              </div>
            ))}
            <div style={{ fontSize: 11, color: "var(--text-muted)",
              marginTop: 10, lineHeight: 1.5, padding: "0 4px" }}>
              Only the active version is applied to rewrites.
              Previous version content is not stored.
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ── API Key Tab ────────────────────────────────────────────────────────────────

function ApiKeyTab() {
  const [show, setShow]   = useState(false);
  const [copied, setCopied] = useState(false);
  const tenantId = getCookie("cis_tenant_id");
  // API key is a one-time secret — cannot be retrieved after creation
  const key = "Your API key was shown once at creation. Contact admin@adventureasia.com to regenerate.";

  const copy = () => {
    navigator.clipboard.writeText(key);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div style={{ maxWidth: 600 }}>
      <h2 style={{ fontSize: 18, fontWeight: 700, color: "var(--text-primary)", marginBottom: 6 }}>
        API Access
      </h2>
      <p style={{ fontSize: 13, color: "var(--text-muted)", marginBottom: 24, lineHeight: 1.6 }}>
        Use your API key to access your rewritten tour catalog programmatically.
      </p>
      <div style={{ background: "var(--bg-card)", border: "1px solid var(--border)",
        borderRadius: 12, padding: 20, marginBottom: 20 }}>
        <div style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)",
          textTransform: "uppercase", letterSpacing: 1, marginBottom: 8 }}>API Key</div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <code style={{ flex: 1, padding: "10px 14px",
            background: "var(--bg-primary)", borderRadius: 8,
            fontSize: 13, color: "var(--text-primary)", letterSpacing: 1,
            fontFamily: "monospace" }}>
            {show ? (key || "Contact admin for your API key") : "•".repeat(40)}
          </code>
          <button onClick={() => setShow(s => !s)}
            style={{ padding: "9px 12px", background: "var(--bg-primary)",
              border: "1px solid var(--border)", borderRadius: 8,
              cursor: "pointer", color: "var(--text-muted)" }}>
            {show ? <EyeOff size={14}/> : <Eye size={14}/>}
          </button>
          <button onClick={copy}
            style={{ padding: "9px 12px", background: "var(--bg-primary)",
              border: "1px solid var(--border)", borderRadius: 8,
              cursor: "pointer", color: copied ? "#22c55e" : "var(--text-muted)" }}>
            {copied ? <CheckCircle size={14}/> : <Copy size={14}/>}
          </button>
        </div>
      </div>
      <div style={{ background: "var(--bg-card)", border: "1px solid var(--border)",
        borderRadius: 12, padding: 20 }}>
        <div style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)",
          textTransform: "uppercase", letterSpacing: 1, marginBottom: 12 }}>
          Quick Reference
        </div>
        {[
          { label: "List my tours", method: "GET",
            path: "/v1/tours/my-versions" },
          { label: "Browse pool",   method: "GET",
            path: "/v1/tours/pool" },
          { label: "Trigger rewrite", method: "POST",
            path: "/v1/tours/pool/{id}/rewrite" },
          { label: "Approve version", method: "PATCH",
            path: "/v1/tours/versions/{id}" },
        ].map(e => (
          <div key={e.path} style={{ display: "flex", gap: 10, alignItems: "center",
            marginBottom: 8, fontSize: 12 }}>
            <span style={{ padding: "2px 8px", borderRadius: 4,
              background: e.method === "GET" ? "rgba(34,197,94,0.1)"
                : e.method === "POST" ? "rgba(219,150,40,0.1)"
                : "rgba(139,92,246,0.1)",
              color: e.method === "GET" ? "#22c55e"
                : e.method === "POST" ? "var(--brand-gold)" : "#a78bfa",
              fontWeight: 700, fontSize: 10 }}>{e.method}</span>
            <code style={{ color: "var(--text-secondary)", fontFamily: "monospace" }}>
              {e.path}
            </code>
            <span style={{ color: "var(--text-muted)", marginLeft: "auto" }}>{e.label}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Main Portal Page ──────────────────────────────────────────────────────────

export default function PortalPage() {
  const [tab, setTab]           = useState<Tab>("dashboard");
  const [tenantName, setName]   = useState("Partner");
  const [planTier, setPlan]     = useState("growth");
  const [notification, setNote] = useState<string | null>(null);

  useEffect(() => {
    const n = getCookie("cis_tenant_name");
    const p = getCookie("cis_tenant_plan");
    if (n) setName(n);
    if (p) setPlan(p);
  }, []);

  const showNote = (msg: string) => {
    setNote(msg);
    setTimeout(() => setNote(null), 4000);
  };

  const handleRewrite = (d: any) => {
    showNote(`Rewrite started — version ${d.version_number}. Check My Catalog.`);
    setTab("catalog");
  };

  const TABS: { id: Tab; icon: React.ReactNode; label: string }[] = [
    { id: "dashboard", icon: <LayoutDashboard size={14}/>, label: "Dashboard" },
    { id: "pool",      icon: <BookOpen size={14}/>,        label: "Browse Pool" },
    { id: "catalog",   icon: <Package size={14}/>,         label: "My Catalog" },
    { id: "brand",     icon: <Tag size={14}/>,             label: "Brand Identity" },
    { id: "apikey",    icon: <Key size={14}/>,             label: "API Access" },
  ];

  return (
    <div>
      {/* Notification */}
      {notification && (
        <div style={{ position: "fixed", top: 70, right: 24, zIndex: 999,
          padding: "12px 20px", background: "#22c55e", borderRadius: 10,
          color: "white", fontSize: 13, fontWeight: 600,
          boxShadow: "0 4px 20px rgba(0,0,0,0.2)" }}>
          <CheckCircle size={14} style={{ display: "inline", marginRight: 8 }}/>
          {notification}
        </div>
      )}

      {/* Header */}
      <div style={{ marginBottom: 24 }}>
        <h1 style={{ fontSize: 22, fontWeight: 700, color: "var(--text-primary)", margin: 0 }}>
          Partner Portal
        </h1>
        <p style={{ fontSize: 13, color: "var(--text-muted)", marginTop: 4 }}>
          Welcome, {tenantName}
        </p>
      </div>

      {/* Tabs */}
      <div style={{ display: "flex", gap: 8, marginBottom: 24, flexWrap: "wrap" as const }}>
        {TABS.map(t => (
          <TabBtn key={t.id} id={t.id} active={tab===t.id}
            icon={t.icon} label={t.label} onClick={() => setTab(t.id)}/>
        ))}
      </div>

      {/* Content */}
      {tab === "dashboard" && <DashboardTab planTier={planTier} onTabChange={setTab}/>}
      {tab === "pool"      && <PoolTab onRewrite={handleRewrite}/>}
      {tab === "catalog"   && <CatalogTab/>}
      {tab === "brand"     && <BrandTab/>}
      {tab === "apikey"    && <ApiKeyTab/>}
    </div>
  );
}
