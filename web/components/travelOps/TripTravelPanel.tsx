"use client";
// web/components/travelOps/TripTravelPanel.tsx — AA-1070 (crm-054 / crm-055): the trip's travel operations under the
// workspace. Loaded in the browser after the page (GET passports, rooming, vouchers, visas):
// - Passports: each traveller's check against the trip's end + the agency's months (status only, never the expiry);
// - Rooming list: rooms (number, type) and who sleeps where (bed preference), checked against the room type's
//   capacity here and against the accepted proposal's room mix by the API;
// - Vouchers: each confirmed service, released or why not, the details travellers need (pick-up, drop-off, start
//   time, booking reference, note: sealed by the API) and a server PDF;
// - Visas: the agency's own tracker per traveller and country (suggested from the destinations), status moves, the
//   granted visa, the document checklist;
// - Documents: a server PDF of the booking confirmation (it appears under Documents when the job is done).
import { useEffect, useMemo, useState } from "react";
import { Badge, Button, Checkbox, Field, Input, K, Select, useHydrated } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { bffSend } from "@/lib/bffClient";
import { formatDate, type DateLocale } from "@/lib/dates";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import {
  BED_PREFS,
  CAPACITY,
  ROOM_TYPES,
  VISA_MOVES,
  passportTone,
  roomingBody,
  travelErrorKey,
  visaOverdue,
  visaTone,
  type BedPref,
  type PassportCheck,
  type RoomType,
  type RoomingView,
  type VisaApplication,
  type VisaStatus,
  type VisaSuggestion,
  type VisasView,
  type Voucher,
  type VouchersView,
} from "@/lib/travelOps";

const h3 = { margin: "4px 0 6px", fontSize: 13, fontWeight: 600 } as const;
const muted = { margin: 0, fontSize: 12.5, color: K.muted } as const;
const row = { display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" } as const;
// A proposal version the text renderer prints (app.proposals.text_proposal RENDERABLE: a builder draft is not one).
const PDF_PROPOSAL_STATUSES = ["sent", "published", "accepted"];

interface Props {
  tripId: string;
  lang: Lang;
  locale: DateLocale;
  closed?: boolean;
}

type Notice = { tone: "danger" | "success"; text: string } | null;

export function TripTravelPanel({ tripId, lang, locale, closed = false }: Props) {
  const tr = (key: string) => t(lang, key as MessageKey);
  const hydrated = useHydrated();
  const [tick, setTick] = useState(0);
  const reload = () => setTick((n) => n + 1);
  const [passports, setPassports] = useState<PassportCheck | null>(null);
  const [rooming, setRooming] = useState<RoomingView | null>(null);
  const [vouchers, setVouchers] = useState<VouchersView | null>(null);
  const [visas, setVisas] = useState<VisasView | null>(null);
  const [pdfSubjects, setPdfSubjects] = useState<{ invoices: { id: string; label: string }[]; proposals: { id: string; label: string }[] }>({ invoices: [], proposals: [] });
  const [notice, setNotice] = useState<Notice>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let stopped = false;
    void (async () => {
      const [p, r, v, s, inv, life] = await Promise.all([
        bffSend<PassportCheck>("GET", `trips/${tripId}/passports`),
        bffSend<RoomingView>("GET", `trips/${tripId}/rooming`),
        bffSend<VouchersView>("GET", `trips/${tripId}/vouchers`),
        bffSend<VisasView>("GET", `trips/${tripId}/visas`),
        bffSend<{ invoices: { invoice_id: string; number: string | null; status: string; kind: string }[] }>("GET", `trips/${tripId}/invoices`),
        bffSend<{ proposals: { proposal_id: string; version: number; status: string }[] }>("GET", `trips/${tripId}/lifecycle`),
      ]);
      if (stopped) return;
      setPdfSubjects({
        invoices: inv.ok ? inv.data.invoices.filter((i) => i.status !== "void").map((i) => ({ id: i.invoice_id, label: i.number ?? tr("travel.pdf.draftInvoice") })) : [],
        proposals: life.ok
          ? life.data.proposals.filter((x) => PDF_PROPOSAL_STATUSES.includes(x.status)).map((x) => ({ id: x.proposal_id, label: fmt(tr("travel.pdf.version"), { n: String(x.version) }) }))
          : [],
      });
      if (p.ok) setPassports(p.data);
      if (r.ok) setRooming(r.data);
      if (v.ok) setVouchers(v.data);
      if (s.ok) setVisas(s.data);
    })();
    return () => {
      stopped = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the labels follow the language only
  }, [tripId, tick]);

  const names = useMemo(() => {
    const out = new Map<string, string>();
    for (const room of rooming?.rooms ?? []) for (const p of room.travellers) out.set(p.traveler_id, p.name);
    for (const p of rooming?.unassigned ?? []) out.set(p.traveler_id, p.name);
    return out;
  }, [rooming]);

  async function call<T>(method: "POST" | "PUT" | "PATCH" | "DELETE", path: string, body?: unknown, ok?: string): Promise<T | null> {
    setBusy(true);
    const res = await bffSend<T>(method, path, body);
    setBusy(false);
    if (!res.ok) {
      if (!res.redirecting) setNotice({ tone: "danger", text: tr(travelErrorKey(res.detail)) });
      return null;
    }
    setNotice(ok ? { tone: "success", text: tr(ok) } : null);
    return res.data;
  }

  const disabled = busy || !hydrated || closed;
  return (
    <Panel title={tr("travel.title")} testId="trip-travel">
      <div style={{ display: "grid", gap: 16 }}>
        {notice && (
          <p role="status" data-testid="travel-notice" style={{ margin: 0, fontSize: 13, color: notice.tone === "danger" ? K.danger : K.success }}>
            {notice.text}
          </p>
        )}
        <PassportSection check={passports} names={names} tr={tr} locale={locale} />
        {rooming && (
          <RoomingSection
            view={rooming}
            tr={tr}
            disabled={disabled}
            onSave={async (rooms) => {
              const out = await call<RoomingView>("PUT", `trips/${tripId}/rooming`, rooms, "travel.rooming.saved");
              if (out) setRooming(out);
            }}
            onError={(key) => setNotice({ tone: "danger", text: tr(key) })}
          />
        )}
        {vouchers && (
          <VoucherSection
            view={vouchers}
            tr={tr}
            locale={locale}
            disabled={disabled}
            onSave={async (lineId, body) => {
              if (await call("PUT", `lines/${lineId}/voucher`, body, "travel.voucher.saved")) reload();
            }}
            onPdf={(lineId) => void call("POST", `trips/${tripId}/pdfs`, { kind: "voucher", booking_line_id: lineId }, "travel.pdf.queued")}
          />
        )}
        {visas && (
          <VisaSection
            tripId={tripId}
            view={visas}
            names={names}
            tr={tr}
            locale={locale}
            disabled={disabled}
            call={call}
            reload={reload}
          />
        )}
        <div style={{ display: "grid", gap: 6 }} data-testid="travel-pdfs">
          <h3 style={h3}>{tr("travel.pdf.title")}</h3>
          <p style={muted}>{tr("travel.pdf.intro")}</p>
          <div style={row}>
            <span data-testid="travel-pdf-confirmation">
              <Button size="sm" variant="secondary" disabled={disabled} onClick={() => void call("POST", `trips/${tripId}/pdfs`, { kind: "confirmation" }, "travel.pdf.queued")}>
                {tr("travel.pdf.confirmation")}
              </Button>
            </span>
            {pdfSubjects.invoices.map((i) => (
              <Button key={i.id} size="sm" variant="ghost" disabled={disabled} onClick={() => void call("POST", `trips/${tripId}/pdfs`, { kind: "invoice", invoice_id: i.id }, "travel.pdf.queued")}>
                {fmt(tr("travel.pdf.invoice"), { ref: i.label })}
              </Button>
            ))}
            {pdfSubjects.proposals.map((x) => (
              <Button key={x.id} size="sm" variant="ghost" disabled={disabled} onClick={() => void call("POST", `trips/${tripId}/pdfs`, { kind: "proposal", proposal_id: x.id }, "travel.pdf.queued")}>
                {fmt(tr("travel.pdf.proposal"), { ref: x.label })}
              </Button>
            ))}
          </div>
        </div>
      </div>
    </Panel>
  );
}

function PassportSection({ check, names, tr, locale }: { check: PassportCheck | null; names: Map<string, string>; tr: (k: string) => string; locale: DateLocale }) {
  if (!check) return null;
  return (
    <div data-testid="travel-passports" style={{ display: "grid", gap: 6 }}>
      <h3 style={h3}>{tr("travel.passport.title")}</h3>
      <p style={muted}>
        {check.valid_until_needed
          ? fmt(tr("travel.passport.rule"), { date: formatDate(check.valid_until_needed, locale, { year: true }), months: String(check.valid_months) })
          : tr("travel.passport.noEnd")}
      </p>
      <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 4 }}>
        {check.travellers.map((p) => (
          <li key={p.traveler_id} data-testid="travel-passport-row" data-status={p.status} style={row}>
            <span style={{ fontSize: 13, minWidth: 140, overflowWrap: "anywhere" }}>{names.get(p.traveler_id) ?? "—"}</span>
            <Badge tone={passportTone(p.status)}>{tr(`travel.passport.status.${p.status}`)}</Badge>
          </li>
        ))}
      </ul>
    </div>
  );
}

interface DraftRoom {
  room_no: number;
  room_type: RoomType;
}

function RoomingSection({
  view,
  tr,
  disabled,
  onSave,
  onError,
}: {
  view: RoomingView;
  tr: (k: string) => string;
  disabled: boolean;
  onSave: (body: { rooms: unknown[] }) => Promise<void>;
  onError: (key: string) => void;
}) {
  const people = useMemo(
    () => [...view.rooms.flatMap((r) => r.travellers), ...view.unassigned.map((p) => ({ ...p, bed_pref: null }))],
    [view],
  );
  const [rooms, setRooms] = useState<DraftRoom[]>(() => view.rooms.map((r) => ({ room_no: r.room_no, room_type: r.room_type })));
  const [placed, setPlaced] = useState<Record<string, { room: number | null; bed: BedPref | null }>>(() => {
    const out: Record<string, { room: number | null; bed: BedPref | null }> = {};
    for (const p of people) out[p.traveler_id] = { room: null, bed: null };
    for (const r of view.rooms) for (const p of r.travellers) out[p.traveler_id] = { room: r.room_no, bed: p.bed_pref ?? null };
    return out;
  });
  const nextNo = (rooms.reduce((m, r) => Math.max(m, r.room_no), 0) || 0) + 1;

  function save() {
    const body = roomingBody(
      rooms.map((r) => ({
        ...r,
        travellers: people.filter((p) => placed[p.traveler_id]?.room === r.room_no).map((p) => ({ traveler_id: p.traveler_id, bed_pref: placed[p.traveler_id]?.bed ?? null })),
      })),
    );
    if (!body.ok) return onError(`travel.err.${body.error}`);
    void onSave(body.body);
  }

  return (
    <div data-testid="travel-rooming" style={{ display: "grid", gap: 8 }}>
      <h3 style={h3}>{tr("travel.rooming.title")}</h3>
      {view.room_config && (
        <p style={muted}>
          {fmt(tr("travel.rooming.config"), {
            rooms: Object.entries(view.room_config).map(([k, n]) => `${n} × ${tr(`travel.room.${k}`)}`).join(", "),
          })}
        </p>
      )}
      {rooms.map((r, i) => (
        <div key={i} data-testid="travel-room" style={row}>
          <span style={{ fontSize: 13, minWidth: 64 }}>{fmt(tr("travel.rooming.room"), { n: String(r.room_no) })}</span>
          <span style={{ minWidth: 150 }}>
            <Select
              size="sm"
              aria-label={tr("travel.rooming.type")}
              value={r.room_type}
              disabled={disabled}
              onChange={(e) => setRooms(rooms.map((x, j) => (j === i ? { ...x, room_type: e.target.value as RoomType } : x)))}
              options={ROOM_TYPES.map((k) => ({ value: k, label: `${tr(`travel.room.${k}`)} (${CAPACITY[k]})` }))}
            />
          </span>
          <Button size="sm" variant="ghost" disabled={disabled} onClick={() => setRooms(rooms.filter((_, j) => j !== i))}>
            {tr("travel.rooming.remove")}
          </Button>
        </div>
      ))}
      <div style={row}>
        <Button size="sm" variant="ghost" disabled={disabled || rooms.length >= 50} onClick={() => setRooms([...rooms, { room_no: nextNo, room_type: "twin" }])}>
          {tr("travel.rooming.add")}
        </Button>
      </div>
      {people.length === 0 ? (
        <p style={muted}>{tr("travel.rooming.noParty")}</p>
      ) : (
        <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 6 }}>
          {people.map((p) => (
            <li key={p.traveler_id} data-testid="travel-room-person" style={row}>
              <span style={{ fontSize: 13, minWidth: 140, overflowWrap: "anywhere" }}>{p.name}</span>
              <span style={{ minWidth: 130 }}>
                <Select
                  size="sm"
                  aria-label={fmt(tr("travel.rooming.pick"), { name: p.name })}
                  value={placed[p.traveler_id]?.room == null ? "" : String(placed[p.traveler_id]?.room)}
                  disabled={disabled}
                  onChange={(e) => setPlaced({ ...placed, [p.traveler_id]: { room: e.target.value ? Number(e.target.value) : null, bed: placed[p.traveler_id]?.bed ?? null } })}
                  options={[{ value: "", label: tr("travel.rooming.none") }, ...rooms.map((r) => ({ value: String(r.room_no), label: fmt(tr("travel.rooming.room"), { n: String(r.room_no) }) }))]}
                />
              </span>
              <span style={{ minWidth: 130 }}>
                <Select
                  size="sm"
                  aria-label={fmt(tr("travel.rooming.bed"), { name: p.name })}
                  value={placed[p.traveler_id]?.bed ?? ""}
                  disabled={disabled}
                  onChange={(e) => setPlaced({ ...placed, [p.traveler_id]: { room: placed[p.traveler_id]?.room ?? null, bed: (e.target.value || null) as BedPref | null } })}
                  options={[{ value: "", label: tr("travel.bed.any") }, ...BED_PREFS.map((b) => ({ value: b, label: tr(`travel.bed.${b}`) }))]}
                />
              </span>
            </li>
          ))}
        </ul>
      )}
      <div style={row}>
        <span data-testid="travel-rooming-save">
          <Button size="sm" variant="primary" disabled={disabled} onClick={save}>
            {tr("travel.rooming.save")}
          </Button>
        </span>
      </div>
    </div>
  );
}

function VoucherSection({
  view,
  tr,
  locale,
  disabled,
  onSave,
  onPdf,
}: {
  view: VouchersView;
  tr: (k: string) => string;
  locale: DateLocale;
  disabled: boolean;
  onSave: (lineId: string, body: Record<string, string | null>) => Promise<void>;
  onPdf: (lineId: string) => void;
}) {
  const [open, setOpen] = useState<string | null>(null);
  return (
    <div data-testid="travel-vouchers" style={{ display: "grid", gap: 8 }}>
      <h3 style={h3}>{tr("travel.voucher.title")}</h3>
      {view.vouchers.length === 0 && <p style={muted}>{tr("travel.voucher.empty")}</p>}
      {view.vouchers.map((v) => (
        <div key={v.booking_line_id} data-testid="travel-voucher" data-released={v.released} style={{ display: "grid", gap: 6 }}>
          <div style={row}>
            <strong style={{ fontSize: 13, overflowWrap: "anywhere" }}>{v.title}</strong>
            <span style={{ fontSize: 12, color: K.muted }}>
              {[v.service_from && formatDate(v.service_from, locale), v.service_time].filter(Boolean).join(" · ")}
            </span>
            <Badge tone={v.released ? "success" : "neutral"}>{v.released ? tr("travel.voucher.released") : tr(`travel.voucher.reason.${v.not_released_reason}`)}</Badge>
            <Button size="sm" variant="ghost" disabled={disabled} onClick={() => setOpen(open === v.booking_line_id ? null : v.booking_line_id)}>
              {tr("travel.voucher.edit")}
            </Button>
            <Button size="sm" variant="ghost" disabled={disabled} onClick={() => onPdf(v.booking_line_id)}>
              {tr("travel.pdf.make")}
            </Button>
          </div>
          {open === v.booking_line_id && <VoucherForm voucher={v} tr={tr} disabled={disabled} onSave={(body) => onSave(v.booking_line_id, body).then(() => setOpen(null))} />}
        </div>
      ))}
    </div>
  );
}

function VoucherForm({ voucher, tr, disabled, onSave }: { voucher: Voucher; tr: (k: string) => string; disabled: boolean; onSave: (body: Record<string, string | null>) => Promise<void> }) {
  const [f, setF] = useState({
    pickup: voucher.pickup ?? "",
    dropoff: voucher.dropoff ?? "",
    service_time: voucher.service_time ?? "",
    pnr: voucher.pnr ?? "",
    note: voucher.note ?? "",
  });
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement>) => setF({ ...f, [k]: e.target.value });
  return (
    <div data-testid="travel-voucher-form" style={{ display: "grid", gap: 8, gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))" }}>
      <Field label={tr("travel.voucher.pickup")}>
        <Input size="sm" maxLength={200} value={f.pickup} onChange={set("pickup")} />
      </Field>
      <Field label={tr("travel.voucher.dropoff")}>
        <Input size="sm" maxLength={200} value={f.dropoff} onChange={set("dropoff")} />
      </Field>
      <Field label={tr("travel.voucher.time")}>
        <Input size="sm" type="time" value={f.service_time} onChange={set("service_time")} />
      </Field>
      <Field label={tr("travel.voucher.pnr")}>
        <Input size="sm" maxLength={80} value={f.pnr} onChange={set("pnr")} />
      </Field>
      <Field label={tr("travel.voucher.note")}>
        <Input size="sm" maxLength={500} value={f.note} onChange={set("note")} />
      </Field>
      <div style={{ alignSelf: "end" }}>
        <Button
          size="sm"
          variant="primary"
          disabled={disabled}
          onClick={() => void onSave(Object.fromEntries(Object.entries(f).map(([k, v]) => [k, v.trim() ? v.trim() : null])))}
        >
          {tr("travel.voucher.save")}
        </Button>
      </div>
    </div>
  );
}

function VisaSection({
  tripId,
  view,
  names,
  tr,
  locale,
  disabled,
  call,
  reload,
}: {
  tripId: string;
  view: VisasView;
  names: Map<string, string>;
  tr: (k: string) => string;
  locale: DateLocale;
  disabled: boolean;
  call: <T>(method: "POST" | "PUT" | "PATCH" | "DELETE", path: string, body?: unknown, ok?: string) => Promise<T | null>;
  reload: () => void;
}) {
  const [suggestions, setSuggestions] = useState<VisaSuggestion[] | null>(null);
  const today = new Date().toISOString().slice(0, 10);

  async function suggest() {
    const res = await bffSend<{ suggestions: VisaSuggestion[] }>("GET", `trips/${tripId}/visas/suggestions`);
    if (res.ok) setSuggestions(res.data.suggestions);
  }

  async function add(s: VisaSuggestion) {
    const body = { traveler_id: s.traveler_id, country: s.country, ...(s.template ? { visa_template_id: s.template.visa_template_id } : { visa_type: "other" }) };
    if (await call("POST", `trips/${tripId}/visas`, body, "travel.visa.added")) {
      setSuggestions((cur) => (cur ?? []).filter((x) => !(x.traveler_id === s.traveler_id && x.country === s.country)));
      reload();
    }
  }

  return (
    <div data-testid="travel-visas" style={{ display: "grid", gap: 8 }}>
      <h3 style={h3}>{tr("travel.visa.title")}</h3>
      <p style={muted}>{tr("travel.visa.byo")}</p>
      {view.applications.length === 0 && <p style={muted}>{tr("travel.visa.empty")}</p>}
      {view.applications.map((a) => (
        <VisaRow key={a.visa_application_id} app={a} name={a.traveler_name ?? names.get(a.traveler_id) ?? "—"} tr={tr} locale={locale} today={today} disabled={disabled} call={call} reload={reload} />
      ))}
      {view.can_add ? (
        <div style={{ display: "grid", gap: 6 }}>
          <div style={row}>
            <span data-testid="travel-visa-suggest">
              <Button size="sm" variant="secondary" disabled={disabled} onClick={() => void suggest()}>
                {tr("travel.visa.suggest")}
              </Button>
            </span>
          </div>
          {suggestions && suggestions.length === 0 && <p style={muted}>{tr("travel.visa.noSuggestions")}</p>}
          {suggestions?.map((s) => (
            <div key={`${s.traveler_id}-${s.country}`} data-testid="travel-visa-suggestion" style={row}>
              <span style={{ fontSize: 13, overflowWrap: "anywhere" }}>
                {fmt(tr("travel.visa.suggestion"), { name: s.traveler_name, country: s.country })}
              </span>
              {!s.nationality_known && <Badge tone="warning">{tr("travel.visa.nationalityUnknown")}</Badge>}
              <Button size="sm" variant="ghost" disabled={disabled} onClick={() => void add(s)}>
                {s.template ? fmt(tr("travel.visa.addWith"), { type: tr(`travel.visaType.${s.template.visa_type}`) }) : tr("travel.visa.add")}
              </Button>
            </div>
          ))}
        </div>
      ) : (
        <p style={muted}>{tr("travel.visa.bookedOnly")}</p>
      )}
    </div>
  );
}

function VisaRow({
  app,
  name,
  tr,
  locale,
  today,
  disabled,
  call,
  reload,
}: {
  app: VisaApplication;
  name: string;
  tr: (k: string) => string;
  locale: DateLocale;
  today: string;
  disabled: boolean;
  call: <T>(method: "POST" | "PUT" | "PATCH" | "DELETE", path: string, body?: unknown, ok?: string) => Promise<T | null>;
  reload: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [label, setLabel] = useState("");
  const [visaNo, setVisaNo] = useState("");
  const [until, setUntil] = useState(app.valid_until ?? "");
  const moves = VISA_MOVES[app.status];
  const done = async (p: Promise<unknown>) => {
    if (await p) reload();
  };
  return (
    <div data-testid="travel-visa" data-status={app.status} style={{ display: "grid", gap: 6, borderTop: `1px solid ${K.line}`, paddingTop: 6 }}>
      <div style={row}>
        <strong style={{ fontSize: 13, overflowWrap: "anywhere" }}>{name}</strong>
        <span style={{ fontSize: 12.5 }}>
          {app.country} · {tr(`travel.visaType.${app.visa_type}`)}
        </span>
        <Badge tone={visaTone(app.status)}>{tr(`travel.visaStatus.${app.status}`)}</Badge>
        {app.deadline && (
          <span style={{ fontSize: 12, color: visaOverdue(app, today) ? K.danger : K.muted }}>
            {fmt(tr(visaOverdue(app, today) ? "travel.visa.overdue" : "travel.visa.deadline"), { date: formatDate(app.deadline, locale) })}
          </span>
        )}
        <span style={{ fontSize: 12, color: K.muted }}>{fmt(tr("travel.visa.checklistCount"), { done: String(app.checklist_done), all: String(app.checklist.length) })}</span>
        {app.visa_no && <span style={{ fontSize: 12, color: K.muted }}>{app.visa_no}</span>}
        <Button size="sm" variant="ghost" onClick={() => setOpen(!open)}>
          {tr(open ? "travel.visa.close" : "travel.visa.open")}
        </Button>
      </div>
      {open && (
        <div style={{ display: "grid", gap: 8, paddingLeft: 8 }}>
          {moves.length > 0 && (
            <div style={row}>
              <span style={{ fontSize: 12.5, color: K.muted }}>{tr("travel.visa.move")}</span>
              {moves.map((m: VisaStatus) => (
                <Button key={m} size="sm" variant="ghost" disabled={disabled} onClick={() => void done(call("PATCH", `visas/${app.visa_application_id}`, { status: m }, "travel.visa.saved"))}>
                  {tr(`travel.visaStatus.${m}`)}
                </Button>
              ))}
            </div>
          )}
          {(app.status === "submitted" || app.status === "approved") && (
            <div style={{ ...row, alignItems: "end" }} data-testid="travel-visa-grant">
              <Field label={tr("travel.visa.number")}>
                <Input size="sm" maxLength={40} value={visaNo} onChange={(e) => setVisaNo(e.target.value)} />
              </Field>
              <Field label={tr("travel.visa.validUntil")}>
                <Input size="sm" type="date" value={until} onChange={(e) => setUntil(e.target.value)} />
              </Field>
              <Button
                size="sm"
                variant="primary"
                disabled={disabled}
                onClick={() =>
                  void done(
                    call("PATCH", `visas/${app.visa_application_id}`, { status: "approved", ...(visaNo.trim() ? { visa_no: visaNo.trim() } : {}), ...(until ? { valid_until: until } : {}) }, "travel.visa.saved"),
                  )
                }
              >
                {tr("travel.visa.grant")}
              </Button>
            </div>
          )}
          <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 4 }}>
            {app.checklist.map((i) => (
              <li key={i.visa_checklist_item_id} data-testid="travel-visa-item" style={row}>
                <Checkbox
                  checked={i.done}
                  disabled={disabled}
                  label={i.label}
                  onChange={(e) => void done(call("PATCH", `visa-checklist/${i.visa_checklist_item_id}`, { done: e.target.checked }))}
                />
                {i.done_by_kind === "traveler" && <span style={{ fontSize: 11.5, color: K.muted }}>{tr("travel.visa.byTraveller")}</span>}
              </li>
            ))}
          </ul>
          <div style={{ ...row, alignItems: "end" }}>
            <Field label={tr("travel.visa.addItem")}>
              <Input size="sm" maxLength={200} value={label} onChange={(e) => setLabel(e.target.value)} />
            </Field>
            <Button
              size="sm"
              variant="ghost"
              disabled={disabled || !label.trim()}
              onClick={() =>
                void done(call("POST", `visas/${app.visa_application_id}/checklist`, { label: label.trim() })).then(() => setLabel(""))
              }
            >
              {tr("travel.visa.addItemButton")}
            </Button>
            <Button size="sm" variant="ghost" disabled={disabled} onClick={() => void done(call("DELETE", `visas/${app.visa_application_id}`, undefined, "travel.visa.deleted"))}>
              {tr("travel.visa.delete")}
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
