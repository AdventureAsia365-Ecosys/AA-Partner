// web/app/(studio)/portal/_components/ui.tsx
// Design system — Adventure Asia brand tokens shared with the admin UI (app/_kit/core/brand.ts,
// AA-605): Fahkwang display + Poppins UI + JetBrains Mono, gold accent, pill buttons.

import Link from "next/link";
import { Loader2 } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import {
  BTN_PRIMARY_TEXT, BTN_RADIUS, FONT_DISPLAY, K, FONT_MONO, FONT_SANS, SkeletonBar, SkeletonStyle,
} from "@/app/_kit";
// AA-1036: the pure helpers (JSON fields, formats, the tour-version status) moved to lib/studio/format.ts.
import type { BadgeVariant } from "@/lib/studio/format";

export type { BadgeVariant };

// ─── Tokens ───────────────────────────────────────────────────────────────────
// Key names unchanged (hundreds of call sites). AA-759: the values are the `--aa-*` CSS variables
// (app/globals.css, light = the BRAND hex these keys held before, dark = the admin dark palette), the
// same move AA-601 part B made for the admin `A`. One <html data-theme> switch recolours every
// portal page. A `var()` cannot take a hex alpha suffix — use the kit `alpha(color, pct)`.
// red = danger only (AA-605: #B14A3B -> brand #C20101, same value as admin's A.red).
// AA-1036 (an AA-1007 step): the values the kit token object K already names come from K (same `var()` strings).
export const T = {
  gold: K.accent, goldDeep: K.accentDeep, goldSoft: K.accentSoft, goldTint: K.accentTint,
  ink: K.ink, ink2: K.ink2, ink3: K.ink3,
  body: K.body, muted: K.muted, muted2: K.muted2,
  bg: K.bg, card: K.card, line: K.line, line2: K.line2,
  green: K.success, greenSoft: K.successSoft,
  red: K.danger, redSoft: K.dangerSoft, redBorder: K.dangerBorder,
  amber: K.amber, amberSoft: K.amberSoft,
  /** White text/icon on a solid coloured surface, in both themes. */
  onSolid: "var(--aa-on-solid)",
  /** Text on the gold primary button (dark ink in both themes since AA-760: white on gold was 2.5:1). */
  onAccent: "var(--aa-on-accent)",
  /** A surface that stays dark in both themes (the sidebar navy) — for the `dark` Card. */
  darkPanel: "var(--aa-side-bg)", darkPanel2: "var(--aa-side-panel)",
} as const;

// `serif` keeps its name; it is now the brand display face (titles only — numbers use `sans`).
export const serif = FONT_DISPLAY;
export const mono  = FONT_MONO;
export const sans  = FONT_SANS;

const BADGE_MAP: Record<BadgeVariant, { bg: string; color: string; dot: string }> = {
  success: { bg: T.greenSoft, color: T.green, dot: T.green },
  warning: { bg: T.amberSoft, color: T.amber, dot: T.gold },
  error:   { bg: T.redSoft, color: T.red, dot: T.red },
  info:    { bg: K.infoSoft, color: K.info, dot: "var(--aa-blue)" },
  gold:    { bg: T.goldTint, color: T.goldDeep, dot: T.gold },
  // AA-760 — ink3, not muted: muted on line2 was 4.0:1 in light (8.3:1 now; 7.7:1 in dark).
  default: { bg: T.line2, color: T.ink3, dot: T.muted2 },
};

// ─── UI Components ────────────────────────────────────────────────────────────

export function Card({ children, style = {}, dark = false }: {
  children: React.ReactNode; style?: React.CSSProperties; dark?: boolean;
}) {
  return (
    <div style={{
      background: dark ? `linear-gradient(160deg,${T.darkPanel} 0%,${T.darkPanel2} 100%)` : T.card,
      border: `1px solid ${dark ? T.darkPanel2 : T.line}`,
      borderRadius: 12, padding: "22px 22px 20px", position: "relative",
      ...style,
    }}>{children}</div>
  );
}

// AA-524 — shared sticky-header wrapper. `<main>` in layout.tsx is the scroll container (the
// outer breadcrumb bar above it is already its own sticky element, zIndex 10); this sticks AT
// the top of that same scrollport, one zIndex band below so the two never compete.
//
// `top: 0` on ANY descendant of `<main>` resolves to `<main>`'s own padding edge — 28px down
// (layout.tsx's `<main>` has `padding: "28px 36px 56px"`), REGARDLESS of how deep the sticky
// element is nested. That 28px band (viewport y 56-84) is inside `<main>`'s padding box, so
// `overflow-y: auto` does NOT clip it — anything that scrolls up INTO that band while this bar
// is already stuck renders ABOVE the bar, uncovered (confirmed live: a form input scrolled
// there, in front of a page title). `bleedTop` (default 28, matching that padding) extends this
// bar's own opaque background up to cover it — every usage needs this, not just page-level ones,
// since the 28px offset is constant no matter the nesting depth. Only skip it (bleedTop=0) for a
// bar that is deliberately NOT the first sticky point `<main>` will hit while scrolling.
//
// AA-836 (D3) — the bleed band is painted ONLY while the bar is actually stuck. At rest it sat 28px
// above the bar and covered whatever was there (T11: the bottom border of the card above, and the
// card's own top border). Stuck = the bar has moved down relative to a zero-height sentinel that
// stays in normal flow just before it. Margin / padding are the same in both states (the band is a
// background layer that starts `bleedTop` lower at rest), so nothing moves when the bar sticks; at
// rest the transparent band also lets clicks through to what is above.
//
// S241 UI verify (AA-836 D3) — the bleed never actually covered the 28px strip: Chrome resolves
// `top: 0` against `<main>`'s CONTENT edge (scrollport inset by its padding), so the stuck bar's
// border box started at y 84 (1440) / y 76 (390, padding 20) and the whole `bleedTop` band sat
// INSIDE the bar, below the strip — scrolled card text showed between the topbar (y 56) and the
// bar (measured 28px / 20px). Fix: stick at `top: -bleedTop`. The stuck border box now starts
// `padding - bleedTop` from the scrollport edge (0 at 1440; -8 at 390, clipped by `<main>`'s
// overflow), so the band paints from the topbar's bottom edge down, and the bar's content lands
// exactly where `top: 0` without a bleed would have put it. Requires bleedTop >= `<main>`'s
// padding-top (28 desktop / 20 phone) — the default 28 covers both. At rest nothing changes:
// sticky offsets apply only once the bar reaches that line, and the band stays transparent.
export function StickyBar({ children, background = T.bg, style = {}, bleedTop = 28 }: {
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
      setStuck(prev => (prev === next ? prev : next));
    };
    const schedule = () => { if (!frame) frame = requestAnimationFrame(measure); };
    schedule();
    // `scroll` does not bubble; capture catches `<main>` (or any other scroll container).
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

// AA-636 — one page header for every portal page (title + one-line description + optional
// action on the right), so pages stop each inventing their own h1/h2 styling.
export function PageHeader({ title, sub, action }: {
  title: React.ReactNode; sub?: React.ReactNode; action?: React.ReactNode;
}) {
  return (
    <div style={{ display: "flex", alignItems: "flex-end", justifyContent: "space-between", gap: 16, flexWrap: "wrap", marginBottom: 22 }}>
      <div style={{ minWidth: 0 }}>
        <h1 style={{ fontFamily: serif, fontSize: 24, fontWeight: 500, color: T.ink, margin: "0 0 6px", letterSpacing: "-0.01em", textWrap: "balance" as React.CSSProperties["textWrap"] }}>
          {title}
        </h1>
        {sub && <p style={{ fontSize: 13, color: T.muted, margin: 0, lineHeight: 1.5, maxWidth: 640 }}>{sub}</p>}
      </div>
      {action}
    </div>
  );
}

// Small in-card link ("View all →"). Next <Link> so it is a real, keyboard-reachable link.
export function TextLink({ href, children }: { href: string; children: React.ReactNode }) {
  return (
    <Link href={href} style={{ fontSize: 12, color: T.ink3, textDecoration: "none", fontWeight: 500, whiteSpace: "nowrap" }}>
      {children}
    </Link>
  );
}

export function CardHead({ title, action, light = false }: {
  title: string; action?: React.ReactNode; light?: boolean;
}) {
  return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 16 }}>
      <span style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em", color: light ? "rgba(255,255,255,0.5)" : T.muted }}>{title}</span>
      {action && <div style={{ fontSize: 12, color: T.ink3 }}>{action}</div>}
    </div>
  );
}

export function Badge({ children, variant = "default", icon }: {
  children: React.ReactNode; variant?: BadgeVariant;
  /** Leading icon (e.g. a lucide `<Milestone size={11} />`), drawn on the same line as the text. */
  icon?: React.ReactNode;
}) {
  const s = BADGE_MAP[variant];
  return (
    // AA-836 — one line, always a pill: a long label (T11 "Route: <hub> · N Segments") used to wrap
    // inside the 999px radius and render as a tall oval; now it ends in an ellipsis instead.
    // S241 UI verify — an icon passed INSIDE the text span (global CSS makes `svg` display:block)
    // rendered on its own line above the text (pill 33.5px tall instead of 22.5px). Icons now go
    // through `icon`: their own non-shrinking flex item between the dot and the text, so dot, icon
    // and text share one row; only the text span shrinks and ends in an ellipsis.
    <span style={{ display: "inline-flex", alignItems: "center", gap: 5, maxWidth: "100%", whiteSpace: "nowrap", fontSize: 11, fontWeight: 600, padding: "3px 9px", borderRadius: 999, letterSpacing: "0.04em", textTransform: "uppercase", background: s.bg, color: s.color }}>
      <span style={{ width: 5, height: 5, borderRadius: "50%", background: s.dot, display: "block", flexShrink: 0 }} />
      {icon && <span aria-hidden style={{ display: "inline-flex", alignItems: "center", flexShrink: 0, lineHeight: 0 }}>{icon}</span>}
      <span style={{ display: "block", minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }}>{children}</span>
    </span>
  );
}

export function ProgressBar({ pct, warn = false }: { pct: number; warn?: boolean }) {
  return (
    <div style={{ height: 7, background: T.line2, borderRadius: 999, overflow: "hidden" }}>
      <div style={{ height: "100%", width: `${Math.min(100, Math.max(0, pct))}%`, borderRadius: 999, transition: "width 0.5s ease", background: warn ? `linear-gradient(90deg,${T.amber},${T.red})` : `linear-gradient(90deg,${T.gold},${T.goldDeep})` }} />
    </div>
  );
}

export function Spinner({ size = 16 }: { size?: number }) {
  return <Loader2 size={size} style={{ animation: "spin 1s linear infinite", color: T.gold }} />;
}

export function LoadingScreen({ message = "Loading..." }: { message?: string }) {
  return (
    <div role="status" aria-busy="true" style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", minHeight: 400, gap: 12 }}>
      <Spinner size={22} /><span style={{ fontSize: 13, color: T.muted }}>{message}</span>
    </div>
  );
}

/** AA-760 — a card-shaped placeholder while one section's data loads (a connection card, a plan
 *  card), so the page around it renders at once instead of behind a full-page spinner. */
export function CardSkeleton({ lines = 3, label = "Loading" }: { lines?: number; label?: string }) {
  return (
    <Card>
      <div aria-busy="true" aria-label={label} style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <SkeletonStyle />
        <SkeletonBar w={140} h={11} />
        {Array.from({ length: lines }).map((_, i) => <SkeletonBar key={i} w={`${90 - i * 18}%`} h={13} />)}
      </div>
    </Card>
  );
}

export function EmptyState({ icon, title, sub, action }: {
  icon: React.ReactNode; title: string; sub?: string; action?: React.ReactNode;
}) {
  return (
    <div data-testid="portal-empty-state" style={{ textAlign: "center", padding: "60px 20px", display: "flex", flexDirection: "column", alignItems: "center", gap: 12 }}>
      <div style={{ fontSize: 36 }}>{icon}</div>
      <div style={{ fontSize: 15, fontWeight: 600, color: T.ink }}>{title}</div>
      {sub && <div style={{ fontSize: 13, color: T.muted, maxWidth: 320, lineHeight: 1.5 }}>{sub}</div>}
      {action}
    </div>
  );
}

type BtnVariant = "primary" | "secondary" | "ghost" | "danger";
type BtnSize = "sm" | "md" | "lg";

export function Btn({ children, onClick, variant = "secondary", size = "md", disabled = false, style = {} }: {
  children: React.ReactNode; onClick?: () => void; variant?: BtnVariant;
  size?: BtnSize; disabled?: boolean; style?: React.CSSProperties;
}) {
  const pad: Record<BtnSize, string> = { sm: "5px 12px", md: "8px 18px", lg: "11px 24px" };
  const fz:  Record<BtnSize, number> = { sm: 11, md: 13, lg: 14 };
  const base: Record<BtnVariant, React.CSSProperties> = {
    primary:   { background: T.gold,    color: T.onAccent, border: `1px solid ${T.gold}` },
    secondary: { background: T.card,    color: T.ink3, border: `1px solid ${T.line}` },
    ghost:     { background: "transparent", color: T.muted, border: `1px solid ${T.line}` },
    danger:    { background: T.redSoft, color: T.red,  border: `1px solid ${T.redBorder}` },
  };
  return (
    <button onClick={onClick} disabled={disabled} style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", gap: 6, padding: pad[size], borderRadius: BTN_RADIUS, fontSize: fz[size], fontWeight: 600, cursor: disabled ? "not-allowed" : "pointer", opacity: disabled ? 0.5 : 1, transition: "all 0.15s", fontFamily: sans, ...base[variant], ...(variant === "primary" && size !== "sm" ? BTN_PRIMARY_TEXT : {}), ...style }}>{children}</button>
  );
}

/** AA-760 — an empty state's way forward: a page link that looks like the primary `Btn` (md). */
export function CtaLink({ href, children }: { href: string; children: React.ReactNode }) {
  return (
    <Link href={href} data-testid="empty-state-cta" style={{
      display: "inline-flex", alignItems: "center", justifyContent: "center", gap: 6, padding: "8px 18px",
      borderRadius: BTN_RADIUS, fontSize: 13, fontWeight: 600, fontFamily: sans, textDecoration: "none",
      background: T.gold, color: T.onAccent, border: `1px solid ${T.gold}`, ...BTN_PRIMARY_TEXT,
    }}>
      {children}
    </Link>
  );
}

/** Rows a text needs at ~44 characters a line (a phone-width textarea), plus its own line breaks. */
function rowsFor(text: string): number {
  return text.split("\n").reduce((n, line) => n + Math.max(1, Math.ceil(line.length / 44)), 0);
}

export function Field({ label, value, onChange, rows = 3, placeholder = "", hint, hintColor, autoGrow = false }: {
  label: string; value: string; onChange: (v: string) => void;
  rows?: number; placeholder?: string; hint?: string; hintColor?: string;
  /** AA-760 — grow with the text (CSS `field-sizing: content`, and enough `rows` where that is not
   *  supported), so a saved answer is never cut mid-line; `rows` stays the minimum (capped at 16). */
  autoGrow?: boolean;
}) {
  const shownRows = autoGrow ? Math.min(16, Math.max(rows, rowsFor(value))) : rows;
  const id = useId(); // unique per Field, even when two share a label
  return (
    <div style={{ marginBottom: 16 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
        <label htmlFor={id} style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.1em", color: T.muted }}>{label}</label>
        {hint && <span style={{ fontSize: 11, color: hintColor ?? T.muted2 }}>{hint}</span>}
      </div>
      <textarea id={id} value={value} onChange={e => onChange(e.target.value)} rows={shownRows} placeholder={placeholder}
        style={{
          width: "100%", padding: "10px 12px", background: T.bg, border: `1px solid ${T.line}`, borderRadius: 8, color: T.body, fontSize: 13, resize: "vertical", outline: "none", lineHeight: 1.6, fontFamily: sans, boxSizing: "border-box",
          ...(autoGrow ? { fieldSizing: "content", minHeight: `calc(${rows} * 1.6em + 22px)`, maxHeight: "60vh" } as React.CSSProperties : {}),
        }} />
    </div>
  );
}
