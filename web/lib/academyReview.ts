// web/lib/academyReview.ts
// AA-1105 — the spaced review scheduler of the academy (research §6.4: FSRS through ts-fsrs, run in the browser, the
// card stored by CIS). The browser computes the next card state from the learner's own rating; CIS grades the chosen
// answer itself and stores the card only when its due date is not earlier than the stored one and its last review is
// the submitted review time (learn.http.v1 postReviewResult). The target retention comes from the API's answer
// (setting cis.learn.review_target_retention), never a literal here.
//
// The contract takes the card with the answer, before the server says whether the answer was right, so the learner
// rates how well they knew it before checking (Forgot / Hard / Good / Easy).

import { createEmptyCard, fsrs, generatorParameters, Rating, State, type Card, type Grade } from "ts-fsrs";
import type { FsrsCardJson } from "./academy";

export const RATINGS = ["again", "hard", "good", "easy"] as const;
export type ReviewRating = (typeof RATINGS)[number];

const GRADE: Record<ReviewRating, Grade> = { again: Rating.Again, hard: Rating.Hard, good: Rating.Good, easy: Rating.Easy };

function toDate(value: string | null | undefined): Date | undefined {
  if (!value) return undefined;
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? undefined : d;
}

function num(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : fallback;
}

/** The stored card (JSON from the API) as a ts-fsrs Card; an unreadable due date starts a new card due now. */
export function fromJson(card: FsrsCardJson, now: Date): Card {
  const due = toDate(card.due);
  if (!due) return createEmptyCard(now);
  const state = [State.New, State.Learning, State.Review, State.Relearning].includes(card.state) ? (card.state as State) : State.New;
  return {
    due,
    stability: num(card.stability),
    difficulty: num(card.difficulty),
    elapsed_days: num(card.elapsed_days),
    scheduled_days: num(card.scheduled_days),
    learning_steps: num(card.learning_steps),
    reps: num(card.reps),
    lapses: num(card.lapses),
    state,
    last_review: toDate(card.last_review),
  };
}

export function toJson(card: Card): FsrsCardJson {
  return {
    due: card.due.toISOString(),
    stability: card.stability,
    difficulty: card.difficulty,
    elapsed_days: card.elapsed_days,
    scheduled_days: card.scheduled_days,
    learning_steps: card.learning_steps,
    reps: card.reps,
    lapses: card.lapses,
    state: card.state,
    last_review: card.last_review ? card.last_review.toISOString() : null,
  };
}

/** A valid FSRS retention (0 < r < 1) from the API, or undefined (ts-fsrs then keeps its own default). */
export function retention(value: number | undefined): number | undefined {
  return typeof value === "number" && value > 0 && value < 1 ? value : undefined;
}

/**
 * The review submission for one card: the next card state after `rating` at `now`, its last review = `reviewed_at`
 * (the same instant, as CIS checks), and never due before the stored due date (the server refuses that).
 */
export function reviewSubmission(
  stored: FsrsCardJson,
  rating: ReviewRating,
  chosen: number[],
  now: Date,
  targetRetention?: number,
): { chosen: number[]; reviewed_at: string; card: FsrsCardJson } {
  const r = retention(targetRetention);
  const scheduler = fsrs(generatorParameters({ enable_fuzz: false, ...(r === undefined ? {} : { request_retention: r }) }));
  const next = scheduler.next(fromJson(stored, now), now, GRADE[rating]).card;
  const storedDue = toDate(stored.due);
  if (storedDue && next.due.getTime() < storedDue.getTime()) next.due = storedDue;
  next.last_review = now;
  return { chosen: [...chosen], reviewed_at: now.toISOString(), card: toJson(next) };
}
