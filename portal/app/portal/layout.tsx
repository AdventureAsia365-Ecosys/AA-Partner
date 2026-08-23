"use client";
// app/(tenant)/portal/layout.tsx — AA-430 route migration.
//
// Was app/(tenant)/portal/page.tsx: a single client component that owned all shell
// state (tenantName/planTier/poolTotal/catTotal/billing/toast/globalSearch) AND
// conditionally rendered one of 8 Tab components inline based on a `tab` useState.
//
// Now each former "tab" is its own route under /portal/* (own page.tsx — see that
// directory listing), so the shell (Sidebar + topbar + toast) lives here instead: a
// real layout.tsx persists across navigations between sibling routes in the same
// segment, exactly like the old page.tsx persisted across `setTab()` calls, so moving
// the state up here (and exposing it to route pages via PortalShellContext, since a
// layout can't pass props into `children`) preserves the same "fetch/state survives
// switching sections" behavior — nothing is refetched or reset on navigation that
// wasn't refetched/reset on a tab switch before.
import { useState, useEffect, useRef } from "react";
import { usePathname, useRouter } from "next/navigation";
import { Search, Bell } from "lucide-react";
import Sidebar from "./_components/Sidebar";
import { PortalShellContext } from "./_components/PortalShellContext";
import { T, sans } from "./_components/ui";

const BREADCRUMBS: Record<string, string> = {
  "/portal/dashboard":  "Dashboard",
  "/portal/t1-rewrite": "Browse Pool",
  "/portal/t4-pool":    "My Catalog",
  "/portal/t0-brand":   "Brand Identity",
  "/portal/t6-atoms":   "Atom Curation", // AA-431
  "/portal/t7-planning": "Content Planning", // AA-448
  "/portal/marketplace": "Marketplace", // AA-444
  "/portal/api":        "API Access",
  "/portal/activity":   "Activity Log",
  "/portal/billing":    "Billing",
  "/portal/settings":   "Settings",
};

export default function PortalLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();

  const [tenantName, setName] = useState("Partner");
  const [planTier, setPlan]   = useState("growth");
  const [poolTotal, setPool]  = useState(0);
  const [catTotal, setCat]    = useState(0);
  const [billing, setBilling] = useState<any>(null);
  const [toast, setToast]     = useState<string | null>(null);
  const [globalSearch, setGlobalSearch] = useState("");
  const searchRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    // AA-443 (gap left by AA-427): cis_tenant_name / cis_tenant_plan became httpOnly in AA-427
    // (PR #184) — no longer readable from document.cookie. PR #184 moved every other file that
    // read these two cookies over to fetch("/api/tenant/me") but missed this one (this shell
    // moved out of portal/page.tsx into its own layout.tsx via AA-430, around the same time AA-427
    // was in flight), so tenantName/planTier silently stayed at their "Partner"/"growth" defaults
    // for every real tenant. Same fix, same pattern, applied here.
    fetch("/api/tenant/me")
      .then(r => (r.ok ? r.json() : null))
      .then(d => {
        if (d?.tenant_name) setName(d.tenant_name);
        if (d?.plan_tier) setPlan(d.plan_tier);
      })
      .catch(() => {});

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

  function refreshCatalogCount() {
    fetch("/api/tenant/v1/tours/my-versions?page_size=1")
      .then(r => r.ok ? r.json() : null)
      .then(d => { if (d) setCat(d.pagination?.total ?? 0); })
      .catch(() => {});
  }

  // Search box: focusing or hitting Enter with text jumps to Browse Pool (T1) — same
  // behavior as the old onFocus/onKeyDown handlers on the tab-state version.
  function goToPool() {
    if (pathname !== "/portal/t1-rewrite") router.push("/portal/t1-rewrite");
  }
  function handleSearchKey(e: React.KeyboardEvent) {
    if (e.key === "Enter" && globalSearch.trim()) goToPool();
  }

  return (
    <PortalShellContext.Provider value={{
      tenantName, planTier, poolTotal, catTotal, billing, globalSearch,
      refreshCatalogCount, showToast,
    }}>
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

        <Sidebar
          poolCount={poolTotal}
          catalogCount={catTotal}
          tenantName={tenantName}
          planTier={planTier}
          onNavClick={() => setGlobalSearch("")}
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
              <span style={{ color: T.body, fontWeight: 500 }}>{BREADCRUMBS[pathname] ?? ""}</span>
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
                onFocus={goToPool}
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
            {children}
          </main>
        </div>
      </div>
    </PortalShellContext.Provider>
  );
}
