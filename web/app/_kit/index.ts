// web/app/_kit/index.ts
// The CRM web's barrel for the AA UI kit. Pages and components import "@/app/_kit" (server or client: every core
// module with a hook carries "use client"). Deep imports are allowed only in these cases (root ADR 0016
// decision 3; listed so that moving a file between core and local stays local):
//   - app/layout.tsx imports core/tokens.css (no PostCSS here), local/theme (the restore script) and
//     local/ThemeKeeper (re-applies the stored theme when React rebuilds the document).
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
// AA-966: the KPI tile (Reports and Home tiles).
export { KpiTile } from "./core/KpiTile";
// AA-952: the kit form controls (Field, Input, Select, Textarea, Checkbox, Radio) for new forms (lint no-raw-form-control).
export * from "./core/FormControls";
export * from "./core/csv";
export * from "./core/savedViews";
export { AiNotice } from "./core/AiNotice";
export { AI_NOTICE_MESSAGES, aiNoticeText, type AiNoticeVariant } from "./core/aiNotice.messages";
export { Providers } from "./core/Providers";
export {
  PageFrame, PageFrameHeader, PageFrameBody, AdminMain, AdminPaneHeader, AdminPaneBody,
} from "./core/PageFrame";
// core/useMediaQuery.ts joined the barrel in AA-994 S2b: the CIS core file is a client module since AA-CIS-App #1083
// (it was left out in S2a because many CRM server components import this barrel and Turbopack refused the build).
export { useMediaQuery, useIsPhone, PHONE_QUERY } from "./core/useMediaQuery";
export { useSidebarCollapsed } from "./core/useSidebar";
export { useHydrated } from "./core/useHydrated";
export type { ThemeChoice, ResolvedTheme } from "./core/theme";

// ── CRM-local ────────────────────────────────────────────────────────────────────────────────────────────────
export { useThemeChoice, useResolvedTheme, useChartColors } from "./local/useTheme";
