// web/lib/planningForm.ts
// AA-943 (CRM-15, C-02; spec §5 "Stage 2", R31) — the Stage 2 planning form as the API serves it (`stage2.v1`: pages,
// fields, `show_if` branches; API app/intake/stage2.py) and the pure rules the page applies before it posts:
//
//   - a branch shows only when its rule holds: the altitude / trek questions for an adventure trip, the health page
//     only after the client ticked the health-data consent (the consent page comes first);
//   - the body carries the answers of the visible fields only (the server evaluates the branches again and drops any
//     hidden answer anyway, counting it in form_submission.hidden_answers_dropped);
//   - a refused answer sends the person to the page of the first refused field.
//
// The same component serves the client's signed link (/c/s/<token>) and the agent's "Fill in from call". Codes only
// here; the page words them (EN by default, VI on request). Edge-safe, unit-tested.

export type FieldType = "select" | "boolean" | "multi_select" | "long_text" | "consent";

export interface ShowIf {
  field: string;
  in: unknown[];
}

export interface PlanField {
  name: string;
  type: FieldType;
  required: boolean;
  options?: string[];
  max_items?: number;
  max_length?: number;
  show_if?: ShowIf;
  /** consent fields: what the consent is for (health_data). */
  purpose?: string;
}

export interface PlanPage {
  key: string;
  fields: PlanField[];
  show_if?: ShowIf;
}

/** GET /crm/public/v1/s/<token> (API app/intake/stage2_public.py read). */
export interface PublicPlanForm {
  schema_key: string;
  version: number;
  pages: PlanPage[];
  trip_style: string | null;
  agency_name: string;
  /** The agency's data language; NOT the page language (owner rule S249). */
  default_locale: string;
  /** Bands and codes the client gave before (F2 only, rule 4). */
  prefill: Record<string, unknown>;
  submitted: boolean;
}

export type Answers = Record<string, unknown>;

/** The signed link's token in the path: 32 random bytes, base64url (43 characters, API LinkToken). */
export const LINK_TOKEN = /^[A-Za-z0-9_-]{43}$/;

function context(tripStyle: string | null, answers: Answers): Record<string, unknown> {
  return { trip_style: tripStyle, health_consent: answers.health_consent === true };
}

function holds(rule: ShowIf | undefined, ctx: Record<string, unknown>): boolean {
  if (!rule) return true;
  return Array.isArray(rule.in) && rule.in.includes(ctx[rule.field]);
}

/** The pages a person sees for this trip and these answers, each with its visible fields; empty pages are left out. */
export function visiblePages(pages: PlanPage[], tripStyle: string | null, answers: Answers): PlanPage[] {
  const ctx = context(tripStyle, answers);
  return pages
    .filter((p) => holds(p.show_if, ctx))
    .map((p) => ({ ...p, fields: p.fields.filter((f) => holds(f.show_if, ctx)) }))
    .filter((p) => p.fields.length > 0);
}

export function visibleFieldNames(pages: PlanPage[], tripStyle: string | null, answers: Answers): Set<string> {
  return new Set(visiblePages(pages, tripStyle, answers).flatMap((p) => p.fields.map((f) => f.name)));
}

function empty(value: unknown): boolean {
  return value === undefined || value === null || value === "" || (Array.isArray(value) && value.length === 0);
}

/** Known answers of the definition only (a prefill or an earlier draft may carry keys this version no longer has). */
export function knownAnswers(pages: PlanPage[], raw: Record<string, unknown> | null | undefined): Answers {
  const names = new Set(pages.flatMap((p) => p.fields.map((f) => f.name)));
  return Object.fromEntries(Object.entries(raw ?? {}).filter(([k, v]) => names.has(k) && !empty(v)));
}

/** The request body: visible, non-empty answers only, the consent as a boolean, and the page's submission id. */
export function answerBody(pages: PlanPage[], tripStyle: string | null, answers: Answers, submissionId: string): Answers {
  const shown = visibleFieldNames(pages, tripStyle, answers);
  const body: Answers = {};
  for (const [name, value] of Object.entries(answers)) {
    if (!shown.has(name) || empty(value)) continue;
    body[name] = typeof value === "string" ? value.trim() : value;
  }
  if (shown.has("health_consent")) body.health_consent = answers.health_consent === true;
  if (submissionId) body.client_submission_id = submissionId;
  return body;
}

/** The index (in the visible pages) of the first page holding one of `fields`, or null. */
export function firstErrorPage(visible: PlanPage[], fields: string[]): number | null {
  const bad = new Set(fields);
  const i = visible.findIndex((p) => p.fields.some((f) => bad.has(f.name)));
  return i < 0 ? null : i;
}

/** Toggle one code of a multi-select, keeping at most `max` codes (the API refuses more). */
export function toggleCode(current: unknown, code: string, max: number | undefined): string[] {
  const list = Array.isArray(current) ? current.filter((c): c is string => typeof c === "string") : [];
  if (list.includes(code)) return list.filter((c) => c !== code);
  if (max !== undefined && list.length >= max) return list;
  return [...list, code];
}

/** The message key prefix that words a field's options (codes shared with the F2 profile labels). */
export const OPTION_PREFIX: Record<string, string> = {
  accommodation_tier: "tier",
  group_pref: "group",
  fitness_band: "fitness",
  trek_experience: "trek",
  interests: "interest",
  must_do_codes: "interest",
  must_avoid_codes: "avoid",
  occasion: "occasion",
  budget_flex: "flex",
  decision_maker: "decider",
  booking_timeline: "timeline",
  dietary_flags: "dietary",
  mobility_flags: "mobility",
};
