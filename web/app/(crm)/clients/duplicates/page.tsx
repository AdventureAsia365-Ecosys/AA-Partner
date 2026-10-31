// web/app/(crm)/clients/duplicates/page.tsx — Clients > Duplicates (AA-962, card CRM-31 "merge screen"; design v3 R6,
// A-06): possible duplicate clients and the merge (owner). Every role sees the pairs (display names only, one
// list_view audit event per load, written by the API).
import Link from "next/link";
import { ErrorState } from "@/app/_kit";
import { DuplicatesView } from "@/components/onboarding/DuplicatesView";
import { PageTitle } from "@/components/PageTitle";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { t } from "@/lib/i18n";
import type { DuplicatesView as View } from "@/lib/onboarding";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

export default async function DuplicatesPage() {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const { session } = await serverContext();
  const view = session ? await crmCall<View>(ctx.cfg, session, "GET", "client-duplicates") : null;
  return (
    <div data-testid="crm-duplicates" style={{ display: "grid", gap: 14, maxWidth: 980 }}>
      <Link href="/clients" style={{ fontSize: 13 }}>
        ← {t(ctx.lang, "nav.clients")}
      </Link>
      <PageTitle title={t(ctx.lang, "onb.dup.title")} subtitle={t(ctx.lang, "onb.dup.subtitle")} />
      {!view || !view.ok ? (
        <ErrorState message={t(ctx.lang, "onb.error")} />
      ) : (
        <DuplicatesView view={view.data} owner={ctx.role === "owner"} lang={ctx.lang} locale={ctx.locale} />
      )}
    </div>
  );
}
