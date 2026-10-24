"use client";
// web/components/settings/AccessLogView.tsx — A-18 Settings > Data & privacy > Access log (AA-940, card CRM-12;
// crm-design-final WF-8: "Access log: [user] [AI] [AA ops] [AA support] [AA emergency] [Download]"). The filter chips
// and the date range live in the URL (the server page reads them); "Show older entries" pages with the API cursor;
// Download fetches the same filtered rows as CSV / JSON lines through the BFF and saves them as a file.
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Badge, Button, EmptyState, K } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import {
  ACTIONS,
  ACTOR_GROUPS,
  type AuditEventView,
  type AuditPage,
  type ExportFormat,
  type LogFilter,
  actorText,
  codeLabel,
  eventText,
  eventsPath,
  exportFilename,
  exportPath,
  filterQuery,
  objectHref,
  whenText,
} from "@/lib/accessLog";
import type { DateLocale } from "@/lib/dates";
import { type Lang, t } from "@/lib/i18n";

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

function chip(active: boolean): React.CSSProperties {
  return {
    font: "inherit",
    fontSize: 12.5,
    padding: "5px 11px",
    borderRadius: 999,
    border: `1px solid ${active ? K.accent : K.line2}`,
    background: active ? K.accentTint : K.card,
    color: active ? K.accentDeep : K.body,
    cursor: "pointer",
  };
}

export function AccessLogView({
  initial,
  filter,
  lang,
  locale,
}: {
  initial: AuditPage;
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

  function go(next: LogFilter) {
    const q = filterQuery(next).toString();
    router.push(q ? `/settings/access-log?${q}` : "/settings/access-log");
  }

  async function more() {
    if (!cursor) return;
    setBusy("more");
    setError(null);
    try {
      const res = await fetch(`/bff/${eventsPath(filter, cursor)}`, { headers: { Accept: "application/json" }, credentials: "same-origin" });
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
      const res = await fetch(`/bff/${exportPath(filter, format)}`, { credentials: "same-origin" });
      if (!res.ok) throw new Error(String(res.status));
      const url = URL.createObjectURL(await res.blob());
      const a = document.createElement("a");
      a.href = url;
      a.download = exportFilename(format);
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch {
      setError(t(lang, "log.downloadError"));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div style={{ display: "grid", gap: 14 }}>
      <Panel testId="access-log-filters">
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginBottom: 12 }} role="group" aria-label={t(lang, "log.who")}>
          <button type="button" style={chip(!filter.actor)} onClick={() => go({ ...filter, actor: undefined })} data-testid="log-actor-all">
            {t(lang, "log.allActors")}
          </button>
          {ACTOR_GROUPS.map((g) => (
            <button
              key={g}
              type="button"
              style={chip(filter.actor === g)}
              aria-pressed={filter.actor === g}
              onClick={() => go({ ...filter, actor: g })}
              data-testid={`log-actor-${g}`}
            >
              {codeLabel(lang, "log.actor", g)}
            </button>
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
            {events.map((e) => {
              const href = objectHref(e);
              return (
                <li
                  key={e.event_id}
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
                    {whenText(e.at, locale)}
                  </time>
                  <div style={{ minWidth: 0, overflowWrap: "anywhere", color: K.ink }}>
                    <strong style={{ fontWeight: 600 }}>{actorText(e, lang)}</strong>{" "}
                    {href ? <a href={href}>{eventText(e, lang)}</a> : eventText(e, lang)}{" "}
                    {e.outcome === "denied" && <Badge tone="danger">{t(lang, "log.outcome.denied")}</Badge>}
                  </div>
                </li>
              );
            })}
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
