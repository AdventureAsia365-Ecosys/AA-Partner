// web/app/(crm)/settings/add-ons/page.tsx — Settings > Add-ons (AA-1036 R3, root ADR 0019 d7): the CRM owner sees the
// agency's add-ons and asks Adventure Asia for the Trip planner (or the CRM) without a Content studio membership. Found
// live on Dev (S269): the sidebar's Trip planner entry led a CRM-only owner to the studio's "No Content studio access".
// GET /crm/v1/add-ons is the owner's only (the API refuses the others, and CIS is never asked); the request is the
// view's POST through the BFF. The requests live in CIS, so the studio's Account › Add-ons page shows the same ones.
import Link from "next/link";
import { ErrorState } from "@/app/_kit";
import { PageTitle } from "@/components/PageTitle";
import { AddOnsView } from "@/components/settings/AddOnsView";
import { type AddOnList, loadErrorKey } from "@/lib/addOns";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { t } from "@/lib/i18n";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

export default async function AddOnsPage() {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const { session } = await serverContext();
  if (!session) return null;
  const owner = ctx.role === "owner";
  const list = owner ? await crmCall<AddOnList>(ctx.cfg, session, "GET", "add-ons") : null;
  return (
    <div data-testid="crm-add-ons" style={{ display: "grid", gap: 14, maxWidth: 820 }}>
      <Link href="/settings" style={{ fontSize: 13 }}>
        ← {t(ctx.lang, "settings.title")}
      </Link>
      <PageTitle title={t(ctx.lang, "addons.title")} subtitle={t(ctx.lang, "addons.subtitle")} />
      {!owner || !list ? (
        <p data-testid="addons-owner-only" style={{ margin: 0, fontSize: 13 }}>
          {t(ctx.lang, "addons.ownerOnly")}
        </p>
      ) : !list.ok ? (
        <ErrorState message={t(ctx.lang, loadErrorKey(list.status, list.detail))} />
      ) : (
        <AddOnsView list={list.data} lang={ctx.lang} locale={ctx.locale} />
      )}
    </div>
  );
}
