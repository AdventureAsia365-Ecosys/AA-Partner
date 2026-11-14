// web/lib/academyApi.ts
// AA-1105 — server-side reads of the CIS learning API (learn.http.v1) for the academy pages: the academy session's
// access token as a bearer token on /learn/v1/<path> of CIS_API_URL. The token never reaches the browser. Paths are
// the BFF allow-list's (lib/academyBff.ts), so a server read and a browser call can never reach different routes.
// Server-only (fetch with a timeout); the page decides what a refusal shows.

import { randomUUID } from "node:crypto";
import type { AcademyErrorKind, Problem } from "./academy";
import { errorKind } from "./academy";
import { academyUpstreamHeaders, LEARN_API_PREFIX, matchAcademyBff } from "./academyBff";

const UPSTREAM_TIMEOUT_MS = 15_000; // technical constant: one learning API call, as the staff BFF

export type LearnResult<T> =
  | { ok: true; status: number; data: T }
  | { ok: false; status: number; kind: AcademyErrorKind | "unauthorized"; problem: Problem | null };

function asProblem(json: unknown): Problem | null {
  if (!json || typeof json !== "object") return null;
  const { error, message, retry_at: retryAt } = json as Record<string, unknown>;
  if (typeof error !== "string") return null;
  return { error, message: typeof message === "string" ? message : "", retry_at: typeof retryAt === "string" ? retryAt : null };
}

async function answer<T>(res: Response): Promise<LearnResult<T>> {
  const json = (await res.json().catch(() => null)) as unknown;
  if (res.ok) return { ok: true, status: res.status, data: json as T };
  const problem = asProblem(json);
  if (res.status === 401) return { ok: false, status: 401, kind: "unauthorized", problem };
  return { ok: false, status: res.status, kind: errorKind(res.status, problem?.error), problem };
}

/** GET (or POST) one allow-listed learning route with the learner's token. */
export async function learnCall<T>(
  cisApiUrl: string,
  accessToken: string,
  method: "GET" | "POST",
  subPath: string,
  opts: { query?: URLSearchParams; body?: unknown; fetchImpl?: typeof fetch } = {},
): Promise<LearnResult<T>> {
  if (!cisApiUrl || !matchAcademyBff(method, subPath)) return { ok: false, status: 404, kind: "not_found", problem: null };
  const qs = opts.query?.toString() ? `?${opts.query.toString()}` : "";
  const body = opts.body === undefined ? undefined : JSON.stringify(opts.body);
  try {
    const res = await (opts.fetchImpl ?? fetch)(`${cisApiUrl}${LEARN_API_PREFIX}/${subPath}${qs}`, {
      method,
      headers: academyUpstreamHeaders(accessToken, randomUUID(), body !== undefined),
      body,
      cache: "no-store",
      redirect: "manual",
      signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
    });
    return await answer<T>(res);
  } catch {
    return { ok: false, status: 0, kind: "unavailable", problem: null };
  }
}
