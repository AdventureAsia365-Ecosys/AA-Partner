"use client";
// app/(tenant)/portal/_components/PlanningTab.tsx — T7 (tenant-facing), page title "Slate".
//
// AA-519 Việc 3 — this page used to open with a Quarter Plan preview/trip-table/finalize/history
// UI (AA-448, extended AA-469 Việc 2) ABOVE <SlateTab/>; Nghiệp's explicit call (AA-519 issue
// body) is that this was fully superseded by the Slate (AA-511) and should come out, not stay as
// dead/hidden code — removed here. See git history on this file (pre-AA-519) for that design's
// full record (API routes it called, the "edit after finalize" decision, etc.) if ever needed
// again. FeedbackSection/ReallocationSection below predate AA-511 too but were kept (scope
// decision posted to the AA-519 Linear comment before building) — neither is "Quarter Plan UI" in
// the narrow sense that changed here.
//
// API this file still calls (via /api/tenant proxy -> Authorization: Bearer <cis_tenant_token>,
// api/routers/v1_planning.py — tenant_id always resolved from the JWT):
//   POST /api/tenant/v1/planning/metrics                {piece_id, reach, engagement, clicks}
//   POST /api/tenant/v1/planning/metrics/rollup
//   GET  /api/tenant/v1/planning/trip-reallocation/suggest?year=&quarter=
//   POST /api/tenant/v1/planning/trip-reallocation/confirm {year, quarter, accept}
// (<SlateTab/> itself calls GET /v1/slate + POST /v1/subjects/{id}/pick — see that file.)
//
// Feedback loop (round 6) is explicitly a NEW extension beyond aa-marketing-v2's own Module H —
// see services/acp_shared/content_metrics.py's module docstring for the full boundary.

import { useState, useCallback } from "react";
import { T, serif, sans, mono, Card, CardHead, Badge, Btn } from "./ui";
// AA-511 — the Slate replaces SlotPickerPanel.tsx (Weekly Slots) on this render path. The old
// component/file is deliberately left in place, unused, per the epic's own "giữ code cũ, chỉ
// ngưng dùng, không xoá" rule — see docs/claude_audit/AA-511-step0-slate-investigation.md.
import SlateTab from "./SlateTab";

// AA-519 Việc 3 — the old Quarter Plan preview/trip-table/finalize/history UI that used to render
// ABOVE <SlateTab/> on this page is REMOVED here (not hidden — Nghiệp's explicit call, "đã thay
// thế hoàn toàn bởi Slate", AA-519 issue body). That UI's own types/components
// (YearQuarterPicker/PreviewPanel/HistorySection/ScoreCell/StatBlock/TripScore/QuarterPlanPayload/
// WeekLock/PreviewResponse/HistoryVersion*) are deleted along with it, not left as dead code — see
// git history (this file, pre-AA-519) to recover if ever needed. FeedbackSection and
// ReallocationSection below are KEPT (scope decision, posted to the AA-519 Linear comment before
// building this): neither is "Quarter Plan UI" in the narrow sense (no trip-selection table/
// preview/finalize), each has its own card heading and stands independently.

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
  return (
    <div>
      <div style={{ marginBottom: 22 }}>
        <h2 style={{ fontFamily: serif, fontSize: 22, fontWeight: 500, color: T.ink, margin: "0 0 6px", letterSpacing: "-0.01em" }}>
          Slate
        </h2>
        <p style={{ fontSize: 13, color: T.muted, lineHeight: 1.6, margin: 0 }}>
          Channel-scoped content picks, ranked by score and cleared against each channel&rsquo;s
          bar — pick one below to start writing.
        </p>
      </div>

      <SlateTab />

      <FeedbackSection />
      <ReallocationSection defaultYear={DEFAULT_YEAR} defaultQuarter={DEFAULT_QUARTER} />
    </div>
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
