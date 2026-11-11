"use client";
// web/components/portal/PortalTravel.tsx — AA-1070 on the traveller portal (/c/t): the trip's vouchers the agency
// released (a booked trip, a confirmed service shown to travellers), each printable on its own (print or save as PDF
// from the browser); the rooming list (the lead sets who shares which room after the deposit, a companion sees their
// own room); and the traveller's own visa checklists, ticked as documents are ready. Everything comes from
// GET `base`/travel and goes back through the portal's own routes (the API checks the session, the lead and the lock).
import { useEffect, useState } from "react";
import { flushSync } from "react-dom";
import { Button, Checkbox, K, Select } from "@/app/_kit";
import { formatDate, type DateLocale } from "@/lib/dates";
import { fmt } from "@/lib/i18n";
import {
  CAPACITY,
  ROOM_TYPES,
  roomingBody,
  type BedPref,
  type RoomType,
  type RoomingView,
  type VisaApplication,
  type Voucher,
  type VouchersView,
} from "@/lib/travelOps";

type T = (key: string) => string;
type Step = (method: string, path: string, body: unknown, done: (data: Record<string, unknown>) => void) => Promise<void>;

const section: React.CSSProperties = { display: "grid", gap: 8, paddingTop: 12, borderTop: `1px solid ${K.line}` };
const line: React.CSSProperties = { margin: 0, fontSize: 13.5, color: K.body, overflowWrap: "anywhere" };
const h2: React.CSSProperties = { margin: 0, fontSize: 16, color: K.ink };
// Print one voucher alone: everything else is hidden while the browser prints.
const PRINT_CSS =
  "@media print{body *{visibility:hidden}[data-print-voucher=on],[data-print-voucher=on] *{visibility:visible}" +
  "[data-print-voucher=on]{position:absolute;left:0;top:0;width:100%}}";

/** What every printed voucher carries besides the service: the agency, the trip, the lead traveller, the party size. */
export type VoucherHeader = Pick<VouchersView, "agency_name" | "trip_title" | "start_date" | "end_date" | "lead_traveller" | "pax">;

interface TravelView {
  vouchers: Voucher[];
  voucher_header?: VoucherHeader | null;
  rooming: RoomingView;
  visas: VisaApplication[];
}

/** The answer of GET `base`/travel, or null when it is not one (an older API, an error page). */
export function travelView(data: unknown): TravelView | null {
  const v = data as Partial<TravelView> | null;
  if (!v || !Array.isArray(v.vouchers) || !Array.isArray(v.visas) || !v.rooming || !Array.isArray(v.rooming.rooms)) return null;
  return v as TravelView;
}

export function PortalTravel({ base, t, locale, busy, step, round }: { base: string; t: T; locale: DateLocale; busy: boolean; step: Step; round: number }) {
  const [view, setView] = useState<TravelView | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let stopped = false;
    void fetch(`${base}/travel`, { method: "GET", credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" } })
      .then(async (res) => (res.ok ? travelView(await res.json()) : null))
      .catch(() => null)
      .then((data) => {
        if (!stopped) setView(data);
      });
    return () => {
      stopped = true;
    };
  }, [base, round, tick]);

  if (!view) return null;
  return (
    <>
      <style>{PRINT_CSS}</style>
      <VouchersSection vouchers={view.vouchers} header={view.voucher_header ?? null} t={t} locale={locale} />
      <RoomingSection rooming={view.rooming} t={t} busy={busy} step={step} onSaved={() => setTick((n) => n + 1)} />
      {view.visas.length > 0 && <VisasSection visas={view.visas} t={t} locale={locale} busy={busy} step={step} onSaved={() => setTick((n) => n + 1)} />}
    </>
  );
}

function VouchersSection({ vouchers, header, t, locale }: { vouchers: Voucher[]; header: VoucherHeader | null; t: T; locale: DateLocale }) {
  const [printing, setPrinting] = useState<string | null>(null);
  function print(id: string) {
    flushSync(() => setPrinting(id)); // the voucher is marked before the browser lays out the print
    window.print();
    setPrinting(null);
  }
  return (
    <section data-testid="portal-vouchers" style={section}>
      <h2 style={h2}>{t("client.travel.vouchers")}</h2>
      {vouchers.length === 0 ? (
        <p style={{ ...line, color: K.muted }}>{t("client.travel.noVouchers")}</p>
      ) : (
        vouchers.map((v) => (
          <div
            key={v.booking_line_id}
            data-testid="portal-voucher"
            data-print-voucher={printing === v.booking_line_id ? "on" : "off"}
            style={{ display: "grid", gap: 4, border: `1px solid ${K.line}`, borderRadius: 8, padding: "10px 12px" }}
          >
            {header && <VoucherHead header={header} t={t} locale={locale} />}
            <strong style={{ fontSize: 14, color: K.ink, overflowWrap: "anywhere" }}>{v.title}</strong>
            <p style={line}>
              {[v.service_from && formatDate(v.service_from, locale, { year: true }), v.service_time].filter(Boolean).join(" · ")}
            </p>
            {[
              ["client.travel.voucherNo", v.voucher_no],
              ["client.travel.pickup", v.pickup],
              ["client.travel.dropoff", v.dropoff],
              ["client.travel.supplier", v.supplier.name],
              ["client.travel.contact", v.supplier.contact_name],
              ["client.travel.confirmation", v.confirmation_no],
              ["client.travel.pnr", v.pnr],
              ["client.travel.aaRef", v.supplier.aa_booking_ref],
              ["client.travel.note", v.note],
            ]
              .filter(([, value]) => value)
              .map(([key, value]) => (
                <p key={key} style={line}>
                  <span style={{ color: K.muted }}>{t(key as string)}: </span>
                  {value}
                </p>
              ))}
            <div>
              <Button size="sm" variant="ghost" onClick={() => print(v.booking_line_id)}>
                {t("client.travel.print")}
              </Button>
            </div>
          </div>
        ))
      )}
    </section>
  );
}

/** A traveller's bed preference on file (an unplaced traveller has none). */
export function bedOf(p: { traveler_id: string; bed_pref?: BedPref | null }): BedPref | null {
  return "bed_pref" in p ? (p.bed_pref ?? null) : null;
}

/** The voucher's header: the agency's name, then the trip (title, dates), the lead traveller and the party size. */
function VoucherHead({ header, t, locale }: { header: VoucherHeader; t: T; locale: DateLocale }) {
  const dates = [header.start_date, header.end_date].filter((d): d is string => !!d).map((d) => formatDate(d, locale, { year: true }));
  const rows: [string, string | null][] = [
    ["client.travel.trip", [header.trip_title, [...new Set(dates)].join(" – ")].filter(Boolean).join(" · ") || null],
    ["client.travel.leadTraveller", header.lead_traveller],
    ["client.travel.party", header.pax ? String(header.pax) : null],
  ];
  return (
    <div data-testid="portal-voucher-head" style={{ display: "grid", gap: 2, paddingBottom: 6, borderBottom: `1px solid ${K.line}` }}>
      {header.agency_name && <strong style={{ fontSize: 15, color: K.ink, overflowWrap: "anywhere" }}>{header.agency_name}</strong>}
      {rows
        .filter(([, value]) => value)
        .map(([key, value]) => (
          <p key={key} style={line}>
            <span style={{ color: K.muted }}>{t(key)}: </span>
            {value}
          </p>
        ))}
    </div>
  );
}

function RoomingSection({ rooming, t, busy, step, onSaved }: { rooming: RoomingView; t: T; busy: boolean; step: Step; onSaved: () => void }) {
  const people = [...rooming.rooms.flatMap((r) => r.travellers), ...rooming.unassigned];
  const [rooms, setRooms] = useState(() => rooming.rooms.map((r) => ({ room_no: r.room_no, room_type: r.room_type })));
  const [placed, setPlaced] = useState<Record<string, number | null>>(() => {
    const out: Record<string, number | null> = {};
    for (const p of rooming.unassigned) out[p.traveler_id] = null;
    for (const r of rooming.rooms) for (const p of r.travellers) out[p.traveler_id] = r.room_no;
    return out;
  });
  const [error, setError] = useState<string | null>(null);
  if (!rooming.can_edit && rooming.rooms.length === 0) return null;
  const roomLabel = (n: number) => fmt(t("client.travel.room"), { n: String(n) });

  if (!rooming.can_edit) {
    return (
      <section data-testid="portal-rooming" style={section}>
        <h2 style={h2}>{t("client.travel.rooming")}</h2>
        {rooming.rooms.map((r) => (
          <p key={r.room_no} style={line}>
            {`${roomLabel(r.room_no)} · ${t(`client.travel.roomType.${r.room_type}`)}: ${r.travellers.map((p) => p.name).join(", ")}`}
          </p>
        ))}
      </section>
    );
  }

  function save() {
    // The lead never sets a bed preference here: each traveller keeps the one the agency set (the PUT replaces the list).
    const travellersOf = (roomNo: number) => people.filter((p) => placed[p.traveler_id] === roomNo).map((p) => ({ traveler_id: p.traveler_id, bed_pref: bedOf(p) }));
    const body = roomingBody(rooms.map((r) => ({ ...r, travellers: travellersOf(r.room_no) })));
    if (!body.ok) return setError(t(`client.travel.err.${body.error}`));
    setError(null);
    void step("PUT", "/rooming", body.body, () => onSaved());
  }

  const next = rooms.reduce((m, r) => Math.max(m, r.room_no), 0) + 1;
  return (
    <section data-testid="portal-rooming" style={section}>
      <h2 style={h2}>{t("client.travel.rooming")}</h2>
      <p style={{ ...line, color: K.muted }}>{t("client.travel.roomingIntro")}</p>
      {rooms.map((r, i) => (
        <div key={i} style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <span style={{ fontSize: 13.5, minWidth: 70 }}>{roomLabel(r.room_no)}</span>
          <span style={{ minWidth: 150 }}>
            <Select
              size="sm"
              aria-label={t("client.travel.roomTypeLabel")}
              value={r.room_type}
              onChange={(e) => setRooms(rooms.map((x, j) => (j === i ? { ...x, room_type: e.target.value as RoomType } : x)))}
              options={ROOM_TYPES.map((k) => ({ value: k, label: `${t(`client.travel.roomType.${k}`)} (${CAPACITY[k]})` }))}
            />
          </span>
        </div>
      ))}
      <div>
        <Button size="sm" variant="ghost" disabled={busy} onClick={() => setRooms([...rooms, { room_no: next, room_type: "twin" }])}>
          {t("client.travel.addRoom")}
        </Button>
      </div>
      {people.map((p) => (
        <div key={p.traveler_id} style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <span style={{ fontSize: 13.5, minWidth: 140, overflowWrap: "anywhere" }}>{p.name}</span>
          <span style={{ minWidth: 140 }}>
            <Select
              size="sm"
              aria-label={fmt(t("client.travel.pickRoom"), { name: p.name })}
              value={placed[p.traveler_id] == null ? "" : String(placed[p.traveler_id])}
              onChange={(e) => setPlaced({ ...placed, [p.traveler_id]: e.target.value ? Number(e.target.value) : null })}
              options={[{ value: "", label: t("client.travel.noRoom") }, ...rooms.map((r) => ({ value: String(r.room_no), label: roomLabel(r.room_no) }))]}
            />
          </span>
        </div>
      ))}
      {error && <p style={{ ...line, color: K.danger }}>{error}</p>}
      <div>
        <Button size="sm" variant="primary" disabled={busy} onClick={save}>
          {t("client.travel.saveRooms")}
        </Button>
      </div>
    </section>
  );
}

function VisasSection({ visas, t, locale, busy, step, onSaved }: { visas: VisaApplication[]; t: T; locale: DateLocale; busy: boolean; step: Step; onSaved: () => void }) {
  return (
    <section data-testid="portal-visas" style={section}>
      <h2 style={h2}>{t("client.travel.visas")}</h2>
      {visas.map((v) => (
        <div key={v.visa_application_id} style={{ display: "grid", gap: 4 }}>
          <p style={line}>
            <strong>{v.country}</strong> · {t(`client.travel.visaStatus.${v.status}`)}
            {v.deadline ? ` · ${fmt(t("client.travel.readyBy"), { date: formatDate(v.deadline, locale) })}` : ""}
          </p>
          {v.checklist.map((i) => (
            <Checkbox
              key={i.visa_checklist_item_id}
              label={i.label}
              checked={i.done}
              disabled={busy}
              onChange={(e) => void step("PUT", `/visa-checklist/${i.visa_checklist_item_id}`, { done: e.target.checked }, () => onSaved())}
            />
          ))}
        </div>
      ))}
    </section>
  );
}
