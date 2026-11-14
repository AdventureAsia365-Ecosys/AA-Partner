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
import { lessonNeighbours, type CitedSection, type LearnLang, type Lesson, type Module } from "@/lib/academy";
import { LESSON_ID } from "@/lib/academyBff";
import { academyContext } from "@/lib/academyContext";
import { academyRead } from "@/lib/academyPage";
import { academyDict, say, type AcademyDict } from "@/lib/i18nAcademy";

export const dynamic = "force-dynamic";

const LESSON_RE = new RegExp(`^${LESSON_ID}$`);

/** The lesson language: the page's ?lang= when valid, else the reader's UI language. */
function lessonLang(raw: string | undefined, uiLang: LearnLang): LearnLang {
  return raw === "en" || raw === "vi" ? raw : uiLang;
}

const lessonHref = (id: string, lang?: LearnLang) => `/academy/lessons/${encodeURIComponent(id)}${lang ? `?lang=${lang}` : ""}`;

function LanguageNav({ lesson, lang, dict }: { lesson: Lesson; lang: LearnLang; dict: AcademyDict }) {
  if (lesson.languages.length < 2) {
    return lang === "vi" ? <span style={{ fontSize: 12.5, color: K.muted }}>{dict["academy.lesson.englishOnly"]}</span> : null;
  }
  return (
    <nav aria-label={dict["academy.lesson.language"]} style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", fontSize: 12.5 }}>
      {lesson.languages.map((l) =>
        l === lesson.lang ? (
          <strong key={l} style={{ color: K.ink }}>
            {l.toUpperCase()}
          </strong>
        ) : (
          <Link key={l} href={lessonHref(lesson.id, l)} style={{ color: K.accentDeep }}>
            {l.toUpperCase()}
          </Link>
        ),
      )}
    </nav>
  );
}

function Sources({ sources, dict }: { sources: CitedSection[]; dict: AcademyDict }) {
  if (sources.length === 0) return null;
  return (
    <Section title={dict["academy.lesson.sources"]} description={dict["academy.lesson.sourcesHelp"]} testId="academy-lesson-sources">
      <div style={{ display: "grid", gap: 10 }}>
        {sources.map((s) => (
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
  );
}

function LessonNav({ prev, next, moduleHref, dict }: { prev: string | null; next: string | null; moduleHref: string; dict: AcademyDict }) {
  return (
    <nav style={{ display: "flex", gap: 16, flexWrap: "wrap", fontSize: 13.5 }} data-testid="academy-lesson-nav">
      {prev && (
        <Link href={lessonHref(prev)} style={{ color: K.accentDeep }}>
          ← {dict["academy.lesson.previous"]}
        </Link>
      )}
      <Link href={moduleHref} style={{ color: K.accentDeep }}>
        {dict["academy.lesson.backToModule"]}
      </Link>
      {next && (
        <Link href={lessonHref(next)} style={{ color: K.accentDeep, fontWeight: 600 }}>
          {dict["academy.lesson.next"]} →
        </Link>
      )}
    </nav>
  );
}

function LessonView({ lesson, lang, order, dict }: { lesson: Lesson; lang: LearnLang; order: string[]; dict: AcademyDict }) {
  const { prev, next } = lessonNeighbours(order, lesson.id);
  const moduleHref = `/academy/modules/${encodeURIComponent(lesson.module_id)}`;
  return (
    <div style={{ display: "grid", gap: 16, maxWidth: 920 }} data-testid="academy-lesson">
      <PageHeader
        title={lesson.title}
        breadcrumbs={[{ label: dict["academy.nav.courses"], href: "/academy" }, { label: lesson.module_id, href: moduleHref }, { label: lesson.id }]}
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
          <LanguageNav lesson={lesson} lang={lang} dict={dict} />
          {lesson.stale && <p style={{ margin: 0, fontSize: 12.5, color: K.amber }}>{dict["academy.staleHelp"]}</p>}
          {lesson.has_video && <VideoPlayer lessonId={lesson.id} lang={lesson.lang} dict={dict} />}
          <LessonBody text={lesson.body_md} skipFirstHeading testId="academy-lesson-body" />
          <LessonProgress lessonId={lesson.id} lang={lesson.lang} status={lesson.status} moduleId={lesson.module_id} dict={dict} />
        </div>
      </Section>
      <Sources sources={lesson.sources} dict={dict} />
      <LessonNav prev={prev} next={next} moduleHref={moduleHref} dict={dict} />
    </div>
  );
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
  if (!read.ok) {
    return (
      <div style={{ display: "grid", gap: 16 }}>
        <PageHeader title={lessonId} breadcrumbs={[{ label: dict["academy.nav.courses"], href: "/academy" }]} />
        <ReadError lang={ctx.lang} kind={read.kind} />
      </div>
    );
  }
  const mod = await academyRead<Module>(ctx, `modules/${read.data.module_id}`);
  return <LessonView lesson={read.data} lang={lang} order={mod.ok ? mod.data.lessons.map((l) => l.id) : []} dict={dict} />;
}
