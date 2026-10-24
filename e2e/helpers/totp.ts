// e2e/helpers/totp.ts — AA-939: RFC 6238 one-time codes (30 s, 6 digits, SHA-1) for the partner pool's required
// authenticator. The secret is a live credential: never logged, never screenshotted.
import { createHash, createHmac } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const BASE32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
const STEP_MS = 30_000;

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

/**
 * The secret from the managed login (v2) authenticator setup page: the "Show secret key" control reveals
 * `<p class="qr-secret-key">Secret key: <52 base32 chars></p>`. Tolerates a label, spaces / hyphens between groups,
 * lower case and `=` padding; undefined when the text holds no base32 secret.
 */
export function parseSetupSecret(text: string): string | undefined {
  const value = text.replace(/^[^:]*:/, "").replace(/[\s-]/g, "").replace(/=+$/, "").toUpperCase();
  return /^[A-Z2-7]{16,128}$/.test(value) ? value : undefined;
}

// Cognito refuses a code it has already accepted (replay) for the same user within its 30 s step, so a second sign-in
// of the same user in the same step (invite.spec.ts then slice1.spec.ts sign the owner in seconds apart) must wait for
// the next step. The last step handed out is kept per secret in this module AND in a small file under the OS temp dir
// (keyed by a hash of the secret, never the secret itself): Playwright starts a new worker process after a failed test,
// which would lose module state.
const lastStep = new Map<string, number>();

function stepFile(secret: string): string {
  const key = createHash("sha256").update(`aa-e2e-totp:${secret.replace(/[\s=-]/g, "").toUpperCase()}`).digest("hex");
  return path.join(os.tmpdir(), `aa-e2e-totp-${key.slice(0, 16)}`);
}

function lastUsed(file: string): number {
  let fromFile = -1;
  try {
    fromFile = Number.parseInt(fs.readFileSync(file, "utf8"), 10);
  } catch {
    // no record yet
  }
  return Math.max(lastStep.get(file) ?? -1, Number.isFinite(fromFile) ? fromFile : -1);
}

function remember(file: string, step: number): void {
  lastStep.set(file, step);
  try {
    fs.writeFileSync(file, String(step), { mode: 0o600 });
  } catch {
    // module state still covers this worker
  }
}

export interface TotpClock {
  now(): number;
  sleep(ms: number): Promise<void>;
}

const realClock: TotpClock = { now: () => Date.now(), sleep: (ms) => new Promise((r) => setTimeout(r, ms)) };

/**
 * A code with at least `margin` seconds left in its window, from a step not handed out before for this secret (waits
 * for the next window otherwise). The step is recorded as used as soon as the code is returned.
 */
export async function freshTotp(secret: string, margin = 5, clock: TotpClock = realClock): Promise<string> {
  const file = stepFile(secret);
  for (;;) {
    const at = clock.now();
    const step = Math.floor(at / STEP_MS);
    const leftMs = (step + 1) * STEP_MS - at;
    if (step > lastUsed(file) && leftMs >= margin * 1000) {
      remember(file, step);
      return totp(secret, at);
    }
    await clock.sleep(leftMs + 200);
  }
}
