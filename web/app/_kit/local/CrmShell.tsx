"use client";
// web/app/_kit/local/CrmShell.tsx
// AA-935 — the agency shell (design v3 §2.2 / §2.3, mockup A-00): [sidebar | topbar over main]. The sidebar mirrors
// the portal's (ink ground, gold active rule): agency card on top, HOME, CRM, MONEY, GROW, the CONTENT STUDIO
// cross-link to the portal (Fix C10), Settings and Help. Below 900 px it is an off-canvas drawer (CSS in
// app/globals.css, data-crm hooks). The shell owns only the drawer state; everything else comes from the server.
// S264: the titled groups (CRM, Money, Grow, Content studio) fold through the kit NavGroup + useNavGroups (core,
// scope "crm-web"): folded unless the user opened them, the group of the current page open on arrival. Home and
// Settings have no title and stay open. The CRM sidebar has no icon rail (only the phone drawer), so `rail` is unset.
// AA-1036 (R3 slice 1, root ADR 0019 d7): the Content studio group is the app switcher ("Apps"): the Content studio
// when the person may open it, and the add-ons the CRM cannot open yet with "Request add-on" (lib/apps.ts). Links
// between apps stay plain <a> (a full page load).

import {
  BookOpen,
  CalendarDays,
  ChartColumn,
  CircleCheck,
  CircleHelp,
  ExternalLink,
  House,
  Inbox,
  Kanban,
  LogOut,
  Map as MapIcon,
  Megaphone,
  Menu,
  Moon,
  Palette,
  Percent,
  Plane,
  Plus,
  Search,
  Settings,
  Sun,
  SunMoon,
  Ticket,
  Truck,
  Users,
  Wallet,
  X,
  Zap,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { K, NavGroup, ToastProvider, useNavGroups, useThemeChoice, type ThemeChoice } from "@/app/_kit";
import type { DateLocale } from "@/lib/dates";
import { CRM_TERMS_PATH } from "@/lib/legal";
import type { MessageKey } from "@/lib/i18n";
import { GROUP_LABEL, GROUP_ORDER, visibleSections, type IconName, type Role, type Section } from "@/lib/nav";
import { BellMenu } from "@/components/BellMenu";
import { LocaleSwitch } from "@/components/LocaleSwitch";

type Dict = Record<MessageKey, string>;

const ICONS: Record<IconName, React.ComponentType<{ size?: number; "aria-hidden"?: boolean }>> = {
  home: House,
  kanban: Kanban,
  users: Users,
  inbox: Inbox,
  check: CircleCheck,
  plane: Plane,
  calendar: CalendarDays,
  wallet: Wallet,
  ticket: Ticket,
  percent: Percent,
  chart: ChartColumn,
  megaphone: Megaphone,
  truck: Truck,
  book: BookOpen,
  zap: Zap,
  studio: Palette,
  settings: Settings,
  help: CircleHelp,
};

export interface ShellProps {
  dict: Dict;
  role: Role;
  agencyName: string;
  agencyCount: number;
  contentStudioHref: string | null;
  /** AA-1036: the "Request add-on" link of the Trip planner (lib/apps.ts requestHref). */
  plannerRequestHref: string;
  locale: DateLocale;
  /** Dev / test: every agency is synthetic (ADR 0010 decision 3), so the agency card says so. */
  syntheticBadge: boolean;
  children: React.ReactNode;
}

function isActive(section: Section, pathname: string): boolean {
  // AA-1036: the CRM home is /home, and "/" too while it still serves the CRM home (WEB_CONTENT_HOME off).
  if (section.key === "home") return pathname === "/" || pathname === section.path;
  return pathname === section.path || pathname.startsWith(`${section.path}/`);
}

function NavLink({ section, dict, pathname, onNavigate }: { section: Section; dict: Dict; pathname: string; onNavigate: () => void }) {
  const Icon = ICONS[section.icon];
  return (
    <Link
      href={section.path}
      data-crm="nav-item"
      aria-current={isActive(section, pathname) ? "page" : undefined}
      onClick={onNavigate}
    >
      <Icon size={16} aria-hidden />
      <span>{dict[section.label]}</span>
    </Link>
  );
}

// The kit NavGroupHeader look, aligned with the CRM nav items (20 px inset, 14 px above the title).
const groupHeaderStyle: React.CSSProperties = { padding: "14px 20px 4px", fontSize: 10, letterSpacing: "0.12em" };

const THEME_NEXT: Record<ThemeChoice, ThemeChoice> = { system: "light", light: "dark", dark: "system" };

function ThemeButton({ label }: { label: string }) {
  const [choice, setChoice] = useThemeChoice();
  const Icon = choice === "light" ? Sun : choice === "dark" ? Moon : SunMoon;
  return (
    <button
      type="button"
      onClick={() => setChoice(THEME_NEXT[choice])}
      title={`${label}: ${choice}`}
      aria-label={`${label}: ${choice}`}
      style={{ ...iconButton, display: "inline-flex" }}
    >
      <Icon size={16} />
    </button>
  );
}

// No `display` here: the menu buttons are shown / hidden by CSS (app/globals.css), and an inline display would win.
const iconButton: React.CSSProperties = {
  alignItems: "center",
  justifyContent: "center",
  width: 34,
  height: 34,
  borderRadius: 8,
  border: `1px solid ${K.line}`,
  background: K.card,
  color: K.ink3,
  cursor: "pointer",
};

export function CrmShell({
  dict,
  role,
  agencyName,
  agencyCount,
  contentStudioHref,
  plannerRequestHref,
  locale,
  syntheticBadge,
  children,
}: ShellProps) {
  const pathname = usePathname() ?? "/";
  const [open, setOpen] = useState(false);
  const sections = visibleSections(role);
  const close = () => setOpen(false);
  const activeGroup = sections.find((s) => isActive(s, pathname))?.group ?? null;
  const navGroups = useNavGroups("crm-web", activeGroup, pathname);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  return (
    <ToastProvider>
      <div data-crm="frame" data-open={open ? "true" : "false"}>
        <nav data-crm="sidebar" aria-label={dict["app.name"]} id="crm-sidebar">
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "16px 18px 6px" }}>
            <span style={{ fontFamily: "var(--font-display, 'Fahkwang', serif)", fontSize: 18, color: K.accent }}>
              {dict["app.short"]}
            </span>
            <button
              type="button"
              data-crm="menu-button"
              onClick={close}
              aria-label={dict["shell.closeMenu"]}
              style={{ ...iconButton, background: "transparent", border: "none", color: "var(--aa-side-text)" }}
            >
              <X size={18} />
            </button>
          </div>
          <div
            data-testid="crm-agency"
            style={{
              margin: "8px 12px 6px",
              padding: "9px 12px",
              borderRadius: 8,
              background: "var(--aa-side-panel)",
              color: "var(--aa-side-title)",
              display: "grid",
              gap: 2,
            }}
          >
            <span style={{ fontWeight: 600, fontSize: 13, overflowWrap: "anywhere" }}>{agencyName}</span>
            <span style={{ fontSize: 11, color: "var(--aa-side-text2)" }}>
              {dict[`role.${role}` as MessageKey]}
              {syntheticBadge ? ` · ${dict["shell.synthetic"]}` : ""}
            </span>
            {agencyCount > 1 && (
              <Link href="/select-agency?manual=1" style={{ fontSize: 11, color: K.accent, textDecoration: "none" }}>
                {dict["shell.switchAgency"]}
              </Link>
            )}
          </div>

          {GROUP_ORDER.map((group) => {
            if (group === "studio") {
              return (
                <NavGroup
                  key={group}
                  id={group}
                  label={dict["nav.group.studio"]}
                  open={navGroups.isOpen(group)}
                  onToggle={() => navGroups.toggle(group)}
                  testId={`crm-nav-group-${group}`}
                  style={{ borderTop: "1px solid var(--aa-side-panel)", marginTop: 8 }}
                  headerStyle={groupHeaderStyle}
                  itemsStyle={{ gap: 0 }}
                >
                  {contentStudioHref && (
                    <a href={contentStudioHref} data-crm="nav-item" data-testid="crm-studio-link">
                      <Palette size={16} aria-hidden />
                      <span>{dict["nav.studio"]}</span>
                      <ExternalLink size={12} aria-hidden style={{ marginLeft: "auto", opacity: 0.7 }} />
                    </a>
                  )}
                  <a href={plannerRequestHref} data-crm="nav-item" data-testid="crm-app-planner" data-app-state="request">
                    <MapIcon size={16} aria-hidden />
                    <span>{dict["nav.planner.request"]}</span>
                    <Plus size={12} aria-hidden style={{ marginLeft: "auto", opacity: 0.7 }} />
                  </a>
                </NavGroup>
              );
            }
            const items = sections.filter((s) => s.group === group);
            if (items.length === 0) return null;
            const title = GROUP_LABEL[group];
            return (
              <NavGroup
                key={group}
                id={group}
                label={title ? dict[title] : null}
                open={navGroups.isOpen(group)}
                onToggle={() => navGroups.toggle(group)}
                active={group === activeGroup}
                testId={`crm-nav-group-${group}`}
                style={group === "settings" ? { marginTop: "auto", paddingBottom: 12 } : undefined}
                headerStyle={groupHeaderStyle}
                itemsStyle={{ gap: 0 }}
              >
                {items.map((s) => (
                  <NavLink key={s.key} section={s} dict={dict} pathname={pathname} onNavigate={close} />
                ))}
              </NavGroup>
            );
          })}
        </nav>
        <div data-crm="backdrop" onClick={close} aria-hidden="true" />

        <div data-crm="main-col">
          <header data-crm="topbar">
            <button
              type="button"
              data-crm="menu-button"
              onClick={() => setOpen(true)}
              aria-label={dict["shell.menu"]}
              aria-controls="crm-sidebar"
              aria-expanded={open}
              style={iconButton}
            >
              <Menu size={18} />
            </button>
            <div
              data-crm="search"
              title={dict["shell.searchSoon"]}
              style={{
                alignItems: "center",
                gap: 8,
                padding: "7px 12px",
                borderRadius: 8,
                border: `1px solid ${K.line}`,
                background: K.bg,
                color: K.muted2,
                fontSize: 13,
              }}
            >
              <Search size={15} aria-hidden />
              <span>{dict["shell.search"]}</span>
            </div>
            <span style={{ flex: 1 }} />
            <div data-crm="hide-phone">
              <LocaleSwitch locale={locale} label={dict["shell.language"]} next={pathname} compact />
            </div>
            <BellMenu dict={dict} buttonStyle={iconButton} />
            <ThemeButton label={dict["shell.theme"]} />
            <form action="/auth/sign-out" method="post">
              <button
                type="submit"
                title={dict["shell.signOut"]}
                aria-label={dict["shell.signOut"]}
                style={{ ...iconButton, display: "inline-flex" }}
              >
                <LogOut size={16} />
              </button>
            </form>
          </header>
          <main data-crm="main" id="main">
            {children}
          </main>
          {/* AA-1033: the terms of use (its AI section) from every agency page; /legal/terms, never /terms (lib/legal.ts). */}
          <footer data-crm="footer">
            <Link href={CRM_TERMS_PATH} data-testid="crm-footer-terms">
              {dict["terms.link"]}
            </Link>
          </footer>
        </div>
      </div>
    </ToastProvider>
  );
}
