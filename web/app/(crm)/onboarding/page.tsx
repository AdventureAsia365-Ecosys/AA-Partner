// web/app/(crm)/onboarding/page.tsx — Getting started (AA-962, card CRM-31, A-20; design v3 §2.7 Fix E2): the full
// checklist (three required steps, optional steps with Later, the ones on later), the shared-responsibility
// acknowledgement and links to Connections, Import and Help. Every role reads; the owner marks (the API decides).
import { ErrorState } from "@/app/_kit";
import { OnboardingChecklist } from "@/components/onboarding/OnboardingChecklist";
import { PageTitle } from "@/components/PageTitle";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { t } from "@/lib/i18n";
import type { OnboardingView } from "@/lib/onboarding";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

export default async function OnboardingPage() {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const { session } = await serverContext();
  const view = session ? await crmCall<OnboardingView>(ctx.cfg, session, "GET", "onboarding") : null;
  return (
    <div data-testid="crm-onboarding" style={{ display: "grid", gap: 14, maxWidth: 900 }}>
      <PageTitle title={t(ctx.lang, "onb.title")} subtitle={t(ctx.lang, "onb.subtitle")} />
      {!view || !view.ok ? (
        <ErrorState message={t(ctx.lang, "onb.error")} />
      ) : (
        <OnboardingChecklist view={view.data} owner={ctx.role === "owner"} lang={ctx.lang} locale={ctx.locale} />
      )}
    </div>
  );
}
