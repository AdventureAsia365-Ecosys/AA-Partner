// web/app/(crm)/calendar/page.tsx — A-09 Calendar (AA-961, card CRM-30; design v3 R15): one month of departures,
// returns, payment and invoice due dates, signing deadlines, tasks and calls, as an agenda by day; my trips or the
// whole agency; a filter per kind (?kinds=). Under it, my private ICS link (FeedCard: create, copy once, stop; the
// revoked banner) and, for an owner, the team's active links. Every role (the API checks).
import { Badge, EmptyState, ErrorState, K } from "@/app/_kit";
import { FeedCard, TeamFeeds } from "@/components/calendar/FeedCard";
import { Panel, PageTitle } from "@/components/PageTitle";
import {
  KINDS,
  KIND_TONE,
  byDay,
  filterKinds,
  kindKey,
  monthWindow,
  shiftMonth,
  type CalendarEvent,
  type CalendarView,
  type EventKind,
  type Feed,
  type Scope,
} from "@/lib/calendar";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { formatDate, type DateLocale } from "@/lib/dates";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import { formatMoney } from "@/lib/ledger";
import { memberLabel, type Member } from "@/lib/pipeline";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

function detail(e: CalendarEvent, lang: Lang, locale: DateLocale): string {
  const tr = (key: string) => t(lang, key as MessageKey);
  const parts: string[] = [];
  if (e.starts_at) {
    parts.push(new Intl.DateTimeFormat(locale, { hour: "2-digit", minute: "2-digit" }).format(new Date(e.starts_at)));
  }
  if (e.kind === "payment_due" && e.payment_kind) parts.push(tr(`calendar.payment.${e.payment_kind}`));
  if (e.invoice_number) parts.push(e.invoice_number);
  if (e.amount) parts.push(formatMoney(e.amount.amount, e.amount.currency, locale));
  if (e.pending_signatures) parts.push(fmt(tr("calendar.signing.pending"), { n: e.pending_signatures }));
  if (e.task_kind) parts.push(e.task_kind.replace(/_/g, " "));
  if (e.priority === "urgent") parts.push(tr("calendar.task.urgent"));
  if (e.call_status) parts.push(tr(`calendar.call.${e.call_status}`));
  return parts.join(" · ");
}

export default async function CalendarPage({
  searchParams,
}: {
  searchParams: Promise<{ month?: string; scope?: string; kinds?: string }>;
}) {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const { session } = await serverContext();
  if (!session) return null;
  const params = await searchParams;
  const tr = (key: string) => t(ctx.lang, key as MessageKey);
  const scope: Scope = params.scope === "agency" ? "agency" : "mine";
  const thisMonth = new Date().toISOString().slice(0, 7);
  const month = params.month && monthWindow(params.month) ? params.month : thisMonth;
  const range = monthWindow(month) ?? { from: `${thisMonth}-01`, to: `${thisMonth}-28` };
  const kinds = new Set(
    (params.kinds ?? "").split(",").filter((k): k is EventKind => (KINDS as readonly string[]).includes(k)),
  );
  const [view, feed, team, members] = await Promise.all([
    crmCall<CalendarView>(ctx.cfg, session, "GET", "calendar", {
      query: new URLSearchParams({ from: range.from, to: range.to, scope }),
    }),
    crmCall<{ feed: Feed | null }>(ctx.cfg, session, "GET", "calendar/feed"),
    ctx.role === "owner" ? crmCall<{ feeds: Feed[] }>(ctx.cfg, session, "GET", "calendar/feeds") : Promise.resolve(null),
    ctx.role === "owner" ? crmCall<{ members: Member[] }>(ctx.cfg, session, "GET", "members") : Promise.resolve(null),
  ]);
  if (!view.ok) return <ErrorState message={tr("error.api")} />;
  const days = byDay(filterKinds(view.data.events, kinds));
  const link = (over: { month?: string; scope?: Scope; kinds?: string }) => {
    const q = new URLSearchParams({ month: over.month ?? month, scope: over.scope ?? scope });
    const k = over.kinds ?? [...kinds].join(",");
    if (k) q.set("kinds", k);
    return `/calendar?${q.toString()}`;
  };
  const monthLabel = new Intl.DateTimeFormat(ctx.locale, { month: "long", year: "numeric" }).format(
    new Date(`${range.from}T00:00:00Z`),
  );
  const names = Object.fromEntries(
    (members && members.ok ? members.data.members : []).map((m) => [m.member_id, memberLabel(ctx.lang, m)]),
  );
  return (
    <div data-testid="crm-calendar" style={{ display: "grid", gap: 14, maxWidth: 1100 }}>
      <PageTitle title={tr("calendar.title")} subtitle={tr("calendar.subtitle")} />
      <nav style={{ display: "flex", flexWrap: "wrap", gap: 12, fontSize: 13, alignItems: "center" }}>
        <a href={link({ month: shiftMonth(month, -1) })}>{tr("calendar.prev")}</a>
        <strong data-testid="calendar-month">{monthLabel}</strong>
        <a href={link({ month: shiftMonth(month, 1) })}>{tr("calendar.next")}</a>
        {month !== thisMonth && <a href={link({ month: thisMonth })}>{tr("calendar.thisMonth")}</a>}
        <span style={{ color: K.line2 }}>|</span>
        {(["mine", "agency"] as const).map((s) => (
          <a key={s} href={link({ scope: s })} aria-current={s === scope ? "page" : undefined}>
            {tr(`calendar.scope.${s}`)}
          </a>
        ))}
      </nav>
      <nav style={{ display: "flex", flexWrap: "wrap", gap: 10, fontSize: 12.5 }} aria-label={tr("calendar.filter")}>
        {KINDS.map((k) => {
          const on = kinds.has(k);
          const next = new Set(kinds);
          if (on) next.delete(k);
          else next.add(k);
          return (
            <a key={k} href={link({ kinds: [...next].join(",") })} aria-current={on ? "true" : undefined}>
              {tr(kindKey(k))}
            </a>
          );
        })}
      </nav>
      {days.length === 0 ? (
        <EmptyState title={tr("calendar.empty.title")} description={tr("calendar.empty.body")} />
      ) : (
        <Panel testId="calendar-agenda">
          <ol style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 12 }}>
            {days.map((d) => (
              <li key={d.date} data-testid={`calendar-day-${d.date}`}>
                <h3
                  style={{
                    margin: "0 0 6px",
                    fontSize: 13,
                    fontWeight: 600,
                    color: d.date === view.data.today ? K.accentDeep : K.ink,
                  }}
                >
                  {formatDate(d.date, ctx.locale)}
                </h3>
                <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 6 }}>
                  {d.events.map((e) => (
                    <li
                      key={e.id}
                      data-testid={`calendar-event-${e.kind}`}
                      style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center", fontSize: 13, minWidth: 0 }}
                    >
                      <Badge tone={KIND_TONE[e.kind]}>{tr(kindKey(e.kind))}</Badge>
                      {e.trip_id ? (
                        <a href={`/pipeline/${e.trip_id}`} style={{ overflowWrap: "anywhere" }}>
                          {e.title ?? e.trip_id.slice(0, 8)}
                        </a>
                      ) : (
                        <span style={{ color: K.muted }}>{tr("calendar.noTrip")}</span>
                      )}
                      <span style={{ color: K.muted }}>{detail(e, ctx.lang, ctx.locale)}</span>
                    </li>
                  ))}
                </ul>
              </li>
            ))}
          </ol>
          {view.data.truncated && <p style={{ margin: "10px 0 0", fontSize: 12.5, color: K.muted }}>{tr("calendar.truncated")}</p>}
        </Panel>
      )}
      <Panel title={tr("calendar.feed.title")} testId="calendar-feed-panel">
        <FeedCard initial={feed.ok ? feed.data.feed : null} lang={ctx.lang} locale={ctx.locale} />
      </Panel>
      {team && team.ok && (
        <Panel title={tr("calendar.feed.team")} testId="calendar-team-panel">
          <TeamFeeds initial={team.data.feeds} memberNames={names} lang={ctx.lang} locale={ctx.locale} />
        </Panel>
      )}
    </div>
  );
}
