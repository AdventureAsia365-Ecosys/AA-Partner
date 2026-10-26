// web/lib/bffBody.ts
// AA-945 — the BFF reads a raw upload (the mail history file) with a cap: it stops reading as soon as the body is
// larger than the route allows, so an oversized file never sits whole in the web server's memory.

/** The request body as bytes, or "too_large" as soon as more than `limit` bytes arrived (the rest is not read). */
export async function readCapped(
  body: ReadableStream<Uint8Array> | null,
  limit: number,
): Promise<Uint8Array<ArrayBuffer> | "too_large"> {
  if (!body) return new Uint8Array(0);
  const reader = body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > limit) {
      await reader.cancel().catch(() => undefined);
      return "too_large";
    }
    chunks.push(value);
  }
  const out = new Uint8Array(size);
  let at = 0;
  for (const c of chunks) {
    out.set(c, at);
    at += c.byteLength;
  }
  return out;
}
