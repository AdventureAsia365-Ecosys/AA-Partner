// web/app/_kit/index.ts
// The CRM web's barrel for the AA UI kit. Pages and components import "@/app/_kit" (server or client: this barrel
// leaves out core/useMediaQuery.ts, see below). Deep imports are allowed only in these cases (root ADR 0016
// decision 3; listed so that moving a file between core and local stays local):
//   - app/layout.tsx imports core/tokens.css (no PostCSS here) and local/theme (the restore script).
//   - app/(crm)/layout.tsx imports local/CrmShell by path: the shell imports this barrel, so it is not re-exported.
//
// AA-994 S2a (root ADR 0016): ./core is a byte copy of AA-CIS-App frontend/app/_kit/core (full profile), written by
// the root tools/kit/kit-sync.sh and pinned by ../../kit.lock.json; Web Build checks it (integrity: core/check-kit-
// copy.mjs; provenance: api/scripts/check_kit_provenance.py). Never edit a file in ./core: change the kit in
// AA-CIS-App, then sync. ./local holds the CRM's own parts: the theme binding (key `cis_theme`) and the CRM shell
// (imported by path from app/(crm)/layout.tsx, not re-exported here, because the shell itself imports this barrel).

// ── kit core (byte copy of AA-CIS-App frontend/app/_kit/core) ───────────────────────────────────────────────
export * from "./core/tokens";
export { BRAND, FONT_DISPLAY, FONT_SANS, FONT_MONO, LOGO_SRC, SUPPORT_EMAIL, SITE_URL } from "./core/brand";
export * from "./core/primitives";
export * from "./core/Modal";
export * from "./core/Drawer";
export * from "./core/Toast";
export * from "./core/DataTable";
export * from "./core/csv";
export * from "./core/savedViews";
export { Providers } from "./core/Providers";
export {
  PageFrame, PageFrameHeader, PageFrameBody, AdminMain, AdminPaneHeader, AdminPaneBody,
} from "./core/PageFrame";
// Not re-exported: core/useMediaQuery.ts (useMediaQuery, useIsPhone, PHONE_QUERY). It calls useSyncExternalStore
// without a "use client" line, and many CRM server components import this barrel, so Turbopack refuses the build.
// The CRM does not use it; once the CIS core file carries "use client", it can join the barrel (AA-994 follow-up).
export { useSidebarCollapsed } from "./core/useSidebar";
export { useHydrated } from "./core/useHydrated";
export type { ThemeChoice, ResolvedTheme } from "./core/theme";

// ── CRM-local ────────────────────────────────────────────────────────────────────────────────────────────────
export { useThemeChoice, useResolvedTheme, useChartColors } from "./local/useTheme";
