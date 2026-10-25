// web/app/(crm)/settings/mailbox/page.tsx — Settings > Mailbox (AA-945, card CRM-17): the member's own mailbox, the
// agency's (owner), the forward-in address and the sender table. ?code&state (or ?oauth_error) arrive from
// /connect/mail/callback after the provider's consent; the view completes the connection.
import { ErrorState } from "@/app/_kit";
import { PageTitle } from "@/components/PageTitle";
import { MailboxSettings } from "@/components/mail/MailboxSettings";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { t } from "@/lib/i18n";
import type { IngestAddress, MailboxView, SenderRow } from "@/lib/mail";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

export default async function MailboxPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const { session } = await serverContext();
  if (!session) return null;
  const query = await searchParams;
  const one = (v: string | string[] | undefined) => (typeof v === "string" ? v : "");
  const oauthError = one(query.oauth_error);
  const oauth = oauthError
    ? { error: ["denied", "provider", "invalid"].includes(oauthError) ? oauthError : "invalid" }
    : one(query.code) && one(query.state)
      ? { code: one(query.code), state: one(query.state) }
      : null;
  const [view, ingest, senders] = await Promise.all([
    crmCall<MailboxView>(ctx.cfg, session, "GET", "mailbox"),
    crmCall<IngestAddress>(ctx.cfg, session, "GET", "mailbox/ingest-address"),
    crmCall<{ senders: SenderRow[] }>(ctx.cfg, session, "GET", "mail/senders"),
  ]);
  return (
    <div data-testid="crm-mailbox-settings" data-card="AA-945" style={{ display: "grid", gap: 14, maxWidth: 820 }}>
      <a href="/settings" style={{ fontSize: 13 }}>
        ← {t(ctx.lang, "settings.title")}
      </a>
      <PageTitle title={t(ctx.lang, "mail.title")} subtitle={t(ctx.lang, "mail.subtitle")} />
      {!view.ok ? (
        <ErrorState message={t(ctx.lang, "error.api")} />
      ) : (
        <MailboxSettings
          view={view.data}
          ingest={ingest.ok ? ingest.data : null}
          senders={senders.ok ? senders.data.senders : []}
          role={ctx.role}
          lang={ctx.lang}
          locale={ctx.locale}
          oauth={oauth}
        />
      )}
    </div>
  );
}
