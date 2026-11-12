// web/app/(crm)/settings/inclusions/page.tsx — Settings > Proposal lines (AA-1070 item crm-35, crm-059): the agency's
// library of standard "Included" / "Not included" lines the proposal builder offers. Every role reads it; the owner and
// agents change it (the API refuses the assistant).
import Link from "next/link";
import { ErrorState } from "@/app/_kit";
import { PageTitle } from "@/components/PageTitle";
import { InclusionLibraryView } from "@/components/settings/InclusionLibraryView";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { t } from "@/lib/i18n";
import type { InclusionItem } from "@/lib/inclusions";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

export default async function InclusionSettingsPage() {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const { session } = await serverContext();
  if (!session) return null;
  const library = await crmCall<{ items: InclusionItem[] }>(ctx.cfg, session, "GET", "inclusion-items", {
    query: new URLSearchParams({ archived: "true" }),
  });
  const canEdit = ctx.role === "owner" || ctx.role === "agent";
  return (
    <div data-testid="crm-inclusion-settings" style={{ display: "grid", gap: 14, maxWidth: 820 }}>
      <Link href="/settings" style={{ fontSize: 13 }}>
        ← {t(ctx.lang, "settings.title")}
      </Link>
      <PageTitle title={t(ctx.lang, "incl.title")} subtitle={t(ctx.lang, canEdit ? "incl.subtitle" : "incl.subtitleReadOnly")} />
      {library.ok ? (
        <InclusionLibraryView items={library.data.items} canEdit={canEdit} lang={ctx.lang} />
      ) : (
        <ErrorState message={t(ctx.lang, "error.api")} />
      )}
    </div>
  );
}
