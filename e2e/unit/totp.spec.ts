// e2e/unit/totp.spec.ts — AA-979 P2b phase 2: unit tests of the TOTP helper (helpers/totp.ts) that the phase 2 spec
// uses to enrol the invitee's authenticator. Pure (no browser, no network, no AWS): run with
//   npx playwright test -c playwright.unit.config.ts
// Vectors: RFC 6238 Appendix B (SHA-1 / SHA-256 / SHA-512, 8 digits, 30 s step, T0 = 0) and RFC 4648 §10 (base32).
import { expect, test } from "@playwright/test";
import { randomBytes } from "node:crypto";
import { base32Decode, freshTotp, parseSetupSecret, totp, totpFromKey, type TotpClock } from "../helpers/totp";

const SEED = {
  sha1: Buffer.from("12345678901234567890", "ascii"),
  sha256: Buffer.from("12345678901234567890123456789012", "ascii"),
  sha512: Buffer.from("1234567890123456789012345678901234567890123456789012345678901234", "ascii"),
} as const;

// RFC 6238 Appendix B: [time in seconds, SHA-1, SHA-256, SHA-512].
const RFC6238: [number, string, string, string][] = [
  [59, "94287082", "46119246", "90693936"],
  [1111111109, "07081804", "68084774", "25091201"],
  [1111111111, "14050471", "67062674", "99943326"],
  [1234567890, "89005924", "91819424", "93441116"],
  [2000000000, "69279037", "90698825", "38618901"],
  [20000000000, "65353130", "77737706", "47863826"],
];

// The SHA-1 seed in base32, as an authenticator setup page would show it.
const SHA1_SEED_B32 = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ";

test.describe("RFC 6238 Appendix B", () => {
  for (const [seconds, sha1, sha256, sha512] of RFC6238) {
    test(`T=${seconds}s`, () => {
      const at = seconds * 1000;
      expect(totpFromKey(SEED.sha1, at, { digits: 8 })).toBe(sha1);
      expect(totpFromKey(SEED.sha256, at, { digits: 8, algorithm: "sha256" })).toBe(sha256);
      expect(totpFromKey(SEED.sha512, at, { digits: 8, algorithm: "sha512" })).toBe(sha512);
      // The 6-digit code is the low 6 digits of the same truncated value (what Cognito and the apps use).
      expect(totp(SHA1_SEED_B32, at)).toBe(sha1.slice(-6));
    });
  }

  test("the code is the same across one 30 s step and changes at the boundary", () => {
    expect(totp(SHA1_SEED_B32, 60_000)).toBe(totp(SHA1_SEED_B32, 89_999));
    expect(totp(SHA1_SEED_B32, 89_999)).not.toBe(totp(SHA1_SEED_B32, 90_000));
  });

  test("digits outside 6..10 are refused", () => {
    expect(() => totpFromKey(SEED.sha1, 0, { digits: 5 })).toThrow();
    expect(() => totpFromKey(SEED.sha1, 0, { digits: 11 })).toThrow();
  });
});

test.describe("base32 (RFC 4648 §10)", () => {
  const vectors: [string, string][] = [
    ["", ""],
    ["f", "MY======"],
    ["fo", "MZXQ===="],
    ["foo", "MZXW6==="],
    ["foob", "MZXW6YQ="],
    ["fooba", "MZXW6YTB"],
    ["foobar", "MZXW6YTBOI======"],
  ];
  for (const [plain, encoded] of vectors) {
    test(`"${plain}"`, () => {
      expect(base32Decode(encoded).toString("ascii")).toBe(plain);
    });
  }

  test("lower case, spaces and hyphens between groups are tolerated", () => {
    expect(base32Decode("gezd gnbv-gy3t qojq gezd gnbv gy3t qojq")).toEqual(SEED.sha1);
  });

  test("a character outside the alphabet is refused", () => {
    expect(() => base32Decode("GEZDGNBV1")).toThrow(/base32/);
  });
});

test.describe("parseSetupSecret (managed login 'Show secret key')", () => {
  test("label, spaces, lower case and padding", () => {
    expect(parseSetupSecret("Secret key: gezd gnbv gy3t qojq gezd gnbv gy3t qojq")).toBe(SHA1_SEED_B32);
    expect(parseSetupSecret(`${SHA1_SEED_B32}====`)).toBe(SHA1_SEED_B32);
  });

  test("no secret in the text", () => {
    expect(parseSetupSecret("Secret key:")).toBeUndefined();
    // 24 letters, all in the base32 alphabet: shorter than a 128-bit secret (RFC 4226 R6), so not a secret.
    expect(parseSetupSecret("Scan the QR code with your app")).toBeUndefined();
    expect(parseSetupSecret("Secret key: GEZDGNBVGY3TQOJQ")).toBeUndefined(); // 80 bits
  });

  test("a 52-character secret (256 bits, what managed login shows) is taken whole", () => {
    const shown = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQGEZA";
    expect(parseSetupSecret(`Secret key: ${shown.match(/.{1,4}/g)!.join(" ")}`)).toBe(shown);
  });
});

test.describe("freshTotp (no code reused within a step, enough time left)", () => {
  function fakeClock(start: number): TotpClock & { slept: number[] } {
    let now = start;
    const slept: number[] = [];
    return {
      slept,
      now: () => now,
      sleep: async (ms: number) => {
        slept.push(ms);
        now += ms;
      },
    };
  }

  // A random secret per test, so the per-secret step record (a temp file) never carries over between tests or runs.
  const secret = () => base32Encode(randomBytes(20));

  test("returns the current step's code when it has the margin left", async () => {
    const s = secret();
    const clock = fakeClock(1_000 * 30_000 + 1_000); // 29 s left in the step
    expect(await freshTotp(s, 5, clock)).toBe(totp(s, clock.now()));
    expect(clock.slept).toEqual([]);
  });

  test("waits for the next step when too little time is left", async () => {
    const s = secret();
    const clock = fakeClock(2_000 * 30_000 + 27_000); // 3 s left < 5 s margin
    const code = await freshTotp(s, 5, clock);
    expect(clock.slept).toHaveLength(1);
    expect(Math.floor(clock.now() / 30_000)).toBe(2_001);
    expect(code).toBe(totp(s, clock.now()));
  });

  test("a second code for the same secret comes from the next step", async () => {
    const s = secret();
    const clock = fakeClock(3_000 * 30_000 + 1_000);
    const first = await freshTotp(s, 5, clock);
    const second = await freshTotp(s, 5, clock);
    expect(clock.slept).toHaveLength(1);
    expect(Math.floor(clock.now() / 30_000)).toBe(3_001);
    expect(second).toBe(totp(s, clock.now()));
    expect(first).toBe(totp(s, 3_000 * 30_000 + 1_000));
  });
});

function base32Encode(buf: Buffer): string {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = "";
  for (const b of buf) bits += b.toString(2).padStart(8, "0");
  let out = "";
  for (let i = 0; i < bits.length; i += 5) out += alphabet[parseInt(bits.slice(i, i + 5).padEnd(5, "0"), 2)];
  return out;
}
