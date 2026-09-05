"use client";
// app/(tenant)/portal/_components/AngleGateTab.tsx — AA-449 (T8 Angle Gate) + AA-450 (T9 Write +
// T10-inline quality check), ONE continuous wizard on ONE page — Nghiep's explicit mid-build
// decision (AA-450 "UI liền mạch" addendum): the tenant never leaves this page or clicks to a
// separate T9 route between choosing an angle and seeing the written result. Route stays
// /portal/t8-angle-gate.
//
// AA-522 (04/09/2026) — Luồng B removed. Before this fix, this ONE component branched on
// `req.subject_id` into 2 very different-looking flows: a 3-step Goal->Angle->Write wizard for a
// Slate-driven request (subject_id set, channel already fixed — "Luồng A", the real, current
// design per AA-511/512), and a 5-step Atom->Goal->Angle->Channel->Write wizard with a raw atom
// dropdown (subject_id null — "Luồng B", the pre-AA-511 design AA-512 said should have been fully
// replaced, not left running in parallel). Luồng B was still reachable — Sidebar's "Write Content"
// link goes straight to /portal/t8-angle-gate with no query params, which used to render the raw
// atom picker. Per AA-511/512/525's confirmed architecture (tenant never creates a request by
// picking an atom directly — every request starts from a Slate pick, api/routers/v1_planning.py's
// pick_subject()), that whole path is now removed: no atom dropdown, no Channel step (channel is
// always fixed at creation from the Subject), no `POST /v1/angle-gate/requests {atom_id}` /
// `POST .../channel` calls from this file — both backend endpoints were deleted with it (see
// services/acp_angle_gate/service.py). A direct visit with no `resume_request_id` now shows an
// empty state pointing at the Slate instead of a dead-end picker.
//
// AA-522's OTHER real fix, same session: the actual bug this issue was filed for. "Write" used to
// look like it silently failed to save whenever the tenant reloaded mid-flow. Root cause (traced
// via AA-525's live Playwright repro, this issue's own comment thread): when POST .../write came
// back 422 (angle_gate_request.cta is NULL — the realistic case for every real Subject-driven
// request today, see migration 114/AA-450's own header), the resulting "enter a CTA" form existed
// ONLY in local React state (`needsCtaInput`). A reload at exactly that moment re-fetched the
// request (still status='approved', channel set) and landed back on the Write card with NO local
// state telling it a CTA was needed — no form, no button, no way forward. Fixed by making the
// Write step ask the SERVER what its real state is on every load (see the `useEffect` below that
// calls GET .../latest-piece), instead of trusting client-only state that a reload wipes. This
// also replaced the old "auto-fire write from inside choose()'s success handler" wiring — the one
// `useEffect` below now owns every path that can lead to the write step (fresh choose, a resumed
// reload, a reopen+re-choose "Change angle" cycle) so there's exactly one place that decides
// "should we call write now, or ask for a CTA, or just show what's already there" — not three.
//
// Per ADR-2026-038 §0.2/§10.3 (tenant self-service — AA does not gate tenant content; the T8
// "gate" is the TENANT choosing, never AA): "Goal" is always the 8-value list (Bang 1), "Angle"
// is always the 3 LLM-generated options per request.
//
// Real back-navigation exists for exactly ONE step, because it's the only one the backend
// supports (AA-497's reopen_request(), approved -> reusable): from the Write card, "Change angle"
// reopens the SAME request and rewinds to the angle-choice card, no new LLM call. Channel has no
// dependency on which angle was picked (T9's write prompt applies channel style independently of
// the angle's own best_final_style) and is fixed at creation anyway (AA-522), so there's nothing
// to re-ask there.
//
// Full workflow:
//   1. Slate (SlateTab.tsx) picks a Subject -> creates the request (subject_id + channel already
//      set) -> navigates here via ?resume_request_id=.
//   2. Tenant picks a Goal from the 8-value list.
//   3. Backend auto-applies fixed brand audience + formula, generates 3 angles, recommends one.
//   4. Tenant picks one of the 3 (recommended or not) — the real gate. status -> approved.
//   5. AUTOMATICALLY: the write-step effect below fires T9's write the instant an angle is
//      approved, UNLESS the request's CTA is still NULL, in which case it asks for one first.
//   6. ONE loading state while T9 writes + T10 checks (up to 2 attempts, inline) -> final result.
//
// API (via /api/tenant proxy -> Authorization: Bearer <cis_tenant_token>, tenant_id always
// resolved from the JWT):
//   GET  /api/tenant/v1/angle-gate/goals
//   GET  /api/tenant/v1/angle-gate/requests/{id}
//   POST /api/tenant/v1/angle-gate/requests/{id}/goal              {goal}
//   POST /api/tenant/v1/angle-gate/requests/{id}/choose            {idx}
//   POST /api/tenant/v1/angle-gate/requests/{id}/reopen             {}     — AA-497, "Change angle"
//   POST /api/tenant/v1/content-writing/requests/{id}/write         {cta?}  — AA-450/466, 202 +
//                                                                     'processing' placeholder,
//                                                                     poll GET .../pieces/{id}
//   GET  /api/tenant/v1/content-writing/pieces/{id}                 — AA-466 poll target
//   GET  /api/tenant/v1/content-writing/requests/{id}/latest-piece  — AA-522, resume support: the
//                                                                     latest piece for THIS
//                                                                     request's currently-chosen
//                                                                     angle, or {piece: null}.

import { useState, useEffect, useCallback, useRef } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Sparkles, CheckCircle2, ChevronRight, RotateCcw, AlertTriangle } from "lucide-react";
import { T, serif, sans, mono, Card, CardHead, Badge, Btn, LoadingScreen, EmptyState, Spinner } from "./ui";

const POLL_INTERVAL_MS = 3000;
const POLL_CEILING_MS = 180_000;

interface Goal {
  key: string;
  name: string;
  description: string;
  logic: string;
  marketing_term: string;
}

interface AngleOption {
  idx: number;
  name: string;
  why_it_works: string;
  formula_fit: string;
  best_final_style: string;
  recommended: boolean;
  chosen: boolean;
  // AA-512 — measurable-ranking evidence (services/acp_angle_gate/ranking.py). Both null
  // together = never ranked (a pre-AA-512 legacy row) — the badge row is simply omitted then.
  answers: string[] | null;
  violations: string[] | null;
}

interface AngleGateRequest {
  request_id: string;
  atom_id: string;
  channel: string | null; // AA-522 — always set from creation now (fixed by the Subject); a
  // legacy pre-AA-522 row is the only way this is ever null.
  goal: string | null;
  cta: string | null; // AA-450 migration 114 — realistically NULL for every real request today
  // (see this file's own header comment) — the write-step effect below asks for one when NULL.
  // AA-497 — "reusable" is an approved request "Change angle" just reopened
  // (services/acp_angle_gate/service.py::reopen_request()): same "pick 1 of 3 already-generated
  // angles" UI as "pending_choice" below, choose() is unchanged either way.
  status: "pending_goal" | "pending_choice" | "approved" | "reusable";
  angles: AngleOption[];
  // AA-512 — the real PAA pool this request's angles were ranked against (AA-501 migration 127,
  // snapshotted at set_goal_and_generate() time) — only used here for the badge's denominator
  // ("answers X/Y"), Y = people_also_ask.length.
  dfs_paa_snapshot: { people_also_ask: string[] } | null;
  // AA-512 — fixed header (Subject + Channel, "không sửa được ở đây"). subject_id null = a
  // legacy pre-AA-522 row (the removed atom-picker path).
  subject_id: string | null;
  subject_score: number | null;
  subject_place: string | null;
  subject_action: string | null;
  subject_hub_name: string | null;
}

// AA-450 — mirrors services/acp_content_writing/service.py::_row_to_dict()'s response shape.
// AA-466: status gains "processing" (the 202 placeholder, before the background task finishes)
// and "failed" (a real system error in the background task — distinct from "held", which is a
// complete business outcome with real content_text; see migration 118's header).
interface GateLedgerEntry {
  gate: string;
  passed: boolean;
  violations: string[];
}

interface ContentPiece {
  piece_id: string;
  angle_gate_request_id: string;
  attempt_number: number;
  content_text: string;
  status: "processing" | "approved" | "held" | "failed";
  held_reason: string | null;
  gate_ledger: GateLedgerEntry[];
}

// AA-522 — 3 steps only now (Atom and Channel are gone with Luồng B).
type Step = 1 | 2 | 3;
const STEP_LABELS: [Step, string][] = [[1, "Goal"], [2, "Angle"], [3, "Write"]];
const ANGLE_STEP: Step = 2;

function currentStep(req: AngleGateRequest | null): Step {
  if (!req) return 1;
  if (req.status === "pending_goal") return 1;
  if (req.status === "pending_choice" || req.status === "reusable") return 2;
  return 3; // approved
}

// AA-512 — fixed, non-editable header shown above the Stepper (Linear: "Header cố định hiện
// Subject + Channel đã chọn, không sửa được ở đây"). Renders nothing for a legacy pre-AA-522 row
// with no subject_id.
function SubjectHeader({ req }: { req: AngleGateRequest }) {
  const place = req.subject_place ?? req.subject_hub_name;
  const detail = req.subject_place && req.subject_action ? `${req.subject_place} — ${req.subject_action}` : place;
  return (
    <div style={{ padding: "10px 14px", background: T.bg, border: `1px solid ${T.line2}`, borderRadius: 8, fontFamily: sans }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10 }}>
        <span style={{ fontSize: 12.5, fontWeight: 700, color: T.ink, textTransform: "capitalize" }}>
          Writing for: {req.channel}
        </span>
        {req.subject_score !== null && <Badge variant="default">Score {req.subject_score}</Badge>}
      </div>
      {detail && <div style={{ fontSize: 12, color: T.muted, marginTop: 4 }}>{detail}</div>}
    </div>
  );
}

// AA-512 — the 2 measurable badges on an angle card. Omitted entirely when this angle was never
// ranked (answers/violations both null — a pre-AA-512 legacy row).
function AngleRankingBadges({ angle, paaTotal }: { angle: AngleOption; paaTotal: number }) {
  if (angle.answers === null || angle.violations === null) return null;
  const n = angle.violations.length;
  return (
    <div style={{ display: "flex", gap: 8, marginTop: 8, paddingTop: 8, borderTop: `1px solid ${T.line2}` }}>
      <span
        title={angle.answers.length ? angle.answers.join("; ") : "No PAA data for this request"}
        style={{ fontSize: 11.5, color: T.muted, fontFamily: sans }}
      >
        {paaTotal > 0 ? `✓ answers ${angle.answers.length}/${paaTotal} PAA questions` : "no PAA data"}
      </span>
      <span
        title={angle.violations.join("; ") || "No avoid-list phrases matched"}
        style={{ fontSize: 11.5, fontFamily: sans, color: n > 0 ? "#8A5A16" : T.muted }}
      >
        {n > 0 ? `⚠ ${n} avoid-list hit${n === 1 ? "" : "s"}` : "0 avoid-list violations"}
      </span>
    </div>
  );
}

// Mirrors SlotPickerPanel.tsx's Breadcrumb — same visual language (chevron-separated, active
// crumb bold, past crumbs dim + checked), but only the "2 Angle" crumb is ever clickable, and
// only from step 3, because reopen_request() is the only backend endpoint that actually supports
// jumping back a step.
function Stepper({ step, canChangeAngle, onChangeAngle }: {
  step: Step; canChangeAngle: boolean; onChangeAngle: () => void;
}) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap", padding: "8px 0" }}>
      {STEP_LABELS.map(([n, label], i) => {
        const done = n < step;
        const active = n === step;
        const clickable = n === ANGLE_STEP && step > ANGLE_STEP && canChangeAngle;
        const crumbStyle: React.CSSProperties = {
          display: "inline-flex", alignItems: "center", gap: 4, fontFamily: sans, fontSize: 12.5,
          fontWeight: active ? 700 : 500, color: active ? T.ink : done ? T.muted : T.muted2,
          background: "none", border: "none", padding: 0, cursor: clickable ? "pointer" : "default",
        };
        const content = <>{done && <CheckCircle2 size={12} color={T.green} />} {n} · {label}</>;
        return (
          <span key={n} style={{ display: "flex", alignItems: "center", gap: 6 }}>
            {i > 0 && <ChevronRight size={13} color={T.muted2} />}
            {clickable
              ? <button onClick={onChangeAngle} style={crumbStyle} title="Change angle — picks from the same 3 already-generated options, no new content generated">{content}</button>
              : <span style={crumbStyle}>{content}</span>}
          </span>
        );
      })}
    </div>
  );
}

export default function AngleGateTab() {
  const router = useRouter();
  // AA-497 — the Slate (SlateTab.tsx::pick_subject()) hands off here via
  // /portal/t8-angle-gate?resume_request_id=..., the ONLY way a request now gets loaded onto this
  // page (AA-522 removed the old ?atom_id= raw-atom-picker entry point along with Luồng B).
  const searchParams = useSearchParams();
  const resumeRequestId = searchParams.get("resume_request_id");

  const [goals, setGoals] = useState<Goal[]>([]);
  // AA-522 — only gates the initial paint while a resume_request_id is actually being resolved;
  // with none given there's nothing to wait for, the empty state below renders immediately.
  const [initialLoading, setInitialLoading] = useState(!!resumeRequestId);

  const [selectedGoal, setSelectedGoal] = useState("");

  const [req, setReq] = useState<AngleGateRequest | null>(null);
  const [generating, setGenerating] = useState(false);
  const [choosing, setChoosing] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  // AA-469 Việc 4 — pick-then-confirm for the angle step: clicking a card only highlights it via
  // this local state; choose() itself isn't called until "Confirm this angle" is pressed.
  const [pendingAngleIdx, setPendingAngleIdx] = useState<number | null>(null);

  // AA-497 — "Change angle" (step 3 -> back to step 2), see changeAngle() below.
  const [reopening, setReopening] = useState(false);
  const [reopenError, setReopenError] = useState<string | null>(null);

  // AA-450 — write + inline T10 check. AA-466: `writing` spans the whole 202+poll cycle.
  const [piece, setPiece] = useState<ContentPiece | null>(null);
  const [writing, setWriting] = useState(false);
  const [writeError, setWriteError] = useState<string | null>(null);
  const [needsCtaInput, setNeedsCtaInput] = useState(false);
  const [ctaInput, setCtaInput] = useState("");
  const [pollTimedOut, setPollTimedOut] = useState(false); // 180s poll ceiling hit, NOT a failure
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // AA-522 — tracks which request_id the write-step effect below has already resolved (fetched
  // latest-piece for, and either restored it or decided to auto-write/ask-for-CTA), so it runs
  // exactly once per real "arrival at the write step" rather than re-firing on every setReq() no-
  // op. Reset in changeAngle()'s success handler so a reopen+re-choose cycle gets a fresh check.
  const resolvedWriteStepFor = useRef<string | null>(null);

  useEffect(() => {
    fetch("/api/tenant/v1/angle-gate/goals")
      .then(r => (r.ok ? r.json() : { goals: [] }))
      .then(d => setGoals(d.goals ?? []))
      .catch(() => {});
  }, []);

  // AA-497/AA-522 — load the resumed request. Loads straight into whatever card its real status
  // calls for (goal / angle-choice / write) — no separate "resume" branch needed, the per-status
  // cards below already cover every value the API can return.
  useEffect(() => {
    if (!resumeRequestId) return;
    fetch(`/api/tenant/v1/angle-gate/requests/${resumeRequestId}`)
      .then(async r => (r.ok ? r.json() : Promise.reject(await r.json().catch(() => ({})))))
      .then(d => { setReq(d); setPendingAngleIdx(null); })
      .catch(e => setError(e.detail ?? "Couldn't load that request — try again from the Slate."))
      .finally(() => setInitialLoading(false));
  }, [resumeRequestId]);

  const submitGoal = useCallback(() => {
    if (!req || !selectedGoal) return;
    setGenerating(true); setError(null);
    fetch(`/api/tenant/v1/angle-gate/requests/${req.request_id}/goal`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ goal: selectedGoal }),
    })
      .then(async r => (r.ok ? r.json() : Promise.reject(await r.json().catch(() => ({})))))
      .then(d => { setReq(d); setPendingAngleIdx(null); })
      .catch(e => setError(e.detail ?? "Couldn't generate angles — try again."))
      .finally(() => setGenerating(false));
  }, [req, selectedGoal]);

  const stopPolling = useCallback(() => {
    if (pollingRef.current) { clearInterval(pollingRef.current); pollingRef.current = null; }
  }, []);

  // AA-466 — single-piece poll for the 202 placeholder.
  const pollPiece = useCallback((pieceId: string) => {
    stopPolling();
    const startTime = Date.now();
    pollingRef.current = setInterval(async () => {
      if (Date.now() - startTime > POLL_CEILING_MS) {
        stopPolling();
        setWriting(false);
        setPollTimedOut(true);
        return;
      }
      try {
        const r = await fetch(`/api/tenant/v1/content-writing/pieces/${pieceId}`);
        if (!r.ok) return; // transient — next tick may succeed, backend is still working either way
        const fresh: ContentPiece = await r.json();
        if (fresh.status !== "processing") {
          stopPolling();
          setPiece(fresh);
          setWriting(false);
        }
      } catch { /* transient network error — keep polling, don't surface as a failure */ }
    }, POLL_INTERVAL_MS);
  }, [stopPolling]);

  useEffect(() => stopPolling, [stopPolling]); // cleanup on unmount

  // AA-450 — `cta` only ever overrides a NULL angle_gate_request.cta. AA-466: POST returns 202 +
  // a 'processing' placeholder immediately — the real result comes from polling.
  const writeContent = useCallback((requestId: string, cta?: string) => {
    setWriting(true); setWriteError(null); setNeedsCtaInput(false); setPollTimedOut(false);
    fetch(`/api/tenant/v1/content-writing/requests/${requestId}/write`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(cta ? { cta } : {}),
    })
      .then(async r => {
        if (r.status === 422) {
          // MissingCTAError — kept as a defense-in-depth fallback (the write-step effect below
          // already checks req.cta BEFORE calling write, so this shouldn't normally trigger, but
          // a stale req from a race is still possible) — same "ask, don't fabricate" behavior.
          setWriting(false);
          setNeedsCtaInput(true);
          return null;
        }
        if (!r.ok) {
          const err = await r.json().catch(() => ({}));
          throw new Error(err.detail ?? "Couldn't write content — try again.");
        }
        return r.json();
      })
      .then((placeholder: ContentPiece | null) => {
        if (!placeholder) return; // 422 branch above already handled its own state
        setPiece(placeholder);
        pollPiece(placeholder.piece_id);
      })
      .catch(e => {
        setWriting(false);
        setWriteError(e instanceof Error ? e.message : "Couldn't write content — try again.");
      });
  }, [pollPiece]);

  // AA-522 — the ONE place that decides what the write step should show, whether arrived at via
  // a fresh choose(), a reload/resume, or a "Change angle" re-choice: ask the server for the
  // latest content_piece under the currently-chosen angle (GET .../latest-piece). This is the
  // actual fix for this issue's bug — the old code trusted local React state (`needsCtaInput`)
  // that a reload silently wiped, leaving the tenant stuck on an empty Write card with no CTA
  // form and no button. Runs once per request_id (resolvedWriteStepFor ref guard); changeAngle()
  // resets that ref so a re-choice gets a fresh check instead of showing the PREVIOUS angle's
  // stale piece (the backend query is itself also scoped to the current chosen option, as a
  // second layer of protection against that).
  useEffect(() => {
    if (!req || req.status !== "approved") return;
    if (resolvedWriteStepFor.current === req.request_id) return;
    resolvedWriteStepFor.current = req.request_id;

    fetch(`/api/tenant/v1/content-writing/requests/${req.request_id}/latest-piece`)
      .then(r => (r.ok ? r.json() : { piece: null }))
      .then(({ piece: latest }: { piece: ContentPiece | null }) => {
        if (latest) {
          setPiece(latest);
          if (latest.status === "processing") { setWriting(true); pollPiece(latest.piece_id); }
          return;
        }
        // No piece written yet under this angle. If a CTA is already known, write immediately
        // (matches the old "no extra click" behavior) — otherwise ask for one up front instead
        // of waiting for write() to come back 422.
        if (req.cta) writeContent(req.request_id);
        else setNeedsCtaInput(true);
      })
      .catch(() => {
        // Best-effort — worst case the tenant sees a blank Write card and can press "Retry"/
        // re-enter a CTA manually; not worth surfacing as a hard error for a resume-convenience
        // lookup.
      });
  }, [req, writeContent, pollPiece]);

  const choose = useCallback((idx: number) => {
    if (!req) return;
    setChoosing(idx); setError(null);
    fetch(`/api/tenant/v1/angle-gate/requests/${req.request_id}/choose`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ idx }),
    })
      .then(async r => (r.ok ? r.json() : Promise.reject(await r.json().catch(() => ({})))))
      .then(d => setReq(d)) // AA-522 — the write-step effect above now owns firing the write call
      .catch(e => setError(e.detail ?? "Couldn't save your choice — try again."))
      .finally(() => setChoosing(null));
  }, [req]);

  // AA-497 — "Change angle" (available from step 3, status 'approved'): reopens THIS SAME
  // request (approved -> reusable, no new LLM call) and rewinds the UI to the angle-choice card.
  const changeAngle = useCallback(() => {
    if (!req) return;
    setReopening(true); setReopenError(null);
    fetch(`/api/tenant/v1/angle-gate/requests/${req.request_id}/reopen`, { method: "POST" })
      .then(async r => (r.ok ? r.json() : Promise.reject(await r.json().catch(() => ({})))))
      .then(d => {
        setReq(d);
        stopPolling();
        setPiece(null); setWriteError(null); setNeedsCtaInput(false); setPollTimedOut(false);
        setPendingAngleIdx(null);
        resolvedWriteStepFor.current = null; // AA-522 — force a fresh latest-piece check on re-choice
      })
      .catch(e => setReopenError(e.detail ?? "Couldn't reopen — try again."))
      .finally(() => setReopening(false));
  }, [req, stopPolling]);

  // "Start over" — discards this request entirely and returns to the empty state (the tenant
  // picks a new Subject from the Slate to start again). Confirms before wiping real progress (a
  // goal already submitted means at least one real LLM call happened).
  const reset = useCallback(() => {
    if (req && !window.confirm("Start over? This clears your current goal, angle, and write progress.")) return;
    stopPolling();
    setReq(null); setSelectedGoal(""); setError(null);
    setPiece(null); setWriteError(null); setNeedsCtaInput(false); setCtaInput("");
    setPollTimedOut(false); setReopenError(null); setPendingAngleIdx(null);
    resolvedWriteStepFor.current = null;
    router.push("/portal/t8-angle-gate");
  }, [req, stopPolling, router]);

  if (initialLoading) return <LoadingScreen message="Loading…" />;

  const step = currentStep(req);
  const chosenAngle = req?.angles.find(a => a.chosen) ?? null;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20, maxWidth: 720 }}>
      <p style={{ fontSize: 12, color: T.muted, margin: 0, lineHeight: 1.5 }}>
        Choose a content goal, then pick 1 of the 3 angles the system generates. You always
        choose — Adventure Asia never approves or blocks this for you.
      </p>

      {req?.subject_id && <SubjectHeader req={req} />}

      {req && (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, flexWrap: "wrap", borderBottom: `1px solid ${T.line2}` }}>
          <Stepper step={step} canChangeAngle={req.status === "approved"} onChangeAngle={changeAngle} />
          <Btn variant="ghost" size="sm" onClick={reset}><RotateCcw size={12} /> Start over</Btn>
        </div>
      )}

      {error && (
        <div style={{ padding: "9px 12px", background: T.redSoft, border: "1px solid #F5C6C6", borderRadius: 8, fontSize: 12, color: T.red }}>
          {error}
        </div>
      )}

      {/* AA-522 — no more raw atom picker: every request now starts from the Slate. */}
      {!req && (
        <Card>
          <CardHead title="Pick a Subject to start" />
          <EmptyState icon="🧭" title="Nothing to write yet"
            sub="Go to the Slate and pick a Subject — Write Content always starts from there now."
            action={<Btn variant="primary" onClick={() => router.push("/portal/t7-planning")}>Go to the Slate</Btn>} />
        </Card>
      )}

      {req && req.status === "pending_goal" && (
        <Card>
          <CardHead title="1 · Choose a Goal" />
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {goals.map(g => (
              <button key={g.key} onClick={() => setSelectedGoal(g.key)} style={{
                textAlign: "left", padding: "10px 14px", borderRadius: 8, cursor: "pointer",
                border: `1px solid ${selectedGoal === g.key ? T.gold : T.line}`,
                background: selectedGoal === g.key ? T.goldTint : "#fff",
              }}>
                <div style={{ fontSize: 13, fontWeight: 700, color: T.ink, fontFamily: sans }}>{g.name}</div>
                <div style={{ fontSize: 11.5, color: T.muted, marginTop: 2 }}>{g.description}</div>
              </button>
            ))}
            <div style={{ marginTop: 6 }}>
              <Btn variant="primary" disabled={!selectedGoal || generating} onClick={submitGoal}>
                {generating ? <>Generating 3 angles…</> : <><Sparkles size={13} /> Generate 3 angles</>}
              </Btn>
            </div>
          </div>
        </Card>
      )}

      {req && (req.status === "pending_choice" || req.status === "reusable") && (
        // This card ONLY shows while actively choosing (not once approved — see the Write card's
        // meta row below for the post-choice summary). Pick-then-confirm: clicking a card only
        // sets pendingAngleIdx (highlight); "Confirm this angle" calls choose().
        <Card>
          <CardHead title={`2 · ${req.status === "reusable" ? "Choose a Different Angle" : "Choose an Angle"}`} />
          <p style={{ fontSize: 12.5, color: T.muted, margin: "0 0 14px", lineHeight: 1.5 }}>
            {req.status === "reusable"
              ? "Pick a different one of the 3 angles below, then confirm — no new content is generated until you do."
              : "Pick one of the 3 angles below, then confirm."}
          </p>
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            {req.angles.map(a => {
              const picked = pendingAngleIdx === a.idx;
              const clickable = !a.chosen;
              return (
                <button key={a.idx} disabled={!clickable}
                  onClick={() => clickable && setPendingAngleIdx(a.idx)}
                  style={{
                    textAlign: "left", width: "100%", cursor: clickable ? "pointer" : "default",
                    padding: "14px 16px", borderRadius: 10, position: "relative", fontFamily: sans,
                    border: `1px solid ${a.chosen ? T.green : picked ? T.gold : a.recommended ? T.goldSoft : T.line}`,
                    borderWidth: picked ? 2 : 1,
                    background: a.chosen ? T.greenSoft : picked ? T.goldTint : a.recommended ? T.goldTint : "#fff",
                  }}>
                  {picked && !a.chosen && <CheckCircle2 size={16} color={T.gold} style={{ position: "absolute", top: 12, right: 12 }} />}
                  <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
                    <span style={{ fontFamily: serif, fontSize: 15, fontWeight: 600, color: T.ink }}>{a.name}</span>
                    {a.recommended && <Badge variant="default">Recommended</Badge>}
                    {a.chosen && <Badge variant="default">Currently chosen</Badge>}
                  </div>
                  <div style={{ fontSize: 12.5, color: T.body, marginBottom: 4 }}>
                    <strong>Why it works:</strong> {a.why_it_works}
                  </div>
                  <div style={{ fontSize: 12.5, color: T.body, marginBottom: 4 }}>
                    <strong>Formula fit:</strong> <span style={{ fontFamily: mono }}>{a.formula_fit}</span>
                  </div>
                  <div style={{ fontSize: 12.5, color: T.body }}>
                    <strong>Best final style:</strong> {a.best_final_style}
                  </div>
                  <AngleRankingBadges angle={a} paaTotal={req.dfs_paa_snapshot?.people_also_ask.length ?? 0} />
                </button>
              );
            })}
          </div>
          <div style={{ marginTop: 14 }}>
            <Btn variant="primary" disabled={pendingAngleIdx === null || choosing !== null}
              onClick={() => pendingAngleIdx !== null && choose(pendingAngleIdx)}>
              {choosing !== null ? "Saving…" : "Confirm this angle"}
            </Btn>
          </div>
        </Card>
      )}

      {req && req.status === "approved" && (
        <Card>
          <CardHead title="3 · Write" />

          {chosenAngle && (
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10, flexWrap: "wrap", marginBottom: 14, padding: "10px 12px", background: T.bg, border: `1px solid ${T.line2}`, borderRadius: 8 }}>
              <div style={{ fontSize: 12.5, color: T.body }}>
                <strong>Angle:</strong> {chosenAngle.name}
                <span style={{ color: T.muted2 }}> · </span>
                <strong>Goal:</strong> {req.goal}
                <span style={{ color: T.muted2 }}> · </span>
                <strong>Channel:</strong> {req.channel}
              </div>
              {!writing && (
                <Btn size="sm" variant="secondary" disabled={reopening} onClick={changeAngle}>
                  {reopening ? "Reopening…" : "Change angle"}
                </Btn>
              )}
            </div>
          )}

          {reopenError && (
            <div style={{ padding: "9px 12px", background: T.redSoft, border: "1px solid #F5C6C6", borderRadius: 8, fontSize: 12, color: T.red, marginBottom: 10 }}>
              {reopenError}
            </div>
          )}

          {writing && (
            <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "10px 4px", color: T.muted, fontSize: 13 }}>
              <Spinner size={16} /> Writing and checking your content — one moment…
            </div>
          )}

          {pollTimedOut && !writing && piece && (
            <div style={{ display: "flex", flexDirection: "column", gap: 10, padding: "9px 12px", background: T.goldTint, border: `1px solid ${T.gold}`, borderRadius: 8, marginBottom: 10 }}>
              <div style={{ fontSize: 12.5, color: T.body, lineHeight: 1.5 }}>
                Still working — this is taking longer than usual. Your content is still being
                written in the background.
              </div>
              <div>
                <Btn size="sm" variant="secondary" onClick={() => { setWriting(true); setPollTimedOut(false); pollPiece(piece.piece_id); }}>
                  Check status
                </Btn>
              </div>
            </div>
          )}

          {needsCtaInput && !writing && (
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              <div style={{ fontSize: 12.5, color: T.body, lineHeight: 1.5 }}>
                This piece needs a call to action before it can be written — what should the
                reader do next?
              </div>
              <input value={ctaInput} onChange={e => setCtaInput(e.target.value)}
                placeholder="e.g. Book a consultation, Read the full guide…"
                style={{ padding: "9px 12px", background: "#fff", border: `1px solid ${T.line}`, borderRadius: 8, color: T.body, fontSize: 13, fontFamily: sans }} />
              <div>
                <Btn variant="primary" disabled={!ctaInput.trim()}
                  onClick={() => writeContent(req.request_id, ctaInput.trim())}>
                  <Sparkles size={13} /> Write content
                </Btn>
              </div>
            </div>
          )}

          {writeError && !writing && (
            <div style={{ padding: "9px 12px", background: T.redSoft, border: "1px solid #F5C6C6", borderRadius: 8, fontSize: 12, color: T.red, marginBottom: 10 }}>
              {writeError}
            </div>
          )}

          {piece && !writing && piece.status === "failed" && (
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 11.5, fontWeight: 700, color: T.red }}>
                <AlertTriangle size={13} /> Something went wrong while writing this content
              </div>
              <div>
                <Btn size="sm" variant="primary" onClick={() => writeContent(req.request_id)}>
                  <Sparkles size={13} /> Retry
                </Btn>
              </div>
            </div>
          )}

          {piece && !writing && (piece.status === "approved" || piece.status === "held") && (
            <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                {piece.status === "approved" ? (
                  <>
                    <Badge variant="default">Approved</Badge>
                    <span style={{ fontSize: 11.5, color: T.muted }}>
                      passed quality review{piece.attempt_number > 1 ? ` (attempt ${piece.attempt_number})` : ""}
                    </span>
                  </>
                ) : (
                  <>
                    <span style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: 11.5, fontWeight: 700, color: T.red }}>
                      <AlertTriangle size={13} /> Needs review
                    </span>
                    <span style={{ fontSize: 11.5, color: T.muted }}>
                      quality review didn&rsquo;t clear after {piece.attempt_number} attempt{piece.attempt_number > 1 ? "s" : ""}
                    </span>
                  </>
                )}
              </div>

              <div style={{ padding: "14px 16px", borderRadius: 10, border: `1px solid ${piece.status === "approved" ? T.line : "#F5C6C6"}`, background: piece.status === "approved" ? "#fff" : T.redSoft, whiteSpace: "pre-wrap", fontSize: 13.5, lineHeight: 1.6, color: T.body, fontFamily: sans }}>
                {piece.content_text}
              </div>

              {piece.status === "held" && piece.held_reason && (
                <div style={{ fontSize: 11.5, color: T.muted }}>
                  <strong>Reason:</strong> {piece.held_reason}
                </div>
              )}

              {req.cta && (
                <div style={{ fontSize: 11, color: T.muted }}><strong>CTA:</strong> {req.cta}</div>
              )}
            </div>
          )}
        </Card>
      )}
    </div>
  );
}
