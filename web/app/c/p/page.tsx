// web/app/c/p/page.tsx — C-03 / C-04 / C-12 (AA-946 text proposal; AA-950 day-by-day proposal: Keep / Swap, hold,
// accept) on the client host. Public: no session, no BFF. The signed link was exchanged for the host-only proposal
// cookie by /c/p/<token> (Fix B11); the browser reads the page from /crm/public/v1/proposal on this origin with that
// cookie. English unless the visitor saved a choice (owner rule S249). Referrer-Policy no-referrer (next.config).
import { CenteredCard } from "@/components/CenteredCard";
import { ProposalPage } from "@/components/proposals/ProposalPage";
import { clientMessages } from "@/lib/clientMessages";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

export default async function ClientProposalPage() {
  const { lang, locale } = await serverContext();
  return (
    <CenteredCard testId="client-proposal-page" wide>
      <div lang={lang}>
        <ProposalPage dict={clientMessages(lang)} locale={locale} />
      </div>
    </CenteredCard>
  );
}
