"use client";
// app/(tenant)/portal/_components/AngleGateTab.tsx — AA-449 (T8 Angle Gate) + AA-450 (T9 Write +
// T10-inline quality check), ONE continuous wizard on ONE page — Nghiep's explicit mid-build
// decision (AA-450 "UI liền mạch" addendum): the tenant never leaves this page or clicks to a
// separate T9 route between choosing an angle and seeing the written result. Route stays
// /portal/t8-angle-gate (least-disruptive choice — 0 real tenant traffic yet, no reason to
// introduce a second URL for one continuous flow); a standalone /portal/t9-write route was NOT
// built, per that same decision.
//
// T8 and T9 remain 2 SEPARATE backend API surfaces (unchanged, already built/deployed/verified,
// PR #203-205) — this file only chains 2 existing calls together in the UI. Nothing here required
// any API/schema change: T9's own `attempt_number` design already means one T8 request (one
// chosen angle) can be written more than once, so T8's request lifecycle and T9's write/attempt
// lifecycle are genuinely different data models, only merged at the UI layer.
//
// Per ADR-2026-038 §0.2/§10.3 (tenant self-service — AA does not gate tenant content; the T8
// "gate" is the TENANT choosing, never AA) + STEP0 §2 (terminology): this component always says
// "Goal" for the 8-value list (Bang 1) and "Angle" only for the 3 LLM-generated options per
// request — never mixes the two.
//
// AA-469 Việc 4 (redesign, this session — 2 passes) — restyled to match T7's
// SlotPickerPanel.tsx drill-down language: a top Stepper replaces the old per-card "Start over"
// buttons (now one consolidated action next to the Stepper), and angle selection is pick-then-
// confirm (click a card to highlight it, a separate "Confirm this angle" button submits) instead
// of a single click submitting immediately — same two-beat pattern as SlotPickerPanel.tsx's
// atom-pick + "Start writing".
//
// PASS 2, same session — FLOW-ORDER FIX, confirmed with Nghiệp (supersedes pass 1's assumption
// that the existing [atom+channel] -> goal -> angle -> write order was already correct — it was
// NOT). The real order is: atom(+DFS/PAA+brand, server-side) -> Goal -> generate 3 angles ->
// pick 1 -> Channel (NEW step, AFTER the angle, not before angle generation) -> T9 write. Channel
// used to be chosen alongside the atom in step 1 AND fed into angle generation itself
// (services/acp_angle_gate/generate.py used to take a `channel` param) — neither is true anymore.
// See that module's own header comment for why dropping channel from angle generation doesn't
// lose any real channel-fit (T9's write prompt re-applies the full channel style block at write
// time regardless, unchanged by this fix).
//
// Stepper: 1 Atom · 2 Goal · 3 Angle · 4 Channel · 5 Write.
//
// Real back-navigation exists for exactly ONE step, because it's the only one the backend
// supports (AA-497's reopen_request(), approved -> reusable): from step 4 or 5, "Change angle"
// (the Stepper's "3 Angle" crumb AND inline on the Write card's meta row) reopens the SAME
// request and rewinds to the angle-choice card, no new LLM call. Reconciled with the channel
// reorder as follows (flagged explicitly, not guessed): reopening does NOT reset or re-ask
// channel — channel has no dependency on which specific angle was picked (T9's write prompt
// applies channel style independently of the angle's own best_final_style), so a previously-set
// channel simply carries over unchanged across a reopen+re-choice cycle, and write auto-fires
// again immediately once the new angle is confirmed (channel is already known). There is
// currently no symmetric "Change channel" action (set_channel() supports being called again
// freely while still 'approved', so one could be added later with no backend work) — not built
// this session, flagged as a natural follow-up rather than guessed into scope.
//
// Steps 1 and 2 have no reopen-style backend endpoint (creating a request / submitting a goal
// are still one-way), so their Stepper crumbs are informational only, not clickable — "Start
// over" (full reset) remains the only way back past step 3.
//
// Full workflow, all in this one component now:
//   1. Pick an atom (from this tenant's own curated T6 atoms). No channel here anymore.
//   2. Pick a Goal from the 8-value list.
//   3-6. Backend auto-applies fixed brand audience, formula, generates 3 angles, recommends one
//      (no channel input to this call anymore either).
//   7. Tenant picks one of the 3 (recommended or not) — the real gate. status -> approved.
//   8. NEW — tenant picks a Channel (1 of 8), a separate confirm step.
//   9. AUTOMATICALLY, no extra click: fires the T9 write call the instant BOTH 7 and 8 resolve.
//   10. ONE loading state while T9 writes + T10 checks (up to 2 attempts, inline, server-side —
//      see docs/claude_audit/AA-450-01-t9-t10-retry-loop-investigation.md) -> final result.
//
// API (via /api/tenant proxy -> Authorization: Bearer <cis_tenant_token>, tenant_id always
// resolved from the JWT):
//   GET  /api/tenant/v1/angle-gate/goals
//   POST /api/tenant/v1/angle-gate/requests            {atom_id}                — step 1, NO
//                                                                     channel/year/month anymore
//   POST /api/tenant/v1/angle-gate/requests/{id}/goal              {goal}
//   GET  /api/tenant/v1/angle-gate/requests/{id}
//   POST /api/tenant/v1/angle-gate/requests/{id}/choose            {idx}
//   POST /api/tenant/v1/angle-gate/requests/{id}/reopen             {}     — AA-497, step "Change
//                                                                     angle" (approved -> reusable)
//   POST /api/tenant/v1/angle-gate/requests/{id}/channel  {channel, year, month} — NEW, step 8.
//                                                                     year/month moved here from
//                                                                     the old create-request body
//                                                                     (AA-451's slot-CTA prefill).
//   POST /api/tenant/v1/content-writing/requests/{id}/write         {cta?}  — AA-450, step 9-10
//                                                                     AA-466: 202 Accepted +
//                                                                     'processing' placeholder,
//                                                                     poll GET .../pieces/{id}
//   GET  /api/tenant/v1/content-writing/pieces/{id}                 — AA-466 poll target
//
// Atom picker reuses the same tenant-scoped atom list T6 (AtomsTab.tsx) already established
// (GET /api/tenant/admin/atoms) — no new atom-listing endpoint needed for this.
//
// AA-466 — /write moved from 1 blocking fetch to 202 Accepted + poll (real API Gateway 504s on
// long LLM+T10 runs, up to ~89s measured). Poll mechanics below (ref-guard against double-start,
// setInterval + hard ceiling, cleanup-on-unmount) mirror CatalogTab.tsx's list-poll skeleton —
// deliberately NOT copied wholesale, since that poll diffs a whole list and this one tracks a
// single piece by id (see docs/implementation-notes/AA-466.md for the full comparison). Ceiling
// is 180s / interval 3s, independent of the 90s API Gateway timeout — that ceiling only ever
// applied to the LLM call itself; POST /write now returns in ms, and each poll GET is a
// single-row read that never approaches it. If the ceiling is hit, the background task keeps
// running regardless (same "backend keeps working" precedent AA-450/452 already documented for
// the pre-202 504 case) — the tenant just needs to check back or press "Check status" again.

import { useState, useEffect, useCallback, useRef } from "react";
import { useSearchParams } from "next/navigation";
import { Sparkles, CheckCircle2, ChevronRight, RotateCcw, AlertTriangle } from "lucide-react";
import { T, serif, sans, mono, Card, CardHead, Badge, Btn, LoadingScreen, EmptyState, Spinner } from "./ui";

const POLL_INTERVAL_MS = 3000;
const POLL_CEILING_MS = 180_000;

// Kept in sync with services/acp_planning/models.py's Channel Literal + admin/tenants
// page.tsx's ALL_CHANNELS (AA-449).
const CHANNELS = ["blog", "facebook", "tiktok", "email", "linkedin", "instagram", "landing_page", "ads"];

interface Atom {
  atom_id: string;
  tour_name: string;
  text: string;
  starred: boolean;
}

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
}

interface AngleGateRequest {
  request_id: string;
  atom_id: string;
  channel: string | null; // AA-469 Việc 4 (flow-order fix) — NULL until step 8, set_channel()
  goal: string | null;
  cta: string | null; // AA-450 migration 114 — usually null today, see that migration's header
  // AA-497 — "reusable" is an approved request SlotPickerPanel.tsx's "Change angle" just
  // reopened (services/acp_angle_gate/service.py::reopen_request()): same "pick 1 of 3 already-
  // generated angles" UI as "pending_choice" below, choose() is unchanged either way.
  status: "pending_goal" | "pending_choice" | "approved" | "reusable";
  angles: AngleOption[];
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

// AA-469 Việc 4 — step derivation reads `req` directly (not local selection state) so the
// Stepper renders correctly both for a fresh start AND for a resumed (`resume_request_id`)
// request, which never populates selectedAtomId/selectedGoal locally.
type Step = 1 | 2 | 3 | 4 | 5;
const STEP_LABELS: [Step, string][] = [
  [1, "Atom"], [2, "Goal"], [3, "Angle"], [4, "Channel"], [5, "Write"],
];

function currentStep(req: AngleGateRequest | null): Step {
  if (!req) return 1;
  if (req.status === "pending_goal") return 2;
  if (req.status === "pending_choice" || req.status === "reusable") return 3;
  if (req.status === "approved" && !req.channel) return 4; // angle chosen, channel not yet
  return 5; // approved, channel set — ready for/at Write
}

// Mirrors SlotPickerPanel.tsx's Breadcrumb — same visual language (chevron-separated, active
// crumb bold, past crumbs dim + checked), but only the "3 Angle" crumb is ever clickable, and
// only from step 4+, because `reopen_request()` is the only backend endpoint that actually
// supports jumping back a step (see module header). The other crumbs are progress display
// only — clicking them would imply a "back" the backend can't do without discarding history.
// (No "4 Channel" crumb click either — set_channel() CAN be called again freely, but that
// symmetric "Change channel" action isn't built this session, see module header.)
function Stepper({ step, canChangeAngle, onChangeAngle }: {
  step: Step; canChangeAngle: boolean; onChangeAngle: () => void;
}) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap", padding: "8px 0" }}>
      {STEP_LABELS.map(([n, label], i) => {
        const done = n < step;
        const active = n === step;
        const clickable = n === 3 && step >= 4 && canChangeAngle;
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
  // AA-494 Step 5 — SlotPickerPanel.tsx (T7 slot-view) hands off here via
  // /portal/t8-angle-gate?atom_id=..., same useSearchParams() pattern AtomsTab.tsx already uses
  // for ?tour_id= (AA-345 round 5/6 lesson: useSearchParams(), never window.location.search).
  // Pre-selects the atom only — channel/goal/angle are still chosen here as before; the
  // write-time-channel move (design doc Decision 1) is explicitly deferred, not built by this
  // handoff.
  const searchParams = useSearchParams();
  const atomIdParam = searchParams.get("atom_id");
  // AA-497 — SlotPickerPanel.tsx's "Change angle" action already called .../reopen (approved ->
  // reusable) before navigating here; this component just needs to load that EXISTING request
  // (skip straight to step 3, the angle-choice card) rather than starting a new one via ?atom_id.
  const resumeRequestId = searchParams.get("resume_request_id");

  const [atoms, setAtoms] = useState<Atom[]>([]);
  const [goals, setGoals] = useState<Goal[]>([]);
  const [atomsLoading, setAtomsLoading] = useState(true);

  const [selectedAtomId, setSelectedAtomId] = useState(atomIdParam ?? "");
  const [selectedGoal, setSelectedGoal] = useState("");

  const [req, setReq] = useState<AngleGateRequest | null>(null);
  const [creating, setCreating] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [choosing, setChoosing] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  // AA-469 Việc 4 — pick-then-confirm for step 3 (matches SlotPickerPanel.tsx's atom-pick +
  // separate "Start writing" button): clicking an angle card only highlights it via this local
  // state; `choose()` itself isn't called until "Confirm this angle" is pressed. Reset at every
  // call site that starts a new angle-choice round (submitGoal, changeAngle, the resume load) —
  // NOT via a useEffect keyed on req, which would call setState synchronously inside an effect
  // (react-hooks/set-state-in-effect) for a value these same setReq() calls can just also clear.
  const [pendingAngleIdx, setPendingAngleIdx] = useState<number | null>(null);

  // AA-497 — "Change angle" (step 5/4 -> back to step 3), see changeAngle() below.
  const [reopening, setReopening] = useState(false);
  const [reopenError, setReopenError] = useState<string | null>(null);

  // AA-469 Việc 4 (flow-order fix) — step 4, NEW: channel picker.
  const [selectedChannel, setSelectedChannel] = useState("facebook");
  const [settingChannel, setSettingChannel] = useState(false);
  const [channelError, setChannelError] = useState<string | null>(null);

  // AA-450 — step 9-10: write + inline T10 check, chained automatically after choose()/
  // submitChannel() resolves (AA-469 Việc 4 moved the trigger point — see both callbacks below).
  // AA-466: `writing` now spans the whole 202+poll cycle (POST -> processing -> poll -> final
  // status), not just the POST round trip.
  const [piece, setPiece] = useState<ContentPiece | null>(null);
  const [writing, setWriting] = useState(false);
  const [writeError, setWriteError] = useState<string | null>(null);
  const [needsCtaInput, setNeedsCtaInput] = useState(false); // 422 fallback — see writeContent()
  const [ctaInput, setCtaInput] = useState("");
  const [pollTimedOut, setPollTimedOut] = useState(false); // 180s poll ceiling hit, NOT a failure
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    fetch("/api/tenant/admin/atoms?limit=100")
      .then(r => (r.ok ? r.json() : { atoms: [] }))
      .then(d => setAtoms((d.atoms ?? []).filter((a: { deleted?: boolean }) => !a.deleted)))
      .catch(() => {})
      .finally(() => setAtomsLoading(false));
    fetch("/api/tenant/v1/angle-gate/goals")
      .then(r => (r.ok ? r.json() : { goals: [] }))
      .then(d => setGoals(d.goals ?? []))
      .catch(() => {});
  }, []);

  // AA-497 — resume an already-reopened request (status should already be 'reusable', set by
  // SlotPickerPanel.tsx's own POST .../reopen before it navigated here). Loads straight into the
  // angle-choice card below — no atom/channel/goal picking, those were already done the first
  // time. If the request somehow isn't reopened (e.g. a stale/reused link), it still just shows
  // whatever real status the request is at — no separate error state needed, the existing
  // per-status cards below already cover every value the API can return.
  useEffect(() => {
    if (!resumeRequestId) return;
    fetch(`/api/tenant/v1/angle-gate/requests/${resumeRequestId}`)
      .then(async r => (r.ok ? r.json() : Promise.reject(await r.json().catch(() => ({})))))
      .then(d => { setReq(d); setPendingAngleIdx(null); })
      .catch(e => setError(e.detail ?? "Couldn't load that request — try again from Weekly Slots."));
  }, [resumeRequestId]);

  const startRequest = useCallback(() => {
    if (!selectedAtomId) { setError("Pick an atom first."); return; }
    setCreating(true); setError(null);
    // AA-469 Việc 4 (flow-order fix) — no channel/year/month here anymore; channel is picked at
    // the new step 4 (submitChannel() below), which is also where the AA-451 slot-CTA prefill
    // (year/month) moved to, since it's genuinely keyed by channel.
    fetch("/api/tenant/v1/angle-gate/requests", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ atom_id: selectedAtomId }),
    })
      .then(async r => (r.ok ? r.json() : Promise.reject(await r.json().catch(() => ({})))))
      .then(d => setReq({ ...d, goal: null, angles: [] }))
      .catch(e => setError(e.detail ?? "Couldn't start a request — try again."))
      .finally(() => setCreating(false));
  }, [selectedAtomId]);

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

  // AA-466 — single-piece poll for the 202 placeholder. Ref-guarded against double-start,
  // hard ceiling distinct from a real failure (see module header comment). Not the list-diff
  // shape CatalogTab.tsx uses — this tracks exactly 1 piece_id, no list to diff against.
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

  // AA-450 — step 10. `cta` is only ever sent to override a NULL angle_gate_request.cta (the
  // realistic case today, see migration 114's header comment) — never overrides a real one.
  // AA-466: POST now returns 202 + a 'processing' placeholder immediately — the real result
  // comes from polling GET .../pieces/{piece_id}, not from this response.
  const writeContent = useCallback((requestId: string, cta?: string) => {
    setWriting(true); setWriteError(null); setNeedsCtaInput(false); setPollTimedOut(false);
    fetch(`/api/tenant/v1/content-writing/requests/${requestId}/write`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(cta ? { cta } : {}),
    })
      .then(async r => {
        if (r.status === 422) {
          // MissingCTAError (services/acp_content_writing/service.py) — this request's
          // angle_gate_request.cta is NULL (the realistic case) and no override was sent yet.
          // Ask the tenant for one instead of silently fabricating a generic CTA (STEP0's
          // resolved Open Question #2).
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

  const choose = useCallback((idx: number) => {
    if (!req) return;
    setChoosing(idx); setError(null);
    fetch(`/api/tenant/v1/angle-gate/requests/${req.request_id}/choose`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ idx }),
    })
      .then(async r => (r.ok ? r.json() : Promise.reject(await r.json().catch(() => ({})))))
      .then(d => {
        setReq(d);
        // AA-450's original "no extra click" auto-fire, adapted for AA-469 Việc 4's flow-order
        // fix: write can't start until channel is ALSO known (T9 requires both), so this only
        // auto-fires when d.channel is already set — true on a reopen+re-choice cycle (channel
        // carries over unchanged, see module header), false on a first-time choice, where the
        // UI advances to the new step 4 (Channel) card instead and submitChannel() below is
        // what eventually fires the write.
        if (d.status === "approved" && d.channel) writeContent(d.request_id);
      })
      .catch(e => setError(e.detail ?? "Couldn't save your choice — try again."))
      .finally(() => setChoosing(null));
  }, [req, writeContent]);

  // AA-469 Việc 4 (flow-order fix) — step 8, NEW: tenant picks a channel, after the angle. Fires
  // the T9 write call automatically on success (same "no extra click" pattern choose() above
  // uses) — by this point both angle AND channel are known, so write can actually proceed.
  const submitChannel = useCallback(() => {
    if (!req) return;
    setSettingChannel(true); setChannelError(null);
    const now = new Date();
    fetch(`/api/tenant/v1/angle-gate/requests/${req.request_id}/channel`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        channel: selectedChannel, year: now.getFullYear(), month: now.getMonth() + 1,
      }),
    })
      .then(async r => (r.ok ? r.json() : Promise.reject(await r.json().catch(() => ({})))))
      .then(d => {
        setReq(d);
        if (d.status === "approved" && d.channel) writeContent(d.request_id);
      })
      .catch(e => setChannelError(e.detail ?? "Couldn't save channel — try again."))
      .finally(() => setSettingChannel(false));
  }, [req, selectedChannel, writeContent]);

  // AA-497 / AA-469 Việc 4 — "Change angle" (available from step 4 OR 5, both are 'approved'):
  // reopens THIS SAME request (approved ->
  // reusable, no new LLM call) and rewinds the UI to the angle-choice card. Only valid from
  // 'approved' (backend enforces via 409 WrongStatusError; the UI only ever surfaces this action
  // while req.status === 'approved', see Stepper's canChangeAngle + the Write card's meta row
  // below, so a stale double-click is the only way to hit that 409). Clearing the piece/write
  // states on success is what makes the Write card unmount and the (now 'reusable') angle card
  // reappear — no separate "step" state to manage, the existing per-status render already covers it.
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
      })
      .catch(e => setReopenError(e.detail ?? "Couldn't reopen — try again."))
      .finally(() => setReopening(false));
  }, [req, stopPolling]);

  // AA-469 Việc 4 — "Start over" now lives once, next to the Stepper, instead of repeated on
  // every card's CardHead. It's a full reset (unlike "Change angle" above, it discards atom/
  // channel/goal too, not just the angle) — still the only escape hatch for steps 1-2, which have
  // no reopen-style backend endpoint. Confirms before wiping real progress (a goal already
  // submitted means at least one real LLM call happened) rather than silently discarding it;
  // skipped when there's nothing to lose yet (no req).
  const reset = useCallback(() => {
    if (req && !window.confirm("Start over? This clears your current atom, goal, angle, and channel choices.")) return;
    stopPolling();
    setReq(null); setSelectedAtomId(""); setSelectedGoal(""); setError(null);
    setPiece(null); setWriteError(null); setNeedsCtaInput(false); setCtaInput("");
    setPollTimedOut(false); setReopenError(null); setPendingAngleIdx(null);
    setChannelError(null); setSettingChannel(false);
  }, [req, stopPolling]);

  if (atomsLoading) return <LoadingScreen message="Loading atoms…" />;

  const step = currentStep(req);
  const chosenAngle = req?.angles.find(a => a.chosen) ?? null;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20, maxWidth: 720 }}>
      <p style={{ fontSize: 12, color: T.muted, margin: 0, lineHeight: 1.5 }}>
        Pick an atom and a channel, choose a content goal, then pick 1 of the 3 angles the
        system generates. You always choose — Adventure Asia never approves or blocks this
        for you.
      </p>

      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, flexWrap: "wrap", borderBottom: `1px solid ${T.line2}` }}>
        <Stepper step={step} canChangeAngle={req?.status === "approved"} onChangeAngle={changeAngle} />
        {req && (
          <Btn variant="ghost" size="sm" onClick={reset}><RotateCcw size={12} /> Start over</Btn>
        )}
      </div>

      {error && (
        <div style={{ padding: "9px 12px", background: T.redSoft, border: "1px solid #F5C6C6", borderRadius: 8, fontSize: 12, color: T.red }}>
          {error}
        </div>
      )}

      {!req && (
        <Card>
          <CardHead title="1 · Atom" />
          {atoms.length === 0 ? (
            <EmptyState icon="🧩" title="No curated atoms yet"
              sub="Curate atoms in Atom Curation (T6) first, then come back here." />
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
              <div>
                <label style={{ fontSize: 11, fontWeight: 600, color: T.muted, textTransform: "uppercase", letterSpacing: "0.1em", display: "block", marginBottom: 6 }}>
                  Atom
                </label>
                <select value={selectedAtomId} onChange={e => setSelectedAtomId(e.target.value)}
                  style={{ width: "100%", padding: "9px 12px", background: "#fff", border: `1px solid ${T.line}`, borderRadius: 8, color: T.body, fontSize: 13, fontFamily: sans }}>
                  <option value="">Select an atom…</option>
                  {atoms.map(a => (
                    <option key={a.atom_id} value={a.atom_id}>
                      {a.starred ? "★ " : ""}{a.tour_name} — {a.text.slice(0, 70)}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <Btn variant="primary" disabled={!selectedAtomId || creating} onClick={startRequest}>
                  {creating ? "Starting…" : "Continue"}
                </Btn>
              </div>
            </div>
          )}
        </Card>
      )}

      {req && req.status === "pending_goal" && (
        <Card>
          <CardHead title="2 · Choose a Goal" />
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
        // AA-469 Việc 4 — this card now ONLY shows while actively choosing (not once approved —
        // see the Write card's meta row below for the post-choice summary instead, mirroring
        // T7's breadcrumb pattern of collapsing a completed level rather than re-showing it in
        // full). Pick-then-confirm: clicking a card only sets pendingAngleIdx (highlight); a
        // separate "Confirm this angle" button below calls choose() — same 2-beat pattern as
        // SlotPickerPanel.tsx's atom-pick + "Start writing".
        <Card>
          <CardHead title={req.status === "reusable" ? "3 · Choose a Different Angle" : "3 · Choose an Angle"} />
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

      {req && req.status === "approved" && !req.channel && (
        // AA-469 Việc 4 (flow-order fix) — NEW step 4: channel, AFTER the angle. Pick-then-confirm,
        // same pattern as step 3's angle cards — click a channel to highlight it, "Confirm channel"
        // submits. Only shows once an angle is chosen but no channel is set yet; on a reopen +
        // re-choice cycle this card is skipped entirely (channel carries over, see module header).
        <Card>
          <CardHead title="4 · Channel" />
          {chosenAngle && (
            <div style={{ fontSize: 12.5, color: T.muted, marginBottom: 14 }}>
              <strong>Angle:</strong> {chosenAngle.name} <span style={{ color: T.muted2 }}>·</span>{" "}
              <strong>Goal:</strong> {req.goal}
            </div>
          )}
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 14 }}>
            {CHANNELS.map(ch => (
              <button key={ch} onClick={() => setSelectedChannel(ch)} style={{
                padding: "7px 12px", borderRadius: 8, cursor: "pointer", fontFamily: sans,
                border: `1px solid ${selectedChannel === ch ? T.gold : T.line}`,
                background: selectedChannel === ch ? T.goldTint : "#fff",
                color: selectedChannel === ch ? "#8A5A16" : T.muted,
                fontSize: 12.5, fontWeight: selectedChannel === ch ? 700 : 400,
              }}>{ch}</button>
            ))}
          </div>
          {channelError && (
            <div style={{ padding: "9px 12px", background: T.redSoft, border: "1px solid #F5C6C6", borderRadius: 8, fontSize: 12, color: T.red, marginBottom: 10 }}>
              {channelError}
            </div>
          )}
          <Btn variant="primary" disabled={!selectedChannel || settingChannel} onClick={submitChannel}>
            {settingChannel ? "Saving…" : "Confirm channel"}
          </Btn>
        </Card>
      )}

      {req && req.status === "approved" && req.channel && (
        <Card>
          <CardHead title="5 · Write" />

          {chosenAngle && (
            // AA-469 Việc 4 — compact summary of what step 3 decided, replacing the old
            // full re-render of the angle card here. "Change angle" (AA-497) lives here as a
            // first-class action, not a bolted-on extra — same reopen() call the Stepper's "3
            // Angle" crumb triggers, just a second, more visible entry point right next to the
            // result it affects. Hidden while `writing` to avoid racing a background write with
            // a reopen (reopen requires 'approved', so a mid-write click would only ever hit a
            // harmless 409 — hiding it is just cleaner than surfacing that edge case).
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
            // AA-466 — hit the 180s FE poll ceiling. NOT a failure: the background task keeps
            // running regardless (same "backend keeps working" precedent as the pre-202 504
            // case) — this only means the FE gave up watching, not that anything broke.
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
            // AA-466 — a real system error in the background task, NOT a quality-gate hold: no
            // usable content_text was produced, so there's nothing to review — offer Retry
            // instead. held_reason carries an internal exception message here (unlike a real
            // 'held' piece, where it's tenant-facing gate feedback) — deliberately not shown.
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
