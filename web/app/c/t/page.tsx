// web/app/c/t/page.tsx — the traveller portal per person (AA-946 base, AA-953 card CRM-24a; design v3 C-05 .. C-10) on
// the client host. Public: no agency session, no BFF. The traveller's own link was exchanged for the host-only portal
// cookie by /c/t/<token> (§2.9 rule 1); the browser talks to /crm/public/v1/traveller on this origin with that cookie,
// and the API keeps the portal session in an httpOnly cookie on the same path, so this page never holds either.
// English unless the visitor saved a choice (owner rule S249). Referrer-Policy no-referrer (next.config).
import { CenteredCard } from "@/components/CenteredCard";
import { PortalPage } from "@/components/portal/PortalPage";
import { clientMessages } from "@/lib/clientMessages";
import { PORTAL_API } from "@/lib/clientPortal";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

export default async function TravellerPortalPage() {
  const { lang, locale } = await serverContext();
  return (
    <CenteredCard testId="client-portal-page" wide>
      <div lang={lang}>
        <PortalPage base={PORTAL_API} dict={clientMessages(lang)} locale={locale} />
      </div>
    </CenteredCard>
  );
}
