// web/app/c/u/[token]/page.tsx — C-11, a client's e-mail preferences page of one campaign mail (AA-968, card CRM-36) on
// the client host. Public: no session, no BFF. The token stays in the path only; the browser reads and changes the
// state through /crm/public/v1/unsubscribe/<token> on this origin (the client host's ALB rule sends that path to the
// API), so the web server never calls the API with the token. Unsubscribe is one button (the mail program's one-click
// POST reaches the same API route directly); "Subscribe again" is the client's own act. English unless the visitor
// saved a choice (cookie). Referrer-Policy no-referrer (next.config).
import { notFound } from "next/navigation";
import { CenteredCard } from "@/components/CenteredCard";
import { UnsubscribePage } from "@/components/campaigns/UnsubscribePage";
import { UNSUBSCRIBE_TOKEN } from "@/lib/campaigns";
import { serverContext } from "@/lib/serverContext";
import { unsubscribeMessages } from "@/lib/unsubscribeMessages";

export const dynamic = "force-dynamic";

export default async function UnsubscribeRoute({ params }: { params: Promise<{ token: string }> }) {
  const { token } = await params;
  if (!UNSUBSCRIBE_TOKEN.test(token)) notFound();
  const { lang } = await serverContext();
  return (
    <CenteredCard testId="unsubscribe-page">
      <div lang={lang}>
        <UnsubscribePage token={token} dict={unsubscribeMessages(lang)} />
      </div>
    </CenteredCard>
  );
}
