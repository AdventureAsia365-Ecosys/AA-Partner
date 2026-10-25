// web/app/c/p/[token]/page.tsx — C-03 (thin) + C-04 accept: the text proposal a signed link opens (AA-946, card
// CRM-18; design v3 §2.4, F6) on the client host. Public: no session, no BFF. The token stays in the path only; the
// browser reads the proposal from /crm/public/v1/p/<token> on this origin (the client host's ALB rule sends that path
// to the API), so the per-address limit keys on the real visitor and the web server never calls the API with the
// token. English unless the visitor saved a choice (owner rule S249). Referrer-Policy no-referrer (next.config).
import { notFound } from "next/navigation";
import { CenteredCard } from "@/components/CenteredCard";
import { ProposalPage } from "@/components/proposals/ProposalPage";
import { clientMessages } from "@/lib/clientMessages";
import { CLIENT_TOKEN } from "@/lib/proposals";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

export default async function ClientProposalPage({ params }: { params: Promise<{ token: string }> }) {
  const { token } = await params;
  if (!CLIENT_TOKEN.test(token)) notFound();
  const { lang, locale } = await serverContext();
  return (
    <CenteredCard testId="client-proposal-page" wide>
      <div lang={lang}>
        <ProposalPage token={token} dict={clientMessages(lang)} locale={locale} />
      </div>
    </CenteredCard>
  );
}
