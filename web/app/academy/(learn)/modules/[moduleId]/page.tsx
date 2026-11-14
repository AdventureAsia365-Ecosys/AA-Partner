// web/app/academy/(learn)/modules/[moduleId]/page.tsx — one AA Academy module (AA-1105, GET /learn/v1/modules/{id}):
// objectives, prerequisites, the lessons with the learner's progress, the module quiz (pass mark, questions per
// attempt, best score, the retake time: all values the API returns, never computed here) and the lab state.
import Link from "next/link";
import { notFound } from "next/navigation";
import { Badge, K, PageHeader, Section } from "@/app/_kit";
import { LabCheck } from "@/components/academy/LabCheck";
import { LocalTime } from "@/components/academy/LocalTime";
import { ReadError } from "@/components/academy/ReadError";
import { lessonProgress, retakeWaiting, type LessonSummary, type Module } from "@/lib/academy";
import { MODULE_ID } from "@/lib/academyBff";
import { academyContext } from "@/lib/academyContext";
import { academyRead } from "@/lib/academyPage";
import type { DateLocale } from "@/lib/dates";
import { academyDict, say, type AcademyDict } from "@/lib/i18nAcademy";

export const dynamic = "force-dynamic";

const MODULE_RE = new RegExp(`^${MODULE_ID}$`);

const LESSON_TONE = { not_started: "neutral", opened: "info", completed: "success" } as const;

function LessonRow({ lesson, dict }: { lesson: LessonSummary; dict: AcademyDict }) {
  return (
    <li
      style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", padding: "10px 0", borderTop: `1px solid ${K.line}` }}
      data-testid={`academy-lesson-row-${lesson.id}`}
    >
      <Link href={`/academy/lessons/${encodeURIComponent(lesson.id)}`} style={{ fontSize: 13.5, color: K.accentDeep, fontWeight: 600, textDecoration: "none" }}>
        {lesson.title}
      </Link>
      <span style={{ fontSize: 12.5, color: K.muted }}>
        {dict[`academy.lesson.type.${lesson.type}`]} · {say(dict, "academy.minutes", { n: lesson.minutes })}
        {lesson.has_video ? ` · ${dict["academy.lesson.video"]}` : ""}
      </span>
      <span style={{ flex: 1 }} />
      {lesson.stale && <Badge tone="warning">{dict["academy.stale"]}</Badge>}
      <Badge tone={LESSON_TONE[lesson.status]}>{dict[`academy.lesson.status.${lesson.status}`]}</Badge>
    </li>
  );
}

function QuizPanel({ module: m, dict, locale }: { module: Module; dict: AcademyDict; locale: DateLocale }) {
  const q = m.quiz;
  const waiting = retakeWaiting(q.retake_at);
  return (
    <Section title={dict["academy.quiz.title"]} testId="academy-module-quiz">
      {!q.available ? (
        <p style={{ margin: 0, fontSize: 13.5, color: K.muted }}>{dict["academy.quiz.notAvailable"]}</p>
      ) : (
        <div style={{ display: "grid", gap: 10, justifyItems: "start" }}>
          <p style={{ margin: 0, fontSize: 13.5, color: K.body }}>
            {say(dict, "academy.quiz.rules", { items: q.items_per_attempt, pct: q.pass_mark_pct })}
          </p>
          {(q.best_score_pct !== null || q.passed) && (
            <span style={{ display: "inline-flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              {q.best_score_pct !== null && (
                <span style={{ fontSize: 13, color: K.muted }}>{say(dict, "academy.quiz.best", { pct: Math.round(q.best_score_pct) })}</span>
              )}
              {q.passed && <Badge tone="success">{dict["academy.quiz.passed"]}</Badge>}
            </span>
          )}
          {waiting && q.retake_at ? (
            <p style={{ margin: 0, fontSize: 13, color: K.amber }} data-testid="academy-retake-wait">
              {dict["academy.quiz.retakeAt"].split("{time}")[0]}
              <LocalTime iso={q.retake_at} locale={locale} />
              {dict["academy.quiz.retakeAt"].split("{time}")[1] ?? ""}
            </p>
          ) : (
            <Link
              href={`/academy/quiz/module/${encodeURIComponent(m.id)}`}
              style={{ fontSize: 13.5, fontWeight: 600, color: K.accentDeep }}
              data-testid="academy-start-quiz"
            >
              {dict["academy.quiz.start"]}
            </Link>
          )}
        </div>
      )}
    </Section>
  );
}

function LabPanel({ module: m, dict }: { module: Module; dict: AcademyDict }) {
  const lab = m.lab;
  const text = !lab.present
    ? dict["academy.lab.none"]
    : lab.state === "passed"
      ? dict["academy.lab.passed"]
      : lab.state === "open"
        ? dict["academy.lab.open"]
        : dict["academy.lab.closed"];
  return (
    <Section title={dict["academy.lab.title"]} testId="academy-module-lab">
      <div style={{ display: "grid", gap: 10, justifyItems: "start" }}>
        <p style={{ margin: 0, fontSize: 13.5, color: K.muted }}>{text}</p>
        {lab.present && lab.state === "open" && <LabCheck moduleId={m.id} dict={dict} />}
      </div>
    </Section>
  );
}

export default async function AcademyModulePage({ params }: { params: Promise<{ moduleId: string }> }) {
  const { moduleId } = await params;
  if (!MODULE_RE.test(moduleId)) notFound();
  const ctx = await academyContext();
  const dict = academyDict(ctx.lang);
  const read = await academyRead<Module>(ctx, `modules/${moduleId}`);
  const crumbs = [{ label: dict["academy.nav.courses"], href: "/academy" }];
  if (!read.ok) {
    return (
      <div style={{ display: "grid", gap: 16 }}>
        <PageHeader title={moduleId} breadcrumbs={crumbs} />
        <ReadError lang={ctx.lang} kind={read.kind} />
      </div>
    );
  }
  const m = read.data;
  const progress = lessonProgress(m.lessons);
  return (
    <div style={{ display: "grid", gap: 16 }} data-testid="academy-module">
      <PageHeader
        title={`${m.id} · ${m.title}`}
        breadcrumbs={[...crumbs, { label: m.id }]}
        description={say(dict, "academy.module.version", { v: m.version })}
        actions={
          <span style={{ display: "inline-flex", gap: 6, flexWrap: "wrap" }}>
            {m.draft && <Badge tone="warning">{dict["academy.draft"]}</Badge>}
            {m.stale && <Badge tone="warning">{dict["academy.stale"]}</Badge>}
            {m.complete && <Badge tone="success">{dict["academy.module.complete"]}</Badge>}
          </span>
        }
      />
      {m.objectives.length > 0 && (
        <Section title={dict["academy.module.objectives"]}>
          <ul style={{ margin: 0, paddingLeft: 20, fontSize: 13.5, color: K.body, display: "grid", gap: 4 }}>
            {m.objectives.map((o) => (
              <li key={o}>{o}</li>
            ))}
          </ul>
        </Section>
      )}
      {m.prerequisites.length > 0 && (
        <Section title={dict["academy.module.prerequisites"]}>
          <ul style={{ margin: 0, paddingLeft: 20, fontSize: 13.5, color: K.body, display: "grid", gap: 4 }}>
            {m.prerequisites.map((p) => (
              <li key={p.id}>
                <Link href={`/academy/modules/${encodeURIComponent(p.id)}`} style={{ color: K.accentDeep }}>
                  {p.id}
                </Link>{" "}
                <span style={{ color: K.muted }}>({dict[`academy.module.prereq.${p.state}`]})</span>
              </li>
            ))}
          </ul>
        </Section>
      )}
      <Section title={dict["academy.module.lessons"]} description={say(dict, "academy.module.lessonsDone", progress)}>
        <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
          {m.lessons.map((l) => (
            <LessonRow key={l.id} lesson={l} dict={dict} />
          ))}
        </ul>
      </Section>
      <QuizPanel module={m} dict={dict} locale={ctx.locale} />
      <LabPanel module={m} dict={dict} />
    </div>
  );
}
