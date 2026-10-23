"use client";
// web/components/pipeline/BoardView.tsx — the A-02 board (AA-936): the "Waiting on" chip row and owner toggle (links),
// one column per open stage, cards that open the trip and can be dragged between the columns Slice 1 opens
// (Inquiry <-> Qualifying; the DB enforces the edges, a refused move shows a message). Every move can be undone for
// 10 s (design v3 §2.3 "Undo toast"). On a phone the columns scroll sideways, one stage per screen (WF-1).
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { Badge, K } from "@/app/_kit";
import { bffSend } from "@/lib/bffClient";
import type { DateLocale } from "@/lib/dates";
import { fmt, type Lang, type MessageKey, t } from "@/lib/i18n";
import {
  boardHref,
  canMove,
  codeLabel,
  dueIn,
  partyLabel,
  stageLabel,
  waitingLabel,
  whenLabel,
  WAITING_ON,
  type Board,
  type BoardStage,
  type Card,
  type WaitingOn,
} from "@/lib/pipeline";

const UNDO_MS = 10_000;

interface Undo {
  tripId: string;
  from: BoardStage;
  to: BoardStage;
}

export function BoardView({ board, lang, locale }: { board: Board; lang: Lang; locale: DateLocale; dict: Record<MessageKey, string> }) {
  const router = useRouter();
  const [stages, setStages] = useState(board.stages);
  const [undo, setUndo] = useState<Undo | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const drag = useRef<{ tripId: string; from: BoardStage } | null>(null);

  useEffect(() => setStages(board.stages), [board]);
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 60_000);
    return () => clearInterval(id);
  }, []);
  useEffect(() => {
    if (!undo) return;
    const id = setTimeout(() => setUndo(null), UNDO_MS);
    return () => clearTimeout(id);
  }, [undo]);

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

  async function move(tripId: string, from: BoardStage, to: BoardStage, isUndo = false) {
    if (from === to) return;
    if (!canMove(from, to)) {
      setMessage(t(lang, "pipeline.moveFailed"));
      return;
    }
    moveLocal(tripId, from, to);
    const res = await bffSend("POST", `trips/${tripId}/stage`, { to, undo: isUndo });
    if (!res.ok) {
      moveLocal(tripId, to, from);
      setMessage(t(lang, "pipeline.moveFailed"));
      return;
    }
    setMessage(isUndo ? t(lang, "pipeline.undone") : null);
    setUndo(isUndo ? null : { tripId, from, to });
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
          const open = column.stage === "inquiry" || column.stage === "qualifying";
          return (
            <section
              key={column.stage}
              data-testid={`column-${column.stage}`}
              aria-label={stageLabel(lang, column.stage)}
              onDragOver={(e) => {
                if (open) e.preventDefault();
              }}
              onDrop={(e) => {
                e.preventDefault();
                const d = drag.current;
                drag.current = null;
                if (d) void move(d.tripId, d.from, column.stage);
              }}
              style={{ scrollSnapAlign: "start", background: K.bg, border: `1px solid ${K.line}`, borderRadius: 10, padding: 10, display: "grid", alignContent: "start", gap: 8, minHeight: 160, opacity: open ? 1 : 0.7 }}
            >
              <header style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
                <h2 style={{ margin: 0, fontSize: 13, fontWeight: 600, color: K.ink }}>{stageLabel(lang, column.stage)}</h2>
                <span style={{ fontSize: 12, color: K.muted }}>{column.count}</span>
              </header>
              {column.cards.map((card) => (
                <TripCard key={card.trip_id} card={card} lang={lang} locale={locale} now={now} onDrag={() => (drag.current = { tripId: card.trip_id, from: column.stage })} />
              ))}
              {column.truncated && (
                <span style={{ fontSize: 12, color: K.muted }}>{fmt(t(lang, "pipeline.more"), { n: column.count - column.cards.length })}</span>
              )}
              {!open && column.cards.length === 0 && <span style={{ fontSize: 11.5, color: K.muted2 }}>{t(lang, "pipeline.later")}</span>}
            </section>
          );
        })}
      </div>
      {(undo || message) && (
        <div role="status" data-testid="board-toast" style={{ position: "sticky", bottom: 12, justifySelf: "center", display: "flex", gap: 12, alignItems: "center", background: K.ink, color: K.card, padding: "8px 14px", borderRadius: 999, fontSize: 13 }}>
          <span>{undo ? fmt(t(lang, "pipeline.moved"), { stage: stageLabel(lang, undo.to) }) : message}</span>
          {undo && (
            <button type="button" data-testid="board-undo" onClick={() => void move(undo.tripId, undo.to, undo.from, true)} style={{ font: "inherit", background: "transparent", border: 0, color: K.accentSoft, cursor: "pointer", fontWeight: 600 }}>
              {t(lang, "pipeline.undo")}
            </button>
          )}
        </div>
      )}
    </div>
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
  // "Me" on the trips the viewer owns (and unassigned ones), "Agency" on a colleague's (design v3 §2.7 F1).
  const waiting =
    card.waiting_on === "agency" && !card.is_mine && card.owner_member_id ? t(lang, "waiting.agencyOther") : waitingLabel(lang, card.waiting_on);
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
      style={{ display: "grid", gap: 4, background: K.card, border: `1px solid ${K.line}`, borderRadius: 8, padding: "9px 10px", textDecoration: "none", color: K.ink, cursor: "grab" }}
    >
      <strong style={{ fontSize: 13.5, fontWeight: 600, overflowWrap: "anywhere" }}>{card.title}</strong>
      <span style={{ fontSize: 12.5, color: K.body, overflowWrap: "anywhere" }} data-testid="trip-card-party">
        {[partyLabel(card), when, card.budget_band ? codeLabel(lang, "budget", card.budget_band) : null].filter(Boolean).join(" · ")}
      </span>
      <span style={{ display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center", fontSize: 12 }}>
        <Badge tone={card.waiting_on === "agency" ? "accent" : "neutral"}>
          {waiting}
          {card.waiting_on === "agency" && due && !due.overdue ? ` · ${fmt(t(lang, "pipeline.replyIn"), { t: due.label })}` : ""}
        </Badge>
        {due?.overdue && <Badge tone="danger">{t(lang, "pipeline.overdue")}</Badge>}
        {card.submissions > 1 && <Badge tone="info" dot={false}>{fmt(t(lang, "pipeline.inquiries"), { n: card.submissions })}</Badge>}
        {!card.owner_member_id && <span style={{ color: K.muted }}>{t(lang, "pipeline.unassigned")}</span>}
      </span>
    </a>
  );
}
