"use client";
// web/components/portal/PortalPerson.tsx — the per-person parts of the traveller portal (AA-953, card CRM-24a; design
// v3 C-06 / C-07 / C-09, R22, R33, §2.9 rules 2-3): the trip as travellers see it (the accepted days, the lines the
// agency shows: never a price, never a cancelled line), this traveller's flights in airport local time, the
// upload-only passport scan, the documents shared with this traveller (or the whole group), and for the lead the
// group with each person's way in plus the "companion e-mails asked at the invite" form. Everything goes to the API's
// /crm/public/v1/traveller routes through the page's `step`; nothing here keeps a Stage 3 or passport value.
import { useState } from "react";
import { Badge, Button, K, LinkButton } from "@/app/_kit";
import {
  COMMON_ZONES,
  EMPTY_FLIGHT,
  fileSize,
  flightBody,
  flightDayKey,
  flightForm,
  localClock,
  scanProblem,
  toBase64,
  type FlightDirection,
  type FlightForm,
  type PortalDay,
  type PortalDocument,
  type PortalFlight,
  type PortalPartyMember,
  type PortalService,
} from "@/lib/clientPortal";
import { formatDate, type DateLocale } from "@/lib/dates";
import { fmt } from "@/lib/i18n";

export type Step = (method: string, path: string, body: unknown, done: (data: Record<string, unknown>) => void) => Promise<void>;
type T = (key: string) => string;

const box: React.CSSProperties = {
  font: "inherit",
  fontSize: 14,
  padding: "8px 10px",
  borderRadius: 8,
  border: `1px solid ${K.line}`,
  background: K.card,
  color: K.ink,
  width: "100%",
  boxSizing: "border-box",
};
const section: React.CSSProperties = { display: "grid", gap: 8, paddingTop: 12, borderTop: `1px solid ${K.line}` };
const line: React.CSSProperties = { margin: 0, fontSize: 13.5, color: K.body, overflowWrap: "anywhere" };
const h2: React.CSSProperties = { margin: 0, fontSize: 16, color: K.ink };

export function TripSection({ days, services, t, locale }: { days: PortalDay[]; services: PortalService[]; t: T; locale: DateLocale }) {
  if (!days.length && !services.length) return null;
  return (
    <section data-testid="portal-trip" style={section}>
      <h2 style={h2}>{t("client.tripTitle")}</h2>
      {days.map((d) => (
        <p key={d.day_no} style={line} data-testid={`portal-day-${d.day_no}`}>
          <strong style={{ color: K.ink }}>{d.before_you_go ? t("client.beforeYouGo") : fmt(t("client.dayN"), { n: d.day_no })}</strong>
          {d.date ? ` · ${formatDate(d.date, locale)}` : ""}
          {d.title ? ` · ${d.title}` : ""}
          {d.place ? ` · ${d.place}` : ""}
          {!d.before_you_go && <span style={{ color: K.muted }}>{` (UTC${d.utc_offset})`}</span>}
        </p>
      ))}
      {services.length > 0 && (
        <div style={{ display: "grid", gap: 6 }} data-testid="portal-services">
          <h3 style={{ margin: 0, fontSize: 14.5, color: K.ink }}>{t("client.servicesTitle")}</h3>
          {services.map((s, i) => (
            <p key={i} style={line}>
              {s.title}
              {s.from ? ` · ${formatDate(s.from, locale)}${s.to && s.to !== s.from ? ` – ${formatDate(s.to, locale)}` : ""}` : ""}{" "}
              <Badge tone={s.status === "confirmed" ? "success" : "neutral"}>{t(`client.service.${s.status}`)}</Badge>
              {s.pay_on_arrival && <span style={{ color: K.muted }}>{` · ${t("client.payOnArrival")}`}</span>}
            </p>
          ))}
        </div>
      )}
    </section>
  );
}

function FlightEditor({
  direction,
  saved,
  locked,
  busy,
  step,
  t,
  onSaved,
}: {
  direction: FlightDirection;
  saved: PortalFlight | undefined;
  locked: boolean;
  busy: boolean;
  step: Step;
  t: T;
  onSaved: (text: string) => void;
}) {
  const [form, setForm] = useState<FlightForm>(() => flightForm(saved));
  const [bad, setBad] = useState<string[]>([]);
  const set = (k: keyof FlightForm) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setForm((f) => ({ ...f, [k]: e.target.value }));
  const zones = form.tz && !(COMMON_ZONES as readonly string[]).includes(form.tz) ? [form.tz, ...COMMON_ZONES] : COMMON_ZONES;
  if (locked) return null;
  return (
    <form
      data-testid={`portal-flight-${direction}`}
      onSubmit={(e) => {
        e.preventDefault();
        const out = flightBody(direction, form);
        if ("fields" in out) {
          setBad(out.fields);
          return;
        }
        setBad([]);
        void step("PUT", "/flights", { flights: [out.body] }, () => onSaved(t("client.flightSaved")));
      }}
      style={{ display: "grid", gap: 6, padding: "8px 10px", border: `1px solid ${K.line}`, borderRadius: 8 }}
    >
      <h3 style={{ margin: 0, fontSize: 14.5, color: K.ink }}>{t(`client.flight.${direction}`)}</h3>
      <div style={{ display: "grid", gap: 6, gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))" }}>
        <label style={{ display: "grid", gap: 4, fontSize: 13.5 }}>
          {t("client.flightNo")}
          <input value={form.flight_no} onChange={set("flight_no")} maxLength={10} autoCapitalize="characters" style={box} aria-invalid={bad.includes("flight_no")} data-testid={`portal-flight-${direction}-no`} />
        </label>
        <label style={{ display: "grid", gap: 4, fontSize: 13.5 }}>
          {t("client.airport")}
          <input value={form.airport} onChange={set("airport")} maxLength={3} autoCapitalize="characters" style={box} aria-invalid={bad.includes("airport")} data-testid={`portal-flight-${direction}-airport`} />
        </label>
        <label style={{ display: "grid", gap: 4, fontSize: 13.5 }}>
          {t("client.localTime")}
          <input type="datetime-local" value={form.local_time} onChange={set("local_time")} style={box} aria-invalid={bad.includes("local_time")} data-testid={`portal-flight-${direction}-time`} />
        </label>
        <label style={{ display: "grid", gap: 4, fontSize: 13.5 }}>
          {t("client.timeZone")}
          <select value={form.tz} onChange={set("tz")} style={box} aria-invalid={bad.includes("tz")} data-testid={`portal-flight-${direction}-tz`}>
            <option value="">—</option>
            {zones.map((z) => (
              <option key={z} value={z}>
                {z.replace(/_/g, " ")}
              </option>
            ))}
          </select>
        </label>
      </div>
      {bad.length > 0 && (
        <p role="alert" style={{ margin: 0, fontSize: 13, color: K.danger }}>
          {t("client.error.invalid_flight")}
        </p>
      )}
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        <Button type="submit" variant="primary" disabled={busy} testId={`portal-flight-${direction}-save`}>
          {t("client.saveFlight")}
        </Button>
        {saved && (
          <Button
            variant="ghost"
            disabled={busy}
            testId={`portal-flight-${direction}-remove`}
            onClick={() =>
              step("PUT", "/flights", { flights: [{ direction, remove: true }] }, () => {
                setForm({ ...EMPTY_FLIGHT });
                onSaved(t("client.flightRemoved"));
              })
            }
          >
            {t("client.removeFlight")}
          </Button>
        )}
      </div>
    </form>
  );
}

export function FlightsSection({
  flights,
  locked,
  busy,
  step,
  t,
  locale,
  onSaved,
}: {
  flights: PortalFlight[];
  locked: boolean;
  busy: boolean;
  step: Step;
  t: T;
  locale: DateLocale;
  onSaved: (text: string) => void;
}) {
  const by = (d: FlightDirection) => flights.find((f) => f.direction === d);
  return (
    <section data-testid="portal-flights" style={section}>
      <h2 style={h2}>{t("client.flightsTitle")}</h2>
      <p style={{ ...line, color: K.muted }}>{t("client.flightsIntro")}</p>
      {flights.map((f) => {
        const day = flightDayKey(f);
        return (
          <p key={f.direction} style={line} data-testid={`portal-flight-${f.direction}-saved`}>
            <strong style={{ color: K.ink }}>{t(`client.flight.${f.direction}`)}</strong>
            {` · ${f.flight_no} · ${f.airport} · ${formatDate(f.local_date, locale)} ${localClock(f.local_time)} (UTC${f.utc_offset})`}
            {day && <span style={{ color: K.muted }}>{` · ${fmt(t(day.key), { n: day.n })}`}</span>}
          </p>
        );
      })}
      {locked ? (
        <p style={line}>{t("client.stage3Locked")}</p>
      ) : (
        (["arrival", "departure"] as const).map((d) => (
          <FlightEditor key={`${d}-${by(d)?.local_time ?? "new"}`} direction={d} saved={by(d)} locked={locked} busy={busy} step={step} t={t} onSaved={onSaved} />
        ))
      )}
    </section>
  );
}

export function PassportScanSection({
  onFile,
  locked,
  busy,
  step,
  t,
  onSaved,
}: {
  onFile: boolean;
  locked: boolean;
  busy: boolean;
  step: Step;
  t: T;
  onSaved: (text: string) => void;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  return (
    <section data-testid="portal-scan" data-on-file={onFile ? "true" : "false"} style={section}>
      <h2 style={h2}>{t("client.scanTitle")}</h2>
      <p style={{ ...line, color: K.muted }}>{t("client.scanIntro")}</p>
      {onFile && (
        <p style={{ ...line, color: K.success }} data-testid="portal-scan-on-file">
          {t("client.scanOnFile")}
        </p>
      )}
      {locked ? (
        <p style={line}>{t("client.stage3Locked")}</p>
      ) : (
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            if (!file) return;
            const why = scanProblem(file);
            if (why) {
              setProblem(t(why === "type" ? "client.scanType" : "client.scanSize"));
              return;
            }
            setProblem(null);
            const body = { filename: file.name, content_type: file.type, content_b64: toBase64(await file.arrayBuffer()) };
            await step("POST", "/passport-scan", body, () => {
              setFile(null);
              onSaved(t("client.scanSaved"));
            });
          }}
          style={{ display: "grid", gap: 8 }}
        >
          <label style={{ display: "grid", gap: 4, fontSize: 14 }}>
            {t("client.scanChoose")}
            <input type="file" accept="image/jpeg,image/png,image/webp,image/heic,application/pdf" onChange={(e) => setFile(e.target.files?.[0] ?? null)} data-testid="portal-scan-file" />
          </label>
          {problem && (
            <p role="alert" style={{ margin: 0, fontSize: 13, color: K.danger }}>
              {problem}
            </p>
          )}
          <Button type="submit" variant="secondary" disabled={busy || !file} testId="portal-scan-upload">
            {t("client.scanUpload")}
          </Button>
        </form>
      )}
    </section>
  );
}

async function download(base: string, doc: PortalDocument): Promise<number> {
  const res = await fetch(`${base}/documents/${encodeURIComponent(doc.document_id)}`, { credentials: "same-origin", cache: "no-store" });
  if (!res.ok) return res.status;
  const data = (await res.json()) as { content_b64: string; content_type: string; filename: string };
  const bytes = Uint8Array.from(atob(data.content_b64), (c) => c.charCodeAt(0));
  const url = URL.createObjectURL(new Blob([bytes], { type: data.content_type }));
  const a = document.createElement("a");
  a.href = url;
  a.download = data.filename;
  a.rel = "noopener";
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
  return 200;
}

export function DocumentsSection({ base, documents, t, locale, onError }: { base: string; documents: PortalDocument[]; t: T; locale: DateLocale; onError: (status: number) => void }) {
  const [busy, setBusy] = useState<string | null>(null);
  return (
    <section data-testid="portal-documents" style={section}>
      <h2 style={h2}>{t("client.documentsTitle")}</h2>
      {documents.length === 0 ? (
        <p style={{ ...line, color: K.muted }}>{t("client.documentsEmpty")}</p>
      ) : (
        documents.map((d) => (
          <p key={d.document_id} style={line} data-testid="portal-document">
            {d.filename} <span style={{ color: K.muted }}>{`· ${t(`client.documentFor.${d.for}`)} · ${fileSize(d.size_bytes)} · ${formatDate(d.created_at.slice(0, 10), locale)}`}</span>{" "}
            <LinkButton
              size="inherit"
              disabled={busy !== null}
              testId={`portal-document-${d.document_id}`}
              onClick={async () => {
                setBusy(d.document_id);
                const status = await download(base, d);
                setBusy(null);
                if (status !== 200) onError(status);
              }}
            >
              {t("client.download")}
            </LinkButton>
          </p>
        ))
      )}
    </section>
  );
}

function reachKey(p: PortalPartyMember): string {
  if (p.me) return "client.reach.own";
  if (p.via_you) return "client.reach.viaYou";
  return p.has_email ? "client.reach.own" : "client.reach.none";
}

export function PartySection({
  party,
  busy,
  step,
  t,
  onInvited,
}: {
  party: PortalPartyMember[];
  busy: boolean;
  step: Step;
  t: T;
  onInvited: (text: string) => void;
}) {
  const [open, setOpen] = useState<string | null>(null);
  const [email, setEmail] = useState("");
  return (
    <section data-testid="portal-party" style={section}>
      <h2 style={h2}>{t("client.partyTitle")}</h2>
      {party.map((p, i) => {
        const name = p.name ?? t(`client.role.${p.role}`);
        const canInvite = !p.me && p.role === "companion" && p.traveler_id && (!p.has_email || !p.portal);
        return (
          <div key={p.traveler_id ?? i} style={{ display: "grid", gap: 6 }} data-testid="portal-party-member" data-reach={p.me ? "me" : p.via_you ? "via_you" : p.has_email ? "own" : "none"}>
            <p style={line}>
              <strong style={{ color: K.ink }}>{p.me ? `${name} (${t("client.you")})` : name}</strong>
              {` · ${t(`client.role.${p.role}`)}: ${t(p.details_done ? "client.detailsDone" : "client.detailsMissing")}`}
              {p.waiver_signed !== null && ` · ${t(p.waiver_signed ? "client.waiverDone" : "client.waiverMissing")}`}
              {p.has_email !== undefined && <span style={{ color: K.muted }}>{` · ${t(reachKey(p))}`}</span>}
              {p.portal && !p.me && <span style={{ color: K.muted }}>{` · ${t(`client.portal.${p.portal}`)}`}</span>}{" "}
              {canInvite && open !== p.traveler_id && (
                <LinkButton
                  size="inherit"
                  testId={`portal-invite-open-${i}`}
                  onClick={() => {
                    setOpen(p.traveler_id ?? null);
                    setEmail("");
                  }}
                >
                  {t(p.has_email ? "client.inviteCompanion" : "client.addEmail")}
                </LinkButton>
              )}
            </p>
            {canInvite && open === p.traveler_id && (
              <form
                data-testid="portal-invite-form"
                onSubmit={(e) => {
                  e.preventDefault();
                  void step("POST", "/companions", { traveler_id: p.traveler_id, email: email.trim() || null }, (d) => {
                    setOpen(null);
                    const link = typeof d.link_path === "string" ? `${window.location.origin}${d.link_path}` : "";
                    onInvited(fmt(t("client.inviteLink"), { name, link }));
                  });
                }}
                style={{ display: "grid", gap: 6, padding: "8px 10px", border: `1px solid ${K.line}`, borderRadius: 8 }}
              >
                {!p.has_email && (
                  <label style={{ display: "grid", gap: 4, fontSize: 13.5 }}>
                    {fmt(t("client.companionEmail"), { name })}
                    <input type="email" autoComplete="off" value={email} onChange={(e) => setEmail(e.target.value)} style={box} data-testid="portal-invite-email" />
                    <span style={{ fontSize: 12.5, color: K.muted }}>{t("client.companionEmailHelp")}</span>
                  </label>
                )}
                <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                  <Button type="submit" variant="primary" disabled={busy} testId="portal-invite-send">
                    {t(!p.has_email && !email.trim() ? "client.inviteViaMe" : "client.inviteCompanion")}
                  </Button>
                  <Button variant="ghost" disabled={busy} testId="portal-invite-cancel" onClick={() => setOpen(null)}>
                    {t("client.cancel")}
                  </Button>
                </div>
              </form>
            )}
          </div>
        );
      })}
    </section>
  );
}
