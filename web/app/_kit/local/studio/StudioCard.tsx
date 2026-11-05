// web/app/_kit/local/studio/StudioCard.tsx
// AA-1008 (S6f, root ADR 0016 decision 7) — the Content studio's own building blocks that have no kit core twin yet:
// the card with its small uppercase head (and the dark membership variant), the quota progress bar, the sticky card
// header with its bleed band, the small in-card link and the primary-look call-to-action link. They were the rest of
// the retired legacy layer `app/(studio)/portal/_components/ui.tsx` (aa-kit/no-legacy-kit); what that layer had a kit
// twin for went to the kit (EmptyState, Field + Textarea, the K tokens in place of its `T` alias object).
// Local kit layer (app/_kit/local): linted like page code, inline styles on K tokens only.

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { BTN_PRIMARY_TEXT, BTN_RADIUS, FONT_SANS, K, alpha } from "@/app/_kit";

export function Card({ children, style = {}, dark = false }: {
  children: React.ReactNode; style?: React.CSSProperties; dark?: boolean;
}) {
  return (
    <div style={{
      background: dark ? `linear-gradient(160deg,${K.sideBg} 0%,${K.sidePanel} 100%)` : K.card,
      border: `1px solid ${dark ? K.sidePanel : K.line}`,
      borderRadius: 12, padding: "22px 22px 20px", position: "relative",
      ...style,
    }}>{children}</div>
  );
}

export function CardHead({ title, action, light = false }: {
  title: string; action?: React.ReactNode; light?: boolean;
}) {
  return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 16 }}>
      <span style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em", color: light ? alpha(K.sideTitle, 50) : K.muted }}>{title}</span>
      {action && <div style={{ fontSize: 12, color: K.ink3 }}>{action}</div>}
    </div>
  );
}

export function ProgressBar({ pct, warn = false }: { pct: number; warn?: boolean }) {
  return (
    <div style={{ height: 7, background: K.line2, borderRadius: 999, overflow: "hidden" }}>
      <div style={{ height: "100%", width: `${Math.min(100, Math.max(0, pct))}%`, borderRadius: 999, transition: "width 0.5s ease", background: warn ? `linear-gradient(90deg,${K.amber},${K.danger})` : `linear-gradient(90deg,${K.accent},${K.accentDeep})` }} />
    </div>
  );
}

/** Small in-card link ("View all →"). A Next <Link>, so it is a real, keyboard-reachable link. */
export function TextLink({ href, children }: { href: string; children: React.ReactNode }) {
  return (
    <Link href={href} style={{ fontSize: 12, color: K.ink3, textDecoration: "none", fontWeight: 500, whiteSpace: "nowrap" }}>
      {children}
    </Link>
  );
}

/** AA-760 — an empty state's way forward: a page link in the primary button look (the kit LinkButton is a <button>;
 *  this must stay a real link: the empty-state smoke reads its href). */
export function CtaLink({ href, children }: { href: string; children: React.ReactNode }) {
  return (
    <Link href={href} data-testid="empty-state-cta" style={{
      display: "inline-flex", alignItems: "center", justifyContent: "center", gap: 6, padding: "8px 18px",
      borderRadius: BTN_RADIUS, fontSize: 13, fontWeight: 600, fontFamily: FONT_SANS, textDecoration: "none",
      background: K.accent, color: K.onAccent, border: `1px solid ${K.accent}`, ...BTN_PRIMARY_TEXT,
    }}>
      {children}
    </Link>
  );
}

// AA-524 / AA-836 (D3) / S241 — a sticky card header inside the studio's scrolling main. It sticks at `top: -bleedTop`
// so its opaque band covers the main's padding strip (Chrome resolves `top: 0` against the content edge), and the band
// is painted only while the bar is stuck (a zero-height sentinel in normal flow just before it tells), so at rest it
// never covers the card above. Requires bleedTop >= the main's padding-top (28 desktop / 20 phone).
export function StickyBar({ children, background = K.bg, style = {}, bleedTop = 28 }: {
  children: React.ReactNode; background?: string; style?: React.CSSProperties; bleedTop?: number;
}) {
  const { marginTop, paddingTop, borderRadius, ...rest } = style;
  const mt = (typeof marginTop === "number" ? marginTop : 0) - bleedTop;
  const pt = (typeof paddingTop === "number" ? paddingTop : 0) + bleedTop;
  const sentinelRef = useRef<HTMLDivElement>(null);
  const barRef = useRef<HTMLDivElement>(null);
  const [stuck, setStuck] = useState(false);

  useEffect(() => {
    if (bleedTop <= 0) return;
    let frame = 0;
    const measure = () => {
      frame = 0;
      const s = sentinelRef.current;
      const b = barRef.current;
      if (!s || !b) return;
      // At rest the bar's top is exactly `mt` from the sentinel; stuck pushes it further down.
      const next = b.getBoundingClientRect().top - s.getBoundingClientRect().top > mt + 0.5;
      setStuck((prev) => (prev === next ? prev : next));
    };
    const schedule = () => { if (!frame) frame = requestAnimationFrame(measure); };
    schedule();
    // `scroll` does not bubble; capture catches the studio main (or any other scroll container).
    document.addEventListener("scroll", schedule, { capture: true, passive: true });
    window.addEventListener("resize", schedule);
    return () => {
      document.removeEventListener("scroll", schedule, { capture: true });
      window.removeEventListener("resize", schedule);
      if (frame) cancelAnimationFrame(frame);
    };
  }, [mt, bleedTop]);

  const showBleed = stuck || bleedTop <= 0;
  return (
    <>
      <div ref={sentinelRef} aria-hidden style={{ display: "flow-root", height: 0 }} />
      <div ref={barRef} style={{
        position: "sticky", top: bleedTop > 0 ? -bleedTop : 0, zIndex: 5,
        marginTop: mt, paddingTop: pt,
        pointerEvents: showBleed ? undefined : "none",
        ...rest,
      }}>
        <div aria-hidden style={{
          position: "absolute", left: 0, right: 0, bottom: 0, top: showBleed ? 0 : bleedTop,
          background, borderRadius,
        }} />
        <div style={{ position: "relative", pointerEvents: "auto" }}>{children}</div>
      </div>
    </>
  );
}
