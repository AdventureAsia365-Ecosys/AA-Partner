"use client";
/**
 * app/(tenant)/portal/page.tsx — v2
 * Added: Activity Log, Billing, Settings tabs
 * Fixed: search in topbar wires to pool search state
 */

import { useState, useEffect, useRef } from "react";
import { Search, Bell } from "lucide-react";
import Sidebar, { type Tab } from "./_components/Sidebar";
import DashboardTab from "./_components/DashboardTab";
import PoolTab      from "./_components/PoolTab";
import CatalogTab   from "./_components/CatalogTab";
import BrandTab     from "./_components/BrandTab";
import ApiTab       from "./_components/ApiTab";
import { ActivityLogTab, BillingTab, SettingsTab } from "./_components/PlaceholderTabs";
import { T, sans } from "./_components/ui";

function getCookie(n: string) {
  if (typeof document === "undefined") return "";
  const m = document.cookie.match(new RegExp(`(^| )${n}=([^;]+)`));
  return m ? decodeURIComponent(m[2]) : "";
}

type ExtTab = Tab | "activity" | "billing" | "settings";

const BREADCRUMBS: Record<ExtTab, string> = {
  dashboard: "Dashboard",
  pool:      "Browse Pool",
  catalog:   "My Catalog",
  brand:     "Brand Identity",
  api:       "API Access",
  activity:  "Activity Log",
  billing:   "Billing",
  settings:  "Settings",
};

export default function PortalPage() {
  const [tab, setTab]         = useState<ExtTab>("dashboard");
  const [tenantName, setName] = useState("Partner");
  const [planTier, setPlan]   = useState("growth");
  const [poolTotal, setPool]  = useState(0);
  const [catTotal, setCat]    = useState(0);
  const [billing, setBilling] = useState<any>(null);
  const [toast, setToast]     = useState<string | null>(null);

  // Global search — passed down to PoolTab when on pool tab
  const [globalSearch, setGlobalSearch] = useState("");
  const searchRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const n = getCookie("cis_tenant_name");
    const p = getCookie("cis_tenant_plan");
    if (n) setName(n);
    if (p) setPlan(p);

    Promise.all([
      fetch("/api/tenant/v1/tours/pool?page_size=1"),
      fetch("/api/tenant/v1/tours/my-versions?page_size=1"),
      fetch("/api/admin/billing"),
    ]).then(async ([pRes, cRes, bRes]) => {
      if (pRes.ok) { const d = await pRes.json(); setPool(d.pagination?.total ?? 0); }
      if (cRes.ok) { const d = await cRes.json(); setCat(d.pagination?.total ?? 0); }
      if (bRes.ok) setBilling(await bRes.json());
    }).catch(() => {});
  }, []);

  // ⌘K focus search
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        searchRef.current?.focus();
      }
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, []);

  function showToast(msg: string) {
    setToast(msg);
    setTimeout(() => setToast(null), 4000);
  }

  function handleRewriteDone() {
    showToast("Rewrite started — check My Catalog in ~30 seconds.");
    fetch("/api/tenant/v1/tours/my-versions?page_size=1")
      .then(r => r.ok ? r.json() : null)
      .then(d => { if (d) setCat(d.pagination?.total ?? 0); })
      .catch(() => {});
    setTab("catalog");
  }

  // When search entered, switch to pool tab
  function handleSearchKey(e: React.KeyboardEvent) {
    if (e.key === "Enter" && globalSearch.trim()) {
      setTab("pool");
    }
  }

  const activity = billing?.activity ?? [];

  return (
    <div style={{ display: "flex", minHeight: "100vh", fontFamily: sans, background: T.bg }}>

      {/* Toast */}
      {toast && (
        <div style={{
          position: "fixed", top: 20, right: 24, zIndex: 999,
          padding: "12px 20px", background: T.green, borderRadius: 10,
          color: "#fff", fontSize: 13, fontWeight: 600,
          boxShadow: "0 4px 20px rgba(0,0,0,0.18)",
        }}>
          ✓ {toast}
        </div>
      )}

      {/* Sidebar — cast to Tab for the 5 main tabs */}
      <Sidebar
        tab={["dashboard","pool","catalog","brand","api"].includes(tab) ? tab as Tab : "dashboard"}
        setTab={(t) => { setTab(t); setGlobalSearch(""); }}
        poolCount={poolTotal}
        catalogCount={catTotal}
        tenantName={tenantName}
        planTier={planTier}
        onActivityLog={() => setTab("activity")}
        onBilling={() => setTab("billing")}
        onSettings={() => setTab("settings")}
      />

      <div style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0, overflow: "hidden" }}>

        {/* Top bar */}
        <header style={{
          height: 56, background: "#fff", borderBottom: `1px solid ${T.line}`,
          display: "flex", alignItems: "center", padding: "0 32px", gap: 16,
          position: "sticky", top: 0, zIndex: 10, flexShrink: 0,
        }}>
          <div style={{ fontSize: 12, color: T.muted2, display: "flex", gap: 6, alignItems: "center" }}>
            <span>Workspace</span>
            <span style={{ color: T.line }}>/</span>
            <span style={{ color: T.body, fontWeight: 500 }}>{BREADCRUMBS[tab]}</span>
          </div>
          <div style={{ flex: 1 }} />

          {/* Functional search */}
          <div style={{ position: "relative" }}>
            <Search size={13} style={{ position: "absolute", left: 12, top: "50%", transform: "translateY(-50%)", color: T.muted2 }} />
            <input
              ref={searchRef}
              value={globalSearch}
              onChange={e => setGlobalSearch(e.target.value)}
              onKeyDown={handleSearchKey}
              onFocus={() => { if (tab !== "pool") setTab("pool"); }}
              placeholder="Search tours, jobs…"
              style={{
                background: T.bg, border: `1px solid ${T.line}`, padding: "7px 40px 7px 32px",
                borderRadius: 8, width: 240, fontSize: 13, color: T.body, outline: "none",
                fontFamily: sans,
              }}
            />
            <span style={{
              position: "absolute", right: 10, top: "50%", transform: "translateY(-50%)",
              fontFamily: "'JetBrains Mono', monospace", fontSize: 10, color: T.muted2,
              background: "#fff", border: `1px solid ${T.line}`, padding: "1px 5px", borderRadius: 3,
              pointerEvents: "none",
            }}>⌘K</span>
          </div>

          <button style={{ width: 36, height: 36, borderRadius: 8, background: "#fff", border: `1px solid ${T.line}`, display: "grid", placeItems: "center", cursor: "pointer", color: T.ink3 }}>
            <Bell size={15} />
          </button>
        </header>

        {/* Content */}
        <main style={{ flex: 1, overflowY: "auto", padding: "28px 36px 56px" }}>
          {tab === "dashboard" && <DashboardTab onTabChange={t => setTab(t)} />}
          {tab === "pool"      && <PoolTab onRewriteDone={handleRewriteDone} externalSearch={globalSearch} />}
          {tab === "catalog"   && <CatalogTab />}
          {tab === "brand"     && <BrandTab />}
          {tab === "api"       && <ApiTab />}
          {tab === "activity"  && <ActivityLogTab activity={activity} />}
          {tab === "billing"   && <BillingTab billing={billing} />}
          {tab === "settings"  && <SettingsTab />}
        </main>
      </div>
    </div>
  );
}
