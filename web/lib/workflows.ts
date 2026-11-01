// web/lib/workflows.ts
// AA-959 (card CRM-29a, Module 10) — the types of the task and automation routes (GET /crm/v1/tasks, /workflows,
// /workflows/runs), the A-09 buckets, how a task and a rule are worded, who may edit (owner) or apply (owner, agent;
// design §4.6), and the refusal wording keys. Edge-safe, unit-tested (tests/workflows.test.ts).
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import type { Role } from "@/lib/nav";

export const BUCKETS = ["overdue", "today", "upcoming", "later", "waiting_for_dates"] as const;
export type Bucket = (typeof BUCKETS)[number];
export type TaskSource = "workflow" | "manual" | "aa_event" | "system";

export interface TaskItem {
  task_id: string;
  kind: string;
  /** A custom task's own title (kind custom_<hex>); null for a coded kind (worded by task.<kind>). */
  title: string | null;
  priority: "normal" | "urgent";
  status: "open" | "done" | "skipped";
  assignee_member_id: string | null;
  due_at: string | null;
  due_anchor: string | null;
  due_offset_days: number | null;
  due_overridden: boolean;
  done_at: string | null;
  created_at: string;
  trip_id: string | null;
  trip_title: string | null;
  trip_stage: string | null;
  traveler_id: string | null;
  workflow_run_id: string | null;
  source: TaskSource;
  bucket?: Bucket;
  flights?: { flight_no: string; airport_iata: string; sched_local: string; tz: string; status_url: string | null }[];
}

export interface TaskList {
  scope: "mine" | "team" | "trip" | "client";
  status: "open" | "done";
  timezone: string;
  digest: boolean;
  can_set_digest: boolean;
  buckets?: Record<Bucket, TaskItem[]>;
  counts?: Record<Bucket, number>;
  done?: TaskItem[];
}

export interface Trigger {
  kind: "stage_enter" | "event" | "date_anchor" | "special_date" | "manual";
  value: string | null;
  offset_days: number;
}

export type Step = { action: "create_task" | "send_email" | "send_text" | "set_field" | "wait" } & Record<string, unknown>;

/** A client's birthday or trip anniversary: every message of the rule is marketing (sent only with consent); the API
 * refuses "transactional" there (422 marketing_only, api/app/workflows/model.py marketing_only). */
export const GREETING_DATES = ["birthday", "trip_anniversary"] as const;
export function isGreeting(trigger: Pick<Trigger, "kind" | "value">): boolean {
  return trigger.kind === "special_date" && (GREETING_DATES as readonly string[]).includes(String(trigger.value));
}

/** The actions a rule's step may take for its trigger: a task list never sends, a client's date sets no trip field,
 * and a rule started by a sent text sends no text (its own text would start it again: 422 message_loop). */
export function actionsFor(trigger: Pick<Trigger, "kind" | "value">): Step["action"][] {
  if (trigger.kind === "manual") return ["create_task", "set_field", "wait"];
  if (trigger.kind === "special_date") return ["create_task", "send_email", "send_text", "wait"];
  if (trigger.kind === "event" && trigger.value === "message.sent") return ["create_task", "send_email", "set_field", "wait"];
  return ["create_task", "send_email", "send_text", "set_field", "wait"];
}

export interface Rule {
  workflow_rule_id: string;
  name: string;
  template_key: string | null;
  template_version: number | null;
  trigger: Trigger;
  conditions: Record<string, unknown>;
  steps: Step[];
  status: "active" | "paused" | "archived";
  version: number;
  templates_missing: number[];
  active_runs?: number;
  finished_runs?: number;
}

export interface LibraryTemplate {
  key: string;
  version: number;
  name: string;
  trigger: Trigger;
  conditions: Record<string, unknown>;
  steps: Step[];
  needs_templates: boolean;
  installed: boolean;
}

export interface WorkflowOptions {
  trigger_kinds: string[];
  events: string[];
  date_anchors: string[];
  special_dates: string[];
  due_anchors: string[];
  task_kinds: string[];
  merge_fields: string[];
  waiting_on: string[];
  condition_keys: string[];
}

export interface WorkflowOverview {
  rules: Rule[];
  library: LibraryTemplate[];
  can_edit: boolean;
  can_apply: boolean;
  options: WorkflowOptions;
}

export interface RunLogEntry {
  step: number;
  action: string;
  outcome: string;
  at: string;
  task_id?: string;
  message_id?: string;
}

export interface Run {
  workflow_run_id: string;
  workflow_rule_id: string;
  rule_name: string;
  template_key: string | null;
  trip_id: string | null;
  traveler_id: string | null;
  trigger: string;
  status: "active" | "completed" | "stopped" | "failed";
  step_index: number;
  step_count: number;
  next_at: string | null;
  stop_reason: string | null;
  log: RunLogEntry[];
  created_at: string;
  finished_at: string | null;
}

export const STAGES = ["inquiry", "qualifying", "proposal", "booked", "pre_departure", "travelling", "completed", "lost"] as const;

export function canEdit(role: Role): boolean {
  return role === "owner";
}

export function canApply(role: Role): boolean {
  return role === "owner" || role === "agent";
}

function label(lang: Lang, key: string, fallback: string): string {
  const text = t(lang, key as MessageKey);
  return text && text !== key ? text : fallback;
}

/** A task's line: its own title (custom), else the label of its kind, else the code made readable. */
export function taskTitle(lang: Lang, task: Pick<TaskItem, "kind" | "title">): string {
  if (task.title) return task.title;
  return label(lang, `task.${task.kind}`, task.kind.replace(/_/g, " "));
}

/** The library template's name and description in the reader's language (the API's name is the English default). */
export function templateName(lang: Lang, tpl: Pick<LibraryTemplate, "key" | "name">): string {
  return label(lang, `wf.tpl.${tpl.key}`, tpl.name);
}

export function templateDescription(lang: Lang, key: string): string {
  return label(lang, `wf.tpl.${key}.about`, "");
}

/** "When a trip enters Booked", "2 days before departure", "On the client's birthday", "Applied by hand". */
export function triggerText(lang: Lang, trigger: Trigger): string {
  const value = trigger.value ?? "";
  if (trigger.kind === "manual") return t(lang, "wf.trigger.manual");
  if (trigger.kind === "stage_enter") return fmt(t(lang, "wf.trigger.stage_enter"), { stage: label(lang, `stage.${value}`, value) });
  if (trigger.kind === "event") return fmt(t(lang, "wf.trigger.event"), { event: label(lang, `wf.event.${value}`, value) });
  const anchor = label(lang, `wf.anchor.${value}`, value);
  return offsetText(lang, trigger.offset_days, anchor);
}

export function offsetText(lang: Lang, days: number, anchor: string): string {
  if (days === 0) return fmt(t(lang, "wf.offset.on"), { anchor });
  const n = Math.abs(days);
  return fmt(t(lang, days < 0 ? "wf.offset.before" : "wf.offset.after"), { n, anchor });
}

/** One step in a sentence (the rule card and the run log). */
export function stepText(lang: Lang, step: Step): string {
  switch (step.action) {
    case "create_task": {
      const title = step.kind === "custom" ? String(step.title ?? "") : label(lang, `task.${String(step.kind)}`, String(step.kind));
      return fmt(t(lang, "wf.step.create_task"), { title });
    }
    case "send_email":
      return t(lang, step.purpose === "marketing" ? "wf.step.send_email_marketing" : "wf.step.send_email");
    case "send_text":
      return fmt(t(lang, "wf.step.send_text"), { channel: label(lang, `msg.channel.${String(step.channel)}`, String(step.channel)) });
    case "set_field":
      return step.field === "owner"
        ? t(lang, "wf.step.set_owner")
        : fmt(t(lang, "wf.step.set_waiting_on"), { value: label(lang, `waiting.${String(step.value)}`, String(step.value)) });
    case "wait": {
      const until = step.until as { anchor?: string; offset_days?: number } | undefined;
      if (until) return fmt(t(lang, "wf.step.wait_until"), { when: offsetText(lang, until.offset_days ?? 0, label(lang, `wf.anchor.${until.anchor}`, String(until.anchor))) });
      return fmt(t(lang, "wf.step.wait"), { days: Number(step.days ?? 0), hours: Number(step.hours ?? 0) });
    }
    default:
      return String((step as { action?: string }).action ?? "");
  }
}

export function outcomeText(lang: Lang, outcome: string): string {
  return label(lang, `wf.outcome.${outcome}`, outcome.replace(/_/g, " "));
}

export function stopText(lang: Lang, reason: string | null): string {
  return reason ? label(lang, `wf.stop.${reason}`, reason.replace(/_/g, " ")) : "";
}

const ERRORS = new Set([
  "templates_missing",
  "template_not_found",
  "member_not_found",
  "version_conflict",
  "template_installed",
  "workflow_archived",
  "workflow_not_active",
  "workflow_not_for_trips",
  "run_not_active",
  "task_closed",
  "title_fixed",
  "role_forbidden",
  "condition_not_available",
  "marketing_only",
  "message_loop",
]);

/** The wording key of a refusal of these routes (a 422 with a field path reads as "check the highlighted field"). */
export function workflowErrorKey(detail: string): MessageKey {
  if (ERRORS.has(detail)) return `wf.error.${detail}` as MessageKey;
  return "wf.error.generic";
}

/** The bucket a due time falls in, the way the API puts it (for a task added or edited on the page). */
export function bucketOf(dueAt: string | null, now: Date, timeZone: string): Bucket {
  if (!dueAt) return "waiting_for_dates";
  const due = new Date(dueAt);
  if (due.getTime() < now.getTime()) return "overdue";
  const day = (d: Date) => new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" }).format(d);
  const days = Math.round((Date.parse(day(due)) - Date.parse(day(now))) / 86_400_000);
  if (days <= 0) return "today";
  if (days <= 7) return "upcoming";
  return "later";
}
