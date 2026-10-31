// web/app/staff/(console)/ops/[tenantId]/[tripId]/page.tsx — S-02, one release for AA ground operations (AA-1019; API
// GET /crm/staff/v1/agencies/{tenant}/trips/{trip}/ops-release, group crm-ops). Every render is one view: the API adds
// 1 to view_count and writes "AA ops viewed trip X" to the agency's access log (the board links here with prefetch off,
// so only a real open counts). 404 release_not_found = no confirmed AA line now, or withdrawn.
import Link from "next/link";
import { notFound } from "next/navigation";
import { Badge, ErrorState, K } from "@/app/_kit";
import { Panel, PageTitle } from "@/components/PageTitle";
import { ManifestDownload } from "@/components/staff/ManifestDownload";
import { ReleaseTravellers } from "@/components/staff/ReleaseTravellers";
import { formatDate } from "@/lib/dates";
import { fmt, t } from "@/lib/i18n";
import { staffCall } from "@/lib/staffApi";
import { missingItems, refusalKey, uuidParam, type Release } from "@/lib/staffConsole";
import { staffContext } from "@/lib/staffContext";

export const dynamic = "force-dynamic";

const mono = "ui-monospace, SFMono-Regular, Menlo, monospace";

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(110px, 160px) minmax(0, 1fr)", gap: 8, fontSize: 13 }}>
      <span style={{ color: K.muted }}>{label}</span>
      <span style={{ color: K.body, overflowWrap: "anywhere" }}>{children}</span>
    </div>
  );
}

export default async function StaffReleasePage({ params }: { params: Promise<{ tenantId: string; tripId: string }> }) {
  const raw = await params;
  const tenantId = uuidParam(raw.tenantId);
  const tripId = uuidParam(raw.tripId);
  if (!tenantId || !tripId) notFound();
  const { cfg, session, lang, locale } = await staffContext();
  const back = (
    <Link href="/staff/ops" style={{ fontSize: 13 }}>
      ← {t(lang, "staff.back")}
    </Link>
  );
  if (!session || !session.groups.includes("crm-ops")) return <ErrorState message={t(lang, "staff.noAccess")} />;
  const res = await staffCall<Release>(cfg, session, "GET", `agencies/${tenantId}/trips/${tripId}/ops-release`);
  if (!res.ok) {
    const message =
      res.status === 404 ? t(lang, "staff.release.notFound") : t(lang, res.status === 0 || res.status >= 500 ? "staff.error.api" : refusalKey(res.detail));
    return (
      <div style={{ display: "grid", gap: 12 }}>
        {back}
        <ErrorState message={message} />
      </div>
    );
  }
  const r = res.data;
  const doc = r.document;
  const day = (iso: string | null) => (iso ? formatDate(iso.slice(0, 10), locale) : "—");
  return (
    <div style={{ display: "grid", gap: 16, gridTemplateColumns: "minmax(0, 1fr)" }} data-testid="staff-release">
      {back}
      <PageTitle
        title={fmt(t(lang, "staff.release.title"), { ref: doc.trip.trip_ref })}
        subtitle={fmt(t(lang, "staff.release.viewNote"), { n: r.view_count })}
      />
      <Panel title={t(lang, "staff.release.summary")}>
        <div style={{ display: "grid", gap: 6 }}>
          <Row label={t(lang, "staff.release.agency")}>{doc.trip.agency_name ?? "—"}</Row>
          <Row label={t(lang, "staff.release.timezone")}>{doc.trip.agency_timezone}</Row>
          <Row label={t(lang, "staff.release.dates")}>
            {day(doc.trip.start_date)} – {day(doc.trip.end_date)}
          </Row>
          <Row label={t(lang, "staff.release.party")}>
            {fmt(t(lang, "staff.release.party.value"), { adults: doc.trip.adults, children: doc.trip.children })}
          </Row>
          <Row label={t(lang, "staff.ops.col.completeness")}>
            <span style={{ display: "inline-flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
              <Badge tone={r.complete ? "success" : "warning"}>
                {r.complete
                  ? t(lang, "staff.ops.complete")
                  : fmt(t(lang, "staff.ops.incomplete"), { complete: r.completeness.complete, n: r.completeness.travellers })}
              </Badge>
              {missingItems(r.completeness).map((m) => (
                <span key={m.item} style={{ fontSize: 12, color: K.muted }}>
                  {fmt(t(lang, `staff.ops.missing.${m.item}`), { n: m.n })}
                </span>
              ))}
            </span>
          </Row>
          <Row label={t(lang, "staff.ops.col.departures")}>
            <span style={{ fontFamily: mono, fontSize: 12 }}>{r.departure_ids.join(", ") || "—"}</span>
          </Row>
          <Row label={t(lang, "staff.release.line.booking")}>
            <span style={{ fontFamily: mono, fontSize: 12 }}>{r.bk_booking_ids.join(", ") || "—"}</span>
          </Row>
          <Row label={t(lang, "staff.ops.col.deletion")}>
            <span data-testid="staff-release-purge">
              {r.purge_at && r.days_to_purge !== null
                ? fmt(t(lang, "staff.release.purge"), { date: day(r.purge_at), n: r.days_to_purge })
                : t(lang, "staff.ops.purgeUnknown")}
            </span>
          </Row>
          <Row label="">{fmt(t(lang, "staff.release.version"), { n: r.payload_version, date: day(r.refreshed_at) })}</Row>
          <Row label="">
            {r.incident_admin_url ? (
              <a href={r.incident_admin_url} target="_blank" rel="noopener noreferrer" data-testid="staff-release-incident-link">
                {t(lang, "staff.ops.incident")} ↗
              </a>
            ) : (
              <span style={{ color: K.muted }}>{t(lang, "staff.ops.incidentMissing")}</span>
            )}
          </Row>
        </div>
      </Panel>
      <Panel title={t(lang, "staff.release.lines")}>
        <div style={{ display: "grid", gap: 8 }}>
          {doc.lines.map((ln) => (
            <div key={ln.booking_line_id} style={{ display: "grid", gap: 2, fontSize: 13 }}>
              <span style={{ color: K.ink, fontWeight: 600 }}>{ln.title ?? ln.tour_id ?? "—"}</span>
              <span style={{ color: K.muted, overflowWrap: "anywhere" }}>
                {t(lang, "staff.release.line.departure")}: <span style={{ fontFamily: mono }}>{ln.departure_id ?? "—"}</span> ·{" "}
                {t(lang, "staff.release.line.booking")}: <span style={{ fontFamily: mono }}>{ln.bk_booking_id ?? "—"}</span> ·{" "}
                {t(lang, "staff.release.line.service")}: {day(ln.service_from)} – {day(ln.service_to)}
              </span>
            </div>
          ))}
        </div>
      </Panel>
      <Panel title={t(lang, "staff.release.travellers")}>
        <ReleaseTravellers travellers={doc.travellers} lang={lang} />
      </Panel>
      <Panel title={t(lang, "staff.manifest.title")}>
        <ManifestDownload tenantId={tenantId} tripId={tripId} lang={lang} />
      </Panel>
      <p style={{ margin: 0, fontSize: 12, color: K.muted }}>{t(lang, "staff.audited")}</p>
    </div>
  );
}
