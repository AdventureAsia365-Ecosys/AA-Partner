"use client";
// web/components/calendar/FeedCard.tsx — "Subscribe in your calendar" (AA-961, card CRM-30; design v3 R15 ICS
// fallback): the member's own ICS link. Create (scope: my trips / the whole agency) shows the URL once, with Copy;
// the API keeps only its hash, so a lost link is replaced by a new one (which stops the old). Stop asks first. Card E:
// a revoked link (by the member or an owner) shows a banner: subscribed calendars stopped updating, trips and
// bookings are unchanged, a new link resumes. No Google / Microsoft calendar is written (bring-your-own sync later).
import { useState } from "react";
import { Button, ConfirmModal, Field, K, Select, useHydrated } from "@/app/_kit";
import { bffSend } from "@/lib/bffClient";
import { feedState, type Feed, type Scope } from "@/lib/calendar";
import { formatDate, type DateLocale } from "@/lib/dates";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";

export function FeedCard({ initial, lang, locale }: { initial: Feed | null; lang: Lang; locale: DateLocale }) {
  const tr = (key: string) => t(lang, key as MessageKey);
  const hydrated = useHydrated();
  const [feed, setFeed] = useState<Feed | null>(initial);
  const [url, setUrl] = useState<string | null>(null);
  const [scope, setScope] = useState<Scope>(initial?.scope ?? "mine");
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState(false);
  const state = feedState(feed);

  async function create() {
    setBusy(true);
    setError(false);
    const res = await bffSend<{ feed: Feed; url: string }>("POST", "calendar/feed", { scope });
    setBusy(false);
    if (!res.ok) return setError(true);
    setFeed(res.data.feed);
    setUrl(res.data.url);
    setCopied(false);
  }

  async function revoke() {
    if (!feed) return;
    setBusy(true);
    setError(false);
    const res = await bffSend<{ feed: Feed }>("POST", `calendar/feeds/${feed.calendar_feed_id}/revoke`);
    setBusy(false);
    setConfirming(false);
    if (!res.ok) return setError(true);
    setFeed(res.data.feed);
    setUrl(null);
  }

  async function copy() {
    if (!url) return;
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }

  return (
    <div data-testid="calendar-feed" style={{ display: "grid", gap: 10, minWidth: 0 }}>
      <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{tr("calendar.feed.body")}</p>
      {(state === "revoked" || state === "revoked_by_owner") && feed && (
        <p
          role="status"
          data-testid="calendar-feed-revoked"
          style={{ margin: 0, fontSize: 13, padding: "8px 10px", borderRadius: 8, background: K.amberSoft, color: K.ink }}
        >
          {fmt(tr(state === "revoked_by_owner" ? "calendar.feed.revokedByOwner" : "calendar.feed.revoked"), {
            date: formatDate(feed.revoked_at, locale),
          })}
        </p>
      )}
      {state === "none" && <p style={{ margin: 0, fontSize: 13 }}>{tr("calendar.feed.none")}</p>}
      {state === "active" && feed && (
        <p style={{ margin: 0, fontSize: 13 }} data-testid="calendar-feed-active">
          {fmt(tr("calendar.feed.active"), { date: formatDate(feed.created_at, locale) })} ·{" "}
          {tr(`calendar.scope.${feed.scope}`)} ·{" "}
          {feed.last_fetched_at
            ? fmt(tr("calendar.feed.lastFetched"), { date: formatDate(feed.last_fetched_at, locale) })
            : tr("calendar.feed.neverFetched")}
        </p>
      )}
      {url && (
        <div data-testid="calendar-feed-url" style={{ display: "grid", gap: 6 }}>
          <p style={{ margin: 0, fontSize: 12.5, color: K.danger }}>{tr("calendar.feed.once")}</p>
          <code style={{ fontSize: 12, overflowWrap: "anywhere", padding: "6px 8px", background: K.neutralBg, borderRadius: 6 }}>
            {url}
          </code>
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <Button size="sm" onClick={copy} disabled={!hydrated}>
              {tr("calendar.feed.copy")}
            </Button>
            {copied && <span style={{ fontSize: 12.5, color: K.success }}>{tr("calendar.feed.copied")}</span>}
          </div>
          <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("calendar.feed.howto")}</p>
        </div>
      )}
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "flex-end" }}>
        <Field label={tr("calendar.feed.scope")}>
          <Select
            size="sm"
            value={scope}
            onChange={(e) => setScope(e.target.value as Scope)}
            options={[
              { value: "mine", label: tr("calendar.scope.mine") },
              { value: "agency", label: tr("calendar.scope.agency") },
            ]}
          />
        </Field>
        <span data-testid="calendar-feed-create" style={{ display: "contents" }}>
          <Button variant="primary" size="sm" disabled={!hydrated || busy} onClick={create}>
            {tr(state === "active" ? "calendar.feed.remake" : "calendar.feed.make")}
          </Button>
        </span>
        {state === "active" && (
          <span data-testid="calendar-feed-revoke" style={{ display: "contents" }}>
            <Button variant="danger" size="sm" disabled={!hydrated || busy} onClick={() => setConfirming(true)}>
              {tr("calendar.feed.revoke")}
            </Button>
          </span>
        )}
      </div>
      {error && (
        <p role="alert" style={{ margin: 0, fontSize: 13, color: K.danger }}>
          {tr("calendar.feed.error")}
        </p>
      )}
      <ConfirmModal
        open={confirming}
        onClose={() => setConfirming(false)}
        onConfirm={revoke}
        title={tr("calendar.feed.revoke")}
        body={tr("calendar.feed.revokeConfirm")}
        confirmLabel={tr("calendar.feed.revoke")}
        destructive
        busy={busy}
      />
    </div>
  );
}

export function TeamFeeds({
  initial,
  memberNames,
  lang,
  locale,
}: {
  initial: Feed[];
  memberNames: Record<string, string>;
  lang: Lang;
  locale: DateLocale;
}) {
  const tr = (key: string) => t(lang, key as MessageKey);
  const hydrated = useHydrated();
  const [feeds, setFeeds] = useState<Feed[]>(initial);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState(false);

  async function revoke(id: string) {
    setBusy(id);
    setError(false);
    const res = await bffSend<{ feed: Feed }>("POST", `calendar/feeds/${id}/revoke`);
    setBusy(null);
    if (!res.ok) return setError(true);
    setFeeds((prev) => prev.filter((f) => f.calendar_feed_id !== id));
  }

  if (feeds.length === 0) return <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{tr("calendar.feed.teamNone")}</p>;
  return (
    <div data-testid="calendar-team-feeds" style={{ display: "grid", gap: 8 }}>
      <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 6 }}>
        {feeds.map((f) => (
          <li key={f.calendar_feed_id} style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", fontSize: 13 }}>
            <span style={{ minWidth: 0, overflowWrap: "anywhere" }}>
              {memberNames[f.member_id] ?? f.member_id.slice(0, 8)} · {tr(`calendar.scope.${f.scope}`)} ·{" "}
              {f.last_fetched_at
                ? fmt(tr("calendar.feed.lastFetched"), { date: formatDate(f.last_fetched_at, locale) })
                : tr("calendar.feed.neverFetched")}
            </span>
            <Button size="sm" variant="ghost" disabled={!hydrated || busy !== null} onClick={() => revoke(f.calendar_feed_id)}>
              {tr("calendar.feed.revoke")}
            </Button>
          </li>
        ))}
      </ul>
      {error && (
        <p role="alert" style={{ margin: 0, fontSize: 13, color: K.danger }}>
          {tr("calendar.feed.error")}
        </p>
      )}
    </div>
  );
}
