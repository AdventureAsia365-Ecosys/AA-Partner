// web/lib/studio/brandMarkets.ts — AA-1080: the Brand Identity market note and the brief-import review notes.
//
// The CIS API returns, on GET /admin/brand-identity (`markets`), on the save and on the brief parse, the agency's
// markets as codes: the market its tour-page keywords come from (`seo_market`, the first supported market it
// declared) and one note per declared market without search data (`reason` "no_search_data", `uses` = the market its
// content uses instead). These helpers turn that into the lines the form shows, so the copy stays in the messages
// file and the logic is unit-tested (tests/studio/brandMarkets.test.ts).

export interface MarketNoteEntry { country: string; reason: string; uses: string }

export interface MarketSummary {
  countries: string[];
  supported?: string[];
  unsupported: string[];
  unknown: string[];
  seo_market?: string;
  defaulted?: boolean;
  notes?: MarketNoteEntry[];
}

/** Country names for the codes the API returns (the brand form's own vocabulary; other codes show as typed). */
const COUNTRY_NAMES: Record<string, string> = {
  US: "United States", UK: "United Kingdom", AU: "Australia", DE: "Germany", FR: "France", NL: "Netherlands",
  AE: "UAE", SA: "Saudi Arabia", SG: "Singapore", HK: "Hong Kong", CA: "Canada", NZ: "New Zealand",
};

export function countryName(code: string): string {
  return COUNTRY_NAMES[code] ?? code;
}

export interface MarketNoteLines {
  /** The market the tour-page keywords come from, as a name. */
  seoMarket: string;
  /** True when no declared market has search data (the US default is used). */
  defaulted: boolean;
  /** One entry per declared market without search data, with the market used instead. */
  unsupported: { country: string; uses: string; reason: string }[];
  unknown: string[];
}

/** The note lines for a market summary, or null when there is nothing to say (no markets at all). */
export function marketNoteLines(m: MarketSummary | null | undefined): MarketNoteLines | null {
  if (!m || (!m.countries?.length && !m.unknown?.length)) return null;
  const seo = m.seo_market ?? m.supported?.[0] ?? "US";
  const notes = m.notes ?? (m.unsupported ?? []).map(c => ({ country: c, reason: "no_search_data", uses: seo }));
  return {
    seoMarket: countryName(seo),
    defaulted: m.defaulted ?? !(m.supported?.length),
    unsupported: notes.map(n => ({ country: countryName(n.country), uses: countryName(n.uses), reason: n.reason })),
    unknown: m.unknown ?? [],
  };
}

/** Brief fields the import drafted rather than read (API `drafted`), as form labels, in form order. */
export const DRAFTED_FIELD_ORDER = ["default_cta", "system_prompt", "signature_words"] as const;

export function draftedFields(drafted: unknown): (typeof DRAFTED_FIELD_ORDER)[number][] {
  const got = new Set(Array.isArray(drafted) ? drafted.map(String) : []);
  return DRAFTED_FIELD_ORDER.filter(f => got.has(f));
}

/** Comma-separated words → a clean list (trimmed, no empties, case-insensitive duplicates dropped). */
export function wordList(value: string): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const w of value.split(",").map(x => x.trim()).filter(Boolean)) {
    const key = w.toLowerCase();
    if (!seen.has(key)) { seen.add(key); out.push(w); }
  }
  return out;
}
