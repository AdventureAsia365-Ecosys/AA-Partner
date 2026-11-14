// web/app/academy/(learn)/layout.tsx — every AA Academy learner page (AA-1105): the ACADEMY session (the proxy already
// sent a page without a valid academy token through /academy/auth/refresh; this is the second line), the learner from
// GET /learn/v1/me, and the academy shell. A signed-in person CIS does not accept as a learner (403 not_a_learner,
// partner_not_open, forbidden) sees why, with a sign-out, instead of the shell. No other session is read here.
import type { Metadata } from "next";
import { AcademyShell } from "@/app/_kit/local/AcademyShell";
import { AcademyRefusal } from "@/components/academy/AcademyRefusal";
import type { Me } from "@/lib/academy";
import { academyContext } from "@/lib/academyContext";
import { academyRead } from "@/lib/academyPage";
import { dictionary } from "@/lib/i18n";

export const dynamic = "force-dynamic";

export const metadata: Metadata = { title: "AA Academy", robots: { index: false, follow: false } };

export default async function AcademyLearnerLayout({ children }: { children: React.ReactNode }) {
  const ctx = await academyContext();
  const me = await academyRead<Me>(ctx, "me");
  if (!me.ok && me.kind !== "unavailable") return <AcademyRefusal lang={ctx.lang} kind={me.kind} />;
  return (
    <AcademyShell
      dict={dictionary(ctx.lang)}
      locale={ctx.locale}
      environment={ctx.cfg.environment}
      learnerName={me.ok ? me.data.display_name : null}
      testLearner={me.ok && me.data.is_test}
    >
      {children}
    </AcademyShell>
  );
}
