"use client";
// web/components/academy/ReviewRunner.tsx — AA-1105: today's spaced review cards (GET /learn/v1/review/due, at most the
// daily cap the API returns). One card at a time: answer, rate how well you knew it (the FSRS grade, lib/
// academyReview.ts), then CIS grades the answer and stores the next schedule (POST /learn/v1/review/{item}/result);
// the correct choices and the explanation show after.
import { useState } from "react";
import { Badge, Button, K, Section } from "@/app/_kit";
import { ItemCard } from "@/components/academy/QuizRunner";
import { toggleChoice, type QuizItem, type ReviewCard, type ReviewResult } from "@/lib/academy";
import { academySend } from "@/lib/academyClient";
import { RATINGS, reviewSubmission, type ReviewRating } from "@/lib/academyReview";
import { say, type AcademyDict } from "@/lib/i18nAcademy";

function Answer({ item, result, dict, onNext }: { item: QuizItem; result: ReviewResult; dict: AcademyDict; onNext: () => void }) {
  const answer = result.correct_choices.map((c) => item.choices[c]).filter(Boolean).join("; ");
  return (
    <>
      <Badge tone={result.correct ? "success" : "danger"}>{result.correct ? dict["academy.quiz.correct"] : dict["academy.quiz.wrong"]}</Badge>
      {!result.correct && <span style={{ fontSize: 13, color: K.body }}>{say(dict, "academy.quiz.correctAnswer", { answer })}</span>}
      {result.explain && <span style={{ fontSize: 13, color: K.body }}>{result.explain}</span>}
      <span data-testid="academy-review-next">
        <Button variant="primary" onClick={onNext}>
          {dict["academy.review.next"]}
        </Button>
      </span>
    </>
  );
}

function Ratings({ busy, dict, onRate }: { busy: boolean; dict: AcademyDict; onRate: (r: ReviewRating) => void }) {
  return (
    <>
      <span style={{ fontSize: 12.5, color: K.muted }}>{dict["academy.review.howSure"]}</span>
      <span style={{ display: "inline-flex", gap: 8, flexWrap: "wrap" }} data-testid="academy-review-rate">
        {RATINGS.map((r) => (
          <Button key={r} size="sm" variant={r === "good" ? "primary" : "secondary"} onClick={() => onRate(r)} disabled={busy}>
            {dict[`academy.review.rate.${r}`]}
          </Button>
        ))}
      </span>
    </>
  );
}

export function ReviewRunner({ cards, targetRetention, dict }: { cards: ReviewCard[]; targetRetention?: number; dict: AcademyDict }) {
  const [index, setIndex] = useState(0);
  const [chosen, setChosen] = useState<number[]>([]);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ReviewResult | null>(null);
  const [failed, setFailed] = useState(false);

  if (index >= cards.length) {
    return (
      <Section testId="academy-review-finished">
        <p style={{ margin: 0, fontSize: 14, color: K.success, fontWeight: 600 }}>{dict["academy.review.finished"]}</p>
      </Section>
    );
  }
  const card = cards[index];
  const item = card.item;

  async function rate(rating: ReviewRating) {
    setBusy(true);
    setFailed(false);
    const body = reviewSubmission(card.card, rating, chosen, new Date(), targetRetention);
    const res = await academySend<ReviewResult>("POST", `review/${encodeURIComponent(item.id)}/result`, { body });
    setBusy(false);
    if (res.ok) setResult(res.data);
    else setFailed(res.status !== 401);
  }

  function next() {
    setIndex((i) => i + 1);
    setChosen([]);
    setResult(null);
    setFailed(false);
  }

  return (
    <div style={{ display: "grid", gap: 12 }} data-testid="academy-review-card">
      <span style={{ fontSize: 12.5, color: K.muted }}>{say(dict, "academy.review.card", { n: index + 1, total: cards.length })}</span>
      <ItemCard
        item={item}
        n={index + 1}
        total={0}
        chosen={chosen}
        graded={undefined}
        locked={busy || result !== null}
        onToggle={(ci) => setChosen((c) => toggleChoice(c, ci, item.kind))}
        dict={dict}
        namePrefix="r"
      />
      <div style={{ display: "grid", gap: 10, justifyItems: "start" }}>
        {result ? <Answer item={item} result={result} dict={dict} onNext={next} /> : <Ratings busy={busy} dict={dict} onRate={rate} />}
        {failed && (
          <span role="alert" style={{ fontSize: 12.5, color: K.danger }}>
            {dict["academy.error"]}
          </span>
        )}
      </div>
    </div>
  );
}
