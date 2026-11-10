"use client";
// AA-636 — the top-bar bell had no handler. It now opens a panel of recent tour activity
// (the same /v1/billing `activity` the shell already loads), with an unread dot driven by a
// per-browser "last seen" timestamp. No new backend: notifications are a view of activity.
// AA-759: lives in the PortalShell topbar; the panel is a disclosure region (not a dialog: it does
// not trap focus), so the kit rule against hand-rolled dialogs holds.
// AA-986 L3 (root ADR 0015, Q7): the bell also reads the tenant's REAL notifications, GET /v1/notifications
// (tour.discontinued / tour.reinstated, written by the tour lifecycle service when AA stops or resumes serving a
// tour the tenant uses). They come first, with a server-side unread count; opening the panel marks them read
// (POST /v1/notifications/read-all). The badges on My Tours / Content / Publish stay the source of truth.
// AA-1039 (option A): a `tour.pages_to_put_back` row (the tour is back on sale; WordPress pages moved to draft while it
// was off sale stay in draft) lists each page with its own "Put back online" button (putBackApi.ts), one click each;
// nothing is published until the tenant clicks. The same pages are on Publish (PutBackList, #put-back).
// AA-1039 follow-up (tech lead decision S276): a `tour.pages_to_review` row (AA no longer offers the tour; the agency's
// WordPress pages about it stay online) lists each page with its own "Move to draft" button (retiredPagesApi.ts), in the
// UI language; nothing changes on the site until the agency clicks, unless it turned automatic drafts on (a page then
// shows its draft date). The same pages are on Publish (RetiredPagesList, #retired-pages).
import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Bell, Check, FileDown, X, RefreshCw, RotateCcw, UploadCloud } from "lucide-react";
import { Button, IconButton, apiGet, apiSend, K, sans, useLang, useT, type Translator } from "@/app/_kit";
import { BELL_MESSAGES } from "./account.messages";
import { PUT_BACK_MESSAGES } from "./putBack.messages";
import { PUT_BACK_ANCHOR, PUT_BACK_QUERY_KEY, isOnline, outcomeText, putBack, type PutBackResult } from "./putBackApi";
import { RETIRED_PAGES_MESSAGES } from "./retiredPages.messages";
import { RETIRED_PAGES_ANCHOR, RETIRED_PAGES_QUERY_KEY, moveOutcomeText, moveToDraft } from "./retiredPagesApi";
import { plainError } from "./studioError";
import { fmtDateTime } from "@/lib/studio/format";
import type { ShellActivityView } from "./PortalShellContext";

/** One row of GET /v1/notifications (api/routers/v1_notifications.py). */
export interface TenantNotification {
  id: number;
  event_type: string;
  title: string;
  message: string;
  tour_id: string | null;
  tour_name: string | null;
  reason: string | null;
  live_posts: number;
  /** AA-1039: the drafted pages of a `tour.pages_to_put_back` row, or (S276) the live pages of a `tour.pages_to_review`
   *  row with `draft_on` (the automatic draft day of an agency that turned it on, else null); [] for any other row. */
  pages?: { publish_id: string; url: string | null; draft_on?: string | null }[];
  is_read: boolean;
  created_at: string | null;
}

export const TENANT_NOTIFICATIONS_KEY = ["tenant-notifications"] as const;

type Activity = ShellActivityView;

const SEEN_KEY = "cis_portal_notif_seen_at";

function readSeen(): number {
  try { return Number(localStorage.getItem(SEEN_KEY) ?? 0) || 0; } catch { return 0; }
}
function writeSeen(ts: number) {
  try { localStorage.setItem(SEEN_KEY, String(ts)); } catch { /* storage blocked — dot just stays */ }
}

// AA-760 — the sentence follows the job state the shell joined in, never the row flags alone.
const describe = (a: Activity, t: Translator<typeof BELL_MESSAGES.en>) => {
  switch (a.state) {
    case "approved": return t("approved");
    case "rejected": return t("rejected");
    case "failed": return t("failed");
    case "queued": case "writing": case "checking": return t("writing");
    case "done": return t("done");
    case "edited": return t("edited");
    default: return t("updated");
  }
};

const PAGES_TO_PUT_BACK = "tour.pages_to_put_back";
const PAGES_TO_REVIEW = "tour.pages_to_review";

/** One restore notice with drafted pages: the sentence, then each page with "Put back online" (one click, one call). */
export function PutBackNote({ note, onNavigate }: { note: TenantNotification; onNavigate?: () => void }) {
  const t = useT(PUT_BACK_MESSAGES);
  const lang = useLang();
  const qc = useQueryClient();
  const [done, setDone] = useState<Record<string, { text: string; ok: boolean }>>({});
  const [busy, setBusy] = useState<string | null>(null);

  async function click(publishId: string) {
    if (busy) return;
    setBusy(publishId);
    try {
      const r: PutBackResult = await putBack(publishId);
      setDone(d => ({ ...d, [publishId]: { text: outcomeText(t, r), ok: isOnline(r) } }));
      qc.invalidateQueries({ queryKey: PUT_BACK_QUERY_KEY });
    } catch (e) {
      setDone(d => ({ ...d, [publishId]: { text: plainError(e, t("failed"), lang), ok: false } }));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div data-testid={`bell-put-back-${note.id}`} style={{
      display: "grid", gridTemplateColumns: "30px minmax(0,1fr)", gap: 10, padding: "11px 16px",
      borderBottom: `1px solid ${K.line2}`, background: note.is_read ? "transparent" : K.accentTint,
    }}>
      <span style={{ width: 30, height: 30, borderRadius: 8, background: K.successSoft, color: K.success, display: "grid", placeItems: "center" }}>
        <UploadCloud size={14} strokeWidth={2.2} />
      </span>
      <span style={{ minWidth: 0 }}>
        <Link href={`/portal/content/publish#${PUT_BACK_ANCHOR}`} onClick={onNavigate}
          style={{ display: "block", fontSize: 12.5, color: K.ink, lineHeight: 1.4, textDecoration: "none" }}>
          <strong style={{ fontWeight: 600 }}>{note.title}</strong> · {note.message}
        </Link>
        {(note.pages ?? []).map(p => {
          const result = done[p.publish_id];
          return (
            <span key={p.publish_id} style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 6, flexWrap: "wrap" }}>
              <span style={{ fontSize: 11.5, color: K.muted, minWidth: 0, overflowWrap: "anywhere", flex: "1 1 140px" }}>{p.url}</span>
              {result ? (
                <span data-testid={`bell-put-back-result-${p.publish_id}`}
                  style={{ fontSize: 11.5, color: result.ok ? K.success : K.muted, flex: "1 1 100%" }}>{result.text}</span>
              ) : (
                <Button size="sm" variant="primary" disabled={!!busy} onClick={() => click(p.publish_id)}
                  testId={`bell-put-back-button-${p.publish_id}`} style={{ whiteSpace: "nowrap" }}>
                  {busy === p.publish_id ? t("puttingBack") : t("putBack")}
                </Button>
              )}
            </span>
          );
        })}
      </span>
    </div>
  );
}

/** S276: a retirement notice with live WordPress pages: the sentence in the UI language, then each page with "Move to
 *  draft" (one click, one call). Nothing changes on the site before the click. */
export function ReviewPagesNote({ note, onNavigate }: { note: TenantNotification; onNavigate?: () => void }) {
  const t = useT(RETIRED_PAGES_MESSAGES);
  const lang = useLang();
  const qc = useQueryClient();
  const [done, setDone] = useState<Record<string, { text: string; ok: boolean }>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const pages = note.pages ?? [];

  async function click(publishId: string) {
    if (busy) return;
    setBusy(publishId);
    try {
      const r = await moveToDraft(publishId);
      setDone(d => ({ ...d, [publishId]: { text: moveOutcomeText(t, r), ok: true } }));
      qc.invalidateQueries({ queryKey: RETIRED_PAGES_QUERY_KEY });
    } catch (e) {
      setDone(d => ({ ...d, [publishId]: { text: plainError(e, t("failed"), lang), ok: false } }));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div data-testid={`bell-review-${note.id}`} style={{
      display: "grid", gridTemplateColumns: "30px minmax(0,1fr)", gap: 10, padding: "11px 16px",
      borderBottom: `1px solid ${K.line2}`, background: note.is_read ? "transparent" : K.accentTint,
    }}>
      <span style={{ width: 30, height: 30, borderRadius: 8, background: K.dangerSoft, color: K.danger, display: "grid", placeItems: "center" }}>
        <AlertTriangle size={14} strokeWidth={2.2} />
      </span>
      <span style={{ minWidth: 0 }}>
        <Link href={`/portal/content/publish#${RETIRED_PAGES_ANCHOR}`} onClick={onNavigate}
          style={{ display: "block", fontSize: 12.5, color: K.ink, lineHeight: 1.4, textDecoration: "none" }}>
          <strong style={{ fontWeight: 600 }}>{t("bellTitle")}</strong> · {t("bellBody", note.tour_name || t("aTour"), pages.length)}
          {pages.some(p => p.draft_on) ? ` ${t("bellDated")}` : ""}
        </Link>
        {pages.map(p => {
          const result = done[p.publish_id];
          return (
            <span key={p.publish_id} style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 6, flexWrap: "wrap" }}>
              <span style={{ fontSize: 11.5, color: K.muted, minWidth: 0, overflowWrap: "anywhere", flex: "1 1 140px" }}>
                {p.url}{p.draft_on ? ` · ${p.draft_on}` : ""}
              </span>
              {result ? (
                <span data-testid={`bell-review-result-${p.publish_id}`}
                  style={{ fontSize: 11.5, color: result.ok ? K.success : K.muted, flex: "1 1 100%" }}>{result.text}</span>
              ) : (
                <Button size="sm" variant="secondary" disabled={!!busy} onClick={() => click(p.publish_id)}
                  testId={`bell-move-to-draft-button-${p.publish_id}`} style={{ whiteSpace: "nowrap" }}>
                  <FileDown size={12} /> {busy === p.publish_id ? t("moving") : t("moveToDraft")}
                </Button>
              )}
            </span>
          );
        })}
      </span>
    </div>
  );
}

export default function NotificationsBell({ activity }: { activity: Activity[] }) {
  const t = useT(BELL_MESSAGES);
  const lang = useLang();
  const [open, setOpen] = useState(false);
  const [seenAt, setSeenAt] = useState<number>(() => (typeof window === "undefined" ? 0 : readSeen()));
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onDown); document.removeEventListener("keydown", onKey); };
  }, [open]);

  const qc = useQueryClient();
  const { data: notes } = useQuery({
    queryKey: TENANT_NOTIFICATIONS_KEY,
    queryFn: () => apiGet<{ items?: TenantNotification[]; unread?: number }>("/api/tenant/v1/notifications?limit=20"),
    refetchInterval: 120_000,
    staleTime: 60_000,
  });
  const markAllRead = useMutation({
    mutationFn: () => apiSend<{ cleared: number }>("/api/tenant/v1/notifications/read-all", { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: TENANT_NOTIFICATIONS_KEY }),
  });
  const tourNotes = notes?.items ?? [];
  const serverUnread = notes?.unread ?? 0;

  const newest = activity.reduce((m, a) => Math.max(m, Date.parse(a.created_at) || 0), 0);
  const unread = activity.filter(a => (Date.parse(a.created_at) || 0) > seenAt).length + serverUnread;

  function toggle() {
    const next = !open;
    setOpen(next);
    if (next && newest > seenAt) { writeSeen(newest); setSeenAt(newest); }
    if (next && serverUnread > 0 && !markAllRead.isPending) markAllRead.mutate();
  }

  return (
    <div ref={ref} style={{ position: "relative" }}>
      <IconButton variant="plain" size="lg" onClick={toggle} label={unread ? t("ariaNew", unread) : t("title")} title=""
        aria-expanded={open} style={{ background: open ? K.accentTint : "transparent", position: "relative" }}>
        <Bell size={15} />
        {unread > 0 && (
          <span style={{ position: "absolute", top: 6, right: 7, width: 8, height: 8, borderRadius: "50%", background: K.accent, boxShadow: `0 0 0 2px ${K.card}` }} />
        )}
      </IconButton>
      {open && (
        <div role="region" aria-label={t("title")} style={{
          position: "absolute", right: 0, top: 44, width: "min(360px, calc(100vw - 32px))", background: K.card,
          border: `1px solid ${K.line}`, borderRadius: 12, boxShadow: K.shadowLg, zIndex: 50, overflow: "hidden", fontFamily: sans,
        }}>
          <div style={{ padding: "12px 16px", borderBottom: `1px solid ${K.line2}`, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <span style={{ fontSize: 13, fontWeight: 600, color: K.ink }}>{t("title")}</span>
            <Link href="/portal/account/activity" onClick={() => setOpen(false)} style={{ fontSize: 12, color: K.ink3, textDecoration: "none" }}>{t("viewAll")}</Link>
          </div>
          {tourNotes.slice(0, 5).map(n => {
            if (n.event_type === PAGES_TO_PUT_BACK) {
              return <PutBackNote key={`n-${n.id}`} note={n} onNavigate={() => setOpen(false)} />;
            }
            if (n.event_type === PAGES_TO_REVIEW) {
              return <ReviewPagesNote key={`n-${n.id}`} note={n} onNavigate={() => setOpen(false)} />;
            }
            const gone = n.event_type === "tour.discontinued";
            const Icon = gone ? AlertTriangle : RotateCcw;
            return (
              <Link key={`n-${n.id}`} href={n.live_posts > 0 ? "/portal/content/publish" : "/portal/tours/mine"}
                onClick={() => setOpen(false)} style={{
                  display: "grid", gridTemplateColumns: "30px minmax(0,1fr)", gap: 10, padding: "11px 16px",
                  textDecoration: "none", borderBottom: `1px solid ${K.line2}`,
                  background: n.is_read ? "transparent" : K.accentTint,
                }}>
                <span style={{ width: 30, height: 30, borderRadius: 8, background: gone ? K.dangerSoft : K.successSoft, color: gone ? K.danger : K.success, display: "grid", placeItems: "center" }}>
                  <Icon size={14} strokeWidth={2.2} />
                </span>
                <span style={{ minWidth: 0 }}>
                  <span style={{ display: "block", fontSize: 12.5, color: K.ink, lineHeight: 1.4 }}>
                    <strong style={{ fontWeight: 600 }}>{n.title}</strong> · {n.message}
                  </span>
                  <span style={{ display: "block", fontSize: 11, color: K.muted2, marginTop: 2 }}>{fmtDateTime(n.created_at, lang)}</span>
                </span>
              </Link>
            );
          })}
          {activity.length === 0 && tourNotes.length === 0 ? (
            <div style={{ padding: "28px 16px", textAlign: "center", fontSize: 13, color: K.muted2 }}>{t("caughtUp")}</div>
          ) : activity.slice(0, 6).map(a => {
            const bad = a.state === "rejected" || a.state === "failed";
            const Icon = a.state === "approved" ? Check : bad ? X : RefreshCw;
            const color = a.state === "approved" ? K.success : bad ? K.danger : K.amber;
            const bg = a.state === "approved" ? K.successSoft : bad ? K.dangerSoft : K.accentTint;
            return (
              <Link key={a.id} href="/portal/tours/mine" onClick={() => setOpen(false)} style={{
                display: "grid", gridTemplateColumns: "30px minmax(0,1fr)", gap: 10, padding: "11px 16px", textDecoration: "none",
                borderBottom: `1px solid ${K.line2}`,
              }}>
                <span style={{ width: 30, height: 30, borderRadius: 8, background: bg, color, display: "grid", placeItems: "center" }}><Icon size={14} strokeWidth={2.2} /></span>
                <span style={{ minWidth: 0 }}>
                  <span style={{ display: "block", fontSize: 12.5, color: K.ink, lineHeight: 1.4 }}>
                    <strong style={{ fontWeight: 600 }}>{a.tour_name || t("aTour")}</strong> {describe(a, t)}
                  </span>
                  <span style={{ display: "block", fontSize: 11, color: K.muted2, marginTop: 2 }}>{fmtDateTime(a.created_at, lang)}</span>
                </span>
              </Link>
            );
          })}
        </div>
      )}
    </div>
  );
}
