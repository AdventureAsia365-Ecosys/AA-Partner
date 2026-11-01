// web/app/(crm)/settings/channels/page.tsx — Settings > SMS and WhatsApp (AA-958, card CRM-28): the agency's own Twilio
// connection, its webhook addresses, the test and the approved WhatsApp templates. Every role sees the status; the
// owner connects and changes it (the API decides).
import Link from "next/link";
import { ErrorState } from "@/app/_kit";
import { ChannelsSettings } from "@/components/messaging/ChannelsSettings";
import { PageTitle } from "@/components/PageTitle";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { t } from "@/lib/i18n";
import type { MessagingView } from "@/lib/messaging";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

export default async function ChannelsPage() {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const { session } = await serverContext();
  if (!session) return null;
  const view = await crmCall<MessagingView>(ctx.cfg, session, "GET", "messaging");
  return (
    <div data-testid="crm-channels" style={{ display: "grid", gap: 14, maxWidth: 820 }}>
      <Link href="/settings" style={{ fontSize: 13 }}>
        ← {t(ctx.lang, "settings.title")}
      </Link>
      <PageTitle title={t(ctx.lang, "msg.title")} subtitle={t(ctx.lang, "msg.subtitle")} />
      {!view.ok ? (
        <ErrorState message={t(ctx.lang, "error.api")} />
      ) : (
        <ChannelsSettings view={view.data} lang={ctx.lang} locale={ctx.locale} />
      )}
    </div>
  );
}
