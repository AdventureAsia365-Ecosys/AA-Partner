"use client";
// portal/app/portal/_components/marketplace/TourDrawer.tsx
// AA-671 — the Marketplace detail drawer (kit Drawer): what the tour is (summary, highlights, itinerary, SEO), and the
// one next step for its state, with the quota note. States follow the shared work-status model (workStatus.tsx):
//   never rewritten → Rewrite this tour
//   ready           → Open in My Tours (primary), Rewrite again
//   queued/writing  → Follow it in My Tours
//   failed          → Retry in My Tours (the retry lives there, with the error)

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Clock, MapPin, Wallet } from "lucide-react";
import { Button, Drawer, K, formatDate, sans } from "@/app/_kit";
import { ROUTES } from "@/app/_kit/local/portalNav";
import { fmtDuration, parseStringList, type QuotaNote } from "@/lib/tours";
import { servedTitle, workClosed } from "../ServedBadge";
import { isActiveWork } from "../workStatus";
import { CardStatusBadge, Chip, type CardStatus } from "./TourCard";
import type { PoolTour } from "./marketplaceApi";

export function myToursHref(tourId: string | null | undefined): string {
  return tourId ? `${ROUTES.myTours}?tour_id=${encodeURIComponent(tourId)}` : ROUTES.myTours;
}

/** A Next link in the kit's secondary Button look (gold outline), for a CTA that must stay a real link (an href the
 *  person can open in a new tab, and the empty-state smoke reads). */
export function LinkButton({ href, children, testId }: { href: string; children: React.ReactNode; testId?: string }) {
  return (
    <Link href={href} data-testid={testId} style={{
      display: "inline-flex", alignItems: "center", justifyContent: "center", gap: 6,
      padding: "8px 16px", borderRadius: 999, fontSize: 13, fontWeight: 600, fontFamily: sans, textDecoration: "none",
      background: "transparent", color: K.accentDeep, border: `1px solid ${K.accentBorder}`, whiteSpace: "nowrap",
    }}>
      {children}
    </Link>
  );
}

export function QuotaLine({ note }: { note: QuotaNote | null }) {
  if (!note) return null;
  const color = note.tone === "danger" ? K.danger : note.tone === "warning" ? K.amber : K.muted;
  return (
    <div data-testid="marketplace-quota-note" data-tone={note.tone}
      style={{ fontSize: 12, color, lineHeight: 1.45, fontFamily: sans, minWidth: 0 }}>
      {note.text}
    </div>
  );
}

export function TourDrawer({ tour, status, quota, busy, onClose, onRewrite }: {
  tour: PoolTour | null;
  status: CardStatus;
  quota: QuotaNote | null;
  busy: boolean;
  onClose: () => void;
  onRewrite: (tour: PoolTour) => void;
}) {
  return (
    <Drawer open={!!tour} onClose={onClose} width={560}
      title={tour?.aa_name ?? ""}
      footer={tour ? <DrawerActions tour={tour} status={status} quota={quota} busy={busy} onRewrite={onRewrite} /> : undefined}>
      {/* key: a new tour opens with its itinerary collapsed again (no state reset in an effect). */}
      {tour && <TourDetail key={tour.id} tour={tour} status={status} />}
    </Drawer>
  );
}

function DrawerActions({ tour, status, quota, busy, onRewrite }: {
  tour: PoolTour; status: CardStatus; quota: QuotaNote | null; busy: boolean; onRewrite: (t: PoolTour) => void;
}) {
  const router = useRouter();
  const href = myToursHref(tour.tour_id);
  const go = () => router.push(href);
  // AA-986 L3: AA paused or retired the tour, so a new rewrite is refused (409): say why instead of offering it.
  const closed = workClosed(tour);
  let actions: React.ReactNode;
  if (closed && (status === null || status === "done")) {
    actions = status === "done" ? <Button variant="primary" onClick={go}>Open in My Tours</Button> : null;
  } else if (status === null) {
    actions = (
      <Button variant="primary" disabled={busy} onClick={() => onRewrite(tour)}>
        {busy ? "Starting rewrite…" : "Rewrite this tour"}
      </Button>
    );
  } else if (status === "done") {
    actions = (
      <>
        <Button variant="secondary" disabled={busy} onClick={() => onRewrite(tour)}>
          {busy ? "Starting…" : "Rewrite again"}
        </Button>
        <Button variant="primary" onClick={go}>Open in My Tours</Button>
      </>
    );
  } else if (isActiveWork(status)) {
    actions = <Button variant="secondary" onClick={go}>Follow it in My Tours</Button>;
  } else {
    actions = <Button variant="primary" onClick={go}>Retry in My Tours</Button>;
  }
  // Quota note only where a rewrite can start from here.
  const showQuota = !closed && (status === null || status === "done");
  return (
    <div data-testid="marketplace-drawer-actions" style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, flexWrap: "wrap", width: "100%" }}>
      <div style={{ flex: "1 1 200px", minWidth: 0 }}>
        {showQuota && <QuotaLine note={quota} />}
        {closed && <div data-testid="marketplace-served-note" style={{ fontSize: 12, color: K.amber, lineHeight: 1.45, fontFamily: sans }}>{closedSentence(tour)}</div>}
      </div>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", justifyContent: "flex-end" }}>{actions}</div>
    </div>
  );
}

/** Why a new rewrite is closed: the served notice's sentence; a supersede waiting for its new version has no notice
 *  (invisible to the tenant, ADR 0015 Q2), so it reads neutral. */
function closedSentence(tour: PoolTour): string {
  return servedTitle(tour)
    ?? (tour.served_reason === "superseded_pending"
      ? "A new version of this tour is on the way: you can rewrite it once it is out."
      : "New rewrites of this tour are closed for now.");
}

const ITINERARY_PREVIEW_PX = 168;

function TourDetail({ tour, status }: { tour: PoolTour; status: CardStatus }) {
  const [fullItinerary, setFullItinerary] = useState(false);
  const highlights = parseStringList(tour.aa_highlights);
  const duration = fmtDuration(tour.duration);
  return (
    <div data-testid="marketplace-drawer" style={{ display: "flex", flexDirection: "column", gap: 20, fontFamily: sans }}>
      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          {tour.country && <Chip icon={<MapPin size={11} aria-hidden />}>{tour.country}</Chip>}
          {duration && <Chip icon={<Clock size={11} aria-hidden />}>{duration}</Chip>}
          {tour.price_raw && <Chip icon={<Wallet size={11} aria-hidden />}>{tour.price_raw}</Chip>}
        </div>
        {tour.aa_subtitle && <div style={{ fontSize: 13.5, color: K.ink3, lineHeight: 1.5 }}>{tour.aa_subtitle}</div>}
        {status && <div><CardStatusBadge status={status} /></div>}
      </div>

      {tour.aa_summary && (
        <Section label="Summary">
          <p style={{ margin: 0, fontSize: 13.5, color: K.body, lineHeight: 1.65 }}>{tour.aa_summary}</p>
        </Section>
      )}

      {highlights.length > 0 && (
        <Section label="Highlights">
          <ul style={{ margin: 0, padding: 0, listStyle: "none", display: "flex", flexDirection: "column", gap: 6 }}>
            {highlights.map((h, i) => (
              <li key={i} style={{ display: "flex", gap: 8, fontSize: 13, color: K.body, lineHeight: 1.5 }}>
                <span aria-hidden style={{ color: K.accent, fontWeight: 700, flexShrink: 0 }}>•</span>{h}
              </li>
            ))}
          </ul>
        </Section>
      )}

      {tour.aa_itineraries && (
        <Section label="Itinerary">
          <div style={{
            fontSize: 13, color: K.body, lineHeight: 1.7, whiteSpace: "pre-wrap", overflowWrap: "break-word",
            maxHeight: fullItinerary ? "none" : ITINERARY_PREVIEW_PX, overflow: "hidden",
            maskImage: fullItinerary ? "none" : "linear-gradient(to bottom, black 70%, transparent)",
            WebkitMaskImage: fullItinerary ? "none" : "linear-gradient(to bottom, black 70%, transparent)",
          }}>
            {tour.aa_itineraries}
          </div>
          <div style={{ marginTop: 8 }}>
            <Button variant="ghost" size="sm" onClick={() => setFullItinerary((v) => !v)}>
              {fullItinerary ? "Show less" : "Show full itinerary"}
            </Button>
          </div>
        </Section>
      )}

      {(tour.seo_title || tour.seo_meta) && (
        <Section label="Search snippet">
          <div style={{ background: K.bg, border: `1px solid ${K.line2}`, borderRadius: 8, padding: "10px 14px", display: "flex", flexDirection: "column", gap: 4 }}>
            {tour.seo_title && <div style={{ fontSize: 13.5, fontWeight: 600, color: K.ink, lineHeight: 1.4 }}>{tour.seo_title}</div>}
            {tour.seo_meta && <div style={{ fontSize: 12.5, color: K.muted, lineHeight: 1.5 }}>{tour.seo_meta}</div>}
          </div>
        </Section>
      )}

      {tour.published_at && (
        <div style={{ fontSize: 12, color: K.muted }}>Published by Adventure Asia on {formatDate(tour.published_at)}</div>
      )}
    </div>
  );
}

function Section({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <section>
      <div style={{ fontSize: 10.5, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.12em", color: K.muted, marginBottom: 8 }}>{label}</div>
      {children}
    </section>
  );
}
