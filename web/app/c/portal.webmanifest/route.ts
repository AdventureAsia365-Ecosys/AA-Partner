// web/app/c/portal.webmanifest/route.ts — the installable traveller page's web app manifest (AA-954, card CRM-24b;
// lib/portalManifest.ts). Client host only (lib/hosts.ts: /c/* is 404 on the partner host).
import { portalManifest } from "@/lib/portalManifest";

export const dynamic = "force-dynamic";

export function GET(request: Request) {
  const lang = new URL(request.url).searchParams.get("lang") === "vi" ? "vi" : "en";
  return new Response(JSON.stringify(portalManifest(lang)), {
    headers: { "Content-Type": "application/manifest+json; charset=utf-8", "Cache-Control": "public, max-age=3600" },
  });
}
