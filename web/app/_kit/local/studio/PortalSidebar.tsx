"use client";
// web/app/_kit/local/studio/PortalSidebar.tsx
// AA-759 — the portal sidebar: navy panel in both themes (--aa-side-* tokens), the nav grouped by
// workflow (portalNav.tsx NAV_GROUPS), a collapse rail (chevron button or the `[` key, ignored while
// typing; state in the vendored sidebarStore, restored before paint by the root layout script).
//
// Presentation is CSS-driven (globals.css `data-pshell` rules): the width, the collapsed rail
// (labels and group titles hidden, a dot instead of the count pill) and the phone drawer (fixed,
// off-canvas, `data-open`). Nothing here sets width or position inline, so the CSS always wins.
//
// S264 — the titled groups (Tours, Brand, Create, Content, Account, Partner CRM) fold through the kit NavGroup +
// useNavGroups (core, scope "portal"): folded unless the user opened them, the group of the current page open on
// arrival. Overview has no title and stays open. The collapsed rail (desktop, >= 900 px) shows every item. The item
// counts (tours available, my tours) are sizes, not things to act on, so a folded header does not repeat them.

import { useEffect } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { ChevronsLeft, ChevronsRight, ExternalLink, Map as MapIcon, Plus, Users } from "lucide-react";
import type { AppEntry } from "@/lib/apps";
import {
  K, NavGroup, alpha, navGroupId, sans, serif, useHydrated, useMediaQuery, useNavGroups, useSidebarCollapsed,
} from "@/app/_kit";
import { LOGO_SRC } from "../../core/brand";
import { NAV_GROUPS, ROUTES, navActive, type NavEntry } from "./portalNav";

// AA-1036 (R3 slice 1, root ADR 0019 d7): the app switcher replaces the old "Open CRM" cross-link. Content is where the
// person is; an add-on the tenant holds opens (the CRM home, a full page load: plain <a>, so the studio's stylesheet
// never stays on a CRM page); any other add-on shows "Request add-on" (a message to Adventure Asia). The entries come
// from /api/studio/apps (lib/apps.ts), which always answers 200. Outside NAV_GROUPS on purpose: other apps, so neither
// the ⌘K palette nor the phone bottom bar routes to them. Until it answers, nothing shows.
const APPS_KEY = ["studio-apps"] as const;

async function fetchApps(): Promise<AppEntry[]> {
  const res = await fetch("/api/studio/apps", { cache: "no-store", headers: { Accept: "application/json" } });
  if (!res.ok) return [];
  const body = (await res.json()) as { apps?: AppEntry[] };
  return Array.isArray(body.apps) ? body.apps : [];
}

export function PortalSidebar({ drawerOpen, poolCount, catalogCount, onNavigate }: {
  drawerOpen: boolean;
  poolCount: number | null;
  catalogCount: number;
  onNavigate: () => void;
}) {
  const pathname = usePathname();
  const [collapsed, setCollapsed] = useSidebarCollapsed();
  const hydrated = useHydrated();
  // The 68 px rail is a desktop state only (globals.css: >= 900 px); below that the sidebar is the full drawer.
  const desktop = useMediaQuery("(min-width: 900px)");
  const rail = collapsed && desktop;
  const activeGroup = NAV_GROUPS.find((g) => g.items.some((e) => navActive(e, pathname)));
  const navGroups = useNavGroups("portal", activeGroup ? navGroupId(activeGroup.label) : null, pathname);

  // `[` toggles the rail, ignored while typing (input / textarea / select / contenteditable).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "[" || e.metaKey || e.ctrlKey || e.altKey) return;
      const t = e.target as HTMLElement | null;
      const tag = t?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || t?.isContentEditable) return;
      e.preventDefault();
      setCollapsed(!collapsed);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [collapsed, setCollapsed]);

  const counts = { pool: poolCount, catalog: catalogCount };

  return (
    <aside
      data-pshell="sidebar"
      data-testid="portal-sidebar"
      data-open={drawerOpen ? "true" : "false"}
      data-hydrated={hydrated ? "true" : undefined}
      aria-label="Portal navigation"
      style={{
        flexShrink: 0, background: "var(--aa-side-bg)", color: "var(--aa-side-text)",
        display: "flex", flexDirection: "column", gap: 22,
        overflowY: "auto", overflowX: "hidden", fontFamily: sans,
      }}
    >
      {/* Brand → Dashboard */}
      <div style={{ paddingBottom: 16, borderBottom: "1px solid rgba(255,255,255,0.07)" }}>
        <Link
          href={ROUTES.dashboard}
          onClick={onNavigate}
          data-pshell-row="true"
          aria-label="Adventure Asia Partner Portal — go to Dashboard"
          title="Adventure Asia — Dashboard"
          style={{ display: "flex", alignItems: "center", gap: 10, textDecoration: "none" }}
        >
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src={LOGO_SRC} alt="" width={38} height={24}
            style={{ width: 38, height: "auto", flexShrink: 0, display: "block" }} />
          <span data-pshell-label="true" style={{ minWidth: 0 }}>
            <span style={{ display: "block", fontFamily: serif, fontSize: 14, fontWeight: 500, color: "var(--aa-side-title)", letterSpacing: "-0.01em", lineHeight: 1.2 }}>
              Adventure Asia
            </span>
            <span style={{ display: "block", fontSize: 9.5, textTransform: "uppercase", letterSpacing: "0.18em", color: K.accent, fontWeight: 600, marginTop: 1 }}>
              Partner Portal
            </span>
          </span>
        </Link>
      </div>

      <nav aria-label="Primary" style={{ flex: 1, display: "flex", flexDirection: "column", gap: 16 }}>
        {NAV_GROUPS.map((group) => {
          const id = navGroupId(group.label);
          return (
            <NavGroup
              key={group.label}
              id={id}
              label={group.label !== "Overview" ? group.label : null}
              open={navGroups.isOpen(id)}
              onToggle={() => navGroups.toggle(id)}
              rail={rail}
              active={group.items.some((e) => navActive(e, pathname))}
              testId={`portal-nav-group-${group.label.toLowerCase()}`}
              headerStyle={groupTitleStyle}
              headerProps={PSHELL_LABEL}
            >
              {group.items.map((entry) => (
                <NavItem
                  key={entry.href}
                  entry={entry}
                  active={navActive(entry, pathname)}
                  count={entry.count ? (counts[entry.count] ?? undefined) : undefined}
                  onClick={onNavigate}
                />
              ))}
            </NavGroup>
          );
        })}
        <StudioApps rail={rail} open={navGroups.isOpen("apps")} onToggle={() => navGroups.toggle("apps")} />
      </nav>

      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <button
          data-pshell="collapse-btn"
          data-pshell-row="true"
          onClick={() => setCollapsed(!collapsed)}
          data-testid="portal-sidebar-collapse"
          title={collapsed ? "Expand sidebar ([)" : "Collapse sidebar ([)"}
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          style={{
            display: "flex", alignItems: "center", gap: 10, width: "100%",
            padding: "8px 10px", borderRadius: 7, border: "none",
            background: "transparent", color: "var(--aa-side-text2)", cursor: "pointer",
            fontSize: 12, fontFamily: sans,
          }}
        >
          {collapsed ? <ChevronsRight size={16} /> : <ChevronsLeft size={16} />}
          <span data-pshell-label="true">Collapse</span>
        </button>
      </div>
    </aside>
  );
}

const APP_ICON: Record<AppEntry["key"], React.ReactNode> = {
  content: null,
  crm: <Users size={16} />,
  planner: <MapIcon size={16} />,
};

/** The add-ons of the app switcher (Content, the current app, is the rest of this sidebar). */
function StudioApps({ rail, open, onToggle }: { rail: boolean; open: boolean; onToggle: () => void }) {
  const { data } = useQuery({ queryKey: APPS_KEY, queryFn: fetchApps, staleTime: 5 * 60_000, retry: false });
  const addOns = (data ?? []).filter((a) => a.key !== "content");
  if (addOns.length === 0) return null;
  return (
    <NavGroup
      id="apps"
      label="Apps"
      open={open}
      onToggle={onToggle}
      rail={rail}
      testId="portal-nav-group-apps"
      headerStyle={groupTitleStyle}
      headerProps={PSHELL_LABEL}
    >
      {addOns.map((app) => {
        const request = app.state === "request";
        return (
          <a
            key={app.key}
            href={app.href}
            data-pshell-row="true"
            data-testid={app.key === "crm" && !request ? "portal-crm-link" : `portal-app-${app.key}`}
            data-studio-app={app.state}
            title={request ? `Request ${app.label} from Adventure Asia` : `Open ${app.label}`}
            style={{
              display: "flex", alignItems: "center", gap: 10, width: "100%",
              padding: "8px 10px", borderRadius: 7, textDecoration: "none",
              fontSize: 13, fontWeight: 500, fontFamily: sans,
            }}
          >
            <span aria-hidden style={{ flexShrink: 0, opacity: 0.75, display: "flex" }}>{APP_ICON[app.key]}</span>
            <span data-pshell-label="true" style={{ flex: 1, minWidth: 0, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
              {request ? `${app.label}: request add-on` : `Open ${app.label}`}
            </span>
            <span data-pshell-label="true" aria-hidden style={{ display: "flex", opacity: 0.6 }}>
              {request ? <Plus size={12} /> : <ExternalLink size={12} />}
            </span>
          </a>
        );
      })}
    </NavGroup>
  );
}

// The kit NavGroupHeader look, with the portal's tighter 6 px under the title. The header carries
// data-pshell-label so the collapsed-rail CSS hides it before hydration too.
const groupTitleStyle: React.CSSProperties = { padding: "0 10px 6px" };
const PSHELL_LABEL = { "data-pshell-label": "true" };

function NavItem({ entry, active, count, onClick }: {
  entry: NavEntry; active: boolean; count?: number; onClick: () => void;
}) {
  const showCount = count != null && count > 0;
  const shown = showCount ? (count > 999 ? "999+" : count.toLocaleString()) : "";
  return (
    <Link
      href={entry.href}
      onClick={onClick}
      data-pshell-row="true"
      aria-current={active ? "page" : undefined}
      title={entry.label}
      style={{
        display: "flex", alignItems: "center", gap: 10, width: "100%",
        padding: "8px 10px", borderRadius: 7, textDecoration: "none",
        background: active ? alpha(K.accent, 12) : "transparent",
        color: active ? "var(--aa-on-solid)" : "var(--aa-side-text)",
        fontSize: 13, fontWeight: 500, fontFamily: sans, position: "relative",
        transition: "background .15s, color .15s",
      }}
    >
      {active && (
        <span aria-hidden style={{ position: "absolute", left: 0, top: 8, bottom: 8, width: 2, background: K.accent, borderRadius: "0 2px 2px 0" }} />
      )}
      <span aria-hidden style={{ flexShrink: 0, opacity: active ? 1 : 0.75, position: "relative", display: "flex" }}>
        {entry.icon}
        {showCount && (
          <span data-pshell-dot="true" style={{
            position: "absolute", top: -4, right: -6, width: 7, height: 7, borderRadius: 999,
            background: K.accent,
          }} />
        )}
      </span>
      <span data-pshell-label="true" style={{ flex: 1, minWidth: 0, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
        {entry.label}
      </span>
      {showCount && (
        <span data-pshell-label="true" aria-label={`${count} ${entry.count === "pool" ? "tours available" : "tours"}`}
          style={{ fontSize: 11, color: active ? K.accent : "var(--aa-side-text2)", fontVariantNumeric: "tabular-nums", flexShrink: 0 }}>
          {shown}
        </span>
      )}
    </Link>
  );
}
