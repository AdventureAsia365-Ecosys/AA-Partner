// web/lib/mail.ts
// AA-945 (card CRM-17) — the shapes of the mail API (api/app/mail) and the small pure helpers Settings > Mailbox, the
// trip Messages tab, the Unmatched inbox and Settings > Saved replies share.
//
// HTML mail is shown only inside <iframe sandbox srcdoc> with an EMPTY sandbox (no allow-scripts, no allow-same-origin,
// no allow-forms, no allow-popups): the frame gets an opaque origin, so a mail cannot run script, read this page, send
// the session cookies or open windows (design S10). `htmlFrame` is the only place that builds its attributes. It also
// puts a Content-Security-Policy <meta> first in the document (a sandboxed srcdoc frame honours it): no remote image,
// style, font or frame loads, so opening a mail sends the sender no read receipt and no IP address.

export type MailboxKind = "gmail" | "microsoft" | "smtp";
export type MailboxStatus = "connected" | "needs_attention" | "disconnected";
export type MessageStatus = "received" | "draft_autosaved" | "queued" | "held_reconnect" | "sent" | "failed";

export interface Mailbox {
  channel_account_id: string;
  scope: "own" | "agency";
  kind: MailboxKind;
  address: string;
  status: MailboxStatus;
  failure_class: string | null;
  last_checked_at: string | null;
  last_ok_at: string | null;
  last_test_at: string | null;
  last_test_result: "ok" | "failed" | null;
  held_count: number;
}

export interface MailboxView {
  mailboxes: Mailbox[];
  providers: { google: boolean; microsoft: boolean; smtp: boolean };
  billing_note: string;
}

export interface IngestAddress {
  ingest_address_id: string;
  address: string | null;
  forwarding_code: string | null;
  forwarding_code_at: string | null;
}

export interface SenderRow {
  path: string;
  sender_kind: "agency_mailbox" | "system";
  recipient: "client" | "agency_member";
  via: "agency_mailbox" | "aa_ses";
  module: string;
}

export interface Conversation {
  conversation_id: string;
  trip_id: string | null;
  traveler_id: string | null;
  channel: string;
  last_message_at: string;
  message_count: number;
  held_count: number;
  last_direction: "in" | "out" | null;
  unmatched: boolean;
}

export interface MailMessage {
  message_id: string;
  direction: "in" | "out";
  status: MessageStatus;
  sender_kind: string | null;
  author_member_id: string | null;
  subject: string | null;
  text: string | null;
  html: string | null;
  from: string | null;
  to: string[];
  attachments: string[];
  attachment_notes: string[];
  failure_code: string | null;
  imported: boolean;
  match_method: string | null;
  occurred_at: string;
  sent_at: string | null;
  updated_at: string;
}

export interface Thread {
  conversation_id: string;
  trip_id: string | null;
  traveler_id: string | null;
  messages: MailMessage[];
}

export interface SavedReply {
  saved_reply_id: string;
  scope: "own" | "agency";
  locale: "en" | "vi";
  title: string;
  body: string;
  updated_at: string;
}

/** Undo window the API gives a sent message (dispatch delay, Fix B8). */
export const UNDO_MS = 10_000;
/** The composer saves the draft at most this often while typing. */
export const AUTOSAVE_MS = 3_000;

/** Nothing remote: inline styles and data: / cid: images only (cid: never resolves here, it just stays blank). */
export const MAIL_FRAME_CSP = "default-src 'none'; img-src data: cid:; style-src 'unsafe-inline'; font-src data:";

/** The iframe attributes for an HTML mail body: an empty sandbox (opaque origin, no script), no referrer, and the
 * CSP <meta> ahead of the mail's own markup (a <meta http-equiv="refresh"> in the mail is blocked by the sandbox). */
export function htmlFrame(html: string): { srcDoc: string; sandbox: string; referrerPolicy: "no-referrer"; title: string } {
  const csp = `<meta http-equiv="Content-Security-Policy" content="${MAIL_FRAME_CSP}">`;
  return { srcDoc: csp + html, sandbox: "", referrerPolicy: "no-referrer", title: "mail" };
}

/** The SMTP ports the API dials (api/app/mail/providers/smtp.py PORTS); anything else is refused there. */
export const SMTP_PORTS = [587, 465, 2525, 25] as const;

const ERRORS = new Set([
  "mailbox_not_connected",
  "conversation_unmatched",
  "already_dispatched",
  "not_a_draft",
  "assigned_only",
  "role_forbidden",
  "mail_oauth_not_configured",
  "oauth_state_expired",
  "oauth_state_invalid",
  "oauth_code_rejected",
  "oauth_no_address",
  "decrypt_denied",
  // PUT /mailbox/smtp: "smtp_" + the provider code, never doubled (api/app/routers/mail.py put_smtp).
  "smtp_auth_revoked",
  "smtp_unreachable",
  "smtp_temporary",
  "smtp_provider_rejected",
  "smtp_tls_required",
  "smtp_invalid_host",
  "smtp_invalid_port",
  "smtp_missing_username",
  "smtp_missing_password",
  "smtp_missing_address",
  "already_linked",
  "import_too_large",
  "mailbox_not_found",
  "network",
]);

export function mailErrorKey(detail: string): string {
  return ERRORS.has(detail) ? `mail.error.${detail}` : "mail.error.other";
}

/** A fresh id for a draft (the composer picks it so the autosave PUT is idempotent). */
export function newDraftId(random: () => string = () => crypto.randomUUID()): string {
  return random();
}

/**
 * Debounced autosave: `schedule()` on every keystroke saves once after `delayMs` of quiet, `flush()` saves now
 * (before Send), `cancel()` drops a pending save (after Send / Discard). Timer functions are injectable for tests.
 */
export function createAutosave(
  save: () => Promise<unknown> | void,
  delayMs: number = AUTOSAVE_MS,
  timers: { set: (fn: () => void, ms: number) => unknown; clear: (handle: unknown) => void } = {
    set: (fn, ms) => setTimeout(fn, ms),
    clear: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
  },
) {
  let handle: unknown = null;
  return {
    schedule() {
      if (handle !== null) timers.clear(handle);
      handle = timers.set(() => {
        handle = null;
        void save();
      }, delayMs);
    },
    async flush() {
      if (handle !== null) {
        timers.clear(handle);
        handle = null;
      }
      await save();
    },
    cancel() {
      if (handle !== null) timers.clear(handle);
      handle = null;
    },
    get pending() {
      return handle !== null;
    },
  };
}

/** The OAuth callback query the provider sent back: code + state, or the provider's error. Values are not echoed. */
export function callbackParams(search: URLSearchParams): { code: string; state: string } | { error: string } {
  const error = search.get("error");
  if (error) return { error: error === "access_denied" ? "denied" : "provider" };
  const code = search.get("code") ?? "";
  const state = search.get("state") ?? "";
  if (!code || !state || code.length > 4096 || state.length > 4096) return { error: "invalid" };
  return { code, state };
}

/** Whether any of the member's mailboxes stopped working (the shell's red banner). */
export function disconnected(view: MailboxView | null): Mailbox[] {
  return (view?.mailboxes ?? []).filter((m) => m.status === "disconnected");
}
