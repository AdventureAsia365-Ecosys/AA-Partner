"use client";
// app/(tenant)/portal/_components/SlateTab.tsx — AA-511, the Slate (Weekly Slots' replacement).
//
// Replaces SlotPickerPanel.tsx on PlanningTab.tsx's own render path (not deleted — the epic's
// own "giữ code cũ, chỉ ngưng dùng ở đường Slate chính, không xoá" rule; see docs/claude_audit/
// AA-511-step0-slate-investigation.md for the full evidence trail behind this build).
//
// 8 tabs, one per Channel: 5 weekly-rhythm (Blog/LinkedIn/Facebook/Instagram/TikTok) + 3
// on-demand (Email/Landing Page/Ads, STEP0 Q3 — the origin's own `derive_posting_rhythm()`
// never produces a Subject list for these at all; this build runs the same Bar against them at
// their default-zero threshold, so an on-demand tab just lists "everything eligible", no
// nhịp/tuần line). Each row shows why it cleared the Bar (`cleared_bar_reason`, verbatim from
// `services/acp_shared/slate.py::_clears_bar()`) and a "Chọn viết" button that posts the pick and
// hands off to the SAME T8 entry point SlotPickerPanel.tsx's atom-picker already used
// (`/portal/t8-angle-gate`) — via `?resume_request_id=`, not `?atom_id=`, because
// `pick_subject()` already creates the `angle_gate_request` (with `channel` pre-set) before this
// component ever navigates; AngleGateTab.tsx's own `resumeRequestId` load path (built for AA-497's
// "Change angle" reopen) already handles loading an existing request at whatever step it's
// actually at (`pending_goal` here — step 2, Goal) with zero changes needed there.
//
// API: GET /api/tenant/v1/slate, POST /api/tenant/v1/subjects/{id}/pick
// (api/routers/v1_planning.py's `slate_router`, AA-511).
//
// AA-511 FE audit fix (2026-09-02, before Done) — 5 of the 6 reported gaps closed here (#4 was
// explicitly "no fix needed, skip"), each cited to the finding it closes:
//  #1 per-tab skeleton (SkeletonPanel) instead of one full-panel spinner covering everything.
//  #2 the important one — pick()'s error handler used to call onPicked() (refresh the whole
//     Slate) even when the POST itself failed, which could show a stale pickError message right
//     next to a row that had actually flipped to "picked" server-side if the client just lost the
//     response (a real timeout, not the request itself failing). onPicked()/setPickError(null)
//     now only run on a CONFIRMED 200 — a failed request leaves the Slate exactly as it was, and
//     the tenant gets an explicit "Refresh" link to check the real state on their own terms
//     instead of an automatic, possibly-misleading refresh.
//  #3 a Channel with decided (picked/used/cut) history but 0 currently-eligible Subjects now gets
//     its own message, distinct from "never had any Subject at all".
//  #5 a thin divider + a group icon (Calendar/Zap) on the tab strip, so the 3 on-demand tabs read
//     as a different group from the 5 weekly ones without opening any of them.
//  #6 the tab strip scrolls horizontally (hidden scrollbar) instead of wrapping onto extra lines
//     on a narrow screen.

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Calendar, ChevronRight, Sparkles, Zap } from "lucide-react";
import { T, serif, sans, mono, Card, CardHead, Badge, Btn, EmptyState } from "./ui";

interface ClearedBarReason {
  channel: string;
  on_demand: boolean;
  needs_demand: number; demand: number | null; demand_ok: boolean;
  needs_questions: number; questions: number; questions_ok: boolean;
  needs_said: number; said: number; said_ok: boolean;
}

interface SlateSubject {
  subject_id: string;
  channel: string;
  state: "proposed" | "picked" | "used" | "cut";
  score: number | null;
  cleared_bar_reason: ClearedBarReason;
  segment_id: string | null;
  route_id: string | null;
  place: string | null;
  action: string | null;
  hub_name: string | null;
  created_at: string | null;
}

interface ChannelSlate {
  channel: string;
  on_demand: boolean;
  eligible_count: number;
  subjects: SlateSubject[];
}

interface SlateResponse {
  channels: Record<string, ChannelSlate>;
  posts_per_week: number;
}

// Order: 5 weekly-rhythm tabs first, then the 3 on-demand ones — matches the build prompt's own
// "5 tab nhịp tuần + 3 tab theo yêu cầu" grouping. `group` drives the tab-strip divider + icon
// (FE audit #5) — the tab list itself is unchanged.
const CHANNEL_TABS: { key: string; label: string; group: "weekly" | "on_demand" }[] = [
  { key: "blog", label: "Blog", group: "weekly" },
  { key: "linkedin", label: "LinkedIn", group: "weekly" },
  { key: "facebook", label: "Facebook", group: "weekly" },
  { key: "instagram", label: "Instagram", group: "weekly" },
  { key: "tiktok", label: "TikTok", group: "weekly" },
  { key: "email", label: "Email", group: "on_demand" },
  { key: "landing_page", label: "Landing Page", group: "on_demand" },
  { key: "ads", label: "Ads", group: "on_demand" },
];

export default function SlateTab() {
  const [data, setData] = useState<SlateResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeChannel, setActiveChannel] = useState("blog");

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    fetch("/api/tenant/v1/slate")
      .then(async r => (r.ok ? r.json() : Promise.reject(await r.json().catch(() => ({})))))
      .then((d: SlateResponse) => setData(d))
      .catch(e => setError(e.detail ?? "Couldn't load the Slate — try again."))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  const active = data?.channels[activeChannel];

  return (
    <Card style={{ padding: "16px 18px", marginBottom: 18 }}>
      {/* Shimmer keyframes (FE audit #1) + hidden-scrollbar class for the tab strip (#6) — same
          plain-<style>-tag pattern CatalogTab.tsx already uses in this same portal. */}
      <style>{`
        @keyframes aa511SlateShimmer { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }
        .aa511-slate-tabstrip { scrollbar-width: none; -ms-overflow-style: none; }
        .aa511-slate-tabstrip::-webkit-scrollbar { display: none; }
      `}</style>

      <CardHead title="Slate" />
      <p style={{ fontSize: 12.5, color: T.muted, lineHeight: 1.6, margin: "0 0 14px" }}>
        Every moment that has cleared its Channel&rsquo;s bar — search-led Channels need
        measured demand and questions to answer; attention-led Channels need enough of the
        journey actually described. Sorted strongest first. Pick one to start writing.
      </p>

      <div className="aa511-slate-tabstrip" style={{
        display: "flex", gap: 4, alignItems: "center", overflowX: "auto", flexWrap: "nowrap",
        borderBottom: `1px solid ${T.line2}`, marginBottom: 16, paddingBottom: 2,
      }}>
        {CHANNEL_TABS.map(({ key, label, group }, i) => {
          const showDivider = i > 0 && CHANNEL_TABS[i - 1].group !== group;
          const count = data?.channels[key]?.eligible_count ?? null;
          const isActive = activeChannel === key;
          const Icon = group === "weekly" ? Calendar : Zap;
          return (
            <div key={key} style={{ display: "flex", alignItems: "center", flexShrink: 0 }}>
              {showDivider && (
                <div aria-hidden style={{ width: 1, height: 20, background: T.line2, margin: "0 6px", flexShrink: 0 }} />
              )}
              <button
                onClick={() => setActiveChannel(key)}
                title={group === "weekly" ? "Nhịp tuần" : "Theo yêu cầu (on-demand)"}
                style={{
                  display: "flex", alignItems: "center", gap: 5, whiteSpace: "nowrap", flexShrink: 0,
                  padding: "7px 12px", borderRadius: "8px 8px 0 0", cursor: "pointer",
                  fontFamily: sans, fontSize: 12.5, fontWeight: isActive ? 700 : 500,
                  color: isActive ? T.ink : T.muted,
                  background: isActive ? T.bg : "transparent",
                  border: "none", borderBottom: isActive ? `2px solid ${T.gold}` : "2px solid transparent",
                }}
              >
                <Icon size={11} style={{ opacity: 0.55, flexShrink: 0 }} />
                {label}{count != null && <span style={{ marginLeft: 2, fontFamily: mono, fontSize: 10.5, color: T.muted2 }}>{count}</span>}
              </button>
            </div>
          );
        })}
      </div>

      {loading && <SkeletonPanel />}

      {error && !loading && (
        <EmptyState icon="⚠️" title="Couldn't load the Slate" sub={error}
          action={<Btn variant="secondary" onClick={load}>Try again</Btn>} />
      )}

      {!loading && !error && active && (
        <ChannelPanel channel={active} postsPerWeek={data!.posts_per_week} onPicked={load} />
      )}
    </Card>
  );
}

// ── Skeleton (FE audit #1) ──────────────────────────────────────────────────
// Shaped like the real content (a counts line + a few SubjectRow-shaped cards), shown in the
// active panel's own place — the tab strip above it is never hidden or replaced.

function SkeletonBlock({ width, height = 12 }: { width: string; height?: number }) {
  return (
    <div style={{
      width, height, borderRadius: 5, flexShrink: 0,
      background: `linear-gradient(90deg, ${T.line} 25%, ${T.line2} 50%, ${T.line} 75%)`,
      backgroundSize: "200% 100%", animation: "aa511SlateShimmer 1.5s infinite",
    }} />
  );
}

function SkeletonRow() {
  return (
    <div style={{
      display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12,
      padding: "12px 14px", borderRadius: 10, border: `1px solid ${T.line}`, background: "#fff",
    }}>
      <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 8, minWidth: 0 }}>
        <SkeletonBlock width="55%" height={14} />
        <SkeletonBlock width="75%" height={11} />
      </div>
      <SkeletonBlock width="96px" height={28} />
    </div>
  );
}

function SkeletonPanel() {
  return (
    <div>
      <SkeletonBlock width="220px" height={12} />
      <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 14 }}>
        <SkeletonRow />
        <SkeletonRow />
        <SkeletonRow />
      </div>
    </div>
  );
}

function ChannelPanel({ channel, postsPerWeek, onPicked }: {
  channel: ChannelSlate; postsPerWeek: number; onPicked: () => void;
}) {
  const proposed = channel.subjects.filter(s => s.state === "proposed");
  const decided = channel.subjects.filter(s => s.state !== "proposed");

  return (
    <div>
      <div style={{ fontSize: 12, color: T.muted, marginBottom: 14 }}>
        <strong style={{ color: T.ink, fontFamily: mono }}>{channel.eligible_count}</strong> subject{channel.eligible_count === 1 ? "" : "s"} đủ điều kiện
        {!channel.on_demand && (
          <> · nhịp đề xuất <strong style={{ color: T.ink, fontFamily: mono }}>{postsPerWeek}</strong> bài/tuần</>
        )}
        {channel.on_demand && <> · viết theo yêu cầu</>}
      </div>

      {channel.subjects.length === 0 ? (
        // Never had any Subject at all — the original "atomize a tour first" guidance.
        <EmptyState icon="🗒️" title="Nothing here yet"
          sub="No Segment or Route on this tenant has cleared this Channel's bar yet — atomize and rank a tour first." />
      ) : proposed.length === 0 ? (
        // FE audit #3 — DIFFERENT message: this Channel has decided history, just nothing NEW
        // to pick right now. Conflating this with the "never had any Subject" case above was the
        // reported gap — a tenant seeing a bare gap above "Already decided" with no explanation.
        <>
          <EmptyState icon="✅" title="Không còn Subject mới đủ điều kiện lúc này"
            sub="Xem lại các Subject đã quyết định bên dưới, hoặc rewrite/atomize thêm tour để có đề xuất mới." />
          <DecidedList subjects={decided} onPicked={onPicked} />
        </>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {proposed.map(s => (
            <SubjectRow key={s.subject_id} subject={s} onPicked={onPicked} />
          ))}
          {decided.length > 0 && <DecidedList subjects={decided} onPicked={onPicked} />}
        </div>
      )}
    </div>
  );
}

function DecidedList({ subjects, onPicked }: { subjects: SlateSubject[]; onPicked: () => void }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 10 }}>
      <div style={{ fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.08em", color: T.muted2, margin: "10px 0 2px" }}>
        Already decided
      </div>
      {subjects.map(s => (
        <SubjectRow key={s.subject_id} subject={s} onPicked={onPicked} />
      ))}
    </div>
  );
}

function barReasonText(reason: ClearedBarReason): string {
  if (reason.on_demand) return "Theo yêu cầu — không giới hạn ngưỡng";
  const parts: string[] = [];
  if (reason.needs_demand > 0) {
    parts.push(`Demand ${reason.demand?.toLocaleString() ?? 0} ≥ ${reason.needs_demand.toLocaleString()}`);
  }
  if (reason.needs_questions > 0) {
    parts.push(`Questions ${reason.questions} ≥ ${reason.needs_questions}`);
  }
  if (reason.needs_said > 0) {
    parts.push(`Said ${reason.said} ký tự ≥ ${reason.needs_said}`);
  }
  return parts.length > 0 ? parts.join(" · ") : "Không có ngưỡng nào áp dụng";
}

function SubjectRow({ subject, onPicked }: { subject: SlateSubject; onPicked: () => void }) {
  const router = useRouter();
  const [picking, setPicking] = useState(false);
  const [pickError, setPickError] = useState<string | null>(null);

  const title = subject.route_id
    ? (subject.hub_name ?? "Untitled journey")
    : [subject.place, subject.action].filter(Boolean).join(" — ") || "Untitled moment";

  const kindLabel = subject.route_id ? "Route" : "Segment";

  const pick = useCallback(() => {
    setPicking(true);
    setPickError(null);
    fetch(`/api/tenant/v1/subjects/${subject.subject_id}/pick`, { method: "POST" })
      .then(async r => {
        const body = await r.json().catch(() => ({}));
        if (!r.ok) throw body;
        return body as { request_id: string };
      })
      .then(d => {
        // FE audit #2 — only a CONFIRMED 200 clears the error and refreshes the Slate. Reaching
        // here means the backend really did flip this Subject to 'picked', so both are safe.
        setPickError(null);
        onPicked();
        router.push(`/portal/t8-angle-gate?resume_request_id=${encodeURIComponent(d.request_id)}`);
      })
      .catch(e => {
        // FE audit #2 (the important fix) — a failed request (4xx/5xx, or the browser giving up
        // on a real network timeout) must NOT call onPicked(). The old code refreshed here
        // unconditionally, which could show this exact error message sitting right next to a
        // "picked" badge if the backend had actually succeeded before the client gave up waiting
        // on the response. Leaving the Slate untouched means the row keeps showing "Chọn viết"
        // (its last KNOWN state) alongside the error — never a state the tenant never asked for.
        setPickError(e?.detail ?? "Couldn't pick this Subject — try again.");
        setPicking(false);
      });
  }, [subject.subject_id, router, onPicked]);

  const stateVariant = subject.state === "proposed" ? "gold"
    : subject.state === "picked" ? "info"
    : subject.state === "used" ? "success" : "default";

  return (
    <div style={{
      display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 12,
      padding: "12px 14px", borderRadius: 10, border: `1px solid ${T.line}`, background: "#fff",
    }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4, flexWrap: "wrap" }}>
          <span style={{ fontFamily: serif, fontSize: 14.5, fontWeight: 500, color: T.ink }}>{title}</span>
          <Badge variant="default">{kindLabel}</Badge>
          {subject.state !== "proposed" && <Badge variant={stateVariant}>{subject.state}</Badge>}
          {subject.score != null && (
            <span style={{ fontFamily: mono, fontSize: 10.5, color: T.muted2 }} title="Rank-sum — lower is better">
              rank {subject.score}
            </span>
          )}
        </div>
        <div style={{ fontSize: 11.5, color: T.muted, lineHeight: 1.5 }}>
          {barReasonText(subject.cleared_bar_reason)}
        </div>
        {pickError && (
          <div style={{
            marginTop: 6, padding: "6px 8px", background: T.redSoft, border: "1px solid #F5C6C6",
            borderRadius: 6, fontSize: 11, color: T.red, display: "flex", alignItems: "center", gap: 8,
          }}>
            <span>{pickError}</span>
            {/* Manual, explicit refresh — never automatic (FE audit #2) — so the tenant can
                check the real state on their own terms instead of an auto-refresh that could
                have shown this same error next to a row that actually succeeded. */}
            <button onClick={onPicked} style={{
              background: "none", border: "none", padding: 0, cursor: "pointer",
              color: T.red, fontFamily: sans, fontSize: 11, fontWeight: 700, textDecoration: "underline",
              flexShrink: 0,
            }}>
              Refresh
            </button>
          </div>
        )}
      </div>
      {subject.state === "proposed" && (
        <Btn variant="primary" size="sm" disabled={picking} onClick={pick}>
          {picking ? "Đang chọn…" : <><Sparkles size={12} /> Chọn viết <ChevronRight size={12} /></>}
        </Btn>
      )}
    </div>
  );
}
