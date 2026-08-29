"use client";
// app/(tenant)/portal/_components/SlotPickerPanel.tsx — AA-494 Step 5 (T7 slot-view + T8
// atom-picker), design doc `docs/claude_tasks/AA-494-design-atom-angle-piece-reuse.md` Decision
// 6's approved UI/UX section.
//
// 4-level drill-down: Year (4 quarters shown) -> pick quarter -> 3 months -> pick month ->
// 4 weeks -> pick week -> slot-cards (1 per compute_slot_grid() slot that landed in that week) ->
// click a slot -> "Suggested for this slot" (its own atom_ids, enriched) + "Other atoms free this
// month" (every tenant atom not locked this month, Decision 6's atom-availability rule) shown as
// two visually separate areas, never merged. Picking an atom only highlights it — a fixed
// "Start writing" button (disabled until an atom is picked) hands off to the real T8 flow by
// navigating to /portal/t8-angle-gate?atom_id=... (AngleGateTab.tsx reads this via
// useSearchParams(), same pattern AtomsTab.tsx already uses for ?tour_id= — see that file).
//
// Suggestion, never a gate (Decision 6's revised product model, confirmed with Nghiep 29/08):
// picking a "free" atom outside its suggested slot is exactly as valid as picking a suggested
// one — create_request() (T8) is unchanged, still accepts any atom_id. This panel never disables
// or blocks a free-atom pick.
//
// API: GET /api/tenant/v1/planning/slot-suggestions?year=&month= (api/routers/v1_planning.py,
// AA-494 Step 4) — 404 means the quarter hasn't been finalized yet (see PlanningTab.tsx's own
// Quarter Plan section above this one on the same page).
//
// capacity_posts_per_week is read from the endpoint's own response (tenant-configurable,
// shared.tenants.posts_per_week, AA-384) — never hardcoded here.

import { useState, useCallback, useMemo } from "react";
import { useRouter } from "next/navigation";
import { ChevronRight, CheckCircle2, Search, Sparkles } from "lucide-react";
import { T, serif, sans, mono, Card, CardHead, Badge, Btn, LoadingScreen, EmptyState, fmtDate } from "./ui";

interface Slot {
  slot_id: string;
  week: number;
  channel: string;
  kind: "evergreen" | "campaign" | "reactive_hold";
  trip_id: string | null;
  atom_ids: string[];
  funnel_stage: string;
  framework: string | null;
  cta_target: string | null;
  topic_hint: string | null;
  keyword_seed: string | null;
}

interface AtomDetail {
  atom_id: string;
  trip_id: string;
  trip_name: string | null;
  destination: string | null;
  text: string;
  activity_type: string | null;
  distinctiveness: "HIGH" | "MED" | "LOW";
}

// AA-497 — request_id lets an already-written slot offer "Change angle" (reopen the request
// behind it), see SlotCard/SlotExpandedPanel below.
interface UsedAtom { used_at: string; channel: string; request_id: string; }

interface SlotSuggestionsResponse {
  slot_grid: { year: number; month: number; slots: Slot[]; capacity_note: string | null };
  atoms_by_id: Record<string, AtomDetail>;
  used_atoms: Record<string, UsedAtom>;
  free_atom_ids: string[];
  capacity_posts_per_week: number;
}

const MONTH_NAMES = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

function quarterMonths(quarter: number): number[] {
  return [(quarter - 1) * 3 + 1, (quarter - 1) * 3 + 2, (quarter - 1) * 3 + 3];
}

const now = new Date();
const DEFAULT_YEAR = now.getFullYear();

type Level = "quarter" | "month" | "week" | "detail";

export default function SlotPickerPanel() {
  const [year, setYear] = useState(DEFAULT_YEAR);
  const [quarter, setQuarter] = useState<number | null>(null);
  const [month, setMonth] = useState<number | null>(null);
  const [week, setWeek] = useState<number | null>(null);

  const [data, setData] = useState<SlotSuggestionsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [notFinalized, setNotFinalized] = useState(false);

  const level: Level = week != null ? "detail" : month != null ? "week" : quarter != null ? "month" : "quarter";

  const pickQuarter = useCallback((q: number) => {
    setQuarter(q); setMonth(null); setWeek(null); setData(null); setNotFinalized(false);
  }, []);

  const pickMonth = useCallback((m: number) => {
    setMonth(m); setWeek(null); setData(null); setNotFinalized(false); setLoading(true);
    fetch(`/api/tenant/v1/planning/slot-suggestions?year=${year}&month=${m}`)
      .then(async r => {
        if (r.status === 404) { setNotFinalized(true); return null; }
        if (!r.ok) return Promise.reject();
        return r.json();
      })
      .then(d => { if (d) setData(d); })
      .catch(() => setNotFinalized(true))
      .finally(() => setLoading(false));
  }, [year]);

  const pickWeek = useCallback((w: number) => setWeek(w), []);

  const jumpTo = useCallback((l: Level) => {
    if (l === "quarter") { setQuarter(null); setMonth(null); setWeek(null); }
    else if (l === "month") { setMonth(null); setWeek(null); }
    else if (l === "week") { setWeek(null); }
  }, []);

  const changeYear = useCallback((y: number) => {
    setYear(y); setQuarter(null); setMonth(null); setWeek(null); setData(null);
  }, []);

  return (
    <Card style={{ padding: "16px 18px", marginBottom: 18 }}>
      <CardHead title="Weekly Slots" />
      <p style={{ fontSize: 12.5, color: T.muted, lineHeight: 1.6, margin: "0 0 14px" }}>
        A pre-arranged priority for what to write each week — never a requirement. Pick any
        suggested atom, or any other atom that&rsquo;s still free this month, and start writing.
      </p>

      <Breadcrumb year={year} quarter={quarter} month={month} week={week} onJump={jumpTo} onYear={changeYear} />

      {level === "quarter" && <QuarterLevel onPick={pickQuarter} />}

      {level === "month" && quarter != null && <MonthLevel quarter={quarter} onPick={pickMonth} />}

      {loading && <LoadingScreen message="Loading this month's slots…" />}

      {notFinalized && !loading && (
        <EmptyState icon="🗓️" title="This quarter isn't finalized yet"
          sub="Finalize the Quarter Plan above for this year/quarter before browsing its weekly slots." />
      )}

      {level === "week" && data && !loading && (
        <WeekLevel data={data} onPick={pickWeek} />
      )}

      {level === "detail" && data && week != null && (
        <DetailLevel data={data} week={week} />
      )}
    </Card>
  );
}

function Breadcrumb({ year, quarter, month, week, onJump, onYear }: {
  year: number; quarter: number | null; month: number | null; week: number | null;
  onJump: (l: Level) => void; onYear: (y: number) => void;
}) {
  const crumbStyle = (active: boolean): React.CSSProperties => ({
    cursor: "pointer", fontFamily: sans, fontSize: 12.5, fontWeight: active ? 700 : 500,
    color: active ? T.ink : T.muted, background: "none", border: "none", padding: 0,
  });
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap", marginBottom: 16, padding: "8px 0", borderBottom: `1px solid ${T.line2}` }}>
      <select value={year} onChange={e => onYear(Number(e.target.value))} style={{
        ...crumbStyle(quarter == null), border: `1px solid ${T.line}`, borderRadius: 6, padding: "3px 6px", background: T.bg,
      }}>
        {[DEFAULT_YEAR - 1, DEFAULT_YEAR, DEFAULT_YEAR + 1].map(y => <option key={y} value={y}>{y}</option>)}
      </select>
      {quarter != null && (
        <>
          <ChevronRight size={13} color={T.muted2} />
          <button onClick={() => onJump("quarter")} style={crumbStyle(month == null)}>Q{quarter}</button>
        </>
      )}
      {month != null && (
        <>
          <ChevronRight size={13} color={T.muted2} />
          <button onClick={() => onJump("month")} style={crumbStyle(week == null)}>{MONTH_NAMES[month - 1]}</button>
        </>
      )}
      {week != null && (
        <>
          <ChevronRight size={13} color={T.muted2} />
          <button onClick={() => onJump("week")} style={crumbStyle(true)}>Week {week}</button>
        </>
      )}
    </div>
  );
}

function QuarterLevel({ onPick }: { onPick: (q: number) => void }) {
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: 10 }}>
      {[1, 2, 3, 4].map(q => (
        <button key={q} onClick={() => onPick(q)} style={{
          padding: "18px 14px", borderRadius: 10, cursor: "pointer", textAlign: "left",
          border: `1px solid ${T.line}`, background: T.bg, fontFamily: sans,
        }}>
          <div style={{ fontFamily: serif, fontSize: 20, fontWeight: 500, color: T.ink }}>Q{q}</div>
          <div style={{ fontSize: 11, color: T.muted, marginTop: 2 }}>
            {MONTH_NAMES[quarterMonths(q)[0] - 1]}–{MONTH_NAMES[quarterMonths(q)[2] - 1]}
          </div>
        </button>
      ))}
    </div>
  );
}

function MonthLevel({ quarter, onPick }: { quarter: number; onPick: (m: number) => void }) {
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: 10 }}>
      {quarterMonths(quarter).map(m => (
        <button key={m} onClick={() => onPick(m)} style={{
          padding: "18px 14px", borderRadius: 10, cursor: "pointer", textAlign: "left",
          border: `1px solid ${T.line}`, background: T.bg, fontFamily: sans,
        }}>
          <div style={{ fontFamily: serif, fontSize: 18, fontWeight: 500, color: T.ink }}>{MONTH_NAMES[m - 1]}</div>
        </button>
      ))}
    </div>
  );
}

function WeekLevel({ data, onPick }: { data: SlotSuggestionsResponse; onPick: (w: number) => void }) {
  // AA-494 close-out (Item 1) — `slot_grid.capacity_note` used to be rendered verbatim here.
  // Investigated: it's `allocator.py::compute_slot_grid()`'s own internal reasoning trail
  // (`_add_note()`, pipe-joined multi-trip log lines — "Trip 'X' atom floor: N live atoms <
  // 2xM planned slots for CHANNEL — capacity implicitly reduced, no silent atom repeat.", etc.),
  // never designed as display copy — confirmed by grep: nothing anywhere in this codebase
  // (admin or tenant) had ever rendered `SlotGrid.capacity_note` before this session's own
  // PlanningTab.tsx addition, unlike the separate `QuarterPlan.capacity_note` (quarter.py,
  // already shown above in the Trips section) which IS a genuine one-line tenant-appropriate
  // sentence ("N eligible trips at M posts/wk — focusing on K trips (applied)."). The two
  // fields share a name but not a design intent — this component mistakenly treated them the
  // same. Removed rather than reformatted: turning allocator.py's per-trip, whole-month-string
  // notes into real tenant copy (e.g. a per-slot/per-trip tooltip explaining a dropped slot)
  // needs its own structured backend field and design pass, not a same-PR string rewrite — see
  // docs/implementation-notes/AA-494.md's post-merge record for this flagged as a follow-up.
  return (
    <div>
      <div style={{ fontSize: 11.5, color: T.muted, marginBottom: 10 }}>
        Capacity: {data.capacity_posts_per_week} post{data.capacity_posts_per_week === 1 ? "" : "s"}/week
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: 10 }}>
        {[1, 2, 3, 4].map(w => {
          const slots = data.slot_grid.slots.filter(s => s.week === w);
          const written = slots.filter(s => s.atom_ids.some(id => data.used_atoms[id]));
          return (
            <button key={w} onClick={() => onPick(w)} style={{
              padding: "16px 14px", borderRadius: 10, cursor: "pointer", textAlign: "left",
              border: `1px solid ${T.line}`, background: T.bg, fontFamily: sans,
            }}>
              <div style={{ fontFamily: serif, fontSize: 17, fontWeight: 500, color: T.ink }}>Week {w}</div>
              <div style={{ fontSize: 11, color: T.muted, marginTop: 4 }}>
                {slots.length} slot{slots.length === 1 ? "" : "s"}
                {slots.length > 0 && ` · ${written.length} written`}
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}

function DetailLevel({ data, week }: { data: SlotSuggestionsResponse; week: number }) {
  const slots = useMemo(() => data.slot_grid.slots.filter(s => s.week === week), [data, week]);
  const [expandedSlotId, setExpandedSlotId] = useState<string | null>(slots[0]?.slot_id ?? null);

  if (slots.length === 0) {
    return <EmptyState icon="📭" title="No slots this week" sub="No trip had eligible atoms left for this week — nothing was planned here." />;
  }

  return (
    <div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 10, marginBottom: 16 }}>
        {slots.map((s, i) => (
          <SlotCard key={s.slot_id} slot={s} index={i} data={data}
            expanded={expandedSlotId === s.slot_id}
            onClick={() => setExpandedSlotId(expandedSlotId === s.slot_id ? null : s.slot_id)} />
        ))}
      </div>
      {expandedSlotId && (
        <SlotExpandedPanel slot={slots.find(s => s.slot_id === expandedSlotId)!} data={data} />
      )}
    </div>
  );
}

function SlotCard({ slot, index, data, expanded, onClick }: {
  slot: Slot; index: number; data: SlotSuggestionsResponse; expanded: boolean; onClick: () => void;
}) {
  const writtenAtomId = slot.atom_ids.find(id => data.used_atoms[id]);
  const written = writtenAtomId ? data.used_atoms[writtenAtomId] : null;
  const atom = writtenAtomId ? data.atoms_by_id[writtenAtomId] : null;

  return (
    <button onClick={onClick} style={{
      padding: "14px 14px", borderRadius: 10, cursor: "pointer", textAlign: "left", fontFamily: sans,
      border: `1px solid ${written ? T.green : expanded ? T.gold : T.line}`,
      background: written ? T.greenSoft : expanded ? T.goldTint : "#fff",
    }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 6 }}>
        <span style={{ fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.08em", color: T.muted }}>
          Post {index + 1}
        </span>
        {written && <CheckCircle2 size={15} color={T.green} />}
      </div>
      {slot.kind === "reactive_hold" ? (
        <div style={{ fontSize: 12, color: T.muted2, fontStyle: "italic" }}>Held for reactive content</div>
      ) : written && atom ? (
        <>
          <div style={{ fontSize: 12.5, fontWeight: 600, color: T.ink }}>{atom.trip_name ?? "—"}</div>
          <div style={{ fontSize: 11, color: T.muted, marginTop: 2 }}>
            {written.channel} · written {fmtDate(written.used_at)}
          </div>
        </>
      ) : (
        <>
          <div style={{ fontSize: 12.5, color: T.body }}>{slot.topic_hint ?? "—"}</div>
          <div style={{ fontSize: 11, color: T.muted2, marginTop: 4 }}>
            {slot.atom_ids.length} suggested atom{slot.atom_ids.length === 1 ? "" : "s"}
          </div>
        </>
      )}
    </button>
  );
}

function SlotExpandedPanel({ slot, data }: { slot: Slot; data: SlotSuggestionsResponse }) {
  const router = useRouter();
  const [pickedAtomId, setPickedAtomId] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  // AA-497 — reopen state for the "already written" branch below.
  const [reopening, setReopening] = useState(false);
  const [reopenError, setReopenError] = useState<string | null>(null);

  const writtenAtomId = slot.atom_ids.find(id => data.used_atoms[id]);
  const written = writtenAtomId ? data.used_atoms[writtenAtomId] : null;
  const writtenAtom = writtenAtomId ? data.atoms_by_id[writtenAtomId] : null;

  // AA-497 (AA-494 Decision 3) — reopens the angle_gate_request behind this already-written
  // slot (approved -> reusable), then hands off to AngleGateTab.tsx's resume mode to pick a
  // different one of the 3 already-generated angles. Deliberately does NOT let a tenant pick a
  // different ATOM here — that's a separate, bigger product question (a new request entirely)
  // not part of this build; "Change angle" is exactly what it says.
  const changeAngle = useCallback(() => {
    if (!written) return;
    setReopening(true); setReopenError(null);
    fetch(`/api/tenant/v1/angle-gate/requests/${written.request_id}/reopen`, { method: "POST" })
      .then(async r => (r.ok ? r.json() : Promise.reject(await r.json().catch(() => ({})))))
      .then(() => router.push(`/portal/t8-angle-gate?resume_request_id=${encodeURIComponent(written.request_id)}`))
      .catch(e => { setReopenError(e.detail ?? "Couldn't reopen — try again."); setReopening(false); });
  }, [written, router]);

  const freeAtoms = useMemo(() => {
    const q = search.trim().toLowerCase();
    return data.free_atom_ids
      .map(id => data.atoms_by_id[id])
      .filter(Boolean)
      .filter(a => !q || a.trip_name?.toLowerCase().includes(q) || a.destination?.toLowerCase().includes(q) || a.text.toLowerCase().includes(q));
  }, [data, search]);

  const suggested = slot.atom_ids.map(id => data.atoms_by_id[id]).filter(Boolean);

  const startWriting = useCallback(() => {
    if (!pickedAtomId) return;
    router.push(`/portal/t8-angle-gate?atom_id=${encodeURIComponent(pickedAtomId)}`);
  }, [pickedAtomId, router]);

  // AA-497 — an already-written slot shows what was used + a "Change angle" action instead of
  // the atom-picker below (picking a different ATOM for an already-decided slot is a separate,
  // bigger question than this build covers — see changeAngle()'s own comment above).
  if (written && writtenAtom) {
    return (
      <div style={{ borderTop: `1px solid ${T.line2}`, paddingTop: 16 }}>
        <div style={{ fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: T.muted, marginBottom: 10 }}>
          Already written
        </div>
        <div style={{ padding: "14px 16px", borderRadius: 10, border: `1px solid ${T.green}`, background: T.greenSoft, marginBottom: 14 }}>
          <div style={{ fontSize: 13, fontWeight: 700, color: T.ink }}>{writtenAtom.trip_name ?? "—"}</div>
          <div style={{ fontSize: 11.5, color: T.muted, marginTop: 4 }}>
            {written.channel} · written {fmtDate(written.used_at)}
          </div>
        </div>
        {reopenError && (
          <div style={{ padding: "8px 10px", background: T.redSoft, border: "1px solid #F5C6C6", borderRadius: 8, fontSize: 11.5, color: T.red, marginBottom: 10 }}>
            {reopenError}
          </div>
        )}
        <Btn variant="secondary" disabled={reopening} onClick={changeAngle}>
          {reopening ? "Reopening…" : "Change angle"}
        </Btn>
        <div style={{ fontSize: 11, color: T.muted2, marginTop: 8, lineHeight: 1.5, maxWidth: 420 }}>
          Picks from the same 3 angles already generated for this atom — no new content is
          written until you choose one and it re-writes.
        </div>
      </div>
    );
  }

  return (
    <div style={{ borderTop: `1px solid ${T.line2}`, paddingTop: 16, paddingBottom: 56, position: "relative" }}>
      <div style={{ fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: T.muted, marginBottom: 10 }}>
        Suggested for this slot
      </div>
      {suggested.length === 0 ? (
        <div style={{ fontSize: 12, color: T.muted2, marginBottom: 18 }}>No suggested atoms — pick a free atom below instead.</div>
      ) : (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: 10, marginBottom: 18 }}>
          {suggested.map(a => (
            <AtomOptionCard key={a.atom_id} atom={a} picked={pickedAtomId === a.atom_id}
              onClick={() => setPickedAtomId(a.atom_id)} />
          ))}
        </div>
      )}

      <div style={{ fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: T.muted, marginBottom: 10 }}>
        Other atoms free this month ({data.free_atom_ids.length})
      </div>
      <div style={{ position: "relative", marginBottom: 10 }}>
        <Search size={13} color={T.muted2} style={{ position: "absolute", left: 10, top: 9 }} />
        <input value={search} onChange={e => setSearch(e.target.value)} placeholder="Search by destination or tour…"
          style={{ width: "100%", padding: "8px 10px 8px 30px", background: T.bg, border: `1px solid ${T.line}`, borderRadius: 8, color: T.body, fontSize: 12.5, fontFamily: sans, outline: "none", boxSizing: "border-box" }} />
      </div>
      {freeAtoms.length === 0 ? (
        <div style={{ fontSize: 12, color: T.muted2 }}>No free atoms match.</div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 6, maxHeight: 320, overflowY: "auto" }}>
          {freeAtoms.map(a => (
            <AtomRow key={a.atom_id} atom={a} picked={pickedAtomId === a.atom_id} onClick={() => setPickedAtomId(a.atom_id)} />
          ))}
        </div>
      )}

      <div style={{ position: "sticky", bottom: 0, marginTop: 16, paddingTop: 12, background: T.card, borderTop: `1px solid ${T.line2}` }}>
        <Btn variant="primary" disabled={!pickedAtomId} onClick={startWriting}>
          <Sparkles size={13} /> Start writing
        </Btn>
      </div>
    </div>
  );
}

function AtomOptionCard({ atom, picked, onClick }: { atom: AtomDetail; picked: boolean; onClick: () => void }) {
  return (
    <button onClick={onClick} style={{
      padding: "12px 14px", borderRadius: 10, cursor: "pointer", textAlign: "left", fontFamily: sans,
      border: `1px solid ${picked ? T.gold : T.line}`, background: picked ? T.goldTint : "#fff", position: "relative",
    }}>
      {picked && <CheckCircle2 size={15} color={T.gold} style={{ position: "absolute", top: 10, right: 10 }} />}
      <div style={{ fontSize: 12.5, fontWeight: 700, color: T.ink, marginBottom: 2 }}>{atom.trip_name ?? "—"}</div>
      <div style={{ fontSize: 11, color: T.muted2, marginBottom: 6 }}>{atom.destination}</div>
      <div style={{ fontSize: 12, color: T.body, lineHeight: 1.4 }}>{atom.text.slice(0, 90)}{atom.text.length > 90 ? "…" : ""}</div>
      <div style={{ marginTop: 8 }}>
        <Badge variant={atom.distinctiveness === "HIGH" ? "success" : atom.distinctiveness === "MED" ? "gold" : "default"}>
          {atom.distinctiveness}
        </Badge>
      </div>
    </button>
  );
}

function AtomRow({ atom, picked, onClick }: { atom: AtomDetail; picked: boolean; onClick: () => void }) {
  return (
    <button onClick={onClick} style={{
      display: "flex", alignItems: "center", gap: 10, padding: "8px 12px", borderRadius: 8, cursor: "pointer",
      textAlign: "left", fontFamily: sans, width: "100%", boxSizing: "border-box",
      border: `1px solid ${picked ? T.gold : T.line2}`, background: picked ? T.goldTint : "#fff",
    }}>
      {picked ? <CheckCircle2 size={14} color={T.gold} /> : <span style={{ width: 14, height: 14, borderRadius: "50%", border: `1px solid ${T.line}`, display: "inline-block", flexShrink: 0 }} />}
      <div style={{ flex: 1, minWidth: 0 }}>
        <span style={{ fontSize: 12.5, fontWeight: 600, color: T.ink }}>{atom.trip_name ?? "—"}</span>
        <span style={{ fontSize: 11, color: T.muted2, marginLeft: 8 }}>{atom.destination}</span>
        <div style={{ fontSize: 11.5, color: T.muted, marginTop: 2, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
          {atom.text}
        </div>
      </div>
      <span style={{ fontFamily: mono, fontSize: 10.5, color: T.muted2, flexShrink: 0 }}>{atom.distinctiveness}</span>
    </button>
  );
}
