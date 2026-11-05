"use client";
// web/app/kit-gallery/GalleryView.tsx — the client part of the gated kit gallery route (page.tsx): the kit PageFrame and
// the kit gallery. `plant` is the visual job's self-test: a strip 2400px wide in the document ("overflow") or inside
// the frame ("frame").
import { PageFrame } from "@/app/_kit";
import { KitGallery } from "@/app/_kit/core/KitGallery";

const PLANTED = { width: 2400, height: 8 } as const;

export function GalleryView({ plant }: { plant: "overflow" | "frame" | null }) {
  return (
    <>
      <PageFrame title="UI kit gallery" description="Every kit core component in its states (static demo data).">
        <KitGallery />
        {plant === "frame" && <div data-testid="planted-overflow" style={PLANTED} aria-hidden="true" />}
      </PageFrame>
      {plant === "overflow" && <div data-testid="planted-overflow" style={PLANTED} aria-hidden="true" />}
    </>
  );
}
