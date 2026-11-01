// portal/app/_kit/index.ts
// The tenant portal's barrel for the AA UI kit. Pages and components import "@/app/_kit". Deep imports are allowed
// only in these cases (root ADR 0016 decision 3; listed so that moving a file between core and local stays local):
//   - Server modules (app/layout.tsx, _legal/LegalPage.tsx, privacy, terms, portal-access/*) import the server-safe
//     core files by path (core/brand, core/Providers) and local/theme (restore script). That rule came from
//     core/useMediaQuery.ts lacking "use client"; the CIS core file has the line since AA-CIS-App #1083 (synced in
//     AA-994 S2b), so moving these imports to the barrel is now possible but not required.
//   - The portal shell's own parts come from ./local by path: layouts import PortalShell / SessionWatch, and pages
//     import the shell-owned values ROUTES (local/portalNav), useCatalog and MY_VERSIONS_KEY (local/useShellData).
//     They are not re-exported here: the shell imports this barrel, and useShellData imports
//     app/portal/_components/ui.tsx, which does too.
//
// AA-994 S2a (root ADR 0016): ./core is a byte copy of AA-CIS-App frontend/app/_kit/core (full profile), written by
// the root tools/kit/kit-sync.sh and pinned by ../../kit.lock.json; Portal Build checks it (integrity: core/check-kit-
// copy.mjs; provenance: api/scripts/check_kit_provenance.py). Never edit a file in ./core: change the kit in
// AA-CIS-App, then sync. ./local holds the portal's own parts: the API helpers (api.ts, from the CIS-local file of the
// same name), the theme binding (key `cis_theme`) and the portal shell (AA-759; imported by path from layout.tsx,
// not re-exported here, because the shell itself imports this barrel).

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
export { useMediaQuery, useIsPhone, PHONE_QUERY } from "./core/useMediaQuery";
export { useSidebarCollapsed } from "./core/useSidebar";
export { useHydrated } from "./core/useHydrated";
export type { ThemeChoice, ResolvedTheme } from "./core/theme";
export { AiNotice } from "./core/AiNotice";
export { AI_NOTICE_MESSAGES, aiNoticeText, type AiNoticeVariant } from "./core/aiNotice.messages";

// ── portal-local ─────────────────────────────────────────────────────────────────────────────────────────────
export * from "./local/api";
export { useThemeChoice, useResolvedTheme, useChartColors } from "./local/useTheme";
