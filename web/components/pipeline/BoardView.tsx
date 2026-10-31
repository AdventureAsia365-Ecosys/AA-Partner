"use client";
// web/components/pipeline/BoardView.tsx — the A-02 board (AA-936): the "Waiting on" chip row and owner toggle (links),
// one column per open stage, cards that open the trip and can be dragged along an allowed edge (the DB enforces the
// edges, a refused move shows a message). On a phone the columns scroll sideways, one stage per screen (WF-1).
// AA-943 (CRM-15): every crm-012 edge between the six open columns; a gated drop (Qualifying -> Proposal below 80 %,
// Proposal -> Booked) opens the gate dialog; every move and bulk move offers Undo for 10 s (design v3 §2.3, Fix B8,
// the API's Undo routes); each column's menu moves its trips at once (dry run -> preview count -> confirm); cards show
// who the trip waits on, a quiet flag after 3 days and the Stage 2 percentage on Inquiry / Qualifying; the Closed group
// (completed / lost / cancelled) sits under the board.
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { Badge, K } from "@/app/_kit";
import { BulkDialog, GateDialog, type BulkTarget } from "@/components/pipeline/StageDialogs";
import { toast, UndoToast, type ToastState } from "@/components/pipeline/UndoToast";
import { bffSend } from "@/lib/bffClient";
import type { DateLocale } from "@/lib/dates";
import { fmt, type Lang, type MessageKey, t } from "@/lib/i18n";
import {
  boardHref,
  bulkScope,
  canMove,
  codeLabel,
  dueIn,
  gateRefusal,
  moveErrorKey,
  MOVES,
  partyLabel,
  quietDays,
  stageLabel,
  STAGE2_GATE,
  waitingLabel,
  whenLabel,
  WAITING_ON,
  type Board,
  type BoardStage,
  type BulkPreview,
  type Card,
  type ClosedCard,
  type GateRefusal,
  type MoveResult,
  type Stage,
  type WaitingOn,
} from "@/lib/pipeline";

export function BoardView({ board, lang, locale }: { board: Board; lang: Lang; locale: DateLocale; dict: Record<MessageKey, string> }) {
  const router = useRouter();
  const [stages, setStages] = useState(board.stages);
  const [toastState, setToast] = useState<ToastState | null>(null);
  const [gate, setGate] = useState<{ tripId: string; from: BoardStage; to: BoardStage; refusal: GateRefusal } | null>(null);
  const [bulk, setBulk] = useState<BulkTarget | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const drag = useRef<{ tripId: string; from: BoardStage } | null>(null);
  const filtered = !!board.filter.waiting_on || board.filter.owner === "me";

  useEffect(() => setStages(board.stages), [board]);
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 60_000);
    return () => clearInterval(id);
  }, []);

  function moveLocal(tripId: string, from: BoardStage, to: BoardStage) {
    setStages((prev) => {
      const card = prev.find((s) => s.stage === from)?.cards.find((c) => c.trip_id === tripId);
      if (!card) return prev;
      return prev.map((s) => {
        if (s.stage === from) return { ...s, count: s.count - 1, cards: s.cards.filter((c) => c.trip_id !== tripId) };
        if (s.stage === to) return { ...s, count: s.count + 1, cards: [{ ...card, stage: to }, ...s.cards] };
        return s;
      });
    });
  }

  async function undoMove(tripId: string, historyId: string) {
    const res = await bffSend<{ dispatch_cancelled: number; stage: Stage }>("POST", `trips/${tripId}/stage/undo`, { history_id: historyId });
    if (res.ok) {
      const n = res.data.dispatch_cancelled;
      setToast(toast(n > 0 ? fmt(t(lang, "undo.undoneCancelled"), { n }) : t(lang, "pipeline.undone"), { data: { "undone-stage": res.data.stage, "dispatch-cancelled": n } }));
    } else setToast(toast(t(lang, moveErrorKey(res.detail)), { tone: "error" }));
    router.refresh();
  }

  async function move(tripId: string, from: BoardStage, to: BoardStage, override?: string): Promise<boolean> {
    if (from === to) return false;
    if (!canMove(from, to)) {
      setToast(toast(t(lang, "pipeline.moveFailed"), { tone: "error" }));
      return false;
    }
    moveLocal(tripId, from, to);
    const res = await bffSend<MoveResult>("POST", `trips/${tripId}/stage`, { to, ...(override ? { override_reason: override } : {}) });
    if (!res.ok) {
      moveLocal(tripId, to, from);
      const refusal = gateRefusal(res.body, from, to);
      if (refusal && !override) setGate({ tripId, from, to, refusal });
      else setToast(toast(t(lang, moveErrorKey(res.detail)), { tone: "error" }));
      return false;
    }
    const data = res.data;
    const stage = stageLabel(lang, data.stage);
    const message = data.stage2_sent
      ? fmt(t(lang, "undo.movedSent"), { stage })
      : data.stage2_skipped
        ? fmt(t(lang, "undo.notSent"), { stage, why: t(lang, `skip.${data.stage2_skipped}` as MessageKey) ?? t(lang, "skip.other") })
        : fmt(t(lang, "pipeline.moved"), { stage });
    const historyId = data.undo?.history_id;
    setToast(toast(message, { undo: historyId ? () => undoMove(tripId, historyId) : undefined, data: { stage: data.stage, "stage2-sent": data.stage2_sent ? "true" : "false" } }));
    router.refresh();
    return true;
  }

  function bulkDone(result: BulkPreview) {
    setBulk(null);
    const stage = stageLabel(lang, result.to);
    const batch = result.batch_id;
    setToast(
      toast(fmt(t(lang, "pipeline.bulkMoved"), { n: result.moved ?? result.eligible.length, stage }), {
        undo: batch
          ? async () => {
              const res = await bffSend<{ undone: number; dispatch_cancelled: number }>("POST", "pipeline/bulk-stage/undo", { batch_id: batch });
              setToast(res.ok ? toast(t(lang, "pipeline.bulkUndone"), { data: { undone: res.data.undone } }) : toast(t(lang, moveErrorKey(res.detail)), { tone: "error" }));
              router.refresh();
            }
          : undefined,
        data: { moved: result.moved ?? 0 },
      }),
    );
    router.refresh();
  }

  const active = board.filter.waiting_on;
  return (
    <div style={{ display: "grid", gap: 12, minWidth: 0 }}>
      <nav aria-label={t(lang, "pipeline.waitingOn")} style={{ display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center", fontSize: 13 }}>
        <span style={{ color: K.muted, marginRight: 4 }}>{t(lang, "pipeline.waitingOn")}:</span>
        <Chip href={boardHref({ owner: board.filter.owner })} on={!active} testId="chip-all">
          {t(lang, "pipeline.all")} {board.total}
        </Chip>
        {WAITING_ON.filter((w) => w !== "none").map((w: WaitingOn) => (
          <Chip key={w} href={boardHref({ waiting_on: w, owner: board.filter.owner })} on={active === w} testId={`chip-${w}`}>
            {waitingLabel(lang, w)} {board.waiting_on_counts[w] ?? 0}
          </Chip>
        ))}
        <span style={{ color: K.line2, margin: "0 6px" }}>|</span>
        <span style={{ color: K.muted }}>{t(lang, "pipeline.owner")}:</span>
        <Chip href={boardHref({ waiting_on: active, owner: "anyone" })} on={board.filter.owner === "anyone"} testId="owner-anyone">
          {t(lang, "pipeline.owner.anyone")}
        </Chip>
        <Chip href={boardHref({ waiting_on: active, owner: "me" })} on={board.filter.owner === "me"} testId="owner-me">
          {t(lang, "pipeline.owner.me")}
        </Chip>
      </nav>
      <p style={{ margin: 0, fontSize: 12, color: K.muted }}>{t(lang, "pipeline.dragHint")}</p>
      <div
        data-testid="board"
        style={{ display: "grid", gridAutoFlow: "column", gridAutoColumns: "minmax(250px, 1fr)", gap: 12, overflowX: "auto", scrollSnapType: "x mandatory", paddingBottom: 6 }}
      >
        {stages.map((column) => {
          const targets = (MOVES[column.stage] ?? []).filter((s) => s !== "cancelled");
          return (
            <section
              key={column.stage}
              data-testid={`column-${column.stage}`}
              aria-label={stageLabel(lang, column.stage)}
              onDragOver={(e) => {
                const d = drag.current;
                if (d && canMove(d.from, column.stage)) e.preventDefault();
              }}
              onDrop={(e) => {
                e.preventDefault();
                const d = drag.current;
                drag.current = null;
                if (d) void move(d.tripId, d.from, column.stage);
              }}
              style={{ scrollSnapAlign: "start", background: K.bg, border: `1px solid ${K.line}`, borderRadius: 10, padding: 10, display: "grid", alignContent: "start", gap: 8, minHeight: 160 }}
            >
              <header style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 6 }}>
                <h2 style={{ margin: 0, fontSize: 13, fontWeight: 600, color: K.ink }}>{stageLabel(lang, column.stage)}</h2>
                <span style={{ display: "flex", gap: 6, alignItems: "baseline" }}>
                  <span style={{ fontSize: 12, color: K.muted }}>{column.count}</span>
                  {column.cards.length > 0 && targets.length > 0 && (
                    <ColumnMenu
                      lang={lang}
                      stage={column.stage}
                      targets={targets}
                      onPick={(to) => setBulk({ from: column.stage, to, scope: bulkScope(column, board.filter) })}
                    />
                  )}
                </span>
              </header>
              {column.cards.map((card) => (
                <TripCard key={card.trip_id} card={card} lang={lang} locale={locale} now={now} onDrag={() => (drag.current = { tripId: card.trip_id, from: column.stage })} />
              ))}
              {column.truncated && (
                <span style={{ fontSize: 12, color: K.muted }}>{fmt(t(lang, "pipeline.more"), { n: column.count - column.cards.length })}</span>
              )}
              {column.cards.length === 0 && <span style={{ fontSize: 11.5, color: K.muted2 }}>{t(lang, "pipeline.later")}</span>}
            </section>
          );
        })}
      </div>
      {board.closed && <ClosedGroup closed={board.closed} lang={lang} locale={locale} />}
      <UndoToast state={toastState} lang={lang} onClose={() => setToast(null)} testId="board-toast" />
      {gate && (
        <GateDialog
          refusal={gate.refusal}
          lang={lang}
          onClose={() => setGate(null)}
          onConfirm={async (reason) => {
            // Closed either way: a refusal of the override shows in the toast.
            await move(gate.tripId, gate.from, gate.to, reason);
            setGate(null);
          }}
        />
      )}
      {bulk && <BulkDialog target={bulk} lang={lang} filtered={filtered} onClose={() => setBulk(null)} onDone={bulkDone} />}
    </div>
  );
}

function ColumnMenu({ lang, stage, targets, onPick }: { lang: Lang; stage: BoardStage; targets: Stage[]; onPick: (to: Stage) => void }) {
  const ref = useRef<HTMLDetailsElement>(null);
  return (
    <details ref={ref} data-testid={`column-menu-${stage}`} style={{ position: "relative" }}>
      <summary aria-label={t(lang, "pipeline.bulkMenu")} title={t(lang, "pipeline.bulkMenu")} style={{ listStyle: "none", cursor: "pointer", color: K.muted, fontSize: 14, padding: "0 4px", lineHeight: 1 }}>
        ⋯
      </summary>
      <div role="menu" style={{ position: "absolute", right: 0, top: "100%", zIndex: 10, background: K.card, border: `1px solid ${K.line}`, borderRadius: 8, padding: 4, display: "grid", minWidth: 180, boxShadow: "0 8px 24px rgba(31,41,51,0.12)" }}>
        {targets.map((to) => (
          <button
            key={to}
            type="button"
            role="menuitem"
            data-testid={`bulk-to-${to}`}
            onClick={() => {
              if (ref.current) ref.current.open = false;
              onPick(to);
            }}
            style={{ font: "inherit", fontSize: 12.5, textAlign: "left", background: "transparent", border: 0, padding: "6px 8px", borderRadius: 6, color: K.ink, cursor: "pointer" }}
          >
            {fmt(t(lang, "pipeline.bulkTo"), { stage: stageLabel(lang, to) })}
          </button>
        ))}
      </div>
    </details>
  );
}

function ClosedGroup({ closed, lang, locale }: { closed: NonNullable<Board["closed"]>; lang: Lang; locale: DateLocale }) {
  return (
    <details data-testid="board-closed" style={{ background: K.bg, border: `1px solid ${K.line}`, borderRadius: 10, padding: "8px 10px" }}>
      <summary style={{ cursor: "pointer", fontSize: 13, fontWeight: 600, color: K.ink }}>
        {t(lang, "pipeline.closed")} <span style={{ fontWeight: 400, color: K.muted }}>{closed.count}</span>
        <span style={{ fontWeight: 400, color: K.muted, fontSize: 12 }}>
          {" · "}
          {(["completed", "lost", "cancelled"] as const).map((s) => `${stageLabel(lang, s)} ${closed.counts[s] ?? 0}`).join(" · ")}
        </span>
      </summary>
      <p style={{ margin: "6px 0", fontSize: 12, color: K.muted }}>{t(lang, "pipeline.closedHelp")}</p>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(min(240px, 100%), 1fr))", gap: 8 }}>
        {closed.cards.map((card) => (
          <ClosedTripCard key={card.trip_id} card={card} lang={lang} locale={locale} />
        ))}
      </div>
    </details>
  );
}

function ClosedTripCard({ card, lang, locale }: { card: ClosedCard; lang: Lang; locale: DateLocale }) {
  const when = whenLabel(card, locale);
  return (
    <a href={`/pipeline/${card.trip_id}`} data-testid="closed-card" data-stage={card.stage} style={{ display: "grid", gap: 4, background: K.card, border: `1px solid ${K.line}`, borderRadius: 8, padding: "8px 10px", textDecoration: "none", color: K.ink }}>
      <strong style={{ fontSize: 13, fontWeight: 600, overflowWrap: "anywhere" }}>{card.title}</strong>
      <span style={{ fontSize: 12, color: K.body }}>{[partyLabel(card), when].filter(Boolean).join(" · ")}</span>
      <span>
        <Badge tone={card.stage === "completed" ? "success" : card.stage === "cancelled" ? "danger" : "neutral"}>{stageLabel(lang, card.stage)}</Badge>
      </span>
    </a>
  );
}

function Chip({ href, on, children, testId }: { href: string; on: boolean; children: React.ReactNode; testId: string }) {
  return (
    <a
      href={href}
      data-testid={testId}
      aria-current={on ? "true" : undefined}
      style={{ padding: "4px 10px", borderRadius: 999, textDecoration: "none", border: `1px solid ${on ? K.accent : K.line2}`, background: on ? K.accentTint : K.card, color: on ? K.accentDeep : K.ink2 }}
    >
      {children}
    </a>
  );
}

function TripCard({ card, lang, locale, now, onDrag }: { card: Card; lang: Lang; locale: DateLocale; now: number; onDrag: () => void }) {
  const due = dueIn(card.next_due_at, now);
  const when = whenLabel(card, locale);
  const quiet = quietDays(card);
  // "Me" on the trips the viewer owns (and unassigned ones), "Agency" on a colleague's (design v3 §2.7 F1).
  const waiting =
    card.waiting_on === "agency" && !card.is_mine && card.owner_member_id ? t(lang, "waiting.agencyOther") : waitingLabel(lang, card.waiting_on);
  // The API sends it on Inquiry / Qualifying cards only.
  const s2 = typeof card.stage2_percent === "number" ? card.stage2_percent : null;
  return (
    <a
      href={`/pipeline/${card.trip_id}`}
      draggable
      onDragStart={(e) => {
        e.dataTransfer.setData("text/plain", card.trip_id);
        onDrag();
      }}
      data-testid="trip-card"
      data-trip-id={card.trip_id}
      data-waiting={card.waiting_on}
      data-quiet={quiet !== null ? "true" : undefined}
      style={{ display: "grid", gap: 4, background: K.card, border: `1px solid ${K.line}`, borderRadius: 8, padding: "9px 10px", textDecoration: "none", color: K.ink, cursor: "grab" }}
    >
      <strong style={{ fontSize: 13.5, fontWeight: 600, overflowWrap: "anywhere" }}>{card.title}</strong>
      <span style={{ fontSize: 12.5, color: K.body, overflowWrap: "anywhere" }} data-testid="trip-card-party">
        {[partyLabel(card), when, card.budget_band ? codeLabel(lang, "budget", card.budget_band) : null].filter(Boolean).join(" · ")}
      </span>
      <span style={{ display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center", fontSize: 12 }}>
        <Badge tone={card.waiting_on === "agency" ? "accent" : card.waiting_on === "aa" ? "info" : "neutral"}>
          {waiting}
          {card.waiting_on === "agency" && due && !due.overdue ? ` · ${fmt(t(lang, "pipeline.replyIn"), { t: due.label })}` : ""}
        </Badge>
        {due?.overdue && <Badge tone="danger">{t(lang, "pipeline.overdue")}</Badge>}
        {card.cancellation_pending && (
          <span data-testid="trip-card-cancellation-pending" style={{ display: "contents" }}>
            <Badge tone="danger">{t(lang, "trip.cancellationPending")}</Badge>
          </span>
        )}
        {quiet !== null && (
          <span data-testid="trip-card-quiet" style={{ display: "contents" }}>
            <Badge tone="warning">{fmt(t(lang, "pipeline.quiet"), { n: quiet })}</Badge>
          </span>
        )}
        {s2 !== null && (
          <span data-testid="trip-card-s2" data-percent={s2} style={{ display: "contents" }}>
            <Badge tone={s2 >= STAGE2_GATE ? "success" : "neutral"} dot={false}>
              {fmt(t(lang, "pipeline.stage2"), { n: s2 })}
            </Badge>
          </span>
        )}
        {card.submissions > 1 && <Badge tone="info" dot={false}>{fmt(t(lang, "pipeline.inquiries"), { n: card.submissions })}</Badge>}
        {!card.owner_member_id && <span style={{ color: K.muted }}>{t(lang, "pipeline.unassigned")}</span>}
      </span>
    </a>
  );
}
