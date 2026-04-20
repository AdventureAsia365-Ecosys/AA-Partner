"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import { Key, Loader2 } from "lucide-react";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "https://api-cis.lumiguides.it.com";

export default function TenantLoginPage() {
  const [apiKey, setApiKey]     = useState("");
  const [error, setError]       = useState("");
  const [loading, setLoading]   = useState(false);
  const router = useRouter();

  const login = async () => {
    if (!apiKey.trim()) {
      setError("Please enter your API key");
      return;
    }
    setLoading(true);
    setError("");

    try {
      const res = await fetch(`${API_URL}/auth/tenant-login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ api_key: apiKey.trim() }),
      });

      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        setError(data.detail ?? "Invalid API key");
        return;
      }

      const { token, tenant_id, tenant_name, plan_tier } = await res.json();

      // Store JWT + metadata in cookies (24h, matches JWT expiry)
      const maxAge = 60 * 60 * 24;
      document.cookie = `cis_role=tenant; path=/; max-age=${maxAge}`;
      document.cookie = `cis_tenant_token=${token}; path=/; max-age=${maxAge}`;
      document.cookie = `cis_tenant_id=${tenant_id}; path=/; max-age=${maxAge}`;
      document.cookie = `cis_tenant_name=${encodeURIComponent(tenant_name)}; path=/; max-age=${maxAge}`;
      document.cookie = `cis_tenant_plan=${plan_tier}; path=/; max-age=${maxAge}`;

      router.push("/portal");
    } catch {
      setError("Connection error — please try again");
    } finally {
      setLoading(false);
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
            <input
              type="password"
              value={apiKey}
              onChange={e => { setApiKey(e.target.value); setError(""); }}
              placeholder="wl_live_sk_..."
              onKeyDown={e => e.key === "Enter" && !loading && login()}
              disabled={loading}
              style={{
                width:"100%", padding:"10px 12px 10px 34px",
                background:"var(--bg-primary)",
                border:`1px solid ${error ? "#ef4444" : "var(--border)"}`,
                borderRadius:8, color:"var(--text-primary)", fontSize:13, outline:"none",
                opacity: loading ? 0.6 : 1,
              }}
            />
          </div>
          {error && <div style={{ fontSize:12, color:"#ef4444", marginTop:6 }}>{error}</div>}
        </div>

        <button
          onClick={login}
          disabled={loading}
          style={{
            width:"100%", padding:12,
            background: loading ? "var(--border)" : "var(--brand-gold)",
            border:"none", borderRadius:8, color:"white",
            fontSize:14, fontWeight:700, cursor: loading ? "not-allowed" : "pointer",
            display:"flex", alignItems:"center", justifyContent:"center", gap:8,
          }}
        >
          {loading ? (
            <>
              <Loader2 size={14} style={{ animation:"spin 1s linear infinite" }} />
              Verifying...
            </>
          ) : "Access Portal"}
        </button>

        <div style={{ marginTop:16, padding:12, background:"rgba(219,150,40,0.06)", border:"1px solid rgba(219,150,40,0.2)", borderRadius:8, fontSize:12, color:"var(--text-muted)" }}>
          Test key (WanderLux): <code style={{ color:"var(--brand-gold)", fontSize:11 }}>wl_live_sk_test_wanderlux_2026</code>
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
