"use client";
// web/components/travelOps/DepartureRoster.tsx — AA-1070 (crm-22): the roster of one AA departure on its page: the state
// the Booking feed sent (scheduled / guaranteed / cancelled), the pax, every trip on it (stage, Stage 3, rooms placed,
// passport issues) and every traveller (Stage 3 details, passport check, room), and the merged manifest for the
// agency's own tour leader (pick the columns, download a watermarked CSV; the API logs it). Every role. A group code
// of the agency's own (not an AA departure) has no roster: nothing shows. The manifest is a bulk reveal: a reason is
// needed above 5 travellers or with a health column (dietary, mobility), and it counts in the member's hourly limit.
import { useEffect, useState } from "react";
import { Badge, Button, Checkbox, Field, K, KpiTile, Select, useHydrated } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { ReportTable } from "@/components/reports/ReportTable";
import { bffSend } from "@/lib/bffClient";
import { formatDate, type DateLocale } from "@/lib/dates";
import { BULK_REASONS } from "@/lib/duty";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import {
  MANIFEST_FIELDS,
  departureTone,
  manifestNeedsReason,
  passportTone,
  paxTotal,
  travelErrorKey,
  type ManifestField,
  type RosterView,
} from "@/lib/travelOps";

export function DepartureRoster({ departureRef, lang, locale }: { departureRef: string; lang: Lang; locale: DateLocale }) {
  const tr = (key: string) => t(lang, key as MessageKey);
  const hydrated = useHydrated();
  const [view, setView] = useState<RosterView | null>(null);
  const [fields, setFields] = useState<ManifestField[]>([]);
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let stopped = false;
    void (async () => {
      const res = await bffSend<RosterView>("GET", `departures/${encodeURIComponent(departureRef)}/roster`);
      if (stopped || !res.ok) return;
      setView(res.data);
      setFields(res.data.manifest_default);
    })();
    return () => {
      stopped = true;
    };
  }, [departureRef]);

  if (!view) return null;

  async function download() {
    setBusy(true);
    const res = await bffSend<{ filename: string; csv: string }>("POST", `departures/${encodeURIComponent(departureRef)}/manifest`, { fields, ...(reason ? { reason } : {}) });
    setBusy(false);
    if (!res.ok) {
      if (!res.redirecting) setError(tr(travelErrorKey(res.detail)));
      return;
    }
    setError(null);
    const url = URL.createObjectURL(new Blob([res.data.csv], { type: "text/csv" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = res.data.filename;
    a.click();
    URL.revokeObjectURL(url);
  }

  const name = new Map(view.trips.map((x) => [x.trip_id, x.title]));
  const reasonMissing = manifestNeedsReason(view.travellers.length, fields) && !reason;
  return (
    <div style={{ display: "grid", gap: 14 }} data-testid="departure-roster">
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <strong style={{ fontSize: 14, overflowWrap: "anywhere" }}>{view.title}</strong>
        <span style={{ fontSize: 13, color: K.muted }}>
          {[view.service_from && formatDate(view.service_from, locale, { year: true }), view.service_to && formatDate(view.service_to, locale, { year: true })].filter(Boolean).join(" – ")}
        </span>
        <span data-testid="departure-state" data-state={view.state.status}>
          <Badge tone={departureTone(view.state.status)}>{tr(`travel.departure.state.${view.state.status}`)}</Badge>
        </span>
        {view.state.min_to_operate != null && <span style={{ fontSize: 12.5, color: K.muted }}>{fmt(tr("travel.departure.min"), { n: String(view.state.min_to_operate) })}</span>}
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 12 }}>
        <KpiTile size="sm" testId="roster-pax" label={tr("travel.departure.pax")} value={String(paxTotal(view.pax))} />
        <KpiTile size="sm" testId="roster-trips" label={tr("travel.departure.trips")} value={String(view.trips.length)} />
        <KpiTile size="sm" testId="roster-stage3" label={tr("travel.departure.stage3")} value={`${view.stage3_complete} / ${view.trips.length}`} />
      </div>
      <Panel title={tr("travel.departure.travellers")} testId="roster-travellers">
        <ReportTable
          tableId="roster-travellers"
          head={[tr("travel.col.traveller"), tr("travel.col.trip"), tr("travel.col.details"), tr("travel.col.passport"), tr("travel.col.room")]}
          rows={view.travellers.map((p) => ({
            key: `${p.trip_id}-${p.traveler_id}`,
            href: `/pipeline/${p.trip_id}`,
            cells: [
              p.role === "lead" ? `${p.name} (${tr("travel.lead")})` : p.name,
              name.get(p.trip_id) ?? "",
              tr(p.details_done ? "travel.details.done" : "travel.details.missing"),
              tr(`travel.passport.status.${p.passport}`),
              p.room ? `${p.room.room_no} · ${tr(`travel.room.${p.room.room_type}`)}` : "—",
            ],
          }))}
          empty={tr("travel.departure.noTravellers")}
        />
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 8 }}>
          {view.travellers.filter((p) => p.passport !== "ok").map((p) => (
            <Badge key={`${p.trip_id}-${p.traveler_id}`} tone={passportTone(p.passport)}>
              {`${p.name}: ${tr(`travel.passport.status.${p.passport}`)}`}
            </Badge>
          ))}
        </div>
      </Panel>
      <Panel title={tr("travel.manifest.title")} testId="roster-manifest">
        <p style={{ margin: "0 0 8px", fontSize: 12.5, color: K.muted }}>{tr("travel.manifest.intro")}</p>
        <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
          {MANIFEST_FIELDS.map((f) => (
            <Checkbox
              key={f}
              label={tr(`travel.manifest.field.${f}`)}
              checked={fields.includes(f)}
              onChange={(e) => setFields(e.target.checked ? MANIFEST_FIELDS.filter((x) => x === f || fields.includes(x)) : fields.filter((x) => x !== f))}
            />
          ))}
        </div>
        <div style={{ maxWidth: 320, marginTop: 10 }}>
          <Field label={tr("duty.reveal.reason")} hint={tr("travel.manifest.reasonNote")}>
            <Select
              size="sm"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder={tr("duty.reveal.reason.pick")}
              options={BULK_REASONS.map((r) => ({ value: r, label: tr(`duty.reveal.reason.${r}`) }))}
            />
          </Field>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 10 }}>
          <span data-testid="roster-manifest-download">
            <Button size="sm" variant="primary" disabled={busy || !hydrated || fields.length === 0 || reasonMissing} onClick={() => void download()}>
              {tr("travel.manifest.download")}
            </Button>
          </span>
          {error && <span style={{ fontSize: 12.5, color: K.danger }}>{error}</span>}
        </div>
      </Panel>
    </div>
  );
}
