"use client";
// web/components/DateInput.tsx
// AA-935 — a date field that reads what the person types in their locale's order and shows, next to the field, the
// date it understood with the month spelled out (R32): "03/04" reads "3 Apr 2026" for en-AU and "Mar 4, 2026" for
// en-US. The form value is the ISO date (hidden input `name`), never the typed text.
//
// Typed before hydration (S249 sign-in check): the page is server-rendered, so a person (or a test) can type while the
// scripts still load. React then keeps that text in the DOM but fires no onChange for it, which left the state empty:
// no reading, no ISO value, until the next keystroke. On mount the field therefore adopts whatever the DOM already
// holds, and marks itself `data-hydrated="true"` so browser checks can wait for it.

import { useEffect, useId, useRef, useState } from "react";
import { K } from "@/app/_kit";
import { formatDate, inputPattern, parseDateInput, type DateLocale } from "@/lib/dates";

export function DateInput({
  locale,
  name,
  label,
  help,
  readAs,
  invalid,
  defaultValue = "",
}: {
  locale: DateLocale;
  name: string;
  label: string;
  help?: string;
  readAs: string;
  invalid: string;
  defaultValue?: string;
}) {
  const id = useId();
  const [text, setText] = useState(defaultValue);
  const [hydrated, setHydrated] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    const typed = inputRef.current?.value ?? "";
    if (typed !== defaultValue) setText(typed);
    setHydrated(true);
    // Mount only: later changes arrive through onChange.
  }, []);
  const iso = parseDateInput(text, locale);
  const status = !text.trim() ? "" : iso ? `${readAs}: ${formatDate(iso, locale)}` : invalid;
  return (
    <div style={{ display: "grid", gap: 6, maxWidth: 360 }}>
      <label htmlFor={id} style={{ fontSize: 13, fontWeight: 600, color: K.ink }}>
        {label}
      </label>
      {help && <span style={{ fontSize: 12, color: K.muted }}>{help}</span>}
      <input
        ref={inputRef}
        id={id}
        inputMode="numeric"
        autoComplete="off"
        placeholder={inputPattern(locale)}
        value={text}
        onChange={(e) => setText(e.target.value)}
        aria-describedby={`${id}-read`}
        aria-invalid={Boolean(text.trim()) && !iso}
        data-testid="date-input"
        data-hydrated={hydrated ? "true" : undefined}
        style={{
          padding: "8px 10px",
          borderRadius: 8,
          border: `1px solid ${text.trim() && !iso ? K.danger : K.line}`,
          background: K.card,
          color: K.ink,
          fontSize: 14,
        }}
      />
      <input type="hidden" name={name} value={iso ?? ""} />
      <span id={`${id}-read`} aria-live="polite" data-testid="date-read-as" style={{ fontSize: 12.5, color: iso ? K.success : K.danger, minHeight: 18 }}>
        {status}
      </span>
    </div>
  );
}
