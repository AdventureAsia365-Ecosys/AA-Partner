// web/lib/accessLog.ts
// AA-940 (CRM-12) — the agency's access log (A-18 Settings > Data & privacy, design v3 §4.9, crm-design-final WF-8):
// the types GET /crm/v1/audit/events returns, the filter <-> query mapping, and the EN/VI wording of one event line
// ("20 Oct 14:02  Minh revealed passport (Tom) - reason: supplier booking" in the wireframe; the log carries codes
// and random ids only, so the line names the member's role and a short id, never a traveller's name). Owner only.
// Edge-safe, unit-tested.

import { type DateLocale } from "./dates";
import { fmt, type Lang, type MessageKey, MESSAGES, t } from "./i18n";
import { type PersonRef, personLabel } from "./people";

export const ACTOR_GROUPS = ["user", "ai", "aa_ops", "aa_support", "aa_emergency", "system"] as const;
export type ActorGroup = (typeof ACTOR_GROUPS)[number];
export const OUTCOMES = ["allowed", "denied"] as const;
export type Outcome = (typeof OUTCOMES)[number];
/** Actions the filter offers (every code the API writes today, design §4.9); unknown codes still display. */
export const ACTIONS = [
  "sign_in",
  "view",
  "list_view",
  "reveal",
  "create",
  "update",
  "delete",
  "decrypt",
  "stage_change",
  "assign",
  "party_add",
  "party_remove",
  "intake_received",
  "intake_created",
  "intake_attached",
  "intake_failed",
  "age_band_recompute",
  "ai_context_build",
  "key_create",
  "key_rotate",
  "key_rewrap",
  "key_destroy",
  "audit_view",
  "audit_export",
  "self_view",
  "report_export",
  // AA-1030: the agency's AA prices opened or downloaded (owner / agent).
  "aa_tariff_view",
  // AA-971: AA support (L2) and emergency (L3) access.
  "support_grant",
  "support_revoke",
  "support_session_start",
  "support_session_end",
  "support_read",
  "breakglass_open",
  "breakglass_close",
  "breakglass_owner_notice",
  // AA-956: AA ground operations (Module 8): AA ops viewed a trip's details / exported a supplier list; the release
  // created, withdrawn and deleted.
  "ops_release_view",
  "ops_manifest_export",
  "ops_release_create",
  "ops_release_withdraw",
  "ops_release_purge",
] as const;
export const EXPORT_FORMATS = ["csv", "jsonl"] as const;
export type ExportFormat = (typeof EXPORT_FORMATS)[number];

export interface AuditEventView {
  event_id: string;
  at: string;
  actor_kind: string;
  actor_group: ActorGroup;
  actor_ref: string;
  member_id: string | null;
  member_role: "owner" | "agent" | "assistant" | null;
  action: string;
  outcome: Outcome;
  object_kind: string;
  object_id: string | null;
  fields: string[];
  reason: string | null;
  object_ids: string[];
}

export interface AuditPage {
  events: AuditEventView[];
  next_cursor: string | null;
  retention_months: number;
  oldest: string;
  /** AA-1129 (S283 row 10): the agency's IANA timezone; the lines print their time in it (absent from an older API). */
  timezone?: string;
}

/** AA-1129: the agency's members by id (the members API: name, role, place in the role), for the member label rule. */
export type MemberDirectory = ReadonlyMap<string, PersonRef>;

/** The directory of the members API's list (`GET /members`); null when that call failed, so a line names the role alone. */
export function memberDirectory(
  members: readonly (PersonRef & { member_id: string })[] | null | undefined,
): MemberDirectory | null {
  return members ? new Map(members.map((m) => [m.member_id, { role: m.role, display_name: m.display_name, seq: m.seq }])) : null;
}

export interface LogFilter {
  actor?: ActorGroup;
  action?: string;
  outcome?: Outcome;
  from?: string; // YYYY-MM-DD
  to?: string;
}

const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/;
const CODE = /^[a-z][a-z0-9_]{0,39}$/;

/** Only well-formed values reach the query (the API validates again). */
export function cleanFilter(raw: Record<string, string | string[] | undefined>): LogFilter {
  const one = (k: string) => {
    const v = raw[k];
    return typeof v === "string" ? v : undefined;
  };
  const out: LogFilter = {};
  const actor = one("actor");
  if (actor && (ACTOR_GROUPS as readonly string[]).includes(actor)) out.actor = actor as ActorGroup;
  const action = one("action");
  if (action && CODE.test(action)) out.action = action;
  const outcome = one("outcome");
  if (outcome && (OUTCOMES as readonly string[]).includes(outcome)) out.outcome = outcome as Outcome;
  const from = one("from");
  if (from && ISO_DAY.test(from)) out.from = from;
  const to = one("to");
  if (to && ISO_DAY.test(to)) out.to = to;
  return out;
}

export function filterQuery(f: LogFilter, extra: Record<string, string | undefined> = {}): URLSearchParams {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries({ ...f, ...extra })) if (v) q.set(k, v);
  return q;
}

/** "audit/events?actor=ai&cursor=…" for the BFF (lib/bff.ts allow-list). */
export function eventsPath(f: LogFilter, cursor?: string | null): string {
  const q = filterQuery(f, { cursor: cursor ?? undefined, limit: "50" });
  return `audit/events?${q.toString()}`;
}

export function exportPath(f: LogFilter, format: ExportFormat): string {
  return `audit/events/export?${filterQuery(f, { format }).toString()}`;
}

export function exportFilename(format: ExportFormat, now: Date = new Date()): string {
  return `access-log-${now.toISOString().slice(0, 10).replace(/-/g, "")}.${format}`;
}

function has(key: string): key is MessageKey {
  return key in MESSAGES.en;
}

/** The label of a code from the ``prefix.code`` messages, else the code itself (a newer API code). */
export function codeLabel(lang: Lang, prefix: string, code: string | null): string {
  if (!code) return "";
  const key = `${prefix}.${code}`;
  return has(key) ? t(lang, key) : code.replace(/_/g, " ");
}

export function shortId(id: string | null): string {
  return id ? id.replace(/^[a-z_]+:/, "").slice(0, 8) : "";
}

/** Who did it, by the member label rule of lib/people.ts (AA-1129, S285): "lan.nguyen · Owner", "Agent 2", "Former
 *  member" (a member the agency no longer has), never an id; without the directory (the members API did not answer)
 *  the role alone. Else "AI (crm_draft)", "System", "AA operations", … */
export function actorText(e: AuditEventView, lang: Lang, members?: MemberDirectory | null): string {
  if (e.member_id) return memberName(e.member_id, e.member_role, lang, members);
  if (e.actor_group === "ai") return fmt(t(lang, "log.actor.aiStage"), { stage: e.actor_ref.replace(/^ai:/, "") });
  return codeLabel(lang, "log.actor", e.actor_group);
}

function memberName(id: string, role: AuditEventView["member_role"], lang: Lang, members?: MemberDirectory | null): string {
  const known = members?.get(id);
  if (known) return personLabel(lang, known);
  return members || !role ? personLabel(lang, null) : personLabel(lang, { role });
}

/** The object of a line. A member object is named by the member rule: the actor itself is "the agency" ("signed in to
 *  the agency"), another member "team member Agent 2", a list of members "team members" (AA-1129: was "the agency"). */
function objectText(e: AuditEventView, lang: Lang, members?: MemberDirectory | null): string {
  if (e.object_kind === "member") {
    if (!e.object_id) return t(lang, "log.obj.members");
    if (e.object_id === e.member_id) return t(lang, "log.obj.ownAgency");
    return `${t(lang, "log.obj.member")} ${memberName(e.object_id, null, lang, members)}`;
  }
  const id = e.object_kind === "tenant_key" ? e.object_id : shortId(e.object_id);
  return e.object_id ? `${codeLabel(lang, "log.obj", e.object_kind)} ${id}` : codeLabel(lang, "log.obj", e.object_kind);
}

/** What happened, without the who: "revealed Passport number of client 0f8fad5b (reason: Prepare a booking)". A list
 *  view reads "viewed the list of trips" (AA-1129, S285: was "viewed the list the agency"). */
export function eventText(e: AuditEventView, lang: Lang, members?: MemberDirectory | null): string {
  const listKey = `log.list.${e.object_kind}`;
  const head =
    e.action === "list_view"
      ? has(listKey)
        ? t(lang, listKey)
        : fmt(t(lang, "log.list.other"), { obj: objectText(e, lang, members) })
      : `${codeLabel(lang, "log.act", e.action)} ${objectText(e, lang, members)}`;
  const fields = e.fields.length ? ` · ${e.fields.map((f) => codeLabel(lang, "field", f.replace(/_enc$/, ""))).join(", ")}` : "";
  const reasonCode = e.reason ? codeLabel(lang, "why", e.reason) : "";
  const reason = reasonCode ? ` · ${fmt(t(lang, "log.reason"), { reason: reasonCode })}` : "";
  const count = e.object_ids.length ? ` · ${fmt(t(lang, "log.items"), { n: e.object_ids.length })}` : "";
  return `${head.trim()}${fields}${reason}${count}`.trim();
}

/** "20 Oct 2026 14:02" in the person's date locale, the time in the AGENCY's timezone (24 h). The server render and the
 *  browser print the same text (AA-1129, S283 row 10: the browser's own zone made React hydration error #418). An
 *  unknown zone prints UTC. */
export function whenText(at: string, locale: DateLocale, timeZone = "UTC"): string {
  const d = new Date(at);
  if (Number.isNaN(d.getTime())) return "";
  const write = (zone: string) => {
    const day = new Intl.DateTimeFormat(locale, { day: "numeric", month: "short", year: "numeric", timeZone: zone }).format(d);
    const time = new Intl.DateTimeFormat(locale, { hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZone: zone }).format(d);
    return `${day} ${time}`;
  };
  try {
    return write(timeZone);
  } catch {
    return write("UTC");
  }
}

/** Where an object id links to inside the CRM (traveller and trip pages), else null. */
export function objectHref(e: AuditEventView): string | null {
  if (!e.object_id || !/^[0-9a-f-]{36}$/.test(e.object_id)) return null;
  if (e.object_kind === "traveler") return `/clients/${e.object_id}`;
  if (e.object_kind === "trip") return `/pipeline/${e.object_id}`;
  return null;
}
