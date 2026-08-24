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
// Full 9-step workflow, all in this one component now:
//   1. Pick an atom (from this tenant's own curated T6 atoms) + a channel.
//   2. Pick a Goal from the 8-value list.
//   3-6. Backend auto-applies fixed brand audience, formula, generates 3 angles, recommends one.
//   7. Tenant picks one of the 3 (recommended or not) — the real gate. status -> approved.
//   8. AUTOMATICALLY, no extra click: fires the T9 write call the instant step 7 resolves.
//   9. ONE loading state while T9 writes + T10 checks (up to 2 attempts, inline, server-side —
//      see docs/claude_audit/AA-450-01-t9-t10-retry-loop-investigation.md) -> final result.
//
// API (via /api/tenant proxy -> Authorization: Bearer <cis_tenant_token>, tenant_id always
// resolved from the JWT):
//   GET  /api/tenant/v1/angle-gate/goals
//   POST /api/tenant/v1/angle-gate/requests                       {atom_id, channel}
//   POST /api/tenant/v1/angle-gate/requests/{id}/goal              {goal}
//   GET  /api/tenant/v1/angle-gate/requests/{id}
//   POST /api/tenant/v1/angle-gate/requests/{id}/choose            {idx}
//   POST /api/tenant/v1/content-writing/requests/{id}/write         {cta?}  — AA-450, step 8-9
//
// Atom picker reuses the same tenant-scoped atom list T6 (AtomsTab.tsx) already established
// (GET /api/tenant/admin/atoms) — no new atom-listing endpoint needed for this.

import { useState, useEffect, useCallback } from "react";
import { Sparkles, CheckCircle2, RotateCcw, AlertTriangle } from "lucide-react";
import { T, serif, sans, mono, Card, CardHead, Badge, Btn, LoadingScreen, EmptyState, Spinner } from "./ui";

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
  channel: string;
  goal: string | null;
  cta: string | null; // AA-450 migration 114 — usually null today, see that migration's header
  status: "pending_goal" | "pending_choice" | "approved";
  angles: AngleOption[];
}

// AA-450 — mirrors services/acp_content_writing/service.py::_row_to_dict()'s response shape.
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
  status: "approved" | "held";
  held_reason: string | null;
  gate_ledger: GateLedgerEntry[];
}

export default function AngleGateTab() {
  const [atoms, setAtoms] = useState<Atom[]>([]);
  const [goals, setGoals] = useState<Goal[]>([]);
  const [atomsLoading, setAtomsLoading] = useState(true);

  const [selectedAtomId, setSelectedAtomId] = useState("");
  const [selectedChannel, setSelectedChannel] = useState("facebook");
  const [selectedGoal, setSelectedGoal] = useState("");

  const [req, setReq] = useState<AngleGateRequest | null>(null);
  const [creating, setCreating] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [choosing, setChoosing] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  // AA-450 — step 8-9: write + inline T10 check, chained automatically after choose() resolves.
  const [piece, setPiece] = useState<ContentPiece | null>(null);
  const [writing, setWriting] = useState(false);
  const [writeError, setWriteError] = useState<string | null>(null);
  const [needsCtaInput, setNeedsCtaInput] = useState(false); // 422 fallback — see writeContent()
  const [ctaInput, setCtaInput] = useState("");

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

  const startRequest = useCallback(() => {
    if (!selectedAtomId) { setError("Pick an atom first."); return; }
    setCreating(true); setError(null);
    fetch("/api/tenant/v1/angle-gate/requests", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ atom_id: selectedAtomId, channel: selectedChannel }),
    })
      .then(async r => (r.ok ? r.json() : Promise.reject(await r.json().catch(() => ({})))))
      .then(d => setReq({ ...d, goal: null, angles: [] }))
      .catch(e => setError(e.detail ?? "Couldn't start a request — try again."))
      .finally(() => setCreating(false));
  }, [selectedAtomId, selectedChannel]);

  const submitGoal = useCallback(() => {
    if (!req || !selectedGoal) return;
    setGenerating(true); setError(null);
    fetch(`/api/tenant/v1/angle-gate/requests/${req.request_id}/goal`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ goal: selectedGoal }),
    })
      .then(async r => (r.ok ? r.json() : Promise.reject(await r.json().catch(() => ({})))))
      .then(d => setReq(d))
      .catch(e => setError(e.detail ?? "Couldn't generate angles — try again."))
      .finally(() => setGenerating(false));
  }, [req, selectedGoal]);

  // AA-450 — step 9. `cta` is only ever sent to override a NULL angle_gate_request.cta (the
  // realistic case today, see migration 114's header comment) — never overrides a real one.
  const writeContent = useCallback((requestId: string, cta?: string) => {
    setWriting(true); setWriteError(null); setNeedsCtaInput(false);
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
          setNeedsCtaInput(true);
          throw new Error("cta_needed");
        }
        return r.ok ? r.json() : Promise.reject(await r.json().catch(() => ({})));
      })
      .then(d => setPiece(d))
      .catch(e => {
        if (e instanceof Error && e.message === "cta_needed") return; // handled above, not an error banner
        setWriteError(e.detail ?? "Couldn't write content — try again.");
      })
      .finally(() => setWriting(false));
  }, []);

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
        // AA-450 step 8 — automatic, no extra tenant click: fires the moment status flips to
        // 'approved', the exact "1 loading, then final result" architecture Nghiep confirmed.
        if (d.status === "approved") writeContent(d.request_id);
      })
      .catch(e => setError(e.detail ?? "Couldn't save your choice — try again."))
      .finally(() => setChoosing(null));
  }, [req, writeContent]);

  const reset = useCallback(() => {
    setReq(null); setSelectedAtomId(""); setSelectedGoal(""); setError(null);
    setPiece(null); setWriteError(null); setNeedsCtaInput(false); setCtaInput("");
  }, []);

  if (atomsLoading) return <LoadingScreen message="Loading atoms…" />;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20, maxWidth: 720 }}>
      <p style={{ fontSize: 12, color: T.muted, margin: 0, lineHeight: 1.5 }}>
        Pick an atom and a channel, choose a content goal, then pick 1 of the 3 angles the
        system generates. You always choose — Adventure Asia never approves or blocks this
        for you.
      </p>

      {error && (
        <div style={{ padding: "9px 12px", background: T.redSoft, border: "1px solid #F5C6C6", borderRadius: 8, fontSize: 12, color: T.red }}>
          {error}
        </div>
      )}

      {!req && (
        <Card>
          <CardHead title="1 · Atom + Channel" />
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
                <label style={{ fontSize: 11, fontWeight: 600, color: T.muted, textTransform: "uppercase", letterSpacing: "0.1em", display: "block", marginBottom: 6 }}>
                  Channel
                </label>
                <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
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
          <CardHead title="2 · Choose a Goal" action={
            <Btn variant="ghost" size="sm" onClick={reset}><RotateCcw size={12} /> Start over</Btn>
          } />
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

      {req && (req.status === "pending_choice" || req.status === "approved") && (
        <Card>
          <CardHead title={req.status === "approved" ? "Approved" : "3 · Choose an Angle"} action={
            <Btn variant="ghost" size="sm" onClick={reset}><RotateCcw size={12} /> Start over</Btn>
          } />
          {req.status === "approved" && (
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 14, color: T.green, fontSize: 13 }}>
              <CheckCircle2 size={16} /> Angle chosen — writing the final piece below.
            </div>
          )}
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            {req.angles.map(a => (
              <div key={a.idx} style={{
                padding: "14px 16px", borderRadius: 10,
                border: `1px solid ${a.chosen ? T.green : a.recommended ? T.gold : T.line}`,
                background: a.chosen ? T.greenSoft : a.recommended ? T.goldTint : "#fff",
              }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
                  <span style={{ fontFamily: serif, fontSize: 15, fontWeight: 600, color: T.ink }}>{a.name}</span>
                  {a.recommended && <Badge variant="default">Recommended</Badge>}
                  {a.chosen && <Badge variant="default">Chosen</Badge>}
                </div>
                <div style={{ fontSize: 12.5, color: T.body, marginBottom: 4 }}>
                  <strong>Why it works:</strong> {a.why_it_works}
                </div>
                <div style={{ fontSize: 12.5, color: T.body, marginBottom: 4 }}>
                  <strong>Formula fit:</strong> <span style={{ fontFamily: mono }}>{a.formula_fit}</span>
                </div>
                <div style={{ fontSize: 12.5, color: T.body, marginBottom: 10 }}>
                  <strong>Best final style:</strong> {a.best_final_style}
                </div>
                {req.status === "pending_choice" && (
                  <Btn size="sm" variant={a.recommended ? "primary" : "secondary"}
                    disabled={choosing !== null} onClick={() => choose(a.idx)}>
                    {choosing === a.idx ? "Saving…" : "Choose this angle"}
                  </Btn>
                )}
              </div>
            ))}
          </div>
        </Card>
      )}

      {req && req.status === "approved" && (
        <Card>
          <CardHead title="4 · Content" />

          {writing && !piece && (
            <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "10px 4px", color: T.muted, fontSize: 13 }}>
              <Spinner size={16} /> Writing and checking your content — one moment…
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

          {piece && !writing && (
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

              <div style={{ fontSize: 11, color: T.muted, display: "flex", flexWrap: "wrap", gap: "4px 14px" }}>
                <span><strong>Goal:</strong> {req.goal}</span>
                <span><strong>Channel:</strong> {req.channel}</span>
                {req.cta && <span><strong>CTA:</strong> {req.cta}</span>}
              </div>
            </div>
          )}
        </Card>
      )}
    </div>
  );
}
