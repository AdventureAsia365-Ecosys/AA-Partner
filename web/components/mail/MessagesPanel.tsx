"use client";
// web/components/mail/MessagesPanel.tsx — the trip workspace's Messages tab (AA-945, card CRM-17): the trip's e-mail
// conversations, one thread at a time (subject, text; the formatted HTML only inside an empty-sandbox iframe), the
// composer with saved replies, a draft saved 3 s after typing stops (PUT messages/drafts/{id}, idempotent), Send with
// a 10-second Undo, and a held message's "On hold: reconnect the mailbox" chip with "Edit and send again".
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";
import { Button, K, useHydrated } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { bffSend } from "@/lib/bffClient";
import { formatDate, type DateLocale } from "@/lib/dates";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import {
  createAutosave,
  htmlFrame,
  mailErrorKey,
  newDraftId,
  UNDO_MS,
  type Conversation,
  type MailMessage,
  type SavedReply,
  type Thread,
} from "@/lib/mail";

const field: React.CSSProperties = {
  font: "inherit",
  fontSize: 13,
  padding: "6px 8px",
  borderRadius: 8,
  border: `1px solid ${K.line2}`,
  background: K.card,
  color: K.ink,
  boxSizing: "border-box",
  width: "100%",
};

function MessageItem({
  m,
  lang,
  locale,
  onEdit,
}: {
  m: MailMessage;
  lang: Lang;
  locale: DateLocale;
  onEdit: (m: MailMessage) => void;
}) {
  const tr = (key: string) => t(lang, key as MessageKey);
  const [formatted, setFormatted] = useState(false);
  const held = m.status === "held_reconnect" || m.status === "failed";
  return (
    <li
      data-testid={`message-${m.message_id}`}
      style={{
        listStyle: "none",
        padding: 10,
        borderRadius: 10,
        border: `1px solid ${K.line2}`,
        background: m.direction === "out" ? K.card : "transparent",
        display: "grid",
        gap: 6,
      }}
    >
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8, fontSize: 12, color: K.muted }}>
        <span>{m.direction === "in" ? (m.from ?? tr("messages.unknownSender")) : tr("messages.from") + " " + (m.from ?? "")}</span>
        <span>{formatDate(m.occurred_at.slice(0, 10), locale)}</span>
        <span
          data-testid={`message-${m.message_id}-status`}
          style={{ color: held ? K.danger : K.muted, fontWeight: held ? 600 : 400 }}
        >
          {tr(`messages.status.${m.status}`)}
        </span>
        {m.imported && <span>{tr("messages.imported")}</span>}
      </div>
      {m.subject && <strong style={{ fontSize: 13.5 }}>{m.subject}</strong>}
      <div style={{ whiteSpace: "pre-wrap", fontSize: 13, overflowWrap: "anywhere" }}>{m.text}</div>
      {m.html && (
        <div>
          <button type="button" onClick={() => setFormatted(!formatted)} style={{ fontSize: 12, background: "none", border: "none", color: K.accent, cursor: "pointer", padding: 0 }}>
            {tr("messages.showFormatted")}
          </button>
          {formatted && (
            // Empty sandbox: opaque origin, no script, no forms, no popups, no cookies (design S10).
            <iframe {...htmlFrame(m.html)} style={{ width: "100%", minHeight: 240, border: `1px solid ${K.line2}`, borderRadius: 8, background: "#fff" }} />
          )}
        </div>
      )}
      {m.attachments.length > 0 && (
        <span style={{ fontSize: 12, color: K.muted }}>{fmt(tr("messages.attachments"), { n: m.attachments.length })}</span>
      )}
      {m.attachment_notes.map((code, i) => (
        <span key={`${code}-${i}`} style={{ fontSize: 12, color: K.muted }}>
          {tr(`messages.note.${code}`)}
        </span>
      ))}
      {held && m.author_member_id && (
        <div>
          <Button size="sm" onClick={() => onEdit(m)}>
            {tr("messages.edit")}
          </Button>
        </div>
      )}
    </li>
  );
}

export function MessagesPanel({
  tripId,
  conversations,
  savedReplies,
  lang,
  locale,
}: {
  tripId: string;
  conversations: Conversation[];
  savedReplies: SavedReply[];
  lang: Lang;
  locale: DateLocale;
}) {
  const router = useRouter();
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [selected, setSelected] = useState<string | null>(conversations[0]?.conversation_id ?? null);
  const [thread, setThread] = useState<Thread | null>(null);
  const [draftId, setDraftId] = useState<string>(() => newDraftId());
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [saveState, setSaveState] = useState<"idle" | "saving" | "saved">("idle");
  const [notice, setNotice] = useState<{ ok: boolean; text: string } | null>(null);
  const [undo, setUndo] = useState<{ messageId: string; until: number } | null>(null);
  const latest = useRef({ subject, body, draftId, selected });
  latest.current = { subject, body, draftId, selected };

  async function load(conversationId: string) {
    const res = await bffSend<Thread>("GET", `conversations/${conversationId}/messages`);
    if (!res.ok) {
      setNotice({ ok: false, text: tr(mailErrorKey(res.detail)) });
      return;
    }
    setThread(res.data);
    const draft = res.data.messages.find((m) => m.status === "draft_autosaved");
    if (draft) {
      setDraftId(draft.message_id);
      setSubject(draft.subject ?? "");
      setBody(draft.text ?? "");
    }
  }

  useEffect(() => {
    if (selected) void load(selected);
  }, [selected]);

  const target = () => {
    const now = latest.current;
    return now.selected ? { conversation_id: now.selected } : { trip_id: tripId };
  };

  const autosave = useMemo(
    () =>
      createAutosave(async () => {
        const now = latest.current;
        if (!now.subject && !now.body) return;
        setSaveState("saving");
        const res = await bffSend("PUT", `messages/drafts/${now.draftId}`, { ...target(), subject: now.subject, body: now.body });
        setSaveState(res.ok ? "saved" : "idle");
        if (!res.ok) setNotice({ ok: false, text: tr(mailErrorKey(res.detail)) });
      }),
    [],
  );

  useEffect(() => () => autosave.cancel(), [autosave]);

  useEffect(() => {
    if (!undo) return;
    const left = undo.until - Date.now();
    const timer = setTimeout(() => {
      setUndo(null);
      setDraftId(newDraftId());
      setSubject("");
      setBody("");
      if (latest.current.selected) void load(latest.current.selected);
      router.refresh();
    }, Math.max(0, left));
    return () => clearTimeout(timer);
  }, [undo]);

  async function send() {
    autosave.cancel();
    setNotice(null);
    const res = await bffSend<{ conversation_id: string }>("POST", "messages", {
      message_id: draftId,
      ...target(),
      subject,
      body,
    });
    if (!res.ok) {
      setNotice({ ok: false, text: tr(mailErrorKey(res.detail)) });
      return;
    }
    if (!selected) setSelected(res.data.conversation_id);
    setNotice({ ok: true, text: tr("messages.queued") });
    setUndo({ messageId: draftId, until: Date.now() + UNDO_MS });
  }

  async function undoSend() {
    if (!undo) return;
    const res = await bffSend("POST", `messages/${undo.messageId}/undo`);
    setUndo(null);
    setNotice(res.ok ? { ok: true, text: tr("messages.undone") } : { ok: false, text: tr(mailErrorKey(res.detail)) });
  }

  async function discard() {
    autosave.cancel();
    await bffSend("DELETE", `messages/drafts/${draftId}`);
    setDraftId(newDraftId());
    setSubject("");
    setBody("");
    setSaveState("idle");
  }

  async function edit(m: MailMessage) {
    const res = await bffSend("POST", `messages/${m.message_id}/edit`);
    if (!res.ok) {
      setNotice({ ok: false, text: tr(mailErrorKey(res.detail)) });
      return;
    }
    setDraftId(m.message_id);
    setSubject(m.subject ?? "");
    setBody(m.text ?? "");
    if (selected) void load(selected);
  }

  const visible = (thread?.messages ?? []).filter((m) => m.status !== "draft_autosaved");
  return (
    <Panel title={tr("messages.title")} testId="trip-messages">
      <div style={{ display: "grid", gap: 12 }}>
        {conversations.length > 1 && (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
            {conversations.map((c) => (
              <Button key={c.conversation_id} size="sm" variant={c.conversation_id === selected ? "primary" : "secondary"} onClick={() => setSelected(c.conversation_id)}>
                {formatDate(c.last_message_at.slice(0, 10), locale)} · {c.message_count}
              </Button>
            ))}
          </div>
        )}
        {notice && (
          <p role="status" style={{ margin: 0, fontSize: 13, color: notice.ok ? K.ink : K.danger }} data-testid="messages-notice">
            {notice.text}
          </p>
        )}
        {visible.length === 0 ? (
          <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{tr("messages.empty")}</p>
        ) : (
          <ul style={{ margin: 0, padding: 0, display: "grid", gap: 8 }}>
            {visible.map((m) => (
              <MessageItem key={m.message_id} m={m} lang={lang} locale={locale} onEdit={edit} />
            ))}
          </ul>
        )}
        <div style={{ display: "grid", gap: 8 }} data-testid="messages-composer">
          <strong style={{ fontSize: 13 }}>{tr("messages.compose")}</strong>
          {savedReplies.length > 0 && (
            <select
              aria-label={tr("messages.savedReply")}
              style={field}
              value=""
              onChange={(e) => {
                const reply = savedReplies.find((r) => r.saved_reply_id === e.target.value);
                if (reply) {
                  setBody((b) => (b ? `${b}\n\n${reply.body}` : reply.body));
                  autosave.schedule();
                }
              }}
            >
              <option value="">{tr("messages.savedReply")}</option>
              {savedReplies.map((r) => (
                <option key={r.saved_reply_id} value={r.saved_reply_id}>
                  {r.title}
                </option>
              ))}
            </select>
          )}
          <input
            aria-label={tr("messages.subject")}
            placeholder={tr("messages.subject")}
            style={field}
            value={subject}
            maxLength={300}
            onChange={(e) => {
              setSubject(e.target.value);
              autosave.schedule();
            }}
          />
          <textarea
            aria-label={tr("messages.body")}
            placeholder={tr("messages.body")}
            style={{ ...field, minHeight: 120, resize: "vertical" }}
            value={body}
            onChange={(e) => {
              setBody(e.target.value);
              autosave.schedule();
            }}
          />
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
            {undo ? (
              <Button size="sm" variant="danger" onClick={undoSend}>
                {tr("messages.undo")}
              </Button>
            ) : (
              <Button size="sm" variant="primary" disabled={!hydrated || !body.trim()} onClick={send}>
                {tr("messages.send")}
              </Button>
            )}
            {(subject || body) && !undo && (
              <Button size="sm" variant="ghost" onClick={discard}>
                {tr("messages.discard")}
              </Button>
            )}
            <span style={{ fontSize: 12, color: K.muted }} data-testid="messages-save-state">
              {saveState === "saving" ? tr("messages.saving") : saveState === "saved" ? tr("messages.saved") : ""}
            </span>
          </div>
        </div>
      </div>
    </Panel>
  );
}
