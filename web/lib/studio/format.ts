// web/lib/studio/format.ts
// AA-1036 (R3, AA-1007 leftover): the pure helpers of the Content studio that lived in its legacy primitive layer
// (app/(studio)/portal/_components/ui.tsx): JSON parsing of API fields, counting, and the date / money / month
// formats. Moved here so a component that only needs a helper no longer imports the legacy layer (aa-kit/no-legacy-kit).
// Every format takes the UI language ("en" by default, "vi" when the person picked Vietnamese; lib/i18n.ts). No React.

import { formatDate, formatDateTime } from "@/app/_kit/core/tokens";

export type UiLang = "en" | "vi";

// ── JSON fields ─────────────────────────────────────────────────────────────────────────────────────────────────

export function parseJSON(raw: unknown): unknown {
  if (raw == null) return null;
  try {
    let v = typeof raw === "string" ? JSON.parse(raw) : raw;
    if (typeof v === "string") v = JSON.parse(v);
    return v;
  } catch {
    return null;
  }
}

export function parseHighlights(raw: unknown): string[] {
  const v = parseJSON(raw);
  return Array.isArray(v) ? (v as string[]) : [];
}

export function parseContent(raw: unknown): Record<string, unknown> | null {
  const v = parseJSON(raw);
  if (v && typeof v === "object" && !Array.isArray(v)) return v as Record<string, unknown>;
  return null;
}

// AA-566 Part A — GET /v1/tours/my-versions returns one row per VERSION (gold_aa_internal.tenant_tour_versions), not
// one per tour. The sidebar badge and the My Tours heading both count unique tours with this one function.
export function countUniqueTours(list: { published_tour_id?: string | null }[]): number {
  return new Set(list.filter((v) => v.published_tour_id).map((v) => v.published_tour_id)).size;
}

// ── Formats ─────────────────────────────────────────────────────────────────────────────────────────────────────

/** AA-759 — the kit date format ("9 Oct 2026" / "9 thg 10, 2026"). */
export function fmtDate(iso: string | null | undefined, lang: UiLang = "en"): string {
  return formatDate(iso, lang);
}

/** The kit date-time format ("9 Oct 2026, 14:05"). */
export function fmtDateTime(iso: string | null | undefined, lang: UiLang = "en"): string {
  return formatDateTime(iso, lang);
}

/** AA-760 — a billing month key ("2026-10") for people: "October 2026" / "tháng 10 năm 2026". Anything else passes
 *  through. */
export function fmtBillingMonth(month: string | null | undefined, lang: UiLang = "en"): string {
  if (!month) return "—";
  const m = /^(\d{4})-(\d{2})$/.exec(month);
  if (!m) return month;
  return new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, 1))
    .toLocaleDateString(lang === "vi" ? "vi-VN" : "en-GB", { month: "long", year: "numeric", timeZone: "UTC" });
}

/** AA-760 — whole US dollars with a thousands separator ("$1,225"). The same in both languages: the price is in USD. */
export function fmtUsd(n: number): string {
  return n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
}

/** A count with the thousands separator of the UI language ("1,234" / "1.234"). */
export function fmtCount(n: number, lang: UiLang = "en"): string {
  return n.toLocaleString(lang === "vi" ? "vi-VN" : "en-US");
}

export { fmtDuration } from "./tours";

// ── Badges ──────────────────────────────────────────────────────────────────────────────────────────────────────

export type BadgeVariant = "success" | "warning" | "error" | "info" | "gold" | "default";

/** The kit Badge tone of a studio badge variant (kit primitives `Tone`). */
export function badgeTone(v: BadgeVariant): "success" | "warning" | "danger" | "info" | "accent" | "neutral" {
  return ({ success: "success", warning: "warning", error: "danger", info: "info", gold: "accent", default: "neutral" } as const)[v];
}
