// web/lib/portability.ts
// AA-967 (card CRM-35, Module 18) — Settings > Data and privacy: the shapes of GET /crm/v1/data/exports and
// GET /crm/v1/data/requests, a client erasure's answer, and the pure helpers the page uses to word them (the API
// returns codes, numbers and dates only).

import { fmt, t, type Lang, type MessageKey } from "./i18n";

export type ExportStatus = "queued" | "building" | "ready" | "failed" | "cancelled" | "expired";

export interface DataExport {
  export_id: string;
  scope: "tenant" | "traveler";
  subject_id: string;
  status: ExportStatus;
  schema_version: number;
  progress: { done: number; total: number };
  rows: number;
  tables: number;
  documents_included: number;
  documents_skipped: number;
  size_bytes: number | null;
  content_sha256: string | null;
  created_at: string;
  completed_at: string | null;
  expires_at: string | null;
  downloaded_at: string | null;
  download_count: number;
  failure_class: string | null;
  data_request_id: string | null;
}

export interface ExportsView {
  exports: DataExport[];
  link_minutes: number;
  file_days: number;
  schema_version: number;
}

export interface ExportLink {
  export_id: string;
  token: string;
  expires_at: string;
  path: string;
}

export type RequestStatus = "open" | "extended" | "done" | "refused" | "withdrawn";

export interface DataRequest {
  data_request_id: string;
  kind: string;
  channel: string;
  traveler_id: string | null;
  received_on: string;
  due_on: string;
  status: RequestStatus;
  overdue: boolean;
  max_due_on: string;
  outcome: string | null;
  closed_at: string | null;
  erasure_request_id: string | null;
  exports: { export_id: string; status: ExportStatus }[];
  created_at: string;
}

export interface RequestsView {
  requests: DataRequest[];
  kinds: string[];
  channels: string[];
  outcomes: Record<"done" | "refused" | "withdrawn", string[]>;
  default_due_days: number;
  max_due_days: number;
  today: string;
}

export interface ErasureAnswer {
  traveler_id: string;
  dry_run: boolean;
  counts: Record<string, number>;
  kept: Record<string, number>;
  exceptions: string[];
  exports_withdrawn: number;
  request_id?: string;
  backup_purge_by?: string;
}

export const OPEN_EXPORT: readonly ExportStatus[] = ["queued", "building"];

/** A byte count in the page's words (B, KB, MB, GB; one decimal from MB on). */
export function formatBytes(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n) || n < 0) return "–";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

/** Progress of an export being built, 0..100 (0 when the job has not counted its steps yet). */
export function progressPercent(e: Pick<DataExport, "progress" | "status">): number {
  if (e.status === "ready") return 100;
  const { done, total } = e.progress;
  if (!total || total <= 0) return 0;
  return Math.max(0, Math.min(100, Math.round((done / total) * 100)));
}

/** The kit Badge tone of an export or request status. */
export function statusTone(status: ExportStatus | RequestStatus, overdue = false): "success" | "warning" | "danger" | "info" | "neutral" {
  if (overdue) return "danger";
  if (status === "ready" || status === "done") return "success";
  if (status === "failed") return "danger";
  if (status === "queued" || status === "building" || status === "open" || status === "extended") return "info";
  return "neutral";
}

/** The first 16 hex characters of a SHA-256, grouped by 4 (the full value is in the tooltip and the manifest). */
export function shortDigest(hex: string | null | undefined): string {
  if (!hex) return "";
  return (hex.slice(0, 16).match(/.{1,4}/g) ?? []).join(" ");
}

/** Records an erasure removes: every count except the kept evidence, the unlinks and the object count. */
export function erasedCount(counts: Record<string, number>): number {
  return Object.entries(counts)
    .filter(([k]) => !k.endsWith("_kept") && !k.endsWith("_unlinked") && k !== "document_object")
    .reduce((sum, [, n]) => sum + (Number.isFinite(n) ? n : 0), 0);
}

export function keptCount(kept: Record<string, number>): number {
  return Object.values(kept).reduce((sum, n) => sum + (Number.isFinite(n) ? n : 0), 0);
}

/** The lines the erasure preview shows, in the page's language. */
export function erasureLines(a: ErasureAnswer, lang: Lang): string[] {
  const lines = [fmt(t(lang, "data.era.preview"), { n: erasedCount(a.counts) })];
  const kept = keptCount(a.kept);
  if (kept > 0) lines.push(fmt(t(lang, "data.era.kept"), { n: kept }));
  for (const code of a.exceptions) {
    if (code !== "signed_agreements_claim_period" || kept === 0) lines.push(t(lang, `data.era.exception.${code}` as MessageKey));
  }
  if (a.exports_withdrawn > 0) lines.push(fmt(t(lang, "data.era.exports"), { n: a.exports_withdrawn }));
  return lines;
}

/** The 409 detail of a refused erasure -> its message key (anything else: the page's generic error). */
export function erasureRefusal(detail: string): MessageKey | null {
  return detail === "traveler_on_trip" || detail === "legal_hold" ? (`data.era.refused.${detail}` as MessageKey) : null;
}

/** The outcomes that fit a closing status (the API's lists, never typed by hand). */
export function outcomesFor(view: RequestsView, status: "done" | "refused" | "withdrawn"): string[] {
  return view.outcomes[status] ?? [];
}

/** A date input's value (YYYY-MM-DD) plus ``days``, as the API's due-date default. */
export function addDays(isoDate: string, days: number): string {
  const d = new Date(`${isoDate}T00:00:00Z`);
  if (Number.isNaN(d.getTime())) return isoDate;
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

export function isOpenRequest(r: Pick<DataRequest, "status">): boolean {
  return r.status === "open" || r.status === "extended";
}

/** The browser file name of a downloaded export. */
export function exportFileName(e: Pick<DataExport, "scope" | "export_id">): string {
  return `crm-export-${e.scope}-${e.export_id.slice(0, 8)}.zip`;
}
