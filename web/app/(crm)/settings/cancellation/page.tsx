// web/app/(crm)/settings/cancellation/page.tsx — Settings > Cancellation policy (AA-1067, crm-049): the agency's
// charge table for travellers who cancel, by days before departure. Every role reads it (agents see what a cancel
// will charge); only the owner starts, edits, publishes or retires a version.
import Link from "next/link";
import { ErrorState } from "@/app/_kit";
import { PageTitle } from "@/components/PageTitle";
import { CancellationPoliciesView } from "@/components/settings/CancellationPoliciesView";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { t } from "@/lib/i18n";
import type { PolicyListing } from "@/lib/retailPayments";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

export default async function CancellationPolicyPage() {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const { session } = await serverContext();
  if (!session) return null;
  const view = await crmCall<PolicyListing>(ctx.cfg, session, "GET", "cancellation-policies");
  return (
    <div data-testid="crm-cancellation-settings" style={{ display: "grid", gap: 14, maxWidth: 820 }}>
      <Link href="/settings" style={{ fontSize: 13 }}>
        ← {t(ctx.lang, "settings.title")}
      </Link>
      <PageTitle title={t(ctx.lang, "policy.title")} subtitle={t(ctx.lang, ctx.role === "owner" ? "policy.subtitle" : "policy.ownerOnly")} />
      {view.ok ? (
        <CancellationPoliciesView view={view.data} lang={ctx.lang} locale={ctx.locale} isOwner={ctx.role === "owner"} />
      ) : (
        <ErrorState message={t(ctx.lang, "error.api")} />
      )}
    </div>
  );
}
