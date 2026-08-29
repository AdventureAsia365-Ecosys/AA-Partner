"use client";
// app/(tenant)/portal/_components/PlanningTab.tsx — AA-448 (T7 Content Planning, tenant-facing)
//
// Per ADR-2026-038 §0.2 (tenant self-service — no AA content gate at any T0-T11 step): a tenant
// plans + finalizes their own quarter here, no admin approval step (Gate B Option A, round 6 —
// see docs/implementation-notes/AA-448-t7-content-planning.md). Reads trips/atoms from the
// tenant's own T4/T6 data (services/acp_planning/tenant_pool.py), never the platform-wide
// catalog.
//
// API (via /api/tenant proxy -> Authorization: Bearer <cis_tenant_token>, api/routers/
// v1_planning.py — tenant_id is always resolved from the JWT, never sent by this component):
//   POST /api/tenant/v1/planning/quarter-plan/preview  {year, quarter, specials, excluded_trip_ids}
//   POST /api/tenant/v1/planning/quarter-plan           (finalize — same body, auto-approved)
//   GET  /api/tenant/v1/planning/quarter-plan?year=&quarter=
//   GET  /api/tenant/v1/planning/quarter-plan/history?year=&quarter=      (AA-469 Việc 2)
//   GET  /api/tenant/v1/planning/quarter-plan/versions/{version_id}      (AA-469 Việc 2)
//   GET  /api/tenant/v1/planning/slot-grid?year=&month=
//   POST /api/tenant/v1/planning/metrics                {piece_id, reach, engagement, clicks}
//   POST /api/tenant/v1/planning/metrics/rollup
//   GET  /api/tenant/v1/planning/trip-reallocation/suggest?year=&quarter=
//   POST /api/tenant/v1/planning/trip-reallocation/confirm {year, quarter, accept}
//
// Feedback loop (round 6) is explicitly a NEW extension beyond aa-marketing-v2's own Module H —
// see services/acp_shared/content_metrics.py's module docstring for the full boundary.
//
// "Edit after finalize" (AA-469 Việc 2(a)) — decision, flagged explicitly rather than silently
// picked: at the QUARTER-PLAN level (which trips get planned), "edit" means adjusting
// specials/excluded_trip_ids and re-finalizing — this generalizes the pre-existing
// specials/excluded_trip_ids override-replay mechanism (QuarterPlanRequest already accepted
// both; only this component's own hardcoded `[]` was wiring them away) rather than inventing a
// new primitive. Re-finalizing always creates a NEW quarter_plan_version (never overwrites —
// migration 092's own design), so nothing is destructively "edited" — the History section below
// is what makes the resulting trail visible/browsable.
// At the SLOT/ATOM level (which atom fills a given week's post), STEP0-refresh (this session)
// confirmed re-picking an atom for an ALREADY-WRITTEN slot is still explicitly out of scope —
// AA-494's own Step 5 notes call this out as a deliberate deferral ("a written slot-card is
// display-only... not re-expandable"), blocked on AA-494 Decision 3's still-unbuilt status-
// transition wiring (`reusable` status has zero code path yet — content_piece's own
// UNIQUE(angle_gate_request_id, attempt_number) constraint would collide the moment a second
// write session is attempted on the same request, a genuine open schema question AA-494's own
// migration 124 header flags, not resolved here). Not built in this pass — see
// docs/implementation-notes/AA-469.md for the full writeup.

import { useState, useCallback } from "react";
import { T, serif, sans, mono, Card, CardHead, Badge, Btn, LoadingScreen, EmptyState } from "./ui";
import SlotPickerPanel from "./SlotPickerPanel";

interface TripScore {
  trip_id: string;
  name: string;
  destination: string | null;
  score: number;
  runway_fit: number;
  richness: number;
  distinctiveness_score: number;
  dfs_relevance_score: number;
  engagement_adjustment_score: number;
  forced: boolean;
  selected: boolean;
  reason: string;
}

interface QuarterPlanPayload {
  trip_ids: string[];
  forced_specials: string[];
  destination_shares: Record<string, number>;
  capacity_note: string | null;
  trip_scores: TripScore[];
}

interface WeekLock {
  year: number;
  month: number;
  week: number;
  locked: boolean;
  reason: string | null;
}

interface PreviewResponse {
  plan: QuarterPlanPayload;
  trip_pool_size: number;
  config: { markets: string[]; channels: string[]; capacity_posts_per_week: number };
  lock_status: WeekLock[];
  fully_locked: boolean;
}

interface ReallocationSuggestion {
  plan: QuarterPlanPayload;
  has_existing_plan: boolean;
  added: string[];
  removed: string[];
  unchanged: string[];
}

const now = new Date();
const DEFAULT_YEAR = now.getFullYear();
const DEFAULT_QUARTER = Math.floor(now.getMonth() / 3) + 1;

export default function PlanningTab() {
  const [year, setYear] = useState(DEFAULT_YEAR);
  const [quarter, setQuarter] = useState(DEFAULT_QUARTER);
  const [preview, setPreview] = useState<PreviewResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [finalizing, setFinalizing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [finalizedVersionId, setFinalizedVersionId] = useState<string | null>(null);

  // AA-469 Việc 2(a) — the edit mechanism: specials/excluded_trip_ids, previously hardcoded to
  // [] below, now real tenant-editable overrides that replay into every preview/finalize call.
  const [specialsInput, setSpecialsInput] = useState("");
  const [excludedTripIds, setExcludedTripIds] = useState<Set<string>>(new Set());

  const toggleExcluded = useCallback((tripId: string) => {
    setExcludedTripIds(prev => {
      const next = new Set(prev);
      if (next.has(tripId)) next.delete(tripId); else next.add(tripId);
      return next;
    });
  }, []);

  const runPreview = useCallback(() => {
    setLoading(true);
    setError(null);
    setFinalizedVersionId(null);
    const specials = specialsInput.split(",").map(s => s.trim()).filter(Boolean);
    fetch("/api/tenant/v1/planning/quarter-plan/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ year, quarter, specials, excluded_trip_ids: Array.from(excludedTripIds) }),
    })
      .then(r => (r.ok ? r.json() : Promise.reject(r)))
      .then(d => setPreview(d))
      .catch(() => setError("Couldn't compute a preview — try again."))
      .finally(() => setLoading(false));
  }, [year, quarter, specialsInput, excludedTripIds]);

  const finalize = useCallback(() => {
    setFinalizing(true);
    setError(null);
    const specials = specialsInput.split(",").map(s => s.trim()).filter(Boolean);
    fetch("/api/tenant/v1/planning/quarter-plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ year, quarter, specials, excluded_trip_ids: Array.from(excludedTripIds) }),
    })
      .then(async r => (r.ok ? r.json() : Promise.reject(await r.json().catch(() => ({})))))
      .then(d => setFinalizedVersionId(d.version_id))
      .catch((e) => setError(e?.detail || "Couldn't finalize this quarter's plan."))
      .finally(() => setFinalizing(false));
  }, [year, quarter, specialsInput, excludedTripIds]);

  return (
    <div>
      <div style={{ marginBottom: 22 }}>
        <h2 style={{ fontFamily: serif, fontSize: 22, fontWeight: 500, color: T.ink, margin: "0 0 6px", letterSpacing: "-0.01em" }}>
          Content Planning
        </h2>
        <p style={{ fontSize: 13, color: T.muted, lineHeight: 1.6, margin: 0 }}>
          Pick which of your tours get planned into this quarter, based on booking-window timing,
          curated atom richness, real search demand, and — once you have engagement data — real
          performance feedback. You finalize your own plan here, no approval step.
        </p>
      </div>

      <Card style={{ padding: "16px 18px", marginBottom: 18 }}>
        <div style={{ display: "flex", gap: 14, alignItems: "flex-end", flexWrap: "wrap" }}>
          <YearQuarterPicker year={year} quarter={quarter} onYear={setYear} onQuarter={setQuarter} />
          <div style={{ flex: "1 1 220px", minWidth: 220 }}>
            <div style={{ fontSize: 10, color: T.muted2, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 4 }}>
              Force-include (comma-separated trip name/destination)
            </div>
            <input value={specialsInput} onChange={e => setSpecialsInput(e.target.value)}
              placeholder="e.g. Sapa, Ha Giang Loop" style={{
                width: "100%", boxSizing: "border-box", padding: "8px 10px", background: T.bg,
                border: `1px solid ${T.line}`, borderRadius: 8, color: T.body, fontSize: 13,
                fontFamily: sans, outline: "none",
              }} />
          </div>
          <Btn variant="primary" onClick={runPreview} disabled={loading}>
            {loading ? "Computing…" : "Preview plan"}
          </Btn>
          {preview && (
            <Btn variant="primary" onClick={finalize} disabled={finalizing || preview.fully_locked}
              style={{ background: T.green, borderColor: T.green, color: "#fff" }}>
              {finalizing ? "Finalizing…" : "Finalize this quarter"}
            </Btn>
          )}
        </div>
        {excludedTripIds.size > 0 && (
          <div style={{ marginTop: 10, fontSize: 12, color: T.muted }}>
            {excludedTripIds.size} trip{excludedTripIds.size === 1 ? "" : "s"} excluded — re-run
            Preview to see the effect, then Finalize to save it as a new version.
          </div>
        )}
        {error && <div style={{ marginTop: 10, fontSize: 12.5, color: T.red }}>{error}</div>}
        {finalizedVersionId && (
          <div style={{ marginTop: 10, fontSize: 12.5, color: T.green }}>
            Finalized — plan is now live for Q{quarter} {year}.
          </div>
        )}
      </Card>

      {loading && <LoadingScreen message="Computing your quarter plan…" />}

      {!loading && preview && (
        <PreviewPanel preview={preview} excludedTripIds={excludedTripIds} onToggleExcluded={toggleExcluded} />
      )}

      {!loading && !preview && (
        <EmptyState icon="🗓️" title="Preview a quarter to get started"
          sub="Pick a year and quarter above, then Preview plan — nothing is saved until you finalize it." />
      )}

      <HistorySection year={year} quarter={quarter} />

      <SlotPickerPanel />

      <FeedbackSection />
      <ReallocationSection defaultYear={year} defaultQuarter={quarter} />
    </div>
  );
}

function YearQuarterPicker({ year, quarter, onYear, onQuarter }: {
  year: number; quarter: number; onYear: (y: number) => void; onQuarter: (q: number) => void;
}) {
  const selectStyle: React.CSSProperties = {
    padding: "8px 10px", background: T.bg, border: `1px solid ${T.line}`, borderRadius: 8,
    color: T.body, fontSize: 13, fontFamily: sans, outline: "none",
  };
  return (
    <>
      <div>
        <div style={{ fontSize: 10, color: T.muted2, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 4 }}>Year</div>
        <select value={year} onChange={e => onYear(Number(e.target.value))} style={selectStyle}>
          {[DEFAULT_YEAR - 1, DEFAULT_YEAR, DEFAULT_YEAR + 1].map(y => <option key={y} value={y}>{y}</option>)}
        </select>
      </div>
      <div>
        <div style={{ fontSize: 10, color: T.muted2, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 4 }}>Quarter</div>
        <select value={quarter} onChange={e => onQuarter(Number(e.target.value))} style={selectStyle}>
          {[1, 2, 3, 4].map(q => <option key={q} value={q}>Q{q}</option>)}
        </select>
      </div>
    </>
  );
}

function PreviewPanel({ preview, excludedTripIds, onToggleExcluded }: {
  preview: PreviewResponse; excludedTripIds: Set<string>; onToggleExcluded: (tripId: string) => void;
}) {
  const lockedWeeks = preview.lock_status.filter(w => w.locked).length;
  return (
    <>
      <Card style={{ padding: "16px 18px", marginBottom: 18 }}>
        <CardHead title="This quarter" />
        <div style={{ display: "flex", gap: 22, flexWrap: "wrap", marginBottom: preview.plan.capacity_note ? 10 : 0 }}>
          <StatBlock label="Trip pool" value={preview.trip_pool_size} />
          <StatBlock label="Selected" value={preview.plan.trip_ids.length} />
          <StatBlock label="Weeks locked" value={`${lockedWeeks} / 12`} />
          <StatBlock label="Markets" value={preview.config.markets.join(", ") || "—"} />
        </div>
        {preview.plan.capacity_note && (
          <div style={{ fontSize: 12, color: T.muted, background: T.bg, padding: "8px 12px", borderRadius: 8 }}>
            {preview.plan.capacity_note}
          </div>
        )}
        {preview.fully_locked && (
          <div style={{ marginTop: 10, fontSize: 12.5, color: T.red }}>
            Every week of this quarter is already produced or in the past — nothing left to plan.
          </div>
        )}
      </Card>

      <Card style={{ padding: "16px 18px", marginBottom: 18 }}>
        <CardHead title={`Trips (${preview.plan.trip_scores.length})`} />
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
            <thead>
              <tr style={{ textAlign: "left", color: T.muted2, fontSize: 10.5, textTransform: "uppercase", letterSpacing: "0.06em" }}>
                <th style={{ padding: "6px 8px" }}>Exclude</th>
                <th style={{ padding: "6px 8px" }}>Trip</th>
                <th style={{ padding: "6px 8px" }}>Selected</th>
                <th style={{ padding: "6px 8px" }}>Score</th>
                <th style={{ padding: "6px 8px" }}>Runway</th>
                <th style={{ padding: "6px 8px" }}>Richness</th>
                <th style={{ padding: "6px 8px" }}>Distinct.</th>
                <th style={{ padding: "6px 8px" }}>DFS demand</th>
                <th style={{ padding: "6px 8px" }}>Feedback</th>
                <th style={{ padding: "6px 8px" }}>Why</th>
              </tr>
            </thead>
            <tbody>
              {preview.plan.trip_scores.map(ts => (
                <tr key={ts.trip_id} style={{ borderTop: `1px solid ${T.line2}`, opacity: excludedTripIds.has(ts.trip_id) ? 0.5 : 1 }}>
                  <td style={{ padding: "8px" }}>
                    <input type="checkbox" checked={excludedTripIds.has(ts.trip_id)}
                      onChange={() => onToggleExcluded(ts.trip_id)}
                      title="Exclude this trip, then Preview again to see the effect" />
                  </td>
                  <td style={{ padding: "8px" }}>
                    <div style={{ fontWeight: 600, color: T.ink }}>{ts.name}</div>
                    <div style={{ fontSize: 10.5, color: T.muted2 }}>{ts.destination}</div>
                  </td>
                  <td style={{ padding: "8px" }}>
                    {ts.selected ? <Badge variant="success">selected</Badge> : <Badge>—</Badge>}
                    {ts.forced && <div style={{ marginTop: 4 }}><Badge variant="gold">special</Badge></div>}
                  </td>
                  <ScoreCell v={ts.score} />
                  <ScoreCell v={ts.runway_fit} />
                  <ScoreCell v={ts.richness} />
                  <ScoreCell v={ts.distinctiveness_score} />
                  <ScoreCell v={ts.dfs_relevance_score} />
                  <ScoreCell v={ts.engagement_adjustment_score} />
                  <td style={{ padding: "8px", fontSize: 11.5, color: T.muted, maxWidth: 180 }}>{ts.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </>
  );
}

function ScoreCell({ v }: { v: number }) {
  return <td style={{ padding: "8px", fontFamily: mono, color: T.ink3 }}>{v.toFixed(2)}</td>;
}

function StatBlock({ label, value }: { label: string; value: number | string }) {
  return (
    <div style={{ minWidth: 100 }}>
      <div style={{ fontSize: 10, color: T.muted2, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 3 }}>{label}</div>
      <div style={{ fontFamily: serif, fontSize: 20, fontWeight: 500, color: T.ink }}>{value}</div>
    </div>
  );
}

// ---------------------------------------------------------------- history (AA-469 Việc 2)

interface HistoryVersion {
  version_id: string;
  version_no: number;
  approval_status: string;
  approved_by: string | null;
  approved_at: string | null;
  created_at: string | null;
  source: string;
}

interface HistoryVersionDetail {
  version_no: number;
  approval_status: string;
  plan: QuarterPlanPayload;
}

function HistorySection({ year, quarter }: { year: number; quarter: number }) {
  const [open, setOpen] = useState(false);
  const [versions, setVersions] = useState<HistoryVersion[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<HistoryVersionDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    setSelectedId(null);
    setDetail(null);
    fetch(`/api/tenant/v1/planning/quarter-plan/history?year=${year}&quarter=${quarter}`)
      .then(r => (r.ok ? r.json() : Promise.reject()))
      .then(d => setVersions(d.versions))
      .catch(() => setVersions([]))
      .finally(() => setLoading(false));
  }, [year, quarter]);

  const toggleOpen = useCallback(() => {
    const next = !open;
    setOpen(next);
    if (next && versions === null) load();
  }, [open, versions, load]);

  const selectVersion = useCallback((versionId: string) => {
    setSelectedId(versionId);
    setDetailLoading(true);
    setDetail(null);
    fetch(`/api/tenant/v1/planning/quarter-plan/versions/${versionId}`)
      .then(r => (r.ok ? r.json() : Promise.reject()))
      .then(d => setDetail(d))
      .catch(() => setDetail(null))
      .finally(() => setDetailLoading(false));
  }, []);

  return (
    <Card style={{ padding: "16px 18px", marginBottom: 18 }}>
      <CardHead title={`History — Q${quarter} ${year}`}
        action={<Btn variant="secondary" size="sm" onClick={toggleOpen}>
          {open ? "Hide" : (loading ? "Loading…" : "View history")}
        </Btn>} />
      {open && (
        <>
          <p style={{ fontSize: 12, color: T.muted, lineHeight: 1.6, margin: "0 0 12px" }}>
            Every version of this quarter&rsquo;s plan you&rsquo;ve ever finalized — re-finalizing
            never overwrites, it always saves a new one. Older versions stay here for reference.
          </p>
          {loading && <div style={{ fontSize: 12.5, color: T.muted }}>Loading…</div>}
          {!loading && versions && versions.length === 0 && (
            <div style={{ fontSize: 12.5, color: T.muted }}>No saved versions for this quarter yet.</div>
          )}
          {!loading && versions && versions.length > 0 && (
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {versions.map(v => (
                <div key={v.version_id} onClick={() => selectVersion(v.version_id)}
                  style={{
                    display: "flex", alignItems: "center", gap: 10, padding: "8px 10px",
                    borderRadius: 8, cursor: "pointer", fontSize: 12.5,
                    background: selectedId === v.version_id ? T.bg : "transparent",
                    border: `1px solid ${selectedId === v.version_id ? T.line : "transparent"}`,
                  }}>
                  <span style={{ fontFamily: mono, color: T.ink3 }}>v{v.version_no}</span>
                  <Badge variant={v.approval_status === "approved" ? "success" : undefined}>
                    {v.approval_status}
                  </Badge>
                  <span style={{ color: T.muted2 }}>{v.approved_by || "—"}</span>
                  <span style={{ color: T.muted2, marginLeft: "auto" }}>
                    {v.created_at ? new Date(v.created_at).toLocaleString() : "—"}
                  </span>
                </div>
              ))}
            </div>
          )}
          {selectedId && (
            <div style={{ marginTop: 14, paddingTop: 14, borderTop: `1px solid ${T.line2}` }}>
              {detailLoading && <div style={{ fontSize: 12.5, color: T.muted }}>Loading version…</div>}
              {!detailLoading && detail && (
                <div style={{ display: "flex", gap: 22, flexWrap: "wrap" }}>
                  <StatBlock label="Trips" value={detail.plan.trip_ids.length} />
                  <StatBlock label="Special" value={detail.plan.forced_specials.length} />
                  <StatBlock label="Status" value={detail.approval_status} />
                  <div style={{ minWidth: 200, fontSize: 12, color: T.muted }}>
                    <div style={{ fontSize: 10, color: T.muted2, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 3 }}>
                      Destination shares
                    </div>
                    {Object.entries(detail.plan.destination_shares).map(([dest, share]) => (
                      <div key={dest}>{dest}: {(share * 100).toFixed(0)}%</div>
                    ))}
                  </div>
                </div>
              )}
              {!detailLoading && !detail && (
                <div style={{ fontSize: 12.5, color: T.red }}>Couldn&rsquo;t load this version.</div>
              )}
            </div>
          )}
        </>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------- feedback loop (round 6)

function FeedbackSection() {
  const [pieceId, setPieceId] = useState("");
  const [reach, setReach] = useState("");
  const [engagement, setEngagement] = useState("");
  const [clicks, setClicks] = useState("");
  const [status, setStatus] = useState<string | null>(null);
  const [rollupStatus, setRollupStatus] = useState<string | null>(null);

  const submitMetric = useCallback(() => {
    setStatus(null);
    fetch("/api/tenant/v1/planning/metrics", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        piece_id: pieceId,
        reach: reach ? Number(reach) : null,
        engagement: engagement ? Number(engagement) : null,
        clicks: clicks ? Number(clicks) : null,
      }),
    })
      .then(async r => (r.ok ? r.json() : Promise.reject(await r.json().catch(() => ({})))))
      .then(() => { setStatus("Saved."); setPieceId(""); setReach(""); setEngagement(""); setClicks(""); })
      .catch((e) => setStatus(e?.detail || "Couldn't save — check the piece ID."));
  }, [pieceId, reach, engagement, clicks]);

  const runRollup = useCallback(() => {
    setRollupStatus("Recomputing…");
    fetch("/api/tenant/v1/planning/metrics/rollup", { method: "POST" })
      .then(r => (r.ok ? r.json() : Promise.reject()))
      .then(d => setRollupStatus(`${d.atoms_adjusted} atom${d.atoms_adjusted === 1 ? "" : "s"} adjusted.`))
      .catch(() => setRollupStatus("Couldn't recompute — try again."));
  }, []);

  return (
    <Card style={{ padding: "16px 18px", marginBottom: 18 }}>
      <CardHead title="Feedback" />
      <p style={{ fontSize: 12.5, color: T.muted, lineHeight: 1.6, margin: "0 0 14px" }}>
        Report what you observed after posting a piece somewhere (no auto-publish yet) — after at
        least 3 posts using the same content atom carry a metric, its weight adjusts and future
        planning reflects it.
      </p>
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "flex-end", marginBottom: 10 }}>
        <LabeledInput label="Piece ID" value={pieceId} onChange={setPieceId} width={200} />
        <LabeledInput label="Reach" value={reach} onChange={setReach} />
        <LabeledInput label="Engagement" value={engagement} onChange={setEngagement} />
        <LabeledInput label="Clicks" value={clicks} onChange={setClicks} />
        <Btn variant="primary" onClick={submitMetric} disabled={!pieceId}>Report metric</Btn>
      </div>
      {status && <div style={{ fontSize: 12, color: status === "Saved." ? T.green : T.red, marginBottom: 10 }}>{status}</div>}
      <div style={{ borderTop: `1px solid ${T.line2}`, paddingTop: 12, display: "flex", alignItems: "center", gap: 10 }}>
        <Btn variant="secondary" onClick={runRollup}>Recompute atom weights</Btn>
        {rollupStatus && <span style={{ fontSize: 12, color: T.muted }}>{rollupStatus}</span>}
      </div>
    </Card>
  );
}

function LabeledInput({ label, value, onChange, width = 100 }: {
  label: string; value: string; onChange: (v: string) => void; width?: number;
}) {
  return (
    <div>
      <div style={{ fontSize: 10, color: T.muted2, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 4 }}>{label}</div>
      <input value={value} onChange={e => onChange(e.target.value)} style={{
        padding: "8px 10px", background: T.bg, border: `1px solid ${T.line}`, borderRadius: 8,
        color: T.body, fontSize: 13, fontFamily: sans, outline: "none", width, boxSizing: "border-box",
      }} />
    </div>
  );
}

// ---------------------------------------------------------------- trip reallocation (round 6)

function ReallocationSection({ defaultYear, defaultQuarter }: { defaultYear: number; defaultQuarter: number }) {
  const nextQuarter = defaultQuarter === 4 ? 1 : defaultQuarter + 1;
  const nextYear = defaultQuarter === 4 ? defaultYear + 1 : defaultYear;
  const [suggestion, setSuggestion] = useState<ReallocationSuggestion | null>(null);
  const [loading, setLoading] = useState(false);
  const [confirmStatus, setConfirmStatus] = useState<string | null>(null);

  const loadSuggestion = useCallback(() => {
    setLoading(true);
    setConfirmStatus(null);
    fetch(`/api/tenant/v1/planning/trip-reallocation/suggest?year=${nextYear}&quarter=${nextQuarter}`)
      .then(r => (r.ok ? r.json() : Promise.reject()))
      .then(d => setSuggestion(d))
      .catch(() => setSuggestion(null))
      .finally(() => setLoading(false));
  }, [nextYear, nextQuarter]);

  const confirm = useCallback((accept: boolean) => {
    setConfirmStatus(accept ? "Applying…" : "Recording…");
    fetch("/api/tenant/v1/planning/trip-reallocation/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ year: nextYear, quarter: nextQuarter, accept }),
    })
      .then(r => (r.ok ? r.json() : Promise.reject()))
      .then(() => setConfirmStatus(accept ? "Applied — plan updated for next quarter." : "Rejected — no change made."))
      .catch(() => setConfirmStatus("Couldn't record your decision — try again."));
  }, [nextYear, nextQuarter]);

  return (
    <Card style={{ padding: "16px 18px" }}>
      <CardHead title={`Reallocation suggestion — Q${nextQuarter} ${nextYear}`}
        action={<Btn variant="secondary" size="sm" onClick={loadSuggestion} disabled={loading}>
          {loading ? "Checking…" : "Check for a suggestion"}
        </Btn>} />
      <p style={{ fontSize: 12.5, color: T.muted, lineHeight: 1.6, margin: "0 0 14px" }}>
        Based on real feedback so far, here&rsquo;s what would change if you re-planned next quarter now —
        nothing changes until you approve it.
      </p>
      {suggestion && (
        <div>
          {suggestion.added.length === 0 && suggestion.removed.length === 0 ? (
            <div style={{ fontSize: 12.5, color: T.muted }}>No change suggested — the plan stays the same.</div>
          ) : (
            <>
              {suggestion.added.length > 0 && (
                <div style={{ marginBottom: 8, fontSize: 12.5 }}>
                  <Badge variant="success">+{suggestion.added.length} add</Badge>
                  <span style={{ marginLeft: 8, color: T.muted, fontFamily: mono, fontSize: 11 }}>
                    {suggestion.added.join(", ")}
                  </span>
                </div>
              )}
              {suggestion.removed.length > 0 && (
                <div style={{ marginBottom: 12, fontSize: 12.5 }}>
                  <Badge variant="error">−{suggestion.removed.length} remove</Badge>
                  <span style={{ marginLeft: 8, color: T.muted, fontFamily: mono, fontSize: 11 }}>
                    {suggestion.removed.join(", ")}
                  </span>
                </div>
              )}
              <div style={{ display: "flex", gap: 8 }}>
                <Btn variant="primary" size="sm" onClick={() => confirm(true)}>Apply this</Btn>
                <Btn variant="ghost" size="sm" onClick={() => confirm(false)}>Reject</Btn>
              </div>
            </>
          )}
        </div>
      )}
      {confirmStatus && <div style={{ marginTop: 10, fontSize: 12, color: T.muted }}>{confirmStatus}</div>}
    </Card>
  );
}
