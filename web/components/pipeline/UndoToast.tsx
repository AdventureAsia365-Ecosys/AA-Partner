"use client";
// web/components/pipeline/UndoToast.tsx — the Undo toast of design v3 §2.3 / Fix B8 (AA-943, CRM-15). Every move, bulk
// move and auto-sent planning form shows one: what happened, an Undo button and the seconds left. It closes itself
// after 10 s (the API's window; the database allows 15 s, so a click in the last second still lands). A toast without
// an undo (a refusal, "Move undone") closes after the same time. One toast at a time: a new one replaces the last.
import { useEffect, useState } from "react";
import { K } from "@/app/_kit";
import { fmt, type Lang, t } from "@/lib/i18n";
import { UNDO_MS } from "@/lib/pipeline";

export interface ToastState {
  /** Changes for every new toast, so the timer restarts. */
  id: number;
  message: string;
  /** Takes the action back; absent for a plain message. */
  undo?: () => Promise<void> | void;
  tone?: "info" | "error";
  /** Extra data attributes for E2E (e.g. the dispatch rows an undo cancelled). */
  data?: Record<string, string | number>;
}

let counter = 0;
export function toast(message: string, extra: Omit<ToastState, "id" | "message"> = {}): ToastState {
  counter += 1;
  return { id: counter, message, ...extra };
}

export function UndoToast({ state, lang, onClose, testId = "undo-toast" }: { state: ToastState | null; lang: Lang; onClose: () => void; testId?: string }) {
  const [left, setLeft] = useState(UNDO_MS / 1000);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!state) return;
    const started = Date.now();
    setLeft(UNDO_MS / 1000);
    setBusy(false);
    const tick = setInterval(() => setLeft(Math.max(0, Math.ceil((UNDO_MS - (Date.now() - started)) / 1000))), 1000);
    const close = setTimeout(onClose, UNDO_MS);
    return () => {
      clearInterval(tick);
      clearTimeout(close);
    };
    // The timer follows the toast id only (onClose is a new function on every parent render).
  }, [state?.id]);

  if (!state) return null;
  const data = Object.fromEntries(Object.entries(state.data ?? {}).map(([k, v]) => [`data-${k}`, String(v)]));
  return (
    <div
      role="status"
      data-testid={testId}
      data-has-undo={state.undo ? "true" : undefined}
      {...data}
      style={{
        position: "sticky",
        bottom: 12,
        justifySelf: "center",
        display: "flex",
        flexWrap: "wrap",
        gap: 12,
        alignItems: "center",
        maxWidth: "min(560px, 100%)",
        background: state.tone === "error" ? K.danger : K.ink,
        color: K.card,
        padding: "8px 14px",
        borderRadius: 18,
        fontSize: 13,
        zIndex: 5,
      }}
    >
      <span style={{ overflowWrap: "anywhere" }}>{state.message}</span>
      {state.undo && (
        <>
          <button
            type="button"
            data-testid={`${testId}-undo`}
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              await state.undo?.();
            }}
            style={{ font: "inherit", background: "transparent", border: 0, color: K.accentSoft, cursor: busy ? "wait" : "pointer", fontWeight: 600, padding: 0 }}
          >
            {t(lang, "pipeline.undo")}
          </button>
          <span aria-hidden="true" style={{ opacity: 0.7, fontVariantNumeric: "tabular-nums" }}>
            {fmt(t(lang, "undo.countdown"), { n: left })}
          </span>
        </>
      )}
    </div>
  );
}
