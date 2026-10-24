// web/app/_kit/index.ts
// AA-935 — the CRM web's barrel for its VENDORED copy of the AA UI kit. Every other file in this folder (and
// app/_brand/tokens.ts) is a byte copy of AA-CIS-App frontend/app/_kit/<same name> (frontend/app/_brand/tokens.ts),
// checked against AA-CIS-App main by CI job "Vendored CIS copies" (api/scripts/check_vendored_copies.py). Never edit
// a copy here: change the kit in AA-CIS-App, then copy it byte for byte. This barrel is CRM-owned and exports only
// the files the CRM has.

export * from "./tokens";
export * from "./primitives";
export * from "./Toast";
export { useThemeChoice, useResolvedTheme } from "./useTheme";
export type { ThemeChoice, ResolvedTheme } from "./theme";
export { useHydrated } from "./useHydrated";
