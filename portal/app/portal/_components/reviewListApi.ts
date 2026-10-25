// portal/app/portal/_components/reviewListApi.ts
// AA-669 — My Content (t10-review) data layer, used with react-query. Tenant BFF proxy, no extra
// headers (session cookie forwarded by the proxy). Keeps the exact ReviewItem shape AA-501/AA-519
// established and the tenant-safe projection (never gate_ledger/held_reason/repair_log).

import { apiGet } from "@/app/_kit";
import type { ServedFields } from "./ServedBadge";
import { exportFileStem, withDocumentTitle } from "@/lib/myContent";

export interface ReviewGoal { key: string; label: string }
export interface ReviewAngle {
  name: string;
  why_it_works: string;
  formula_fit: string;
  best_final_style: string;
}
export interface ReviewAtom {
  text: string;
  activity_type: string | null;
  emotional_hook: string | null;
  season_note: string | null;
}
export interface ReviewTour { name: string; destination: string }
export interface DfsPaaSnapshot {
  relevance: string;
  people_also_ask: string[];
  related_keywords: string[];
}

export type ReadyState = "ready" | "in_progress" | "not_ready";

// AA-986 L3: each review row carries the served state of its tour (badge on Content).
export interface ReviewItem extends ServedFields {
  request_id: string;
  piece_id: string;
  channel: string;
  ready_state: ReadyState;
  content_text: string | null;
  goal: ReviewGoal | null;
  angle: ReviewAngle | null;
  atom: ReviewAtom | null;
  tour: ReviewTour | null;
  dfs_paa_snapshot: DfsPaaSnapshot | null;
  cta: string | null;
  created_at: string | null;
  route_hub_name: string | null;
  route_segment_count: number | null;
  /** AA-669 — the request's live post (CIS joins the newest 'published' publish_log row of any piece of the
   *  request). Optional: absent from a CIS build before AA-669, read as "not published". */
  published_at?: string | null;
  published_url?: string | null;
  /** The piece whose post is live (differs from piece_id when the post shows an earlier version). */
  published_piece_id?: string | null;
}

export function fetchReviews(): Promise<ReviewItem[]> {
  return apiGet<{ data?: ReviewItem[] }>(`/api/tenant/v1/content-writing/reviews`).then(
    (d) => d.data ?? [],
  );
}

export async function savePieceText(pieceId: string, contentText: string): Promise<{ content_text: string }> {
  const r = await fetch(`/api/tenant/v1/content-writing/pieces/${pieceId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content_text: contentText }),
  });
  if (!r.ok) throw new Error(`Save failed (${r.status})`);
  return r.json();
}

export interface PublishResult {
  success: boolean;
  status: string;
  external_url: string | null;
  replayed?: boolean;
}

/** Channels the publish endpoint supports (api/routers/v1_publish.py _SUPPORTED_CHANNELS). */
export const PUBLISHABLE_CHANNELS: ReadonlySet<string> = new Set(["blog", "facebook"]);

/**
 * Publish one piece (blog/facebook + connected channel). Backend validates eligibility.
 * AA-668 — the endpoint answers 200 with `success: false` when the remote post failed (the row is
 * kept as 'failed' for a retry); that is a failure too, not a publish.
 */
export async function publishPiece(pieceId: string): Promise<PublishResult> {
  const r = await fetch(`/api/tenant/v1/publish-log/${pieceId}/publish`, { method: "POST" });
  if (!r.ok) {
    const e = await r.json().catch(() => ({}));
    throw new Error(e.detail || `Publish failed (${r.status})`);
  }
  const d: PublishResult = await r.json();
  if (d.success === false) throw new Error("The post didn't go through — try again in a moment.");
  return d;
}

/**
 * Fetch an export blob for a piece and trigger a browser download. Kept client-side (the proxy
 * forwards Content-Type but not Content-Disposition, so a plain <a href> would open inline).
 * The backend audit-logs each export per tenant/channel (AA-613/AA-614).
 * S251 D5: the file name and, for an HTML page, its <title> are the piece title (lib/myContent.ts pieceTitle), the
 * same title the list and the drawer show; CIS writes a fixed "Blog post" <title>, replaced here.
 */
export async function exportPiece(
  // AA-668 — only these fields are read, so the Writer panel can export a just-written piece too.
  item: Pick<ReviewItem, "piece_id" | "channel"> & { title?: string | null; angle?: { name: string } | null },
  format: "text" | "html",
  mode: "document" | "fragment" = "document",
): Promise<void> {
  const qs = format === "html" ? `format=html&mode=${mode}` : "format=text";
  const r = await fetch(`/api/tenant/v1/content-writing/pieces/${item.piece_id}/export?${qs}`);
  if (!r.ok) throw new Error(`Export failed (${r.status})`);
  const title = (item.title || item.angle?.name || "").trim();
  let blob = await r.blob();
  if (format === "html" && mode === "document" && title) {
    blob = new Blob([withDocumentTitle(await blob.text(), title)], { type: blob.type || "text/html" });
  }
  const url = URL.createObjectURL(blob);
  const ext = format === "html" ? (mode === "fragment" ? "fragment.html" : "html") : "txt";
  const a = document.createElement("a");
  a.href = url;
  a.download = `${exportFileStem(title, item.channel || "content")}.${ext}`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

/**
 * AA-669 — write the piece again (same request, same angle): POST .../requests/{id}/write, 202. The new piece
 * becomes the request's latest (the list shows it as Writing, then Ready); the old one stays in history. Not
 * offered on a published row (lib/myContent.ts regenerateBlock): the tenant unpublishes the live post first. No CTA is sent: the request's (or the brand's) CTA is
 * used, as for the first write. 409 = busy / tour closed, 422 = no CTA anywhere (write it from Create instead).
 */
export async function regeneratePiece(requestId: string): Promise<{ piece_id: string }> {
  const r = await fetch(`/api/tenant/v1/content-writing/requests/${requestId}/write`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
  if (!r.ok) {
    const e = await r.json().catch(() => ({}));
    if (r.status === 422) throw new Error("Add a call to action first: open it from Create and write it there.");
    throw new Error(typeof e.detail === "string" ? e.detail : `Could not start the rewrite (${r.status})`);
  }
  return r.json();
}

export function channelLabel(channel: string): string {
  return channel.charAt(0).toUpperCase() + channel.slice(1);
}

export const READY_STATE_META: Record<ReadyState, { label: string; tone: string }> = {
  ready: { label: "Ready", tone: "success" },
  in_progress: { label: "Writing…", tone: "info" },
  not_ready: { label: "Not Ready Yet", tone: "warning" },
};
