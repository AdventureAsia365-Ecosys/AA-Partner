// web/lib/academyVerify.ts
// AA-1105 (root ADR 0022 decision 11) — the public certificate verify page's server side: the call to the CIS public
// route GET /learn/public/v1/certificates/{code} and the web's own per-visitor limit in front of it.
//
// Why the web limits its own visitors: the page calls CIS from this server, so behind the web's NAT every visitor
// reaches CIS from ONE address, and CIS's per-address limit (setting cis.learn.verify_requests_per_minute) becomes a
// cap for the whole host. One visitor refreshing in a loop would then lock everybody out. So the web counts each
// visitor (CloudFront-Viewer-Address, IPv6 per /64, lib/intakeForms.ts viewerKey) before it calls CIS:
//
//   - the per-minute limit is the CIS setting itself, read from the X-RateLimit-Limit header CIS sends with its verify
//     answers (never a literal or an env var here; the tech lead changes it on the CIS settings page);
//   - until CIS has sent that header (an older CIS release), a visitor may have one verify call in flight at a time
//     and no more (a structural rule, not a number), and CIS's own limit still caps the host;
//   - a CIS 429 is passed on with its Retry-After.
//
// State is per process (one web task on Dev; each task counts its own visitors, like the CRM API's in-process limits).

import { viewerKey } from "./intakeForms";
import type { CertificateVerify } from "./academy";
import { CERT_CODE, LEARN_PUBLIC_VERIFY_PREFIX } from "./academyBff";

const VERIFY_TIMEOUT_MS = 10_000; // technical constant: one public CIS call
const MAX_TRACKED_VISITORS = 10_000; // technical constant: memory bound of the window map (cleared past it)
const LIMIT_HEADER = "x-ratelimit-limit";
const CODE_RE = new RegExp(`^${CERT_CODE}$`);

export function isCertificateCode(code: string): boolean {
  return CODE_RE.test(code);
}

interface Window {
  minute: number;
  count: number;
}

export class VisitorLimiter {
  /** The per-visitor, per-minute limit last reported by CIS; null until CIS sent one. */
  limit: number | null = null;
  private windows = new Map<string, Window>();
  private inflight = new Map<string, number>();

  /** Take the limit CIS reported (a positive integer header value); anything else leaves the known limit. */
  learn(headerValue: string | null | undefined): void {
    const raw = (headerValue ?? "").trim();
    if (!/^\d{1,6}$/.test(raw)) return;
    const n = Number(raw);
    if (n > 0) this.limit = n;
  }

  /** This minute's count of `visitor`. */
  private count(visitor: string, minute: number): number {
    const w = this.windows.get(visitor);
    return w && w.minute === minute ? w.count : 0;
  }

  /** Seconds to wait when `visitor` may not call now, or null. */
  private refusal(visitor: string, nowMs: number, minute: number): number | null {
    if (this.limit === null) return (this.inflight.get(visitor) ?? 0) > 0 ? 1 : null;
    return this.count(visitor, minute) >= this.limit ? 60 - (Math.floor(nowMs / 1000) % 60) : null;
  }

  private release(visitor: string): void {
    const n = (this.inflight.get(visitor) ?? 1) - 1;
    if (n <= 0) this.inflight.delete(visitor);
    else this.inflight.set(visitor, n);
  }

  /** Admit one verify call of `visitor` now, or say when to retry. `release()` must be called when the call ends. */
  admit(visitor: string, nowMs: number = Date.now()): { ok: true; release: () => void } | { ok: false; retryAfterS: number } {
    const minute = Math.floor(nowMs / 60_000);
    const wait = this.refusal(visitor, nowMs, minute);
    if (wait !== null) return { ok: false, retryAfterS: wait };
    if (this.windows.size > MAX_TRACKED_VISITORS) this.windows.clear();
    this.windows.set(visitor, { minute, count: this.count(visitor, minute) + 1 });
    this.inflight.set(visitor, (this.inflight.get(visitor) ?? 0) + 1);
    let released = false;
    return {
      ok: true,
      release: () => {
        if (!released) this.release(visitor);
        released = true;
      },
    };
  }
}

/** The process's limiter (tests make their own). */
export const verifyLimiter = new VisitorLimiter();

export type VerifyOutcome =
  | { kind: "ok"; certificate: CertificateVerify }
  | { kind: "not_found" }
  | { kind: "rate_limited"; retryAfterS: number }
  | { kind: "unavailable" };

function retryAfter(raw: string | null): number {
  const n = Number((raw ?? "").trim());
  return Number.isFinite(n) && n > 0 && n <= 3600 ? Math.ceil(n) : 60;
}

async function outcomeOf(res: Response): Promise<VerifyOutcome> {
  if (res.status === 200) {
    const json = (await res.json().catch(() => null)) as CertificateVerify | null;
    return typeof json?.path_title === "string" ? { kind: "ok", certificate: json } : { kind: "unavailable" };
  }
  if (res.status === 404) return { kind: "not_found" };
  if (res.status === 429) return { kind: "rate_limited", retryAfterS: retryAfter(res.headers.get("retry-after")) };
  return { kind: "unavailable" };
}

/**
 * Verify one code for one visitor: the web's own limit first, then the CIS public route (no token). An invalid code
 * never reaches CIS and is not counted.
 */
export async function verifyCertificate(
  cisApiUrl: string,
  code: string,
  viewerAddress: string | null,
  opts: { limiter?: VisitorLimiter; fetchImpl?: typeof fetch; nowMs?: number } = {},
): Promise<VerifyOutcome> {
  if (!isCertificateCode(code)) return { kind: "not_found" };
  if (!cisApiUrl) return { kind: "unavailable" };
  const limiter = opts.limiter ?? verifyLimiter;
  const admitted = limiter.admit(viewerKey(viewerAddress), opts.nowMs);
  if (!admitted.ok) return { kind: "rate_limited", retryAfterS: admitted.retryAfterS };
  try {
    const res = await (opts.fetchImpl ?? fetch)(`${cisApiUrl}${LEARN_PUBLIC_VERIFY_PREFIX}/${encodeURIComponent(code)}`, {
      method: "GET",
      headers: { Accept: "application/json" },
      cache: "no-store",
      redirect: "manual",
      signal: AbortSignal.timeout(VERIFY_TIMEOUT_MS),
    });
    limiter.learn(res.headers.get(LIMIT_HEADER));
    return await outcomeOf(res);
  } catch {
    return { kind: "unavailable" };
  } finally {
    admitted.release();
  }
}
