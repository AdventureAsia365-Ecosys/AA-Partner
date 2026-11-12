// AA-1070 guard (S278 live check: Settings > Proposal lines answered 500 for everyone): `crmCall` matches its sub path
// against the server BFF allow-list as written, so a query string inside the path ("inclusion-items?archived=true")
// never matches `^inclusion-items$` and the server page throws. A query goes through `opts.query` (URLSearchParams).
// This test refuses a crmCall / crmGet whose sub path literal carries a "?" in any runtime source file.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test } from "vitest";

const ROOT = fileURLToPath(new URL("..", import.meta.url));
const SCAN = ["app", "components", "lib"];
const SKIP_DIRS = new Set(["node_modules", ".next", "_kit"]);
// crmCall<T>(cfg, session, "GET", "path?query" ...) or crmCall(cfg, session, "GET", `path?${x}` ...)
const CALL = /crmCall(?:<[^>]*>)?\(\s*[^,]+,\s*[^,]+,\s*"[A-Z]+",\s*(["'`])([^"'`]*)\1/g;

function sourceFiles(rel: string): string[] {
  const abs = path.join(ROOT, rel);
  if (!fs.existsSync(abs)) return [];
  if (fs.statSync(abs).isFile()) return [rel];
  const out: string[] = [];
  for (const entry of fs.readdirSync(abs, { withFileTypes: true })) {
    const child = path.join(rel, entry.name);
    if (entry.isDirectory()) {
      if (!SKIP_DIRS.has(entry.name)) out.push(...sourceFiles(child));
    } else if (/\.(ts|tsx)$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) {
      out.push(child);
    }
  }
  return out;
}

export function queryInPath(code: string): string[] {
  const bad: string[] = [];
  for (const m of code.matchAll(CALL)) if (m[2].includes("?")) bad.push(m[2]);
  return bad;
}

test("the detector catches a query in the sub path and accepts a clean one", () => {
  expect(queryInPath('crmCall<{ items: X[] }>(ctx.cfg, session, "GET", "inclusion-items?archived=true")')).toEqual([
    "inclusion-items?archived=true",
  ]);
  expect(queryInPath('crmCall<X>(cfg, session, "GET", "inclusion-items", { query })')).toEqual([]);
});

test("no crmCall puts a query string in its sub path (use opts.query)", () => {
  const offenders: string[] = [];
  for (const rel of SCAN.flatMap(sourceFiles)) {
    for (const p of queryInPath(fs.readFileSync(path.join(ROOT, rel), "utf8"))) offenders.push(`${rel}: ${p}`);
  }
  expect(offenders).toEqual([]);
});
