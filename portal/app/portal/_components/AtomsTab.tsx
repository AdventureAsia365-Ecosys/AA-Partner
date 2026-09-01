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
//
// AA-509 — Segment: `segment_id`/`segment_canonical_*` come from admin_atoms.py's LEFT JOIN onto
// acp_contract.atom_segment(_member) — NULL for an atom not yet grouped (pre-migration-129, or
// segment_matching.py hasn't run since). Grouping below is a pure render-layer transform over
// whatever page of atoms is already loaded (filter/select/star/delete stay entirely per-atom,
// unchanged) — a Segment whose members straddle two paginated pages won't group into one header,
// a known limitation at this tool's current per-tenant volumes (see implementation notes).

import { useState, useEffect, useCallback } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Star, Trash2, BookOpen, ArrowLeft, ChevronDown, ChevronRight, Layers } from "lucide-react";
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
  segment_id: string | null;
  canonical_place: string | null;
  canonical_action: string | null;
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
  // AA-454 — arriving from CatalogTab's "View atoms →" link (?tour_id=...). The backend
  // (api/routers/admin_atoms.py::list_atoms) already accepts tour_id as a real filter param —
  // no new backend endpoint needed, just wiring this component to use it.
  const searchParams = useSearchParams();
  const tourIdFilter = searchParams.get("tour_id");

  const [summary, setSummary] = useState<Summary | null>(null);
  const [atoms, setAtoms] = useState<Atom[]>([]);
  const [total, setTotal] = useState(0);
  const [distinctiveness, setDistinctiveness] = useState("");
  const [unreviewedOnly, setUnreviewedOnly] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  // AA-509 — Segment header collapse state, keyed by segment_id. Empty set = everything expanded
  // (default), same "nothing hidden by default" behavior this page had before grouping existed.
  const [collapsedSegments, setCollapsedSegments] = useState<Set<string>>(new Set());

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
    if (tourIdFilter) params.set("tour_id", tourIdFilter);
    fetch(`/api/tenant/admin/atoms?${params}`)
      .then(r => (r.ok ? r.json() : null))
      .then(d => {
        if (!d) return;
        setAtoms(prev => (append ? [...prev, ...d.atoms] : d.atoms));
        setTotal(d.total ?? 0);
      })
      .finally(() => { setLoading(false); setLoadingMore(false); });
  }, [distinctiveness, unreviewedOnly, tourIdFilter]);

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

  const filterTourName = tourIdFilter ? atoms[0]?.tour_name : undefined;

  return (
    <div>
      {/* AA-454 — T4<->T6 nav (was zero navigation either direction). Always visible, not
          just when filtered — the audit gap was "no nav" full stop, not "no nav while filtered". */}
      <Link href={tourIdFilter ? `/portal/t4-pool?tour_id=${tourIdFilter}` : "/portal/t4-pool"} style={{
        display: "inline-flex", alignItems: "center", gap: 5, fontSize: 12.5, fontWeight: 600,
        color: T.muted, textDecoration: "none", marginBottom: 14,
      }}>
        <ArrowLeft size={13} /> My Catalog
      </Link>

      <div style={{ marginBottom: 22 }}>
        <h2 style={{ fontFamily: serif, fontSize: 22, fontWeight: 500, color: T.ink, margin: "0 0 6px", letterSpacing: "-0.01em" }}>
          Atom Curation
        </h2>
        <p style={{ fontSize: 13, color: T.muted, lineHeight: 1.6, margin: 0 }}>
          Review the content atoms extracted from your rewritten tours. Starred atoms are prioritized
          in future scheduling; removed atoms are excluded entirely.
        </p>
      </div>

      {tourIdFilter && (
        <div style={{
          display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10,
          padding: "9px 14px", marginBottom: 16, borderRadius: 8,
          background: T.goldTint, border: `1px solid ${T.goldSoft}`, fontFamily: sans,
        }}>
          <span style={{ fontSize: 12, color: T.body }}>
            <BookOpen size={12} style={{ verticalAlign: -2, marginRight: 5 }} />
            Showing atoms for: <strong>{filterTourName ?? "this tour"}</strong>
          </span>
          <Link href="/portal/t6-atoms" style={{ fontSize: 12, fontWeight: 600, color: T.body, textDecoration: "none" }}>
            Clear filter ×
          </Link>
        </div>
      )}

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
          {groupBySegment(atoms).map(row =>
            row.kind === "atom" ? (
              <AtomCard key={row.atom.atom_id} atom={row.atom} onStar={toggleStar} onDelete={deleteAtom} />
            ) : (
              <SegmentGroup
                key={row.segmentId}
                segmentId={row.segmentId}
                place={row.place}
                action={row.action}
                atoms={row.atoms}
                collapsed={collapsedSegments.has(row.segmentId)}
                onToggle={() => setCollapsedSegments(prev => {
                  const next = new Set(prev);
                  next.has(row.segmentId) ? next.delete(row.segmentId) : next.add(row.segmentId);
                  return next;
                })}
                onStar={toggleStar}
                onDelete={deleteAtom}
              />
            )
          )}
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

// AA-509 — one row of the rendered list: either a lone atom (no Segment, or a Segment with only
// one member in the currently-loaded page) or a Segment header wrapping >=2 atoms. Grouping is
// evaluated over the atoms already loaded, in their existing order — a Segment split across two
// paginated pages just renders as separate singleton rows on each page (known limitation, see
// implementation notes).
type AtomRow =
  | { kind: "atom"; atom: Atom }
  | { kind: "segment"; segmentId: string; place: string; action: string; atoms: Atom[] };

function groupBySegment(atoms: Atom[]): AtomRow[] {
  const bySegment = new Map<string, Atom[]>();
  for (const atom of atoms) {
    if (!atom.segment_id) continue;
    const list = bySegment.get(atom.segment_id) ?? [];
    list.push(atom);
    bySegment.set(atom.segment_id, list);
  }

  const rows: AtomRow[] = [];
  const emitted = new Set<string>();
  for (const atom of atoms) {
    const members = atom.segment_id ? bySegment.get(atom.segment_id) : undefined;
    if (members && members.length > 1) {
      if (emitted.has(atom.segment_id!)) continue;
      emitted.add(atom.segment_id!);
      rows.push({
        kind: "segment",
        segmentId: atom.segment_id!,
        place: members[0].canonical_place ?? "",
        action: members[0].canonical_action ?? "",
        atoms: members,
      });
    } else {
      rows.push({ kind: "atom", atom });
    }
  }
  return rows;
}

function AtomCard({ atom, onStar, onDelete }: {
  atom: Atom; onStar: (a: Atom) => void; onDelete: (a: Atom) => void;
}) {
  return (
    <Card style={{ padding: "14px 18px" }}>
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
          <button onClick={() => onStar(atom)} title={atom.starred ? "Unstar" : "Star"}
            style={{ background: atom.starred ? T.goldTint : "none", border: `1px solid ${atom.starred ? T.goldSoft : T.line}`, borderRadius: 6, padding: 6, cursor: "pointer", color: atom.starred ? T.gold : T.muted2, display: "flex" }}>
            <Star size={14} fill={atom.starred ? T.gold : "none"} />
          </button>
          <button onClick={() => onDelete(atom)} title="Remove"
            style={{ background: "none", border: `1px solid ${T.line}`, borderRadius: 6, padding: 6, cursor: "pointer", color: T.red, display: "flex" }}>
            <Trash2 size={14} />
          </button>
        </div>
      </div>
    </Card>
  );
}

function SegmentGroup({ place, action, atoms, collapsed, onToggle, onStar, onDelete }: {
  segmentId: string; place: string; action: string; atoms: Atom[]; collapsed: boolean;
  onToggle: () => void; onStar: (a: Atom) => void; onDelete: (a: Atom) => void;
}) {
  return (
    <div style={{ border: `1px solid ${T.line}`, borderRadius: 10, overflow: "hidden" }}>
      <button onClick={onToggle} style={{
        width: "100%", display: "flex", alignItems: "center", gap: 8, padding: "10px 14px",
        background: T.goldTint, border: "none", cursor: "pointer", textAlign: "left",
      }}>
        {collapsed ? <ChevronRight size={14} color={T.muted} /> : <ChevronDown size={14} color={T.muted} />}
        <Layers size={13} color={T.gold} />
        <span style={{ fontSize: 13, fontWeight: 600, color: T.body, fontFamily: sans }}>
          {place}{action ? ` — ${action}` : ""}
        </span>
        <span style={{ fontSize: 11.5, color: T.muted2, marginLeft: "auto" }}>
          {atoms.length} atoms, same moment
        </span>
      </button>
      {!collapsed && (
        <div style={{ display: "flex", flexDirection: "column", gap: 8, padding: 8, background: T.card }}>
          {atoms.map(atom => (
            <AtomCard key={atom.atom_id} atom={atom} onStar={onStar} onDelete={onDelete} />
          ))}
        </div>
      )}
    </div>
  );
}
