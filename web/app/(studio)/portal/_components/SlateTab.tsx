"use client";
// web/app/(studio)/portal/_components/SlateTab.tsx — AA-511, the Slate (Weekly Slots' replacement).
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
// `services/acp_shared/slate.py::_clears_bar()`) and a "Chọn viết" button that posts the pick.
//
// AA-564 Nhóm 4.2 (2026-09-08) — pick() used to navigate to the standalone T8 route
// (`/portal/create/write?resume_request_id=`). It now expands `AngleGateWizard` INLINE under
// the same Subject row instead (accordion — an `expanded` state held at most one
// {subjectId, requestId} at a time, shared by both `proposed.map()` and `DecidedList` via the
// same `SubjectRow` so the wizard stays open across the row moving from one list to the other
// when `onPicked()`'s refetch flips `state` proposed -> picked). The standalone route/page still
// exists unchanged (a valid bookmark/deep-link), just no longer the only way in — its own
// `AngleGateWizard`'s `requestId` load path (built for AA-497's "Change angle" reopen) already
// handled loading an existing request at whatever step it's actually at, with zero changes
// needed there for this embedded use.
//
// AA-556 (2026-09-10) — "Cut" button added next to "Chọn viết" in SubjectRow's `state ===
// "proposed"` block. `cut_subject()` (services/acp_shared/slate.py) already existed backend-only
// since AA-554 H.2, always re-SELECTs the real DB state before allowing proposed->cut (never
// trusts a client-supplied value) — confirmed via direct read + posted on AA-556 before building
// this, since AA-564 Nhóm 5 flagged a possible race between this same row's `pick()` succeeding
// (isExpanded flips true synchronously) and the Slate's own refetch landing (which is what
// actually updates `subject.state` away from "proposed"). Backend is safe either way (a
// same-row Cut mid-race just 409s, same as an already-picked row would), so disabling Cut during
// `picking || isExpanded` here is purely UX polish (avoid a pointless 409/flicker), not a data
// safety fix.
//
// AA-668 (S237) — the inline accordion above is replaced by the Writer panel (WriterPanel.tsx, a
// right-side kit Drawer; full-screen on a phone). "Pick to write" opens it on the new request;
// a Subject picked earlier in this visit gets a "Continue" button that reopens it (the Slate API
// does not return a picked Subject's request_id, so a reload loses that shortcut — the piece is in
// My Content either way). Closing the panel while a piece is still being written hands it to
// useWriteWatcher, which raises a toast when the background job finishes.
//
// API: GET /api/tenant/v1/slate, POST /api/tenant/v1/subjects/{id}/pick,
// POST /api/tenant/v1/subjects/{id}/cut (api/routers/v1_planning.py's `slate_router`, AA-511/556).
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

import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Calendar, ChevronRight, Sparkles, X, Zap, AlertTriangle, ClipboardList, CheckCircle2 } from "lucide-react";
import { Card, CardHead, CtaLink, StickyBar } from "@/app/_kit/local/studio/StudioCard";
import { Badge, Button, EmptyState, LinkButton, ToastProvider, K, mono, sans, serif, useLang, useT, Pressable, type Translator, type UiLang } from "@/app/_kit";
import { badgeTone } from "@/lib/studio/format";
import { SLATE_MESSAGES } from "./writer.messages";
import { channelName } from "./channelLabels";

type SlateT = Translator<typeof SLATE_MESSAGES.en>;
import type { ContentPiece } from "./AngleGateWizard";
import { WriterPanel, useWriteWatcher, watchPiece, type WriterTarget } from "./WriterPanel";
import { plainError } from "./studioError";
import { BlogTopicsNote } from "./BlogTopicsNote";
import type { BlogTopics } from "@/lib/studio/blogTopics";

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
  // AA-629 — non-empty ONLY when this tenant declared real target_market.countries the platform
  // doesn't support yet (a tenant who declared nothing at all never sees this — that case's US
  // default is a legitimate product choice, not something to warn about).
  unmatched_markets?: string[];
  // AA-764 — the Slate is proposed by a background job; `refreshing` while it runs.
  refresh?: { status: string; refreshing: boolean; finished_at: string | null };
  // AA-1081 — per adopted tour: its Blog topic count and, with none, a typed reason (null before the first update).
  blog_topics?: BlogTopics | null;
}

const SLATE_REFRESH_POLL_MS = 15_000;
const SLATE_KEY = ["tenant-slate"] as const;

// Order: 5 weekly-rhythm tabs first, then the 3 on-demand ones — matches the build prompt's own
// "5 tab nhịp tuần + 3 tab theo yêu cầu" grouping. `group` drives the tab-strip divider + icon
// (FE audit #5) — the tab list itself is unchanged.
// AA-1036 (R3): a tab shows channelName(key, lang), the one catalogue of channel names (writer.messages.ts).
const CHANNEL_TABS: { key: string; group: "weekly" | "on_demand" }[] = [
  { key: "blog", group: "weekly" },
  { key: "linkedin", group: "weekly" },
  { key: "facebook", group: "weekly" },
  { key: "instagram", group: "weekly" },
  { key: "tiktok", group: "weekly" },
  { key: "email", group: "on_demand" },
  { key: "landing_page", group: "on_demand" },
  { key: "ads", group: "on_demand" },
];

// AA-668 — the Writer panel's state, shared by every row of every Channel tab: at most one panel
// open (`panel`), plus the requests picked during this visit (`known`, keyed by subject_id) so a
// picked row can reopen its panel after it moved from `proposed` to `decided`.
interface WriterControls {
  openSubjectId: string | null;
  known: Record<string, WriterTarget>;
  open: (t: WriterTarget) => void;
}

/** AA-759 — the Channels a Create page shows: Articles = blog, Social = every other Channel. */
export const ARTICLE_CHANNELS: readonly string[] = ["blog"];
export const SOCIAL_CHANNELS: readonly string[] = CHANNEL_TABS.map(c => c.key).filter(k => !ARTICLE_CHANNELS.includes(k));

// The kit toast needs a provider; the Slate is the only toast user on this page.
export default function SlateTab({ channels, title, intro }: {
  /** AA-759 — the Channel tabs to show (default: all 8). */
  channels?: readonly string[];
  title?: string;
  /** AA-760 — the plain-words line under the card title (each Create page passes its own). */
  intro?: string;
}) {
  return (
    <ToastProvider>
      <SlateTabInner channels={channels} title={title} intro={intro} />
    </ToastProvider>
  );
}

// AA-760 — tenant copy only: no Channel "bar", demand thresholds or ranking terms (tenant sees content only).
function SlateTabInner({ channels, title, intro }: { channels?: readonly string[]; title?: string; intro?: string }) {
  const t = useT(SLATE_MESSAGES);
  const lang = useLang();
  const tabs = channels ? CHANNEL_TABS.filter(c => channels.includes(c.key)) : CHANNEL_TABS;
  const [activeChannel, setActiveChannel] = useState(() => tabs[0]?.key ?? "blog");
  const [panel, setPanel] = useState<WriterTarget | null>(null);
  const [known, setKnown] = useState<Record<string, WriterTarget>>({});
  const { watch, unwatch } = useWriteWatcher();

  const openWriter = useCallback((target: WriterTarget) => {
    unwatch(target.requestId);
    setKnown(prev => ({ ...prev, [target.subjectId]: target }));
    setPanel(target);
  }, [unwatch]);

  const closeWriter = useCallback((lastPiece: ContentPiece | null) => {
    if (panel && lastPiece?.ready_state === "in_progress") {
      watch(watchPiece(lastPiece.piece_id, panel.requestId, panel.title, lang));
    }
    setPanel(null);
  }, [panel, watch, lang]);

  // A write whose 202 arrived after the panel closed (closed in the click → write-call window).
  // Skipped when that request's panel is open again by then: the reopened wizard resumes it itself.
  const panelRef = useRef<WriterTarget | null>(null);
  useEffect(() => { panelRef.current = panel; }, [panel]);
  const watchDetached = useCallback((p: ContentPiece, target: WriterTarget) => {
    if (p.ready_state !== "in_progress" || panelRef.current?.requestId === target.requestId) return;
    watch(watchPiece(p.piece_id, target.requestId, target.title, lang));
  }, [watch, lang]);

  const writer: WriterControls = { openSubjectId: panel?.subjectId ?? null, known, open: openWriter };

  // AA-760 — the Slate is a react-query query (was a fetch in an effect + a setInterval while a
  // background proposal ran). It re-reads quietly every 15 s only while `refresh.refreshing`
  // (AA-764: GET /v1/slate only reads; new topics appear on their own when the job finishes) and
  // stops once the proposal settled. Social Posts and Articles share the one cached answer.
  const qc = useQueryClient();
  const slateQ = useQuery({
    queryKey: SLATE_KEY,
    queryFn: async (): Promise<SlateResponse> => {
      const r = await fetch("/api/tenant/v1/slate");
      if (!r.ok) {
        const e = await r.json().catch(() => ({}));
        throw new Error(e.detail ?? t("loadFailed"));
      }
      return r.json();
    },
    refetchInterval: (q) => (q.state.data?.refresh?.refreshing ? SLATE_REFRESH_POLL_MS : false),
  });
  const data = slateQ.data ?? null;
  const loading = slateQ.isPending;
  const error = slateQ.isError ? plainError(slateQ.error, t("loadFailed"), lang) : null;
  const load = useCallback(() => { qc.invalidateQueries({ queryKey: SLATE_KEY }); }, [qc]);
  const refreshing = !!data?.refresh?.refreshing;

  const [refreshError, setRefreshError] = useState<string | null>(null);
  const requestRefresh = useCallback(() => {
    setRefreshError(null);
    fetch("/api/tenant/v1/slate/refresh", { method: "POST" })
      .then(async r => (r.ok ? r.json() : Promise.reject(await r.json().catch(() => ({})))))
      .then((d: { refresh: SlateResponse["refresh"] }) =>
        qc.setQueryData<SlateResponse>(SLATE_KEY, prev => (prev ? { ...prev, refresh: d.refresh } : prev)))
      .catch(e => setRefreshError(e.detail ?? t("refreshFailed")));
  }, [qc, t]);

  const active = data?.channels[activeChannel];

  return (
    <Card style={{ padding: "16px 18px", marginBottom: 18 }}>
      {/* Shimmer keyframes (FE audit #1) — same plain-<style>-tag pattern CatalogTab.tsx uses. The tab strip's hidden
          scrollbar (#6) is its inline `scrollbarWidth: "none"` (AA-1008: no class name in page code). */}
      <style>{`
        @keyframes aa511SlateShimmer { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }
      `}</style>

      {/* AA-524 — sticky, bleeding out to this Card's own custom padding (16px/18px) so it reads
          as one continuous bar rather than floating detached from the card's border once a
          Channel's Subject list scrolls underneath it. */}
      <StickyBar
        background={K.card}
        style={{
          marginTop: -16, marginLeft: -18, marginRight: -18,
          paddingTop: 16, paddingLeft: 18, paddingRight: 18,
          borderRadius: "12px 12px 0 0",
        }}
      >
        {/* AA-564 4.1 — Tenant-facing label renamed "Slate" -> "Social Content"; the component/file
            name (SlateTab.tsx, internal-only) is unchanged, matching CONTEXT.md's own Subject/Slate
            terminology used everywhere else in the codebase. */}
        <CardHead title={title ?? t("defaultTitle")} />
        <p style={{ fontSize: 12.5, color: K.muted, lineHeight: 1.6, margin: "0 0 14px" }}>{intro ?? t("defaultIntro")}</p>

        {/* AA-764 — proposal state: topics are re-ranked in the background, never on page load. */}
        {data && (
          <div style={{
            marginBottom: 14, display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap",
            fontSize: 12, color: K.muted,
          }}>
            {refreshing ? (
              <span role="status">{t("updatingBg")}</span>
            ) : (
              <span>
                {data.refresh?.finished_at
                  ? <>{t("lastUpdated", new Date(data.refresh.finished_at).toLocaleString(lang === "vi" ? "vi-VN" : "en-GB", { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" }))}</>
                  : <>{t("notPrepared")}</>}
                {data.refresh?.status === "failed" && <>{t("lastFailed")}</>}
              </span>
            )}
            <Button variant="secondary" disabled={refreshing} onClick={requestRefresh}>
              {refreshing ? t("updating") : t("update")}
            </Button>
            {refreshError && <span style={{ color: K.danger }}>{refreshError}</span>}
          </div>
        )}

        {/* AA-629 — explicit notice instead of silently showing US-market ranking data for a
            tenant whose declared markets aren't supported yet. */}
        {data && data.unmatched_markets && data.unmatched_markets.length > 0 && (
          <div style={{
            marginBottom: 14, padding: "10px 12px", background: K.accentTint,
            border: `1px solid ${K.accent}`, borderRadius: 8,
            fontSize: 12, color: K.ink, display: "flex", alignItems: "flex-start", gap: 8,
          }}>
            <span style={{ fontSize: 14, flexShrink: 0 }}>⚠</span>
            <span>
              {t("marketsNotSupported", data.unmatched_markets.join(", "), data.unmatched_markets.length > 1)}
            </span>
          </div>
        )}

        {/* AA-760 — on a phone the strip scrolls sideways with its scrollbar hidden; a right-edge fade
            shows there are more Channels (the end padding lets the last tab clear the fade). */}
        <div data-testid="slate-tabstrip" style={{
          display: "flex", gap: 4, alignItems: "center", overflowX: "auto", flexWrap: "nowrap", scrollbarWidth: "none",
          borderBottom: `1px solid ${K.line2}`, paddingBottom: 2, paddingRight: tabs.length > 1 ? 28 : 0,
          ...(tabs.length > 1 ? TABSTRIP_FADE : {}),
        }}>
          {tabs.map(({ key, group }, i) => {
            const showDivider = i > 0 && tabs[i - 1].group !== group;
            const count = data?.channels[key]?.eligible_count ?? null;
            const isActive = activeChannel === key;
            const Icon = group === "weekly" ? Calendar : Zap;
            return (
              <div key={key} style={{ display: "flex", alignItems: "center", flexShrink: 0 }}>
                {showDivider && (
                  <div aria-hidden style={{ width: 1, height: 20, background: K.line2, margin: "0 6px", flexShrink: 0 }} />
                )}
                <Pressable
                  display="inline"
                  aria-pressed={isActive}
                  onClick={() => setActiveChannel(key)}
                  title={group === "weekly" ? t("weekly") : t("onDemand")}
                  style={{
                    display: "flex", alignItems: "center", gap: 5, whiteSpace: "nowrap", flexShrink: 0,
                    padding: "7px 12px", borderRadius: "8px 8px 0 0",
                    fontFamily: sans, fontSize: 12.5, fontWeight: isActive ? 700 : 500,
                    color: isActive ? K.ink : K.muted,
                    background: isActive ? K.bg : "transparent",
                    border: "none", borderBottom: isActive ? `2px solid ${K.accent}` : "2px solid transparent",
                  }}
                >
                  <Icon size={11} style={{ opacity: 0.55, flexShrink: 0 }} />
                  {channelName(key, lang)}{count != null && <span style={{ marginLeft: 2, fontFamily: mono, fontSize: 10.5, color: K.muted2 }}>{count}</span>}
                </Pressable>
              </div>
            );
          })}
        </div>
      </StickyBar>

      <div style={{ marginTop: 16 }}>
      {loading && <SkeletonPanel />}

      {error && !loading && (
        <EmptyState icon={<AlertTriangle size={32} strokeWidth={1.5} color={K.accent} />} title={t("loadTopicsFailed")} description={error}
          action={<Button variant="secondary" onClick={load}>{t("tryAgain")}</Button>} />
      )}

      {!loading && !error && active && (
        <ChannelPanel
          channel={active} postsPerWeek={data!.posts_per_week} onPicked={load} writer={writer}
        />
      )}
      {/* AA-1081 — Articles: the tours with no article topic yet, and why (tenant words, from the last update). */}
      {!loading && !error && activeChannel === "blog" && <BlogTopicsNote blogTopics={data?.blog_topics} />}
      </div>

      <WriterPanel key={panel?.requestId ?? "closed"} target={panel} onClose={closeWriter} onBackgroundPiece={watchDetached} />
    </Card>
  );
}

const TABSTRIP_FADE: React.CSSProperties = {
  maskImage: "linear-gradient(to right, black calc(100% - 28px), transparent)", // a mask: black = shown, not a colour
  WebkitMaskImage: "linear-gradient(to right, black calc(100% - 28px), transparent)",
};

// ── Skeleton (FE audit #1) ──────────────────────────────────────────────────
// Shaped like the real content (a counts line + a few SubjectRow-shaped cards), shown in the
// active panel's own place — the tab strip above it is never hidden or replaced.

function SkeletonBlock({ width, height = 12 }: { width: string; height?: number }) {
  return (
    <div style={{
      width, height, borderRadius: 5, flexShrink: 0,
      background: `linear-gradient(90deg, ${K.line} 25%, ${K.line2} 50%, ${K.line} 75%)`,
      backgroundSize: "200% 100%", animation: "aa511SlateShimmer 1.5s infinite",
    }} />
  );
}

function SkeletonRow() {
  return (
    <div style={{
      display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12,
      padding: "12px 14px", borderRadius: 10, border: `1px solid ${K.line}`, background: K.card,
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
  const t = useT(SLATE_MESSAGES);
  return (
    <div aria-busy="true" aria-label={t("loadingTopics")}>
      <SkeletonBlock width="220px" height={12} />
      <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 14 }}>
        <SkeletonRow />
        <SkeletonRow />
        <SkeletonRow />
      </div>
    </div>
  );
}

function ChannelPanel({ channel, postsPerWeek, onPicked, writer }: {
  channel: ChannelSlate; postsPerWeek: number; onPicked: () => void; writer: WriterControls;
}) {
  const t = useT(SLATE_MESSAGES);
  const proposed = channel.subjects.filter(s => s.state === "proposed");
  const decided = channel.subjects.filter(s => s.state !== "proposed");

  const never = channel.subjects.length === 0;
  return (
    <div>
      {/* AA-760 — a Channel that never had a topic shows the empty state alone (no "0 topics ready"). */}
      {!never && (
        <div style={{ fontSize: 12, color: K.muted, marginBottom: 14 }}>
          <strong style={{ color: K.ink, fontFamily: mono }}>{channel.eligible_count}</strong> {t("topicsReady", channel.eligible_count)}
          {!channel.on_demand && (
            <>{t("plan")} <strong style={{ color: K.ink, fontFamily: mono }}>{postsPerWeek}</strong> {t("postsPerWeek")}</>
          )}
          {channel.on_demand && <>{t("writtenOnDemand")}</>}
        </div>
      )}

      {never ? (
        // Never had any Subject at all. AA-760 — tenant words: no "atomize / rank / bar" (steps the
        // tenant cannot run), and a way forward they can take (Update topics sits in the bar above).
        <EmptyState icon={<ClipboardList size={32} strokeWidth={1.5} color={K.accent} />} title={t("nothingYet")}
          description={t("nothingYetSub")}
          action={<CtaLink href="/portal/tours">{t("browseTours")}</CtaLink>} />
      ) : proposed.length === 0 ? (
        // FE audit #3 — DIFFERENT message: this Channel has decided history, just nothing NEW
        // to pick right now. Conflating this with the "never had any Subject" case above was the
        // reported gap — a tenant seeing a bare gap above "Already decided" with no explanation.
        <>
          <EmptyState icon={<CheckCircle2 size={32} strokeWidth={1.5} color={K.accent} />} title={t("noNewTopics")}
            description={t("noNewTopicsSub")} />
          <DecidedList subjects={decided} onPicked={onPicked} writer={writer} />
        </>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {proposed.map(s => (
            <SubjectRow key={s.subject_id} subject={s} onPicked={onPicked} writer={writer} />
          ))}
          {decided.length > 0 && <DecidedList subjects={decided} onPicked={onPicked} writer={writer} />}
        </div>
      )}
    </div>
  );
}

function DecidedList({ subjects, onPicked, writer }: {
  subjects: SlateSubject[]; onPicked: () => void; writer: WriterControls;
}) {
  const t = useT(SLATE_MESSAGES);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 10 }}>
      <div style={{ fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.08em", color: K.muted2, margin: "10px 0 2px" }}>
        {t("alreadyDecided")}
      </div>
      {subjects.map(s => (
        <SubjectRow key={s.subject_id} subject={s} onPicked={onPicked} writer={writer} />
      ))}
    </div>
  );
}

// AA-760 — the row's second line in tenant words. The Bar check itself (thresholds, rank-sum score,
// Segment/Route kind) is internal ranking data and is no longer shown (tenant sees content only);
// only measured search demand is kept, as "~N searches a month", where the Channel uses it.
function demandText(reason: ClearedBarReason, t: SlateT, lang: UiLang): string | null {
  if (reason.on_demand || reason.needs_demand <= 0 || !reason.demand) return null;
  return t("searchesMonth", reason.demand.toLocaleString(lang === "vi" ? "vi-VN" : "en-US"));
}

/** First letter upper-case ("a local Buddhist monastery → …" → "A local …"). */
function capFirst(s: string): string {
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}

/** AA-760 — a Segment topic reads action-first ("Swim in the lower pools · Kuang Si Falls"), not the
 *  mechanical "place — action". The API has no written headline for a Subject yet. */
export function subjectTitle(s: Pick<SlateSubject, "route_id" | "hub_name" | "place" | "action">, t: SlateT): string {
  if (s.route_id) return capFirst(s.hub_name ?? "") || t("untitledJourney");
  if (s.action && s.place) return `${capFirst(s.action)} · ${s.place}`;
  return capFirst(s.action ?? s.place ?? "") || t("untitledMoment");
}

/** Tenant wording for a decided topic (a key of SLATE_MESSAGES). */
const DECIDED_LABEL = {
  picked: "stPicked", used: "stUsed", cut: "stCut",
} as const satisfies Record<Exclude<SlateSubject["state"], "proposed">, keyof typeof SLATE_MESSAGES.en>;

function SubjectRow({ subject, onPicked, writer }: {
  subject: SlateSubject; onPicked: () => void; writer: WriterControls;
}) {
  const t = useT(SLATE_MESSAGES);
  const lang = useLang();
  const [picking, setPicking] = useState(false);
  const [pickError, setPickError] = useState<string | null>(null);
  const [cutting, setCutting] = useState(false);
  const [cutError, setCutError] = useState<string | null>(null);

  const title = subjectTitle(subject, t);
  const demand = demandText(subject.cleared_bar_reason, t, lang);

  const kindLabel = subject.route_id ? "Route" : "Segment";
  // AA-668 — kept the AA-556 name: "this row's writer is open".
  const isExpanded = writer.openSubjectId === subject.subject_id;
  const knownTarget = writer.known[subject.subject_id] ?? null;
  const { open: openWriter } = writer;

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
        // AA-668 — opens the Writer panel on the new request (was: AA-564 4.2's inline
        // accordion; before that, navigation to the standalone T8 route).
        setPickError(null);
        setPicking(false);
        onPicked();
        openWriter({
          subjectId: subject.subject_id, requestId: d.request_id, title,
          channel: subject.channel, kind: kindLabel,
        });
      })
      .catch(e => {
        // FE audit #2 (the important fix) — a failed request (4xx/5xx, or the browser giving up
        // on a real network timeout) must NOT call onPicked(). The old code refreshed here
        // unconditionally, which could show this exact error message sitting right next to a
        // "picked" badge if the backend had actually succeeded before the client gave up waiting
        // on the response. Leaving the Slate untouched means the row keeps showing "Pick to write"
        // (its last KNOWN state) alongside the error — never a state the tenant never asked for.
        setPickError(e?.detail ?? t("pickFailed"));
        setPicking(false);
      });
  }, [subject.subject_id, subject.channel, title, kindLabel, onPicked, openWriter, t]);

  // AA-556 — one-way action (no "un-cut"), so a confirm dialog gates every call. Disabled while
  // `picking` or `isExpanded` (see file header) — purely to avoid a same-row race that would just
  // 409 harmlessly, not a data-safety guard (the backend already re-checks live DB state).
  const cut = useCallback(() => {
    if (!window.confirm(t("skipConfirm"))) return;
    setCutting(true);
    setCutError(null);
    fetch(`/api/tenant/v1/subjects/${subject.subject_id}/cut`, { method: "POST" })
      .then(async r => {
        const body = await r.json().catch(() => ({}));
        if (!r.ok) throw body;
        return body;
      })
      .then(() => {
        setCutError(null);
        onPicked();
      })
      .catch(e => {
        setCutError(e?.detail ?? t("skipFailed"));
        setCutting(false);
      });
  }, [subject.subject_id, onPicked, t]);

  const stateVariant = subject.state === "proposed" ? "gold"
    : subject.state === "picked" ? "info"
    : subject.state === "used" ? "success" : "default";

  return (
    <div style={{
      borderRadius: 10, border: `1px solid ${isExpanded ? K.accent : K.line}`, background: K.card,
      overflow: "hidden",
    }}>
      {/* AA-760 — wraps: the title keeps at least 260 px, so on a phone the buttons drop to their own
          row under it instead of squeezing the title into a ~70 px column. */}
      <div style={{
        display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 12,
        padding: "12px 14px", flexWrap: "wrap",
      }}>
        <div style={{ flex: "1 1 260px", minWidth: 0 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: demand ? 4 : 0, flexWrap: "wrap" }}>
            <span style={{ fontFamily: serif, fontSize: 14.5, fontWeight: 500, color: K.ink, overflowWrap: "anywhere", minWidth: 0 }}>{title}</span>
            {subject.state !== "proposed" && <Badge tone={badgeTone(stateVariant)}>{t(DECIDED_LABEL[subject.state])}</Badge>}
          </div>
          {demand && <div style={{ fontSize: 11.5, color: K.muted, lineHeight: 1.5 }}>{demand}</div>}
          {pickError && (
            <div style={{
              marginTop: 6, padding: "6px 8px", background: K.dangerSoft, border: `1px solid ${K.dangerBorder}`,
              borderRadius: 6, fontSize: 11, color: K.danger, display: "flex", alignItems: "center", gap: 8,
            }}>
              <span>{pickError}</span>
              {/* Manual, explicit refresh — never automatic (FE audit #2) — so the tenant can
                  check the real state on their own terms instead of an auto-refresh that could
                  have shown this same error next to a row that actually succeeded. */}
              <LinkButton tone="danger" size="sm" onClick={onPicked} style={{ textDecoration: "underline", flexShrink: 0 }}>
                {t("refresh")}
              </LinkButton>
            </div>
          )}
          {/* AA-556 — same shape as pickError above, own Refresh (cut() failing never calls
              onPicked() itself, same FE-audit-#2 "only a confirmed success refetches" rule). */}
          {cutError && (
            <div style={{
              marginTop: 6, padding: "6px 8px", background: K.dangerSoft, border: `1px solid ${K.dangerBorder}`,
              borderRadius: 6, fontSize: 11, color: K.danger, display: "flex", alignItems: "center", gap: 8,
            }}>
              <span>{cutError}</span>
              <LinkButton tone="danger" size="sm" onClick={onPicked} style={{ textDecoration: "underline", flexShrink: 0 }}>
                {t("refresh")}
              </LinkButton>
            </div>
          )}
        </div>
        {subject.state === "proposed" && (
          <div style={{ display: "flex", gap: 6, flexShrink: 0 }}>
            <Button variant="primary" size="sm" disabled={picking} onClick={pick}>
              {picking ? t("picking") : <><Sparkles size={12} /> {t("pickToWrite")} <ChevronRight size={12} /></>}
            </Button>
            {/* AA-556 — disabled while picking/isExpanded/cutting: UX polish only (see file
                header) — the backend already 409s a stale/racing cut safely on its own. */}
            <Button variant="ghost" size="sm" disabled={picking || isExpanded || cutting} onClick={cut}>
              {cutting ? t("skipping") : <><X size={12} /> {t("skipTopic")}</>}
            </Button>
          </div>
        )}
        {/* AA-668 — a Subject picked earlier in this visit reopens its Writer panel. */}
        {subject.state !== "proposed" && knownTarget && (
          <div style={{ display: "flex", gap: 6, flexShrink: 0 }}>
            <Button variant="secondary" size="sm" disabled={isExpanded} onClick={() => openWriter(knownTarget)}>
              <Sparkles size={12} /> {t("continue")} <ChevronRight size={12} />
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}
