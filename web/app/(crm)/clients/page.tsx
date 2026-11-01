// web/app/(crm)/clients/page.tsx — A-06 Clients (AA-937, card CRM-09; design v3 §2.2, §4.4). The directory shows
// display names only (no e-mail, no phone, no partial digits) and the API writes one list_view audit event per load
// with the ids shown. Search is exact (blind indexes, R35) and never in a URL: the form POSTs the term to
// /clients/search (sealed short-lived cookie, lib/clientSearch.ts) and the page searches with POST
// /crm/v1/travelers/search. Filters and pages are links, so the list is a server render; adding a client is the one
// client-side form.
import { cookies } from "next/headers";
import Link from "next/link";
import { EmptyState, ErrorState, K } from "@/app/_kit";
import { NewClientForm } from "@/components/clients/NewClientForm";
import { PageTitle } from "@/components/PageTitle";
import { directoryHref, leadSourceLabel, parseDirectory, SEARCH_MAX, tripsCount, type Directory } from "@/lib/clients";
import { openSearch, searchCookieName } from "@/lib/clientSearch";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { formatDate } from "@/lib/dates";
import { fmt, t } from "@/lib/i18n";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

const chip = (active: boolean): React.CSSProperties => ({
  fontSize: 12.5,
  padding: "4px 10px",
  borderRadius: 999,
  border: `1px solid ${active ? K.accent : K.line2}`,
  background: active ? K.accentTint : K.card,
  color: active ? K.accentDeep : K.body,
  textDecoration: "none",
});

export default async function ClientsPage({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const { session } = await serverContext();
  const f = parseDirectory(await searchParams);
  const q = f.search ? openSearch(ctx.cfg, session, (await cookies()).get(searchCookieName(ctx.cfg))?.value) : "";
  const search = Boolean(q);
  const query = new URLSearchParams();
  if (f.archived) query.set("archived", "true");
  if (f.owner === "me") query.set("owner", "me");
  if (f.page > 1) query.set("page", String(f.page));
  const res = !session
    ? null
    : search
      ? await crmCall<Directory>(ctx.cfg, session, "POST", "travelers/search", { body: { q, archived: f.archived, owner: f.owner, page: f.page } })
      : await crmCall<Directory>(ctx.cfg, session, "GET", "travelers", { query });
  const { lang, locale } = ctx;
  const KIND = { email: "clients.byEmail", phone: "clients.byPhone", name: "clients.byName" } as const;
  const matched = res?.ok ? res.data.filter.search : "none";
  const kindLabel = matched !== "none" ? t(lang, KIND[matched]) : null;
  return (
    <div data-testid="crm-clients" style={{ display: "grid", gap: 14, minWidth: 0, maxWidth: 1080 }}>
      <PageTitle title={t(lang, "clients.title")} subtitle={t(lang, "clients.subtitle")} />
      <form method="post" action="/clients/search" style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
        <input
          name="q"
          defaultValue={q}
          maxLength={SEARCH_MAX}
          aria-label={t(lang, "clients.search")}
          placeholder={t(lang, "clients.search")}
          style={{ font: "inherit", fontSize: 13.5, padding: "7px 10px", borderRadius: 8, border: `1px solid ${K.line2}`, background: K.card, color: K.ink, flex: "1 1 260px", minWidth: 0 }}
        />
        {f.archived && <input type="hidden" name="archived" value="true" />}
        {f.owner === "me" && <input type="hidden" name="owner" value="me" />}
        <button type="submit" style={{ ...chip(true), cursor: "pointer", font: "inherit", fontSize: 13 }}>
          {t(lang, "clients.searchButton")}
        </button>
        {search && (
          <a href={directoryHref({ archived: f.archived, owner: f.owner })} style={chip(false)}>
            {t(lang, "clients.clear")}
          </a>
        )}
      </form>
      <nav style={{ display: "flex", flexWrap: "wrap", gap: 8 }} aria-label={t(lang, "clients.title")}>
        <a href={directoryHref({ search, owner: f.owner })} style={chip(!f.archived)}>
          {t(lang, "clients.active")}
        </a>
        <a href={directoryHref({ search, owner: f.owner, archived: true })} style={chip(f.archived)}>
          {t(lang, "clients.archived")}
        </a>
        <a href={directoryHref({ search, archived: f.archived })} style={chip(f.owner === "anyone")}>
          {t(lang, "clients.everyone")}
        </a>
        <a href={directoryHref({ search, archived: f.archived, owner: "me" })} style={chip(f.owner === "me")}>
          {t(lang, "clients.mine")}
        </a>
        <Link href="/clients/duplicates" style={chip(false)} data-testid="clients-duplicates-link">
          {t(lang, "onb.dup.title")}
        </Link>
      </nav>
      <NewClientForm lang={lang} />
      {!res || !res.ok ? (
        <ErrorState message={t(lang, res?.status === 422 ? "clients.noResults" : "clients.error")} />
      ) : res.data.items.length === 0 ? (
        search ? (
          <EmptyState title={t(lang, "clients.noResults")} />
        ) : (
          <EmptyState title={t(lang, "section.clients.title")} description={t(lang, "section.clients.body")} />
        )
      ) : (
        <>
          {kindLabel && <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{fmt(t(lang, "clients.searchedBy"), { kind: kindLabel })}</p>}
          <ul data-testid="client-list" style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 8 }}>
            {res.data.items.map((c) => (
              <li
                key={c.traveler_id}
                style={{ background: K.card, border: `1px solid ${K.line}`, borderRadius: 10, padding: "10px 14px", display: "flex", flexWrap: "wrap", gap: "4px 16px", alignItems: "baseline", justifyContent: "space-between" }}
              >
                <a href={`/clients/${c.traveler_id}`} style={{ fontSize: 14.5, color: K.ink, fontWeight: 500, overflowWrap: "anywhere", textDecoration: "none" }}>
                  {c.display_name}
                </a>
                <span style={{ fontSize: 12.5, color: K.muted, display: "flex", flexWrap: "wrap", gap: 10 }}>
                  <span>{[c.has_email ? t(lang, "clients.hasEmail") : null, c.has_phone ? t(lang, "clients.hasPhone") : null].filter(Boolean).join(" · ") || t(lang, "clients.noContact")}</span>
                  <span>{tripsCount(lang, c.trips)}</span>
                  {c.lead_source && <span>{leadSourceLabel(lang, c.lead_source)}</span>}
                  <span>{formatDate(c.created_at, locale, { year: true })}</span>
                </span>
              </li>
            ))}
          </ul>
          <nav style={{ display: "flex", gap: 10 }}>
            {f.page > 1 && (
              <a href={directoryHref({ ...f, search, page: f.page - 1 })} style={chip(false)}>
                {t(lang, "clients.prev")}
              </a>
            )}
            {res.data.has_more && (
              <a href={directoryHref({ ...f, search, page: f.page + 1 })} style={chip(false)}>
                {t(lang, "clients.next")}
              </a>
            )}
          </nav>
        </>
      )}
    </div>
  );
}
