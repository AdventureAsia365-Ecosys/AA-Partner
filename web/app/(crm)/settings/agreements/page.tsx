// web/app/(crm)/settings/agreements/page.tsx — Settings > Agreements (AA-946, card CRM-18; AA-955 CRM-25: the
// insurance declination and the merge fields): the agency's booking terms, traveller waiver and insurance declination
// versions. Every member reads them (the trip's Proposal tab picks the terms); only the owner
// drafts and publishes (the API checks TERMS_PUBLISH), other roles see the list and why they cannot change it.
import { ErrorState, K } from "@/app/_kit";
import { PageTitle } from "@/components/PageTitle";
import { AgreementsView } from "@/components/settings/AgreementsView";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { t } from "@/lib/i18n";
import type { Agreement, MergeField } from "@/lib/proposals";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

export default async function AgreementsSettingsPage() {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const { session } = await serverContext();
  if (!session) return null;
  const docs = await crmCall<{ agreements: Agreement[]; merge_fields?: MergeField[] }>(ctx.cfg, session, "GET", "agreements");
  return (
    <div data-testid="crm-agreements-settings" style={{ display: "grid", gap: 14, maxWidth: 820 }}>
      <a href="/settings" style={{ fontSize: 13 }}>
        ← {t(ctx.lang, "settings.title")}
      </a>
      <PageTitle title={t(ctx.lang, "agreements.title")} subtitle={t(ctx.lang, "agreements.subtitle")} />
      {!docs.ok ? (
        <ErrorState message={t(ctx.lang, "error.api")} />
      ) : ctx.role === "owner" ? (
        <AgreementsView docs={docs.data.agreements} mergeFields={docs.data.merge_fields ?? []} lang={ctx.lang} locale={ctx.locale} />
      ) : (
        <div style={{ display: "grid", gap: 8 }}>
          <p style={{ margin: 0, fontSize: 13, color: K.muted }} data-testid="agreements-owner-only">
            {t(ctx.lang, "agreements.ownerOnly")}
          </p>
          {docs.data.agreements
            .filter((d) => d.status === "published")
            .map((d) => (
              <p key={d.doc_id} style={{ margin: 0, fontSize: 13 }}>
                {t(ctx.lang, `agreements.kind.${d.kind}`)}: {d.version_label}
              </p>
            ))}
        </div>
      )}
    </div>
  );
}
