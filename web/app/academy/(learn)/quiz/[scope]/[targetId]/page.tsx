// web/app/academy/(learn)/quiz/[scope]/[targetId]/page.tsx — an AA Academy quiz (AA-1105): /academy/quiz/module/<id>
// (the module quiz) or /academy/quiz/final/<path> (the path's final mixed quiz). Nothing is read or started on render:
// the learner starts the attempt (components/academy/QuizRunner.tsx), so a reload never opens a new attempt.
import { notFound } from "next/navigation";
import { PageHeader } from "@/app/_kit";
import { QuizRunner } from "@/components/academy/QuizRunner";
import { MODULE_ID, PATH_ID } from "@/lib/academyBff";
import { academyContext } from "@/lib/academyContext";
import { requireAcademySession } from "@/lib/academyPage";
import { academyDict } from "@/lib/i18nAcademy";

export const dynamic = "force-dynamic";

const MODULE_RE = new RegExp(`^${MODULE_ID}$`);
const PATH_RE = new RegExp(`^${PATH_ID}$`);

export default async function AcademyQuizPage({ params }: { params: Promise<{ scope: string; targetId: string }> }) {
  const { scope, targetId } = await params;
  if (!((scope === "module" && MODULE_RE.test(targetId)) || (scope === "final" && PATH_RE.test(targetId)))) notFound();
  const ctx = await academyContext();
  requireAcademySession(ctx);
  const dict = academyDict(ctx.lang);
  const isModule = scope === "module";
  const backHref = isModule ? `/academy/modules/${encodeURIComponent(targetId)}` : "/academy";
  const crumbs = [{ label: dict["academy.nav.courses"], href: "/academy" }, ...(isModule ? [{ label: targetId, href: backHref }] : [])];
  return (
    <div style={{ display: "grid", gap: 16, maxWidth: 880 }}>
      <PageHeader title={isModule ? dict["academy.quiz.title"] : dict["academy.quiz.finalTitle"]} breadcrumbs={crumbs} />
      <QuizRunner
        scope={isModule ? "module" : "final"}
        targetId={targetId}
        backHref={backHref}
        backLabel={isModule ? dict["academy.quiz.backToModule"] : dict["academy.quiz.backToCourses"]}
        origin={ctx.cfg.publicOrigin}
        locale={ctx.locale}
        dict={dict}
      />
    </div>
  );
}
