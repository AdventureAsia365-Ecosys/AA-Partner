// web/app/(crm)/marketing/page.tsx — A-15 Marketing (owner, agent). AA-960 (card CRM-29b, R36): "Ready to travel
// again" from GET /crm/v1/marketing/ready-to-travel: past clients home for long enough with no open trip, each row with
// its reasons (card E "the list explains its reasons"), their last trip, and the state of their marketing consent
// (sending to them is marketing: it needs a yes, and the send gate checks it again). The list is the agency's own; the
// page says Adventure Asia never reads it. Campaigns (AA-968) arrive later: their place says so. Filters and pages are
// links (a server render); the names are opened by the API and the view is audited there.
import Link from "next/link";
import { Badge, EmptyState, ErrorState, K } from "@/app/_kit";
import { Panel, PageTitle } from "@/components/PageTitle";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { formatDate } from "@/lib/dates";
import { fmt, t } from "@/lib/i18n";
import { serverContext } from "@/lib/serverContext";
import { consentKey, reasonText, seesMarketing, type ReadyList } from "@/lib/tags";

export const dynamic = "force-dynamic";

const CONSENT_TONE = { yes: "success", no: "danger", unknown: "neutral" } as const;

function href(owner: string, page: number): string {
  const q = new URLSearchParams();
  if (owner === "me") q.set("owner", "me");
  if (page > 1) q.set("page", String(page));
  const s = q.toString();
  return s ? `/marketing?${s}` : "/marketing";
}

export default async function MarketingPage({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const { lang, locale } = ctx;
  if (!seesMarketing(ctx.role)) {
    return (
      <div data-testid="crm-section-forbidden">
        <PageTitle title={t(lang, "nav.marketing")} />
        <EmptyState title={t(lang, "section.forbidden.title")} description={t(lang, "section.forbidden.body")} />
      </div>
    );
  }
  const { session } = await serverContext();
  if (!session) return null;
  const sp = await searchParams;
  const owner = sp.owner === "me" ? "me" : "anyone";
  const page = Math.min(Math.max(Number.parseInt(String(sp.page ?? "1"), 10) || 1, 1), 200);
  const query = new URLSearchParams({ owner, page: String(page) });
  const res = await crmCall<ReadyList>(ctx.cfg, session, "GET", "marketing/ready-to-travel", { query });
  const chip = (active: boolean): React.CSSProperties => ({
    fontSize: 12.5,
    padding: "4px 10px",
    borderRadius: 999,
    border: `1px solid ${active ? K.accent : K.line2}`,
    background: active ? K.accentTint : K.card,
    color: active ? K.accentDeep : K.body,
    textDecoration: "none",
  });
  return (
    <div data-testid="crm-marketing" style={{ display: "grid", gap: 14, minWidth: 0, maxWidth: 1080 }}>
      <PageTitle title={t(lang, "nav.marketing")} subtitle={t(lang, "tag.ready.ownersOnly")} />
      <Panel title={t(lang, "tag.ready.title")} testId="ready-to-travel">
        {!res.ok ? (
          <ErrorState message={t(lang, "error.api")} />
        ) : (
          <div style={{ display: "grid", gap: 12 }}>
            <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{fmt(t(lang, "tag.ready.subtitle"), { months: res.data.ready_after_months })}</p>
            <nav style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }} aria-label={t(lang, "tag.ready.title")}>
              <a href={href("anyone", 1)} style={chip(owner === "anyone")}>
                {t(lang, "tag.ready.all")}
              </a>
              <a href={href("me", 1)} style={chip(owner === "me")}>
                {t(lang, "tag.ready.mine")}
              </a>
              <span style={{ fontSize: 12.5, color: K.muted }}>{fmt(t(lang, "tag.ready.count"), { n: res.data.total })}</span>
            </nav>
            {res.data.items.length === 0 ? (
              <EmptyState title={t(lang, "tag.ready.title")} description={t(lang, "tag.ready.empty")} />
            ) : (
              <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 10 }}>
                {res.data.items.map((item) => (
                  <li key={item.traveler_id} data-testid="ready-row" data-consent={item.marketing_consent}
                    style={{ display: "grid", gap: 4, padding: "10px 0", borderTop: `1px solid ${K.line}`, minWidth: 0 }}>
                    <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
                      <strong style={{ fontSize: 14, overflowWrap: "anywhere" }}>{item.display_name}</strong>
                      <Badge tone={CONSENT_TONE[item.marketing_consent]} dot={false}>
                        {t(lang, "tag.ready.consent")}: {t(lang, consentKey(item.marketing_consent))}
                      </Badge>
                      <Link href={`/clients/${item.traveler_id}`} style={{ fontSize: 13, marginLeft: "auto" }}>
                        {t(lang, "tag.ready.open")}
                      </Link>
                    </div>
                    <ul aria-label={t(lang, "tag.ready.why")} style={{ margin: 0, paddingLeft: 18, fontSize: 13, color: K.body }}>
                      {item.reasons.map((r, i) => (
                        <li key={i} data-reason={r.code}>
                          {reasonText(lang, r)}
                        </li>
                      ))}
                    </ul>
                    {item.last_trip && (
                      <span style={{ fontSize: 12.5, color: K.muted, overflowWrap: "anywhere" }}>
                        {t(lang, "tag.ready.lastTrip")}: {item.last_trip.title}
                        {item.last_trip.end_date ? ` · ${formatDate(item.last_trip.end_date, locale)}` : ""}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            )}
            {(page > 1 || res.data.has_more) && (
              <nav style={{ display: "flex", gap: 12 }}>
                {page > 1 && <Link href={href(owner, page - 1)}>{t(lang, "tag.ready.prev")}</Link>}
                {res.data.has_more && <Link href={href(owner, page + 1)}>{t(lang, "tag.ready.more")}</Link>}
              </nav>
            )}
          </div>
        )}
      </Panel>
      <Panel title={t(lang, "tag.marketing.campaigns")} testId="marketing-campaigns">
        <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{t(lang, "tag.marketing.campaignsSoon")}</p>
      </Panel>
    </div>
  );
}
