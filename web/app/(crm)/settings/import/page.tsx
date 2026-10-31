// web/app/(crm)/settings/import/page.tsx — Settings > Import clients (AA-962, card CRM-31 "phased CSV import with dry
// run"): the four phases, preview, import and the recent imports. Owner only (the API answers other roles 403; the
// page says so instead of showing the wizard).
import Link from "next/link";
import { EmptyState } from "@/app/_kit";
import { ImportWizard } from "@/components/onboarding/ImportWizard";
import { PageTitle } from "@/components/PageTitle";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { t } from "@/lib/i18n";
import type { ImportStatus } from "@/lib/onboarding";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

export default async function ImportPage() {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const owner = ctx.role === "owner";
  const { session } = await serverContext();
  const recent =
    session && owner ? await crmCall<{ imports: ImportStatus[] }>(ctx.cfg, session, "GET", "client-imports") : null;
  return (
    <div data-testid="crm-import" style={{ display: "grid", gap: 14, maxWidth: 980 }}>
      <Link href="/settings" style={{ fontSize: 13 }}>
        ← {t(ctx.lang, "settings.title")}
      </Link>
      <PageTitle title={t(ctx.lang, "onb.imp.title")} subtitle={t(ctx.lang, "onb.imp.subtitle")} />
      {owner ? (
        <ImportWizard recent={recent && recent.ok ? recent.data.imports : []} lang={ctx.lang} locale={ctx.locale} />
      ) : (
        <EmptyState title={t(ctx.lang, "onb.imp.title")} description={t(ctx.lang, "onb.imp.ownerOnly")} />
      )}
    </div>
  );
}
