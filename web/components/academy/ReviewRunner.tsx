"use client";
// web/components/academy/ReviewRunner.tsx — AA-1105: today's spaced review cards (GET /learn/v1/review/due, at most the
// daily cap the API returns). One card at a time: answer, rate how well you knew it (the FSRS grade, lib/
// academyReview.ts), then CIS grades the answer and stores the next schedule (POST /learn/v1/review/{item}/result);
// the correct choices and the explanation show after.
import { useState } from "react";
import { Badge, Button, Checkbox, K, Radio, Section } from "@/app/_kit";
import { toggleChoice, type ReviewCard, type ReviewResult } from "@/lib/academy";
import { academySend } from "@/lib/academyClient";
import { RATINGS, reviewSubmission, type ReviewRating } from "@/lib/academyReview";
import { say, type AcademyDict } from "@/lib/i18nAcademy";

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
  const Control = item.kind === "single" ? Radio : Checkbox;

  async function rate(rating: ReviewRating) {
    setBusy(true);
    setFailed(false);
    const body = reviewSubmission(card.card, rating, chosen, new Date(), targetRetention);
    const res = await academySend<ReviewResult>("POST", `review/${encodeURIComponent(item.id)}/result`, { body });
    setBusy(false);
    if (res.ok) setResult(res.data);
    else if (res.status !== 401) setFailed(true);
  }

  function next() {
    setIndex((i) => i + 1);
    setChosen([]);
    setResult(null);
    setFailed(false);
  }

  return (
    <Section title={say(dict, "academy.review.card", { n: index + 1, total: cards.length })} testId="academy-review-card">
      <fieldset style={{ border: "none", margin: 0, padding: 0, display: "grid", gap: 8, minWidth: 0 }} disabled={busy || result !== null}>
        <legend style={{ fontSize: 14, color: K.ink, fontWeight: 600, marginBottom: 4, padding: 0 }}>{item.prompt}</legend>
        <span style={{ fontSize: 12, color: K.muted }}>{item.kind === "single" ? dict["academy.quiz.single"] : dict["academy.quiz.multiple"]}</span>
        {item.choices.map((choice, ci) => (
          <div key={ci} style={{ display: "flex", alignItems: "center", gap: 8, color: result?.correct_choices.includes(ci) ? K.success : K.body }}>
            <Control name={`r-${item.id}`} checked={chosen.includes(ci)} onChange={() => setChosen((c) => toggleChoice(c, ci, item.kind))} label={choice} />
          </div>
        ))}
      </fieldset>
      <div style={{ display: "grid", gap: 10, marginTop: 12, justifyItems: "start" }}>
        {result ? (
          <>
            <Badge tone={result.correct ? "success" : "danger"}>{result.correct ? dict["academy.quiz.correct"] : dict["academy.quiz.wrong"]}</Badge>
            {!result.correct && (
              <span style={{ fontSize: 13, color: K.body }}>
                {say(dict, "academy.quiz.correctAnswer", { answer: result.correct_choices.map((c) => item.choices[c]).filter(Boolean).join("; ") })}
              </span>
            )}
            {result.explain && <span style={{ fontSize: 13, color: K.body }}>{result.explain}</span>}
            <span data-testid="academy-review-next">
              <Button variant="primary" onClick={next}>
                {dict["academy.review.next"]}
              </Button>
            </span>
          </>
        ) : (
          <>
            <span style={{ fontSize: 12.5, color: K.muted }}>{dict["academy.review.howSure"]}</span>
            <span style={{ display: "inline-flex", gap: 8, flexWrap: "wrap" }} data-testid="academy-review-rate">
              {RATINGS.map((r) => (
                <Button key={r} size="sm" variant={r === "good" ? "primary" : "secondary"} onClick={() => rate(r)} disabled={busy}>
                  {dict[`academy.review.rate.${r}`]}
                </Button>
              ))}
            </span>
          </>
        )}
        {failed && (
          <span role="alert" style={{ fontSize: 12.5, color: K.danger }}>
            {dict["academy.error"]}
          </span>
        )}
      </div>
    </Section>
  );
}
