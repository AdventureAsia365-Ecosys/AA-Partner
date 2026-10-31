// web/app/(crm)/settings/connections/page.tsx — Settings > Connections and privacy (AA-962, card CRM-31; errata S247
// §2): the status of every connection the agency brings and the controller settings. Every role sees the status; the
// owner changes the privacy settings (the API decides).
import Link from "next/link";
import { ErrorState } from "@/app/_kit";
import { ConnectionsView } from "@/components/onboarding/ConnectionsView";
import { PageTitle } from "@/components/PageTitle";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { t } from "@/lib/i18n";
import type { OnboardingView } from "@/lib/onboarding";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

export default async function ConnectionsPage() {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const { session } = await serverContext();
  const view = session ? await crmCall<OnboardingView>(ctx.cfg, session, "GET", "onboarding") : null;
  return (
    <div data-testid="crm-connections" style={{ display: "grid", gap: 14, maxWidth: 900 }}>
      <Link href="/settings" style={{ fontSize: 13 }}>
        ← {t(ctx.lang, "settings.title")}
      </Link>
      <PageTitle title={t(ctx.lang, "onb.settings.connections")} subtitle={t(ctx.lang, "onb.conn.subtitle")} />
      {!view || !view.ok ? (
        <ErrorState message={t(ctx.lang, "onb.error")} />
      ) : (
        <ConnectionsView view={view.data} owner={ctx.role === "owner"} lang={ctx.lang} />
      )}
    </div>
  );
}
