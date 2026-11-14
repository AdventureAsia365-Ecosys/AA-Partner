// web/lib/academy.ts
// AA-1105 — the shapes of the CIS learning API (contract learn.http.v1, root docs/contracts/learn.http.v1.openapi.yaml)
// as the academy pages read them, and small pure helpers (status words, the retake wait, the quiz draft). The values
// in force (pass marks, retake delay, items per attempt, review cap) always come from the API's answer: the web never
// computes or stores one (they are CIS settings, cis.learn.*). Edge-safe, no React.

export type CertificateStatus = "valid" | "expired" | "renewal_due" | "revoked";
export type LessonStatus = "not_started" | "opened" | "completed";
export type LessonType = "reading" | "walkthrough" | "worked-example";
export type LearnLang = "en" | "vi";

export interface Problem {
  error: string;
  message: string;
  retry_at?: string | null;
}

export interface Me {
  learner_id: string;
  kind: "staff" | "partner";
  display_name: string | null;
  preview: boolean;
  is_test: boolean;
}

export interface PathModule {
  id: string;
  title: string | null;
  required: boolean;
  status: "active" | "draft" | "planned";
  complete: boolean;
}

export interface PathSummary {
  id: string;
  title: string;
  tier: "staff" | "partner";
  enrolled: boolean;
  completed: boolean;
  final_quiz: boolean;
  certificate_status: CertificateStatus | null;
  modules: PathModule[];
  planned?: string[];
}

export interface PathList {
  bundle_sha: string | null;
  paths: PathSummary[];
}

export interface LessonSummary {
  id: string;
  title: string;
  type: LessonType;
  minutes: number;
  stale: boolean;
  has_video: boolean;
  status: LessonStatus;
}

export interface QuizState {
  available: boolean;
  items_per_attempt: number;
  pass_mark_pct: number;
  best_score_pct: number | null;
  passed: boolean;
  retake_at: string | null;
}

export interface Module {
  id: string;
  title: string;
  version: string;
  status: "active" | "draft";
  draft: boolean;
  stale: boolean;
  objectives: string[];
  prerequisites: { id: string; state: "complete" | "open" | "planned" }[];
  complete: boolean;
  lessons: LessonSummary[];
  quiz: QuizState;
  lab: { present: boolean; state: "none" | "closed" | "open" | "passed" };
}

export interface CitedSection {
  ref: string;
  kind: "doc" | "adr" | "skill" | "steering" | "context";
  stale: boolean;
  available: boolean;
  body_md: string | null;
}

export interface Lesson {
  id: string;
  module_id: string;
  module_version: string;
  title: string;
  type: LessonType;
  minutes: number;
  objective: string;
  lang: LearnLang;
  languages: LearnLang[];
  body_md: string;
  stale: boolean;
  draft: boolean;
  has_video: boolean;
  status: LessonStatus;
  sources: CitedSection[];
}

export interface VideoUrls {
  url: string;
  expires_at: string;
  captions: { lang: LearnLang; url: string }[];
}

export interface QuizItem {
  id: string;
  kind: "single" | "multiple";
  prompt: string;
  choices: string[];
}

export interface QuizAttempt {
  attempt_id: string;
  scope: "module" | "final";
  target_id: string;
  started_at: string;
  pass_mark_pct: number;
  items: QuizItem[];
}

export interface GradedItem {
  item_id: string;
  correct: boolean;
  chosen: number[];
  correct_choices: number[];
  explain: string;
  source_ref: string;
}

export interface CertificateCode {
  certificate_id: string;
  path_id: string;
  code: string;
  verify_path: string;
}

export interface QuizResult {
  attempt_id: string;
  scope: "module" | "final";
  target_id: string;
  score_pct: number;
  pass_mark_pct: number;
  passed: boolean;
  retake_at: string | null;
  module_complete: boolean;
  items: GradedItem[];
  certificate: CertificateCode | null;
}

export interface FsrsCardJson {
  due: string;
  stability: number;
  difficulty: number;
  elapsed_days: number;
  scheduled_days: number;
  learning_steps?: number;
  reps: number;
  lapses: number;
  state: number;
  last_review: string | null;
}

export interface ReviewCard {
  module_id: string;
  item: QuizItem;
  card: FsrsCardJson;
}

export interface ReviewDue {
  daily_cap: number;
  target_retention?: number;
  cards: ReviewCard[];
}

export interface ReviewResult {
  item_id: string;
  correct: boolean;
  correct_choices: number[];
  explain: string;
  card: FsrsCardJson;
}

export interface LabResult {
  module_id: string;
  status: "passed" | "failed";
}

export interface PinnedModule {
  module_id: string;
  version: string;
}

export interface Certificate {
  certificate_id: string;
  path_id: string;
  path_title: string;
  issued_at: string;
  expires_at: string | null;
  status: CertificateStatus;
  module_versions: PinnedModule[];
}

export interface CertificateVerify {
  path_id: string;
  path_title: string;
  holder_display_name: string | null;
  issued_at: string;
  expires_at: string | null;
  status: CertificateStatus;
  module_versions: PinnedModule[];
}

/** A learner-facing error kind for a refused call (Problem.error, or the BFF's own detail). */
export type AcademyErrorKind = "not_a_learner" | "partner_not_open" | "unavailable" | "not_found" | "forbidden" | "error";

const KIND_BY_CODE: Record<string, AcademyErrorKind> = {
  not_a_learner: "not_a_learner",
  partner_not_open: "partner_not_open",
  not_found: "not_found",
  unavailable: "unavailable",
};
const KIND_BY_STATUS: Record<number, AcademyErrorKind> = { 0: "unavailable", 403: "forbidden", 404: "not_found", 502: "unavailable", 503: "unavailable", 504: "unavailable" };

export function errorKind(status: number, code: string | null | undefined): AcademyErrorKind {
  return KIND_BY_CODE[code ?? ""] ?? KIND_BY_STATUS[status] ?? "error";
}

/** The tone of a certificate status badge. */
export function certificateTone(status: CertificateStatus | null): "success" | "warning" | "danger" | "neutral" {
  if (status === "valid") return "success";
  if (status === "renewal_due") return "warning";
  if (status === "expired" || status === "revoked") return "danger";
  return "neutral";
}

/** Whether a retake time is still in the future (the API's retake_at; the UI never computes the delay). */
export function retakeWaiting(retakeAt: string | null | undefined, now: number = Date.now()): boolean {
  if (!retakeAt) return false;
  const at = Date.parse(retakeAt);
  return Number.isFinite(at) && at > now;
}

/** Lessons done / all of a module (for the progress line). */
export function lessonProgress(lessons: readonly LessonSummary[]): { done: number; total: number } {
  return { done: lessons.filter((l) => l.status === "completed").length, total: lessons.length };
}

/** Modules complete / required modules of a path (planned modules never count). */
export function pathProgress(path: PathSummary): { done: number; total: number } {
  const required = path.modules.filter((m) => m.required && m.status !== "planned");
  return { done: required.filter((m) => m.complete).length, total: required.length };
}

/** Toggle one choice in a draft answer: one choice for a single-answer item, a set for a multiple-answer one. */
export function toggleChoice(current: readonly number[], index: number, kind: QuizItem["kind"]): number[] {
  if (kind === "single") return [index];
  return current.includes(index) ? current.filter((i) => i !== index) : [...current, index].sort((a, b) => a - b);
}

/** The submission body of an attempt: every item, unanswered ones with no choice (the server counts them wrong). */
export function quizResponses(items: readonly QuizItem[], answers: Record<string, number[]>): { item_id: string; chosen: number[] }[] {
  return items.map((it) => ({ item_id: it.id, chosen: [...(answers[it.id] ?? [])] }));
}

/** The academy URL of a public verify path the API returned ("/verify/<code>"), or null for any other shape. */
export function verifyUrl(origin: string, verifyPath: string): string | null {
  return /^\/verify\/[A-Za-z0-9_-]{22}$/.test(verifyPath) ? `${origin}${verifyPath}` : null;
}

/** The lessons before and after `id` in its module's order (null at either end, or when `id` is not in it). */
export function lessonNeighbours(order: readonly string[], id: string): { prev: string | null; next: string | null } {
  const at = order.indexOf(id);
  if (at < 0) return { prev: null, next: null };
  return { prev: order[at - 1] ?? null, next: order[at + 1] ?? null };
}
