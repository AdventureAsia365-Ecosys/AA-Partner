// web/lib/onboarding.ts
// AA-962 (card CRM-31, A-20) — the pure parts of onboarding: the checklist and Connections shapes of
// GET /crm/v1/onboarding, which steps the Home list shows, the import phases and their file guard, the import report
// shapes, the raw upload of a file to the preview / start routes, and the error keys.
//
// API: api/app/onboarding.py, api/app/imports/service.py, api/app/travellers/merge.py (module docstrings):
//   GET  /crm/v1/onboarding                                   steps, connections, controller settings, acknowledgement
//   PUT  /crm/v1/onboarding/steps/{key}       {state}         done (manual steps) | later (optional) | open — owner
//   PUT  /crm/v1/onboarding/controller        {...}           privacy-notice link, data-request contact, consent text
//   POST /crm/v1/onboarding/acknowledgement   {version}       409 version_changed when the text changed
//   POST /crm/v1/client-imports/preview?entity=&phone_region= raw file <= 5 MB -> the dry-run report (nothing written)
//   POST /crm/v1/client-imports?entity=&sha256=&phone_region= the previewed file -> 202 {import_id}; 409 file_changed
//   GET  /crm/v1/client-imports[/{id}]                        progress and counts (codes and numbers only)
//   GET  /crm/v1/client-duplicates, POST /crm/v1/travelers/{id}/merge {other_traveler_id}

import type { BffResult } from "./bffClient";

/** Must equal api/app/onboarding.py ACK_VERSION (the API refuses another version with 409 version_changed). */
export const ACK_VERSION = "2026-10-31.1";

export type StepKey =
  | "connect_mailbox"
  | "invite_team"
  | "embed_form"
  | "import_clients"
  | "review_duplicates"
  | "payments"
  | "deposit_rules"
  | "templates"
  | "privacy_settings"
  | "kickoff_call"
  | "help_centre";

export interface Step {
  key: StepKey;
  required: boolean;
  done: boolean;
  marked_done: boolean;
  later: boolean;
  can_mark_done: boolean;
  href: string;
  help: string;
}

export type ConnectionState = "not_set_up" | "connected" | "needs_attention" | "disconnected";

export interface Connection {
  key: "mailbox" | "card_payments" | "sms_whatsapp" | "calendar" | "esign";
  required: boolean;
  status: ConnectionState;
  bank_transfer?: boolean;
  href: string | null;
  help: string;
}

export interface OnboardingView {
  steps: Step[];
  required_total: number;
  required_done: number;
  optional_open: number;
  complete: boolean;
  acknowledgement: { version: string; accepted: boolean; accepted_version: string | null; accepted_at: string | null };
  controller: { privacy_notice_url: string | null; data_request_contact: string | null; consent_text: string | null };
  connections: Connection[];
  duplicate_count: number;
}

/** The Home list: every required step, then the optional ones not done and not put on "Later". */
export function homeSteps(view: OnboardingView): Step[] {
  return [...view.steps.filter((s) => s.required), ...view.steps.filter((s) => !s.required && !s.done && !s.later)];
}

export function connectionTone(status: ConnectionState): "success" | "warning" | "danger" | "neutral" {
  return status === "connected" ? "success" : status === "needs_attention" ? "warning" : status === "disconnected" ? "danger" : "neutral";
}

// ── import ─────────────────────────────────────────────────────────────────────────────────────────
export type Entity = "clients" | "relationships" | "trips" | "notes";
/** The order the design asks for (clients -> relationships -> trips -> notes): each phase names refs of the earlier. */
export const ENTITIES: readonly Entity[] = ["clients", "relationships", "trips", "notes"];
export const OUTCOMES = ["create", "match", "already_imported", "duplicate_in_file", "invalid"] as const;
export type Outcome = (typeof OUTCOMES)[number];
/** The API's cap (api/app/imports/formats.py MAX_BYTES): 5 MiB, under the proxy's 10 MiB body clone limit. */
export const MAX_IMPORT_FILE_BYTES = 5 * 1024 * 1024;
export const IMPORT_POLL_MS = 2_000;

/** The columns of each file (api/app/imports/spec.py): required first; notes need client_ref or trip_ref. */
export const COLUMNS: Record<Entity, { required: string[]; optional: string[] }> = {
  clients: {
    required: ["client_ref", "name"],
    optional: ["given_name", "family_name", "email", "phone", "preferred_channel", "dob", "nationality", "marketing_consent"],
  },
  relationships: { required: ["client_ref", "related_client_ref", "relation"], optional: [] },
  trips: { required: ["trip_ref", "client_ref", "title"], optional: ["start_date", "end_date", "status", "adults", "companion_refs"] },
  notes: { required: ["text", "client_ref or trip_ref"], optional: ["note_ref", "date"] },
};

/** A header line agencies can copy into a new file (the template the page offers to download). */
export function templateCsv(entity: Entity): string {
  const cols = [...COLUMNS[entity].required.flatMap((c) => c.split(" or ")), ...COLUMNS[entity].optional];
  return `${[...new Set(cols)].join(",")}\n`;
}

export interface Issue {
  row: number;
  outcome: Outcome;
  codes: string[];
}

export interface PreviewReport {
  entity: Entity;
  format: "csv" | "xlsx";
  sha256: string;
  columns: string[];
  ignored_columns: string[];
  row_count: number;
  counts: Record<Outcome, number>;
  possible_duplicate_count: number;
  issues: Issue[];
  issues_truncated: boolean;
  can_apply: boolean;
}

export interface ImportStatus {
  import_id: string;
  entity: Entity;
  status: "queued" | "running" | "done" | "failed";
  row_count: number;
  processed_count: number;
  counts: Record<Outcome, number>;
  possible_duplicate_count: number;
  created_at: string;
  finished_at: string | null;
}

export function checkImportFile(file: { size: number; name: string }): "ok" | "empty" | "too_large" | "wrong_type" {
  if (file.size <= 0) return "empty";
  if (file.size > MAX_IMPORT_FILE_BYTES) return "too_large";
  const ext = file.name.toLowerCase().split(".").pop() ?? "";
  return ["csv", "xlsx", "txt"].includes(ext) ? "ok" : "wrong_type";
}

/** Upper-case two letters, or "" (the field is optional). */
export function normaliseRegion(value: string): string {
  const v = value.trim().toUpperCase();
  return /^[A-Z]{2}$/.test(v) ? v : "";
}

export function importQuery(entity: Entity, region: string, sha256?: string): string {
  const q = new URLSearchParams({ entity });
  if (sha256) q.set("sha256", sha256);
  if (region) q.set("phone_region", region);
  return q.toString();
}

/** The i18n key of an upload / preview / start error: known API and BFF codes by name, a 413 as too large. */
export function importErrorKey(status: number, detail: string): string {
  if (status === 413 || detail === "import_too_large" || detail === "body_too_large") return "onb.err.import_too_large";
  return KNOWN_ERRORS.has(detail) ? `onb.err.${detail}` : "onb.err.other";
}

const KNOWN_ERRORS = new Set([
  "missing_columns",
  "empty_upload",
  "no_header",
  "duplicate_column",
  "too_many_columns",
  "too_many_rows",
  "unreadable_file",
  "file_encoding",
  "file_changed",
  "mail_ingest_not_configured",
  "role_forbidden",
  "upload_busy",
  "network",
  "merge_blocked",
  "legal_hold",
  "version_changed",
  "invalid_privacy_notice_url",
  "invalid_data_request_contact",
]);

/** The missing column names a 422 missing_columns carries. */
export function missingColumns(body: unknown): string[] {
  const cols = (body as { columns?: unknown } | null)?.columns;
  return Array.isArray(cols) ? cols.filter((c): c is string => typeof c === "string") : [];
}

/** POSTs the file as the raw body (XMLHttpRequest for upload progress; a 401 reloads through /auth/refresh). */
export function uploadRaw<T>(path: string, file: Blob, Xhr: typeof XMLHttpRequest = XMLHttpRequest): Promise<BffResult<T>> {
  return new Promise((resolve) => {
    const xhr = new Xhr();
    xhr.open("POST", `/bff/${path}`);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.setRequestHeader("Accept", "application/json");
    xhr.withCredentials = true;
    xhr.onerror = () => resolve({ ok: false, status: 0, detail: "network" });
    xhr.ontimeout = () => resolve({ ok: false, status: 0, detail: "network" });
    xhr.onload = () => {
      let body: unknown = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        body = null;
      }
      if (xhr.status === 401 && typeof window !== "undefined") {
        window.location.assign(`/auth/refresh?next=${encodeURIComponent(window.location.pathname + window.location.search)}`);
        resolve({ ok: false, status: 401, detail: "session_expired" });
        return;
      }
      if (xhr.status < 200 || xhr.status >= 300) {
        const detail = typeof (body as { detail?: unknown })?.detail === "string" ? (body as { detail: string }).detail : "error";
        resolve({ ok: false, status: xhr.status, detail, body });
        return;
      }
      resolve({ ok: true, data: body as T });
    };
    xhr.send(file);
  });
}

export function isFinished(status: string): boolean {
  return status === "done" || status === "failed";
}

// ── duplicates ─────────────────────────────────────────────────────────────────────────────────────
export interface DuplicateClient {
  traveler_id: string;
  display_name: string;
  lead_source: string | null;
  trips: number;
  created_at: string;
}

export interface DuplicatePair {
  clients: [DuplicateClient, DuplicateClient];
  matched_on: ("name" | "email" | "phone")[];
}

export interface DuplicatesView {
  pairs: DuplicatePair[];
  total: number;
  has_more: boolean;
}

/** The blocker codes of a 409 merge_blocked, and whether keeping the other client instead would work. */
export function mergeBlock(body: unknown): { blockers: string[]; swap: boolean } {
  const b = body as { blockers?: unknown; swap_possible?: unknown } | null;
  return {
    blockers: Array.isArray(b?.blockers) ? b.blockers.filter((x): x is string => typeof x === "string") : [],
    swap: b?.swap_possible === true,
  };
}
