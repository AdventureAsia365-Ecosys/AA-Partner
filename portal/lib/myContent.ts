// portal/lib/myContent.ts
// AA-669 — My Content table: the pure rules behind the columns, filters, views and bulk actions. No React and no
// imports, so `node --test` runs lib/myContent.test.ts directly (the AA-761 pattern). Shapes follow GET
// /v1/content-writing/reviews (CIS fetch_review_list: tenant-safe, no gate data); `published_at` / `published_url`
// come from the request's live post (CIS AA-669: any piece of the request, so a rewrite of a published piece keeps
// the row published) and are optional, so this works before that CIS change ships.

/** The fields of a review item these rules read (a subset of reviewListApi.ReviewItem). */
export interface ContentRow {
  piece_id: string;
  request_id: string;
  channel: string;
  ready_state: "ready" | "in_progress" | "not_ready" | string;
  content_text: string | null;
  goal: { key: string; label: string } | null;
  angle: { name: string; formula_fit?: string; best_final_style?: string } | null;
  atom: { text: string; activity_type: string | null } | null;
  tour: { name: string; destination: string } | null;
  route_hub_name: string | null;
  route_segment_count: number | null;
  created_at: string | null;
  published_at?: string | null;
  published_url?: string | null;
  /** The piece whose post is live; differs from piece_id when the post shows an earlier version of the text. */
  published_piece_id?: string | null;
  served_status?: "served" | "paused" | "retired" | null;
}

/** The table's status (the issue's ready / published / not ready, plus the in-flight write). */
export type ContentStatus = "published" | "ready" | "writing" | "not_ready";

export const CONTENT_STATUS_LABEL: Record<ContentStatus, string> = {
  published: "Published",
  ready: "Ready",
  writing: "Writing",
  not_ready: "Not ready",
};

export function contentStatus(r: Pick<ContentRow, "ready_state" | "published_at">): ContentStatus {
  if (r.published_at) return "published";
  if (r.ready_state === "ready") return "ready";
  if (r.ready_state === "in_progress") return "writing";
  return "not_ready";
}

/** Words of the written text (markdown marks and URLs are not words); null when there is no text yet. */
export function wordCount(text: string | null | undefined): number | null {
  if (text == null) return null;
  const plain = text
    .replace(/https?:\/\/\S+/g, " ")
    .replace(/[#*_>`~|[\]()-]+/g, " ")
    .trim();
  if (!plain) return 0;
  return plain.split(/\s+/).filter((w) => /[\p{L}\p{N}]/u.test(w)).length;
}

/** The piece's own headline: its markdown H1 when the text has one, else the angle's name (the headline the tenant
 *  picked; a blog body starts at its first section, so its first `##` is a section heading, never the title: S251
 *  D5). One title everywhere: the list, the drawer, the export file name and the exported page's <title>. */
export function pieceTitle(r: Pick<ContentRow, "content_text" | "angle">): string {
  const heading = r.content_text?.match(/^\s{0,3}#\s+(.+?)\s*#*\s*$/m)?.[1]?.trim();
  if (heading) return heading.length > 120 ? `${heading.slice(0, 117)}…` : heading;
  return r.angle?.name?.trim() || "Untitled";
}

/** The angle as a short label for the table: its name, only when it says something the title does not (most
 *  titles ARE the angle's name). Never the angle's writing brief (`formula_fit`, "Hook-Insight-CTA: …"): that is an
 *  internal instruction to the writer, not something to show in the list (S251 D2). Null = nothing to show. */
export function angleLabel(r: Pick<ContentRow, "content_text" | "angle">): string | null {
  const name = r.angle?.name?.trim();
  if (!name) return null;
  const norm = (s: string) => s.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();
  if (norm(name) === norm(pieceTitle(r).replace(/…$/, ""))) return null;
  return name.length > 80 ? `${name.slice(0, 77)}…` : name;
}

/** A download's file name stem from the piece title: lower case, a-z / 0-9 runs joined by "-", at most 60 chars. */
export function exportFileStem(title: string | null | undefined, fallback = "content"): string {
  const stem = (title ?? "")
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 60)
    .replace(/-+$/g, "");
  return stem || fallback;
}

/** An exported HTML page with its <title> set to the piece title (CIS writes a fixed "Blog post"). A page without
 *  a <title> element is returned unchanged. */
export function withDocumentTitle(html: string, title: string): string {
  const esc = title.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  return html.replace(/<title>[\s\S]*?<\/title>/i, () => `<title>${esc}</title>`);
}

/** "Tour name · Country", without the country when the name already starts with it. */
export function tourLine(t: { name: string; destination?: string | null }): string {
  const dest = (t.destination ?? "").trim();
  if (!dest || t.name.toLowerCase().startsWith(dest.toLowerCase())) return t.name;
  return `${t.name} · ${dest}`;
}

/** Place / topic: the tour, else the route's hub, else the moment's activity type; null when none is known. */
export function placeTopic(r: Pick<ContentRow, "tour" | "route_hub_name" | "route_segment_count" | "atom">): string | null {
  if (r.tour) return tourLine(r.tour);
  if (r.route_hub_name) {
    const n = r.route_segment_count;
    return n ? `${r.route_hub_name} route · ${n} stop${n === 1 ? "" : "s"}` : `${r.route_hub_name} route`;
  }
  const activity = r.atom?.activity_type?.trim();
  return activity ? activity.charAt(0).toUpperCase() + activity.slice(1).replace(/_/g, " ") : null;
}

// ── Filters and views ────────────────────────────────────────────────────────────────────────────

export interface ContentFilters {
  channel: string; // "all" or a channel key
  status: "all" | ContentStatus;
  goal: string; // "all" or a goal label
  /** Which date the range applies to. */
  dateField: "created" | "published";
  from: string; // YYYY-MM-DD, "" = open
  to: string; // YYYY-MM-DD, "" = open (inclusive)
  place: string; // free text over place / topic
}

export const NO_FILTERS: ContentFilters = {
  channel: "all", status: "all", goal: "all", dateField: "created", from: "", to: "", place: "",
};

export type SortRule = { id: string; desc: boolean };

/** The table's sortable columns. A saved view naming another column (the Angle column before S251) drops it. */
export const SORT_COLUMNS: readonly string[] = [
  "title", "channel", "place", "goal", "status", "words", "created", "published",
];

/** A saved view: the filters and the sort. Built-in views are computed (their dates follow today). */
export interface ContentView {
  id: string;
  name: string;
  builtIn: boolean;
  filters: ContentFilters;
  sorting: SortRule[];
}

/** YYYY-MM-DD of a Date in the viewer's local calendar. */
export function ymd(d: Date): string {
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

export function monthStart(now: Date): string {
  return ymd(new Date(now.getFullYear(), now.getMonth(), 1));
}

/** The issue's two views plus "All content". */
export function builtInViews(now: Date): ContentView[] {
  return [
    { id: "all", name: "All content", builtIn: true, filters: NO_FILTERS, sorting: [{ id: "created", desc: true }] },
    {
      id: "ready-to-publish",
      name: "Ready to publish",
      builtIn: true,
      filters: { ...NO_FILTERS, status: "ready" },
      sorting: [{ id: "created", desc: true }],
    },
    {
      id: "published-this-month",
      name: "Published this month",
      builtIn: true,
      filters: { ...NO_FILTERS, status: "published", dateField: "published", from: monthStart(now) },
      sorting: [{ id: "published", desc: true }],
    },
  ];
}

export function hasFilters(f: ContentFilters): boolean {
  return (
    f.channel !== "all" || f.status !== "all" || f.goal !== "all" || f.from !== "" || f.to !== "" ||
    f.place.trim() !== ""
  );
}

/** A local calendar day of an ISO instant (the viewer's day, like the dates the table shows). */
function localDay(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : ymd(d);
}

export function matchesFilters(r: ContentRow, f: ContentFilters): boolean {
  if (f.channel !== "all" && r.channel !== f.channel) return false;
  if (f.status !== "all" && contentStatus(r) !== f.status) return false;
  if (f.goal !== "all" && (r.goal?.label ?? "") !== f.goal) return false;
  if (f.from || f.to) {
    const day = localDay(f.dateField === "published" ? r.published_at : r.created_at);
    if (!day) return false; // a range on "published" leaves out what is not published
    if (f.from && day < f.from) return false;
    if (f.to && day > f.to) return false;
  }
  const q = f.place.trim().toLowerCase();
  if (q) {
    const hay = [placeTopic(r), r.tour?.name, r.tour?.destination, r.route_hub_name]
      .filter(Boolean).join(" ").toLowerCase();
    if (!hay.includes(q)) return false;
  }
  return true;
}

/** A user view read back from storage: anything malformed falls back to the defaults, never throws. */
export function sanitizeView(raw: unknown): { filters: ContentFilters; sorting: SortRule[] } {
  const o = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  const fr = (o.filters && typeof o.filters === "object" ? o.filters : {}) as Record<string, unknown>;
  const str = (v: unknown, d: string) => (typeof v === "string" ? v : d);
  const day = (v: unknown) => (typeof v === "string" && /^\d{4}-\d{2}-\d{2}$/.test(v) ? v : "");
  const statuses: ContentFilters["status"][] = ["all", "published", "ready", "writing", "not_ready"];
  const status = statuses.includes(fr.status as ContentFilters["status"]) ? (fr.status as ContentFilters["status"]) : "all";
  const filters: ContentFilters = {
    channel: str(fr.channel, "all"),
    status,
    goal: str(fr.goal, "all"),
    dateField: fr.dateField === "published" ? "published" : "created",
    from: day(fr.from),
    to: day(fr.to),
    place: str(fr.place, "").slice(0, 100),
  };
  const sorting = Array.isArray(o.sorting)
    ? (o.sorting as unknown[])
        .filter((s): s is SortRule =>
          !!s && typeof s === "object" && typeof (s as SortRule).id === "string" && typeof (s as SortRule).desc === "boolean" &&
          SORT_COLUMNS.includes((s as SortRule).id))
        .slice(0, 3)
    : [];
  return { filters, sorting };
}

// ── Actions ──────────────────────────────────────────────────────────────────────────────────────

/** Channels the publish endpoint takes (CIS v1_publish _SUPPORTED_CHANNELS) and the connection each needs. */
export const PUBLISH_CONNECTION: Record<string, "wordpress" | "facebook"> = { blog: "wordpress", facebook: "facebook" };

export interface Connections {
  wordpress: boolean;
  facebook: boolean;
}

/** Why one row cannot be published from the table (null = it can). Held pieces are refused by the API itself
 *  (the tenant never sees why: AA-614), so the table does not try to tell them apart. */
export function publishBlock(r: ContentRow, c: Connections): string | null {
  const status = contentStatus(r);
  if (status === "published") return "Already published";
  if (status !== "ready") return "Not ready yet";
  if (r.served_status === "retired" || r.served_status === "paused") return "Publishing closed for this tour";
  const needs = PUBLISH_CONNECTION[r.channel];
  if (!needs) return "Export this channel and post it yourself";
  if (!c[needs]) return needs === "wordpress" ? "Connect WordPress to publish" : "Connect Facebook to publish";
  return null;
}

/** Bulk publish takes blog pieces only, and only with WordPress connected (the issue's rule); everything else is
 *  counted as skipped so the toast can say how many were left out. */
export function bulkPublishPlan(rows: ContentRow[], c: Connections): { eligible: ContentRow[]; skipped: number } {
  const eligible = rows.filter((r) => r.channel === "blog" && c.wordpress && publishBlock(r, c) === null);
  return { eligible, skipped: rows.length - eligible.length };
}

/** What "Export" downloads for a row: blog as an HTML document, every other channel as text. Null = nothing yet. */
export function exportFormat(r: Pick<ContentRow, "channel" | "content_text">): "html" | "text" | null {
  if (r.content_text == null) return null;
  return r.channel === "blog" ? "html" : "text";
}

/** Why a piece cannot be written again (null = it can). A published piece is refused: the rewrite would become the
 *  request's latest version while the old post stays live, and publishing it would put a second post up for the
 *  same request (review S251). The tenant unpublishes first (Publish > Published), then writes again. */
export function regenerateBlock(r: ContentRow): string | null {
  if (contentStatus(r) === "published") return "Unpublish the live post first to write a new version";
  if (r.ready_state === "in_progress") return "Being written now";
  if (r.served_status === "retired" || r.served_status === "paused") return "Closed for this tour";
  return null;
}

export function canRegenerate(r: ContentRow): boolean {
  return regenerateBlock(r) === null;
}

/** The live post shows an earlier version than the text in the list (a rewrite made before this rule, or through
 *  the API). Unknown (older CIS without published_piece_id) reads false. */
export function liveIsEarlierVersion(r: Pick<ContentRow, "piece_id" | "published_at" | "published_piece_id">): boolean {
  return !!r.published_at && !!r.published_piece_id && r.published_piece_id !== r.piece_id;
}
