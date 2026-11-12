"use client";
// web/components/pipeline/AaCancelCard.tsx — AA-1070 (crm-13): the trip page's guided card when Adventure Asia
// cancelled a departure the trip is on (GET /trips/{id} `aa_cancel`). Two answers, nothing sent to the client:
//   (a) "Offer another date": the open departures of the same AA tour (GET aa/products/{source_id}, live from Booking)
//       -> POST trips/{id}/aa-cancel/{case}/requote -> a new proposal version prefilled on that date (a new trip when
//       the trip is booked or its proposal accepted), opened in the builder;
//   (b) "Cancel at no charge": the cancel dialog on the reason "aa_cancelled" (the API forces the charge to 0 and
//       prefills full refunds as drafts that still need approval).
// Kit components only; no personal data (ids, codes and dates).
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Badge, Button, Field, K, Select } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { bffSend } from "@/lib/bffClient";
import type { AaDeparture, AaProduct } from "@/lib/aa";
import { agencyToday, requoteChoices, requoteErrorKey, requoteHref, type AaCancelCase, type AaCancelView, type RequoteResult } from "@/lib/aaCancel";
import { formatDate, type DateLocale } from "@/lib/dates";
import { fmt, type Lang, type MessageKey, t } from "@/lib/i18n";

export function AaCancelCard({
  tripId,
  view,
  lang,
  locale,
  timeZone,
  canCancel,
  onCancel,
}: {
  tripId: string;
  view: AaCancelView;
  lang: Lang;
  locale: DateLocale;
  /** The agency's IANA zone: departures before its today are not offered. */
  timeZone: string;
  /** The member may cancel the trip (owner / agent; the API checks too). */
  canCancel: boolean;
  /** Opens the cancel dialog on the reason "aa_cancelled". */
  onCancel: () => void;
}) {
  const shown = view.cases.filter((c) => c.active || c.resolution === "requoted");
  if (!shown.length && !view.rebooked_from) return null;
  return (
    <Panel title={t(lang, "aaCancel.title")} testId="trip-aa-cancel">
      <div style={{ display: "grid", gap: 14 }}>
        {view.rebooked_from && (
          <p style={{ margin: 0, fontSize: 13, color: K.body }} data-testid="trip-aa-rebooked-from">
            {t(lang, "aaCancel.rebookedFrom")}{" "}
            <Link href={`/pipeline/${view.rebooked_from}`} style={{ color: K.accentDeep }}>
              {t(lang, "aaCancel.openOldTrip")}
            </Link>
          </p>
        )}
        {shown.map((c) => (
          <CaseBlock key={c.case_id} tripId={tripId} c={c} lang={lang} locale={locale} timeZone={timeZone} canCancel={canCancel} onCancel={onCancel} />
        ))}
      </div>
    </Panel>
  );
}

function CaseBlock({
  tripId,
  c,
  lang,
  locale,
  timeZone,
  canCancel,
  onCancel,
}: {
  tripId: string;
  c: AaCancelCase;
  lang: Lang;
  locale: DateLocale;
  timeZone: string;
  canCancel: boolean;
  onCancel: () => void;
}) {
  const router = useRouter();
  const [choices, setChoices] = useState<AaDeparture[] | null>(null);
  const [picked, setPicked] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const tr = (key: string) => t(lang, key as MessageKey);
  const title = c.tour?.title ?? c.departure_id;
  const when = c.tour?.service_from ? formatDate(c.tour.service_from, locale) : null;

  async function loadDates() {
    if (!c.tour?.tour_source_id) return;
    setBusy(true);
    setError(null);
    const res = await bffSend<AaProduct>("GET", `aa/products/${encodeURIComponent(c.tour.tour_source_id)}`);
    setBusy(false);
    if (!res.ok) {
      setError(tr("aaCancel.err.dates"));
      return;
    }
    const open = requoteChoices(res.data.departures ?? [], c.departure_id, agencyToday(timeZone));
    setChoices(open);
    setPicked(open[0]?.departure_id ?? "");
  }

  async function requote() {
    const dep = choices?.find((d) => d.departure_id === picked);
    if (!dep) return;
    setBusy(true);
    setError(null);
    const res = await bffSend<RequoteResult>("POST", `trips/${tripId}/aa-cancel/${c.case_id}/requote`, {
      departure_id: dep.departure_id,
      start_date: dep.start_date,
    });
    setBusy(false);
    if (!res.ok) {
      setError(tr(requoteErrorKey(res.detail)));
      return;
    }
    router.push(requoteHref(res.data));
  }

  return (
    <div data-testid="aa-cancel-case" data-case={c.case_id} data-resolution={c.resolution ?? "open"} style={{ display: "grid", gap: 8, fontSize: 13 }}>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
        <Badge tone="danger">{tr("aaCancel.badge")}</Badge>
        <span style={{ color: K.ink, overflowWrap: "anywhere" }}>{when ? fmt(tr("aaCancel.lineWhen"), { title, date: when }) : fmt(tr("aaCancel.line"), { title })}</span>
      </div>
      <p style={{ margin: 0, color: K.muted }}>{tr("aaCancel.help")}</p>
      {c.resolution === "requoted" && c.proposal_id && (
        <p style={{ margin: 0, color: K.body }} data-testid="aa-cancel-requoted">
          {c.rebook_trip_id ? tr("aaCancel.requotedTrip") : fmt(tr("aaCancel.requotedVersion"), { n: c.proposal_version ?? "" })}{" "}
          <Link href={requoteHref({ proposal_id: c.proposal_id, trip_id: c.rebook_trip_id ?? tripId })} style={{ color: K.accentDeep }}>
            {tr("aaCancel.openVersion")}
          </Link>
        </p>
      )}
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "flex-end" }}>
        {c.can_requote && choices === null && (
          <span data-testid="aa-cancel-requote" style={{ display: "contents" }}>
            <Button size="sm" variant="primary" disabled={busy || !c.tour?.tour_source_id} onClick={() => void loadDates()}>
              {tr(c.requote_mode === "new_trip" ? "aaCancel.requoteTrip" : "aaCancel.requote")}
            </Button>
          </span>
        )}
        {c.can_requote && choices !== null && choices.length === 0 && (
          <span style={{ color: K.amber }} data-testid="aa-cancel-no-dates">
            {tr("aaCancel.noDates")}
          </span>
        )}
        {c.can_requote && choices !== null && choices.length > 0 && (
          <>
            <Field label={tr("aaCancel.pickDate")}>
              <Select
                size="sm"
                value={picked}
                data-testid="aa-cancel-date"
                onChange={(e) => setPicked(e.target.value)}
                options={choices.map((d) => ({ value: d.departure_id, label: fmt(tr("aaCancel.dateOption"), { date: formatDate(d.start_date, locale), n: d.available }) }))}
              />
            </Field>
            <span data-testid="aa-cancel-requote-confirm" style={{ display: "contents" }}>
              <Button size="sm" variant="primary" disabled={busy || !picked} onClick={() => void requote()}>
                {tr("aaCancel.requoteConfirm")}
              </Button>
            </span>
          </>
        )}
        {c.can_cancel && canCancel && (
          <span data-testid="aa-cancel-cancel" style={{ display: "contents" }}>
            <Button size="sm" variant="danger" disabled={busy} onClick={onCancel}>
              {tr("aaCancel.cancelFree")}
            </Button>
          </span>
        )}
      </div>
      {error && (
        <p role="alert" style={{ margin: 0, color: K.danger }}>
          {error}
        </p>
      )}
    </div>
  );
}
