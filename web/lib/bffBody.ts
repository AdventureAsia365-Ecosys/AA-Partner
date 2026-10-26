// web/lib/bffBody.ts
// AA-945 — the BFF reads a raw upload (the mail history file) with a cap: it stops reading as soon as the body is
// larger than the route allows, so an oversized file never sits whole in the web server's memory. The chunks go into
// a Blob (no second concatenated copy) and the Blob is the upstream fetch body (undici streams it with its
// Content-Length), so a 25 MiB upload is held about once, not three times.

/**
 * The request body as a Blob; "too_large" as soon as more than `limit` bytes arrived (the rest is not read);
 * "aborted" when the stream fails mid-way (the browser cancelled the upload, the connection dropped).
 */
export async function readCapped(
  body: ReadableStream<Uint8Array> | null,
  limit: number,
): Promise<Blob | "too_large" | "aborted"> {
  if (!body) return new Blob([]);
  const reader = body.getReader();
  const chunks: Uint8Array<ArrayBuffer>[] = [];
  let size = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > limit) {
        await reader.cancel().catch(() => undefined);
        return "too_large";
      }
      chunks.push(value as Uint8Array<ArrayBuffer>);
    }
  } catch {
    return "aborted";
  }
  return new Blob(chunks);
}
