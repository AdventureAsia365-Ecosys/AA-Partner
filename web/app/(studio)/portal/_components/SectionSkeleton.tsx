"use client";
// web/app/(studio)/portal/_components/SectionSkeleton.tsx
// AA-760 — a card-shaped placeholder while one section's data loads (a connection card, a plan card), so the page
// around it renders at once instead of behind a full-page spinner. AA-1036 (AA-1007 leftover): built from kit parts
// (Section, SkeletonBar) instead of the studio's legacy CardSkeleton.
import { Section, SkeletonBar, SkeletonStyle, useT } from "@/app/_kit";
import { COMMON_MESSAGES } from "./common.messages";

export function SectionSkeleton({ lines = 3, label }: { lines?: number; label?: string }) {
  const t = useT(COMMON_MESSAGES);
  return (
    <Section bodyStyle={{ padding: "22px 22px 20px" }}>
      <div aria-busy="true" aria-label={label ?? t("loading")} style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <SkeletonStyle />
        <SkeletonBar w={140} h={11} />
        {Array.from({ length: lines }).map((_, i) => <SkeletonBar key={i} w={`${90 - i * 18}%`} h={13} />)}
      </div>
    </Section>
  );
}
