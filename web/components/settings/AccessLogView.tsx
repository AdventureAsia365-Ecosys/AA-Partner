"use client";
// web/components/settings/AccessLogView.tsx — A-18 Settings > Data & privacy > Access log (AA-940, card CRM-12;
// crm-design-final WF-8: "Access log: [user] [AI] [AA ops] [AA support] [AA emergency] [Download]"). The filter chips
// and the date range live in the URL (the server page reads them); "Show older entries" pages with the API cursor;
// Download fetches the same filtered rows as CSV / JSON lines through the BFF and saves them as a file.
import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";
import { Badge, Button, EmptyState, K, ChipButton } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { bffDownload, bffFetch } from "@/lib/bffClient";
import {
  ACTIONS,
  ACTOR_GROUPS,
  type AuditEventView,
  type AuditPage,
  type ExportFormat,
  type LogFilter,
  type MemberDirectory,
  actorText,
  codeLabel,
  eventText,
  eventsPath,
  exportFilename,
  exportPath,
  filterQuery,
  memberDirectory,
  objectHref,
  whenText,
} from "@/lib/accessLog";
import type { DateLocale } from "@/lib/dates";
import { type Lang, t } from "@/lib/i18n";
import type { Member } from "@/lib/pipeline";

const field: React.CSSProperties = {
  font: "inherit",
  fontSize: 13,
  padding: "6px 8px",
  borderRadius: 8,
  border: `1px solid ${K.line2}`,
  background: K.card,
  color: K.ink,
  minWidth: 0,
};

export function AccessLogView({
  initial,
  members,
  filter,
  lang,
  locale,
}: {
  initial: AuditPage;
  /** AA-1129: the agency's members (the members API), so a line names its actor by the member rule; null: role only. */
  members?: Member[] | null;
  filter: LogFilter;
  lang: Lang;
  locale: DateLocale;
}) {
  const router = useRouter();
  const [events, setEvents] = useState<AuditEventView[]>(initial.events);
  const [cursor, setCursor] = useState<string | null>(initial.next_cursor);
  const [draft, setDraft] = useState<LogFilter>(filter);
  const [busy, setBusy] = useState<"more" | ExportFormat | null>(null);
  const [error, setError] = useState<string | null>(null);
  // AA-1129 (S283 row 10): one zone for the server render and the browser (the agency's), so hydration matches;
  // whenText falls back to UTC for an older API without the field.
  const zone = initial.timezone;
  const directory = useMemo(() => memberDirectory(members), [members]);

  function go(next: LogFilter) {
    const q = filterQuery(next).toString();
    router.push(q ? `/settings/access-log?${q}` : "/settings/access-log");
  }

  async function more() {
    if (!cursor) return;
    setBusy("more");
    setError(null);
    try {
      const res = await bffFetch(eventsPath(filter, cursor), { headers: { Accept: "application/json" } });
      if (res.status === 401) return; // AA-1014: the renewal failed too; the page is reloading through /auth/refresh
      if (!res.ok) throw new Error(String(res.status));
      const page = (await res.json()) as AuditPage;
      setEvents((prev) => [...prev, ...page.events]);
      setCursor(page.next_cursor);
    } catch {
      setError(t(lang, "log.error"));
    } finally {
      setBusy(null);
    }
  }

  async function download(format: ExportFormat) {
    setBusy(format);
    setError(null);
    try {
      const res = await bffDownload(exportPath(filter, format), { filename: exportFilename(format) });
      if (!res.ok && !res.redirecting) setError(t(lang, "log.downloadError"));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div style={{ display: "grid", gap: 14 }}>
      <Panel testId="access-log-filters">
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginBottom: 12 }} role="group" aria-label={t(lang, "log.who")}>
          <ChipButton selected={!filter.actor} onClick={() => go({ ...filter, actor: undefined })} testId="log-actor-all">
            {t(lang, "log.allActors")}
          </ChipButton>
          {ACTOR_GROUPS.map((g) => (
            <ChipButton
              key={g}
              selected={filter.actor === g}
              onClick={() => go({ ...filter, actor: g })}
              testId={`log-actor-${g}`}
            >
              {codeLabel(lang, "log.actor", g)}
            </ChipButton>
          ))}
        </div>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            go({ ...draft, actor: filter.actor });
          }}
          style={{ display: "flex", flexWrap: "wrap", gap: 10, alignItems: "flex-end" }}
        >
          <label style={{ display: "grid", gap: 4, fontSize: 12, color: K.muted }}>
            {t(lang, "log.from")}
            <input type="date" lang={locale} style={field} value={draft.from ?? ""} onChange={(e) => setDraft({ ...draft, from: e.target.value || undefined })} />
          </label>
          <label style={{ display: "grid", gap: 4, fontSize: 12, color: K.muted }}>
            {t(lang, "log.to")}
            <input type="date" lang={locale} style={field} value={draft.to ?? ""} onChange={(e) => setDraft({ ...draft, to: e.target.value || undefined })} />
          </label>
          <label style={{ display: "grid", gap: 4, fontSize: 12, color: K.muted }}>
            {t(lang, "log.action")}
            <select style={field} value={draft.action ?? ""} onChange={(e) => setDraft({ ...draft, action: e.target.value || undefined })}>
              <option value="">{t(lang, "log.anyAction")}</option>
              {ACTIONS.map((a) => (
                <option key={a} value={a}>
                  {codeLabel(lang, "log.act", a)}
                </option>
              ))}
            </select>
          </label>
          <label style={{ display: "grid", gap: 4, fontSize: 12, color: K.muted }}>
            {t(lang, "log.outcome")}
            <select
              style={field}
              value={draft.outcome ?? ""}
              onChange={(e) => setDraft({ ...draft, outcome: (e.target.value || undefined) as LogFilter["outcome"] })}
            >
              <option value="">{t(lang, "log.anyOutcome")}</option>
              <option value="allowed">{t(lang, "log.outcome.allowed")}</option>
              <option value="denied">{t(lang, "log.outcome.denied")}</option>
            </select>
          </label>
          <Button type="submit" size="sm">
            {t(lang, "log.apply")}
          </Button>
          <Button type="button" size="sm" variant="ghost" onClick={() => go({})}>
            {t(lang, "log.reset")}
          </Button>
          <span style={{ flex: 1 }} />
          <Button type="button" size="sm" variant="secondary" disabled={busy !== null} onClick={() => download("csv")} ariaLabel={t(lang, "log.download")}>
            {busy === "csv" ? t(lang, "log.downloading") : t(lang, "log.download")}
          </Button>
          <Button type="button" size="sm" variant="ghost" disabled={busy !== null} onClick={() => download("jsonl")}>
            {busy === "jsonl" ? t(lang, "log.downloading") : t(lang, "log.downloadJson")}
          </Button>
        </form>
      </Panel>

      {error && (
        <p role="alert" style={{ margin: 0, color: K.danger, fontSize: 13 }}>
          {error}
        </p>
      )}

      <Panel testId="access-log-list">
        {events.length === 0 ? (
          <EmptyState title={t(lang, "log.none")} />
        ) : (
          <ol style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 0 }}>
            {events.map((e) => (
              <AccessLogRow key={e.event_id} e={e} lang={lang} locale={locale} zone={zone} directory={directory} />
            ))}
          </ol>
        )}
        {cursor && (
          <div style={{ marginTop: 12 }}>
            <Button type="button" size="sm" variant="secondary" disabled={busy !== null} onClick={more}>
              {t(lang, "log.more")}
            </Button>
          </div>
        )}
        <p style={{ margin: "12px 0 0", fontSize: 12, color: K.muted }}>{t(lang, "log.viewedNote")}</p>
      </Panel>
    </div>
  );
}

/** One line of the log: when (the agency's zone), who (the member rule), what (a link to the object when it has a page). */
function AccessLogRow({
  e,
  lang,
  locale,
  zone,
  directory,
}: {
  e: AuditEventView;
  lang: Lang;
  locale: DateLocale;
  zone: string | undefined;
  directory: MemberDirectory | null;
}) {
  const href = objectHref(e);
  const text = eventText(e, lang, directory);
  return (
    <li
      data-testid="access-log-row"
      style={{
        display: "grid",
        gridTemplateColumns: "minmax(96px, 120px) minmax(0, 1fr)",
        gap: "2px 14px",
        padding: "9px 0",
        borderTop: `1px solid ${K.line}`,
        fontSize: 13,
      }}
    >
      <time dateTime={e.at} style={{ color: K.muted, whiteSpace: "nowrap" }}>
        {whenText(e.at, locale, zone)}
      </time>
      <div style={{ minWidth: 0, overflowWrap: "anywhere", color: K.ink }}>
        <strong style={{ fontWeight: 600 }}>{actorText(e, lang, directory)}</strong> {href ? <a href={href}>{text}</a> : text}{" "}
        {e.outcome === "denied" && <Badge tone="danger">{t(lang, "log.outcome.denied")}</Badge>}
      </div>
    </li>
  );
}
