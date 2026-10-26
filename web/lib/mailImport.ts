// web/lib/mailImport.ts
// AA-945 (card CRM-17, "history import") — the pure parts of Settings > Mailbox > Import mail history: the file kind
// from its name, the 25 MB guard before any byte is sent, the status shape of GET /crm/v1/mail/imports/{id}, the
// poller that stops on done / failed, the raw upload through the BFF and the ids this tab remembers.
//
// API: api/app/mail/history_import.py (module docstring) and api/app/routers/mail.py "history import":
//   POST /crm/v1/mail/imports?source_kind=mbox|csv  raw body <= 25 MB -> 202 {mail_import_id, status, created_at};
//                                                   413 import_too_large, 422 empty_upload / invalid_source_kind
//   GET  /crm/v1/mail/imports/{id}                   counts + status queued | running | done | failed (+ error_code)
// There is no list endpoint: the section shows the imports started in this browser tab (ids only, sessionStorage).

import type { BffResult } from "./bffClient";

export type ImportKind = "mbox" | "csv";
export type ImportStatus = "queued" | "running" | "done" | "failed";

export interface MailImport {
  mail_import_id: string;
  source_kind: ImportKind;
  size_bytes: number;
  status: ImportStatus;
  total_count: number;
  imported_count: number;
  duplicate_count: number;
  skipped_count: number;
  failed_count: number;
  error_code: string | null;
  created_at: string;
  finished_at: string | null;
}

export interface ImportStarted {
  mail_import_id: string;
  status: ImportStatus;
  created_at: string;
}

/** The API's cap (history_import.MAX_BYTES, crm-015 `mail_import_size_chk`): 25 MiB. */
export const MAX_IMPORT_BYTES = 25 * 1024 * 1024;
/** How often the section asks for the progress of an unfinished import. */
export const IMPORT_POLL_MS = 3_000;
/** Give up polling after this many failed status calls in a row (network, 5xx); the member can reload. */
export const MAX_POLL_ERRORS = 5;
/** How many imports of this tab are remembered. */
export const REMEMBERED_IMPORTS = 5;
export const IMPORTS_STORAGE_KEY = "crm.mailImports";

const MBOX_EXTENSIONS = ["mbox", "mbx"];
const CSV_EXTENSIONS = ["csv"];

/**
 * The kind a file name says: `.csv` -> csv, `.mbox` / `.mbx` -> mbox (Google Takeout names its file
 * "All mail Including Spam and Trash.mbox"). Anything else, including Thunderbird's folder files that have no
 * extension ("Inbox", "Sent"), is null: the member picks the kind (the section then defaults to mbox).
 */
export function detectKind(fileName: string): ImportKind | null {
  const dot = fileName.lastIndexOf(".");
  if (dot <= 0 || dot === fileName.length - 1) return null;
  const ext = fileName.slice(dot + 1).trim().toLowerCase();
  if (CSV_EXTENSIONS.includes(ext)) return "csv";
  if (MBOX_EXTENSIONS.includes(ext)) return "mbox";
  return null;
}

/** The client-side guard: nothing is sent for an empty file or one over the cap. */
export function checkFile(file: { size: number }): "ok" | "empty" | "too_large" {
  if (file.size <= 0) return "empty";
  if (file.size > MAX_IMPORT_BYTES) return "too_large";
  return "ok";
}

export function isFinished(status: ImportStatus | string): boolean {
  return status === "done" || status === "failed";
}

/** "12.4 MB" / "830 KB" / "12 B" (1 MB = 1024 * 1024 bytes, like the cap). */
export function formatBytes(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${bytes} B`;
}

/** The i18n key of an upload / status error: known API and BFF codes by name, any 413 as "too large". */
export function importErrorKey(status: number, detail: string): string {
  if (status === 413 || detail === "import_too_large" || detail === "body_too_large") return "mail.error.import_too_large";
  if (IMPORT_ERRORS.has(detail)) return `mail.error.${detail}`;
  return "";
}

const IMPORT_ERRORS = new Set([
  "empty_upload",
  "invalid_source_kind",
  "mail_ingest_not_configured",
  "mail_import_not_found",
  "role_forbidden",
  "api_timeout",
  "api_unreachable",
  "network",
]);

/** The key that explains a failed import's `error_code` (job side: csv_columns, bad_payload, import_failed). */
export function failureKey(code: string | null): string {
  return code && ["csv_columns", "bad_payload", "import_failed"].includes(code)
    ? `mail.import.failure.${code}`
    : "mail.import.failure.other";
}

/** The API's own `message` of an error body, when it is a plain short string (shown when the code has no text). */
export function apiMessage(body: unknown): string | null {
  const m = (body as { message?: unknown } | null)?.message;
  return typeof m === "string" && m.length > 0 && m.length <= 300 ? m : null;
}

type Timers = { set: (fn: () => void, ms: number) => unknown; clear: (handle: unknown) => void };

const realTimers: Timers = {
  set: (fn, ms) => setTimeout(fn, ms),
  clear: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
};

/**
 * Polls one import: asks at once, then every `intervalMs` until the status is done / failed, a 4xx answer (the import
 * is gone, or another agency's), {@link MAX_POLL_ERRORS} failures in a row, or `stop()`. Never two calls at once.
 */
export function createImportPoller(
  importId: string,
  fetchStatus: (id: string) => Promise<BffResult<MailImport>>,
  on: { update: (row: MailImport) => void; error: (status: number, detail: string, body?: unknown) => void },
  intervalMs: number = IMPORT_POLL_MS,
  timers: Timers = realTimers,
) {
  let handle: unknown = null;
  let stopped = false;
  let errors = 0;

  async function tick() {
    handle = null;
    if (stopped) return;
    const res = await fetchStatus(importId);
    if (stopped) return;
    if (res.ok) {
      errors = 0;
      on.update(res.data);
      if (isFinished(res.data.status)) {
        stopped = true;
        return;
      }
    } else {
      errors += 1;
      const permanent = res.status >= 400 && res.status < 500;
      if (permanent || errors >= MAX_POLL_ERRORS) {
        stopped = true;
        on.error(res.status, res.detail, res.body);
        return;
      }
    }
    handle = timers.set(() => void tick(), intervalMs);
  }

  return {
    start() {
      if (!stopped && handle === null) void tick();
    },
    stop() {
      stopped = true;
      if (handle !== null) timers.clear(handle);
      handle = null;
    },
    get running() {
      return !stopped;
    },
  };
}

/** The imports this tab started, newest first (ids and kinds only: no file name, nothing personal). */
export interface RememberedImport {
  id: string;
  kind: ImportKind;
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export function readRemembered(storage: Pick<Storage, "getItem"> | null): RememberedImport[] {
  try {
    const raw = storage?.getItem(IMPORTS_STORAGE_KEY);
    const list = raw ? (JSON.parse(raw) as unknown) : [];
    if (!Array.isArray(list)) return [];
    return list
      .filter(
        (x): x is RememberedImport =>
          !!x && typeof x.id === "string" && UUID_RE.test(x.id) && (x.kind === "mbox" || x.kind === "csv"),
      )
      .slice(0, REMEMBERED_IMPORTS);
  } catch {
    return [];
  }
}

export function writeRemembered(storage: Pick<Storage, "setItem"> | null, list: RememberedImport[]): void {
  try {
    storage?.setItem(IMPORTS_STORAGE_KEY, JSON.stringify(list.slice(0, REMEMBERED_IMPORTS)));
  } catch {
    // Private mode / storage full: the section still works for this page view.
  }
}

/**
 * POSTs the file as the raw request body to /bff/mail/imports?source_kind=<kind> (the BFF forwards the bytes to the
 * API unchanged). XMLHttpRequest, not fetch, because only it reports upload progress. A 401 reloads through
 * /auth/refresh like every BFF call (lib/bffClient.ts).
 */
export function uploadImport(
  file: Blob,
  kind: ImportKind,
  onProgress: (fraction: number) => void,
  Xhr: typeof XMLHttpRequest = XMLHttpRequest,
): Promise<BffResult<ImportStarted>> {
  return new Promise((resolve) => {
    const xhr = new Xhr();
    xhr.open("POST", `/bff/mail/imports?source_kind=${kind}`);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.setRequestHeader("Accept", "application/json");
    xhr.withCredentials = true;
    xhr.upload.onprogress = (e: ProgressEvent) => {
      if (e.lengthComputable && e.total > 0) onProgress(Math.min(1, e.loaded / e.total));
    };
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
      resolve({ ok: true, data: body as ImportStarted });
    };
    xhr.send(file);
  });
}
