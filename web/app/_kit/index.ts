// web/app/_kit/index.ts
// The CRM web's barrel for the AA UI kit. Pages and components import "@/app/_kit" (server or client: every core
// module with a hook carries "use client"). Deep imports are allowed only in these cases (root ADR 0016
// decision 3; listed so that moving a file between core and local stays local):
//   - app/layout.tsx imports core/tokens.css (no PostCSS here), local/theme (the restore script) and
//     local/ThemeKeeper (re-applies the stored theme when React rebuilds the document).
//   - app/(crm)/layout.tsx imports local/CrmShell by path: the shell imports this barrel, so it is not re-exported.
//   - AA-1036: the Content studio's shell (local/studio/: PortalShell, SessionWatch, portalNav, useShellData) is
//     imported by path for the same reason; studio pages import ROUTES / useCatalog / MY_VERSIONS_KEY from there.
//   - AA-1036 (R3, studio EN/VI): pure modules (lib/studio/*.ts, the *.messages.ts catalogues read by server pages,
//     app/(studio)/_legal) import core/i18n.ts and core/tokens.ts by path: no React, safe on the server, and no
//     "use client" module pulled in through this barrel.
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
// AA-1036 (R3): the kit card region, first used by the Content studio in place of its legacy Card.
export { Section } from "./core/Section";
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
export { useSidebarCollapsed, useNavGroups } from "./core/useSidebar";
// AA-1036: the Content studio's sidebar groups (moved in from the studio app's barrel).
export { navGroupId } from "./core/sidebarStore";
export { NavGroup } from "./core/NavGroup";
export { useHydrated } from "./core/useHydrated";
// AA-1036 (R3): the kit UI-language catalogue (core/i18n.ts) and its React bindings (core/useI18n.ts), first used by
// the Content studio (message files `*.messages.ts` next to its components). The language itself is this app's one
// setting (cookie aa_crm_locale, lib/i18n.ts); the studio's provider is local/studio/StudioLang.tsx.
export {
  defineMessages, translator, missingTranslations, localized, LANGS, DEFAULT_LANG,
  type Lang as UiLang, type Messages, type Translator, type Catalog, type LangStore,
} from "./core/i18n";
export { I18nProvider, useT, useLang, useSetLang, useLocalized, useFormat } from "./core/useI18n";
export type { ThemeChoice, ResolvedTheme } from "./core/theme";

// ── CRM-local ────────────────────────────────────────────────────────────────────────────────────────────────
export { useThemeChoice, useResolvedTheme, useChartColors } from "./local/useTheme";
// AA-1036 (R3 slice 1): the Content studio's fetch helpers for its BFF (/api/tenant/*), moved in with the studio
// (./local/studio/: its shell too, imported by path from app/(studio)/portal/layout.tsx: the shell imports this barrel).
export * from "./local/studio/api";
