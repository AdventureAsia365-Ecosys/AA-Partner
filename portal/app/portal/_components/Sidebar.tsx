"use client";
// app/(tenant)/portal/_components/Sidebar.tsx — v2

import { LayoutDashboard, Globe2, BookOpen, Sparkles, Code2, LogOut } from "lucide-react";
import { T, serif, sans } from "./ui";

export type Tab = "dashboard" | "pool" | "catalog" | "brand" | "api";

interface Props {
  tab: Tab;
  setTab: (t: Tab) => void;
  poolCount: number;
  catalogCount: number;
  tenantName: string;
  planTier: string;
  onActivityLog?: () => void;
  onBilling?: () => void;
  onSettings?: () => void;
}

const NAV1: { id: Tab; icon: React.ReactNode; label: string }[] = [
  { id: "dashboard", icon: <LayoutDashboard size={15} />, label: "Dashboard" },
  { id: "pool",      icon: <Globe2 size={15} />,          label: "Browse Pool" },
  { id: "catalog",   icon: <BookOpen size={15} />,        label: "My Catalog" },
  { id: "brand",     icon: <Sparkles size={15} />,        label: "Brand Identity" },
  { id: "api",       icon: <Code2 size={15} />,           label: "API Access" },
];

export default function Sidebar({
  tab, setTab, poolCount, catalogCount, tenantName, planTier,
  onActivityLog, onBilling, onSettings,
}: Props) {
  const initials = tenantName.split(" ").map(w => w[0]).join("").slice(0, 2).toUpperCase();

  function logout() {
    ["cis_role","cis_user","cis_tenant_token","cis_tenant_id","cis_tenant_name","cis_tenant_plan"]
      .forEach(k => (document.cookie = `${k}=; path=/; max-age=0`));
    window.location.href = "/tenant-login";
  }

  const counts: Partial<Record<Tab, number>> = { pool: poolCount, catalog: catalogCount };
  const nav2 = [
    { label: "Activity Log", onClick: onActivityLog },
    { label: "Billing",      onClick: onBilling },
    { label: "Settings",     onClick: onSettings },
  ];

  return (
    <aside style={{
      width: 220, flexShrink: 0, background: T.ink, color: "#C9CFD8",
      padding: "22px 14px 24px", display: "flex", flexDirection: "column",
      gap: 28, position: "sticky", top: 0, height: "100vh", overflowY: "auto",
    }}>
      {/* Brand */}
      <div style={{ display: "flex", alignItems: "center", gap: 10, paddingBottom: 18, borderBottom: "1px solid rgba(255,255,255,0.07)" }}>
        <div style={{
          width: 32, height: 32, borderRadius: 7, flexShrink: 0,
          background: `linear-gradient(135deg,${T.gold} 0%,#B97A1B 100%)`,
          display: "grid", placeItems: "center",
          fontFamily: serif, fontWeight: 600, color: T.ink, fontSize: 17,
        }}>A</div>
        <div>
          <div style={{ fontFamily: serif, fontSize: 14, fontWeight: 500, color: "#F4F1EC", letterSpacing: "-0.01em", lineHeight: 1.15 }}>
            Adventure Asia
          </div>
          <div style={{ fontSize: 9.5, textTransform: "uppercase", letterSpacing: "0.18em", color: T.gold, fontWeight: 600, marginTop: 2 }}>
            CIS Platform
          </div>
        </div>
      </div>

      {/* Nav */}
      <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 26 }}>
        <NavGroup label="Workspace">
          {NAV1.map(n => (
            <NavItem key={n.id} active={tab === n.id} icon={n.icon} label={n.label}
              count={counts[n.id]} onClick={() => setTab(n.id)} />
          ))}
        </NavGroup>
        <NavGroup label="Account">
          {nav2.map(n => (
            <NavItem key={n.label} active={false} icon={null} label={n.label}
              onClick={() => n.onClick?.()} />
          ))}
        </NavGroup>
      </div>

      {/* Footer */}
      <div style={{ paddingTop: 14, borderTop: "1px solid rgba(255,255,255,0.07)" }}>
        <div style={{
          display: "flex", alignItems: "center", gap: 9,
          padding: 8, borderRadius: 8, background: "rgba(255,255,255,0.03)",
        }}>
          <div style={{
            width: 30, height: 30, borderRadius: 6, background: "#3A4453",
            display: "grid", placeItems: "center",
            color: "#F4E2C2", fontWeight: 600, fontSize: 11, flexShrink: 0,
          }}>{initials}</div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ color: "#F4F1EC", fontSize: 12, fontWeight: 600, lineHeight: 1.2, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {tenantName}
            </div>
            <div style={{ color: "#8A929D", fontSize: 10.5, lineHeight: 1.3 }}>
              {planTier.charAt(0).toUpperCase() + planTier.slice(1)} Plan
            </div>
          </div>
          <button onClick={logout} title="Sign out"
            style={{ background: "none", border: "none", cursor: "pointer", color: "#8A929D", padding: 2, display: "flex" }}>
            <LogOut size={12} />
          </button>
        </div>
      </div>
    </aside>
  );
}

function NavGroup({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div style={{ fontSize: 9.5, textTransform: "uppercase", letterSpacing: "0.16em", color: "#6E7681", padding: "0 10px 8px", fontWeight: 600 }}>
        {label}
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 1 }}>{children}</div>
    </div>
  );
}

function NavItem({ active, icon, label, count, onClick }: {
  active: boolean; icon: React.ReactNode; label: string;
  count?: number; onClick: () => void;
}) {
  return (
    <button onClick={onClick} style={{
      display: "flex", alignItems: "center", gap: 10, width: "100%",
      padding: "8px 10px", borderRadius: 7, border: "none",
      background: active ? "rgba(219,150,40,0.12)" : "transparent",
      color: active ? "#fff" : "#C9CFD8",
      fontSize: 13, fontWeight: 500, cursor: "pointer",
      textAlign: "left", fontFamily: sans, position: "relative",
      transition: "background .15s, color .15s",
    }}>
      {active && (
        <span style={{ position: "absolute", left: 0, top: 8, bottom: 8, width: 2, background: T.gold, borderRadius: "0 2px 2px 0" }} />
      )}
      {icon && <span style={{ flexShrink: 0, opacity: active ? 1 : 0.75 }}>{icon}</span>}
      <span style={{ flex: 1 }}>{label}</span>
      {count != null && count > 0 && (
        <span style={{ fontSize: 11, color: active ? T.gold : "#8A929D", fontVariantNumeric: "tabular-nums" }}>
          {count}
        </span>
      )}
    </button>
  );
}
