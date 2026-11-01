"use client";
// web/components/FilterTabs.tsx — a list page's status filter on the kit underline tabs (AA-957 S262 round 3; first on
// /commissions, now also /payments). Each tab moves to its filter's own URL, so a reload or a shared link keeps it; the
// active tab is marked by the kit (accent underline, aria-selected). The kit's tab strip scrolls sideways on a phone and
// the live run found the active filter off-screen at 390 px ("All", "Cancelled"): after each render the strip is
// scrolled so the active tab is in view (only the strip's own scrollLeft: the page never jumps).
import { useRouter } from "next/navigation";
import { useEffect, useRef } from "react";
import { Tabs } from "@/app/_kit";

export type FilterTab = { key: string; label: string; href: string };

/** The strip's scrollLeft that shows the active tab whole with `margin` px of room, from the two boxes as they are on
 * screen now (unchanged when the tab already shows). */
export function scrollToShow(scrollLeft: number, strip: { left: number; right: number }, tab: { left: number; right: number }, margin = 16): number {
  if (tab.left < strip.left + margin) return Math.max(0, scrollLeft - (strip.left + margin - tab.left));
  if (tab.right > strip.right - margin) return scrollLeft + (tab.right - (strip.right - margin));
  return scrollLeft;
}

export function FilterTabs({ tabs, active, ariaLabel, testId }: { tabs: FilterTab[]; active: string; ariaLabel: string; testId: string }) {
  const router = useRouter();
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const strip = box.current?.querySelector<HTMLElement>('[role="tablist"]');
    const tab = strip?.querySelector<HTMLElement>('[role="tab"][aria-selected="true"]');
    if (!strip || !tab) return;
    strip.scrollLeft = scrollToShow(strip.scrollLeft, strip.getBoundingClientRect(), tab.getBoundingClientRect());
  }, [active]);
  return (
    <div ref={box} data-testid={testId} data-active={active} style={{ minWidth: 0 }}>
      <Tabs
        variant="underline"
        ariaLabel={ariaLabel}
        active={active}
        tabs={tabs.map((t) => ({ key: t.key, label: t.label }))}
        onChange={(key) => {
          const tab = tabs.find((t) => t.key === key);
          if (tab) router.push(tab.href);
        }}
      />
    </div>
  );
}
