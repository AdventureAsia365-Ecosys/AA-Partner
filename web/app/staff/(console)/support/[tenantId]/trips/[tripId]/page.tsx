// web/app/staff/(console)/support/[tenantId]/trips/[tripId]/page.tsx — S-04, one trip inside the caller's live session
// (AA-1019 on GET /crm/staff/v1/agencies/{id}/trips/{trip}): stage, dates, party size, destinations, the stage history;
// the travellers' display names only when the owner allowed "show identity" for a support session (never emergency
// access), and that reveal is its own line in the agency's access log. A navigation from another origin gets the
// OpenStep first (review finding B1).
import Link from "next/link";
import { notFound } from "next/navigation";
import { ErrorState, K } from "@/app/_kit";
import { Panel, PageTitle } from "@/components/PageTitle";
import { OpenStep } from "@/components/staff/OpenStep";
import { SessionBanner } from "@/components/staff/SessionBanner";
import { codeLabel } from "@/lib/accessLog";
import { formatDate, formatInstant } from "@/lib/dates";
import { fmt, t } from "@/lib/i18n";
import { staffCall } from "@/lib/staffApi";
import { liveSession, refusalKey, uuidParam, type MySessions, type TripDetail } from "@/lib/staffConsole";
import { staffContext } from "@/lib/staffContext";

export const dynamic = "force-dynamic";

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(110px, 160px) minmax(0, 1fr)", gap: 8, fontSize: 13 }}>
      <span style={{ color: K.muted }}>{label}</span>
      <span style={{ color: K.body, overflowWrap: "anywhere" }}>{children}</span>
    </div>
  );
}

export default async function StaffTripPage({ params }: { params: Promise<{ tenantId: string; tripId: string }> }) {
  const raw = await params;
  const tenantId = uuidParam(raw.tenantId);
  const tripId = uuidParam(raw.tripId);
  if (!tenantId || !tripId) notFound();
  const { cfg, session, lang, locale, foreignNavigation } = await staffContext();
  if (!session) return <ErrorState message={t(lang, "staff.noAccess")} />;
  const back = (
    <Link href={`/staff/support/${tenantId}`} style={{ fontSize: 13 }}>
      ← {t(lang, "staff.back")}
    </Link>
  );
  if (foreignNavigation) return <OpenStep href={`/staff/support/${tenantId}/trips/${tripId}`} back={back} lang={lang} />;
  const mine = await staffCall<MySessions>(cfg, session, "GET", `agencies/${tenantId}/session`);
  const live = mine.ok ? liveSession(mine.data.sessions) : null;
  const res = await staffCall<TripDetail>(cfg, session, "GET", `agencies/${tenantId}/trips/${tripId}`);
  if (!res.ok) {
    if (res.status === 404 || res.status === 422) notFound();
    return (
      <div style={{ display: "grid", gap: 12 }} data-testid="staff-agency-refused">
        {back}
        <ErrorState message={t(lang, res.status === 0 || res.status >= 500 ? "staff.error.api" : refusalKey(res.detail))} />
      </div>
    );
  }
  const trip = res.data;
  const day = (iso: string | null) => (iso ? formatDate(iso.slice(0, 10), locale) : "—");
  return (
    <div style={{ display: "grid", gap: 16, gridTemplateColumns: "minmax(0, 1fr)" }} data-testid="staff-trip">
      {back}
      {live && mine.ok && <SessionBanner session={live} agencyName={mine.data.agency_name ?? ""} lang={lang} locale={locale} />}
      <PageTitle title={fmt(t(lang, "staff.trip.title"), { ref: trip.trip_id.slice(0, 8) })} subtitle={t(lang, "staff.audited")} />
      <Panel>
        <div style={{ display: "grid", gap: 6 }}>
          <Row label={t(lang, "staff.trip.stage")}>{codeLabel(lang, "stage", trip.stage)}</Row>
          <Row label={t(lang, "staff.trip.waiting")}>{codeLabel(lang, "waiting", trip.waiting_on)}</Row>
          <Row label={t(lang, "staff.trip.dates")}>{trip.start_date ? `${day(trip.start_date)} – ${day(trip.end_date)}` : trip.month ?? "—"}</Row>
          <Row label={t(lang, "staff.trip.party")}>
            {fmt(t(lang, "staff.release.party.value"), { adults: trip.party_adults, children: trip.party_children })}
          </Row>
          <Row label={t(lang, "staff.trip.destinations")}>{trip.destinations.join(", ") || "—"}</Row>
          <Row label={t(lang, "staff.trip.activity")}>{day(trip.last_activity_at)}</Row>
        </div>
      </Panel>
      <Panel title={t(lang, "staff.trip.people")}>
        {trip.party === null || trip.identity_hidden ? (
          <p style={{ margin: 0, fontSize: 13, color: K.muted }} data-testid="staff-trip-identity-hidden">
            {t(lang, "staff.trip.identityHidden")}
          </p>
        ) : (
          <ul style={{ margin: 0, paddingLeft: 18, display: "grid", gap: 2, fontSize: 13 }}>
            {trip.party.map((p) => (
              <li key={p.traveler_id}>
                {p.display_name} · {codeLabel(lang, "staff.release.role", p.role)}
              </li>
            ))}
          </ul>
        )}
      </Panel>
      <Panel title={t(lang, "staff.trip.history")}>
        <ul style={{ margin: 0, paddingLeft: 18, display: "grid", gap: 2, fontSize: 12.5, color: K.body }}>
          {trip.stage_history.map((h) => (
            <li key={h.history_id}>
              {h.at ? formatInstant(h.at, locale, "UTC") : "—"} · {h.from_stage ? `${codeLabel(lang, "stage", h.from_stage)} → ` : ""}
              {codeLabel(lang, "stage", h.to_stage)} · {h.reason_code.replace(/_/g, " ")}
            </li>
          ))}
        </ul>
      </Panel>
    </div>
  );
}
