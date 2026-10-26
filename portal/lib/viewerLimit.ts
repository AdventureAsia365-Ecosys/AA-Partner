// portal/lib/viewerLimit.ts
// AA-979 P2b (critic S2) — a per-viewer limit on the API-key sign-in, BEFORE the call to CIS. On Fargate every studio
// call to CIS leaves through one NAT address, so CIS's per-IP login throttle (20 failures / 15 min) would become one
// bucket for every tenant; here each browser is counted by CloudFront-Viewer-Address (the ALB only accepts CloudFront
// requests on this host). No header (Vercel, local) = no limit here; CIS's own throttle still applies there.
// In memory, per task: one portal task on Dev; a second task only doubles the allowance.

export const WINDOW_MS = 5 * 60 * 1000;
export const MAX_ATTEMPTS = 10;
const MAX_KEYS = 5000;

export class ViewerLimiter {
  private hits = new Map<string, { count: number; start: number }>();
  private readonly max: number;
  private readonly windowMs: number;
  private readonly now: () => number;

  // No parameter properties: `npm test` runs this file with node --experimental-strip-types.
  constructor(max: number = MAX_ATTEMPTS, windowMs: number = WINDOW_MS, now: () => number = Date.now) {
    this.max = max;
    this.windowMs = windowMs;
    this.now = now;
  }

  /** True when this viewer may try once more now (and counts the attempt). */
  allow(viewer: string | null): boolean {
    if (!viewer) return true;
    const t = this.now();
    const hit = this.hits.get(viewer);
    if (!hit || t - hit.start >= this.windowMs) {
      if (this.hits.size >= MAX_KEYS) this.prune(t);
      this.hits.set(viewer, { count: 1, start: t });
      return true;
    }
    hit.count += 1;
    return hit.count <= this.max;
  }

  private prune(t: number) {
    for (const [k, v] of this.hits) if (t - v.start >= this.windowMs) this.hits.delete(k);
    if (this.hits.size >= MAX_KEYS) this.hits.clear();
  }
}

/** The viewer key of CloudFront-Viewer-Address ("<ip>:<port>"): the IP only; IPv6 as its /64. */
export function viewerKey(header: string | null): string | null {
  if (!header) return null;
  const value = header.trim();
  const ip = value.includes("]") ? value.slice(value.indexOf("[") + 1, value.indexOf("]")) : value.replace(/:\d+$/, "");
  if (!ip) return null;
  if (ip.includes(":")) return `${ip.split(":").slice(0, 4).join(":")}::/64`;
  return ip;
}
