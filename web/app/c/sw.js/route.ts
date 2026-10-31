// web/app/c/sw.js/route.ts — the traveller portal's service worker (AA-954, card CRM-24b; lib/portalServiceWorker.ts).
// Served from /c/ so it may control the scope /c/t; client host only (lib/hosts.ts: /c/* is 404 on the partner host).
// `no-cache`: the browser checks for a new worker on every registration instead of keeping an old one for a day.
import { SERVICE_WORKER_SOURCE } from "@/lib/portalServiceWorker";

export const dynamic = "force-static";

export function GET() {
  return new Response(SERVICE_WORKER_SOURCE, {
    headers: {
      "Content-Type": "text/javascript; charset=utf-8",
      "Cache-Control": "no-cache",
    },
  });
}
