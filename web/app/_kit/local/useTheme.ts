"use client";
// web/app/_kit/local/useTheme.ts
// The theme hooks of the CRM web (AA-994 S2a): the kit core hooks (../core/useTheme.ts) bound to this app's theme store
// (./theme.ts, key `cis_theme`). Pages read them through the barrel (`@/app/_kit`).

import { createThemeHooks } from "../core/useTheme";
import { themeStore } from "./theme";

export const { useThemeChoice, useResolvedTheme, useChartColors } = createThemeHooks(themeStore);
