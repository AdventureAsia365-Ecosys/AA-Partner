// web/app/(crm)/partners/page.tsx — AA-1071 (crm-056; S275 study items crm-28 and crm-29): the agency's sales partners
// (sub-agents, referrers, freelancers, online agencies) with their commission terms and links, and the partner
// commissions of booked trips (a cost in each trip's P&L). Every member sees the partner names; owner and agent see
// the terms, the amounts and the commissions; only the owner manages partners, links and approvals (the API and the
// database refuse anyone else). The commission filter is its own URL (/partners?status=...).
import { ErrorState, K, KpiTile } from "@/app/_kit";
import { FilterTabs } from "@/components/FilterTabs";
import { PageTitle } from "@/components/PageTitle";
import type { FormRow } from "@/components/pipeline/FormsAdmin";
import { PartnerCommissionsTable } from "@/components/partners/PartnerCommissionsTable";
import { PartnersTable } from "@/components/partners/PartnersTable";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { fmt, t, type MessageKey } from "@/lib/i18n";
import { canManage, FILTERS, isFilter, type PartnerCommissionList, type PartnerList, seesMoney } from "@/lib/partners";
import { formatAmount } from "@/lib/proposals";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

export default async function PartnersPage({ searchParams }: { searchParams: Promise<{ status?: string }> }) {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const { session } = await serverContext();
  if (!session) return null;
  const tr = (key: string) => t(ctx.lang, key as MessageKey);
  const { status: raw } = await searchParams;
  const status = isFilter(raw) ? raw : "open";
  const money = seesMoney(ctx.role);
  const owner = canManage(ctx.role);
  const [partners, commissions, forms] = await Promise.all([
    crmCall<PartnerList>(ctx.cfg, session, "GET", "sales-partners"),
    money ? crmCall<PartnerCommissionList>(ctx.cfg, session, "GET", "partner-commissions", { query: new URLSearchParams({ status }) }) : null,
    owner ? crmCall<{ forms: FormRow[] }>(ctx.cfg, session, "GET", "intake-forms") : null,
  ]);
  if (!partners.ok) return <ErrorState message={tr("error.api")} />;
  const base = partners.data.base_currency;
  const amount = (value: string | null | undefined, currency: string) => formatAmount(value, currency, ctx.locale);
  const names = Object.fromEntries(partners.data.items.map((p) => [p.sales_partner_id, p.name]));
  const clientHost = ctx.cfg.clientHosts[0];
  const activeForms = forms?.ok ? forms.data.forms.filter((f) => f.status === "active").map((f) => ({ intake_form_id: f.intake_form_id, label: f.label })) : [];
  return (
    <div data-testid="crm-partners" data-crm-toast-room="" style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 14, maxWidth: 1200 }}>
      <PageTitle title={tr("partners.title")} subtitle={tr("partners.subtitle")} />
      <PartnersTable
        partners={partners.data.items}
        base={base}
        role={ctx.role}
        forms={activeForms}
        clientOrigin={clientHost ? `https://${clientHost}` : null}
        lang={ctx.lang}
        locale={ctx.locale}
      />
      {commissions && (
        <section data-testid="partner-commissions" style={{ display: "grid", gap: 12, minWidth: 0 }}>
          <h2 style={{ margin: "8px 0 0", fontSize: 17, color: K.ink }}>{tr("partners.commissions")}</h2>
          {!commissions.ok ? (
            <ErrorState message={tr("error.api")} />
          ) : (
            <>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 12 }}>
                <KpiTile size="sm" testId="partner-total-accrued" label={tr("partners.commissions.total.accrued")} value={amount(commissions.data.totals.accrued, base)} />
                <KpiTile size="sm" testId="partner-total-approved" label={tr("partners.commissions.total.approved")} value={amount(commissions.data.totals.approved, base)} />
                <KpiTile size="sm" testId="partner-total-paid" label={tr("partners.commissions.total.paid")} value={amount(commissions.data.totals.paid, base)} />
              </div>
              {commissions.data.unconverted > 0 && (
                <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{fmt(tr("partners.commissions.unconverted"), { n: commissions.data.unconverted })}</p>
              )}
              {!owner && <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("partners.ownerOnly")}</p>}
              <FilterTabs
                testId="partner-commission-filters"
                ariaLabel={tr("partners.commissions")}
                active={status}
                tabs={FILTERS.map((f) => ({ key: f, label: tr(`partners.filter.${f}`), href: f === "open" ? "/partners" : `/partners?status=${f}` }))}
              />
              {commissions.data.items.length === 0 ? (
                <p style={{ margin: 0, fontSize: 13, color: K.muted }} data-testid="partner-commissions-empty">
                  {tr("partners.commissions.empty")}
                </p>
              ) : (
                <PartnerCommissionsTable items={commissions.data.items} names={names} owner={owner} lang={ctx.lang} locale={ctx.locale} />
              )}
            </>
          )}
        </section>
      )}
    </div>
  );
}
