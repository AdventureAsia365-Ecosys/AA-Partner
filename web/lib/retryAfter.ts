// web/lib/retryAfter.ts
// S270 — the Retry-After the BFF passes on (app/bff/[...path]/route.ts) and the browser reads (lib/bffClient.ts
// bffDownload). Only a 429 or 503 carries it, and only as 1 to 4 digits of seconds: an HTTP date or anything else is
// dropped, so the header is rebuilt from a checked value and never copied as it came. lib/proxyResponse.ts still
// forwards Content-Type only.

const SECONDS = /^\d{1,4}$/;

/** The seconds of a Retry-After value (1 to 4 digits), else null. */
export function retryAfterSeconds(value: string | null | undefined): number | null {
  return value && SECONDS.test(value) ? Number(value) : null;
}

/** The Retry-After the BFF passes on for an upstream answer: its value on a 429 or 503 when it is 1 to 4 digits. */
export function passedRetryAfter(upstream: Pick<Response, "status" | "headers">): string | null {
  if (upstream.status !== 429 && upstream.status !== 503) return null;
  const value = upstream.headers.get("retry-after");
  return retryAfterSeconds(value) === null ? null : value;
}
