// portal/app/_kit/local/theme.ts
// The theme store of the tenant portal (AA-994 S2a, root ADR 0016 decision 4): the kit core store (../core/theme.ts) bound to
// the key `cis_theme`, the key this app has always used (localStorage is per origin, so it never meets CIS admin's).
// app/layout.tsx puts `themeStore.restoreScript` in <head>, so the key is read before first paint; ./useTheme.ts
// binds the hooks.
// No "use client": nothing here touches React, and a server module may read `themeStore.restoreScript`.

import { createThemeStore } from "../core/theme";

export type { ResolvedTheme, ThemeChoice } from "../core/theme";

export const THEME_KEY = "cis_theme";

export const themeStore = createThemeStore({ storageKey: THEME_KEY });
