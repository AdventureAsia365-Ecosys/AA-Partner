"use client";
// web/app/_kit/local/studio/PortalSidebar.tsx
// AA-759 — the portal sidebar: navy panel in both themes (--aa-side-* tokens), the nav grouped by
// workflow (portalNav.tsx NAV_GROUPS), a collapse rail (chevron button or the `[` key, ignored while
// typing; state in the vendored sidebarStore, restored before paint by the root layout script).
//
// Presentation is CSS-driven (studio.css `data-pshell` rules): the width and the collapsed rail (labels and group titles
// hidden, a dot instead of the count pill). The phone drawer (fixed, off-canvas) is the kit ShellFrame's wrapper around
// this sidebar (AA-1008 S6e). Nothing here sets width or position inline, so the CSS always wins.
//
// S264 — the titled groups (Tours, Brand, Create, Content, Account, Partner CRM) fold through the kit NavGroup +
// useNavGroups (core, scope "portal"): folded unless the user opened them, the group of the current page open on
// arrival. Overview has no title and stays open. The collapsed rail (desktop, >= 900 px) shows every item. The item
// counts (tours available, my tours) are sizes, not things to act on, so a folded header does not repeat them.

import { useEffect } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { Check, ChevronsLeft, ChevronsRight, Clock, ExternalLink, Map as MapIcon, Plus, Users } from "lucide-react";
import type { AppEntry } from "@/lib/apps";
import {
  K, NavGroup, Pressable, alpha, navGroupId, sans, serif, useHydrated, useMediaQuery, useNavGroups, useSidebarCollapsed,
  useT, type Translator,
} from "@/app/_kit";
import { LOGO_SRC } from "../../core/brand";
import { NAV_GROUPS, ROUTES, navActive, type NavEntry } from "./portalNav";
import { STUDIO_MESSAGES } from "./studio.messages";
import { STUDIO_APPS_KEY, fetchStudioApps } from "./studioApps";

type T = Translator<typeof STUDIO_MESSAGES.en>;

// AA-1036 (R3 slice 1, root ADR 0019 d7): the app switcher replaces the old "Open CRM" cross-link. Content is where the
// person is; an add-on the tenant holds opens (the CRM home, a full page load: plain <a>, so the studio's stylesheet
// never stays on a CRM page); any other add-on links to Account › Add-ons, labelled by its state (request, requested,
// on; AA-1036 R3: the request is stored in CIS and decided by Adventure Asia staff, no longer an e-mail). The entries come
// from /api/studio/apps (lib/apps.ts), which always answers 200. Outside NAV_GROUPS on purpose: other apps, so neither
// the ⌘K palette nor the phone bottom bar routes to them. Until it answers, nothing shows. The fetch and its key are
// shared with Account › Add-ons (studioApps.ts).

export function PortalSidebar({ drawerOpen, poolCount, catalogCount, onNavigate }: {
  drawerOpen: boolean;
  poolCount: number | null;
  catalogCount: number;
  onNavigate: () => void;
}) {
  const t = useT(STUDIO_MESSAGES);
  const pathname = usePathname();
  const [collapsed, setCollapsed] = useSidebarCollapsed();
  const hydrated = useHydrated();
  // The 68 px rail is a desktop state only (globals.css: >= 900 px); below that the sidebar is the full drawer.
  const desktop = useMediaQuery("(min-width: 900px)");
  const rail = collapsed && desktop;
  const activeGroup = NAV_GROUPS.find((g) => g.items.some((e) => navActive(e, pathname)));
  const navGroups = useNavGroups("portal", activeGroup ? navGroupId(activeGroup.id) : null, pathname);

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
      aria-label={t("sidebarAria")}
      style={{
        flexShrink: 0, background: K.sideBg, color: K.sideText,
        display: "flex", flexDirection: "column", gap: 22,
        overflowY: "auto", overflowX: "hidden", fontFamily: sans,
      }}
    >
      {/* Brand → Dashboard */}
      <div style={{ paddingBottom: 16, borderBottom: `1px solid ${alpha(K.sideText, 10)}` }}>
        <Link
          href={ROUTES.dashboard}
          onClick={onNavigate}
          data-pshell-row="true"
          aria-label={t("brandHomeAria")}
          title={t("brandHomeTitle")}
          style={{ display: "flex", alignItems: "center", gap: 10, textDecoration: "none" }}
        >
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src={LOGO_SRC} alt="" width={38} height={24}
            style={{ width: 38, height: "auto", flexShrink: 0, display: "block" }} />
          <span data-pshell-label="true" style={{ minWidth: 0 }}>
            <span style={{ display: "block", fontFamily: serif, fontSize: 14, fontWeight: 500, color: K.sideTitle, letterSpacing: "-0.01em", lineHeight: 1.2 }}>
              {t("brandName")}
            </span>
            <span style={{ display: "block", fontSize: 9.5, textTransform: "uppercase", letterSpacing: "0.18em", color: K.accent, fontWeight: 600, marginTop: 1 }}>
              {t("brandProduct")}
            </span>
          </span>
        </Link>
      </div>

      <nav aria-label={t("navPrimary")} style={{ flex: 1, display: "flex", flexDirection: "column", gap: 16 }}>
        {NAV_GROUPS.map((group) => {
          const id = navGroupId(group.id);
          return (
            <NavGroup
              key={group.id}
              id={id}
              label={group.id !== "Overview" ? t(group.label) : null}
              open={navGroups.isOpen(id)}
              onToggle={() => navGroups.toggle(id)}
              rail={rail}
              active={group.items.some((e) => navActive(e, pathname))}
              testId={`portal-nav-group-${group.id.toLowerCase()}`}
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
                  t={t}
                />
              ))}
            </NavGroup>
          );
        })}
        <StudioApps t={t} rail={rail} open={navGroups.isOpen("apps")} onToggle={() => navGroups.toggle("apps")} />
      </nav>

      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {/* A sidebar row (the kit Pressable: no look of its own, the kit focus ring); studio.css hides it below 900 px. */}
        <Pressable
          data-pshell="collapse-btn"
          data-pshell-row="true"
          onClick={() => setCollapsed(!collapsed)}
          testId="portal-sidebar-collapse"
          title={collapsed ? t("expandSidebarKey") : t("collapseSidebarKey")}
          aria-label={collapsed ? t("expandSidebar") : t("collapseSidebar")}
          style={{
            display: "flex", alignItems: "center", gap: 10,
            padding: "8px 10px", borderRadius: 7, color: K.sideText2, fontSize: 12, fontFamily: sans,
          }}
        >
          {collapsed ? <ChevronsRight size={16} /> : <ChevronsLeft size={16} />}
          <span data-pshell-label="true">{t("collapse")}</span>
        </Pressable>
      </div>
    </aside>
  );
}

const APP_ICON: Record<AppEntry["key"], React.ReactNode> = {
  content: null,
  crm: <Users size={16} />,
  planner: <MapIcon size={16} />,
};

/** The app's name in the UI language (lib/apps.ts labels are English). */
function appName(t: T, app: AppEntry): string {
  return app.key === "crm" ? t("appCrm") : app.key === "planner" ? t("appPlanner") : t("appContent");
}

/** The add-ons of the app switcher (Content, the current app, is the rest of this sidebar). */
function StudioApps({ t, rail, open, onToggle }: { t: T; rail: boolean; open: boolean; onToggle: () => void }) {
  const { data } = useQuery({ queryKey: STUDIO_APPS_KEY, queryFn: fetchStudioApps, staleTime: 5 * 60_000, retry: false });
  const addOns = (data?.apps ?? []).filter((a) => a.key !== "content");
  if (addOns.length === 0) return null;
  return (
    <NavGroup
      id="apps"
      label={t("groupApps")}
      open={open}
      onToggle={onToggle}
      rail={rail}
      testId="portal-nav-group-apps"
      headerStyle={groupTitleStyle}
      headerProps={PSHELL_LABEL}
    >
      {addOns.map((app) => {
        const opens = app.state === "open";
        const name = appName(t, app);
        const label = opens ? t("appOpen", name) : app.state === "requested" ? t("appRequested", name)
          : app.state === "active" ? t("appActive", name) : t("appRequest", name);
        return (
          <a
            key={app.key}
            href={app.href}
            data-pshell-row="true"
            data-testid={app.key === "crm" && opens ? "portal-crm-link" : `portal-app-${app.key}`}
            data-studio-app={app.state}
            title={app.state === "request" ? t("appRequestTitle", name) : label}
            style={{
              display: "flex", alignItems: "center", gap: 10, width: "100%",
              padding: "8px 10px", borderRadius: 7, textDecoration: "none",
              fontSize: 13, fontWeight: 500, fontFamily: sans,
            }}
          >
            <span aria-hidden style={{ flexShrink: 0, opacity: 0.75, display: "flex" }}>{APP_ICON[app.key]}</span>
            <span data-pshell-label="true" style={{ flex: 1, minWidth: 0, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
              {label}
            </span>
            <span data-pshell-label="true" aria-hidden style={{ display: "flex", opacity: 0.6 }}>
              {opens ? <ExternalLink size={12} /> : app.state === "request" ? <Plus size={12} />
                : app.state === "requested" ? <Clock size={12} /> : <Check size={12} />}
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

function NavItem({ entry, active, count, onClick, t }: {
  entry: NavEntry; active: boolean; count?: number; onClick: () => void; t: T;
}) {
  const label = t(entry.label);
  const showCount = count != null && count > 0;
  const shown = showCount ? (count > 999 ? "999+" : count.toLocaleString()) : "";
  return (
    <Link
      href={entry.href}
      onClick={onClick}
      data-pshell-row="true"
      aria-current={active ? "page" : undefined}
      title={label}
      style={{
        display: "flex", alignItems: "center", gap: 10, width: "100%",
        padding: "8px 10px", borderRadius: 7, textDecoration: "none",
        background: active ? alpha(K.accent, 12) : "transparent",
        color: active ? K.onSolid : K.sideText,
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
        {label}
      </span>
      {showCount && (
        <span data-pshell-label="true" aria-label={entry.count === "pool" ? t("toursAvailable", count) : t("toursCount", count)}
          style={{ fontSize: 11, color: active ? K.accent : K.sideText2, fontVariantNumeric: "tabular-nums", flexShrink: 0 }}>
          {shown}
        </span>
      )}
    </Link>
  );
}
