// web/components/academy/AcademyRefusal.tsx — AA-1105: the academy page for a signed-in person the learning API does
// not accept (not a learner, the agency academy not open yet, another sign-in, tokens refused twice): why, and a
// sign-out of the academy (the academy session only). Server component.
import { Button, K } from "@/app/_kit";
import { CenteredCard } from "@/components/CenteredCard";
import type { AcademyErrorKind } from "@/lib/academy";
import { refusalKey } from "@/lib/academyPage";
import { t, type Lang } from "@/lib/i18n";

export function AcademyRefusal({ lang, kind }: { lang: Lang; kind: AcademyErrorKind | "refused" }) {
  return (
    <CenteredCard testId="academy-refusal" brand={t(lang, "academy.title")}>
      <div style={{ display: "grid", gap: 14, justifyItems: "start" }}>
        <h1 style={{ margin: 0, fontSize: 18, color: K.ink }}>{t(lang, "academy.notLearner.title")}</h1>
        <p style={{ margin: 0, fontSize: 13.5, color: K.muted }} data-testid="academy-refusal-message">
          {t(lang, refusalKey(kind))}
        </p>
        <form action="/academy/auth/sign-out" method="post">
          <Button type="submit" variant="secondary">
            {t(lang, "academy.signOut")}
          </Button>
        </form>
      </div>
    </CenteredCard>
  );
}
