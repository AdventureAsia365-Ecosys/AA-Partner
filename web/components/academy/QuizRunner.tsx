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
import { quizResponses, toggleChoice, verifyUrl, type GradedItem, type QuizAttempt, type QuizItem, type QuizResult } from "@/lib/academy";
import { academySend } from "@/lib/academyClient";
import type { DateLocale } from "@/lib/dates";
import { say, type AcademyDict, type AcademyKey } from "@/lib/i18nAcademy";

type Phase =
  | { kind: "idle" }
  | { kind: "starting" }
  | { kind: "answering"; attempt: QuizAttempt }
  | { kind: "submitting"; attempt: QuizAttempt }
  | { kind: "graded"; attempt: QuizAttempt; result: QuizResult }
  | { kind: "refused"; code: string; retryAt: string | null };

/** A message with a {time} slot filled by the reader's local time. */
export function WithTime({ text, iso, locale }: { text: string; iso: string; locale: DateLocale }) {
  const [before, after = ""] = text.split("{time}");
  return (
    <>
      {before}
      <LocalTime iso={iso} locale={locale} />
      {after}
    </>
  );
}

const REFUSAL_KEY: Record<string, AcademyKey> = {
  prerequisites_open: "academy.quiz.prerequisites",
  attempt_closed: "academy.quiz.closed",
  conflict: "academy.quiz.closed",
  not_found: "academy.notFound",
};

/** Whether a refusal lets the learner simply start again (the attempt was closed or replaced). */
export function restartable(code: string): boolean {
  return code === "attempt_closed" || code === "conflict";
}

function Refused({ title, code, retryAt, locale, dict, onRetry, back }: { title: string; code: string; retryAt: string | null; locale: DateLocale; dict: AcademyDict; onRetry: () => void; back: React.ReactNode }) {
  const wait = code === "retake_wait" && retryAt;
  return (
    <Section title={title} testId="academy-quiz-refused">
      <div style={{ display: "grid", gap: 12, justifyItems: "start" }}>
        <p role="alert" style={{ margin: 0, fontSize: 13.5, color: K.amber }} data-testid="academy-quiz-refused-message">
          {wait ? <WithTime text={dict["academy.quiz.retakeWait"]} iso={retryAt} locale={locale} /> : dict[REFUSAL_KEY[code] ?? "academy.error"]}
        </p>
        {restartable(code) && <Button onClick={onRetry}>{dict["academy.retry"]}</Button>}
        {back}
      </div>
    </Section>
  );
}

function Intro({ title, starting, dict, onStart, back }: { title: string; starting: boolean; dict: AcademyDict; onStart: () => void; back: React.ReactNode }) {
  return (
    <Section title={title} testId="academy-quiz-intro">
      <div style={{ display: "grid", gap: 12, justifyItems: "start" }}>
        <p style={{ margin: 0, fontSize: 13.5, color: K.body }}>{dict["academy.quiz.intro"]}</p>
        <span data-testid="academy-quiz-start">
          <Button variant="primary" onClick={onStart} disabled={starting}>
            {starting ? dict["academy.quiz.starting"] : dict["academy.quiz.start"]}
          </Button>
        </span>
        {back}
      </div>
    </Section>
  );
}

function ResultPanel({ title, result, origin, locale, dict, back }: { title: string; result: QuizResult; origin: string; locale: DateLocale; dict: AcademyDict; back: React.ReactNode }) {
  const link = result.certificate ? verifyUrl(origin, result.certificate.verify_path) : null;
  return (
    <Section title={title} testId="academy-quiz-result">
      <div style={{ display: "grid", gap: 10, justifyItems: "start" }}>
        <span style={{ display: "inline-flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <Badge tone={result.passed ? "success" : "warning"}>{result.passed ? dict["academy.quiz.resultPassed"] : dict["academy.quiz.resultFailed"]}</Badge>
          <span style={{ fontSize: 14, color: K.ink, fontWeight: 600 }} data-testid="academy-quiz-score">
            {say(dict, "academy.quiz.score", { score: Math.round(result.score_pct), pct: result.pass_mark_pct })}
          </span>
        </span>
        {result.module_complete && <p style={{ margin: 0, fontSize: 13.5, color: K.success }}>{dict["academy.quiz.moduleComplete"]}</p>}
        {!result.passed && result.retake_at && (
          <p style={{ margin: 0, fontSize: 13.5, color: K.amber }} data-testid="academy-quiz-retake">
            <WithTime text={dict["academy.quiz.retakeWait"]} iso={result.retake_at} locale={locale} />
          </p>
        )}
        {link && (
          <div style={{ display: "grid", gap: 4 }} data-testid="academy-quiz-certificate">
            <span style={{ fontSize: 13.5, color: K.body }}>{dict["academy.quiz.certificate"]}</span>
            <code style={{ fontSize: 12.5, overflowWrap: "anywhere", color: K.ink }}>{link}</code>
          </div>
        )}
        {back}
      </div>
    </Section>
  );
}

/** The correct answer, the explanation and the source of one graded item. */
function Feedback({ item, g, n, dict }: { item: QuizItem; g: GradedItem; n: number; dict: AcademyDict }) {
  const answer = g.correct_choices.map((c) => item.choices[c]).filter(Boolean).join("; ");
  return (
    <div style={{ display: "grid", gap: 4, marginTop: 6 }} data-testid={`academy-quiz-feedback-${n}`}>
      <span>
        <Badge tone={g.correct ? "success" : "danger"}>{g.correct ? dict["academy.quiz.correct"] : dict["academy.quiz.wrong"]}</Badge>
      </span>
      {!g.correct && <span style={{ fontSize: 13, color: K.body }}>{say(dict, "academy.quiz.correctAnswer", { answer })}</span>}
      {g.explain && <span style={{ fontSize: 13, color: K.body }}>{g.explain}</span>}
      {g.source_ref && <span style={{ fontSize: 12, color: K.muted, overflowWrap: "anywhere" }}>{say(dict, "academy.quiz.source", { ref: g.source_ref })}</span>}
    </div>
  );
}

/** One question: its choices (Radio for one answer, Checkbox for several) and, once graded, its feedback. */
export function ItemCard({
  item,
  n,
  total,
  chosen,
  graded,
  locked,
  onToggle,
  dict,
  namePrefix = "q",
}: {
  item: QuizItem;
  n: number;
  total: number;
  chosen: number[];
  graded: GradedItem | undefined;
  locked: boolean;
  onToggle: (choice: number) => void;
  dict: AcademyDict;
  namePrefix?: string;
}) {
  const Control = item.kind === "single" ? Radio : Checkbox;
  return (
    <Section title={total > 0 ? say(dict, "academy.quiz.question", { n, total }) : undefined} testId={`academy-quiz-item-${n}`}>
      <fieldset style={{ border: "none", margin: 0, padding: 0, display: "grid", gap: 8, minWidth: 0 }} disabled={locked}>
        <legend style={{ fontSize: 14, color: K.ink, fontWeight: 600, marginBottom: 4, padding: 0 }}>{item.prompt}</legend>
        <span style={{ fontSize: 12, color: K.muted }}>{item.kind === "single" ? dict["academy.quiz.single"] : dict["academy.quiz.multiple"]}</span>
        {item.choices.map((choice, ci) => (
          <div key={ci} style={{ display: "flex", alignItems: "center", gap: 8, color: graded?.correct_choices.includes(ci) ? K.success : K.body }}>
            <Control name={`${namePrefix}-${item.id}`} checked={chosen.includes(ci)} onChange={() => onToggle(ci)} label={choice} />
          </div>
        ))}
        {graded && <Feedback item={item} g={graded} n={n} dict={dict} />}
      </fieldset>
    </Section>
  );
}

function SubmitBar({ unanswered, submitting, dict, onSubmit }: { unanswered: number; submitting: boolean; dict: AcademyDict; onSubmit: () => void }) {
  return (
    <div style={{ display: "grid", gap: 8, justifyItems: "start" }}>
      {unanswered > 0 && <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{say(dict, "academy.quiz.unanswered", { n: unanswered })}</p>}
      <span data-testid="academy-quiz-submit">
        <Button variant="primary" onClick={onSubmit} disabled={submitting}>
          {submitting ? dict["academy.quiz.submitting"] : dict["academy.quiz.submit"]}
        </Button>
      </span>
    </div>
  );
}

function attemptPath(scope: "module" | "final", targetId: string): string {
  const id = encodeURIComponent(targetId);
  return scope === "module" ? `modules/${id}/quiz/attempts` : `paths/${id}/final-quiz/attempts`;
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

  const refused = (status: number, problem: { error: string; retry_at?: string | null }) => {
    if (status !== 401) setPhase({ kind: "refused", code: problem.error, retryAt: problem.retry_at ?? null });
  };

  async function start() {
    setPhase({ kind: "starting" });
    setAnswers({});
    const res = await academySend<QuizAttempt>("POST", attemptPath(scope, targetId));
    if (res.ok) setPhase({ kind: "answering", attempt: res.data });
    else refused(res.status, res.problem);
  }

  async function submit(attempt: QuizAttempt) {
    setPhase({ kind: "submitting", attempt });
    const res = await academySend<QuizResult>("POST", `quiz/attempts/${encodeURIComponent(attempt.attempt_id)}/submit`, {
      body: { responses: quizResponses(attempt.items, answers) },
    });
    if (res.ok) setPhase({ kind: "graded", attempt, result: res.data });
    else refused(res.status, res.problem);
  }

  const title = scope === "module" ? `${dict["academy.quiz.title"]} · ${targetId}` : dict["academy.quiz.finalTitle"];
  const back = (
    <a href={backHref} style={{ fontSize: 13.5, color: K.accentDeep }}>
      {backLabel}
    </a>
  );

  if (phase.kind === "idle" || phase.kind === "starting") {
    return <Intro title={title} starting={phase.kind === "starting"} dict={dict} onStart={start} back={back} />;
  }
  if (phase.kind === "refused") {
    return <Refused title={title} code={phase.code} retryAt={phase.retryAt} locale={locale} dict={dict} onRetry={start} back={back} />;
  }

  const attempt = phase.attempt;
  const graded = phase.kind === "graded" ? phase.result : null;
  const byItem = new Map(graded?.items.map((g) => [g.item_id, g]) ?? []);
  const unanswered = attempt.items.filter((it) => (answers[it.id] ?? []).length === 0).length;

  return (
    <div style={{ display: "grid", gap: 16 }} data-testid="academy-quiz">
      {graded && <ResultPanel title={title} result={graded} origin={origin} locale={locale} dict={dict} back={back} />}
      {attempt.items.map((item, idx) => (
        <ItemCard
          key={item.id}
          item={item}
          n={idx + 1}
          total={attempt.items.length}
          chosen={graded ? (byItem.get(item.id)?.chosen ?? []) : (answers[item.id] ?? [])}
          graded={byItem.get(item.id)}
          locked={graded !== null || phase.kind === "submitting"}
          onToggle={(ci) => setAnswers((a) => ({ ...a, [item.id]: toggleChoice(a[item.id] ?? [], ci, item.kind) }))}
          dict={dict}
        />
      ))}
      {!graded && <SubmitBar unanswered={unanswered} submitting={phase.kind === "submitting"} dict={dict} onSubmit={() => submit(attempt)} />}
    </div>
  );
}
