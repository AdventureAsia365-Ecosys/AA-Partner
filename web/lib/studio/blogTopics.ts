// AA-1081 — the Articles page's "tours without an article topic" note: the pure part.
//
// GET /v1/slate returns `blog_topics` (CIS services/acp_shared/slate.py::tenant_blog_topics): per tour the agency
// adopted, its catalog name, how many Blog topics it has, and when it has none a typed reason code
// (slate.NO_TOPIC_REASONS). The codes are the API contract; the words for the agency live in BLOG_TOPICS_MESSAGES
// (tenant sees content only: no "bar", score, Debate or gate). An unknown code falls back to the generic line.

export const NO_TOPIC_REASONS = [
  "not_ready",
  "low_search_interest",
  "no_place_match",
  "seed_too_thin",
  "no_fitting_topic",
  "places_covered",
] as const;

export type NoTopicReason = (typeof NO_TOPIC_REASONS)[number];

export interface BlogTopicTour {
  tour_id: string;
  tour_name: string | null;
  topics: number;
  reason: string | null;
}

export interface BlogTopics {
  tours: BlogTopicTour[];
  total: number;
  with_topic: number;
}

export interface NoTopicRow {
  tourId: string;
  name: string | null;
  reason: NoTopicReason | null;
}

export function isNoTopicReason(code: unknown): code is NoTopicReason {
  return typeof code === "string" && (NO_TOPIC_REASONS as readonly string[]).includes(code);
}

/** The adopted tours with no Blog topic, in the API's order (by name), each with a known reason or null. */
export function toursWithoutTopic(bt: BlogTopics | null | undefined): NoTopicRow[] {
  if (!bt || !Array.isArray(bt.tours)) return [];
  return bt.tours
    .filter(t => t && typeof t.tour_id === "string" && !(t.topics > 0))
    .map(t => ({ tourId: t.tour_id, name: t.tour_name ?? null, reason: isNoTopicReason(t.reason) ? t.reason : null }));
}
