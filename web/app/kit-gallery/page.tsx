// web/app/kit-gallery/page.tsx
// AA-1008 (S1d, root ADR 0016 decision 9) — the gated kit gallery route of the visual job: every kit core component in
// its states (app/_kit/core/KitGallery.tsx, the ShellFrame demo included), so the per-PR visual run (`next build` and
// the standalone server in the runner, .github/workflows/visual.yml) captures the kit as this app renders it. It exists
// only when the server runs with KIT_GALLERY=1; every deployed build answers 404 (notFound) here. The path is public on
// the partner host (lib/hosts.ts PUBLIC_PARTNER_PATHS): the run has no session, and a deployed build shows only the 404.
// `?plant=overflow` adds an element wider than the viewport to the document, `?plant=frame` one inside the page frame:
// the visual job's self-test opens both and must fail on each, which proves both overflow assertions work.
import { notFound } from "next/navigation";
import { GalleryView } from "./GalleryView";

export const dynamic = "force-dynamic";

export default async function KitGalleryPage({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  if (process.env.KIT_GALLERY !== "1") notFound();
  const { plant } = await searchParams;
  return <GalleryView plant={plant === "overflow" || plant === "frame" ? plant : null} />;
}
