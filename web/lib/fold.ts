// web/lib/fold.ts
// AA-1129 (S283 audit P4 row 14): a long settings list shows its first rows and a "Show all n" / "Show fewer" toggle,
// so the page does not grow to thousands of pixels (Settings > Data and privacy: exports and data requests; Settings >
// Agreements: older versions). Nothing is capped: "Show all" opens every row. Edge-safe, unit-tested.

/** How many rows a folded list shows before "Show all" (a display choice, not a limit: every row stays one click away). */
export const FOLD_FIRST = 5;

/** The rows to show and how many are folded away (0: no toggle needed, or the list is open). */
export function fold<T>(items: readonly T[], open: boolean, first: number = FOLD_FIRST): { shown: T[]; hidden: number } {
  if (open || items.length <= first) return { shown: [...items], hidden: 0 };
  return { shown: items.slice(0, first), hidden: items.length - first };
}

/** Which toggle a folded list shows: "all" (rows are folded away), "fewer" (open and longer than `first`) or none. */
export function foldToggle(total: number, open: boolean, first: number = FOLD_FIRST): "all" | "fewer" | null {
  if (total <= first) return null;
  return open ? "fewer" : "all";
}
