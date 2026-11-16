// web/lib/search.ts
// AA-963 (CRM-32, design v3 §2.3 A-00, R35) — the global search of the agency app: the shapes the API answers
// (POST /crm/v1/search, GET /crm/v1/search/config), where each result opens, the accent fold of the Content pieces
// filter and the messages of the refusals. The API decides what a member may find (role matrix + feature keys) and
// how (clients through blind indexes only); this file only shows it.

import { directoryHref } from "./clients";

export const SEARCH_KINDS = [
  "client",
  "trip",
  "booking",
  "supplier",
  "service",
  "message_template",
  "workflow",
  "inclusion",
  "tag",
  "whatsapp_template",
] as const;
export type SearchKind = (typeof SEARCH_KINDS)[number];

export interface SearchConfig {
  enabled: boolean;
  kinds: SearchKind[];
  query_max_chars: number;
  min_text_chars: number;
  results_per_kind: number;
}

export interface SearchItem {
  id: string;
  label: string;
  meta: Record<string, string | number | boolean | null>;
}

export interface SearchGroup {
  kind: SearchKind;
  items: SearchItem[];
}

export interface SearchResult {
  mode: "email" | "phone" | "text" | "too_short";
  kinds: SearchKind[];
  groups: SearchGroup[];
}

/** The API's refusals the palette explains (any other failure is the generic error). */
export const REFUSALS = ["partial_contact_search", "partial_search_refused", "query_too_long", "search_not_enabled"] as const;
export type Refusal = (typeof REFUSALS)[number];

export function refusalOf(detail: string): Refusal | null {
  return (REFUSALS as readonly string[]).includes(detail) ? (detail as Refusal) : null;
}

/** The config of an agency where search is off (also what a failed config read becomes: fail closed). */
export const SEARCH_OFF: SearchConfig = { enabled: false, kinds: [], query_max_chars: 0, min_text_chars: 0, results_per_kind: 0 };

export function parseConfig(data: unknown): SearchConfig {
  const d = data as Partial<SearchConfig> | null;
  if (!d || d.enabled !== true || !Array.isArray(d.kinds)) return SEARCH_OFF;
  return {
    enabled: true,
    kinds: d.kinds.filter((k): k is SearchKind => (SEARCH_KINDS as readonly string[]).includes(k)),
    query_max_chars: Number(d.query_max_chars) || 0,
    min_text_chars: Number(d.min_text_chars) || 0,
    results_per_kind: Number(d.results_per_kind) || 0,
  };
}

/** Where one result opens (an agency page; never a URL that carries the term). */
export function hrefFor(kind: SearchKind, item: SearchItem): string {
  const id = encodeURIComponent(item.id);
  switch (kind) {
    case "client":
      return `/clients/${id}`;
    case "trip":
      return `/pipeline/${id}`;
    case "booking":
      return typeof item.meta.trip_id === "string" ? `/pipeline/${encodeURIComponent(item.meta.trip_id)}` : "/pipeline";
    case "supplier":
      return "/suppliers";
    case "service":
      return "/services";
    case "message_template":
      return "/settings/templates";
    case "workflow":
      return "/automations";
    case "inclusion":
      return "/settings/inclusions";
    case "tag":
      return item.meta.applies_to === "client" && typeof item.meta.code === "string"
        ? directoryHref({ tag: item.meta.code })
        : "/settings/tags";
    case "whatsapp_template":
      return "/settings/channels";
  }
}

/**
 * The accent fold of the API (api/app/search/fold.py): NFKD, combining marks removed, đ/Đ ø/Ø ł/Ł -> d o l,
 * ß -> ss, lower-cased. "Đà Lạt" and "Da Lat" fold the same.
 */
export function foldText(text: string): string {
  return text
    .replace(/[đĐ]/g, "d")
    .replace(/[øØ]/g, "o")
    .replace(/[łŁ]/g, "l")
    .replace(/[ßẞ]/g, "ss")
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase();
}

/** A Content piece as the palette needs it (from the studio's GET /v1/content-writing/reviews). */
export interface ContentPieceHit {
  pieceId: string;
  title: string;
  tour: string | null;
}

/** The person's Content pieces whose title or tour contains the term (folded), at most `limit`. Pieces hold no
 * client data (they are the agency's marketing texts), so a substring match is allowed here. */
export function matchContentPieces(pieces: ContentPieceHit[], term: string, limit: number): ContentPieceHit[] {
  const needle = foldText(term.trim());
  if (!needle || limit <= 0) return [];
  return pieces.filter((p) => [p.title, p.tour ?? ""].some((s) => foldText(s).includes(needle))).slice(0, limit);
}

/** Where a Content piece opens: the studio's My content with the piece (a plain link: another app of the host). */
export function contentPieceHref(pieceId: string): string {
  return `/portal/content?piece=${encodeURIComponent(pieceId)}`;
}

/** Whether the palette should ask the API yet (it never sends a term shorter than the platform minimum). */
export function shouldSearch(term: string, cfg: SearchConfig): boolean {
  const t = term.trim();
  if (!cfg.enabled || !t) return false;
  if (cfg.query_max_chars > 0 && t.length > cfg.query_max_chars) return true; // the API answers query_too_long
  return t.includes("@") || t.startsWith("+") || t.length >= Math.max(1, cfg.min_text_chars);
}

/** One row of the palette. `external`: another app of the host (the studio), opened with a full page load. */
export interface PaletteRow {
  key: string;
  label: string;
  sub: string | null;
  href: string;
  external: boolean;
}

export interface PaletteGroup {
  kind: SearchKind | "content";
  rows: PaletteRow[];
}

/** The palette's groups, in the API's kind order, then the Content pieces. Pure. */
export function paletteGroups(result: SearchResult | null, content: ContentPieceHit[], archivedLabel: string): PaletteGroup[] {
  const groups: PaletteGroup[] = (result?.groups ?? [])
    .filter((g) => (SEARCH_KINDS as readonly string[]).includes(g.kind) && g.items.length > 0)
    .map((g) => ({
      kind: g.kind,
      rows: g.items.map((item) => ({
        key: `${g.kind}:${item.id}`,
        label: item.label,
        sub: subOf(g.kind, item, archivedLabel),
        href: hrefFor(g.kind, item),
        external: false,
      })),
    }));
  if (content.length > 0) {
    groups.push({
      kind: "content",
      rows: content.map((p) => ({
        key: `content:${p.pieceId}`,
        label: p.title,
        sub: p.tour,
        href: contentPieceHref(p.pieceId),
        external: true,
      })),
    });
  }
  return groups;
}

function subOf(kind: SearchKind, item: SearchItem, archivedLabel: string): string | null {
  if (item.meta.archived === true) return archivedLabel;
  if (kind === "booking" && typeof item.meta.trip_title === "string") return item.meta.trip_title;
  if (kind === "inclusion" && typeof item.meta.text_vi === "string") return item.meta.text_vi;
  return null;
}

/** One library of the Library page (R9): its words and where it opens. `external`: the studio (a full page load). */
export interface LibraryEntry {
  key: "templates" | "workflows" | "inclusions" | "content";
  href: string;
  external: boolean;
}

/** The Library page's entries: the three CRM libraries, and the Content studio's pieces only for a person CIS lists
 * in this agency's studio (`contentHref` is the studio link the layout keeps for them, else null). Pure. */
export function libraryEntries(contentHref: string | null): LibraryEntry[] {
  const out: LibraryEntry[] = [
    { key: "templates", href: "/settings/templates", external: false },
    { key: "workflows", href: "/automations", external: false },
    { key: "inclusions", href: "/settings/inclusions", external: false },
  ];
  if (contentHref) out.push({ key: "content", href: "/portal/content", external: true });
  return out;
}
