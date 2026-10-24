// web/lib/planMessages.ts
// AA-943 — the strings the Stage 2 planning form (C-02, components/intake/PlanningForm.tsx) uses: only these go to the
// client page's browser, not the agency app's whole dictionary. The trip page's "Fill in from call" uses the same set.

import { dictionary, type Lang, type MessageKey } from "./i18n";

export const PLAN_PREFIXES = [
  "plan.",
  "tier.",
  "group.",
  "fitness.",
  "trek.",
  "interest.",
  "avoid.",
  "occasion.",
  "flex.",
  "decider.",
  "timeline.",
  "dietary.",
  "mobility.",
  "common.",
] as const;

export function planMessages(lang: Lang): Partial<Record<MessageKey, string>> {
  const all = dictionary(lang);
  return Object.fromEntries(Object.entries(all).filter(([k]) => PLAN_PREFIXES.some((p) => k.startsWith(p))));
}
