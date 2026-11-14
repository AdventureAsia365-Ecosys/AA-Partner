"use client";
// web/components/academy/QuizRunner.tsx — AA-1105: one quiz attempt, module or final (learn.http.v1 "Grading"). The
// learner starts the attempt (POST .../quiz/attempts: the server draws the items and returns them WITHOUT the correct
// choices), answers, submits (POST /learn/v1/quiz/attempts/{id}/submit) and only then sees the score, the correct
// choices, the explanations and their sources. A failed quiz shows when the retake opens (retake_at from the API:
// the delay is the setting cis.learn.quiz_retake_delay_hours, never computed here); a 409 retake_wait on start shows
// the same. A certificate earned by this attempt shows its share link once.
import { useState } from "react";
import { Badge, Button, Checkbox, K, Radio, Section } from "@/app/_kit";
import { LocalTime } from "@/components/academy/LocalTime";
import { quizResponses, toggleChoice, verifyUrl, type QuizAttempt, type QuizResult } from "@/lib/academy";
import { academySend } from "@/lib/academyClient";
import type { DateLocale } from "@/lib/dates";
import { say, type AcademyDict } from "@/lib/i18nAcademy";

type Phase =
  | { kind: "idle" }
  | { kind: "starting" }
  | { kind: "answering"; attempt: QuizAttempt }
  | { kind: "submitting"; attempt: QuizAttempt }
  | { kind: "graded"; attempt: QuizAttempt; result: QuizResult }
  | { kind: "refused"; code: string; retryAt: string | null };

function WithTime({ text, iso, locale }: { text: string; iso: string; locale: DateLocale }) {
  const [before, after = ""] = text.split("{time}");
  return (
    <>
      {before}
      <LocalTime iso={iso} locale={locale} />
      {after}
    </>
  );
}

export function QuizRunner({
  scope,
  targetId,
  backHref,
  backLabel,
  origin,
  locale,
  dict,
}: {
  scope: "module" | "final";
  targetId: string;
  backHref: string;
  backLabel: string;
  /** The academy's public origin (for the certificate share link). */
  origin: string;
  locale: DateLocale;
  dict: AcademyDict;
}) {
  const [phase, setPhase] = useState<Phase>({ kind: "idle" });
  const [answers, setAnswers] = useState<Record<string, number[]>>({});

  async function start() {
    setPhase({ kind: "starting" });
    setAnswers({});
    const path = scope === "module" ? `modules/${encodeURIComponent(targetId)}/quiz/attempts` : `paths/${encodeURIComponent(targetId)}/final-quiz/attempts`;
    const res = await academySend<QuizAttempt>("POST", path);
    if (res.ok) setPhase({ kind: "answering", attempt: res.data });
    else if (res.status !== 401) setPhase({ kind: "refused", code: res.problem.error, retryAt: res.problem.retry_at ?? null });
  }

  async function submit(attempt: QuizAttempt) {
    setPhase({ kind: "submitting", attempt });
    const res = await academySend<QuizResult>("POST", `quiz/attempts/${encodeURIComponent(attempt.attempt_id)}/submit`, {
      body: { responses: quizResponses(attempt.items, answers) },
    });
    if (res.ok) setPhase({ kind: "graded", attempt, result: res.data });
    else if (res.status !== 401) setPhase({ kind: "refused", code: res.problem.error, retryAt: res.problem.retry_at ?? null });
  }

  const title = scope === "module" ? `${dict["academy.quiz.title"]} · ${targetId}` : dict["academy.quiz.finalTitle"];
  const back = (
    <a href={backHref} style={{ fontSize: 13.5, color: K.accentDeep }}>
      {backLabel}
    </a>
  );

  if (phase.kind === "idle" || phase.kind === "starting") {
    return (
      <Section title={title} testId="academy-quiz-intro">
        <div style={{ display: "grid", gap: 12, justifyItems: "start" }}>
          <p style={{ margin: 0, fontSize: 13.5, color: K.body }}>{dict["academy.quiz.intro"]}</p>
          <span data-testid="academy-quiz-start">
            <Button variant="primary" onClick={start} disabled={phase.kind === "starting"}>
              {phase.kind === "starting" ? dict["academy.quiz.starting"] : dict["academy.quiz.start"]}
            </Button>
          </span>
          {back}
        </div>
      </Section>
    );
  }

  if (phase.kind === "refused") {
    const message =
      phase.code === "retake_wait" && phase.retryAt ? (
        <WithTime text={dict["academy.quiz.retakeWait"]} iso={phase.retryAt} locale={locale} />
      ) : phase.code === "prerequisites_open" ? (
        dict["academy.quiz.prerequisites"]
      ) : phase.code === "attempt_closed" || phase.code === "conflict" ? (
        dict["academy.quiz.closed"]
      ) : phase.code === "not_found" ? (
        dict["academy.notFound"]
      ) : (
        dict["academy.error"]
      );
    return (
      <Section title={title} testId="academy-quiz-refused">
        <div style={{ display: "grid", gap: 12, justifyItems: "start" }}>
          <p role="alert" style={{ margin: 0, fontSize: 13.5, color: K.amber }} data-testid="academy-quiz-refused-message">
            {message}
          </p>
          {(phase.code === "attempt_closed" || phase.code === "conflict") && <Button onClick={start}>{dict["academy.retry"]}</Button>}
          {back}
        </div>
      </Section>
    );
  }

  const attempt = phase.attempt;
  const graded = phase.kind === "graded" ? phase.result : null;
  const byItem = new Map(graded?.items.map((g) => [g.item_id, g]) ?? []);
  const unanswered = attempt.items.filter((it) => (answers[it.id] ?? []).length === 0).length;

  return (
    <div style={{ display: "grid", gap: 16 }} data-testid="academy-quiz">
      {graded && (
        <Section title={title} testId="academy-quiz-result">
          <div style={{ display: "grid", gap: 10, justifyItems: "start" }}>
            <span style={{ display: "inline-flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <Badge tone={graded.passed ? "success" : "warning"}>
                {graded.passed ? dict["academy.quiz.resultPassed"] : dict["academy.quiz.resultFailed"]}
              </Badge>
              <span style={{ fontSize: 14, color: K.ink, fontWeight: 600 }} data-testid="academy-quiz-score">
                {say(dict, "academy.quiz.score", { score: Math.round(graded.score_pct), pct: graded.pass_mark_pct })}
              </span>
            </span>
            {graded.module_complete && <p style={{ margin: 0, fontSize: 13.5, color: K.success }}>{dict["academy.quiz.moduleComplete"]}</p>}
            {!graded.passed && graded.retake_at && (
              <p style={{ margin: 0, fontSize: 13.5, color: K.amber }} data-testid="academy-quiz-retake">
                <WithTime text={dict["academy.quiz.retakeWait"]} iso={graded.retake_at} locale={locale} />
              </p>
            )}
            {graded.certificate && verifyUrl(origin, graded.certificate.verify_path) && (
              <div style={{ display: "grid", gap: 4 }} data-testid="academy-quiz-certificate">
                <span style={{ fontSize: 13.5, color: K.body }}>{dict["academy.quiz.certificate"]}</span>
                <code style={{ fontSize: 12.5, overflowWrap: "anywhere", color: K.ink }}>{verifyUrl(origin, graded.certificate.verify_path)}</code>
              </div>
            )}
            {back}
          </div>
        </Section>
      )}
      {attempt.items.map((item, idx) => {
        const g = byItem.get(item.id);
        const chosen = graded ? (g?.chosen ?? []) : (answers[item.id] ?? []);
        const Control = item.kind === "single" ? Radio : Checkbox;
        return (
          <Section key={item.id} title={say(dict, "academy.quiz.question", { n: idx + 1, total: attempt.items.length })} testId={`academy-quiz-item-${idx + 1}`}>
            <fieldset style={{ border: "none", margin: 0, padding: 0, display: "grid", gap: 8, minWidth: 0 }} disabled={graded !== null || phase.kind === "submitting"}>
              <legend style={{ fontSize: 14, color: K.ink, fontWeight: 600, marginBottom: 4, padding: 0 }}>{item.prompt}</legend>
              <span style={{ fontSize: 12, color: K.muted }}>{item.kind === "single" ? dict["academy.quiz.single"] : dict["academy.quiz.multiple"]}</span>
              {item.choices.map((choice, ci) => {
                const right = g?.correct_choices.includes(ci);
                return (
                  <div key={ci} style={{ display: "flex", alignItems: "center", gap: 8, color: g ? (right ? K.success : K.body) : K.body }}>
                    <Control
                      name={`q-${item.id}`}
                      checked={chosen.includes(ci)}
                      onChange={() => setAnswers((a) => ({ ...a, [item.id]: toggleChoice(a[item.id] ?? [], ci, item.kind) }))}
                      label={choice}
                    />
                  </div>
                );
              })}
              {g && (
                <div style={{ display: "grid", gap: 4, marginTop: 6 }} data-testid={`academy-quiz-feedback-${idx + 1}`}>
                  <span>
                    <Badge tone={g.correct ? "success" : "danger"}>{g.correct ? dict["academy.quiz.correct"] : dict["academy.quiz.wrong"]}</Badge>
                  </span>
                  {!g.correct && (
                    <span style={{ fontSize: 13, color: K.body }}>
                      {say(dict, "academy.quiz.correctAnswer", { answer: g.correct_choices.map((c) => item.choices[c]).filter(Boolean).join("; ") })}
                    </span>
                  )}
                  {g.explain && <span style={{ fontSize: 13, color: K.body }}>{g.explain}</span>}
                  {g.source_ref && <span style={{ fontSize: 12, color: K.muted, overflowWrap: "anywhere" }}>{say(dict, "academy.quiz.source", { ref: g.source_ref })}</span>}
                </div>
              )}
            </fieldset>
          </Section>
        );
      })}
      {!graded && (
        <div style={{ display: "grid", gap: 8, justifyItems: "start" }}>
          {unanswered > 0 && <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{say(dict, "academy.quiz.unanswered", { n: unanswered })}</p>}
          <span data-testid="academy-quiz-submit">
            <Button variant="primary" onClick={() => submit(attempt)} disabled={phase.kind === "submitting"}>
              {phase.kind === "submitting" ? dict["academy.quiz.submitting"] : dict["academy.quiz.submit"]}
            </Button>
          </span>
        </div>
      )}
    </div>
  );
}
