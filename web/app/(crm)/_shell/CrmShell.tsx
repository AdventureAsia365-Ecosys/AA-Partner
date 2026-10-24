"use client";
// web/app/(crm)/_shell/CrmShell.tsx
// AA-935 — the agency shell (design v3 §2.2 / §2.3, mockup A-00): [sidebar | topbar over main]. The sidebar mirrors
// the portal's (ink ground, gold active rule): agency card on top, HOME, CRM, MONEY, GROW, the CONTENT STUDIO
// cross-link to the portal (Fix C10), Settings and Help. Below 900 px it is an off-canvas drawer (CSS in
// app/globals.css, data-crm hooks). The shell owns only the drawer state; everything else comes from the server.

import {
  BookOpen,
  ChartColumn,
  CircleCheck,
  CircleHelp,
  ExternalLink,
  House,
  Inbox,
  Kanban,
  LogOut,
  Megaphone,
  Menu,
  Moon,
  Palette,
  Percent,
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
import { K, ToastProvider, useThemeChoice, type ThemeChoice } from "@/app/_kit";
import type { DateLocale } from "@/lib/dates";
import type { MessageKey } from "@/lib/i18n";
import { GROUP_LABEL, GROUP_ORDER, visibleSections, type IconName, type Role, type Section } from "@/lib/nav";
import { LocaleSwitch } from "@/components/LocaleSwitch";

type Dict = Record<MessageKey, string>;

const ICONS: Record<IconName, React.ComponentType<{ size?: number; "aria-hidden"?: boolean }>> = {
  home: House,
  kanban: Kanban,
  users: Users,
  inbox: Inbox,
  check: CircleCheck,
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
  locale: DateLocale;
  /** Dev / test: every agency is synthetic (ADR 0010 decision 3), so the agency card says so. */
  syntheticBadge: boolean;
  children: React.ReactNode;
}

function isActive(section: Section, pathname: string): boolean {
  return section.path === "/" ? pathname === "/" : pathname === section.path || pathname.startsWith(`${section.path}/`);
}

function NavLink({ section, dict, pathname, onNavigate }: { section: Section; dict: Dict; pathname: string; onNavigate: () => void }) {
  const Icon = ICONS[section.icon];
  return (
    <Link
      href={section.path}
      data-crm="nav-item"
      data-card={section.card}
      aria-current={isActive(section, pathname) ? "page" : undefined}
      onClick={onNavigate}
    >
      <Icon size={16} aria-hidden />
      <span>{dict[section.label]}</span>
    </Link>
  );
}

function GroupTitle({ children }: { children: React.ReactNode }) {
  return (
    <div
      style={{
        fontSize: 10,
        fontWeight: 600,
        letterSpacing: "0.12em",
        textTransform: "uppercase",
        color: "var(--aa-side-text2)",
        padding: "14px 20px 4px",
      }}
    >
      {children}
    </div>
  );
}

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
  locale,
  syntheticBadge,
  children,
}: ShellProps) {
  const pathname = usePathname() ?? "/";
  const [open, setOpen] = useState(false);
  const sections = visibleSections(role);
  const close = () => setOpen(false);

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
              if (!contentStudioHref) return null;
              return (
                <div key={group} style={{ borderTop: "1px solid var(--aa-side-panel)", marginTop: 8 }}>
                  <GroupTitle>{dict["nav.group.studio"]}</GroupTitle>
                  <a href={contentStudioHref} data-crm="nav-item" data-testid="crm-studio-link">
                    <Palette size={16} aria-hidden />
                    <span>{dict["nav.studio"]}</span>
                    <ExternalLink size={12} aria-hidden style={{ marginLeft: "auto", opacity: 0.7 }} />
                  </a>
                </div>
              );
            }
            const items = sections.filter((s) => s.group === group);
            if (items.length === 0) return null;
            const title = GROUP_LABEL[group];
            return (
              <div key={group} style={group === "settings" ? { marginTop: "auto", paddingBottom: 12 } : undefined}>
                {title && <GroupTitle>{dict[title]}</GroupTitle>}
                {items.map((s) => (
                  <NavLink key={s.key} section={s} dict={dict} pathname={pathname} onNavigate={close} />
                ))}
              </div>
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
        </div>
      </div>
    </ToastProvider>
  );
}
