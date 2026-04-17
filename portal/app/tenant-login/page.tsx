"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import { Key } from "lucide-react";

export default function TenantLoginPage() {
  const [apiKey, setApiKey] = useState("");
  const [error, setError]   = useState("");
  const router = useRouter();

  const login = () => {
    // Mock validation — replace với real API call
    if (apiKey.startsWith("wl_live_")) {
      document.cookie = `cis_role=tenant; path=/; max-age=86400`;
      document.cookie = `cis_tenant_key=${apiKey}; path=/; max-age=86400`;
      router.push("/portal");
    } else {
      setError("Invalid API key. Keys start with wl_live_");
    }
  };

  return (
    <div style={{ minHeight:"100vh", display:"flex", alignItems:"center", justifyContent:"center", background:"var(--bg-primary)" }}>
      <div style={{ background:"var(--bg-card)", border:"1px solid var(--border)", borderRadius:16, padding:40, width:380 }}>
        <div style={{ display:"flex", alignItems:"center", gap:12, marginBottom:8 }}>
          <div style={{ width:36, height:36, background:"var(--brand-gold)", borderRadius:8, display:"flex", alignItems:"center", justifyContent:"center", fontWeight:800, color:"white" }}>AA</div>
          <div>
            <div style={{ fontWeight:700, color:"var(--text-primary)", fontSize:16 }}>Partner Portal</div>
            <div style={{ fontSize:11, color:"var(--text-muted)" }}>Adventure Asia B2B</div>
          </div>
        </div>
        <p style={{ fontSize:13, color:"var(--text-secondary)", marginBottom:24 }}>
          Enter your API key to access the tenant portal.
        </p>

        <div style={{ marginBottom:16 }}>
          <label style={{ fontSize:11, fontWeight:600, color:"var(--text-muted)", textTransform:"uppercase" as const, letterSpacing:1, display:"block", marginBottom:8 }}>
            API Key
          </label>
          <div style={{ position:"relative" }}>
            <Key size={13} style={{ position:"absolute", left:12, top:"50%", transform:"translateY(-50%)", color:"var(--text-muted)" }} />
            <input type="password" value={apiKey} onChange={e => { setApiKey(e.target.value); setError(""); }}
              placeholder="wl_live_sk_..."
              onKeyDown={e => e.key === "Enter" && login()}
              style={{ width:"100%", padding:"10px 12px 10px 34px", background:"var(--bg-primary)", border:`1px solid ${error ? "#ef4444" : "var(--border)"}`, borderRadius:8, color:"var(--text-primary)", fontSize:13, outline:"none" }} />
          </div>
          {error && <div style={{ fontSize:12, color:"#ef4444", marginTop:6 }}>{error}</div>}
        </div>

        <button onClick={login} style={{ width:"100%", padding:12, background:"var(--brand-gold)", border:"none", borderRadius:8, color:"white", fontSize:14, fontWeight:700, cursor:"pointer" }}>
          Access Portal
        </button>

        <div style={{ marginTop:16, padding:12, background:"rgba(219,150,40,0.06)", border:"1px solid rgba(219,150,40,0.2)", borderRadius:8, fontSize:12, color:"var(--text-muted)" }}>
          Demo key: <code style={{ color:"var(--brand-gold)" }}>***REMOVED***</code>
        </div>

        <div style={{ marginTop:16, textAlign:"center" as const }}>
          <a href="/login" style={{ fontSize:12, color:"var(--text-muted)", textDecoration:"none" }}>
            ← Staff login
          </a>
        </div>
      </div>
    </div>
  );
}
