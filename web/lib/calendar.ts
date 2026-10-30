// web/lib/calendar.ts
// AA-961 (card CRM-30; design v3 R15, A-09): the shapes of GET /calendar and the ICS feed routes, and the pure
// helpers of the calendar page (group by day, labels, the feed banner). The feed URL is shown once, when it is made;
// after that the API keeps only its hash. Edge-safe.

import type { MessageKey } from "./i18n";

export type EventKind = "departure" | "return" | "payment_due" | "invoice_due" | "signing_deadline" | "task" | "call";
export type Scope = "mine" | "agency";

export interface CalendarEvent {
  kind: EventKind;
  id: string;
  date: string;
  all_day: boolean;
  starts_at?: string;
  ends_at?: string;
  trip_id: string | null;
  title: string | null;
  stage?: string;
  payment_kind?: "deposit" | "balance";
  amount?: { amount: string; currency: string };
  invoice_number?: string | null;
  pending_signatures?: number;
  task_id?: string;
  task_kind?: string;
  priority?: "normal" | "urgent";
  call_status?: "requested" | "confirmed";
}

export interface CalendarView {
  from: string;
  to: string;
  today: string;
  timezone: string;
  scope: Scope;
  events: CalendarEvent[];
  truncated: boolean;
}

export interface Feed {
  calendar_feed_id: string;
  member_id: string;
  scope: Scope;
  status: "active" | "revoked";
  created_at: string;
  expires_at: string;
  revoked_at: string | null;
  revoked_by_owner: boolean;
  last_fetched_at: string | null;
  fetch_count: number;
}

export const KINDS: readonly EventKind[] = ["departure", "return", "payment_due", "invoice_due", "signing_deadline", "task", "call"];

export function kindKey(kind: EventKind): MessageKey {
  return `calendar.kind.${kind}` as MessageKey;
}

export const KIND_TONE: Record<EventKind, "info" | "success" | "warning" | "danger" | "neutral"> = {
  departure: "info",
  return: "neutral",
  payment_due: "warning",
  invoice_due: "warning",
  signing_deadline: "danger",
  task: "neutral",
  call: "success",
};

/** Events grouped by their day, days in order (the API sorts; this keeps the order and groups). */
export function byDay(events: readonly CalendarEvent[]): { date: string; events: CalendarEvent[] }[] {
  const out: { date: string; events: CalendarEvent[] }[] = [];
  for (const e of events) {
    const last = out[out.length - 1];
    if (last && last.date === e.date) last.events.push(e);
    else out.push({ date: e.date, events: [e] });
  }
  return out;
}

/** Keep only the kinds switched on (an empty set = every kind). */
export function filterKinds(events: readonly CalendarEvent[], kinds: ReadonlySet<EventKind>): CalendarEvent[] {
  return kinds.size === 0 ? [...events] : events.filter((e) => kinds.has(e.kind));
}

/** Which banner the feed card shows (card E: a revoked feed pauses the subscription; the page says so). */
export function feedState(feed: Feed | null): "none" | "active" | "revoked" | "revoked_by_owner" {
  if (!feed) return "none";
  if (feed.status === "active") return "active";
  return feed.revoked_by_owner ? "revoked_by_owner" : "revoked";
}

/** The month window starting at the first of ``month`` (YYYY-MM): [from, to] as ISO dates. */
export function monthWindow(month: string): { from: string; to: string } | null {
  const m = /^(\d{4})-(0[1-9]|1[0-2])$/.exec(month);
  if (!m) return null;
  const y = Number(m[1]);
  const mo = Number(m[2]);
  const last = new Date(Date.UTC(y, mo, 0)).getUTCDate();
  return { from: `${m[1]}-${m[2]}-01`, to: `${m[1]}-${m[2]}-${String(last).padStart(2, "0")}` };
}

/** The month (YYYY-MM) ``delta`` months away from ``month``. */
export function shiftMonth(month: string, delta: number): string {
  const [y, m] = month.split("-").map(Number);
  const d = new Date(Date.UTC(y, m - 1 + delta, 1));
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}`;
}
