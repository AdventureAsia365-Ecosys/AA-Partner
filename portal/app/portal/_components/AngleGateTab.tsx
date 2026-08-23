"use client";
// app/(tenant)/portal/_components/AngleGateTab.tsx — AA-449 (T8 Angle Gate, tenant-facing)
//
// Per ADR-2026-038 §0.2/§10.3 (tenant self-service — AA does not gate tenant content; the T8
// "gate" is the TENANT choosing, never AA) + STEP0 §2 (terminology): this component always says
// "Goal" for the 8-value list (Bang 1) and "Angle" only for the 3 LLM-generated options per
// request — never mixes the two.
//
// Workflow (docs/claude_tasks/AA-449-01-build-t8-angle-gate.md):
//   1. Pick an atom (from this tenant's own curated T6 atoms) + a channel.
//   2. Pick a Goal from the 8-value list.
//   3-6. Backend auto-applies fixed brand audience, formula, generates 3 angles, recommends one.
//   7. Tenant picks one of the 3 (recommended or not) — the real gate. status -> approved.
//
// API (via /api/tenant proxy -> Authorization: Bearer <cis_tenant_token>, api/routers/
// v1_angle_gate.py — tenant_id always resolved from the JWT):
//   GET  /api/tenant/v1/angle-gate/goals
//   POST /api/tenant/v1/angle-gate/requests                    {atom_id, channel}
//   POST /api/tenant/v1/angle-gate/requests/{id}/goal           {goal}
//   GET  /api/tenant/v1/angle-gate/requests/{id}
//   POST /api/tenant/v1/angle-gate/requests/{id}/choose         {idx}
//
// Atom picker reuses the same tenant-scoped atom list T6 (AtomsTab.tsx) already established
// (GET /api/tenant/admin/atoms) — no new atom-listing endpoint needed for this.

import { useState, useEffect, useCallback } from "react";
import { Sparkles, CheckCircle2, RotateCcw } from "lucide-react";
import { T, serif, sans, mono, Card, CardHead, Badge, Btn, LoadingScreen, EmptyState } from "./ui";

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
  status: "pending_goal" | "pending_choice" | "approved";
  angles: AngleOption[];
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

  const choose = useCallback((idx: number) => {
    if (!req) return;
    setChoosing(idx); setError(null);
    fetch(`/api/tenant/v1/angle-gate/requests/${req.request_id}/choose`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ idx }),
    })
      .then(async r => (r.ok ? r.json() : Promise.reject(await r.json().catch(() => ({})))))
      .then(d => setReq(d))
      .catch(e => setError(e.detail ?? "Couldn't save your choice — try again."))
      .finally(() => setChoosing(null));
  }, [req]);

  const reset = useCallback(() => {
    setReq(null); setSelectedAtomId(""); setSelectedGoal(""); setError(null);
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
              <CheckCircle2 size={16} /> You&rsquo;ve chosen an angle — ready for content writing (T9).
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
    </div>
  );
}
