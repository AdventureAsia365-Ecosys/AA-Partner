// web/lib/tags.ts
// AA-960 (card CRM-29b; design v3 R3, R29, R30, R36) — the types of the tag routes (GET /crm/v1/tags, the tags of a trip
// or client, POST /tags/{id}/bulk, GET /marketing/ready-to-travel, GET /workflows/coverage), how a tag and a reason are
// worded (a system tag by its code: tag.sys.<code>; an agency tag by its own name), who may manage (owner) or bulk
// apply (owner, agent), and the refusal wording keys. Edge-safe, unit-tested (tests/tags.test.ts).
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import type { Role } from "@/lib/nav";

export type Subject = "trip" | "client";
export const TONES = ["neutral", "accent", "success", "warning", "danger", "info", "purple"] as const;
export type TagTone = (typeof TONES)[number];

export interface Tag {
  tag_id: string;
  kind: "agency" | "system";
  applies_to: Subject;
  code: string;
  name: string;
  tone: TagTone;
  archived: boolean;
  uses?: number;
}

export interface TagVocabulary {
  tags: Tag[];
  tones: TagTone[];
  can_manage: boolean;
  can_bulk: boolean;
}

export interface Reason {
  code: string;
  [key: string]: string | number;
}

export interface TagLink {
  tag_link_id: string;
  tag_id: string;
  kind: "agency" | "system";
  code: string;
  name: string;
  tone: TagTone;
  archived: boolean;
  source: "member" | "system";
  reasons: Reason[];
  since: string | null;
}

/** A tag as a list or a board shows it (GET /travelers, the pipeline board). */
export interface TagChip {
  code: string;
  name: string;
  tone: TagTone;
  kind: "agency" | "system";
}

export interface BulkSelection {
  ids?: string[];
  filter?: { stage?: string; owner?: string; tag?: string; archived?: boolean };
}

export interface BulkResult {
  tag_id: string;
  action: "add" | "remove";
  applies_to: Subject;
  total: number;
  count: number;
  already: number;
  held: number;
  archived_suppressed: number;
  sample: string[];
  dry_run: boolean;
  workflows_started?: number;
}

export interface ReadyItem {
  traveler_id: string;
  display_name: string;
  owner_member_id: string | null;
  is_mine: boolean;
  reasons: Reason[];
  since: string | null;
  marketing_consent: "yes" | "no" | "unknown";
  last_trip: { trip_id: string; title: string; start_date: string | null; end_date: string | null } | null;
}

export interface ReadyList {
  items: ReadyItem[];
  total: number;
  page: number;
  has_more: boolean;
  ready_after_months: number;
  filter: { owner: "anyone" | "me" };
}

export interface CoverageRule {
  workflow_rule_id: string;
  name: string;
  template_key: string | null;
  trigger: { kind: string; value: string | null };
  stage: string | null;
  missing_count: number;
  trip_ids: string[];
}

export interface Coverage {
  rules: CoverageRule[];
  by_trip: Record<string, string[]>;
}

export const READY_TAG = "sys_ready_to_travel";
export const SYSTEM_PREFIX = "sys_";

/** The words of a tag: a system tag by its code (the web's own wording, EN / VI), an agency tag by its name. */
export function tagLabel(lang: Lang, tag: { code: string; name: string; kind: string }): string {
  if (tag.kind !== "system") return tag.name;
  const text: string | undefined = t(lang, `tag.sys.${tag.code}` as MessageKey);
  return typeof text === "string" ? text : tag.name;
}

/** One reason of a system tag in words ("Came home 14 months ago", "AA tour T-100"). */
export function reasonText(lang: Lang, reason: Reason): string {
  const text: string | undefined = t(lang, `tag.reason.${reason.code}` as MessageKey);
  if (typeof text !== "string") return reason.code;
  const vars: Record<string, string | number> = {};
  for (const [k, v] of Object.entries(reason)) if (k !== "code") vars[k] = v;
  return fmt(text, vars);
}

export function canManageTags(role: Role): boolean {
  return role === "owner";
}

export function canBulkTag(role: Role): boolean {
  return role === "owner" || role === "agent";
}

export function seesMarketing(role: Role): boolean {
  return role === "owner" || role === "agent";
}

const ERRORS = [
  "tag_exists",
  "tag_archived",
  "system_tag",
  "tag_subject_mismatch",
  "too_many_tags",
  "count_changed",
  "too_many_records",
  "selection_required",
  "legal_hold",
  "text_required",
  "text_too_long",
  "invalid_text",
  "role_forbidden",
] as const;

export function tagErrorKey(detail: string): MessageKey {
  return ((ERRORS as readonly string[]).includes(detail) ? `tag.err.${detail}` : "tag.err.generic") as MessageKey;
}

/** The preview sentence of a bulk apply (card E): the count first, then what is skipped or suppressed. */
export function bulkSummary(lang: Lang, r: BulkResult, tagName: string): string[] {
  const subject = t(lang, r.applies_to === "trip" ? "tag.subject.trips" : "tag.subject.clients");
  const out = [fmt(t(lang, r.action === "add" ? "tag.bulk.willAdd" : "tag.bulk.willRemove"), { n: r.count, tag: tagName, subject })];
  if (r.already) out.push(fmt(t(lang, r.action === "add" ? "tag.bulk.alreadyHave" : "tag.bulk.alreadyWithout"), { n: r.already }));
  if (r.held) out.push(fmt(t(lang, "tag.bulk.held"), { n: r.held }));
  if (r.archived_suppressed) out.push(fmt(t(lang, "tag.bulk.archived"), { n: r.archived_suppressed }));
  return out;
}

export function consentKey(state: ReadyItem["marketing_consent"]): MessageKey {
  return `tag.ready.consent.${state}` as MessageKey;
}

/** The query of the clients directory with a tag filter kept (lib/clients directoryHref adds the rest). */
export function withTag(href: string, tag: string | null | undefined): string {
  if (!tag) return href;
  return `${href}${href.includes("?") ? "&" : "?"}tag=${encodeURIComponent(tag)}`;
}
