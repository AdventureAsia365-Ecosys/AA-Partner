// web/app/(crm)/inbox/page.tsx — Inbox (AA-945, card CRM-17): the Unmatched conversations (forwarded mail that no trip
// token, reply header or client address placed) with "Link to client". Threads that found their trip are on the trip's
// Messages tab.
import { ErrorState } from "@/app/_kit";
import { PageTitle } from "@/components/PageTitle";
import { UnmatchedInbox } from "@/components/mail/UnmatchedInbox";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { t } from "@/lib/i18n";
import type { Conversation } from "@/lib/mail";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

export default async function InboxPage() {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const { session } = await serverContext();
  if (!session) return null;
  const list = await crmCall<{ conversations: Conversation[] }>(ctx.cfg, session, "GET", "conversations", {
    query: new URLSearchParams({ unmatched: "true" }),
  });
  return (
    <div data-testid="crm-inbox" style={{ display: "grid", gap: 14, maxWidth: 860 }}>
      <PageTitle title={t(ctx.lang, "inbox.title")} subtitle={t(ctx.lang, "inbox.subtitle")} />
      {!list.ok ? (
        <ErrorState message={t(ctx.lang, "error.api")} />
      ) : (
        <UnmatchedInbox conversations={list.data.conversations} lang={ctx.lang} locale={ctx.locale} />
      )}
    </div>
  );
}
