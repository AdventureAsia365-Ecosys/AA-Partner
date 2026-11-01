"use client";
// web/components/SubmitOnceForm.tsx
// S263 (AA-939 join fix) — a plain POST form whose button sends it once: the first submit goes through, the button
// turns disabled with a "working" label, and any further submit (a double click, Enter twice) is dropped. Without
// JavaScript it is an ordinary form; the server side keeps a second POST harmless anyway (app/join/claim/route.ts).
// When the browser restores the page from its back-forward cache, the form is ready again.

import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { Button } from "@/app/_kit";

export function SubmitOnceForm({
  action,
  label,
  pendingLabel,
  testId,
  children,
}: {
  action: string;
  label: string;
  pendingLabel: string;
  testId?: string;
  children?: ReactNode;
}) {
  const sent = useRef(false);
  const [pending, setPending] = useState(false);

  useEffect(() => {
    const restored = (e: PageTransitionEvent) => {
      if (!e.persisted) return;
      sent.current = false;
      setPending(false);
    };
    window.addEventListener("pageshow", restored);
    return () => window.removeEventListener("pageshow", restored);
  }, []);

  function onSubmit(e: FormEvent<HTMLFormElement>) {
    if (sent.current) {
      e.preventDefault();
      return;
    }
    sent.current = true;
    setPending(true);
  }

  return (
    <form method="post" action={action} onSubmit={onSubmit} style={{ display: "grid", gap: 10, justifyItems: "start" }}>
      {children}
      <Button type="submit" variant="primary" disabled={pending} aria-busy={pending} testId={testId}>
        {pending ? pendingLabel : label}
      </Button>
    </form>
  );
}
