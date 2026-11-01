"use client";
// web/components/messaging/NoTripInbox.tsx — the Inbox's "Clients with no open trip" list (AA-958, card CRM-28 "inbound
// match"; spec §11.2): a known client (their number matched one client) wrote on SMS / WhatsApp while no open trip
// holds them, so the conversation belongs to the client but to no trip, and neither the trip Messages tab nor the
// Unmatched list shows it. Open the message, then link it to one of the client's trips (the thread moves there and
// the reply is written from the trip), or open the client to start a trip first.
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Button, K, useHydrated } from "@/app/_kit";
import { bffSend } from "@/lib/bffClient";
import type { ClientProfile } from "@/lib/clients";
import { formatDate, type DateLocale } from "@/lib/dates";
import { t, type Lang, type MessageKey } from "@/lib/i18n";
import { mailErrorKey, type Conversation, type Thread } from "@/lib/mail";
import { stageLabel } from "@/lib/pipeline";

function Row({ c, lang, locale }: { c: Conversation; lang: Lang; locale: DateLocale }) {
  const router = useRouter();
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [thread, setThread] = useState<Thread | null>(null);
  const [client, setClient] = useState<ClientProfile | null>(null);
  const [notice, setNotice] = useState<{ ok: boolean; text: string } | null>(null);

  async function open() {
    const [res, profile] = await Promise.all([
      bffSend<Thread>("GET", `conversations/${c.conversation_id}/messages`),
      bffSend<ClientProfile>("GET", `travelers/${c.traveler_id}`),
    ]);
    if (!res.ok) {
      setNotice({ ok: false, text: tr(mailErrorKey(res.detail)) });
      return;
    }
    setThread(res.data);
    if (profile.ok) setClient(profile.data);
  }

  async function link(tripId: string) {
    const res = await bffSend("POST", `conversations/${c.conversation_id}/link`, {
      traveler_id: c.traveler_id,
      trip_id: tripId,
    });
    if (!res.ok) {
      setNotice({ ok: false, text: tr(mailErrorKey(res.detail)) });
      return;
    }
    setNotice({ ok: true, text: tr("msg.noTrip.linked") });
    router.refresh();
  }

  const last = thread ? [...thread.messages].reverse().find((m) => m.direction === "in") : undefined;
  return (
    <li
      data-testid={`no-trip-${c.conversation_id}`}
      style={{ listStyle: "none", border: `1px solid ${K.line2}`, borderRadius: 10, padding: 10, display: "grid", gap: 8 }}
    >
      <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", fontSize: 13 }}>
        <span>{formatDate(c.last_message_at.slice(0, 10), locale)}</span>
        <span style={{ color: K.muted }}>{tr(`msg.channel.${c.channel === "sms" ? "sms" : "whatsapp"}`)}</span>
        <span style={{ color: K.muted }}>{c.message_count}</span>
        {client && <strong>{client.traveler.display_name}</strong>}
        <a href={`/clients/${c.traveler_id}`} style={{ color: K.ink }}>
          {tr("msg.noTrip.openClient")}
        </a>
        {!thread && (
          <Button size="sm" disabled={!hydrated} onClick={open} testId={`no-trip-${c.conversation_id}-open`}>
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
          <div style={{ fontSize: 13, whiteSpace: "pre-wrap", overflowWrap: "anywhere" }} data-testid={`no-trip-${c.conversation_id}-text`}>
            {last?.text ?? ""}
          </div>
          {client && client.trips.length > 0 ? (
            <ul style={{ margin: 0, padding: 0, display: "grid", gap: 4 }}>
              {client.trips.map((trip) => (
                <li key={trip.trip_id} style={{ listStyle: "none", display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", fontSize: 13 }}>
                  <span style={{ overflowWrap: "anywhere" }}>{trip.title}</span>
                  <span style={{ color: K.muted }}>{stageLabel(lang, trip.stage)}</span>
                  <Button size="sm" variant="primary" onClick={() => link(trip.trip_id)} testId={`no-trip-link-${trip.trip_id}`}>
                    {tr("msg.noTrip.link")}
                  </Button>
                </li>
              ))}
            </ul>
          ) : (
            <p style={{ margin: 0, fontSize: 13, color: K.muted }} data-testid={`no-trip-${c.conversation_id}-none`}>
              {tr("msg.noTrip.noTrips")}
            </p>
          )}
        </>
      )}
    </li>
  );
}

export function NoTripInbox({ conversations, lang, locale }: { conversations: Conversation[]; lang: Lang; locale: DateLocale }) {
  if (conversations.length === 0) return null;
  return (
    <section style={{ display: "grid", gap: 10 }} data-testid="inbox-no-trip">
      <h2 style={{ margin: 0, fontSize: 15 }}>{t(lang, "msg.noTrip.title")}</h2>
      <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{t(lang, "msg.noTrip.help")}</p>
      <ul style={{ margin: 0, padding: 0, display: "grid", gap: 10 }}>
        {conversations.map((c) => (
          <Row key={c.conversation_id} c={c} lang={lang} locale={locale} />
        ))}
      </ul>
    </section>
  );
}
