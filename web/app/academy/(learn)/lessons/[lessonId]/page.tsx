// web/app/academy/(learn)/lessons/[lessonId]/page.tsx — the AA Academy lesson reader (AA-1105, GET
// /learn/v1/lessons/{id}?lang=): the lesson body in the reader's language when a translation exists (English
// otherwise, the API decides), the objective, the video with captions, the live cited sections ("a course never copies
// its sources", GUIDE-13), the learning events and the previous / next lesson of the module.
import Link from "next/link";
import { notFound } from "next/navigation";
import { Badge, K, PageHeader, Section } from "@/app/_kit";
import { LessonBody } from "@/components/academy/LessonBody";
import { LessonProgress } from "@/components/academy/LessonProgress";
import { ReadError } from "@/components/academy/ReadError";
import { VideoPlayer } from "@/components/academy/VideoPlayer";
import type { LearnLang, Lesson, Module } from "@/lib/academy";
import { LESSON_ID } from "@/lib/academyBff";
import { academyContext } from "@/lib/academyContext";
import { academyRead } from "@/lib/academyPage";
import { academyDict, say } from "@/lib/i18nAcademy";

export const dynamic = "force-dynamic";

const LESSON_RE = new RegExp(`^${LESSON_ID}$`);

/** The lesson language: the page's ?lang= when valid, else the reader's UI language. */
function lessonLang(raw: string | undefined, uiLang: LearnLang): LearnLang {
  return raw === "en" || raw === "vi" ? raw : uiLang;
}

export default async function AcademyLessonPage({
  params,
  searchParams,
}: {
  params: Promise<{ lessonId: string }>;
  searchParams: Promise<{ lang?: string }>;
}) {
  const { lessonId } = await params;
  if (!LESSON_RE.test(lessonId)) notFound();
  const { lang: rawLang } = await searchParams;
  const ctx = await academyContext();
  const dict = academyDict(ctx.lang);
  const lang = lessonLang(rawLang, ctx.lang);
  const read = await academyRead<Lesson>(ctx, `lessons/${lessonId}`, new URLSearchParams({ lang }));
  const crumbs = [{ label: dict["academy.nav.courses"], href: "/academy" }];
  if (!read.ok) {
    return (
      <div style={{ display: "grid", gap: 16 }}>
        <PageHeader title={lessonId} breadcrumbs={crumbs} />
        <ReadError lang={ctx.lang} kind={read.kind} />
      </div>
    );
  }
  const lesson = read.data;
  const mod = await academyRead<Module>(ctx, `modules/${lesson.module_id}`);
  const order = mod.ok ? mod.data.lessons.map((l) => l.id) : [];
  const at = order.indexOf(lesson.id);
  const prev = at > 0 ? order[at - 1] : null;
  const next = at >= 0 && at < order.length - 1 ? order[at + 1] : null;
  const moduleHref = `/academy/modules/${encodeURIComponent(lesson.module_id)}`;
  return (
    <div style={{ display: "grid", gap: 16, maxWidth: 920 }} data-testid="academy-lesson">
      <PageHeader
        title={lesson.title}
        breadcrumbs={[...crumbs, { label: lesson.module_id, href: moduleHref }, { label: lesson.id }]}
        description={`${dict[`academy.lesson.type.${lesson.type}`]} · ${say(dict, "academy.minutes", { n: lesson.minutes })}`}
        actions={
          <span style={{ display: "inline-flex", gap: 6, flexWrap: "wrap" }}>
            {lesson.draft && <Badge tone="warning">{dict["academy.draft"]}</Badge>}
            {lesson.stale && <Badge tone="warning">{dict["academy.stale"]}</Badge>}
          </span>
        }
      />
      <Section>
        <div style={{ display: "grid", gap: 14 }}>
          <p style={{ margin: 0, fontSize: 13.5, color: K.body }} data-testid="academy-lesson-objective">
            <strong>{dict["academy.lesson.objective"]}:</strong> {lesson.objective}
          </p>
          <nav aria-label={dict["academy.lesson.language"]} style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", fontSize: 12.5 }}>
            {lesson.languages.length > 1 ? (
              lesson.languages.map((l) =>
                l === lesson.lang ? (
                  <strong key={l} style={{ color: K.ink }}>
                    {l.toUpperCase()}
                  </strong>
                ) : (
                  <Link key={l} href={`/academy/lessons/${encodeURIComponent(lesson.id)}?lang=${l}`} style={{ color: K.accentDeep }}>
                    {l.toUpperCase()}
                  </Link>
                ),
              )
            ) : (
              <span style={{ color: K.muted }}>{lang === "vi" ? dict["academy.lesson.englishOnly"] : null}</span>
            )}
          </nav>
          {lesson.stale && <p style={{ margin: 0, fontSize: 12.5, color: K.amber }}>{dict["academy.staleHelp"]}</p>}
          {lesson.has_video && <VideoPlayer lessonId={lesson.id} lang={lesson.lang} dict={dict} />}
          <LessonBody text={lesson.body_md} skipFirstHeading testId="academy-lesson-body" />
          <LessonProgress lessonId={lesson.id} lang={lesson.lang} status={lesson.status} moduleId={lesson.module_id} dict={dict} />
        </div>
      </Section>
      {lesson.sources.length > 0 && (
        <Section title={dict["academy.lesson.sources"]} description={dict["academy.lesson.sourcesHelp"]} testId="academy-lesson-sources">
          <div style={{ display: "grid", gap: 10 }}>
            {lesson.sources.map((s) => (
              <details key={s.ref} style={{ border: `1px solid ${K.line}`, borderRadius: 8, padding: "8px 12px" }}>
                <summary style={{ cursor: "pointer", fontSize: 13, color: K.ink, display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                  <span style={{ overflowWrap: "anywhere" }}>{s.ref}</span>
                  <Badge dot={false}>{s.kind}</Badge>
                  {s.stale && <Badge tone="warning">{dict["academy.stale"]}</Badge>}
                </summary>
                <div style={{ marginTop: 8 }}>
                  {s.available && s.body_md ? (
                    <LessonBody text={s.body_md} />
                  ) : (
                    <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{dict["academy.lesson.sourceUnavailable"]}</p>
                  )}
                </div>
              </details>
            ))}
          </div>
        </Section>
      )}
      <nav style={{ display: "flex", gap: 16, flexWrap: "wrap", fontSize: 13.5 }} data-testid="academy-lesson-nav">
        {prev && (
          <Link href={`/academy/lessons/${encodeURIComponent(prev)}`} style={{ color: K.accentDeep }}>
            ← {dict["academy.lesson.previous"]}
          </Link>
        )}
        <Link href={moduleHref} style={{ color: K.accentDeep }}>
          {dict["academy.lesson.backToModule"]}
        </Link>
        {next && (
          <Link href={`/academy/lessons/${encodeURIComponent(next)}`} style={{ color: K.accentDeep, fontWeight: 600 }}>
            {dict["academy.lesson.next"]} →
          </Link>
        )}
      </nav>
    </div>
  );
}
