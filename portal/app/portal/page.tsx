"use client";
import { useState, useEffect, useCallback } from "react";
import {
  LayoutDashboard, Upload, FileSearch, Key,
  CheckCircle, XCircle, RotateCcw, Copy, Eye, EyeOff,
  Search, TrendingUp, Package, AlertCircle, CloudUpload,
  ChevronRight, Loader2, BarChart3, Zap, Globe,
} from "lucide-react";

// ── Config ────────────────────────────────────────────────────────────────────

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "https://api-cis.lumiguides.it.com";

function getCookie(name: string): string {
  if (typeof document === "undefined") return "";
  const m = document.cookie.match(new RegExp(`(^| )${name}=([^;]+)`));
  return m ? decodeURIComponent(m[2]) : "";
}

// ── Types ─────────────────────────────────────────────────────────────────────

type Tab = "dashboard" | "upload" | "review" | "apikey";

interface Tour {
  tour_id: string;
  src_name: string;
  pipeline_status: string;
  ingest_at?: string;
}

// ── Sub-components ────────────────────────────────────────────────────────────

function StatCard({
  icon, label, value, sub, accent = false,
}: {
  icon: React.ReactNode; label: string; value: string | number; sub?: string; accent?: boolean;
}) {
  return (
    <div style={{
      background: accent ? "linear-gradient(135deg, rgba(219,150,40,0.12), rgba(219,150,40,0.04))" : "var(--bg-card)",
      border: `1px solid ${accent ? "rgba(219,150,40,0.3)" : "var(--border)"}`,
      borderRadius: 12, padding: "20px 24px",
      display: "flex", alignItems: "flex-start", gap: 16,
    }}>
      <div style={{
        width: 40, height: 40, borderRadius: 10,
        background: accent ? "rgba(219,150,40,0.2)" : "rgba(255,255,255,0.05)",
        display: "flex", alignItems: "center", justifyContent: "center",
        color: accent ? "var(--brand-gold)" : "var(--text-muted)", flexShrink: 0,
      }}>{icon}</div>
      <div>
        <div style={{ fontSize: 11, color: "var(--text-muted)", fontWeight: 600, textTransform: "uppercase", letterSpacing: 1, marginBottom: 4 }}>{label}</div>
        <div style={{ fontSize: 26, fontWeight: 800, color: accent ? "var(--brand-gold)" : "var(--text-primary)", lineHeight: 1 }}>{value}</div>
        {sub && <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>{sub}</div>}
      </div>
    </div>
  );
}

function TabBtn({ id, active, icon, label, onClick }: {
  id: Tab; active: boolean; icon: React.ReactNode; label: string; onClick: () => void;
}) {
  return (
    <button onClick={onClick} style={{
      display: "flex", alignItems: "center", gap: 8,
      padding: "10px 18px", borderRadius: 8, border: "none",
      background: active ? "var(--brand-gold)" : "var(--bg-card)",
      color: active ? "white" : "var(--text-secondary)",
      fontSize: 13, fontWeight: active ? 700 : 500, cursor: "pointer",
      transition: "all 0.15s",
      boxShadow: active ? "0 2px 8px rgba(219,150,40,0.3)" : "none",
    }}>
      {icon}
      <span>{label}</span>
    </button>
  );
}

// ── Dashboard Tab ─────────────────────────────────────────────────────────────

function DashboardTab({ tenantName, planTier, token }: { tenantName: string; planTier: string; token: string }) {
  const [dashData, setDashData] = useState<{
    used: number; avgScore: number; passRate: number; pendingReview: number;
    scores: { range: string; count: number; color: string }[];
  } | null>(null);
  const [dashLoading, setDashLoading] = useState(true);
  useEffect(() => {
    const fetchDash = async () => {
      try {
        const headers: HeadersInit = token ? { "Authorization": `Bearer ${token}` } : {};
        const [toursRes, queueRes] = await Promise.all([
          fetch(`${API_URL}/v1/tours?page=1&page_size=100`, { headers }),
          fetch(`${API_URL}/v1/pipeline/review-queue?page_size=1`, { headers }),
        ]);
        const toursData = toursRes.ok ? await toursRes.json() : null;
        const queueData = queueRes.ok ? await queueRes.json() : null;
        const tours = toursData?.data ?? [];
        const total = toursData?.pagination?.total ?? tours.length;
        const scores = tours.map((t: { quality_score?: number }) => t.quality_score ?? 0).filter((s: number) => s > 0);
        const avgScore = scores.length ? +(scores.reduce((a: number, b: number) => a + b, 0) / scores.length).toFixed(1) : 0;
        const passRate = scores.length ? Math.round((scores.filter((s: number) => s >= 7.0).length / scores.length) * 100) : 0;
        const pendingReview = queueData?.pagination?.total ?? 0;
        const bands = [
          { range: "9.0–10.0", min: 9.0, max: 10.1, color: "#22c55e" },
          { range: "8.0–8.9",  min: 8.0, max: 9.0,  color: "#86efac" },
          { range: "7.0–7.9",  min: 7.0, max: 8.0,  color: "#fbbf24" },
          { range: "<7.0",     min: 0,   max: 7.0,  color: "#f87171" },
        ];
        const scoreDist = bands.map(b => ({
          range: b.range,
          color: b.color,
          count: scores.filter((s: number) => s >= b.min && s < b.max).length,
        }));
        setDashData({ used: total, avgScore, passRate, pendingReview, scores: scoreDist });
      } catch (e) {
        console.error("Dashboard fetch error:", e);
      } finally { setDashLoading(false); }
    };
    fetchDash();
  }, [token]);
  const quotaMap: Record<string, number> = { starter: 1000, growth: 5000, business: 20000, enterprise: 999999, internal: 999999 };
  const quota = quotaMap[planTier] ?? 1000;
  const used = dashData?.used ?? 0;
  const pct = Math.min(100, Math.round((used / quota) * 100));

  const scores = dashData?.scores ?? [];
  const maxCount = scores.length ? Math.max(...scores.map(s => s.count)) : 1;

  if (dashLoading) return <div style={{ padding: 40, textAlign: "center", color: "var(--text-muted)" }}>Loading dashboard…</div>;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 24 }}>
      {/* Stats row */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 16 }}>
        <StatCard icon={<Package size={20}/>}    label="Tours Processed"  value={used}       sub="this month"        accent />
        <StatCard icon={<TrendingUp size={20}/>}  label="Avg Quality Score" value={dashData?.avgScore ?? "—"} sub="across all tours" />
        <StatCard icon={<CheckCircle size={20}/>} label="Pass Rate"         value={dashData ? `${dashData.passRate}%` : "—"} sub="score ≥ 7.0"     />
        <StatCard icon={<Zap size={20}/>}         label="Pending Review"    value={dashData?.pendingReview ?? "—"} sub="requires action"   />
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
        {/* Quota */}
        <div style={{ background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: 12, padding: 24 }}>
          <div style={{ fontSize: 12, fontWeight: 700, color: "var(--text-secondary)", textTransform: "uppercase", letterSpacing: 1, marginBottom: 20 }}>Monthly Quota</div>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 10 }}>
            <span style={{ fontSize: 32, fontWeight: 800, color: "var(--text-primary)" }}>{used.toLocaleString()}</span>
            <span style={{ fontSize: 14, color: "var(--text-muted)" }}>/ {quota.toLocaleString()} tours</span>
          </div>
          <div style={{ height: 8, background: "rgba(255,255,255,0.06)", borderRadius: 4, overflow: "hidden", marginBottom: 8 }}>
            <div style={{
              height: "100%", width: `${pct}%`,
              background: pct > 80 ? "linear-gradient(90deg,#f59e0b,#ef4444)" : "linear-gradient(90deg,var(--brand-gold),#f59e0b)",
              borderRadius: 4, transition: "width 0.6s ease",
            }}/>
          </div>
          <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, color: "var(--text-muted)" }}>
            <span>{pct}% used</span>
            <span style={{ color: "var(--brand-gold)", fontWeight: 600 }}>{(quota - used).toLocaleString()} remaining</span>
          </div>
          <div style={{ marginTop: 16, padding: "10px 14px", background: "rgba(219,150,40,0.06)", borderRadius: 8, fontSize: 12, color: "var(--text-muted)" }}>
            Plan: <span style={{ color: "var(--brand-gold)", fontWeight: 700, textTransform: "capitalize" }}>{planTier}</span>
            <span style={{ marginLeft: 8, color: "var(--text-muted)" }}>· Resets 1st of month</span>
          </div>
        </div>

        {/* Quality distribution */}
        <div style={{ background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: 12, padding: 24 }}>
          <div style={{ fontSize: 12, fontWeight: 700, color: "var(--text-secondary)", textTransform: "uppercase", letterSpacing: 1, marginBottom: 20 }}>Quality Score Distribution</div>
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            {scores.map(s => (
              <div key={s.range} style={{ display: "flex", alignItems: "center", gap: 10 }}>
                <div style={{ width: 70, fontSize: 12, color: "var(--text-muted)", flexShrink: 0 }}>{s.range}</div>
                <div style={{ flex: 1, height: 20, background: "rgba(255,255,255,0.04)", borderRadius: 4, overflow: "hidden" }}>
                  <div style={{
                    height: "100%", width: `${(s.count / maxCount) * 100}%`,
                    background: s.color, borderRadius: 4, opacity: 0.8,
                    transition: "width 0.8s ease",
                  }}/>
                </div>
                <div style={{ width: 28, fontSize: 12, fontWeight: 700, color: s.color, textAlign: "right", flexShrink: 0 }}>{s.count}</div>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Quick actions */}
      <div style={{ background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: 12, padding: 20 }}>
        <div style={{ fontSize: 12, fontWeight: 700, color: "var(--text-secondary)", textTransform: "uppercase", letterSpacing: 1, marginBottom: 16 }}>Quick Actions</div>
        <div style={{ display: "flex", gap: 12 }}>
          {[
            { label: "Upload New Batch",    icon: <Upload size={14}/>,      tab: "upload" as Tab },
            { label: "Review Pending (3)",  icon: <FileSearch size={14}/>,  tab: "review" as Tab },
            { label: "View API Keys",       icon: <Key size={14}/>,         tab: "apikey" as Tab },
          ].map(a => (
            <button key={a.label} style={{
              display: "flex", alignItems: "center", gap: 8,
              padding: "10px 20px", background: "rgba(219,150,40,0.08)",
              border: "1px solid rgba(219,150,40,0.2)", borderRadius: 8,
              color: "var(--brand-gold)", fontSize: 13, fontWeight: 600, cursor: "pointer",
            }}>
              {a.icon}{a.label}<ChevronRight size={12}/>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

// ── Batch Upload Tab ──────────────────────────────────────────────────────────

function BatchUploadTab() {
  const [dragging, setDragging]   = useState(false);
  const [file, setFile]           = useState<File | null>(null);
  const [batchName, setBatchName] = useState("");
  const [market, setMarket]       = useState("vietnam");
  const [seoMode, setSeoMode]     = useState("dataforseo");
  const [submitted, setSubmitted] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [batchId, setBatchId]     = useState("");

  const markets = ["vietnam", "cambodia", "thailand", "indonesia", "japan", "sri-lanka"];
  const seoModes = [
    { id: "dataforseo", label: "DataForSEO (Default)", desc: "Auto keyword research" },
    { id: "custom",     label: "Custom Keywords",      desc: "Use your brand keywords" },
    { id: "disabled",   label: "Disabled",             desc: "Skip SEO enrichment" },
  ];

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault(); setDragging(false);
    const f = e.dataTransfer.files[0];
    if (f && f.name.endsWith(".xlsx")) setFile(f);
  };

  const handleSubmit = async () => {
    if (!file || !batchName) return;
    setSubmitting(true);
    await new Promise(r => setTimeout(r, 1500)); // S8: replace with real API call
    setBatchId(`batch-${Date.now().toString(36).toUpperCase()}`);
    setSubmitted(true);
    setSubmitting(false);
  };

  if (submitted) return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 20, padding: "48px 0" }}>
      <div style={{ width: 64, height: 64, borderRadius: "50%", background: "rgba(34,197,94,0.15)", border: "2px solid rgba(34,197,94,0.3)", display: "flex", alignItems: "center", justifyContent: "center" }}>
        <CheckCircle size={28} color="#22c55e"/>
      </div>
      <div style={{ textAlign: "center" }}>
        <div style={{ fontSize: 18, fontWeight: 700, color: "var(--text-primary)", marginBottom: 8 }}>Batch Submitted</div>
        <div style={{ fontSize: 13, color: "var(--text-muted)", marginBottom: 4 }}>Batch ID: <code style={{ color: "var(--brand-gold)" }}>{batchId}</code></div>
        <div style={{ fontSize: 13, color: "var(--text-muted)" }}>You'll be notified when processing completes.</div>
      </div>
      <button onClick={() => { setSubmitted(false); setFile(null); setBatchName(""); }}
        style={{ padding: "10px 24px", background: "var(--brand-gold)", border: "none", borderRadius: 8, color: "white", fontSize: 13, fontWeight: 700, cursor: "pointer" }}>
        Upload Another Batch
      </button>
    </div>
  );

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      {/* Drop zone */}
      <div
        onDragOver={e => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={handleDrop}
        onClick={() => document.getElementById("file-input")?.click()}
        style={{
          border: `2px dashed ${dragging ? "var(--brand-gold)" : file ? "rgba(34,197,94,0.5)" : "var(--border)"}`,
          borderRadius: 12, padding: "40px 24px", textAlign: "center", cursor: "pointer",
          background: dragging ? "rgba(219,150,40,0.04)" : file ? "rgba(34,197,94,0.04)" : "var(--bg-card)",
          transition: "all 0.2s",
        }}>
        <input id="file-input" type="file" accept=".xlsx" style={{ display: "none" }}
          onChange={e => { if (e.target.files?.[0]) setFile(e.target.files[0]); }} />
        {file ? (
          <>
            <CheckCircle size={32} color="#22c55e" style={{ margin: "0 auto 12px" }}/>
            <div style={{ fontSize: 14, fontWeight: 700, color: "#22c55e" }}>{file.name}</div>
            <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 4 }}>{(file.size / 1024).toFixed(1)} KB · Click to replace</div>
          </>
        ) : (
          <>
            <CloudUpload size={32} color="var(--text-muted)" style={{ margin: "0 auto 12px" }}/>
            <div style={{ fontSize: 14, fontWeight: 600, color: "var(--text-secondary)" }}>Drop your Excel file here</div>
            <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 4 }}>or click to browse · .xlsx only</div>
          </>
        )}
      </div>

      {/* Config */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
        <div>
          <label style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)", textTransform: "uppercase" as const, letterSpacing: 1, display: "block", marginBottom: 8 }}>Batch Name</label>
          <input value={batchName} onChange={e => setBatchName(e.target.value)} placeholder="e.g. Vietnam Q2 2026"
            style={{ width: "100%", padding: "10px 12px", background: "var(--bg-primary)", border: "1px solid var(--border)", borderRadius: 8, color: "var(--text-primary)", fontSize: 13, outline: "none" }}/>
        </div>
        <div>
          <label style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)", textTransform: "uppercase" as const, letterSpacing: 1, display: "block", marginBottom: 8 }}>Destination Market</label>
          <select value={market} onChange={e => setMarket(e.target.value)}
            style={{ width: "100%", padding: "10px 12px", background: "var(--bg-primary)", border: "1px solid var(--border)", borderRadius: 8, color: "var(--text-primary)", fontSize: 13, outline: "none" }}>
            {markets.map(m => <option key={m} value={m}>{m.charAt(0).toUpperCase() + m.slice(1)}</option>)}
          </select>
        </div>
      </div>

      {/* SEO mode */}
      <div>
        <label style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)", textTransform: "uppercase" as const, letterSpacing: 1, display: "block", marginBottom: 10 }}>SEO Mode</label>
        <div style={{ display: "flex", gap: 10 }}>
          {seoModes.map(s => (
            <button key={s.id} onClick={() => setSeoMode(s.id)} style={{
              flex: 1, padding: "12px 16px", borderRadius: 8, cursor: "pointer", textAlign: "left" as const,
              background: seoMode === s.id ? "rgba(219,150,40,0.1)" : "var(--bg-primary)",
              border: `1px solid ${seoMode === s.id ? "rgba(219,150,40,0.4)" : "var(--border)"}`,
            }}>
              <div style={{ fontSize: 13, fontWeight: 600, color: seoMode === s.id ? "var(--brand-gold)" : "var(--text-primary)", marginBottom: 2 }}>{s.label}</div>
              <div style={{ fontSize: 11, color: "var(--text-muted)" }}>{s.desc}</div>
            </button>
          ))}
        </div>
      </div>

      <button onClick={handleSubmit} disabled={!file || !batchName || submitting}
        style={{
          padding: "12px 32px", borderRadius: 8, border: "none", fontSize: 14, fontWeight: 700, cursor: (!file || !batchName) ? "not-allowed" : "pointer",
          background: (!file || !batchName) ? "var(--border)" : "var(--brand-gold)",
          color: (!file || !batchName) ? "var(--text-muted)" : "white",
          display: "flex", alignItems: "center", gap: 8, alignSelf: "flex-start" as const,
        }}>
        {submitting ? <><Loader2 size={14} style={{ animation: "spin 1s linear infinite" }}/>Submitting...</> : "Submit Batch →"}
      </button>
    </div>
  );
}

// ── Content Review Tab ────────────────────────────────────────────────────────

function ContentReviewTab() {
  const [tours, setTours]         = useState<Tour[]>([]);
  const [loading, setLoading]     = useState(true);
  const [search, setSearch]       = useState("");
  const [selected, setSelected]   = useState<Tour | null>(null);
  const [rewriting, setRewriting] = useState(false);
  const [rewritten, setRewritten] = useState(false);
  const [decision, setDecision]   = useState<"approved"|"rejected"|null>(null);

  const tenantToken = typeof window !== "undefined" ? getCookie("cis_tenant_token") : "";

  useEffect(() => {
    const fetchTours = async () => {
      try {
        const res = await fetch(`${API_URL}/tours?limit=20`, {
          headers: tenantToken ? { "Authorization": `Bearer ${tenantToken}` } : {},
        });
        if (res.ok) {
          const data = await res.json();
          setTours(data.data ?? []);
        }
      } catch (e) {
        console.error("Tours fetch error:", e);
        setTours([]);
      } finally { setLoading(false); }
    };
    fetchTours();
  }, [tenantToken]);

  const filtered = tours.filter(t =>
    t.src_name?.toLowerCase().includes(search.toLowerCase())
  );

  const runRewrite = async () => {
    setRewriting(true); setRewritten(false); setDecision(null);
    await new Promise(r => setTimeout(r, 2000)); // S8: POST /v1/tours/{id}/rewrite
    setRewriting(false); setRewritten(true);
  };

  const mockBefore = { name: selected?.src_name ?? "", summary: "A timeless journey through one of Asia's most iconic destinations, crafted by Adventure Asia's editorial team with precision and depth." };
  const mockAfter  = { name: `${selected?.src_name ?? ""} — Exclusive Experience`, summary: "An extraordinary private journey, curated exclusively for discerning travellers seeking transformative encounters with Asia's most coveted destinations." };

  return (
    <div style={{ display: "grid", gridTemplateColumns: selected ? "320px 1fr" : "1fr", gap: 20, alignItems: "start" }}>
      {/* Tour list */}
      <div style={{ background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: 12, overflow: "hidden" }}>
        <div style={{ padding: "14px 16px", borderBottom: "1px solid var(--border)" }}>
          <div style={{ position: "relative" }}>
            <Search size={12} style={{ position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)", color: "var(--text-muted)" }}/>
            <input value={search} onChange={e => setSearch(e.target.value)} placeholder="Search tours..."
              style={{ width: "100%", padding: "8px 10px 8px 30px", background: "var(--bg-primary)", border: "1px solid var(--border)", borderRadius: 6, color: "var(--text-primary)", fontSize: 12, outline: "none" }}/>
          </div>
        </div>
        <div style={{ maxHeight: 480, overflowY: "auto" as const }}>
          {loading ? (
            <div style={{ padding: 24, textAlign: "center", color: "var(--text-muted)", fontSize: 13 }}>
              <Loader2 size={16} style={{ animation: "spin 1s linear infinite", margin: "0 auto 8px", display: "block" }}/>Loading...
            </div>
          ) : filtered.length === 0 ? (
            <div style={{ padding: 24, textAlign: "center", color: "var(--text-muted)", fontSize: 13 }}>No tours found</div>
          ) : filtered.map(tour => {
            const isSelected = selected?.tour_id === tour.tour_id;
            return (
              <div key={tour.tour_id} onClick={() => { setSelected(isSelected ? null : tour); setRewritten(false); setDecision(null); }}
                style={{
                  padding: "12px 16px", cursor: "pointer", borderBottom: "1px solid var(--border)",
                  background: isSelected ? "rgba(219,150,40,0.08)" : "transparent",
                  borderLeft: `3px solid ${isSelected ? "var(--brand-gold)" : "transparent"}`,
                  transition: "all 0.1s",
                }}>
                <div style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)", marginBottom: 4 }}>{tour.src_name}</div>
                <div style={{ fontSize: 11, color: "var(--text-muted)", display: "flex", alignItems: "center", gap: 6 }}>
                  <span style={{ width: 6, height: 6, borderRadius: "50%", background: "#22c55e", display: "inline-block" }}/>
                  {tour.pipeline_status}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* Review panel */}
      {selected && (
        <div style={{ background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: 12, overflow: "hidden" }}>
          <div style={{ padding: "14px 20px", borderBottom: "1px solid var(--border)", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <div style={{ fontSize: 13, fontWeight: 700, color: "var(--text-primary)" }}>{selected.src_name}</div>
            <button onClick={runRewrite} disabled={rewriting}
              style={{ padding: "8px 18px", background: rewriting ? "var(--border)" : "var(--brand-gold)", border: "none", borderRadius: 6, color: rewriting ? "var(--text-muted)" : "white", fontSize: 12, fontWeight: 700, cursor: rewriting ? "not-allowed" : "pointer", display: "flex", alignItems: "center", gap: 6 }}>
              {rewriting ? <><Loader2 size={12} style={{ animation: "spin 1s linear infinite" }}/>Rewriting...</> : <><RotateCcw size={12}/>Generate Tenant Version</>}
            </button>
          </div>

          {!rewritten ? (
            <div style={{ padding: 32, textAlign: "center", color: "var(--text-muted)" }}>
              <BarChart3 size={32} style={{ margin: "0 auto 12px", opacity: 0.3 }}/>
              <div style={{ fontSize: 13 }}>Click "Generate Tenant Version" to create a branded rewrite</div>
            </div>
          ) : (
            <div style={{ padding: 20 }}>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 16 }}>
                {/* Before */}
                <div style={{ border: "1px solid rgba(239,68,68,0.2)", borderRadius: 10, overflow: "hidden" }}>
                  <div style={{ padding: "8px 14px", background: "rgba(239,68,68,0.08)", fontSize: 11, fontWeight: 700, color: "#f87171" }}>AA ORIGINAL</div>
                  <div style={{ padding: 14 }}>
                    <div style={{ fontSize: 11, color: "#9ca3af", marginBottom: 4 }}>TITLE</div>
                    <div style={{ fontSize: 13, color: "var(--text-primary)", marginBottom: 12 }}>{mockBefore.name}</div>
                    <div style={{ fontSize: 11, color: "#9ca3af", marginBottom: 4 }}>SUMMARY</div>
                    <div style={{ fontSize: 12, color: "var(--text-secondary)", lineHeight: 1.6 }}>{mockBefore.summary}</div>
                  </div>
                </div>
                {/* After */}
                <div style={{ border: "1px solid rgba(34,197,94,0.2)", borderRadius: 10, overflow: "hidden" }}>
                  <div style={{ padding: "8px 14px", background: "rgba(34,197,94,0.08)", fontSize: 11, fontWeight: 700, color: "#4ade80" }}>TENANT VERSION</div>
                  <div style={{ padding: 14 }}>
                    <div style={{ fontSize: 11, color: "#9ca3af", marginBottom: 4 }}>TITLE</div>
                    <div contentEditable suppressContentEditableWarning style={{ fontSize: 13, color: "var(--text-primary)", marginBottom: 12, outline: "none", padding: "2px 4px", borderRadius: 4, background: "rgba(255,255,255,0.03)" }}>{mockAfter.name}</div>
                    <div style={{ fontSize: 11, color: "#9ca3af", marginBottom: 4 }}>SUMMARY</div>
                    <div contentEditable suppressContentEditableWarning style={{ fontSize: 12, color: "var(--text-secondary)", lineHeight: 1.6, outline: "none", padding: "2px 4px", borderRadius: 4, background: "rgba(255,255,255,0.03)" }}>{mockAfter.summary}</div>
                  </div>
                </div>
              </div>

              {!decision ? (
                <div style={{ display: "flex", gap: 10, justifyContent: "flex-end" }}>
                  <button onClick={() => setDecision("rejected")} style={{ padding: "9px 20px", background: "rgba(239,68,68,0.1)", border: "1px solid rgba(239,68,68,0.3)", borderRadius: 8, color: "#ef4444", fontSize: 13, fontWeight: 600, cursor: "pointer", display: "flex", alignItems: "center", gap: 6 }}>
                    <XCircle size={14}/> Reject
                  </button>
                  <button onClick={runRewrite} style={{ padding: "9px 20px", background: "var(--bg-primary)", border: "1px solid var(--border)", borderRadius: 8, color: "var(--text-secondary)", fontSize: 13, cursor: "pointer", display: "flex", alignItems: "center", gap: 6 }}>
                    <RotateCcw size={14}/> Regenerate
                  </button>
                  <button onClick={() => setDecision("approved")} style={{ padding: "9px 24px", background: "rgba(34,197,94,0.15)", border: "1px solid rgba(34,197,94,0.3)", borderRadius: 8, color: "#22c55e", fontSize: 13, fontWeight: 700, cursor: "pointer", display: "flex", alignItems: "center", gap: 6 }}>
                    <CheckCircle size={14}/> Approve & Export
                  </button>
                </div>
              ) : (
                <div style={{ padding: 14, borderRadius: 8, background: decision === "approved" ? "rgba(34,197,94,0.08)" : "rgba(239,68,68,0.08)", border: `1px solid ${decision === "approved" ? "rgba(34,197,94,0.2)" : "rgba(239,68,68,0.2)"}`, fontSize: 13, fontWeight: 600, color: decision === "approved" ? "#22c55e" : "#ef4444" }}>
                  {decision === "approved" ? "✓ Approved — available via API" : "✗ Rejected — not published"}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── API Key Tab ───────────────────────────────────────────────────────────────

function ApiKeyTab({ tenantId }: { tenantId: string }) {
  const [showKey, setShowKey] = useState(false);
  const [copied, setCopied]   = useState(false);

  // Real key from cookie — set by tenant-login page
  const rawKey = getCookie("cis_tenant_key") || "wl_live_sk_test_wanderlux_2026";

  const copyKey = () => {
    navigator.clipboard.writeText(rawKey);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const endpoints = [
    ["GET",  "/v1/tours",              "List your approved tours (Gold layer)"],
    ["GET",  "/v1/tours/{id}",         "Get single tour with full content"],
    ["POST", "/v1/tours/{id}/rewrite", "Trigger tenant-branded rewrite"],
    ["POST", "/v1/exports",            "Create bulk export job (JSON/CSV/XML)"],
    ["GET",  "/v1/usage",              "Check quota and billing stats"],
    ["POST", "/v1/webhooks/test",      "Send test webhook payload"],
  ];

  const curlExample = `curl -X GET \\
  https://api-cis.lumiguides.it.com/v1/tours \\
  -H "X-API-Key: ${showKey ? rawKey : rawKey.slice(0, 12) + "..."}" \\
  -H "Accept: application/json"`;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      {/* Key display */}
      <div style={{ background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: 12, padding: 24 }}>
        <div style={{ fontSize: 12, fontWeight: 700, color: "var(--text-secondary)", textTransform: "uppercase", letterSpacing: 1, marginBottom: 16 }}>Your API Key</div>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
          <div style={{ flex: 1, fontFamily: "monospace", fontSize: 13, padding: "11px 16px", background: "var(--bg-primary)", border: "1px solid var(--border)", borderRadius: 8, color: showKey ? "var(--brand-gold)" : "var(--text-muted)", letterSpacing: showKey ? 0.5 : 2 }}>
            {showKey ? rawKey : "•".repeat(Math.min(rawKey.length, 32))}
          </div>
          <button onClick={() => setShowKey(!showKey)} style={{ padding: "11px 14px", background: "var(--bg-primary)", border: "1px solid var(--border)", borderRadius: 8, cursor: "pointer", color: "var(--text-secondary)", display: "flex", alignItems: "center" }}>
            {showKey ? <EyeOff size={15}/> : <Eye size={15}/>}
          </button>
          <button onClick={copyKey} style={{ padding: "11px 18px", background: copied ? "rgba(34,197,94,0.1)" : "var(--bg-primary)", border: `1px solid ${copied ? "rgba(34,197,94,0.3)" : "var(--border)"}`, borderRadius: 8, cursor: "pointer", color: copied ? "#22c55e" : "var(--text-secondary)", fontSize: 13, fontWeight: 600, display: "flex", alignItems: "center", gap: 6 }}>
            <Copy size={13}/>{copied ? "Copied!" : "Copy"}
          </button>
        </div>
        <div style={{ padding: "10px 14px", background: "rgba(219,150,40,0.06)", border: "1px solid rgba(219,150,40,0.15)", borderRadius: 8, fontSize: 12, color: "var(--text-muted)" }}>
          <AlertCircle size={12} style={{ display: "inline", marginRight: 6, color: "var(--brand-gold)", verticalAlign: "middle" }}/>
          Keep your API key secret. Pass it as <code style={{ color: "var(--brand-gold)" }}>X-API-Key</code> header on every request. Do not commit to version control.
        </div>
      </div>

      {/* Endpoints */}
      <div style={{ background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: 12, overflow: "hidden" }}>
        <div style={{ padding: "14px 20px", borderBottom: "1px solid var(--border)", fontSize: 12, fontWeight: 700, color: "var(--text-secondary)", textTransform: "uppercase", letterSpacing: 1 }}>Available Endpoints</div>
        <div style={{ padding: "12px 16px", display: "flex", flexDirection: "column", gap: 6 }}>
          {endpoints.map(([method, path, desc]) => (
            <div key={path} style={{ display: "flex", alignItems: "center", gap: 12, padding: "10px 12px", background: "var(--bg-primary)", border: "1px solid var(--border)", borderRadius: 8 }}>
              <span style={{ fontSize: 10, fontWeight: 800, padding: "3px 8px", borderRadius: 4, background: method === "GET" ? "rgba(59,130,246,0.15)" : "rgba(34,197,94,0.15)", color: method === "GET" ? "#60a5fa" : "#4ade80", flexShrink: 0 }}>{method}</span>
              <code style={{ fontSize: 12, color: "var(--brand-gold)", flex: 1 }}>{path}</code>
              <span style={{ fontSize: 12, color: "var(--text-muted)" }}>{desc}</span>
              <Globe size={12} color="var(--text-muted)" style={{ flexShrink: 0, opacity: 0.4 }}/>
            </div>
          ))}
        </div>
      </div>

      {/* cURL example */}
      <div style={{ background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: 12, overflow: "hidden" }}>
        <div style={{ padding: "14px 20px", borderBottom: "1px solid var(--border)", fontSize: 12, fontWeight: 700, color: "var(--text-secondary)", textTransform: "uppercase", letterSpacing: 1 }}>Quick Start</div>
        <div style={{ padding: 20 }}>
          <pre style={{ margin: 0, fontFamily: "monospace", fontSize: 12, color: "#a5f3fc", background: "#0d1117", padding: "16px 20px", borderRadius: 8, overflowX: "auto" as const, lineHeight: 1.7 }}>{curlExample}</pre>
        </div>
      </div>
    </div>
  );
}

// ── Main Page ─────────────────────────────────────────────────────────────────

export default function TenantPortalPage() {
  const [tab, setTab] = useState<Tab>("dashboard");
  const [tenantName, setTenantName] = useState("Partner");
  const [planTier, setPlanTier]     = useState("growth");
  const [tenantId, setTenantId]     = useState("");
  const [tenantToken, setTenantToken] = useState("");

  useEffect(() => {
    setTenantName(getCookie("cis_tenant_name") || "Partner");
    setPlanTier(getCookie("cis_tenant_plan") || "growth");
    setTenantId(getCookie("cis_tenant_id") || "");
    setTenantToken(getCookie("cis_tenant_token") || "");
  }, []);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 24 }}>
      {/* Page header */}
      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between" }}>
        <div>
          <h1 style={{ fontSize: 22, fontWeight: 800, color: "var(--text-primary)", margin: 0 }}>Tenant Portal</h1>
          <p style={{ color: "var(--text-secondary)", fontSize: 13, marginTop: 4, margin: 0 }}>
            {tenantName} ·{" "}
            <span style={{ color: "var(--brand-gold)", textTransform: "capitalize" }}>{planTier}</span>
          </p>
        </div>
      </div>

      {/* Tabs */}
      <div style={{ display: "flex", gap: 8 }}>
        <TabBtn id="dashboard" active={tab === "dashboard"} icon={<LayoutDashboard size={14}/>} label="Dashboard"      onClick={() => setTab("dashboard")}/>
        <TabBtn id="upload"    active={tab === "upload"}    icon={<Upload size={14}/>}           label="Batch Upload"  onClick={() => setTab("upload")}/>
        <TabBtn id="review"    active={tab === "review"}    icon={<FileSearch size={14}/>}       label="Content Review" onClick={() => setTab("review")}/>
        <TabBtn id="apikey"    active={tab === "apikey"}    icon={<Key size={14}/>}              label="API Access"    onClick={() => setTab("apikey")}/>
      </div>

      {/* Tab content */}
      {tab === "dashboard" && <DashboardTab tenantName={tenantName} planTier={planTier} token={tenantToken}/>}
      {tab === "upload"    && <BatchUploadTab/>}
      {tab === "review"    && <ContentReviewTab/>}
      {tab === "apikey"    && <ApiKeyTab tenantId={tenantId}/>}
    </div>
  );
}
