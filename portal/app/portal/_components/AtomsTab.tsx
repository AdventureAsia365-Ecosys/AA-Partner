"use client";
// app/(tenant)/portal/_components/AtomsTab.tsx — AA-431 (T6 Atom Curation, tenant-facing)
// API (via /api/tenant proxy -> Authorization: Bearer <cis_tenant_token> -> backend
// resolves owner_scope from the JWT, see api/routers/admin_atoms.py::_resolve_atom_owner_scope
// — no owner_scope is ever sent by this component, the backend derives it from the token):
//   GET   /api/tenant/admin/atoms/summary
//   GET   /api/tenant/admin/atoms?limit=&offset=&distinctiveness=&unreviewed_only=
//   PATCH /api/tenant/admin/atoms/{atom_id}
//
// Deliberately NOT a copy of app/admin/curation/page.tsx (826 lines, staff tool that
// browses every owner_scope across the whole platform) — this is tenant-scoped to the
// caller's own atoms only (the backend enforces the scoping, not this component), so no
// tour-batch-budget pagination, no cross-tenant tooling, no bulk multi-select, no
// platform delete-forever messaging. Reuses this portal's own ui.tsx tokens
// (Card/Badge/Btn/LoadingScreen/EmptyState), not adminUi.tsx — a deliberately smaller
// tool for a tenant curating their own handful of tours, not AA staff curating hundreds.

import { useState, useEffect, useCallback } from "react";
import { Star, Trash2 } from "lucide-react";
import { T, serif, sans, mono, Card, Badge, Btn, LoadingScreen, EmptyState } from "./ui";

interface Atom {
  atom_id: string;
  tour_id: string;
  tour_name: string;
  text: string;
  activity_type: string | null;
  distinctiveness: "HIGH" | "MED" | "LOW";
  starred: boolean;
  deleted: boolean;
  unreviewed: boolean;
}

interface Summary {
  distinctiveness_breakdown: { HIGH: number; MED: number; LOW: number };
  total_count: number;
  reviewed_count: number;
}

const DIST_VARIANT: Record<string, "success" | "warning" | "default"> = {
  HIGH: "success", MED: "warning", LOW: "default",
};

const PAGE_SIZE = 50;

export default function AtomsTab() {
  const [summary, setSummary] = useState<Summary | null>(null);
  const [atoms, setAtoms] = useState<Atom[]>([]);
  const [total, setTotal] = useState(0);
  const [distinctiveness, setDistinctiveness] = useState("");
  const [unreviewedOnly, setUnreviewedOnly] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);

  const loadSummary = useCallback(() => {
    fetch("/api/tenant/admin/atoms/summary")
      .then(r => (r.ok ? r.json() : null))
      .then(d => { if (d) setSummary(d); })
      .catch(() => {});
  }, []);

  const loadAtoms = useCallback((offset: number, append: boolean) => {
    if (append) setLoadingMore(true); else setLoading(true);
    const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset) });
    if (distinctiveness) params.set("distinctiveness", distinctiveness);
    if (unreviewedOnly) params.set("unreviewed_only", "true");
    fetch(`/api/tenant/admin/atoms?${params}`)
      .then(r => (r.ok ? r.json() : null))
      .then(d => {
        if (!d) return;
        setAtoms(prev => (append ? [...prev, ...d.atoms] : d.atoms));
        setTotal(d.total ?? 0);
      })
      .finally(() => { setLoading(false); setLoadingMore(false); });
  }, [distinctiveness, unreviewedOnly]);

  useEffect(() => { loadSummary(); }, [loadSummary]);
  useEffect(() => { loadAtoms(0, false); }, [loadAtoms]);

  async function toggleStar(atom: Atom) {
    const next = !atom.starred;
    setAtoms(prev => prev.map(a => (a.atom_id === atom.atom_id ? { ...a, starred: next } : a)));
    await fetch(`/api/tenant/admin/atoms/${atom.atom_id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ starred: next }),
    });
  }

  async function deleteAtom(atom: Atom) {
    if (!confirm("Remove this atom from your content pool? It will no longer be used in future scheduling.")) return;
    setAtoms(prev => prev.filter(a => a.atom_id !== atom.atom_id));
    await fetch(`/api/tenant/admin/atoms/${atom.atom_id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ deleted: true }),
    });
    loadSummary();
  }

  if (loading) return <LoadingScreen message="Loading your atoms…" />;

  return (
    <div>
      <div style={{ marginBottom: 22 }}>
        <h2 style={{ fontFamily: serif, fontSize: 22, fontWeight: 500, color: T.ink, margin: "0 0 6px", letterSpacing: "-0.01em" }}>
          Atom Curation
        </h2>
        <p style={{ fontSize: 13, color: T.muted, lineHeight: 1.6, margin: 0 }}>
          Review the content atoms extracted from your rewritten tours. Starred atoms are prioritized
          in future scheduling; removed atoms are excluded entirely.
        </p>
      </div>

      {summary && (
        <div style={{ display: "flex", gap: 22, marginBottom: 22, flexWrap: "wrap" }}>
          <StatBlock label="Total atoms" value={summary.total_count} />
          <StatBlock label="Reviewed" value={summary.reviewed_count} />
          <StatBlock label="High distinctiveness" value={summary.distinctiveness_breakdown.HIGH} accent={T.green} />
          <StatBlock label="Medium" value={summary.distinctiveness_breakdown.MED} accent={T.amber} />
          <StatBlock label="Low" value={summary.distinctiveness_breakdown.LOW} />
        </div>
      )}

      <div style={{ display: "flex", gap: 10, marginBottom: 16, alignItems: "center" }}>
        <select value={distinctiveness} onChange={e => setDistinctiveness(e.target.value)}
          style={{ padding: "8px 12px", background: T.card, border: `1px solid ${T.line}`, borderRadius: 8, fontSize: 13, fontFamily: sans, color: T.body, cursor: "pointer" }}>
          <option value="">All distinctiveness</option>
          <option value="HIGH">High</option>
          <option value="MED">Medium</option>
          <option value="LOW">Low</option>
        </select>
        <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13, color: T.body, cursor: "pointer" }}>
          <input type="checkbox" checked={unreviewedOnly} onChange={e => setUnreviewedOnly(e.target.checked)} />
          Unreviewed only
        </label>
      </div>

      {atoms.length === 0 ? (
        <EmptyState icon="🧩" title="No atoms yet"
          sub="Atoms are generated automatically once one of your rewritten tours passes QA. Rewrite a tour from Browse Pool to get started." />
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {atoms.map(atom => (
            <Card key={atom.atom_id} style={{ padding: "14px 18px" }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12 }}>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 11, color: T.muted2, marginBottom: 4, fontFamily: mono }}>{atom.tour_name}</div>
                  <div style={{ fontSize: 13.5, color: T.body, lineHeight: 1.5 }}>{atom.text}</div>
                  <div style={{ display: "flex", gap: 8, marginTop: 8, flexWrap: "wrap" }}>
                    <Badge variant={DIST_VARIANT[atom.distinctiveness] ?? "default"}>{atom.distinctiveness}</Badge>
                    {atom.activity_type && <Badge>{atom.activity_type}</Badge>}
                    {atom.unreviewed && <Badge variant="warning">New</Badge>}
                  </div>
                </div>
                <div style={{ display: "flex", gap: 6, flexShrink: 0 }}>
                  <button onClick={() => toggleStar(atom)} title={atom.starred ? "Unstar" : "Star"}
                    style={{ background: atom.starred ? T.goldTint : "none", border: `1px solid ${atom.starred ? T.goldSoft : T.line}`, borderRadius: 6, padding: 6, cursor: "pointer", color: atom.starred ? T.gold : T.muted2, display: "flex" }}>
                    <Star size={14} fill={atom.starred ? T.gold : "none"} />
                  </button>
                  <button onClick={() => deleteAtom(atom)} title="Remove"
                    style={{ background: "none", border: `1px solid ${T.line}`, borderRadius: 6, padding: 6, cursor: "pointer", color: T.red, display: "flex" }}>
                    <Trash2 size={14} />
                  </button>
                </div>
              </div>
            </Card>
          ))}
        </div>
      )}

      {atoms.length < total && (
        <div style={{ textAlign: "center", marginTop: 16 }}>
          <Btn variant="secondary" disabled={loadingMore} onClick={() => loadAtoms(atoms.length, true)}>
            {loadingMore ? "Loading…" : `Load more (${atoms.length} / ${total})`}
          </Btn>
        </div>
      )}
    </div>
  );
}

function StatBlock({ label, value, accent }: { label: string; value: number; accent?: string }) {
  return (
    <div style={{ minWidth: 100 }}>
      <div style={{ fontSize: 10, color: T.muted2, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 3 }}>{label}</div>
      <div style={{ fontFamily: serif, fontSize: 22, fontWeight: 500, color: accent ?? T.ink }}>{value}</div>
    </div>
  );
}
