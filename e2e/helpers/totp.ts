// e2e/helpers/totp.ts — AA-939: RFC 6238 one-time codes (30 s, 6 digits, SHA-1) for the partner pool's required
// authenticator. The secret is a live credential: never logged, never screenshotted.
import { createHmac } from "node:crypto";

const BASE32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";

export function base32Decode(secret: string): Buffer {
  const clean = secret.replace(/[\s=-]/g, "").toUpperCase();
  let bits = "";
  for (const ch of clean) {
    const v = BASE32.indexOf(ch);
    if (v < 0) throw new Error("not a base32 secret");
    bits += v.toString(2).padStart(5, "0");
  }
  const bytes = [];
  for (let i = 0; i + 8 <= bits.length; i += 8) bytes.push(parseInt(bits.slice(i, i + 8), 2));
  return Buffer.from(bytes);
}

export function totp(secret: string, at: number = Date.now(), step = 30): string {
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(Math.floor(at / 1000 / step)));
  const mac = createHmac("sha1", base32Decode(secret)).update(counter).digest();
  const offset = mac[mac.length - 1] & 0x0f;
  const code = (mac.readUInt32BE(offset) & 0x7fffffff) % 1_000_000;
  return code.toString().padStart(6, "0");
}

/** A code with at least `margin` seconds left in its window (waits for the next window otherwise). */
export async function freshTotp(secret: string, margin = 5): Promise<string> {
  const left = 30 - (Math.floor(Date.now() / 1000) % 30);
  if (left < margin) await new Promise((r) => setTimeout(r, left * 1000 + 200));
  return totp(secret);
}
