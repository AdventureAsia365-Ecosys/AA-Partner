"use client";
// web/components/mail/UnmatchedInbox.tsx — the Unmatched inbox (AA-945, card CRM-17; lane §6.5 "no match ->
// Unmatched"): forwarded mail no trip token, reply header or client address placed. Open a thread, search a client
// (the term travels in the POST body, never in the URL: AA-937), link it; the conversation then belongs to that client.
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Button, K, useHydrated } from "@/app/_kit";
import { bffSend } from "@/lib/bffClient";
import type { ClientRow, Directory } from "@/lib/clients";
import { formatDate, type DateLocale } from "@/lib/dates";
import { t, type Lang, type MessageKey } from "@/lib/i18n";
import { mailErrorKey, type Conversation, type Thread } from "@/lib/mail";

const field: React.CSSProperties = {
  font: "inherit",
  fontSize: 13,
  padding: "6px 8px",
  borderRadius: 8,
  border: `1px solid ${K.line2}`,
  background: K.card,
  color: K.ink,
  boxSizing: "border-box",
};

function Row({ c, lang, locale }: { c: Conversation; lang: Lang; locale: DateLocale }) {
  const router = useRouter();
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [thread, setThread] = useState<Thread | null>(null);
  const [results, setResults] = useState<ClientRow[]>([]);
  const [notice, setNotice] = useState<{ ok: boolean; text: string } | null>(null);

  async function open() {
    const res = await bffSend<Thread>("GET", `conversations/${c.conversation_id}/messages`);
    if (res.ok) setThread(res.data);
    else setNotice({ ok: false, text: tr(mailErrorKey(res.detail)) });
  }

  async function search(q: string) {
    if (!q.trim()) return;
    const res = await bffSend<Directory>("POST", "travelers/search", { q: q.trim() });
    setResults(res.ok ? res.data.items : []);
  }

  async function link(travelerId: string) {
    const res = await bffSend("POST", `conversations/${c.conversation_id}/link`, { traveler_id: travelerId });
    if (!res.ok) {
      setNotice({ ok: false, text: tr(mailErrorKey(res.detail)) });
      return;
    }
    setNotice({ ok: true, text: tr("inbox.linked") });
    router.refresh();
  }

  const first = thread?.messages.find((m) => m.direction === "in");
  return (
    <li data-testid={`unmatched-${c.conversation_id}`} style={{ listStyle: "none", border: `1px solid ${K.line2}`, borderRadius: 10, padding: 10, display: "grid", gap: 8 }}>
      <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", fontSize: 13 }}>
        <span>{formatDate(c.last_message_at.slice(0, 10), locale)}</span>
        <span style={{ color: K.muted }}>{c.message_count}</span>
        {!thread && (
          <Button size="sm" disabled={!hydrated} onClick={open}>
            {tr("inbox.open")}
          </Button>
        )}
      </div>
      {notice && (
        <p role="status" style={{ margin: 0, fontSize: 13, color: notice.ok ? K.ink : K.danger }}>
          {notice.text}
        </p>
      )}
      {thread && (
        <>
          <div style={{ fontSize: 13 }}>
            <div style={{ color: K.muted }}>{first?.from ?? tr("messages.unknownSender")}</div>
            {first?.subject && <strong>{first.subject}</strong>}
            <div style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{first?.text}</div>
          </div>
          <form
            action={(data) => void search(String(data.get("q") ?? ""))}
            style={{ display: "flex", gap: 8, flexWrap: "wrap" }}
          >
            <input name="q" aria-label={tr("inbox.search")} placeholder={tr("inbox.search")} style={{ ...field, flex: "1 1 220px" }} autoComplete="off" />
            <Button type="submit" size="sm">
              {tr("inbox.link")}
            </Button>
          </form>
          {results.length > 0 && (
            <ul style={{ margin: 0, padding: 0, display: "grid", gap: 4 }}>
              {results.map((r) => (
                <li key={r.traveler_id} style={{ listStyle: "none", display: "flex", gap: 8, alignItems: "center", fontSize: 13 }}>
                  <span>{r.display_name}</span>
                  <Button size="sm" variant="primary" onClick={() => link(r.traveler_id)}>
                    {tr("inbox.link")}
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </li>
  );
}

export function UnmatchedInbox({ conversations, lang, locale }: { conversations: Conversation[]; lang: Lang; locale: DateLocale }) {
  if (conversations.length === 0) {
    return (
      <p style={{ margin: 0, fontSize: 13, color: K.muted }} data-testid="inbox-empty">
        {t(lang, "inbox.empty")}
      </p>
    );
  }
  return (
    <ul style={{ margin: 0, padding: 0, display: "grid", gap: 10 }} data-testid="inbox-unmatched">
      {conversations.map((c) => (
        <Row key={c.conversation_id} c={c} lang={lang} locale={locale} />
      ))}
    </ul>
  );
}
