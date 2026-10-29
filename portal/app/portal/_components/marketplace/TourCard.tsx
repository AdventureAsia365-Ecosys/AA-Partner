"use client";
// portal/app/portal/_components/marketplace/TourCard.tsx
// AA-671 — the one tour card of the Marketplace grid (both views): cover photo (catalog view), chips (country, duration,
// intensity, New), name, subtitle, activity or theme tags, price and the tour's status. The card body is one keyboard-reachable button-like region; the optional
// select checkbox sits outside it (no control inside a control).

import { useState } from "react";
import { Clock, Gauge, ImageOff, MapPin } from "lucide-react";
import { Badge, K, SkeletonBar, SkeletonStyle, alpha, sans, serif } from "@/app/_kit";
import { coverSrcSet } from "@/lib/tours";
import { WorkStatusBadge, type WorkStatus } from "../workStatus";

/** The tour's place in the tenant's work: null = never rewritten. */
export type CardStatus = WorkStatus | null;

export const GRID_STYLE: React.CSSProperties = {
  display: "grid",
  // minmax(0, …) so a long word never widens a column past the page (lesson S254, grid + min-content).
  gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 260px), 1fr))",
  gap: 14,
};

/** "In My Tours" for a finished rewrite; the shared work-status badge while one runs or after it failed. */
export function CardStatusBadge({ status }: { status: CardStatus }) {
  if (!status) return null;
  if (status === "done") return <Badge tone="success">In My Tours</Badge>;
  return <WorkStatusBadge status={status} />;
}

/** A small label pill. The icon never shrinks: on a narrow screen a long text (a multi-tier price) used to squeeze the
 *  icon down to a dot, which read as a stray "·" before the text (S256). The text truncates with an ellipsis, or wraps
 *  over several lines with `wrap` (the drawer's price, which must stay readable on a phone). */
export function Chip({ icon, children, tone = "neutral", wrap = false, testId }: {
  icon?: React.ReactNode; children: React.ReactNode; tone?: "neutral" | "accent"; wrap?: boolean; testId?: string;
}) {
  return (
    <span data-testid={testId} style={{
      display: "inline-flex", alignItems: wrap ? "flex-start" : "center", gap: 4, maxWidth: "100%", minWidth: 0,
      fontSize: 11.5, lineHeight: "18px", padding: "1px 8px", borderRadius: wrap ? 10 : 999, fontFamily: sans,
      background: tone === "accent" ? K.accentTint : K.line2,
      color: tone === "accent" ? K.accentDeep : K.ink3,
    }}>
      {icon && <span aria-hidden style={{ display: "inline-flex", flexShrink: 0, height: 18, alignItems: "center" }}>{icon}</span>}
      <span style={wrap
        ? { minWidth: 0, overflowWrap: "anywhere" }
        : { minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
        {children}
      </span>
    </span>
  );
}

/** The cover photo, 16:9, or a quiet placeholder when the tour has no photo yet or the image fails to load (a card
 *  never shows a broken image). Both CIS renditions go in `srcset` (600 / 1600 px, `coverSrcSet`) with the slot width in
 *  `sizes`, so the browser picks the file for the slot and the screen density. `src` null = placeholder. */
export function CoverImage({ src, large = null, sizes, alt, eager = false, radius = "12px 12px 0 0", testId = "marketplace-card-cover" }: {
  src: string | null; large?: string | null; sizes: string; alt: string; eager?: boolean; radius?: string; testId?: string;
}) {
  const [failed, setFailed] = useState<string | null>(null);
  const url = src ?? large;
  const show = !!url && failed !== url;
  return (
    <div data-testid={testId} data-cover={show ? "photo" : "none"} style={{
      position: "relative", width: "100%", aspectRatio: "16 / 9", overflow: "hidden", borderRadius: radius,
      background: K.line2, display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0,
    }}>
      {show ? (
        // A plain <img>: the photo comes from the CIS public photo route (a 302 to the photos CDN, already resized to
        // WebP renditions), not from this app, so next/image would only add a second resize hop.
        // eslint-disable-next-line @next/next/no-img-element
        <img src={url} srcSet={coverSrcSet(src, large)} sizes={sizes} alt={alt}
          loading={eager ? "eager" : "lazy"} decoding="async" onError={() => setFailed(url)}
          style={{ position: "absolute", inset: 0, width: "100%", height: "100%", objectFit: "cover", display: "block" }} />
      ) : (
        <ImageOff size={22} strokeWidth={1.5} aria-hidden style={{ color: K.muted2 }} />
      )}
    </div>
  );
}

/** `sizes` of a grid card cover: the full content width on a phone (one column), else about one grid column. */
export const CARD_COVER_SIZES = "(max-width: 640px) calc(100vw - 32px), 340px";

const clamp = (lines: number): React.CSSProperties => ({
  display: "-webkit-box", WebkitLineClamp: lines, WebkitBoxOrient: "vertical", overflow: "hidden",
});

export function TourCard({
  name, subtitle, country, duration, intensity, isNew = false, tags = [], cover, coverLarge = null, price, priceSub, status, extraBadge,
  selected = false, onSelectToggle, active = false, onOpen, testId = "marketplace-card", footer,
}: {
  name: string;
  /** Intensity label ("Challenging"); no chip when absent. */
  intensity?: string | null;
  /** Cover photo URL (the small rendition); null = placeholder; undefined = the card has no cover area. */
  cover?: string | null;
  /** The large rendition of the same photo, for `srcset` (high-density phones). */
  coverLarge?: string | null;
  subtitle?: string | null;
  country?: string | null;
  duration?: string;
  isNew?: boolean;
  tags?: string[];
  price?: string | null;
  priceSub?: string;
  status: CardStatus;
  extraBadge?: React.ReactNode;
  /** With `onSelectToggle`, the card shows a select checkbox (bulk rewrite). */
  selected?: boolean;
  onSelectToggle?: () => void;
  active?: boolean;
  onOpen: () => void;
  testId?: string;
  /** Replaces the default footer (price + status). */
  footer?: React.ReactNode;
}) {
  const border = selected ? K.accent : active ? alpha(K.accent, 45) : K.line;
  return (
    <div data-testid={testId} style={{
      position: "relative", display: "flex", flexDirection: "column", minWidth: 0,
      background: K.card, border: `1px solid ${border}`, borderRadius: 12,
      boxShadow: active ? `0 0 0 3px ${alpha(K.accent, 15)}` : "none", transition: "border-color .15s, box-shadow .15s",
      fontFamily: sans,
    }}>
      {onSelectToggle && (
        <label title={selected ? "Remove from selection" : "Select for a bulk rewrite"}
          style={{ position: "absolute", top: 10, right: 10, display: "inline-flex", padding: 4, cursor: "pointer", zIndex: 1,
            background: K.card, borderRadius: 6, boxShadow: `0 1px 3px ${alpha(K.ink, 18)}` }}>
          <input type="checkbox" checked={selected} onChange={onSelectToggle} aria-label={`Select ${name}`}
            style={{ width: 16, height: 16, margin: 0, accentColor: K.accent, cursor: "pointer" }} />
        </label>
      )}
      <div role="button" tabIndex={0} aria-label={`Open ${name}`}
        onClick={onOpen}
        onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onOpen(); } }}
        style={{ flex: 1, display: "flex", flexDirection: "column", cursor: "pointer", minWidth: 0, borderRadius: 12, outlineOffset: 2 }}>
        {cover !== undefined && <CoverImage src={cover} large={coverLarge} sizes={CARD_COVER_SIZES} alt={name} />}
        <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 8, padding: "14px 16px", minWidth: 0 }}>
          {/* The select box sits over the cover when there is one, so only a coverless card keeps room for it here. */}
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", paddingRight: onSelectToggle && cover === undefined ? 26 : 0, minWidth: 0 }}>
            {country && <Chip icon={<MapPin size={11} aria-hidden />}>{country}</Chip>}
            {duration && <Chip icon={<Clock size={11} aria-hidden />}>{duration}</Chip>}
            {intensity && <Chip icon={<Gauge size={11} aria-hidden />}>{intensity}</Chip>}
            {isNew && <Chip tone="accent">New</Chip>}
          </div>
          <div style={{ ...clamp(2), fontFamily: serif, fontSize: 15.5, fontWeight: 600, color: K.ink, lineHeight: 1.35, overflowWrap: "break-word" }}>
            {name}
          </div>
          {subtitle && (
            <div style={{ ...clamp(2), fontSize: 12.5, color: K.muted, lineHeight: 1.45, overflowWrap: "break-word" }}>{subtitle}</div>
          )}
          {tags.length > 0 && (
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap", minWidth: 0 }}>
              {tags.map((t) => <Chip key={t}>{t}</Chip>)}
            </div>
          )}
          <div style={{ flex: 1 }} />
          {footer ?? (
            <div style={{ display: "flex", alignItems: "flex-end", justifyContent: "space-between", gap: 10, flexWrap: "wrap", paddingTop: 8, borderTop: `1px solid ${K.line2}` }}>
              <div style={{ minWidth: 0 }}>
                <div style={{ fontSize: 10.5, fontWeight: 600, letterSpacing: "0.12em", textTransform: "uppercase", color: K.muted }}>Price</div>
                <div style={{ fontSize: 14, fontWeight: 600, color: K.ink, fontVariantNumeric: "tabular-nums", overflowWrap: "anywhere" }}>{price || "On request"}</div>
                {priceSub && <div style={{ fontSize: 11, color: K.muted }}>{priceSub}</div>}
              </div>
              <div style={{ display: "flex", gap: 6, flexWrap: "wrap", justifyContent: "flex-end" }}>
                {extraBadge}
                <CardStatusBadge status={status} />
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

/** Placeholder cards while the grid loads (aria-busy, so smoke and screen readers wait for it). */
export function CardGridSkeleton({ count = 8, label, withCover = false }: { count?: number; label: string; withCover?: boolean }) {
  return (
    <div aria-busy="true" aria-label={label} style={GRID_STYLE}>
      <SkeletonStyle />
      {Array.from({ length: count }).map((_, i) => (
        <div key={i} style={{ background: K.card, border: `1px solid ${K.line}`, borderRadius: 12, padding: "14px 16px", display: "flex", flexDirection: "column", gap: 10, minHeight: 170 }}>
          {withCover && <SkeletonBar w="100%" h={120} r={8} />}
          <div style={{ display: "flex", gap: 6 }}>
            <SkeletonBar w={70} h={16} r={999} /><SkeletonBar w={56} h={16} r={999} />
          </div>
          <SkeletonBar w="85%" h={16} />
          <SkeletonBar w="60%" h={12} />
          <div style={{ flex: 1 }} />
          <SkeletonBar w="40%" h={14} />
        </div>
      ))}
    </div>
  );
}

