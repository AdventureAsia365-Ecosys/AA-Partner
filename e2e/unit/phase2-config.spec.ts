// e2e/unit/phase2-config.spec.ts — AA-979 P2b phase 2: the run's configuration guards (phase2/phase2-helpers.ts):
// the evidence folder never lands in a tracked part of the repo, and a staff mode without what it needs fails before
// any browser opens. Pure: no browser, no network.
import { expect, test } from "@playwright/test";
import os from "node:os";
import path from "node:path";
import { loadConfig, outDir } from "../phase2/phase2-helpers";

const E2E_DIR = path.resolve(__dirname, "..");

const BASE: Record<string, string> = {
  E2EP2PORTAL: "https://portal.example.test",
  E2EP2CISAPI: "https://api.example.test",
  E2EP2TENANT: "00000000-0000-4000-8000-000000000001",
  E2EP2INVITEE: "Someone@Example.test",
  E2EP2SINCE: "2026-10-26T08:00:00Z",
};

function withEnv<T>(vars: Record<string, string | undefined>, fn: () => T): T {
  const saved: Record<string, string | undefined> = {};
  const names = new Set([...Object.keys(vars), ...Object.keys(process.env).filter((k) => k.startsWith("E2EP2"))]);
  for (const k of names) {
    saved[k] = process.env[k];
    delete process.env[k];
    if (vars[k] !== undefined) process.env[k] = vars[k];
  }
  try {
    return fn();
  } finally {
    for (const k of names) {
      if (saved[k] === undefined) delete process.env[k];
      else process.env[k] = saved[k];
    }
  }
}

test("the evidence folder defaults to e2e/results/phase2 (gitignored)", () => {
  expect(outDir("")).toBe(path.join(E2E_DIR, "results", "phase2"));
});

test("a folder outside the repository is fine", () => {
  const out = path.join(os.tmpdir(), "aa979-phase2");
  expect(outDir(out)).toBe(out);
});

test("a tracked folder inside the repository is refused", () => {
  expect(() => outDir("phase2/evidence")).toThrow(/e2e\/results/);
  expect(() => outDir("../docs/evidence")).toThrow(/e2e\/results/);
  expect(() => outDir("results-not")).toThrow(/e2e\/results/);
});

test("a complete staff-mode-none configuration loads (address lower-cased)", () => {
  const cfg = withEnv(BASE, () => loadConfig());
  expect(cfg.staff).toBe("none");
  expect(cfg.role).toBe("viewer");
  expect(cfg.invitee).toBe("someone@example.test");
  expect(cfg.cf).toBe("");
  expect(cfg.since).toBe(Date.parse(BASE.E2EP2SINCE));
});

test("staff mode none needs E2EP2SINCE unless the person is reused", () => {
  expect(() => withEnv({ ...BASE, E2EP2SINCE: undefined }, () => loadConfig())).toThrow(/E2EP2SINCE/);
  expect(withEnv({ ...BASE, E2EP2SINCE: undefined, E2EP2REUSE: "1" }, () => loadConfig()).reuse).toBe(true);
});

test("staff modes browser / a10b need the admin origin", () => {
  expect(() => withEnv({ ...BASE, E2EP2STAFF: "browser" }, () => loadConfig())).toThrow(/E2EP2ADMIN/);
  expect(withEnv({ ...BASE, E2EP2STAFF: "browser", E2EP2ADMIN: "https://admin.example.test" }, () => loadConfig()).admin).toBe(
    "https://admin.example.test",
  );
});

test("bad values are refused", () => {
  expect(() => withEnv({ ...BASE, E2EP2STAFF: "root" }, () => loadConfig())).toThrow(/E2EP2STAFF/);
  expect(() => withEnv({ ...BASE, E2EP2ROLE: "admin" }, () => loadConfig())).toThrow(/E2EP2ROLE/);
  expect(() => withEnv({ ...BASE, E2EP2TENANT: "acme" }, () => loadConfig())).toThrow(/E2EP2TENANT/);
  expect(() => withEnv({ ...BASE, E2EP2PORTAL: "http://portal.example.test" }, () => loadConfig())).toThrow(/E2EP2PORTAL/);
  expect(() => withEnv({ ...BASE, E2EP2CF: "d1.cloudfront.net --no-sandbox" }, () => loadConfig())).toThrow(/E2EP2CF/);
});
