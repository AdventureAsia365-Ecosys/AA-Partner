// AA-1185 (S295 security audit SA-73): public/.well-known/security.txt (RFC 9116) is served on every host of the web
// without a session (partner, client and academy hosts), and the file is valid and not expired.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { NextRequest } from "next/server";
import { beforeEach, describe, expect, it } from "vitest";
import { ACADEMY_EXTRA_PATHS, CLIENT_PATH_PREFIXES, PUBLIC_PARTNER_PATHS, SECURITY_TXT_PATH } from "@/lib/hosts";
import { gate } from "@/proxy";
import { testConfig } from "./helpers";

const TEXT = readFileSync(join(__dirname, "..", "public", ".well-known", "security.txt"), "utf8");

function req(url: string): NextRequest {
  const headers = new Headers();
  headers.set("host", new URL(url).host);
  return new NextRequest(url, { headers });
}

function fields(text: string): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  for (const line of text.split("\n")) {
    if (!line.trim() || line.startsWith("#")) continue;
    const m = /^([A-Za-z-]+): (.+)$/.exec(line);
    expect(m, `not a "Field: value" line: ${line}`).not.toBeNull();
    (out[m![1].toLowerCase()] ??= []).push(m![2]);
  }
  return out;
}

beforeEach(() => {
  testConfig(); // the test hosts (partner / go / academy .example.test) in the environment webConfig() reads
});

describe("security.txt", () => {
  it("is public on all three host kinds", () => {
    expect(SECURITY_TXT_PATH).toBe("/.well-known/security.txt");
    expect(CLIENT_PATH_PREFIXES).toContain(SECURITY_TXT_PATH);
    expect(PUBLIC_PARTNER_PATHS).toContain(SECURITY_TXT_PATH);
    expect(ACADEMY_EXTRA_PATHS).toContain(SECURITY_TXT_PATH);
    expect(gate(req("https://partner.example.test/.well-known/security.txt")).headers.get("x-middleware-next")).toBe("1");
    expect(gate(req("https://go.example.test/.well-known/security.txt")).headers.get("x-middleware-next")).toBe("1");
    // Nothing else under /.well-known/ is opened by it.
    expect(gate(req("https://go.example.test/.well-known/other")).headers.get("x-middleware-next")).toBeNull();
  });

  it("has Contact and one future Expires at most a year ahead (refresh it yearly)", () => {
    const f = fields(TEXT);
    expect(f.contact?.length).toBeGreaterThanOrEqual(1);
    for (const c of f.contact) expect(c).toMatch(/^(mailto:[^@\s]+@[^@\s]+|https:\/\/\S+)$/);
    expect(f.expires).toHaveLength(1);
    const expires = Date.parse(f.expires[0]);
    expect(expires).toBeGreaterThan(Date.now());
    expect(expires - Date.now()).toBeLessThanOrEqual(366 * 24 * 3600 * 1000);
    expect(TEXT.endsWith("\n") && !TEXT.includes("\r")).toBe(true);
  });
});
