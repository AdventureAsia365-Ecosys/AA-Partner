"use client";
import { useState } from "react";
import { CheckCircle, XCircle, RotateCcw, Copy, Eye, EyeOff, Search } from "lucide-react";

const MOCK_TENANT = {
  id: "t-001", name: "Wanderlust Premium", slug: "wanderlust-premium",
  plan: "professional", api_key: "***REMOVED***",
  tours_this_month: 142, quota: 500,
};

const MOCK_CATALOG = [
  { id:"c-001", name:"Halong Bay Private Cruise",  country:"Vietnam",   trip_type:"coastal",   quality_score:9.2 },
  { id:"c-002", name:"Angkor Wat Sunrise Trek",    country:"Cambodia",  trip_type:"cultural",  quality_score:8.7 },
  { id:"c-003", name:"Bali Wellness Retreat",      country:"Indonesia", trip_type:"wellness",  quality_score:9.5 },
  { id:"c-004", name:"Kyoto Culinary Journey",     country:"Japan",     trip_type:"culinary",  quality_score:8.9 },
  { id:"c-005", name:"Sri Lanka Wildlife Safari",  country:"Sri Lanka", trip_type:"wildlife",  quality_score:9.0 },
  { id:"c-006", name:"Mekong Delta Explorer",      country:"Vietnam",   trip_type:"adventure", quality_score:8.1 },
];

const TYPE_COLORS: Record<string, { bg: string; color: string }> = {
  cultural:  { bg:"rgba(139,92,246,0.15)", color:"#a78bfa" },
  adventure: { bg:"rgba(249,115,22,0.15)", color:"#fb923c" },
  wellness:  { bg:"rgba(34,197,94,0.15)",  color:"#4ade80" },
  culinary:  { bg:"rgba(234,179,8,0.15)",  color:"#facc15" },
  wildlife:  { bg:"rgba(16,185,129,0.15)", color:"#34d399" },
  coastal:   { bg:"rgba(59,130,246,0.15)", color:"#60a5fa" },
};

function Badge({ label, bg, color }: { label: string; bg: string; color: string }) {
  return <span style={{ padding:"3px 10px", borderRadius:20, fontSize:11, fontWeight:600, background:bg, color, border:`1px solid ${color}33` }}>{label}</span>;
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ background:"var(--bg-card)", border:"1px solid var(--border)", borderRadius:12, overflow:"hidden" }}>
      <div style={{ padding:"14px 20px", borderBottom:"1px solid var(--border)", fontSize:12, fontWeight:700, color:"var(--text-secondary)", textTransform:"uppercase" as const, letterSpacing:1 }}>{title}</div>
      <div style={{ padding:20 }}>{children}</div>
    </div>
  );
}

const MOCK_REWRITE = {
  original: {
    name: "Halong Bay Private Cruise",
    summary: "Discover the timeless grandeur of Halong Bay aboard a refined private vessel, designed for discerning travellers.",
    highlights: ["Private sundeck with panoramic karst views", "Chef-prepared Vietnamese cuisine", "Guided kayaking through lagoons"],
    seo_title: "Halong Bay Private Cruise | Adventure Asia",
  },
  tenant: {
    name: "Halong Bay Exclusive Cruise — Wanderlust Collection",
    summary: "Embark on an extraordinary private voyage through the emerald waters of Halong Bay, curated exclusively for Wanderlust Premium members.",
    highlights: ["Exclusive member access to private bay anchoring", "Personalised chef menu with dietary preferences", "Private kayaking guide — no group tours"],
    seo_title: "Halong Bay Exclusive Cruise | Wanderlust Premium",
  },
};

type Tab = "tours" | "config" | "rewrite" | "apikey";

export default function TenantPage() {
  const [tab, setTab]                 = useState<Tab>("tours");
  const [search, setSearch]           = useState("");
  const [selectedTour, setSelected]   = useState<typeof MOCK_CATALOG[0] | null>(null);
  const [showKey, setShowKey]         = useState(false);
  const [copied, setCopied]           = useState(false);
  const [isRewriting, setRewriting]   = useState(false);
  const [rewriteDone, setRewriteDone] = useState(false);
  const [reviewDone, setReviewDone]   = useState<"approved"|"rejected"|null>(null);
  const [brandVoice, setBrandVoice]   = useState("Exclusive, personalised, member-focused");
  const [forbidden, setForbidden]     = useState("cheap, deal, discount, book now");
  const [outputFmt, setOutputFmt]     = useState("json");

  const filtered = MOCK_CATALOG.filter(t =>
    t.name.toLowerCase().includes(search.toLowerCase()) ||
    t.country.toLowerCase().includes(search.toLowerCase())
  );

  const copyKey = () => {
    navigator.clipboard.writeText(MOCK_TENANT.api_key);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const runRewrite = async () => {
    setRewriting(true); setRewriteDone(false); setReviewDone(null);
    await new Promise(r => setTimeout(r, 2500));
    setRewriting(false); setRewriteDone(true);
  };

  const quotaPct = Math.round((MOCK_TENANT.tours_this_month / MOCK_TENANT.quota) * 100);

  const tabStyle = (t: Tab): React.CSSProperties => ({
    padding:"8px 18px", borderRadius:8, fontSize:13, fontWeight:500,
    cursor:"pointer", border:"none", transition:"all 0.15s",
    background: tab === t ? "var(--brand-gold)" : "var(--bg-card)",
    color: tab === t ? "white" : "var(--text-secondary)",
  });

  return (
    <div style={{ display:"flex", flexDirection:"column", gap:24 }}>
      {/* Header */}
      <div style={{ display:"flex", alignItems:"flex-start", justifyContent:"space-between" }}>
        <div>
          <h1 style={{ fontSize:24, fontWeight:700, color:"var(--text-primary)", margin:0 }}>Tenant Portal</h1>
          <p style={{ color:"var(--text-secondary)", fontSize:13, marginTop:6 }}>
            {MOCK_TENANT.name} · <span style={{ color:"var(--brand-gold)" }}>{MOCK_TENANT.plan}</span>
          </p>
        </div>
        <div style={{ background:"var(--bg-card)", border:"1px solid var(--border)", borderRadius:10, padding:"12px 20px", minWidth:200 }}>
          <div style={{ display:"flex", justifyContent:"space-between", fontSize:12, color:"var(--text-secondary)", marginBottom:8 }}>
            <span>Monthly quota</span>
            <span style={{ color:"var(--brand-gold)", fontWeight:700 }}>{MOCK_TENANT.tours_this_month}/{MOCK_TENANT.quota}</span>
          </div>
          <div style={{ height:6, background:"var(--border)", borderRadius:3, overflow:"hidden" }}>
            <div style={{ height:"100%", width:`${quotaPct}%`, background:"linear-gradient(90deg, var(--brand-gold), #f59e0b)", borderRadius:3 }} />
          </div>
          <div style={{ fontSize:11, color:"var(--text-muted)", marginTop:6 }}>{quotaPct}% used this month</div>
        </div>
      </div>

      {/* Tabs */}
      <div style={{ display:"flex", gap:6 }}>
        <button style={tabStyle("tours")}   onClick={() => setTab("tours")}>🗺 Tour Browser</button>
        <button style={tabStyle("config")}  onClick={() => setTab("config")}>⚙️ Brand Config</button>
        <button style={tabStyle("rewrite")} onClick={() => setTab("rewrite")}>✨ Rewrite & Review</button>
        <button style={tabStyle("apikey")}  onClick={() => setTab("apikey")}>🔑 API Access</button>
      </div>

      {/* TAB: Tour Browser */}
      {tab === "tours" && (
        <Section title="Published Catalog — Select Tour to Rewrite">
          <div style={{ position:"relative", marginBottom:16 }}>
            <Search size={13} style={{ position:"absolute", left:10, top:"50%", transform:"translateY(-50%)", color:"var(--text-muted)" }} />
            <input type="text" placeholder="Search tours..." value={search} onChange={e => setSearch(e.target.value)}
              style={{ width:"100%", padding:"8px 12px 8px 32px", background:"var(--bg-primary)", border:"1px solid var(--border)", borderRadius:8, color:"var(--text-primary)", fontSize:13, outline:"none" }} />
          </div>
          <div style={{ display:"flex", flexDirection:"column", gap:8 }}>
            {filtered.map(tour => {
              const tc = TYPE_COLORS[tour.trip_type] || { bg:"var(--border)", color:"var(--text-muted)" };
              const isSelected = selectedTour?.id === tour.id;
              return (
                <div key={tour.id} onClick={() => setSelected(isSelected ? null : tour)}
                  style={{ display:"flex", alignItems:"center", gap:16, padding:"12px 16px", borderRadius:10, cursor:"pointer", transition:"all 0.15s", background: isSelected ? "rgba(219,150,40,0.08)" : "var(--bg-primary)", border:`1px solid ${isSelected ? "var(--brand-gold)" : "var(--border)"}` }}>
                  <div style={{ width:20, height:20, borderRadius:"50%", border:`2px solid ${isSelected ? "var(--brand-gold)" : "var(--border)"}`, background: isSelected ? "var(--brand-gold)" : "transparent", flexShrink:0, display:"flex", alignItems:"center", justifyContent:"center" }}>
                    {isSelected && <CheckCircle size={12} color="white" />}
                  </div>
                  <div style={{ flex:1 }}>
                    <div style={{ fontSize:13, fontWeight:600, color:"var(--text-primary)" }}>{tour.name}</div>
                    <div style={{ fontSize:11, color:"var(--text-muted)", marginTop:2 }}>{tour.country}</div>
                  </div>
                  <Badge label={tour.trip_type} bg={tc.bg} color={tc.color} />
                  <span style={{ fontWeight:800, fontSize:13, color: tour.quality_score >= 9 ? "#22c55e" : "#DB9628" }}>{tour.quality_score}</span>
                </div>
              );
            })}
          </div>
          {selectedTour && (
            <div style={{ marginTop:16, padding:14, background:"rgba(219,150,40,0.06)", border:"1px solid rgba(219,150,40,0.3)", borderRadius:10 }}>
              <div style={{ fontSize:13, color:"var(--brand-gold)", fontWeight:600, marginBottom:8 }}>✓ Selected: {selectedTour.name}</div>
              <button onClick={() => setTab("rewrite")}
                style={{ padding:"8px 20px", background:"var(--brand-gold)", border:"none", borderRadius:8, color:"white", fontSize:13, fontWeight:700, cursor:"pointer" }}>
                Rewrite for {MOCK_TENANT.name} →
              </button>
            </div>
          )}
        </Section>
      )}

      {/* TAB: Brand Config */}
      {tab === "config" && (
        <Section title="Brand & SEO Configuration">
          <div style={{ display:"flex", flexDirection:"column", gap:20 }}>
            <div>
              <label style={{ fontSize:11, fontWeight:600, color:"var(--text-muted)", textTransform:"uppercase" as const, letterSpacing:1, display:"block", marginBottom:8 }}>Brand Voice</label>
              <textarea value={brandVoice} onChange={e => setBrandVoice(e.target.value)} rows={3}
                style={{ width:"100%", padding:12, background:"var(--bg-primary)", border:"1px solid var(--border)", borderRadius:8, color:"var(--text-primary)", fontSize:13, resize:"vertical" as const, outline:"none", lineHeight:1.6 }} />
            </div>
            <div>
              <label style={{ fontSize:11, fontWeight:600, color:"var(--text-muted)", textTransform:"uppercase" as const, letterSpacing:1, display:"block", marginBottom:8 }}>Forbidden Words</label>
              <input type="text" value={forbidden} onChange={e => setForbidden(e.target.value)}
                style={{ width:"100%", padding:12, background:"var(--bg-primary)", border:"1px solid var(--border)", borderRadius:8, color:"var(--text-primary)", fontSize:13, outline:"none" }} />
              <div style={{ marginTop:8, display:"flex", flexWrap:"wrap" as const, gap:6 }}>
                {forbidden.split(",").map(w => w.trim()).filter(Boolean).map(w => (
                  <span key={w} style={{ padding:"2px 10px", background:"rgba(239,68,68,0.1)", color:"#f87171", border:"1px solid rgba(239,68,68,0.2)", borderRadius:20, fontSize:11 }}>{w}</span>
                ))}
              </div>
            </div>
            <div>
              <label style={{ fontSize:11, fontWeight:600, color:"var(--text-muted)", textTransform:"uppercase" as const, letterSpacing:1, display:"block", marginBottom:8 }}>Output Format</label>
              <div style={{ display:"flex", gap:8 }}>
                {["json","markdown","html"].map(f => (
                  <button key={f} onClick={() => setOutputFmt(f)}
                    style={{ padding:"8px 16px", borderRadius:8, border:`1px solid ${outputFmt===f ? "var(--brand-gold)" : "var(--border)"}`, background: outputFmt===f ? "rgba(219,150,40,0.1)" : "var(--bg-primary)", color: outputFmt===f ? "var(--brand-gold)" : "var(--text-secondary)", fontSize:13, fontWeight: outputFmt===f ? 600 : 400, cursor:"pointer" }}>
                    {f.toUpperCase()}
                  </button>
                ))}
              </div>
            </div>
            <button style={{ padding:"10px 24px", background:"var(--brand-gold)", border:"none", borderRadius:8, color:"white", fontSize:13, fontWeight:700, cursor:"pointer", alignSelf:"flex-start" as const }}>
              Save Configuration
            </button>
          </div>
        </Section>
      )}

      {/* TAB: Rewrite & Review */}
      {tab === "rewrite" && (
        <Section title={selectedTour ? `Rewrite: ${selectedTour.name}` : "Rewrite & Review"}>
          {!selectedTour ? (
            <div style={{ textAlign:"center", padding:"24px 0", color:"var(--text-muted)" }}>
              <div style={{ marginBottom:8 }}>No tour selected</div>
              <button onClick={() => setTab("tours")}
                style={{ padding:"8px 20px", background:"var(--brand-gold)", border:"none", borderRadius:8, color:"white", fontSize:13, cursor:"pointer" }}>
                Go to Tour Browser
              </button>
            </div>
          ) : (
            <div>
              <div style={{ display:"flex", gap:12, alignItems:"center", marginBottom:16 }}>
                <div style={{ flex:1, fontSize:13, color:"var(--text-secondary)" }}>
                  Applying brand config for <strong style={{ color:"var(--brand-gold)" }}>{MOCK_TENANT.name}</strong>
                </div>
                <button onClick={runRewrite} disabled={isRewriting}
                  style={{ padding:"10px 24px", background:isRewriting?"var(--border)":"var(--brand-gold)", border:"none", borderRadius:8, color:isRewriting?"var(--text-muted)":"white", fontSize:13, fontWeight:700, cursor:isRewriting?"not-allowed":"pointer" }}>
                  {isRewriting ? "Rewriting..." : rewriteDone ? "Regenerate" : "Generate Tenant Rewrite"}
                </button>
              </div>
              {rewriteDone && (
                <>
                  <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr", gap:12, marginBottom:16 }}>
                    <div style={{ background:"var(--before-bg)", border:"1px solid var(--before-border)", borderRadius:10, overflow:"hidden" }}>
                      <div style={{ padding:"10px 16px", background:"#F5C6C6", fontSize:11, fontWeight:700, color:"#7F1D1D" }}>AA ORIGINAL</div>
                      <div style={{ padding:16, display:"flex", flexDirection:"column", gap:10 }}>
                        <div><div style={{ fontSize:10, color:"#9B1C1C", marginBottom:4 }}>NAME</div><div style={{ fontSize:12, color:"#374151", fontFamily:"monospace" }}>{MOCK_REWRITE.original.name}</div></div>
                        <div><div style={{ fontSize:10, color:"#9B1C1C", marginBottom:4 }}>SUMMARY</div><div style={{ fontSize:12, color:"#374151", fontFamily:"monospace", lineHeight:1.6 }}>{MOCK_REWRITE.original.summary}</div></div>
                        <div><div style={{ fontSize:10, color:"#9B1C1C", marginBottom:4 }}>HIGHLIGHTS</div><ul style={{ margin:0, padding:"0 0 0 16px" }}>{MOCK_REWRITE.original.highlights.map((h,i) => <li key={i} style={{ fontSize:12, color:"#374151", fontFamily:"monospace" }}>{h}</li>)}</ul></div>
                      </div>
                    </div>
                    <div style={{ background:"var(--after-bg)", border:"1px solid var(--after-border)", borderRadius:10, overflow:"hidden" }}>
                      <div style={{ padding:"10px 16px", background:"#B8DFA0", fontSize:11, fontWeight:700, color:"#166534" }}>TENANT VERSION</div>
                      <div style={{ padding:16, display:"flex", flexDirection:"column", gap:10 }}>
                        <div><div style={{ fontSize:10, color:"#166534", marginBottom:4 }}>NAME</div><div contentEditable suppressContentEditableWarning style={{ fontSize:12, color:"#14532D", padding:"4px 6px", borderRadius:4, background:"rgba(255,255,255,0.5)", outline:"none" }}>{MOCK_REWRITE.tenant.name}</div></div>
                        <div><div style={{ fontSize:10, color:"#166534", marginBottom:4 }}>SUMMARY</div><div contentEditable suppressContentEditableWarning style={{ fontSize:12, color:"#14532D", lineHeight:1.6, padding:"4px 6px", borderRadius:4, background:"rgba(255,255,255,0.5)", outline:"none" }}>{MOCK_REWRITE.tenant.summary}</div></div>
                        <div><div style={{ fontSize:10, color:"#166534", marginBottom:4 }}>HIGHLIGHTS</div><ul style={{ margin:0, padding:"0 0 0 16px" }}>{MOCK_REWRITE.tenant.highlights.map((h,i) => <li key={i} style={{ fontSize:12, color:"#14532D" }}>{h}</li>)}</ul></div>
                      </div>
                    </div>
                  </div>
                  {!reviewDone ? (
                    <div style={{ display:"flex", gap:10, justifyContent:"flex-end" }}>
                      <button onClick={() => setReviewDone("rejected")} style={{ padding:"10px 20px", background:"rgba(239,68,68,0.1)", border:"1px solid rgba(239,68,68,0.3)", borderRadius:8, color:"#ef4444", fontSize:13, fontWeight:600, cursor:"pointer" }}>Reject</button>
                      <button onClick={runRewrite} style={{ padding:"10px 20px", background:"var(--bg-card)", border:"1px solid var(--border)", borderRadius:8, color:"var(--text-secondary)", fontSize:13, cursor:"pointer" }}>Regenerate</button>
                      <button onClick={() => setReviewDone("approved")} style={{ padding:"10px 24px", background:"#166534", border:"1px solid #22c55e33", borderRadius:8, color:"#22c55e", fontSize:13, fontWeight:700, cursor:"pointer" }}>Approve and Export</button>
                    </div>
                  ) : (
                    <div style={{ padding:16, borderRadius:10, background:reviewDone==="approved"?"rgba(34,197,94,0.08)":"rgba(239,68,68,0.08)", border:`1px solid ${reviewDone==="approved"?"rgba(34,197,94,0.2)":"rgba(239,68,68,0.2)"}` }}>
                      <span style={{ color:reviewDone==="approved"?"#22c55e":"#ef4444", fontWeight:600, fontSize:13 }}>
                        {reviewDone==="approved" ? "Approved - available via API" : "Rejected - not published"}
                      </span>
                    </div>
                  )}
                </>
              )}
            </div>
          )}
        </Section>
      )}

      {/* TAB: API Access */}
      {tab === "apikey" && (
        <div style={{ display:"flex", flexDirection:"column", gap:16 }}>
          <Section title="API Key">
            <div style={{ display:"flex", flexDirection:"column", gap:16 }}>
              <div style={{ display:"flex", alignItems:"center", gap:10 }}>
                <div style={{ flex:1, fontFamily:"monospace", fontSize:13, padding:"10px 16px", background:"var(--bg-primary)", border:"1px solid var(--border)", borderRadius:8, color:showKey?"var(--brand-gold)":"var(--text-muted)" }}>
                  {showKey ? MOCK_TENANT.api_key : "x".repeat(MOCK_TENANT.api_key.length)}
                </div>
                <button onClick={() => setShowKey(!showKey)} style={{ padding:"10px 14px", background:"var(--bg-card)", border:"1px solid var(--border)", borderRadius:8, cursor:"pointer", color:"var(--text-secondary)", display:"flex", alignItems:"center" }}>
                  {showKey ? <EyeOff size={16}/> : <Eye size={16}/>}
                </button>
                <button onClick={copyKey} style={{ padding:"10px 16px", background:copied?"rgba(34,197,94,0.1)":"var(--bg-card)", border:`1px solid ${copied?"rgba(34,197,94,0.3)":"var(--border)"}`, borderRadius:8, cursor:"pointer", color:copied?"#22c55e":"var(--text-secondary)", fontSize:13, fontWeight:600, display:"flex", alignItems:"center", gap:6 }}>
                  <Copy size={14}/> {copied?"Copied!":"Copy"}
                </button>
              </div>
            </div>
          </Section>
          <Section title="API Endpoints">
            <div style={{ display:"flex", flexDirection:"column", gap:8 }}>
              {[["GET","/v1/tours","List approved tours"],["GET","/v1/tours/{id}","Get single tour"],["POST","/v1/tours/{id}/rewrite","Trigger rewrite"],["GET","/v1/catalog","Browse AA catalog"],["GET","/v1/usage","Check quota"]].map(([m,p,d]) => (
                <div key={p} style={{ display:"flex", alignItems:"center", gap:12, padding:"10px 14px", background:"var(--bg-primary)", border:"1px solid var(--border)", borderRadius:8 }}>
                  <span style={{ fontSize:11, fontWeight:700, padding:"2px 8px", borderRadius:4, background:m==="GET"?"rgba(59,130,246,0.15)":"rgba(34,197,94,0.15)", color:m==="GET"?"#60a5fa":"#4ade80" }}>{m}</span>
                  <code style={{ fontSize:12, color:"var(--brand-gold)", flex:1 }}>{p}</code>
                  <span style={{ fontSize:12, color:"var(--text-muted)" }}>{d}</span>
                </div>
              ))}
            </div>
          </Section>
        </div>
      )}
    </div>
  );
}
