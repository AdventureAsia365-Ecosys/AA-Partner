// web/app/c/s/[token]/page.tsx — C-02, the Stage 2 planning form a signed link opens (AA-943, card CRM-15; design v3
// §2.4, §2.7 F2, R31) on the client host. Public: no session, no BFF. The token stays in the path only; the browser
// reads the form from /crm/public/v1/s/<token> on this origin (the client host's ALB rule sends that path to the API),
// so the API's per-address limit keys on the real visitor, the first open is the visitor's (link sent -> opened) and
// the web server never calls the API with the token. The page words everything in English unless the visitor saved a choice
// (cookie); the agency's default_locale does not switch it (owner rule S249). Referrer-Policy no-referrer (next.config).
import { notFound } from "next/navigation";
import { CenteredCard } from "@/components/CenteredCard";
import { PlanningPage } from "@/components/intake/PlanningPage";
import { planMessages } from "@/lib/planMessages";
import { LINK_TOKEN } from "@/lib/planningForm";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

export default async function PlanningFormPage({ params }: { params: Promise<{ token: string }> }) {
  const { token } = await params;
  if (!LINK_TOKEN.test(token)) notFound();
  const { lang } = await serverContext();
  return (
    <CenteredCard testId="plan-page" wide>
      <div lang={lang} data-testid="plan-page-body">
        <PlanningPage token={token} dict={planMessages(lang)} />
      </div>
    </CenteredCard>
  );
}
