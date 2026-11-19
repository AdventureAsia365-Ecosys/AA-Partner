// web/lib/accessLog.ts
// AA-940 (CRM-12) — the agency's access log (A-18 Settings > Data & privacy, design v3 §4.9, crm-design-final WF-8):
// the types GET /crm/v1/audit/events returns, the filter <-> query mapping, and the EN/VI wording of one event line
// ("20 Oct 14:02  Minh revealed passport (Tom) - reason: supplier booking" in the wireframe; the log carries codes
// and random ids only, so the line names the member by the member rule and the object by a plain noun, never an id or a
// traveller's name; AA-1129: every code the API writes has words in lib/i18nAuditLog.ts). Owner only. Edge-safe,
// unit-tested.

import { type DateLocale } from "./dates";
import { fmt, type Lang, type MessageKey, MESSAGES, plural, t } from "./i18n";
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

/** The object's noun ("trip", "the agency"), or null for a kind the web has no word for. */
function noun(lang: Lang, kind: string): string | null {
  const key = `log.obj.${kind}`;
  return has(key) ? t(lang, key) : null;
}

const KEY_VERSION = /^[a-z][a-z_]*:v\d+$/;

/** "a" or "an" before an English noun: "an invoice", "an AI draft", "an SMS …", "a trip". */
export function article(word: string): string {
  const first = word.split(/\s/)[0] ?? "";
  const acronym = first.length > 1 && first === first.toUpperCase() && /^[A-Z]/.test(first);
  return (acronym ? /^[AEFHILMNORSX]/ : /^[aeio]/i).test(first) ? "an" : "a";
}

/** The object of a line, never an id (AA-1129, S285: was "trip f0a838d8", "the agency bab16039"). A member object is
 *  named by the member rule: the actor itself is "the agency" ("signed in to the agency"), another member "team member
 *  Agent 2", a list of members "team members". Any other object is a plain noun: "a trip" for one object (the line
 *  links to it when it has a page), "the agency" for the one of its kind; Vietnamese has no article. */
function objectText(e: AuditEventView, lang: Lang, members?: MemberDirectory | null): string {
  if (e.object_kind === "member") {
    if (!e.object_id) return t(lang, "log.obj.members");
    if (e.object_id === e.member_id) return t(lang, "log.obj.ownAgency");
    return `${t(lang, "log.obj.member")} ${memberName(e.object_id, null, lang, members)}`;
  }
  const word = noun(lang, e.object_kind);
  return word === null ? t(lang, "log.obj.unknown") : nounPhrase(e, lang, word);
}

/** "a trip" (one object), "the agency" (the one of its kind), "version vault:v1" (a key version is a name the
 *  agency's key page shows, not a random id); Vietnamese has no article. */
function nounPhrase(e: AuditEventView, lang: Lang, word: string): string {
  if (e.object_kind === "tenant_key" && e.object_id && KEY_VERSION.test(e.object_id)) return `${word} ${e.object_id}`;
  if (lang === "vi" || /^the\s/i.test(word)) return word;
  return e.object_id ? `${article(word)} ${word}` : `the ${word}`;
}

/** "viewed the trip list" for a list view (or a view of no one object). */
function listText(e: AuditEventView, lang: Lang): string {
  const own = `log.list.${e.object_kind}`;
  if (has(own)) return t(lang, own);
  const word = noun(lang, e.object_kind);
  return word === null ? t(lang, "log.listAny") : fmt(t(lang, "log.listOf"), { obj: word.replace(/^the\s+/i, "") });
}

/** The sentence without the details: a list view, a whole sentence of the action (`log.say.*`, which may name the
 *  object as {obj}), or the action's verb and the object. An unknown code reads "worked on a record", never a code. */
function headText(e: AuditEventView, lang: Lang, members?: MemberDirectory | null): string {
  if (e.action === "list_view" || (e.action === "view" && !e.object_id && e.object_kind !== "member")) return listText(e, lang);
  const say = `log.say.${e.action}`;
  if (has(say)) return fmt(t(lang, say), { obj: objectText(e, lang, members) });
  const act = `log.act.${e.action}`;
  return `${t(lang, has(act) ? act : "log.act.unknown")} ${objectText(e, lang, members)}`;
}

/** The details shown: their labels; codes the web has no word for are counted ("2 other details"), never printed. */
function fieldsText(fields: readonly string[], lang: Lang): string {
  const labels: string[] = [];
  let other = 0;
  for (const f of fields) {
    const key = `field.${f.replace(/_enc$/, "")}`;
    if (has(key)) labels.push(t(lang, key));
    else other += 1;
  }
  if (other) labels.push(plural(lang, "log.moreFields", other));
  return labels.join(", ");
}

/** What happened, without the who: "revealed a client · Passport number · reason: Prepare a booking · 1 item shown".
 *  Every part is words from the messages: no id, no raw code, no raw field list (AA-1129, S285 live check). A reason
 *  the web has no word for is left out (the CSV / JSON download keeps every code). */
export function eventText(e: AuditEventView, lang: Lang, members?: MemberDirectory | null): string {
  const parts = [headText(e, lang, members).trim()];
  const fields = fieldsText(e.fields, lang);
  if (fields) parts.push(fields);
  const reasonKey = e.reason ? `why.${e.reason}` : "";
  if (reasonKey && has(reasonKey)) parts.push(fmt(t(lang, "log.reason"), { reason: t(lang, reasonKey) }));
  if (e.object_ids.length) parts.push(plural(lang, "log.items", e.object_ids.length));
  return parts.join(" · ");
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
