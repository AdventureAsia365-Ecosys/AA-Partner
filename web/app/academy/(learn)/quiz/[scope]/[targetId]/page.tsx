// web/app/academy/(learn)/quiz/[scope]/[targetId]/page.tsx — an AA Academy quiz (AA-1105): /academy/quiz/module/<id>
// (the module quiz) or /academy/quiz/final/<path> (the path's final mixed quiz). Nothing is started on render:
// the learner starts the attempt (components/academy/QuizRunner.tsx), so a reload never opens a new attempt.
// AA-1129 (A1, S283 audit): the page reads what the learner should know before starting (GET modules/<id>: the module
// title, questions per attempt, pass mark, best score, passed; GET paths: the path title and a certificate already
// held). An unknown module or path answers 404; any other failed read only drops this context line.
import { notFound } from "next/navigation";
import { Badge, K, PageHeader } from "@/app/_kit";
import { QuizRunner } from "@/components/academy/QuizRunner";
import type { Module, PathList } from "@/lib/academy";
import { MODULE_ID, PATH_ID } from "@/lib/academyBff";
import { academyContext, type AcademyContext } from "@/lib/academyContext";
import { academyRead, requireAcademySession } from "@/lib/academyPage";
import { academyDict, say, type AcademyDict } from "@/lib/i18nAcademy";

export const dynamic = "force-dynamic";

const MODULE_RE = new RegExp(`^${MODULE_ID}$`);
const PATH_RE = new RegExp(`^${PATH_ID}$`);

function ModuleContext({ module: m, dict }: { module: Module; dict: AcademyDict }) {
  const q = m.quiz;
  return (
    <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", fontSize: 13.5, color: K.body }} data-testid="academy-quiz-context">
      {q.available && <span>{say(dict, "academy.quiz.rules", { items: q.items_per_attempt, pct: q.pass_mark_pct })}</span>}
      {q.best_score_pct !== null && <span style={{ color: K.muted }}>{say(dict, "academy.quiz.best", { pct: Math.round(q.best_score_pct) })}</span>}
      {q.passed && <Badge tone="success">{dict["academy.quiz.passed"]}</Badge>}
    </div>
  );
}

type QuizContext = { description?: string; context: React.ReactNode };

async function moduleQuizContext(ctx: AcademyContext, moduleId: string, dict: AcademyDict): Promise<QuizContext> {
  const read = await academyRead<Module>(ctx, `modules/${moduleId}`);
  if (!read.ok && read.kind === "not_found") notFound();
  if (!read.ok) return { context: null };
  return { description: `${read.data.id} · ${read.data.title}`, context: <ModuleContext module={read.data} dict={dict} /> };
}

async function finalQuizContext(ctx: AcademyContext, pathId: string, dict: AcademyDict): Promise<QuizContext> {
  const read = await academyRead<PathList>(ctx, "paths");
  if (!read.ok) return { context: null };
  const path = read.data.paths.find((p) => p.id === pathId);
  if (!path || !path.final_quiz) notFound();
  if (path.certificate_status !== "valid") return { description: path.title, context: null };
  return {
    description: path.title,
    context: (
      <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", fontSize: 13.5, color: K.body }} data-testid="academy-quiz-context">
        <Badge tone="success">{dict["academy.quiz.passed"]}</Badge>
        <span>{dict["academy.quiz.holdsCertificate"]}</span>
      </div>
    ),
  };
}

/** The header and the way back of a quiz page: the module's page for a module quiz, the catalogue for a final quiz. */
function quizNav(isModule: boolean, targetId: string, dict: AcademyDict) {
  const backHref = isModule ? `/academy/modules/${encodeURIComponent(targetId)}` : "/academy";
  return {
    backHref,
    title: isModule ? dict["academy.quiz.title"] : dict["academy.quiz.finalTitle"],
    backLabel: isModule ? dict["academy.quiz.backToModule"] : dict["academy.quiz.backToCourses"],
    crumbs: [{ label: dict["academy.nav.courses"], href: "/academy" }, ...(isModule ? [{ label: targetId, href: backHref }] : [])],
  };
}

export default async function AcademyQuizPage({ params }: { params: Promise<{ scope: string; targetId: string }> }) {
  const { scope, targetId } = await params;
  if (!((scope === "module" && MODULE_RE.test(targetId)) || (scope === "final" && PATH_RE.test(targetId)))) notFound();
  const ctx = await academyContext();
  requireAcademySession(ctx);
  const dict = academyDict(ctx.lang);
  const isModule = scope === "module";
  const nav = quizNav(isModule, targetId, dict);
  const { description, context } = isModule ? await moduleQuizContext(ctx, targetId, dict) : await finalQuizContext(ctx, targetId, dict);

  return (
    <div style={{ display: "grid", gap: 16, maxWidth: 880 }}>
      <PageHeader title={nav.title} description={description} breadcrumbs={nav.crumbs} />
      {context}
      <QuizRunner
        scope={isModule ? "module" : "final"}
        targetId={targetId}
        backHref={nav.backHref}
        backLabel={nav.backLabel}
        origin={ctx.cfg.publicOrigin}
        locale={ctx.locale}
        dict={dict}
      />
    </div>
  );
}
