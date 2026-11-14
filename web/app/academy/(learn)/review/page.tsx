// web/app/academy/(learn)/review/page.tsx — AA-1105: the learner's spaced review (GET /learn/v1/review/due): missed
// quiz questions come back on an FSRS schedule, capped per day by the setting the API applies and returns.
import { EmptyState, PageHeader } from "@/app/_kit";
import { ReadError } from "@/components/academy/ReadError";
import { ReviewRunner } from "@/components/academy/ReviewRunner";
import type { ReviewDue } from "@/lib/academy";
import { academyContext } from "@/lib/academyContext";
import { academyRead } from "@/lib/academyPage";
import { academyDict, say } from "@/lib/i18nAcademy";

export const dynamic = "force-dynamic";

export default async function AcademyReviewPage() {
  const ctx = await academyContext();
  const dict = academyDict(ctx.lang);
  const due = await academyRead<ReviewDue>(ctx, "review/due");
  return (
    <div style={{ display: "grid", gap: 16, maxWidth: 880 }} data-testid="academy-review">
      <PageHeader
        title={dict["academy.review.title"]}
        description={due.ok ? `${dict["academy.review.subtitle"]} ${say(dict, "academy.review.cap", { n: due.data.daily_cap })}` : dict["academy.review.subtitle"]}
      />
      {!due.ok ? (
        <ReadError lang={ctx.lang} kind={due.kind} />
      ) : due.data.cards.length === 0 ? (
        <EmptyState title={dict["academy.review.empty"]} />
      ) : (
        <ReviewRunner cards={due.data.cards} targetRetention={due.data.target_retention} dict={dict} />
      )}
    </div>
  );
}
