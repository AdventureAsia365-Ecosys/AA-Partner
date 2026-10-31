// web/app/(crm)/reports/page.tsx — A-14 Reports (AA-966, card CRM-34; spec module 16, design v3 R23, R28): the agency's
// numbers for a period in its own time zone: the KPI tiles against the same period last year (or the last three months
// for an agency without a year of history), the funnel and win rate, open trips and stalled ones, Lost / cancel
// reasons, lead sources, the client's option picks, revenue and margin by month (owner / agent only), top products,
// outstanding payments, team activity and the spec's success metrics; every section downloads as CSV. Every number
// comes from the API, computed from the agency's own rows only (RLS); the assistant never receives money.
import { Badge, EmptyState, ErrorState, K } from "@/app/_kit";
import { Panel, PageTitle } from "@/components/PageTitle";
import { ExportMenu } from "@/components/reports/ExportMenu";
import { BarList, TileRow } from "@/components/reports/ReportParts";
import { ReportTable } from "@/components/reports/ReportTable";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { formatDate } from "@/lib/dates";
import { fmt, t, type MessageKey } from "@/lib/i18n";
import { formatMoney } from "@/lib/ledger";
import { memberLabel, type Member } from "@/lib/pipeline";
import {
  PERIODS,
  codeText,
  countText,
  pageHref,
  parseFilters,
  pctText,
  reportQuery,
  successTarget,
  successValue,
  type ReportView,
} from "@/lib/reports";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

const STATUS_TONE = { met: "success", below: "warning", no_data: "neutral", info: "info", not_built: "neutral" } as const;

export default async function ReportsPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const { session } = await serverContext();
  if (!session) return null;
  const filters = parseFilters(await searchParams);
  const [view, people] = await Promise.all([
    crmCall<ReportView>(ctx.cfg, session, "GET", "reports/summary", { query: reportQuery(filters) }),
    crmCall<{ members: Member[] }>(ctx.cfg, session, "GET", "members"),
  ]);
  const { lang, locale } = ctx;
  const tr = (key: string) => t(lang, key as MessageKey);
  if (!view.ok) return <ErrorState message={tr("error.api")} />;
  const r = view.data;
  const members = people.ok ? people.data.members.filter((m) => m.status === "active") : [];
  const label = (id: string | null) => {
    const m = members.find((x) => x.member_id === id);
    return m ? memberLabel(lang, m) : tr("reports.formerMember");
  };
  const currency = r.period.base_currency;
  const money = (v: string | null | undefined, cur: string = currency) => formatMoney(v, cur, locale);
  const n = (v: number | null | undefined) => countText(v, locale);
  const pct = (v: number | null | undefined) => pctText(v, locale);
  const empty =
    r.funnel.created === 0 && r.open_trips.every((s) => s.count === 0) && (!r.money || r.money.lines === 0);

  return (
    <div data-testid="crm-reports" style={{ display: "grid", gap: 14, maxWidth: 1200, minWidth: 0 }}>
      <PageTitle
        title={tr("reports.title")}
        subtitle={fmt(tr("reports.subtitle"), {
          from: formatDate(r.period.from, locale),
          to: formatDate(r.period.to, locale),
          tz: r.period.timezone,
        })}
      />
      <div style={{ display: "flex", flexWrap: "wrap", gap: 12, alignItems: "center", justifyContent: "space-between" }}>
        <nav aria-label={tr("reports.period")} style={{ display: "flex", flexWrap: "wrap", gap: 10, fontSize: 13 }}>
          {PERIODS.map((p) => (
            <a
              key={p}
              href={pageHref({ ...filters, period: p })}
              aria-current={p === filters.period ? "page" : undefined}
              style={{ fontWeight: p === filters.period ? 600 : 400 }}
            >
              {tr(`reports.period.${p}`)}
            </a>
          ))}
        </nav>
        <ExportMenu lang={lang} period={filters.period} owner={filters.owner} moneyVisible={r.money_visible} />
      </div>
      <nav aria-label={tr("reports.owner")} style={{ display: "flex", flexWrap: "wrap", gap: 10, fontSize: 12.5 }}>
        <span style={{ color: K.muted }}>{tr("reports.owner")}:</span>
        <a href={pageHref({ ...filters, owner: null })} aria-current={filters.owner === null ? "page" : undefined}>
          {tr("reports.owner.all")}
        </a>
        <a href={pageHref({ ...filters, owner: "me" })} aria-current={filters.owner === "me" ? "page" : undefined}>
          {tr("reports.owner.me")}
        </a>
        {members
          .filter((m) => !m.is_me)
          .map((m) => (
            <a
              key={m.member_id}
              href={pageHref({ ...filters, owner: m.member_id })}
              aria-current={filters.owner === m.member_id ? "page" : undefined}
            >
              {memberLabel(lang, m)}
            </a>
          ))}
      </nav>

      <TileRow
        tiles={r.tiles}
        compare={r.period.compare}
        currency={currency}
        lang={lang}
        locale={locale}
        testId="reports-tiles"
      />
      {r.period.compare === "trend_3m" && (
        <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("reports.newAgency")}</p>
      )}

      {empty ? (
        <EmptyState title={tr("reports.empty.title")} description={tr("reports.empty.body")} />
      ) : (
        <div style={{ display: "grid", gap: 14, gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 340px), 1fr))" }}>
          <Panel title={tr("reports.funnel")} testId="reports-funnel">
            <BarList
              rows={r.funnel.stages
                .filter((s) => s.stage !== "pre_departure" && s.stage !== "travelling")
                .map((s) => ({ key: s.stage, label: tr(`stage.${s.stage}`), value: s.count, text: n(s.count) }))}
              empty={tr("reports.none")}
            />
            <p style={{ margin: "10px 0 0", fontSize: 13 }}>
              {fmt(tr("reports.funnel.summary"), {
                won: n(r.funnel.won),
                rate: pct(r.funnel.win_rate_pct),
                lost: n(r.funnel.lost),
                cancelled: n(r.funnel.cancelled),
              })}
            </p>
            <p style={{ margin: "4px 0 0", fontSize: 12.5, color: K.muted }}>
              {r.cycle.median_days === null
                ? tr("reports.cycle.none")
                : fmt(tr("reports.cycle"), { days: n(r.cycle.median_days), n: n(r.cycle.trips) })}
            </p>
          </Panel>

          <Panel title={tr("reports.open")} testId="reports-open">
            <ReportTable
              tableId="open"
              head={[tr("reports.col.stage"), tr("reports.col.trips"), tr("reports.col.stalled"), tr("reports.col.age")]}
              rows={r.open_trips.map((s) => ({
                key: s.stage,
                href: "/pipeline",
                cells: [tr(`stage.${s.stage}`), n(s.count), n(s.stalled), n(s.median_age_days)],
              }))}
              empty={tr("reports.none")}
            />
            <p style={{ margin: "8px 0 0", fontSize: 12.5, color: K.muted }}>{tr("reports.open.help")}</p>
          </Panel>

          <Panel title={tr("reports.outcomes")} testId="reports-outcomes">
            <h3 style={{ margin: "0 0 6px", fontSize: 12.5, color: K.muted, fontWeight: 600 }}>{tr("reports.outcomes.lost")}</h3>
            <BarList
              rows={r.outcomes.lost.map((o) => ({
                key: o.reason ?? "none",
                label: codeText(lang, "lost", o.reason, "reports.notSet"),
                value: o.count,
                text: n(o.count),
              }))}
              empty={tr("reports.outcomes.noneLost")}
            />
            <h3 style={{ margin: "12px 0 6px", fontSize: 12.5, color: K.muted, fontWeight: 600 }}>
              {tr("reports.outcomes.cancelled")}
            </h3>
            <BarList
              rows={r.outcomes.cancelled.map((o) => ({
                key: o.reason ?? "none",
                label: codeText(lang, "cancel", o.reason, "reports.notSet"),
                value: o.count,
                text: n(o.count),
              }))}
              empty={tr("reports.outcomes.noneCancelled")}
            />
          </Panel>

          <Panel title={tr("reports.sources")} testId="reports-sources">
            <ReportTable
              tableId="sources"
              head={[tr("reports.col.source"), tr("reports.col.trips"), tr("reports.col.won"), tr("reports.col.winRate")]}
              rows={r.lead_sources.map((s) => ({
                key: s.source ?? "none",
                cells: [codeText(lang, "reports.source", s.source, "reports.notSet"), n(s.trips), n(s.won), pct(s.win_rate_pct)],
              }))}
              empty={tr("reports.none")}
            />
          </Panel>

          <Panel title={tr("reports.picks")} testId="reports-picks">
            {r.picks.picks === 0 ? (
              <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{tr("reports.picks.none")}</p>
            ) : (
              <ul style={{ margin: 0, paddingLeft: 18, display: "grid", gap: 6, fontSize: 13 }}>
                <li>{fmt(tr("reports.picks.total"), { n: n(r.picks.picks) })}</li>
                <li>{fmt(tr("reports.picks.upgraded"), { pct: pct(r.picks.upgrade_rate_pct) })}</li>
                <li>{fmt(tr("reports.picks.kept"), { pct: pct(r.picks.kept_default_pct) })}</li>
                {r.picks.most_changed_type && (
                  <li>
                    {fmt(tr("reports.picks.mostChanged"), {
                      type: codeText(lang, "builder.blockType", r.picks.most_changed_type, "reports.notSet"),
                    })}
                  </li>
                )}
              </ul>
            )}
          </Panel>

          {r.money && (
            <Panel title={tr("reports.money")} testId="reports-money">
              <p style={{ margin: "0 0 10px", fontSize: 13 }}>
                {fmt(tr("reports.money.summary"), {
                  sell: money(r.money.sell_base),
                  margin: money(r.money.margin_base),
                  pct: pct(r.money.margin_pct),
                  lines: n(r.money.lines),
                })}
              </p>
              <ReportTable
                tableId="money"
                head={[tr("reports.col.month"), tr("reports.col.sell"), tr("reports.col.cost"), tr("reports.col.margin"), tr("reports.col.marginPct")]}
                rows={r.money.by_month.map((m) => ({
                  key: m.month,
                  cells: [
                    new Date(`${m.month}T00:00:00Z`).toLocaleDateString(locale, { month: "short", year: "numeric", timeZone: "UTC" }),
                    money(m.sell_base),
                    money(m.cost_base),
                    money(m.margin_base),
                    pct(m.margin_pct),
                  ],
                }))}
                empty={tr("reports.none")}
              />
              {r.money.lines_missing_amounts > 0 && (
                <p style={{ margin: "8px 0 0", fontSize: 12.5, color: K.muted }}>
                  {fmt(tr("reports.money.missing"), { n: n(r.money.lines_missing_amounts) })}
                </p>
              )}
              {r.money.by_party_type.length > 0 && (
                <>
                  <h3 style={{ margin: "12px 0 6px", fontSize: 12.5, color: K.muted, fontWeight: 600 }}>
                    {tr("reports.money.byParty")}
                  </h3>
                  <BarList
                    rows={r.money.by_party_type.map((p) => ({
                      key: p.party_type ?? "none",
                      label: codeText(lang, "party", p.party_type, "reports.notSet"),
                      value: Number(p.sell_base),
                      text: `${money(p.sell_base)} · ${pct(p.margin_pct)}`,
                    }))}
                    empty={tr("reports.none")}
                  />
                </>
              )}
            </Panel>
          )}

          <Panel title={tr("reports.products")} testId="reports-products">
            <ReportTable
              tableId="products"
              head={
                r.money
                  ? [tr("reports.col.product"), tr("reports.col.lines"), tr("reports.col.trips"), tr("reports.col.sell")]
                  : [tr("reports.col.product"), tr("reports.col.lines"), tr("reports.col.trips")]
              }
              rows={r.top_products.map((p) => ({
                key: `${p.supplier_kind}:${p.title}`,
                cells: [
                  `${p.title} (${tr(`reports.supplier.${p.supplier_kind}`)})`,
                  n(p.lines),
                  n(p.trips),
                  ...(r.money ? [money(p.sell_base)] : []),
                ],
              }))}
              empty={tr("reports.products.none")}
            />
          </Panel>

          <Panel title={tr("reports.payments")} testId="reports-payments">
            <ReportTable
              tableId="payments"
              head={[tr("reports.col.what"), tr("reports.col.count"), tr("reports.col.outstanding"), tr("reports.col.overdue")]}
              rows={[
                ...r.payments.open_invoices.map((i) => ({
                  key: `inv-${i.currency}`,
                  href: "/payments",
                  cells: [
                    fmt(tr("reports.payments.invoices"), { currency: i.currency }),
                    n(i.count),
                    money(i.outstanding, i.currency),
                    `${money(i.overdue, i.currency)} (${n(i.overdue_count)})`,
                  ],
                })),
                ...r.payments.schedule_due.map((s) => ({
                  key: `sch-${s.currency}-${s.kind}`,
                  href: "/payments",
                  cells: [
                    fmt(tr(`reports.payments.schedule.${s.kind === "deposit" ? "deposit" : "balance"}`), { currency: s.currency }),
                    n(s.count),
                    money(s.total, s.currency),
                    `${money(s.overdue, s.currency)} (${n(s.overdue_count)})`,
                  ],
                })),
              ]}
              empty={tr("reports.payments.none")}
            />
            {r.payments.collected.length > 0 && (
              <p style={{ margin: "8px 0 0", fontSize: 12.5 }}>
                {tr("reports.payments.collected")}:{" "}
                {r.payments.collected.map((c) => money(c.net, c.currency)).join(" · ")}
              </p>
            )}
          </Panel>

          <Panel title={tr("reports.team")} testId="reports-team">
            <ReportTable
              tableId="team"
              head={[
                tr("reports.col.member"),
                tr("reports.col.open"),
                tr("reports.col.new"),
                tr("reports.col.won"),
                tr("reports.col.overdueTasks"),
                tr("reports.col.reply"),
              ]}
              rows={r.team.members.map((m) => ({
                key: m.member_id,
                cells: [
                  m.is_me ? tr("trip.me") : label(m.member_id),
                  n(m.open_trips),
                  n(m.new_trips),
                  n(m.won),
                  n(m.overdue_tasks),
                  m.first_reply_median_min === null ? "—" : fmt(tr("reports.minutes"), { n: n(m.first_reply_median_min) }),
                ],
              }))}
              empty={tr("reports.none")}
            />
            <p style={{ margin: "8px 0 0", fontSize: 12.5, color: K.muted }}>
              {fmt(tr("reports.team.summary"), {
                unassigned: n(r.team.unassigned_open_trips),
                unanswered: n(r.team.unanswered_24h),
              })}
            </p>
          </Panel>
        </div>
      )}

      <Panel title={tr("reports.success")} testId="reports-success">
        <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 8 }}>
          {r.success.map((m) => (
            <li
              key={m.key}
              data-testid={`reports-success-${m.key}`}
              style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "baseline", fontSize: 13, borderTop: `1px solid ${K.line}`, paddingTop: 6 }}
            >
              <span style={{ flex: "1 1 240px", minWidth: 0 }}>{tr(`reports.success.${m.key}`)}</span>
              <span style={{ fontVariantNumeric: "tabular-nums", color: K.ink }}>{successValue(m, lang, locale)}</span>
              {successTarget(m, lang, locale) && (
                <span style={{ color: K.muted, fontSize: 12 }}>{successTarget(m, lang, locale)}</span>
              )}
              <Badge tone={STATUS_TONE[m.status]}>{tr(`reports.status.${m.status}`)}</Badge>
            </li>
          ))}
        </ul>
      </Panel>

      <p data-testid="reports-insights" style={{ margin: 0, fontSize: 12.5, color: K.muted }}>
        {tr(r.insights_included ? "reports.insights.included" : "reports.insights.optedOut")}
      </p>
    </div>
  );
}
