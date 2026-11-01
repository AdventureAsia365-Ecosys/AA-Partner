"use client";
// web/components/messaging/TextMessagesPanel.tsx — the trip's WhatsApp and SMS (AA-958, card CRM-28; WF-11 "WhatsApp
// window open 21 h left (outside: templates)"). One tab per channel: the thread with the client (their messages and the
// agency's, each outbound one with its delivery state from the status callbacks), the WhatsApp window, and the
// composer: free text while the window is open (SMS always), otherwise an approved template with its values and a
// preview. Send queues the message with the 10-second Undo of the mail composer (POST messages/{id}/undo). Why a
// channel cannot send (not connected, no mobile number, the client texted STOP) is said in words.
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { Badge, Button, Field, Input, K, Select, Tabs, Textarea, useHydrated } from "@/app/_kit";
import { useTripClosed } from "@/components/pipeline/TripClosed";
import { Panel } from "@/components/PageTitle";
import { bffSend } from "@/lib/bffClient";
import { formatDate, type DateLocale } from "@/lib/dates";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import { newDraftId, UNDO_MS, type Conversation, type MailMessage, type Thread } from "@/lib/mail";
import {
  deliveryTone,
  failureKey,
  MAX_TEXT,
  messagingErrorKey,
  renderTemplate,
  TEXT_CHANNELS,
  templateVariables,
  variableOk,
  windowHoursLeft,
  type ComposeState,
  type Delivery,
  type TextChannel,
} from "@/lib/messaging";

/** The end of the Undo delay, and the time left to it (outside render: the clock is read in handlers / effects). */
const undoUntil = () => Date.now() + UNDO_MS;
const msUntil = (until: number) => Math.max(0, until - Date.now());

type TextMessage = MailMessage & { delivery?: Delivery | null };
type TextThread = Thread & { messages: TextMessage[]; window_until?: string | null; opted_out_at?: string | null };

function Bubble({ m, lang, locale }: { m: TextMessage; lang: Lang; locale: DateLocale }) {
  const tr = (key: string) => t(lang, key as MessageKey);
  const out = m.direction === "out";
  const failed = m.status === "failed" || m.status === "held_reconnect";
  return (
    <li
      data-testid={`text-${m.message_id}`}
      style={{
        listStyle: "none",
        justifySelf: out ? "end" : "start",
        maxWidth: "min(520px, 92%)",
        padding: "8px 10px",
        borderRadius: 10,
        border: `1px solid ${K.line2}`,
        background: out ? K.card : K.bg,
        display: "grid",
        gap: 4,
      }}
    >
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8, fontSize: 11.5, color: K.muted, alignItems: "center" }}>
        <span>{out ? tr("msg.from") + " " + (m.from ?? "") : (m.from ?? tr("msg.unknownNumber"))}</span>
        <span>{formatDate(m.occurred_at.slice(0, 10), locale)}</span>
        {out && !failed && m.delivery?.status && (
          <Badge tone={deliveryTone(m.delivery.status)}>{tr(`msg.delivery.${m.delivery.status}`)}</Badge>
        )}
        {out && !failed && !m.delivery?.status && <Badge tone="neutral">{tr(`messages.status.${m.status}`)}</Badge>}
      </div>
      <div style={{ whiteSpace: "pre-wrap", fontSize: 13, overflowWrap: "anywhere" }}>{m.text}</div>
      {m.attachment_notes.includes("media_not_kept") && (
        <span style={{ fontSize: 12, color: K.muted }}>{tr("msg.mediaNotKept")}</span>
      )}
      {failed && (
        <span data-testid={`text-${m.message_id}-failure`} style={{ fontSize: 12, color: K.danger }}>
          {tr(failureKey(m.failure_code))}
        </span>
      )}
    </li>
  );
}

export function TextMessagesPanel({
  tripId,
  conversations,
  lang,
  locale,
  closed: closedProp = false,
}: {
  tripId: string;
  /** The trip's SMS / WhatsApp conversations (GET conversations?trip_id=, channel sms | whatsapp). */
  conversations: Conversation[];
  lang: Lang;
  locale: DateLocale;
  closed?: boolean;
}) {
  const router = useRouter();
  const hydrated = useHydrated();
  const closed = useTripClosed(closedProp);
  const tr = (key: string) => t(lang, key as MessageKey);
  const [channel, setChannel] = useState<TextChannel>(
    (conversations.find((c) => c.channel === "whatsapp" || c.channel === "sms")?.channel as TextChannel) ?? "whatsapp",
  );
  const [state, setState] = useState<ComposeState | null>(null);
  const [thread, setThread] = useState<TextThread | null>(null);
  const [body, setBody] = useState("");
  const [templateSid, setTemplateSid] = useState("");
  const [values, setValues] = useState<Record<string, string>>({});
  const [draftId, setDraftId] = useState(() => newDraftId());
  const [undo, setUndo] = useState<{ messageId: string; until: number } | null>(null);
  const [notice, setNotice] = useState<{ ok: boolean; text: string } | null>(null);
  const conversation = conversations
    .filter((c) => c.channel === channel)
    .sort((a, b) => b.last_message_at.localeCompare(a.last_message_at))[0];

  const conversationId = conversation?.conversation_id;
  const [stateTick, setStateTick] = useState(0);
  const [threadTick, setThreadTick] = useState(0);

  useEffect(() => {
    let stopped = false;
    void (async () => {
      const res = await bffSend<ComposeState>("GET", `messaging/compose?trip_id=${tripId}`);
      if (!stopped && res.ok) setState(res.data);
    })();
    return () => {
      stopped = true;
    };
  }, [tripId, stateTick]);

  useEffect(() => {
    if (!conversationId) return;
    let stopped = false;
    void (async () => {
      const res = await bffSend<TextThread>("GET", `conversations/${conversationId}/messages`);
      if (stopped) return;
      if (res.ok) setThread(res.data);
      else setNotice({ ok: false, text: t(lang, messagingErrorKey(res.detail) as MessageKey) });
    })();
    return () => {
      stopped = true;
    };
  }, [conversationId, threadTick, lang]);

  useEffect(() => {
    if (!undo) return;
    const timer = setTimeout(() => {
      setUndo(null);
      setDraftId(newDraftId());
      setBody("");
      setTemplateSid("");
      setValues({});
      setThreadTick((n) => n + 1);
      setStateTick((n) => n + 1);
      router.refresh();
    }, msUntil(undo.until));
    return () => clearTimeout(timer);
  }, [undo, router]);

  const shown = conversationId && thread?.conversation_id === conversationId ? thread : null;
  const here = state?.channels[channel];
  const hours = channel === "whatsapp" ? windowHoursLeft(shown?.window_until ?? here?.window_until) : 0;
  const needsTemplate = channel === "whatsapp" && hours === 0;
  const useTemplate = needsTemplate || templateSid !== "";
  const template = state?.templates.find((x) => x.content_sid === templateSid);
  const names = template ? templateVariables(template) : [];
  const ready = useTemplate
    ? !!template && names.every((n) => variableOk(values[n] ?? ""))
    : body.trim().length > 0 && body.length <= MAX_TEXT[channel];
  const canSend = hydrated && !!here?.available && ready && !undo;

  async function send() {
    setNotice(null);
    const target = conversation ? { conversation_id: conversation.conversation_id } : { trip_id: tripId, channel };
    const payload = useTemplate
      ? { content_sid: templateSid, variables: Object.fromEntries(names.map((n) => [n, (values[n] ?? "").trim()])) }
      : { body };
    const res = await bffSend<{ message_id: string; conversation_id: string }>("POST", "messaging/messages", {
      message_id: draftId,
      ...target,
      ...payload,
    });
    if (!res.ok) {
      setNotice({ ok: false, text: tr(messagingErrorKey(res.detail)) });
      return;
    }
    setNotice({ ok: true, text: tr("msg.queued") });
    setUndo({ messageId: draftId, until: undoUntil() });
    if (!conversation) router.refresh();
  }

  async function undoSend() {
    if (!undo) return;
    const res = await bffSend("POST", `messages/${undo.messageId}/undo`);
    setUndo(null);
    setNotice(res.ok ? { ok: true, text: tr("msg.undone") } : { ok: false, text: tr(messagingErrorKey(res.detail)) });
  }

  const visible = (shown?.messages ?? []).filter((m) => m.status !== "draft_autosaved");
  return (
    <Panel title={tr("msg.panel")} testId="trip-texts">
      <div style={{ display: "grid", gap: 12 }}>
        <Tabs
          tabs={TEXT_CHANNELS.map((c) => ({ key: c, label: tr(`msg.channel.${c}`) }))}
          active={channel}
          onChange={(k) => {
            setChannel(k as TextChannel);
            setTemplateSid("");
            setValues({});
          }}
          testId="texts-channel"
        />
        {closed && (
          <p style={{ margin: 0, padding: "8px 10px", borderRadius: 8, background: K.bg, fontSize: 12.5, color: K.muted }}>
            {tr("messages.closed")}
          </p>
        )}
        {visible.length === 0 ? (
          <p style={{ margin: 0, fontSize: 13, color: K.muted }} data-testid="texts-empty">
            {tr("msg.panelEmpty")}
          </p>
        ) : (
          <ul style={{ margin: 0, padding: 0, display: "grid", gap: 6 }} data-testid="texts-thread">
            {visible.map((m) => (
              <Bubble key={m.message_id} m={m} lang={lang} locale={locale} />
            ))}
          </ul>
        )}
        {notice && (
          <p role="status" data-testid="texts-notice" style={{ margin: 0, fontSize: 13, color: notice.ok ? K.ink : K.danger }}>
            {notice.text}
          </p>
        )}
        {here && !here.available && here.reason && (
          <p data-testid="texts-reason" style={{ margin: 0, fontSize: 13, color: K.muted }}>
            {tr(`msg.reason.${here.reason}`)}
          </p>
        )}
        {here?.available && (
          <div style={{ display: "grid", gap: 8 }} data-testid="texts-composer">
            {channel === "whatsapp" && (
              <span data-testid="texts-window" style={{ fontSize: 12.5, color: hours > 0 ? K.ink : K.muted }}>
                {hours > 0 ? fmt(tr("msg.window"), { hours }) : tr("msg.windowClosed")}
              </span>
            )}
            {channel === "whatsapp" && (state?.templates.length ?? 0) > 0 && (
              <Field label={tr("msg.template")}>
                <Select
                  value={templateSid}
                  onChange={(e) => setTemplateSid(e.currentTarget.value)}
                  placeholder={needsTemplate ? tr("msg.templatePick") : tr("msg.templateNone")}
                  options={(state?.templates ?? []).map((x) => ({ value: x.content_sid, label: `${x.name} (${x.language})` }))}
                  data-testid="texts-template"
                />
              </Field>
            )}
            {useTemplate ? (
              template && (
                <>
                  {names.map((n) => (
                    <Field key={n} label={fmt(tr("msg.variable"), { name: n })}>
                      <Input
                        value={values[n] ?? ""}
                        maxLength={1024}
                        onChange={(e) => {
                          const v = e.currentTarget.value;
                          setValues((old) => ({ ...old, [n]: v }));
                        }}
                      />
                    </Field>
                  ))}
                  <Field label={tr("msg.preview")}>
                    <div data-testid="texts-preview" style={{ whiteSpace: "pre-wrap", fontSize: 13, color: K.ink, overflowWrap: "anywhere" }}>
                      {renderTemplate(template.body, values)}
                    </div>
                  </Field>
                </>
              )
            ) : (
              <Field label={tr("msg.text")} hint={fmt(tr("msg.chars"), { n: body.length, max: MAX_TEXT[channel] })}>
                <Textarea
                  rows={4}
                  value={body}
                  maxLength={MAX_TEXT[channel]}
                  onChange={(e) => setBody(e.currentTarget.value)}
                  data-testid="texts-body"
                />
              </Field>
            )}
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              {undo ? (
                <Button size="sm" variant="danger" onClick={undoSend} testId="texts-undo">
                  {tr("msg.undo")}
                </Button>
              ) : (
                <Button size="sm" variant="primary" disabled={!canSend} onClick={send} testId="texts-send">
                  {tr("msg.send")}
                </Button>
              )}
            </div>
          </div>
        )}
      </div>
    </Panel>
  );
}
