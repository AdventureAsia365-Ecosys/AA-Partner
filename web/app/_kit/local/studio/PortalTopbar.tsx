"use client";
// web/app/_kit/local/studio/PortalTopbar.tsx
// AA-759 — the portal topbar (56 px, every portal page). Left: ☰ (below 900 px) + breadcrumb
// (<nav group> / <page>, kit Breadcrumb). Centre: the search pill that opens ⌘K (an icon below
// 768 px — search is no longer hidden on phones). Right: theme switch (light → dark → system),
// the notifications bell, the account menu (tenant, plan, settings, language, legal links, sign out).
// AA-1036 (R3): every label in the UI language (studio.messages.ts). The account menu carries the app's one language
// switch (components/LocaleSwitch.tsx, the CRM's: cookie aa_crm_locale, the page reloads in the chosen language).
// AA-1008 (S6f): every button is a kit button (IconButton for the icons, Pressable for the search pill and the account
// trigger, ListButton for the menu row); the responsive show / hide hooks sit on a wrapper span where the kit button
// sets its own display.

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { ChevronDown, LogOut, Menu, Monitor, Moon, Search, Settings, Sun, CreditCard } from "lucide-react";
import { signOutEverywhere } from "@/lib/studio/signOut";
import {
  Breadcrumb, IconButton, K, ListButton, Pressable, SkeletonBar, SkeletonStyle, alpha, sans, serif, useT, useThemeChoice,
  type ThemeChoice, type Translator,
} from "@/app/_kit";
import { LocaleSwitch } from "@/components/LocaleSwitch";
import { PRIVACY_PATH, TERMS_PATH } from "@/lib/studio/legal";
import NotificationsBell from "@/app/(studio)/portal/_components/NotificationsBell";
import type { ShellActivityView } from "@/app/(studio)/portal/_components/PortalShellContext";
import { ROUTES, pageGroup, pageTitle } from "./portalNav";
import { STUDIO_MESSAGES, type StudioKey } from "./studio.messages";
import { useStudioLocale } from "./StudioLang";

type T = Translator<typeof STUDIO_MESSAGES.en>;

export function PortalTopbar({ tenantName, planTier, activity, onOpenSearch, onOpenMenu, menuControls, menuExpanded }: {
  tenantName: string;
  planTier: string;
  activity: ShellActivityView[];
  onOpenSearch: () => void;
  onOpenMenu: () => void;
  /** The drawer's id and state (the kit ShellFrame), for the menu button's aria-controls / aria-expanded. */
  menuControls?: string;
  menuExpanded?: boolean;
}) {
  const t = useT(STUDIO_MESSAGES);
  const pathname = usePathname();
  const title = pageTitle(pathname);
  const isMac = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform);

  return (
    <header
      data-pshell="topbar"
      style={{
        height: 56, flexShrink: 0, background: K.card, borderBottom: `1px solid ${K.line}`,
        display: "flex", alignItems: "center", position: "sticky", top: 0, zIndex: 20,
        fontFamily: sans, minWidth: 0,
      }}
    >
      <span data-pshell="menu-btn">
        <IconButton variant="plain" size="lg" label={t("openMenu")} title="" onClick={onOpenMenu}
          aria-controls={menuControls} aria-expanded={menuExpanded}>
          <Menu size={18} />
        </IconButton>
      </span>

      <Breadcrumb root={t(pageGroup(pathname))} title={title ? t(title) : ""} fontSize={12.5} />

      {/* A trigger that looks like a search field: the kit Pressable (no look of its own) with the pill style. */}
      <Pressable
        data-pshell="search-pill"
        onClick={onOpenSearch}
        aria-label={t("searchAria")}
        style={{
          display: "flex", alignItems: "center", gap: 8, maxWidth: 380, flex: 1, minWidth: 0, width: "auto",
          margin: "0 auto", padding: "7px 12px", borderRadius: 999, border: `1px solid ${K.line}`,
          background: K.bg, color: K.muted, fontFamily: sans, fontSize: 13,
        }}
      >
        <Search size={15} style={{ flexShrink: 0 }} />
        <span style={{ flex: 1, textAlign: "left", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {t("searchPill")}
        </span>
        <kbd style={{ fontSize: 11, color: K.muted, border: `1px solid ${K.line}`, borderRadius: 6, padding: "1px 6px", background: K.card, flexShrink: 0, fontFamily: sans }}>
          {isMac ? "⌘K" : "Ctrl K"}
        </kbd>
      </Pressable>

      <span data-pshell="spacer" style={{ flex: 1 }} />

      <div style={{ display: "flex", alignItems: "center", gap: 4, flexShrink: 0 }}>
        <span data-pshell="search-icon">
          <IconButton variant="plain" size="lg" label={t("search")} title="" onClick={onOpenSearch}>
            <Search size={18} />
          </IconButton>
        </span>
        <ThemeToggle t={t} />
        <NotificationsBell activity={activity} />
        <AccountMenu t={t} tenantName={tenantName} planTier={planTier} />
      </div>
    </header>
  );
}

// ── Theme switch (same cycle as the admin topbar: light → dark → system) ─────
const THEME_OPTIONS: { key: ThemeChoice; label: StudioKey; icon: React.ReactNode }[] = [
  { key: "light", label: "themeLight", icon: <Sun size={16} /> },
  { key: "dark", label: "themeDark", icon: <Moon size={16} /> },
  { key: "system", label: "themeSystem", icon: <Monitor size={16} /> },
];

function ThemeToggle({ t }: { t: T }) {
  const [choice, setChoice] = useThemeChoice();
  const i = Math.max(0, THEME_OPTIONS.findIndex((o) => o.key === choice));
  const cur = THEME_OPTIONS[i];
  const next = THEME_OPTIONS[(i + 1) % THEME_OPTIONS.length];
  return (
    <IconButton
      variant="plain"
      size="lg"
      onClick={() => setChoice(next.key)}
      testId="portal-theme-toggle"
      title={t("themeTitle", t(cur.label), t(next.label))}
      label={t("themeAria", t(cur.label), t(next.label))}
      style={{ color: choice === "system" ? K.muted : K.accent }}
    >
      {cur.icon}
    </IconButton>
  );
}

// ── Account menu ─────────────────────────────────────────────────────────────
function AccountMenu({ t, tenantName, planTier }: { t: T; tenantName: string; planTier: string }) {
  const [open, setOpen] = useState(false);
  const pathname = usePathname();
  const locale = useStudioLocale();
  const ref = useRef<HTMLDivElement>(null);
  // Initials from the words that start with a letter or digit ("Atlas & Hearth" → "AH", not "A&").
  const initials = tenantName.split(/\s+/).filter((w) => /^[\p{L}\p{N}]/u.test(w)).map((w) => w[0])
    .join("").slice(0, 2).toUpperCase() || "P";
  const plan = planTier ? t("plan", `${planTier.charAt(0).toUpperCase()}${planTier.slice(1)}`) : "";

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    window.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey);
    return () => { window.removeEventListener("mousedown", onDown); window.removeEventListener("keydown", onKey); };
  }, [open]);

  async function logout() {
    // AA-427: the tenant cookies are httpOnly — cleared server-side. AA-979: a partner sign-in also ends the CRM
    // session on the same host (lib/signOut.ts).
    await signOutEverywhere();
  }

  return (
    <div ref={ref} style={{ position: "relative" }}>
      <Pressable
        display="inline"
        onClick={() => setOpen((v) => !v)}
        aria-label={t("accountMenu")}
        aria-expanded={open}
        aria-haspopup="menu"
        style={{
          gap: 8, padding: "3px 8px 3px 3px", borderRadius: 999,
          border: `1px solid ${K.line}`, background: K.card, fontFamily: sans,
        }}
      >
        {/* AA-760 — while /api/tenant/me loads (tenantName ""), a skeleton pill instead of the generic
            "P / Partner" that flashed a wrong identity. */}
        {tenantName ? (
          <>
            <span aria-hidden style={{
              width: 28, height: 28, borderRadius: "50%", background: K.accent, display: "grid", placeItems: "center",
              color: K.onAccent, fontWeight: 700, fontSize: 11.5,
            }}>{initials}</span>
            <span data-pshell="account-name" style={{ fontSize: 13, fontWeight: 500, color: K.ink, maxWidth: 140, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {tenantName}
            </span>
          </>
        ) : (
          <>
            <SkeletonStyle />
            <SkeletonBar w={28} h={28} r={14} />
            <span data-pshell="account-name"><SkeletonBar w={84} h={12} /></span>
          </>
        )}
        <ChevronDown size={14} style={{ color: K.muted2 }} />
      </Pressable>
      {open && (
        <div role="menu" aria-label={t("account")} style={{
          position: "absolute", top: "100%", right: 0, marginTop: 6, width: 220, zIndex: 100,
          background: K.card, border: `1px solid ${K.line}`, borderRadius: 10,
          boxShadow: `0 12px 32px ${alpha(K.ink, 18)}`, overflow: "hidden",
        }}>
          <div style={{ padding: "10px 14px", borderBottom: `1px solid ${K.line}` }}>
            <div style={{ fontSize: 13, fontWeight: 600, color: K.ink, fontFamily: serif, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{tenantName}</div>
            {plan && <div style={{ fontSize: 11, color: K.muted }}>{plan}</div>}
          </div>
          <MenuLink href={ROUTES.settings} icon={<Settings size={15} />} label={t("navSettings")} onClick={() => setOpen(false)} />
          <MenuLink href={ROUTES.billing} icon={<CreditCard size={15} />} label={t("navBilling")} onClick={() => setOpen(false)} />
          <ListButton role="menuitem" onClick={logout} style={{ gap: 10, padding: "9px 14px", color: K.ink3, fontSize: 13 }}>
            <span style={{ color: K.muted2, display: "flex" }}><LogOut size={15} /></span>
            {t("signOut")}
          </ListButton>
          <div data-testid="portal-locale" style={{ padding: "8px 14px", borderTop: `1px solid ${K.line}` }}>
            <LocaleSwitch locale={locale} label={t("language")} next={pathname} compact example={t("dateExample")} />
          </div>
          {/* AA-885/AA-886 — the portal's draft legal pages (moved here from the old sidebar footer). */}
          <div data-testid="portal-legal-links" style={{ display: "flex", gap: 14, padding: "8px 14px 10px", borderTop: `1px solid ${K.line}`, fontSize: 11 }}>
            <a href={PRIVACY_PATH} target="_blank" rel="noopener noreferrer" style={{ color: K.muted, textDecoration: "none" }}>{t("privacy")}</a>
            <a href={TERMS_PATH} target="_blank" rel="noopener noreferrer" style={{ color: K.muted, textDecoration: "none" }}>{t("terms")}</a>
          </div>
        </div>
      )}
    </div>
  );
}

const menuRow: React.CSSProperties = {
  display: "flex", alignItems: "center", gap: 10, width: "100%", padding: "9px 14px",
  border: "none", background: "transparent", cursor: "pointer", color: K.ink3,
  fontSize: 13, fontFamily: sans, textAlign: "left", textDecoration: "none", boxSizing: "border-box",
};

function MenuLink({ href, icon, label, onClick }: { href: string; icon: React.ReactNode; label: string; onClick: () => void }) {
  return (
    <Link href={href} role="menuitem" onClick={onClick} style={menuRow}>
      <span style={{ color: K.muted2, display: "flex" }}>{icon}</span>
      {label}
    </Link>
  );
}
