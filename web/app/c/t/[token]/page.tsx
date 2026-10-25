// web/app/c/t/[token]/page.tsx — the traveller portal base (AA-946, card CRM-18; design v3 C-05 .. C-08, C-10) on
// the client host: each traveller's own signed link, then a 6-digit e-mail code. Public: no agency session, no BFF.
// The browser talks to /crm/public/v1/portal/<token> on this origin; the API keeps the portal session in an httpOnly
// cookie on that path, so this page never holds it. Referrer-Policy no-referrer (next.config).
import { notFound } from "next/navigation";
import { CenteredCard } from "@/components/CenteredCard";
import { PortalPage } from "@/components/portal/PortalPage";
import { clientMessages } from "@/lib/clientMessages";
import { CLIENT_TOKEN } from "@/lib/proposals";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

export default async function TravellerPortalPage({ params }: { params: Promise<{ token: string }> }) {
  const { token } = await params;
  if (!CLIENT_TOKEN.test(token)) notFound();
  const { lang, locale } = await serverContext();
  return (
    <CenteredCard testId="client-portal-page" wide>
      <div lang={lang}>
        <PortalPage token={token} dict={clientMessages(lang)} locale={locale} />
      </div>
    </CenteredCard>
  );
}
