"use client";
// web/components/proposals/AgencyBlockNote.tsx
// AA-1144 — what the builder drawer says about the agency's own items on this block (root ADR 0019 b6 as amended):
// - an Other block: the agency's own flights, visas, travel insurance and service fee go here (never sent to AA);
// - a lodging, activity, transport, meal or guide block: Adventure Asia items only (the drawer shows no agency tab);
//   on a lodging block, "Ask AA for extra nights" (B11): the place, before / after the tour, the check-in day, nights,
//   rooms and the party go to AA ground operations (POST trips/{id}/aa-extra-nights); the trip's earlier requests are
//   listed with "AA quoted" / "Not needed". No traveller names: AA ops read these rows across agencies.
// Kit: Field, Input, Select, Button, Badge.

import { useEffect, useState } from "react";
import { Badge, Button, Field, Input, K, Select, useHydrated } from "@/app/_kit";
import { DateInput } from "@/components/DateInput";
import {
  asksAaForNights,
  AGENCY_BLOCK_TYPES,
  EXTRA_NIGHT_PLACE_MAX,
  EXTRA_NIGHT_POSITIONS,
  extraNightBody,
  type ExtraNightForm,
  type ExtraNightRequest,
} from "@/lib/agencyItems";
import { bffSend } from "@/lib/bffClient";
import type { BlockType } from "@/lib/builder";
import { formatDate, type DateLocale } from "@/lib/dates";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";

const noteStyle = { margin: 0, fontSize: 12.5, color: K.muted, lineHeight: 1.5 } as const;

export function AgencyBlockNote({ blockType, tripId, lang, locale }: { blockType: BlockType; tripId: string; lang: Lang; locale: DateLocale }) {
  const tr = (key: string) => t(lang, key as MessageKey);
  if (AGENCY_BLOCK_TYPES.includes(blockType)) {
    return <p style={noteStyle} data-testid="agency-block-note">{tr("agencyItem.otherBlock")}</p>;
  }
  return (
    <div style={{ display: "grid", gap: 8 }}>
      <p style={noteStyle} data-testid="agency-block-note">{tr("agencyItem.onlyAa")}</p>
      {asksAaForNights(blockType) && <ExtraNightsAsk tripId={tripId} lang={lang} locale={locale} />}
    </div>
  );
}

function formOf(data: FormData): ExtraNightForm {
  const get = (k: string) => String(data.get(k) ?? "");
  return {
    position: get("position"),
    place: get("place"),
    check_in: get("check_in"),
    nights: get("nights"),
    rooms: get("rooms"),
    adults: get("adults"),
    children: get("children"),
  };
}

export function ExtraNightsAsk({ tripId, lang, locale }: { tripId: string; lang: Lang; locale: DateLocale }) {
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ tone: "ok" | "danger"; text: string } | null>(null);
  const [requests, setRequests] = useState<ExtraNightRequest[]>([]);

  async function load() {
    const res = await bffSend<{ requests: ExtraNightRequest[] }>("GET", `trips/${tripId}/aa-extra-nights`);
    if (res.ok) setRequests(res.data.requests);
  }

  useEffect(() => {
    let live = true;
    void bffSend<{ requests: ExtraNightRequest[] }>("GET", `trips/${tripId}/aa-extra-nights`).then((res) => {
      if (live && res.ok) setRequests(res.data.requests);
    });
    return () => {
      live = false;
    };
  }, [tripId]);

  async function send(data: FormData) {
    const out = extraNightBody(formOf(data));
    if ("invalid" in out) {
      setMessage({ tone: "danger", text: tr(`extraNights.invalid.${out.invalid}`) });
      return;
    }
    setBusy(true);
    const res = await bffSend("POST", `trips/${tripId}/aa-extra-nights`, out.body);
    setBusy(false);
    setMessage(res.ok ? { tone: "ok", text: tr("extraNights.sent") } : { tone: "danger", text: tr("extraNights.error") });
    if (res.ok) {
      setOpen(false);
      await load();
    }
  }

  async function close(request: ExtraNightRequest, reason: "quoted" | "not_needed") {
    setBusy(true);
    await bffSend("POST", `aa-extra-nights/${request.request_id}/close`, { reason });
    setBusy(false);
    await load();
  }

  return (
    <div style={{ display: "grid", gap: 8 }} data-testid="extra-nights">
      <p style={noteStyle}>{tr("extraNights.hint")}</p>
      {!open && (
        <div>
          <Button size="sm" disabled={!hydrated || busy} onClick={() => setOpen(true)} testId="extra-nights-ask">
            {tr("extraNights.ask")}
          </Button>
        </div>
      )}
      {open && <ExtraNightsFormFields lang={lang} locale={locale} busy={busy || !hydrated} onSend={send} onClose={() => setOpen(false)} />}
      {message && (
        <p role="status" style={{ margin: 0, fontSize: 12.5, color: message.tone === "ok" ? K.ink : K.danger }} data-testid="extra-nights-message">
          {message.text}
        </p>
      )}
      {requests.length > 0 && (
        <ul style={{ margin: 0, padding: 0, listStyle: "none", display: "grid", gap: 6 }} aria-label={tr("extraNights.asked")}>
          {requests.map((r) => (
            <ExtraNightRow key={r.request_id} request={r} lang={lang} locale={locale} busy={busy || !hydrated} onClose={close} />
          ))}
        </ul>
      )}
    </div>
  );
}

function ExtraNightsFormFields({
  lang,
  locale,
  busy,
  onSend,
  onClose,
}: {
  lang: Lang;
  locale: DateLocale;
  busy: boolean;
  onSend: (data: FormData) => void;
  onClose: () => void;
}) {
  const tr = (key: string) => t(lang, key as MessageKey);
  return (
    <form action={onSend} style={{ display: "grid", gap: 8, minWidth: 0 }} data-testid="extra-nights-form">
      <div style={{ display: "grid", gap: 8, gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 140px), 1fr))" }}>
        <Field label={tr("extraNights.position")}>
          <Select name="position" defaultValue="after" options={EXTRA_NIGHT_POSITIONS.map((p) => ({ value: p, label: tr(`extraNights.position.${p}`) }))} />
        </Field>
        <Field label={tr("extraNights.place")} hint={tr("extraNights.placeHint")} required>
          <Input name="place" required maxLength={EXTRA_NIGHT_PLACE_MAX} data-testid="extra-nights-place" />
        </Field>
        <DateInput locale={locale} name="check_in" label={tr("extraNights.checkIn")} readAs={tr("settings.readAs")} invalid={tr("settings.invalidDate")} />
        {(["nights", "rooms", "adults", "children"] as const).map((k) => (
          <Field key={k} label={tr(`extraNights.${k}`)}>
            <Input name={k} type="number" min={k === "children" ? 0 : 1} step={1} defaultValue={k === "children" ? "0" : k === "adults" ? "2" : "1"} data-testid={`extra-nights-${k}`} />
          </Field>
        ))}
      </div>
      <div style={{ display: "flex", gap: 8 }}>
        <Button type="submit" size="sm" variant="primary" disabled={busy} testId="extra-nights-send">
          {tr("extraNights.send")}
        </Button>
        <Button size="sm" variant="ghost" onClick={onClose}>
          {tr("extraNights.cancel")}
        </Button>
      </div>
    </form>
  );
}

function ExtraNightRow({
  request,
  lang,
  locale,
  busy,
  onClose,
}: {
  request: ExtraNightRequest;
  lang: Lang;
  locale: DateLocale;
  busy: boolean;
  onClose: (r: ExtraNightRequest, reason: "quoted" | "not_needed") => void;
}) {
  const tr = (key: string) => t(lang, key as MessageKey);
  const summary = fmt(tr("extraNights.summary"), {
    place: request.place,
    nights: request.nights,
    checkIn: formatDate(request.check_in, locale),
    rooms: request.rooms,
    adults: request.adults,
    children: request.children,
  });
  const isOpen = request.status === "open";
  return (
    <li style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", fontSize: 12.5 }} data-testid="extra-nights-row">
      <Badge tone={isOpen ? "info" : "neutral"}>{tr(`extraNights.status.${request.status}`)}</Badge>
      <span style={{ minWidth: 0, overflowWrap: "anywhere" }}>{summary}</span>
      {isOpen ? (
        <span style={{ display: "flex", gap: 6 }}>
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => onClose(request, "quoted")}>
            {tr("extraNights.close.quoted")}
          </Button>
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => onClose(request, "not_needed")}>
            {tr("extraNights.close.not_needed")}
          </Button>
        </span>
      ) : (
        request.close_reason && <span style={{ color: K.muted }}>{tr(`extraNights.close.${request.close_reason}`)}</span>
      )}
    </li>
  );
}
